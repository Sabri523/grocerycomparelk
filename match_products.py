"""
Match Cargills, Glomark, Keells, and SPAR2U products by name AND pack
size, then produce comparison.json for the website.

Why this exists: fuzzy name matching alone can't tell "Tea 400g" apart
from "Tea 100g" — it'll happily pair them up because the names are
similar, which then compares a 400g price against a 100g price as if
they were the same product. That gives wrong "cheapest"/"savings" values.

This version:
  1. Extracts a quantity + unit from each product name (e.g. "500g",
     "1.5kg", "6 x 200ml", "10 pcs") and normalizes it to grams, millilitres,
     or piece-count. Prefers a scraper-provided "quantity" field over
     re-parsing the name when one exists (Cargills' pack-size button
     text, Keells' price-unit/trailing-name quantity, and SPAR2U's
     trailing-name quantity all land in this same field) — Glomark is
     currently the only scraper without one, so it always falls back to
     parsing its name.
  2. Only accepts a fuzzy name match if the two items' quantities are
     compatible (same unit type, and not wildly different scale — e.g.
     won't match a 1kg bag against a 25kg bulk sack).
  3. Computes a normalized unit price (per 100g / per 100ml / per piece)
     for every item, so even legitimately different pack sizes of "the
     same" product can be compared fairly instead of comparing raw prices.

N-way matching approach:
  Rather than a proper N-way assignment (which would need something like
  the Hungarian algorithm to be globally optimal), this uses a simple
  greedy, pass-per-store strategy, generalized over STORE_ORDER below:
    For each store, in STORE_ORDER, that hasn't already been "claimed" as
    someone else's match: treat it as the anchor for a new row, and
    independently look for the best compatible match in every store
    LATER in STORE_ORDER. Each match found is marked "used" so it can't
    be reused by a later anchor. Once every store's turn as anchor has
    passed, any of its items not yet claimed becomes its own solo row.
  With STORE_ORDER = [cargills, glomark, keells, spar2u], this reduces to
  exactly the earlier hand-written 3-pass version when spar2u is absent,
  and adding a 5th store later is a one-line change (add it to
  STORE_FILES and STORE_ORDER) rather than writing a new pass by hand.
  This is greedy, not globally optimal — an earlier store's item can
  "steal" the best match away from a different, otherwise better-
  matching, later item. Good enough for a price comparison tool; flag it
  if mismatches turn out to be a real problem in practice and a proper
  assignment solver can be swapped in.

  Any store's input file can simply be missing (e.g. you haven't run a
  given scraper yet) — that store is just treated as empty and every row
  shows "NA" for it, rather than the whole script failing.

Usage:
    pip install rapidfuzz
    python match_products.py
"""

import json
import re
from rapidfuzz import fuzz, process

SIMILARITY_THRESHOLD = 85  # 0-100, raise this if you get bad name matches

# How much a matched pair's quantities are allowed to differ and still be
# considered "the same product" (e.g. 400g vs 500g is fine; 1kg vs 25kg is
# not). 0.4-2.5x means one item can be up to 2.5x the size of the other.
MIN_QTY_RATIO = 0.4
MAX_QTY_RATIO = 2.5

WEIGHT_UNITS = {
    "g": 1, "gram": 1, "grams": 1, "gm": 1, "gms": 1,
    "kg": 1000, "kilo": 1000, "kilos": 1000, "kilogram": 1000, "kilograms": 1000,
}
VOLUME_UNITS = {
    "ml": 1, "millilitre": 1, "milliliter": 1, "millilitres": 1, "milliliters": 1,
    "l": 1000, "lt": 1000, "ltr": 1000, "litre": 1000, "liter": 1000,
    "litres": 1000, "liters": 1000,
}
COUNT_UNITS = {
    "pcs": 1, "pc": 1, "piece": 1, "pieces": 1, "pack": 1, "pkt": 1,
    "nos": 1, "eggs": 1, "egg": 1, "pack(s)": 1, "unit": 1, "units": 1,
}

# e.g. "6 x 200g" / "6x200ml"
MULTI_PACK_RE = re.compile(r"(\d+)\s*[xX]\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)")
# e.g. "500g" / "1.5 Kg" / "750 Ml" / "10 Pcs"
SINGLE_QTY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+)\b")

