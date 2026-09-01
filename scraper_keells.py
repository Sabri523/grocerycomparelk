"""
Keells Online (keellssuper.com) scraper
----------------------------------------
Like Cargills Online, Keells' product listing pages are rendered client-side
(React-style SPA) — the raw HTML you get from a plain `requests.get` is
basically an empty shell with no prices in it. You need a real headless
browser to run the page's JS, same as the Cargills scraper.

Card structure (confirmed from the rendered DOM, via devtools inspection):

    <div class="product-card-nameV2">Ladies Fingers</div>
    <div class="product-card-price-containerV2">
      <div class="product-card-final-priceV2">
        Rs 190.00
        <span style="font-weight: 1000;"></span>
        KG
      </div>
    </div>
    <div class="product-card-button-containerV2 w-50">...</div>

  - Name lives in a div with class "product-card-nameV2".
  - Price + unit live in a div with class "product-card-final-priceV2"
    (itself inside "product-card-price-containerV2"): the text is
    "Rs <price>", then an (empty) styled span, then a trailing unit word
    like "KG" as a bare text node — this is the price's unit (e.g. items
    sold loose by weight show "KG" here).
  - Occasionally the product's pack size/volume is instead baked into the
    name itself (e.g. "Milk Powder 400g") rather than shown via that
    trailing unit. This scraper checks for a trailing quantity pattern in
    the name and, if found, strips it out into the "quantity" field,
    falling back to the price container's trailing unit text otherwise.
  - Pagination: a "View All" control switches the category from a teaser
    carousel to the full grid; after that, next-page navigation is a
    <button class="page-number-button-arrow"> containing an
    <img src=".../Right_Arrow...svg"> — confirmed via devtools screenshot.
    click_next() targets that image specifically rather than guessing
    which of possibly-several "page-number-button-arrow" elements is the
    right one by DOM order, since that DOM-order guess was likely why
    pagination previously got stuck on page 1 for every category.

BEFORE RUNNING THIS AT ANY SCALE:
  - Read https://www.keellssuper.com/terms-and-conditions (or equivalent)
    and https://www.keellssuper.com/robots.txt and make sure this use is
    allowed.
  - Keep concurrency at 1 and keep the delays below — this drives a real
    browser session against their site.
  - This is for personal / research price comparison, not resale or
    republishing their catalog.

Usage:
    pip install playwright beautifulsoup4
    playwright install chromium
    python scraper_keells.py

Output:
    keells_prices.json  ->  [{"name": ..., "price": ..., "quantity": ..., "url": ..., "category": ...}, ...]

Flow per category (per your instructions):
    1. Load the category page.
    2. Click "View All" to expand to the full product grid.
    3. Parse whatever products are currently on screen.
    4. Click the "page-number-button-arrow" next-page control, wait for
       the product set to change, parse again.
    5. Repeat until no next control is found/enabled, or the product set
       stops changing.
"""

import json
import re
import time
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

BASE_URL = "https://www.keellssuper.com"

# Set to True to discover every category from the site's own top category
# menu (clicks each menu item and records the URL it navigates to — see
# discover_categories()). Set to False and fill in MANUAL_CATEGORY_PAGES
# to test on just one or two categories first.
DISCOVER_ALL_CATEGORIES = True

MANUAL_CATEGORY_PAGES = {
    # Fill in a real category URL from the site to test with, e.g.:
    # "Fruits & Vegetables": "https://www.keellssuper.com/category/fruits-vegetables",
}

# Always dump the last-rendered HTML per category (not just for
# suspiciously-small categories). Handy while confirming pagination
# behaves as expected; turn off once you trust it, to save disk/time.
SAVE_DEBUG_ALWAYS = True

PRICE_RE = re.compile(r"Rs\.?\s?([\d,]+(?:\.\d{1,2})?)")

# Trailing pack-size/volume pattern occasionally baked into the product
# name itself, e.g. "Milk Powder 400g", "Coconut Oil 1L", "Rice 5 Kg".
TRAILING_QTY_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<qty>\d+(?:\.\d+)?\s?(?:g|kg|ml|l|pcs?|pack|ea)\.?)$",
    re.I,
)

