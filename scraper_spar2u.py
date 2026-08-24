"""
SPAR2U (spar2u.lk) scraper
--------------------------
Unlike Cargills, Glomark, and Keells, this one does NOT need Playwright /
a headless browser at all. spar2u.lk is a standard Shopify store, and its
collection (category) and product-listing pages are fully server-rendered
Liquid templates — fetching the plain HTML (no JS execution) already
contains every product's name, price, and link. Confirmed by fetching
https://spar2u.lk/collections/beverages directly: all 20-ish product
cards on the page were present in the raw response, plus real
pagination via a query string (?page=2, ?page=3, ..., no "click Next"
button/JS needed).

Card structure (confirmed via devtools screenshot):

    <h3 class="card__heading">
      <a class="full-unstyled-link" href="/products/anchor-full-cream-milk-powder-400g">
        ANCHOR Full Cream Milk Powder, 400g
      </a>
    </h3>
    <div class="card-information">
      <div class="price ">
        <div class="price__container">
          <div class="price_regular">...</div>
          <div class="price_sale">
            <span class="visually-hidden">Regular price</span>
            <span><s class="price-item price-item--regular">Rs 5,300.00</s></span>
            <span class="visually-hidden">Sale price</span>
            <span class="price-item price-item--sale price-item--last">Rs 4,300.00</span>
          </div>
        </div>
      </div>
    </div>

  - Name + product URL: the <a> inside the h3.card__heading. Note: the
    class is "card__heading" with a DOUBLE underscore — the devtools
    screenshot this was originally built from rendered as a single
    underscore, which turned out to be a copy/rendering artifact, not
    the real class name. Confirmed against an actual debug HTML dump
    after the first version came back with 0 products found.
  - Quantity: usually trails the name after a comma, e.g. "SUSTAGEN
    Vanilla, 400g" or "SUNQUICK Orange,700ml" (comma with no space, seen
    on a real product) — split_trailing_quantity() below handles both.
  - Price: always read from span.price-item--sale (inside div.price_sale)
    — this is the theme's "current/effective" price whether or not the
    item is actually discounted (a non-sale item just has the same value
    in both the struck-through price-item--regular and price-item--sale
    spans). div.price_regular is a separate, seemingly-unused sibling
    block for this theme/site and is ignored.
  - Multi-variant products (e.g. "BIG Onions" with several pack sizes)
    show "From Rs 82.50" — the regex below just extracts the number after
    "Rs", so this naturally captures the lowest variant price shown on
    the card, same as what a shopper sees before clicking in.

Categories (per instructions) come from the FOOTER's "SHOP BY CATEGORY"
block specifically — confirmed via devtools screenshot:

    <ul class="footer-block__details-content list-unstyled">
      <li><a href="/collections/beverages">Beverages</a></li>
      <li><a href="/collections/grocery">Grocery</a></li>
      <li><a href="/collections/health-beauty">Health & Beauty</a></li>
      <li><a href="/collections/household">Household</a></li>
      <li><a href="/collections/offers-promotions">Offers & Promotions</a></li>
    </ul>

These are real <a href> links (unlike Keells), so no clicking is needed
here either — just parse them out of the homepage HTML. Note the footer
also has OTHER link lists with the same classes (e.g. a "USE FULL LINKS"
block with Contact Us / Delivery Information / Privacy Policy), so this
specifically finds the block whose heading mentions "category" rather
than just grabbing the first matching <ul> on the page.

BEFORE RUNNING THIS AT ANY SCALE:
  - Read https://spar2u.lk's terms of service and robots.txt
    (https://spar2u.lk/robots.txt) and make sure this use is allowed.
  - Keep concurrency at 1 and keep the delays below — even without a
    browser, this is still real traffic against their site.
  - This is for personal / research price comparison, not resale or
    republishing their catalog.

IMPORTANT — untested against the live site: I fetched a couple of pages
of spar2u.lk directly to confirm the structure above, but I can't run
actual HTTP requests against spar2u.lk from this sandbox (network is
restricted to a small allowlist that doesn't include it), so the
`requests.get(...)` calls themselves are unverified — only the parsing
logic has been tested, against synthetic HTML built from the real
fetched structure. Run this yourself and let me know what happens; if a
category or the footer comes back empty, it'll dump debug HTML the same
way the other scrapers do, so we can see what actually came back.

Usage:
    pip install requests beautifulsoup4
    python scraper_spar2u.py

Output:
    spar2u_prices.json  ->  [{"name": ..., "price": ..., "quantity": ..., "url": ..., "category": ...}, ...]
"""