# The stores this script knows about, and the JSON file each one's
# scraper produces. STORE_ORDER controls anchor priority in main()'s
# matching passes (see the N-way matching note above) — items from
# earlier stores get first pick of matches from every later store.
# Adding a store: add its file here AND its name to STORE_ORDER below.
STORE_FILES = {
    "cargills": "cargills_prices.json",
    "glomark": "glomark_prices.json",
    "keells": "keells_prices.json",
    "spar2u": "spar2u_prices.json",
}
STORE_ORDER = ["cargills", "glomark", "keells", "spar2u"]


def _unit_lookup(word):
    w = word.lower().rstrip(".")
    if w in WEIGHT_UNITS:
        return "weight", WEIGHT_UNITS[w]
    if w in VOLUME_UNITS:
        return "volume", VOLUME_UNITS[w]
    if w in COUNT_UNITS:
        return "count", COUNT_UNITS[w]
    return None, None


def extract_quantity(name):
    """Return (unit_type, base_qty) — base_qty is in grams for 'weight',
    millilitres for 'volume', or a plain count for 'count'. Returns
    (None, None) if no recognizable quantity is found in the name."""
    m = MULTI_PACK_RE.search(name)
    if m:
        multiplier = float(m.group(1))
        qty = float(m.group(2))
        unit_type, factor = _unit_lookup(m.group(3))
        if unit_type:
            return unit_type, multiplier * qty * factor

    # Prefer the LAST number+unit in the name (pack size is usually at the
    # end, e.g. "Anchor Milk Powder 400G") rather than the first (which
    # could be part of a product name/brand/percentage, e.g. "100% Pure").
    for m in reversed(list(SINGLE_QTY_RE.finditer(name))):
        unit_type, factor = _unit_lookup(m.group(2))
        if unit_type:
            return unit_type, float(m.group(1)) * factor

    return None, None


def unit_price(price, unit_type, base_qty):
    """Normalized price: per 100g, per 100ml, or per piece."""
    if not unit_type or not base_qty:
        return None
    if unit_type in ("weight", "volume"):
        return round(price / base_qty * 100, 2)
    if unit_type == "count":
        return round(price / base_qty, 2)
    return None


def unit_label(unit_type):
    return {"weight": "per 100g", "volume": "per 100ml", "count": "per piece"}.get(unit_type)


def quantities_compatible(type_a, qty_a, type_b, qty_b):
    """True if two quantities can be considered 'the same product, maybe
    a different pack size' rather than genuinely different products."""
    if type_a is None or type_b is None:
        # Can't verify — allow the name match through, but the caller
        # should treat this as lower-confidence (flagged in the output).
        return True
    if type_a != type_b:
        return False
    if qty_a <= 0 or qty_b <= 0:
        return False
    ratio = qty_a / qty_b
    return MIN_QTY_RATIO <= ratio <= MAX_QTY_RATIO