# Known brand names to split out of the product name into their own field.
# Shared with scraper_cargills.py / scraper_glomark.py / scraper_spar2u.py
# via the same external file, so all scrapers stay in sync — add new
# brands to the file, not here.
KNOWN_BRANDS_FILE = "known_brand_list.txt"

with open(KNOWN_BRANDS_FILE, "r", encoding="utf-8") as f:
    KNOWN_BRANDS = [
        line.strip()
        for line in f
        if line.strip()
    ]

# Longer names (e.g. "Coca-Cola") are tried before shorter ones so they
# win over any accidental substring match. Matching is case-insensitive
# and whole-word.
_BRAND_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(b) for b in sorted(KNOWN_BRANDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def extract_brand(name):
    """Find a known brand inside a raw product name and split it out.

    Returns (brand, name_without_brand). If no known brand is matched,
    returns ("", name) unchanged — the item still gets a "brand" column
    in the output JSON, just empty, so downstream code doesn't need to
    special-case missing brands.
    """
    if not name:
        return "", name

    match = _BRAND_PATTERN.search(name)
    if not match:
        return "", name

    matched_text = match.group(1)
    # Re-map to the canonical spelling from KNOWN_BRANDS (case-insensitive)
    # so e.g. matching "nestle" in a lowercase listing still outputs "Nestle".
    brand = next((b for b in KNOWN_BRANDS if b.lower() == matched_text.lower()), matched_text)

    cleaned = name[:match.start()] + name[match.end():]
    # Collapse doubled spaces and stray leftover separators (" - ", ", ")
    # left behind where the brand used to sit.
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -,")
    return brand, cleaned


# --- "View All" candidates -------------------------------------------------
VIEW_ALL_TEXT_CANDIDATES = [
    "View All", "View all", "VIEW ALL", "Show All", "See All", "See all",
]
VIEW_ALL_SELECTOR_CANDIDATES = [
    "a.view-all", "button.view-all", ".view-all-btn", "[data-testid='view-all']",
]

# --- Next-page control (confirmed class name) -------------------------------
NEXT_ARROW_SELECTOR = "[class*='page-number-button-arrow']"

NAME_SELECTOR = "[class*='product-card-nameV2']"
PRICE_CONTAINER_SELECTOR = "[class*='product-card-price-containerV2']"
FINAL_PRICE_SELECTOR = "[class*='product-card-final-priceV2']"

# Confirmed from devtools: each product's image src contains the item's
# numeric code, e.g. ".../ItemAsset/Pic120957.jpg" -> 120957. Combined
# with the product name (spaces -> underscores), this reconstructs the
# real product-detail URL without having to click anything:
#   https://www.keellssuper.com/productDetail?itemcode=849&Richlife_Set_Yoghurt_80g
# This is deterministic and fast — no browser interaction needed per item.
IMG_ITEM_CODE_RE = re.compile(r"Pic(\d+)\.jpg", re.I)

# The actual clickable tile (confirmed via devtools: the outer wrapper
# marked with an 'event' badge) shares this class with nothing else
# nearby ("product-card-image-containerV2", "product-card-button-
# containerV2" etc. all have extra words breaking the exact substring
# match), so a plain "contains" check is safe here without needing
# word-boundary padding.
CLICKABLE_TILE_CLASS_FRAGMENT = "product-card-containerV2"

MAX_PAGES_PER_CATEGORY = 200  # safety cap

# Category pages have been observed to take a while to finish loading
# after navigating to them (whether via a fresh goto or clicking into one
# from the footer) — give this much time before assuming the page is
# ready to interact with (look for "View All", start parsing, etc).
CATEGORY_LOAD_WAIT_MS = 20000

# If the deterministic item-code pattern (see IMG_ITEM_CODE_RE) fails for
# more than this many items in one category, don't fall back to clicking
# through all of them one by one — that's slow and impolite at scale, and
# a large failure count more likely means the image-src pattern changed
# (or doesn't apply to this category) than that the site genuinely has
# this many unresolvable products. Those items just keep the category
# page as their URL instead.
MAX_CLICK_RESOLVE_PER_CATEGORY = 100


ROOT_SELECTOR = "#root"


def dump_debug(page, tag):
    """Save HTML + a screenshot + recent console/page errors. Used when
    something looks wrong, so there's more than raw HTML to diagnose
    from next time."""
    try:
        with open(f"keells_debug_{tag}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception as e:
        print(f"  (couldn't save debug HTML: {e})")
    try:
        page.screenshot(path=f"keells_debug_{tag}.png", full_page=True, timeout=10000)
    except Exception as e:
        print(f"  (couldn't save debug screenshot: {e})")
    print(f"  saved keells_debug_{tag}.html / .png — current URL: {page.url}")


def wait_for_app_ready(page, attempts=3):
    """Wait for the SPA to actually mount before interacting with it.
    Earlier runs sometimes hit an empty page — <body> with only tracking
    scripts and no <div id="root"> content at all — presumably a load
    that raced ahead of hydration (networkidle firing before the JS
    bundle finished mounting), or the site throttling/blocking a rapid
    repeat headless visit. Rather than trust a fixed wait_for_timeout(),
    this polls for #root to have actual children, and retries the full
    navigation a couple of times if it doesn't show up."""
    console_errors = []
    page.on("pageerror", lambda exc: console_errors.append(str(exc)))
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

    for attempt in range(1, attempts + 1):
        try:
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('#root');
                    return el && el.children.length > 0;
                }""",
                timeout=15000,
            )
            return True
        except Exception:
            print(f"  app root didn't render on attempt {attempt}/{attempts}")
            if console_errors:
                print(f"  console/page errors seen: {console_errors[-5:]}")
            if attempt < attempts:
                page.wait_for_timeout(2000)
                try:
                    page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
                except Exception as e:
                    print(f"  reload failed: {e}")
                page.wait_for_timeout(2000)

    return False


FOOTER_HOLDER_SELECTOR = ".v2-fo-footer-link-holder"
FOOTER_HEADING_SELECTOR = ".v2-fo-footer-link-heading"
FOOTER_ITEM_SELECTOR = ".v2-fo-footer-link-item"


def get_categories_footer_holder(page):
    """Find the specific footer block whose heading is 'Categories' —
    confirmed from devtools screenshot:

        <div class="v2-fo-footer-link-holder ...">
          <div class="v2-fo-footer-link-heading">Categories</div>
          <div class="v2-fo-footer-link-item">Grocery</div>
          <div class="v2-fo-footer-link-item">Beverages</div>
          <div class="v2-fo-footer-link-item">Household</div>
          <div class="v2-fo-footer-link-item">Vegetables</div>
          <div class="v2-fo-footer-link-item">Fruits</div>
          ...
        </div>

    The footer likely has several ".v2-fo-footer-link-holder" blocks (one
    per heading, e.g. "Categories", "Useful Links", "Contact", ...), so
    this checks each one's heading text rather than assuming the first
    holder found is the right one. Returns the matching Locator, or None
    if no holder has a "Categories" heading."""
    holders = page.locator(FOOTER_HOLDER_SELECTOR)
    for i in range(holders.count()):
        holder = holders.nth(i)
        heading = holder.locator(FOOTER_HEADING_SELECTOR).first
        try:
            if heading.inner_text().strip().lower() == "categories":
                return holder
        except Exception:
            continue
    return None


def discover_categories(page):
    """Load the homepage and discover categories from the FOOTER's
    "Categories" block, not the top-nav flyout menu.

    The top-nav menu (an earlier version of this function) turned out to
    require an extra hover-to-reveal-submenu step, since the department
    entries there are just flyout triggers rather than direct links — see
    the git history / prior version of this docstring for that whole
    saga. The footer's "Categories" list (confirmed via devtools
    screenshot) is much simpler: each entry there is a single click
    straight to that category's product page, e.g.

        <div class="v2-fo-footer-link-holder ...">
          <div class="v2-fo-footer-link-heading">Categories</div>
          <div class="v2-fo-footer-link-item">Grocery</div>
          <div class="v2-fo-footer-link-item">Beverages</div>
          ...
        </div>

    Like the nav menu items, these are div click-handlers (not <a href>
    links, per the 'event' badges in devtools), so we still have to
    click each one and read page.url after the SPA navigates — but
    there's no hover/submenu step needed first.

    Flow: collect the category names from the footer once, then for each
    name: re-locate it fresh (in case of re-render), click it, record
    page.url, and use browser back-navigation to return to the homepage
    for the next one (cheaper than a full reload, and should work fine
    since SPA routing normally goes through the History API anyway)."""
    page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1000)

    if not wait_for_app_ready(page):
        print("  app never mounted (#root stayed empty) — dumping for inspection")
        dump_debug(page, "app_not_ready")
        return {}

    def scroll_to_footer():
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        page.wait_for_timeout(800)

    scroll_to_footer()

    holder = get_categories_footer_holder(page)
    if holder is None:
        print("  couldn't find the footer 'Categories' block — dumping for inspection")
        dump_debug(page, "footer_categories_not_found")
        return {}

    item_names = [n.strip() for n in holder.locator(FOOTER_ITEM_SELECTOR).all_inner_texts() if n.strip()]
    print(f"  found {len(item_names)} category items in the footer: {item_names}")

    if not item_names:
        dump_debug(page, "footer_categories_empty")
        return {}

    categories = {}
    for name in item_names:
        try:
            holder = get_categories_footer_holder(page)
            if holder is None:
                print(f"  footer 'Categories' block missing before clicking '{name}' — stopping early")
                dump_debug(page, f"footer_missing_before_{name.replace(' ', '_')}")
                break

            # Exact-text match (anchored regex) rather than a loose
            # has-text filter, so e.g. "Fruits" doesn't accidentally also
            # match some other longer label containing that word.
            item = holder.locator(FOOTER_ITEM_SELECTOR).filter(has_text=re.compile(rf"^{re.escape(name)}$"))
            item.first.scroll_into_view_if_needed()
            item.first.click(timeout=5000)
            page.wait_for_timeout(1500)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            categories[name] = page.url
            print(f"  {name} -> {page.url}")

            # Back to the homepage for the next item.
            page.go_back(wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(1000)
            if not wait_for_app_ready(page, attempts=2):
                print(f"  app didn't remount after going back from '{name}' — stopping discovery early")
                dump_debug(page, f"app_not_ready_after_{name.replace(' ', '_')}")
                break
            scroll_to_footer()
        except Exception as e:
            print(f"  couldn't resolve category '{name}': {e}")
            # Best-effort recovery: try to get back to the homepage with
            # the footer visible so the next iteration isn't doomed too.
            try:
                page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
                wait_for_app_ready(page, attempts=2)
                scroll_to_footer()
            except Exception:
                pass
            continue

    return categories


def click_first_match(page, selector_candidates, text_candidates, what):
    """Try each selector, then each exact-text match, and click the first
    visible+enabled control found. Returns True if something was clicked."""
    for selector in selector_candidates:
        try:
            el = page.locator(selector).first
            if el and el.is_visible() and el.is_enabled():
                el.scroll_into_view_if_needed()
                el.click(timeout=3000)
                return True
        except Exception:
            continue

    for text in text_candidates:
        try:
            el = page.get_by_text(text, exact=True).first
            if el and el.is_visible():
                el.scroll_into_view_if_needed()
                el.click(timeout=3000)
                return True
        except Exception:
            continue

    return False


def click_view_all(page):
    return click_first_match(page, VIEW_ALL_SELECTOR_CANDIDATES, VIEW_ALL_TEXT_CANDIDATES, "View All")


def click_next(page):
    """Click the next-page arrow. Confirmed via devtools screenshot: the
    'next' control is a <button class="page-number-button-arrow"> that
    contains an <img src=".../Right_Arrow...svg">. That's an unambiguous
    signal — much more reliable than the earlier 'assume next is the LAST
    element with this shared class' heuristic, which was likely the real
    bug behind pagination getting stuck on page 1 (if the previous-page
    arrow happens to come after the next-page arrow in the DOM, or if
    there's normally only one arrow rendered at a time, that heuristic
    could easily click the wrong thing or nothing useful).

    Tries the image-based selector first; falls back to the old
    last-enabled-element heuristic only if that specific selector doesn't
    match anything (e.g. if the hashed filename or DOM structure changes
    later). Returns True if something was clicked."""
    targeted_selector = "button[class*='page-number-button-arrow']:has(img[src*='Right_Arrow' i])"
    try:
        candidates = page.locator(targeted_selector)
        if candidates.count() > 0:
            el = candidates.first
            if el.is_visible() and el.is_enabled():
                el.scroll_into_view_if_needed()
                el.click(timeout=3000)
                return True
    except Exception:
        pass

    # Fallback: old "last visible+enabled element sharing the class"
    # heuristic, kept only as a safety net.
    try:
        arrows = page.locator(NEXT_ARROW_SELECTOR)
        count = arrows.count()
    except Exception:
        count = 0

    for i in range(count - 1, -1, -1):
        try:
            el = arrows.nth(i)
            if el.is_visible() and el.is_enabled():
                el.scroll_into_view_if_needed()
                el.click(timeout=3000)
                return True
        except Exception:
            continue

    return False


def find_card_container(name_div, max_levels=8):
    """Walk up from a product-card-nameV2 div to find the smallest
    ancestor that also contains a product-card-price-containerV2 — that
    ancestor is the product card. Stop early if we widen into a parent
    holding more than one product-card-nameV2, since going any higher
    would start pulling in a neighboring card's price."""
    node = name_div
    for _ in range(max_levels):
        parent = node.parent
        if parent is None:
            break

        name_divs = parent.select(NAME_SELECTOR)
        if len(name_divs) > 1:
            break  # widened into a container with multiple products

        if parent.select_one(PRICE_CONTAINER_SELECTOR):
            return parent

        node = parent

    return None


