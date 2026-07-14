"""
Match Cargills and Glomark products by name and produce comparison.json,
the file the website (index.html) expects for live data.

Products are matched by fuzzy name similarity. Unmatched items are included 
using empty strings for missing prices to prevent frontend toLocaleString() crashes.
"""

import json
from rapidfuzz import fuzz, process

SIMILARITY_THRESHOLD = 80  # 0-100, raise this if you get bad matches


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    cargills = load("cargills_prices.json")
    glomark = load("glomark_prices.json")

    glomark_names = [g["name"] for g in glomark]
    
    matched_glomark_indices = set()
    matched = []

    for c in cargills:
        best = process.extractOne(c["name"], glomark_names, scorer=fuzz.token_sort_ratio)
        
        if best and best[1] >= SIMILARITY_THRESHOLD:
            glomark_idx = best[2]
            g = glomark[glomark_idx]
            matched_glomark_indices.add(glomark_idx)
            
            matched.append({
                "name": c["name"],
                "cat": c.get("category", ""),
                "cargills": c["price"],
                "glomark": g["price"],
                "match_confidence": round(best[1], 1),
            })
        else:
            # Cargills item has no match in Glomark
            matched.append({
                "name": c["name"],
                "cat": c.get("category", ""),
                "cargills": c["price"],
                "glomark": "NA",  # Empty string instead of null
                "match_confidence": 0,
            })

    # Add remaining unmatched Glomark items
    for idx, g in enumerate(glomark):
        if idx not in matched_glomark_indices:
            matched.append({
                "name": g["name"],
                "cat": g.get("category", ""),
                "cargills": "NA",  # Empty string instead of null
                "glomark": g["price"],
                "match_confidence": 0,
            })

    # Sort matched items first, then group unmatched items at the bottom
    matched.sort(key=lambda x: -x["match_confidence"])

    with open("comparison.json", "w", encoding="utf-8") as f:
        json.dump(matched, f, ensure_ascii=False, indent=2)

    total_matched = sum(1 for x in matched if x["match_confidence"] >= SIMILARITY_THRESHOLD)
    print(f"Total entries written: {len(matched)}")
    print(f"Successfully matched pairs: {total_matched}")


if __name__ == "__main__":
    main()