def load_optional(path):
    """Load a scraper's JSON output, or return an empty list if the file
    doesn't exist yet — so running this before every scraper has been run
    degrades gracefully instead of crashing."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"  {path} not found — continuing without it (treated as empty)")
        return []


def enrich(item):
    # Prefer an explicit "quantity" field if the scraper provided one —
    # it's far more reliable than regexing a size back out of the
    # product name. Fall back to parsing the name itself if no such
    # field exists or it's empty (currently only needed for Glomark).
    qty_source = item.get("quantity") or item["name"]
    unit_type, base_qty = extract_quantity(qty_source)
    return {
        **item,
        "unit_type": unit_type,
        "base_qty": base_qty,
        "unit_price": unit_price(item["price"], unit_type, base_qty),
        "qty_source": "quantity_field" if item.get("quantity") else "name",
    }


def best_match(item, pool, pool_names, used_indices, stats):
    """Return (matched_item, idx, score) for the best not-yet-used,
    fuzzy-and-size-compatible match of `item` in `pool`, or None if
    nothing qualifies. `stats['rejected_for_size']` is incremented for
    every otherwise-good-enough candidate that got turned down purely for
    an incompatible pack size, so main() can report that count."""
    if not pool_names:
        return None
    candidates = process.extract(item["name"], pool_names, scorer=fuzz.token_sort_ratio, limit=8)
    for _, score, idx in candidates:
        if idx in used_indices:
            continue
        if score < SIMILARITY_THRESHOLD:
            continue
        candidate = pool[idx]
        if quantities_compatible(item["unit_type"], item["base_qty"], candidate["unit_type"], candidate["base_qty"]):
            return candidate, idx, score
        stats["rejected_for_size"] += 1
    return None


def na_fields(store):
    """The {store}/{store}_url/{store}_qty/{store}_unit_price fields to
    use when a row has no match on that side."""
    return {
        store: "NA",
        f"{store}_url": None,
        f"{store}_qty": None,
        f"{store}_unit_price": None,
    }


def present_fields(store, item):
    return {
        store: item["price"],
        f"{store}_url": item.get("url"),
        f"{store}_qty": item["base_qty"],
        f"{store}_unit_price": item["unit_price"],
    }


def main():
    loaded = {store: [enrich(x) for x in load_optional(STORE_FILES[store])] for store in STORE_ORDER}
    names = {store: [x["name"] for x in loaded[store]] for store in STORE_ORDER}
    used = {store: set() for store in STORE_ORDER}
    stats = {"rejected_for_size": 0}

    matched = []

    # One pass per store, in STORE_ORDER — see the N-way matching note in
    # the module docstring for why this generalizes the old hand-written
    # per-pair passes.
    for i, anchor_store in enumerate(STORE_ORDER):
        later_stores = STORE_ORDER[i + 1:]
        earlier_stores = STORE_ORDER[:i]

        for idx, item in enumerate(loaded[anchor_store]):
            if idx in used[anchor_store]:
                continue  # already claimed as a match by an earlier-anchored row

            row = {"name": item["name"], "cat": item.get("category", "")}
            for earlier in earlier_stores:
                row.update(na_fields(earlier))
            row.update(present_fields(anchor_store, item))

            confidences = []
            present_unit_types = [item["unit_type"]]

            for other_store in later_stores:
                match = best_match(item, loaded[other_store], names[other_store], used[other_store], stats)
                if match:
                    m, midx, score = match
                    used[other_store].add(midx)
                    confidences.append(score)
                    present_unit_types.append(m["unit_type"])
                    row.update(present_fields(other_store, m))
                else:
                    row.update(na_fields(other_store))

            row["unit_type"] = next((u for u in present_unit_types if u), None)
            row["unit_label"] = unit_label(row["unit_type"])
            # match_confidence is the LOWEST of whichever pairs were
            # actually matched (a chain is only as trustworthy as its
            # weakest link) — 0 if this item matched nothing at all.
            row["match_confidence"] = round(min(confidences), 1) if confidences else 0
            # size_verified: true only if at least one match was made AND
            # every store present on this row had a parseable quantity.
            row["size_verified"] = bool(confidences) and all(u is not None for u in present_unit_types)

            matched.append(row)

    matched.sort(key=lambda x: -x["match_confidence"])

    with open("comparison.json", "w", encoding="utf-8") as f:
        json.dump(matched, f, ensure_ascii=False, indent=2)

    def count_present(store):
        return sum(1 for x in matched if isinstance(x.get(store), (int, float)))

    total_matched = sum(1 for x in matched if x["match_confidence"] >= SIMILARITY_THRESHOLD)
    verified = sum(1 for x in matched if x["size_verified"])
    from_field = sum(
        1 for store in STORE_ORDER
        for item in loaded[store]
        if item["qty_source"] == "quantity_field"
    )

    print(f"Total entries written: {len(matched)}")
    for store in STORE_ORDER:
        print(f"  {store.capitalize()} prices present: {count_present(store)}")
    print(f"Rows with at least one fuzzy match made (confidence >= {SIMILARITY_THRESHOLD}): {total_matched}")
    print(f"  of which size-verified (quantity checked across every store present on the row): {verified}")
    print(f"Items with quantity from an explicit field (not guessed from name): {from_field}")
    print(f"Fuzzy candidates rejected for incompatible pack size: {stats['rejected_for_size']}")
    print("Review comparison.json — rows with size_verified=false had a quantity that")
    print("couldn't be parsed on at least one present side, so the size check was skipped there.")


if __name__ == "__main__":
    main()