def split_trailing_quantity(name):
    """If the name ends with a pack-size/volume pattern (e.g. 'Milk Powder
    400g'), split it off. Returns (clean_name, quantity_or_none)."""
    match = TRAILING_QTY_RE.match(name.strip())
    if match:
        return match.group("name").strip(), match.group("qty").strip()
    return name.strip(), None


def _search_card_and_ancestors(card, extract_fn, max_ancestor_levels=4):
    """Try extract_fn(node) starting at `card`, then widen to a few
    ancestor levels above it, stopping early if an ancestor holds more
    than one product-card-nameV2 (so this can never cross into a
    neighboring product's data). Returns the first non-None result."""
    node = card
    result = extract_fn(node)
    if result is not None:
        return result
    for _ in range(max_ancestor_levels):
        parent = node.parent
        if parent is None:
            break
        if len(parent.select(NAME_SELECTOR)) > 1:
            break  # widened into a container with multiple products
        result = extract_fn(parent)
        if result is not None:
            return result
        node = parent
    return None


def _extract_href(node):
    if node.name == "a" and node.has_attr("href"):
        return node["href"]
    link = node.find("a", href=True)
    return link["href"] if link else None


def _extract_img_src(node):
    img = node.find("img", src=True)
    return img["src"] if img else None


def _extract_discount(node):
    """Return this product's discount text from its
    div.product-card-promotion-badge-two (see the "11 / % / Off"
    screenshot), or None if that badge isn't in this node so
    _search_card_and_ancestors keeps widening outward.

    The badge splits the discount across several small divs — a
    percentage number (product-card-promotion-badge-percentageV2) and
    one-or-more suffix pieces (product-card-promotion-badge-suffixV2,
    e.g. "%" then "Off") — so this reassembles them into one string
    ("11% Off") rather than a single get_text() call, which would insert
    an unwanted space between the number and the "%" sign.
    """
    badge = node.find("div", class_=lambda c: c and "product-card-promotion-badge-two" in c.split())
    if not badge:
        return None  # let the caller widen to the next ancestor level

    percentage_div = badge.find(
        class_=lambda c: c and "product-card-promotion-badge-percentageV2" in c.split()
    )
    percentage = percentage_div.get_text(strip=True) if percentage_div else ""

    suffix_divs = badge.find_all(
        class_=lambda c: c and "product-card-promotion-badge-suffixV2" in c.split()
    )
    suffixes = [s.get_text(strip=True) for s in suffix_divs if s.get_text(strip=True)]

    parts = []
    if percentage and suffixes:
        parts.append(percentage + suffixes[0])  # "11" + "%" -> "11%"
        parts.extend(suffixes[1:])              # remaining suffix(es), e.g. "Off"
    elif percentage:
        parts.append(percentage)
    else:
        parts.extend(suffixes)

    discount = " ".join(p for p in parts if p).strip()
    # The badge div exists but had nothing usable in it — that's a
    # definite "no discount" for this product, not "keep looking".
    return discount if discount else "0"