import json
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://spar2u.lk"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

REQUEST_DELAY_SECONDS = 1.5  # politeness delay between page requests
MAX_PAGES_PER_CATEGORY = 60  # safety cap (424 products / ~20 per page ≈ 22 pages seen for one category)

PRICE_RE = re.compile(r"Rs\.?\s?([\d,]+(?:\.\d{1,2})?)")

# Pack-size/volume trailing the product name, optionally after a comma
# and with or without a space — both "Name, 400g" and "Name,700ml" (no
# space) have been seen on real product titles on this site.
TRAILING_QTY_RE = re.compile(
    r"^(?P<name>.+?),?\s*(?P<qty>\d+(?:\.\d+)?\s?(?:g|kg|ml|l|pcs?|pack|ea)\.?)$",
    re.I,
)

NAME_HEADING_SELECTOR = "[class*='card__heading']"
PRICE_SALE_SELECTOR = "[class*='price-item--sale']"
FOOTER_LIST_SELECTOR = "ul[class*='footer-block__details-content']"


def get_soup(url, params=None):
    resp = requests.get(url, headers=HEADERS, params=params, timeout=20)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser"), resp.text


def split_trailing_quantity(name):
    """If the name ends with a pack-size/volume pattern (optionally after
    a comma, e.g. 'SUSTAGEN Vanilla, 400g' or 'SUNQUICK Orange,700ml'),
    split it off. Returns (clean_name, quantity_or_none)."""
    match = TRAILING_QTY_RE.match(name.strip())
    if match:
        return match.group("name").strip().rstrip(","), match.group("qty").strip()
    return name.strip(), None


def discover_categories():
    """Fetch the homepage and pull categories from the footer's
    "SHOP BY CATEGORY" block specifically (not the larger nav-menu
    subcategory tree, and not the footer's other link lists like
    "USE FULL LINKS" which share the same CSS classes)."""
    soup, html = get_soup(BASE_URL)

    target_list = None
    for heading in soup.find_all(class_=lambda c: c and "footer-block__heading" in c.split()):
        if "category" in heading.get_text(" ", strip=True).lower():
            container = heading.find_parent(class_=lambda c: c and "footer-block" in c.split()) or heading.parent
            target_list = container.select_one(FOOTER_LIST_SELECTOR) if container else None
            if target_list:
                break

    # Fallback: if no heading matched "category" (wording might differ),
    # try every footer link list and keep whichever one's links are all
    # "/collections/..." URLs — a decent heuristic since the other footer
    # lists (contact/delivery/privacy) are "/pages/..." links instead.
    if target_list is None:
        for candidate in soup.select(FOOTER_LIST_SELECTOR):
            hrefs = [a.get("href", "") for a in candidate.find_all("a", href=True)]
            if hrefs and all("/collections/" in h for h in hrefs):
                target_list = candidate
                break

    if target_list is None:
        print("  couldn't find the footer 'SHOP BY CATEGORY' block — dumping homepage HTML for inspection")
        with open("spar2u_debug_homepage.html", "w", encoding="utf-8") as f:
            f.write(html)
        return {}

    categories = {}
    for a in target_list.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        href = a["href"]
        if not label or not href:
            continue
        categories[label] = urljoin(BASE_URL, href)

    return categories


def resolve_full_name(heading, link_text):
    """The site truncates long product titles in the card's visible link
    text with a literal '...' (confirmed from a real debug dump — e.g.
    'MALIBAN Full Cream Milk Powder Pouch,...', which also swallows the
    trailing pack size). The product image's alt attribute nearby has the
    full, untruncated name instead. Walk up a few ancestor levels from
    the heading looking for an <img alt="..."> to use instead — but only
    when it looks like the same product (same ~15-char prefix) and is
    actually more complete, so an unrelated image's alt text can't get
    grabbed by mistake.

    Note: unlike the price search below, this does NOT stop early when
    an ancestor contains more than one product heading. The site renders
    two duplicate card structures per product (confirmed: exactly double
    the heading count vs. unique product URLs on a page — likely a
    mobile/desktop or quick-add variant pair), and the second copy's own
    image sits behind an ancestor that already contains both copies'
    headings. Stopping there would mean the second copy never finds its
    image and stays truncated. The alt-text prefix match is enough of a
    safety net here — a wrong-product img would very rarely share the
    first 15 characters of this heading's own (truncated) text."""
    stripped_link = link_text.rstrip(".").strip()
    node = heading
    for _ in range(8):
        parent = node.parent
        if parent is None:
            break
        img = parent.find("img", alt=True)
        if img:
            alt_text = (img.get("alt") or "").strip()
            if alt_text and alt_text.startswith(stripped_link[:15]):
                if link_text.endswith("...") or len(alt_text) > len(link_text):
                    return alt_text
        node = parent
    return link_text


