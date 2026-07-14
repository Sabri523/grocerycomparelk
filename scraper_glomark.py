"""
Glomark.lk scraper (v3 — discovers real category pages automatically)
------------------------------------------------------------------------
Two issues fixed from earlier versions:

1. Glomark's category pages only render a first batch of products
   (~18-20) up front, then load more only when you click the
   "Show More..." button — not a separate URL you can page through. This
   script clicks that button repeatedly (with proper waiting, since the
   new batch renders with a short delay) until nothing new appears.

2. The department-level "dp/<id>" URLs (e.g. glomark.lk/grocery/dp/15)
   are NOT full category listings — they're landing pages made entirely
   of promo carousels ("Our Promotions", "New Arrivals", "Most Popular").
   There is no real product grid on them at all. The real grids with
   "Show More" live one level down, at category URLs like
   glomark.lk/fresh/vegetable/c/145. So instead of hardcoding department
   pages, this script loads Glomark's nav menu (which is present in the
   plain HTML, no JS needed) and pulls out every real ".../c/<id>"
   category link automatically.

BEFORE RUNNING THIS AT ANY SCALE:
  - Read https://glomark.lk/terms-and-conditions and robots.txt
    (https://glomark.lk/robots.txt) and make sure bulk scraping is allowed
    for your use case.
  - Keep concurrency at 1 and keep the delays below — clicking "Show More"
    repeatedly, across many categories, is still real traffic against
    their site. This will take a while for a full-catalog run.
  - This is for personal / research price comparison, not resale or
    republishing their catalog.

Usage:
    pip install playwright beautifulsoup4
    playwright install chromium
    python scraper_glomark.py

Output:
    glomark_prices.json  ->  [{"name": ..., "price": ..., "url": ..., "category": ...}, ...]
"""

import json
import re
import time
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# Set to True to discover and scrape every category from Glomark's own nav
# menu. Set to False and fill in MANUAL_CATEGORY_PAGES to scrape only
# specific categories (much faster, good for testing).
DISCOVER_ALL_CATEGORIES = True

MANUAL_CATEGORY_PAGES = {
    "Vegetable": "https://glomark.lk/fresh/vegetable/c/145",
}

CATEGORY_LINK_RE = re.compile(r"/c/\d+$")
PRODUCT_LINK_RE = re.compile(r"/p/\d+$")
PRICE_RE = re.compile(r"Rs\s?([\d,]+(?:\.\d{1,2})?)")

# Below the actual category grid, Glomark renders extra widgets ("Our
# Promotions", "New Arrivals", "Most Popular") that also contain /p/
# product links. These markers let us stop collecting once we hit one of
# those sections, so we don't mix unrelated promo items into the category.
STOP_SECTION_MARKERS = {
    "our promotions", "new arrivals", "most popular",
    "customer needs", "best seller",
}

MAX_CLICKS = 100          # safety cap so a stuck page can't loop forever
SHOW_MORE_TEXT = "Show More"


def discover_categories(page):
    """Load the homepage and pull out every real category listing link
    (".../c/<id>") from the nav menu — these are the pages that actually
    have a product grid, unlike the "dp/<id>" department landing pages."""
    page.goto("https://glomark.lk/", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1000)

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    categories = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not CATEGORY_LINK_RE.search(href):
            continue
        full_url = href if href.startswith("http") else f"https://glomark.lk{href}"
        label = a.get_text(" ", strip=True) or full_url.rstrip("/").split("/")[-2]
        label = re.sub(r"^All\s+", "", label).strip()  # nav text is often "All Vegetable"
        categories[label] = full_url  # dict dedupes repeated links automatically

    return categories