def build_deterministic_url(raw_name, item_code):
    """Confirmed real URL pattern (from devtools): the item's numeric
    code plus its name with spaces replaced by underscores, e.g.
    https://www.keellssuper.com/productDetail?itemcode=849&Richlife_Set_Yoghurt_80g
    Uses raw_name (including any trailing pack size like "80g") rather
    than the cleaned name, since that's what the confirmed example URL
    actually contains."""
    url_name = re.sub(r"\s+", "_", raw_name.strip())
    return f"{BASE_URL}/productDetail?itemcode={item_code}&{url_name}"


def resolve_product_url(card, raw_name, category_url):
    """Resolve a product's detail-page URL, trying in order:
      1. Deterministic reconstruction from the product image's item code
         (confirmed pattern — see IMG_ITEM_CODE_RE / build_deterministic_url).
         Fast: no browser interaction needed. This covers the vast
         majority of items.
      2. A real <a href> nearby, if one happens to exist (checked as a
         fallback in case some cards do have one, even though the
         devtools screenshot for a typical card showed none).
      3. Neither found — mark as needing a live click to resolve. This
         can't be done here (parse_current_page only has static HTML, no
         live Playwright page), so it's handled separately by the caller
         in scrape_category, with `category_url` as a temporary
         placeholder until (if) that click-through succeeds.

    Returns (url, status) where status is one of "deterministic", "href",
    "needs_click"."""
    img_src = _search_card_and_ancestors(card, _extract_img_src)
    if img_src:
        m = IMG_ITEM_CODE_RE.search(img_src)
        if m:
            return build_deterministic_url(raw_name, m.group(1)), "deterministic"

    href = _search_card_and_ancestors(card, _extract_href)
    if href:
        return urljoin(BASE_URL, href), "href"

    return category_url, "needs_click"