def parse_products(html, category_name):
    soup = BeautifulSoup(html, "html.parser")
    items = {}

    for heading in soup.select(NAME_HEADING_SELECTOR):
        link = heading.find("a", href=True)
        if not link:
            continue
        raw_name = link.get_text(" ", strip=True)
        if not raw_name:
            continue
        raw_name = resolve_full_name(heading, raw_name)
        href = link["href"]
        url = urljoin(BASE_URL, href)

        # The price sits in a sibling structure below the heading, inside
        # the same product-card container. Walk up a few ancestor levels
        # from the heading (guarding against crossing into a container
        # that holds more than one product's heading) to find it, rather
        # than assuming a fixed nesting depth.
        price_span = None
        node = heading
        for _ in range(6):
            price_span = node.find(class_=lambda c: c and "price-item--sale" in c.split())
            if price_span:
                break
            parent = node.parent
            if parent is None:
                break
            if len(parent.select(NAME_HEADING_SELECTOR)) > 1:
                break  # widened into a container with multiple products
            node = parent

        if not price_span:
            continue
        price_match = PRICE_RE.search(price_span.get_text(" ", strip=True))
        if not price_match:
            continue
        price = float(price_match.group(1).replace(",", ""))

        clean_name, quantity = split_trailing_quantity(raw_name)

        # The site renders two duplicate card structures per product
        # (same URL, same price) — resolve_full_name() usually recovers
        # the full name for both, but as a defense-in-depth fallback,
        # if a duplicate ever DOES still come through truncated, don't
        # let it overwrite an already-good, more complete entry for the
        # same URL just because it happened to be parsed second.
        existing = items.get(url)
        if existing and len(clean_name) <= len(existing["name"]):
            continue

        items[url] = {
            "name": clean_name,
            "price": price,
            "quantity": quantity,
            "url": url,
            "category": category_name,
        }

    return items


def scrape_category(name, url):
    all_items = {}
    last_html = None

    for page_num in range(1, MAX_PAGES_PER_CATEGORY + 1):
        soup_unused, html = get_soup(url, params={"page": page_num})
        last_html = html
        page_items = parse_products(html, name)

        new_count = sum(1 for k in page_items if k not in all_items)
        all_items.update(page_items)

        print(f"    page {page_num}: {len(page_items)} on page, {new_count} new, {len(all_items)} total")

        # No products at all on this page (or nothing new, past page 1)
        # means we've run past the end of the category.
        if len(page_items) == 0 or (page_num > 1 and new_count == 0):
            break

        time.sleep(REQUEST_DELAY_SECONDS)

    if len(all_items) == 0 and last_html:
        debug_path = f"spar2u_debug_{name.replace(' ', '_').replace('&', 'and')}.html"
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(last_html)
        print(f"  found 0 products — saved rendered HTML to {debug_path} for inspection")

    return list(all_items.values())


def main():
    print("Discovering categories from the footer's 'SHOP BY CATEGORY' block...")
    categories = discover_categories()
    print(f"  found {len(categories)} categories: {list(categories.keys())}")

    if not categories:
        print("No categories found — see spar2u_debug_homepage.html. Stopping.")
        return

    all_items = []
    for name, url in categories.items():
        print(f"Scraping {name} ({url}) ...")
        try:
            items = scrape_category(name, url)
            print(f"  found {len(items)} products")
            all_items.extend(items)
        except Exception as e:
            print(f"  failed: {e}")
        time.sleep(REQUEST_DELAY_SECONDS)

    with open("spar2u_prices.json", "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)

    have_qty = sum(1 for i in all_items if i.get("quantity"))
    print(f"\nSaved {len(all_items)} items to spar2u_prices.json ({have_qty} with a quantity found)")


if __name__ == "__main__":
    main()