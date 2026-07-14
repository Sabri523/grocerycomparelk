"""
Cargills Online scraper
------------------------
Unlike Glomark, Cargills Online's product listing pages ship an Angular
template — the raw HTML contains literal placeholders like
"{{product.ItemName}}" and "Rs. {{product.Price}}" that only get filled in
by JavaScript after the page loads. Plain `requests` will NOT see any
prices here — you need a real (headless) browser to run the page's JS.

BEFORE RUNNING THIS AT ANY SCALE:
  - Read https://cargillsonline.com/TermConditions and robots.txt
    (https://cargillsonline.com/robots.txt) and make sure this use is allowed.
  - Keep concurrency at 1 and add delays — this drives a real browser
    session against their site, so be extra polite with request rates.
  - This is for personal / research price comparison, not resale or
    republishing their catalog.

Usage:
    pip install playwright beautifulsoup4
    playwright install chromium
    python scraper_cargills.py

Output:
    cargills_prices.json  ->  [{"name": ..., "price": ..., "quantity": ..., "category": ...}, ...]

Note on pagination:
  Earlier versions tried scrolling + clicking "load more" style buttons,
  assuming products get appended to the same page (like Glomark's "Show
  More"). That only ever found ~20 items per category, which means
  Cargills instead REPLACES the product list per page — i.e. real
  pagination (page numbers and/or a "Next" control), not an appending
  "show more". This version pages through explicitly: scrape the current
  page, click "Next" (trying several common label/selector variants),
  wait for the product set to actually change, and repeat — accumulating
  items across every page until "Next" is gone/disabled or the product
  set stops changing.

  If a category still comes back with only ~20 items, open its
  cargills_debug_<category>.html (saved automatically when a category
  looks suspiciously small) and search for "Next" or "pagination" to see
  what control is actually there, so the selectors below can be adjusted.

Note on quantity:
  The pack size (e.g. "500.0 g") is rendered in a <button class="dropbtn1
  ...">, close to the product's name/price link but NOT inside any
  specially-named wrapper div (an earlier version guessed a "div.veg"
  container that turned out not to exist, so it silently found nothing).
  This scraper instead walks up a few ancestor levels from the anchor
  itself and grabs the first "dropbtn1" button it finds — separate from
  the name, into its own "quantity" field. match_products.py uses this
  field directly (falling back to parsing the name only if it's missing).
"""

import json
import re
import time
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# Set to True to scrape every category found in the site's own nav menu.
# Set to False and edit MANUAL_CATEGORY_PAGES below to scrape only specific
# categories (faster, useful for testing).
DISCOVER_ALL_CATEGORIES = True

MANUAL_CATEGORY_PAGES = {
    "Fruits": "https://cargillsonline.com/Product/Fruits?IC=OQ%3D%3D&NC=RnJ1aXRz",
}

PRICE_RE = re.compile(r"Rs\.?\s?([\d,]+(?:\.\d{1,2})?)")

# Candidate ways "Next page" might be exposed. Tried in order on every page.
NEXT_TEXT_CANDIDATES = ["Next", "next", ">", "»", "Next Page", "Next »"]
NEXT_SELECTOR_CANDIDATES = [
    "[aria-label='Next']",
    "[aria-label='next']",
    "a.next", "li.next a", "button.next",
    ".pagination .next", ".pagination-next",
]

MAX_PAGES_PER_CATEGORY = 200  # safety cap


def discover_categories(page):
    """Load the homepage, let Angular render the nav menu, and pull out
    every '/Product/<category>?IC=...&NC=...' link it contains."""
    page.goto("https://cargillsonline.com/Index", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2500)

    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    categories = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/Product/" not in href or "IC=" not in href:
            continue
        full_url = href if href.startswith("http") else f"https://cargillsonline.com{href}"
        label = a.get_text(" ", strip=True) or full_url.split("/Product/")[-1].split("?")[0]
        categories[label] = full_url  # dict dedupes repeated links automatically

    return categories


def extract_id(href):
    try:
        qs = parse_qs(urlparse(href).query)
        return qs.get("ID", [None])[0]
    except Exception:
        return None


def find_quantity_near(anchor, max_levels=6):
    """Pull the pack-size text out of the quantity dropdown button
    (e.g. '500.0 g') near a product's name/price anchor.

    Earlier versions assumed the anchor sat inside a wrapping
    'div.veg' card and searched within that — but that class was an
    unverified guess and doesn't actually exist, so the search silently
    found nothing. Looking at the real rendered HTML, the quantity
    button is a close sibling of the <a> tag (often just one parent up,
    separated only by an Angular "<!--ngIf-->" comment), not nested
    inside some specially-named card. So instead this walks upward from
    the anchor itself, checking each ancestor level for a
    'dropbtn1' button, and stops at the first one found — rather than
    requiring a specific container class we can't verify in advance."""
    node = anchor
    for _ in range(max_levels):
        node = node.parent
        if node is None:
            break
        button = node.find("button", class_=lambda c: c and "dropbtn1" in c.split())
        if button:
            text = button.get_text(" ", strip=True)
            if text:
                return text
    return None