def parse_current_page(html, category_url):
    """Parse whatever products are currently rendered into
    {key: {"name", "brand", "price", "quantity", "discount", "url"}},
    using the confirmed product-card-nameV2 / product-card-price-
    containerV2 / product-card-final-priceV2 structure.

    Returns (items, unresolved, link_stats):
      - unresolved: list of {"key", "raw_name"} for items whose URL
        couldn't be built deterministically and have no nearby <a href>
        — the caller (scrape_category) resolves these via live click-
        through afterward, since that needs the actual Playwright page.
      - link_stats: {"deterministic": n, "href": n, "needs_click": n}
        counting how each item's URL was obtained, for the console log.
    """
    soup = BeautifulSoup(html, "html.parser")
    items = {}
    unresolved = []
    link_stats = {"deterministic": 0, "href": 0, "needs_click": 0}

    for name_div in soup.select(NAME_SELECTOR):
        raw_name = name_div.get_text(" ", strip=True)
        if not raw_name:
            continue

        card = find_card_container(name_div)
        if card is None:
            continue  # couldn't safely pair this name with a price

        price_el = card.select_one(FINAL_PRICE_SELECTOR) or card.select_one(PRICE_CONTAINER_SELECTOR)
        if not price_el:
            continue

        price_text = price_el.get_text(" ", strip=True)
        price_match = PRICE_RE.search(price_text)
        if not price_match:
            continue
        price = float(price_match.group(1).replace(",", ""))

        # The unit trails the price text, e.g. "Rs 190.00 KG" -> "KG".
        # Strip the "Rs <amount>" part off the front and whatever's left
        # (if anything) is the price-container's unit/quantity hint.
        remainder = PRICE_RE.sub("", price_text, count=1).strip()
        price_unit = remainder or None

        # Occasionally the volume is baked into the name instead
        # (e.g. "Milk Powder 400g") — prefer that when present, since
        # it's more specific than a generic unit like "KG".
        clean_name, name_qty = split_trailing_quantity(raw_name)
        brand, clean_name = extract_brand(clean_name)  # strip brand before title-casing
        clean_name = clean_name.title()
        quantity = name_qty or price_unit
        quantity = quantity.replace("/", "1") if quantity else None  # strip any literal backslash

        discount = _search_card_and_ancestors(card, _extract_discount) or "0"

        url, status = resolve_product_url(card, raw_name, category_url)
        link_stats[status] += 1

        key = url if status != "needs_click" else f"{clean_name}|{price}"
        items[key] = {
            "name": clean_name,
            "brand": brand,
            "price": price,
            "quantity": quantity,
            "discount": discount,
            "url": url,
        }
        if status == "needs_click":
            unresolved.append({"key": key, "raw_name": raw_name})

    return items, unresolved, link_stats


