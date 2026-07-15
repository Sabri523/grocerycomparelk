"""
Match Cargills and Glomark products by name AND pack size, then produce
comparison.json for the website.

Why this exists: fuzzy name matching alone can't tell "Tea 400g" apart
from "Tea 100g" — it'll happily pair them up because the names are
similar, which then compares a 400g price against a 100g price as if
they were the same product. That gives wrong "cheapest"/"savings" values.

This version:
  1. Extracts a quantity + unit from each product name (e.g. "500g",
     "1.5kg", "6 x 200ml", "10 pcs") and normalizes it to grams, millilitres,
     or piece-count.
  2. Only accepts a fuzzy name match if the two items' quantities are
     compatible (same unit type, and not wildly different scale — e.g.
     won't match a 1kg bag against a 25kg bulk sack).
  3. Computes a normalized unit price (per 100g / per 100ml / per piece)
     for every item, so even legitimately different pack sizes of "the
     same" product can be compared fairly instead of comparing raw prices.

Usage:
    pip install rapidfuzz
    python match_products.py
"""

import json
import re
from rapidfuzz import fuzz, process

SIMILARITY_THRESHOLD = 80  # 0-100, raise this if you get bad name matches

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


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def enrich(item):
    # Prefer an explicit "quantity" field if the scraper provided one (e.g.
    # Cargills' pack-size button text, like "400g") — it's far more
    # reliable than trying to regex a size back out of the product name.
    # Fall back to parsing the name itself if no such field exists or it's
    # empty (this is the only option for Glomark, and for any Cargills item
    # where the button wasn't found).
    qty_source = item.get("quantity") or item["name"]
    unit_type, base_qty = extract_quantity(qty_source)
    return {
        **item,
        "unit_type": unit_type,
        "base_qty": base_qty,
        "unit_price": unit_price(item["price"], unit_type, base_qty),
        "qty_source": "quantity_field" if item.get("quantity") else "name",
    }


def main():
    cargills = [enrich(c) for c in load("cargills_prices.json")]
    glomark = [enrich(g) for g in load("glomark_prices.json")]

    glomark_names = [g["name"] for g in glomark]

    matched_glomark_indices = set()
    matched = []
    rejected_for_size = 0

    for c in cargills:
        # Get several fuzzy candidates, not just the top one, so a
        # size-incompatible best match doesn't block a good second choice.
        candidates = process.extract(
            c["name"], glomark_names, scorer=fuzz.token_sort_ratio, limit=5
        )

        chosen = None
        for _, score, idx in candidates:
            if score < SIMILARITY_THRESHOLD:
                continue
            g = glomark[idx]
            if quantities_compatible(c["unit_type"], c["base_qty"], g["unit_type"], g["base_qty"]):
                chosen = (g, idx, score)
                break
            else:
                rejected_for_size += 1

        if chosen:
            g, idx, score = chosen
            matched_glomark_indices.add(idx)
            matched.append({
                "name": c["name"],
                "cat": c.get("category", ""),
                "cargills": c["price"],
                "glomark": g["price"],
                "match_confidence": round(score, 1),
                "unit_type": c["unit_type"] or g["unit_type"],
                "cargills_qty": c["base_qty"],
                "glomark_qty": g["base_qty"],
                "cargills_unit_price": c["unit_price"],
                "glomark_unit_price": g["unit_price"],
                "unit_label": unit_label(c["unit_type"] or g["unit_type"]),
                "size_verified": c["unit_type"] is not None and g["unit_type"] is not None,
            })
        else:
            matched.append({
                "name": c["name"],
                "cat": c.get("category", ""),
                "cargills": c["price"],
                "glomark": "NA",
                "match_confidence": 0,
                "unit_type": c["unit_type"],
                "cargills_qty": c["base_qty"],
                "glomark_qty": None,
                "cargills_unit_price": c["unit_price"],
                "glomark_unit_price": None,
                "unit_label": unit_label(c["unit_type"]),
                "size_verified": False,
            })

    for idx, g in enumerate(glomark):
        if idx not in matched_glomark_indices:
            matched.append({
                "name": g["name"],
                "cat": g.get("category", ""),
                "cargills": "NA",
                "glomark": g["price"],
                "match_confidence": 0,
                "unit_type": g["unit_type"],
                "cargills_qty": None,
                "glomark_qty": g["base_qty"],
                "cargills_unit_price": None,
                "glomark_unit_price": g["unit_price"],
                "unit_label": unit_label(g["unit_type"]),
                "size_verified": False,
            })

    matched.sort(key=lambda x: -x["match_confidence"])

    with open("comparison.json", "w", encoding="utf-8") as f:
        json.dump(matched, f, ensure_ascii=False, indent=2)

    total_matched = sum(1 for x in matched if x["match_confidence"] >= SIMILARITY_THRESHOLD)
    verified = sum(1 for x in matched if x["size_verified"])
    from_field = sum(1 for c in cargills if c["qty_source"] == "quantity_field") + \
                 sum(1 for g in glomark if g["qty_source"] == "quantity_field")
    print(f"Total entries written: {len(matched)}")
    print(f"Successfully matched pairs: {total_matched}")
    print(f"  of which size-verified (quantity checked on both sides): {verified}")
    print(f"Items with quantity from an explicit field (not guessed from name): {from_field}")
    print(f"Fuzzy candidates rejected for incompatible pack size: {rejected_for_size}")
    print("Review comparison.json — pairs with size_verified=false had a quantity")
    print("that couldn't be parsed from one side's name, so the size check was skipped.")


if __name__ == "__main__":
    main()