def parse_current_page(html):
    """Parse whatever products are currently rendered on the page into
    {pid: {"name": ..., "price": ..., "quantity": ...}}. Does NOT
    accumulate across pages — that happens in scrape_category."""
    soup = BeautifulSoup(html, "html.parser")

    # temporarily removes discount div (avoids picking up promo/MRP text
    # as if it were the real price or name)
    for element in soup.find_all(class_="dis"):
        element.decompose()

    anchors = soup.find_all("a", href=lambda h: h and "ProductDetails" in h)

    by_id = {}
    for a in anchors:
        pid = extract_id(a["href"])
        if not pid:
            continue
        text = a.get_text(" ", strip=True)
        by_id.setdefault(pid, {"name_parts": [], "price_parts": [], "quantity": None})

        if PRICE_RE.search(text):
            by_id[pid]["price_parts"].append(text)
        elif text:
            by_id[pid]["name_parts"].append(text)

        # Look for the quantity dropdown button near this anchor (see
        # find_quantity_near's docstring for why this walks up from the
        # anchor itself rather than searching within a named card).
        if by_id[pid]["quantity"] is None:
            qty = find_quantity_near(a)
            if qty:
                by_id[pid]["quantity"] = qty

    page_items = {}
    for pid, parts in by_id.items():
        if not parts["name_parts"] or not parts["price_parts"]:
            continue
        item_name = max(parts["name_parts"], key=len).title()  # longest candidate text
        price_match = PRICE_RE.search(parts["price_parts"][0])
        if not price_match:
            continue
        price = float(price_match.group(1).replace(",", ""))
        page_items[pid] = {"name": item_name, "price": price, "quantity": parts["quantity"]}

    return page_items


def click_next(page):
    """Try every known way of finding a 'Next page' control and click it.
    Returns True if something was clicked, False if no Next control was
    found (i.e. we're on the last page)."""
    for selector in NEXT_SELECTOR_CANDIDATES:
        try:
            el = page.locator(selector).first
            if el and el.is_visible() and el.is_enabled():
                el.scroll_into_view_if_needed()
                el.click(timeout=3000)
                return True
        except Exception:
            continue

    for text in NEXT_TEXT_CANDIDATES:
        try:
            el = page.get_by_text(text, exact=True).first
            if el and el.is_visible():
                el.scroll_into_view_if_needed()
                el.click(timeout=3000)
                return True
        except Exception:
            continue

    return False


def scrape_category(page, name, url, save_debug_html=False):
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2000)

    all_items_by_id = {}
    last_debug_html = None

    for page_num in range(1, MAX_PAGES_PER_CATEGORY + 1):
        html = page.content()
        last_debug_html = html
        page_items = parse_current_page(html)

        new_count = sum(1 for pid in page_items if pid not in all_items_by_id)
        all_items_by_id.update(page_items)

        print(f"    page {page_num}: {len(page_items)} on page, {new_count} new, {len(all_items_by_id)} total")

        # If a page yields nothing new at all, assume we've looped or
        # reached the end — don't keep clicking forever.
        if page_num > 1 and new_count == 0:
            break

        clicked = click_next(page)
        if not clicked:
            break

        # Wait for the product set to actually change before parsing again
        # (Angular needs a moment to swap in the next page's data).
        page.wait_for_timeout(1500)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(1000)

    if save_debug_html and last_debug_html:
        debug_path = f"cargills_debug_{name.replace(' ', '_')}.html"
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(last_debug_html)
        print(f"  saved last rendered page HTML to {debug_path} for inspection")

    items = [
        {"name": v["name"], "price": v["price"], "quantity": v.get("quantity"), "category": name}
        for v in all_items_by_id.values()
    ]
    return items


def main():
    all_items = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        if DISCOVER_ALL_CATEGORIES:
            print("Discovering categories from the homepage nav menu...")
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
                items = scrape_category(page, name, url)
                if len(items) <= 20:
                    # Suspiciously small — save debug HTML so we can check
                    # whether pagination actually advanced at all.
                    print(f"  only found {len(items)} — saving debug HTML just in case...")
                    items = scrape_category(page, name, url, save_debug_html=True)
                print(f"  found {len(items)} products")
                all_items.extend(items)
            except Exception as e:
                print(f"  failed: {e}")
            time.sleep(3)  # be polite between categories
        browser.close()

    with open("cargills_prices.json", "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)

    have_qty = sum(1 for i in all_items if i.get("quantity"))
    print(f"\nSaved {len(all_items)} items to cargills_prices.json ({have_qty} with a quantity found)")


if __name__ == "__main__":
    main()