def resolve_via_click(page, raw_name):
    """Click the product tile whose name matches raw_name exactly, to
    capture the real URL the SPA navigates to, then return to the
    category listing via browser back navigation. This is the fallback
    used only for items with no parseable item code AND no nearby href —
    expected to be rare (see MAX_CLICK_RESOLVE_PER_CATEGORY).

    NOTE: this trusts that page.go_back() restores the SPA to the same
    pagination page rather than resetting to page 1 — a reasonable
    assumption for a well-built site (people routinely view a product
    then hit their browser's back button, so this is a well-trodden
    path), but it's unverified against the live site since I can't drive
    a browser against keellssuper.com from here. If resolved URLs look
    wrong, or pagination seems to reset after this runs, that assumption
    is likely broken and this would need reworking to re-navigate by
    page number instead of trusting go_back().

    Returns (url, success)."""
    try:
        name_loc = page.locator(NAME_SELECTOR).filter(has_text=re.compile(rf"^{re.escape(raw_name)}$"))
        if name_loc.count() == 0:
            return None, False

        target = name_loc.first
        # Climb to the nearest ancestor sharing the clickable tile's class
        # (see CLICKABLE_TILE_CLASS_FRAGMENT) — that's the actual
        # click-handler element, one or more levels above the name div.
        tile = target.locator(
            f"xpath=(ancestor::div[contains(@class, '{CLICKABLE_TILE_CLASS_FRAGMENT}')])[last()]"
        )
        clickable = tile.first if tile.count() > 0 else target

        clickable.scroll_into_view_if_needed()
        clickable.click(timeout=5000)
        page.wait_for_timeout(1200)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        resolved_url = page.url
        if resolved_url.rstrip("/") in (BASE_URL, BASE_URL.rstrip("/")):
            # Didn't actually navigate anywhere — treat as a failed click.
            page.go_back(wait_until="networkidle", timeout=15000)
            return None, False

        page.go_back(wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(800)
        return resolved_url, True
    except Exception as e:
        print(f"    click-resolve failed for '{raw_name}': {e}")
        try:
            page.go_back(wait_until="networkidle", timeout=15000)
        except Exception:
            pass
        return None, False


def scrape_category(page, name, url):
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(CATEGORY_LOAD_WAIT_MS)

    # Step 1: expand to the full grid if there's a "View All" control.
    if click_view_all(page):
        page.wait_for_timeout(1500)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(1000)

    all_items = {}
    last_html = None
    link_totals = {"deterministic": 0, "href": 0, "needs_click": 0}
    unresolved_by_page = {}  # page_num -> [{"key", "raw_name"}, ...]

    for page_num in range(1, MAX_PAGES_PER_CATEGORY + 1):
        html = page.content()
        last_html = html
        page_items, page_unresolved, page_stats = parse_current_page(html, url)
        for k in page_stats:
            link_totals[k] += page_stats[k]

        existing_keys_before = set(all_items.keys())
        new_count = sum(1 for k in page_items if k not in existing_keys_before)
        all_items.update(page_items)

        # Only track unresolved entries that are actually new this round
        # — avoids re-queuing the same click if a page ever gets parsed
        # twice (e.g. a stalled retry where new_count came back 0).
        if page_unresolved:
            new_unresolved = [e for e in page_unresolved if e["key"] not in existing_keys_before]
            if new_unresolved:
                unresolved_by_page.setdefault(page_num, []).extend(new_unresolved)

        print(f"    page {page_num}: {len(page_items)} on page, {new_count} new, {len(all_items)} total "
              f"({page_stats['deterministic']} deterministic, {page_stats['href']} real href, "
              f"{page_stats['needs_click']} need click-resolve)")

        if page_num > 1 and new_count == 0:
            break

        # Step 2: click ">" to go to the next page.
        if not click_next(page):
            break

        page.wait_for_timeout(1500)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(1000)

    if (SAVE_DEBUG_ALWAYS or len(all_items) <= 20) and last_html:
        debug_path = f"keells_debug_{name.replace(' ', '_')}.html"
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(last_html)
        print(f"  saved last rendered page HTML to {debug_path} for inspection")

    # Deferred click-resolution pass: for items whose URL couldn't be
    # built deterministically (no parseable item code) and had no nearby
    # href, actually click each one to capture its real URL. Done AFTER
    # the main pagination loop finishes, as a separate pass, rather than
    # interleaved with it — interleaving would risk go_back() disturbing
    # the main loop's own pagination position mid-scrape.
    total_needing_click = sum(len(v) for v in unresolved_by_page.values())
    if total_needing_click > MAX_CLICK_RESOLVE_PER_CATEGORY:
        print(f"  {total_needing_click} items need click-resolve for '{name}' — over the "
              f"{MAX_CLICK_RESOLVE_PER_CATEGORY} safety cap, so skipping click-through for all "
              f"of them (they keep the category page as their URL). This many failures usually "
              f"means the item-code image pattern isn't matching for this category — check "
              f"{debug_path if (SAVE_DEBUG_ALWAYS or len(all_items) <= 20) else '(re-run with SAVE_DEBUG_ALWAYS=True)'}.")
    elif total_needing_click:
        print(f"  resolving {total_needing_click} item(s) without a parseable item code "
              f"via click-through for '{name}'...")
        resolved_count = 0
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(1500)
            if click_view_all(page):
                page.wait_for_timeout(1500)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(1000)

            current_page = 1
            for target_page_num in sorted(unresolved_by_page.keys()):
                while current_page < target_page_num:
                    if not click_next(page):
                        raise RuntimeError(
                            f"couldn't advance to page {target_page_num} during URL resolution "
                            f"(stopped at page {current_page})"
                        )
                    page.wait_for_timeout(1500)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    page.wait_for_timeout(800)
                    current_page += 1

                for entry in unresolved_by_page[target_page_num]:
                    resolved_url, ok = resolve_via_click(page, entry["raw_name"])
                    if ok and entry["key"] in all_items:
                        all_items[entry["key"]]["url"] = resolved_url
                        resolved_count += 1
                        link_totals["needs_click"] -= 1
                        link_totals["href"] += 1
        except Exception as e:
            print(f"  click-resolution pass stopped early: {e}")

        print(f"  resolved {resolved_count}/{total_needing_click} via click-through for '{name}' "
              f"(any remaining keep the category page as their URL)")

    print(f"  link summary for '{name}': {link_totals['deterministic']} deterministic, "
          f"{link_totals['href']} real href/click-resolved, {link_totals['needs_click']} category-fallback")

    return [
        {"name": v["name"], "brand": v.get("brand", ""), "price": v["price"], "quantity": v.get("quantity"),
         "discount": v.get("discount", "0"), "url": v.get("url"), "category": name}
        for v in all_items.values()
    ]


def main():
    all_items = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        if DISCOVER_ALL_CATEGORIES:
            print("Discovering categories from the homepage footer's 'Categories' block...")
            category_pages = discover_categories(page)
            print(f"  found {len(category_pages)} categories")
            if not category_pages:
                print("  discovery found nothing — falling back to MANUAL_CATEGORY_PAGES")
                category_pages = MANUAL_CATEGORY_PAGES
        else:
            category_pages = MANUAL_CATEGORY_PAGES

        if not category_pages:
            print("No category pages configured. Set DISCOVER_ALL_CATEGORIES=True or fill "
                  "in MANUAL_CATEGORY_PAGES with at least one real category URL, then re-run.")
            browser.close()
            return

        for name, url in category_pages.items():
            print(f"Scraping {name} ({url}) ...")
            try:
                items = scrape_category(page, name, url)
                print(f"  found {len(items)} products")
                all_items.extend(items)
            except Exception as e:
                print(f"  failed: {e}")
            time.sleep(3)  # be polite between categories
        browser.close()

    with open("keells_prices.json", "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)

    have_qty = sum(1 for i in all_items if i.get("quantity"))
    print(f"\nSaved {len(all_items)} items to keells_prices.json ({have_qty} with a quantity found)")


if __name__ == "__main__":
    main()