def load_all_products(page, url):
    """Load a category page and keep clicking 'Show More' until it stops
    adding new products (or disappears, or we hit the safety cap).

    Glomark's "Show More" click doesn't render the new batch instantly —
    there's a brief delay while it fetches/renders. So after each click we
    wait, then double-check the count again after a further pause before
    concluding nothing new arrived (rather than trusting a single, possibly
    premature, read)."""
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1500)

    def product_count():
        return len(page.query_selector_all("a[href*='/p/']"))

    clicks = 0
    while clicks < MAX_CLICKS:
        show_more = page.get_by_text(SHOW_MORE_TEXT, exact=False).first
        if not show_more or not show_more.is_visible():
            break

        count_before = product_count()

        try:
            show_more.scroll_into_view_if_needed()
            show_more.click(timeout=5000)
        except Exception:
            break  # button not clickable anymore -> assume end of list

        clicks += 1

        # Let the new batch load. Try to wait for network activity to
        # settle, then poll a few times in case rendering lags behind the
        # network response.
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        grew = False
        for _ in range(6):  # poll for up to ~6 seconds after the click
            page.wait_for_timeout(1000)
            if product_count() > count_before:
                grew = True
                break

        if not grew:
            # One more, longer grace wait in case the site is just slow,
            # before we accept that this really is the end of the list.
            page.wait_for_timeout(3000)
            if product_count() <= count_before:
                break

        print(f"    click {clicks}: {count_before} -> {product_count()} items")

    return page.content()


def parse_products(html, category_name):
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # Walk every element in document order. Once we hit a heading/section
    # whose text matches a "stop marker" (a promo widget below the real
    # grid), ignore every product link from that point on.
    stopped = False
    for tag in soup.descendants:
        if stopped:
            break
        if not hasattr(tag, "name") or tag.name is None:
            continue

        # Section headings are usually short standalone text in a heading
        # or div/span — check short exact-ish matches to avoid false hits
        # on longer strings that merely contain one of these words.
        text = tag.get_text(" ", strip=True)
        if text and len(text) < 40 and text.lower() in STOP_SECTION_MARKERS:
            stopped = True
            break

        if tag.name == "a" and tag.get("href") and PRODUCT_LINK_RE.search(tag["href"]):

            # The product information is stored in the parent product-caption
            product_card = tag.find_parent("div", class_="product-caption")

            if not product_card:
                continue

            # Product name is inside the <a>
            name_part = tag.get_text(" ", strip=True)

            #if name ends with G then it has grams in the name, so remove the mass. Same for kg and bulk keywords
            if name_part.endswith("G"):
                name_part = name_part.rsplit(' ', 1)[0]
            if name_part.endswith("Kg"):
                name_part = name_part.rsplit(' ', 1)[0]
            if name_part.endswith("Bulk"):
                name_part = name_part.rsplit(' ', 2)[0]
            if name_part.endswith("Ml"):
                name_part = name_part.rsplit(' ', 1)[0]

            # Change name to Title case from full upper or full lower
            name_part = name_part.title()

            # Price is outside the <a>
            price_element = product_card.find("div", class_="price")

            if not price_element:
                continue

            price_text = price_element.get_text(" ", strip=True)

            price_match = PRICE_RE.search(price_text)

            if not price_match:
                continue

            price = float(price_match.group(1).replace(",", ""))

            href = tag["href"]

            items.append({
                "name": name_part,
                "price": price,
                "url": href if href.startswith("http") else f"https://glomark.lk{href}",
                "category": category_name,
            })

    # De-duplicate (product tiles link the image AND the name to the same URL)
    seen = set()
    deduped = []
    for item in items:
        key = (item["name"], item["url"])
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def main():
    all_items = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        if DISCOVER_ALL_CATEGORIES:
            print("Discovering category pages from Glomark's nav menu...")
            category_pages = discover_categories(page)
            print(f"  found {len(category_pages)} categories")
            if not category_pages:
                print("  discovery found nothing — falling back to MANUAL_CATEGORY_PAGES")
                category_pages = MANUAL_CATEGORY_PAGES
        else:
            category_pages = MANUAL_CATEGORY_PAGES

        for name, url in category_pages.items():
            print(f"Scraping {name} ({url}) ...")
            try:
                html = load_all_products(page, url)
                items = parse_products(html, name)
                if not items:
                    debug_path = f"glomark_debug_{name.replace(' ', '_')}.html"
                    with open(debug_path, "w", encoding="utf-8") as f:
                        f.write(html)
                    print(f"  found 0 products — saved rendered HTML to {debug_path} for inspection")
                print(f"  found {len(items)} products")
                all_items.extend(items)
            except Exception as e:
                print(f"  failed: {e}")
            time.sleep(2)  # be polite between category pages
        browser.close()

    with open("glomark_prices.json", "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)

    print(f"\nSaved {len(all_items)} items to glomark_prices.json")


if __name__ == "__main__":
    main()