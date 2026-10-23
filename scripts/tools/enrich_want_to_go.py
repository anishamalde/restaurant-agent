"""
enrich_want_to_go — batch-enrich a list of restaurant names via the deployed
search_google_places Lambda, writing an intermediate JSON for review before
merging into want-to-go.json.

Reads RAW_NAMES below, calls agent-fc-restaurant-read (search_google_places)
in parallel, extracts area + cuisine, and dumps the result to
scripts/enrichment_out.json.

Usage:
  python3 scripts/enrich_want_to_go.py

Output: scripts/enrichment_out.json — review before merging.
"""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3

REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-west-2")
LAMBDA_NAME = "agent-fc-restaurant-read"
OUT_PATH = Path(__file__).parent / "enrichment_out.json"

# 65 entries pasted from user's Google Maps want-to-go list.
# (name, cuisine_hint_or_None) — hint is from user's own tags where available.
RAW_NAMES: list[tuple[str, str | None]] = [
    ("MYKA Greek Frozen Yoghurt", "Fro-Yo"),
    ("Fonda", "Mexican"),
    ("Saawaros", "Thai"),
    ("Cantoast Bakery", "Bakery"),
    ("THE LEAF London Shoreditch", "Dessert shop"),
    ("Restaurant Henne", None),
    ("Cafe Mama & Sons", "Cafe"),
    ("Cafe Kowloon", "Cantonese"),
    ("FlipDog", "Bar"),
    ("Holy Carrot", None),
    ("Impala", "Mediterranean"),
    ("Homies On Donkeys", "Mexican"),
    ("Logma", "Middle Eastern"),
    ("AYA & SUKI", "Vegetarian"),
    ("Marksman", "Pub"),
    ("Tigermilk Tottenham Court Road", None),
    ("Mestizo Chelsea", "Mexican"),
    ("Fika", "Coffee shop"),
    ("Running Late Coffee", "Cafe"),
    ("Common Breads", "Bakery"),
    ("SlowBurn London", None),
    ("Miga", None),
    ("JandaDiner", None),
    ("The Hero Maida Vale", None),
    ("74 Duke", None),
    ("Table at 464", None),
    ("Waltz", None),
    ("Jackson Boxer At The Corner", None),
    ("Lagom", None),
    ("Mc & Sons Borough", None),
    ("The Camberwell Arms", None),
    ("Jambo Zamsareh Ugandan Restaurant", "Ugandan"),
    ("Pomme Bakery", "Bakery"),
    ("2210 by NattyCanCook", None),
    ("Kruk", None),
    ("The Sensational Sandwich Shop", None),
    ("SINO", None),
    ("ADOH Sri Lankan Street Food Covent Garden", "Sri Lankan"),
    ("Colombo Kitchen Hopper & Kottu Bar", "Sri Lankan"),
    ("Aguamiel Churros Mexicanos", "Churros"),
    ("Godet", None),
    ("Hanbaagaasuuteeki", None),
    ("Vicino", None),
    ("Discount Suit Company", None),
    ("Kossoffs", None),
    ("Araw Ice Cream at Brityard", "Ice Cream"),
    ("Mauby", None),
    ("Nina", None),
    ("Bottarga", None),
    ("Lupa", None),
    ("Kix Bar + Kitchen", None),
    ("Empire Empire", None),
    ("Mahali Bakery", "Bakery"),
    ("Corrochio's", None),
    ("Calong", None),
    ("FKABAM", None),
    ("Lever & Bloom Coffee", "Coffee"),
    ("Seed Library", "Cocktail bar"),
    ("Canteen Notting Hill", "Italian"),
    ("Hjem", "Bakery"),
    ("Akoko", "West African"),
    ("Apricity Restaurant", "Modern European"),
    ("Chuku's Tottenham", "West African"),
    ("Shankeys", "Irish"),
    ("Ida", None),
]

lambda_client = boto3.client("lambda", region_name=REGION)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def extract_area(address: str) -> str:
    """
    Google formattedAddress looks like:
      '123 Some St, London EC2A 4BN, UK'
      '45 Foo Rd, Notting Hill, London W11 2AB, UK'
    We want the neighbourhood if present, else fall back to the postcode prefix.
    """
    if not address:
        return ""
    parts = [p.strip() for p in address.split(",")]
    # Look for the segment before "London <postcode>"
    for i, part in enumerate(parts):
        if part.lower().startswith("london ") or part.lower() == "london":
            # neighbourhood is typically the segment right before
            if i > 0:
                candidate = parts[i - 1]
                # If it's just a street number/street name, skip it
                if not re.match(r"^\d+", candidate) and len(candidate) < 40:
                    return candidate
            # Fall back to postcode area (e.g., "London W11 2AB" -> "W11")
            match = re.search(r"London\s+([A-Z]{1,2}\d[A-Z\d]?)", part)
            if match:
                return match.group(1)
    return ""


def extract_cuisine(place: dict) -> str:
    """Prefer primaryType, cleaned up."""
    ptype = place.get("primaryType", "")
    if not ptype:
        return ""
    # e.g., 'italian_restaurant' -> 'Italian', 'cafe' -> 'Cafe', 'bakery' -> 'Bakery'
    cleaned = ptype.replace("_restaurant", "").replace("_", " ").strip()
    return cleaned.title()


def enrich_one(name: str, cuisine_hint: str | None) -> dict:
    """Call search_google_places for one name and pick the top result."""
    payload = {
        "tool_name": "search_google_places",
        "tool_input": {"query": name, "area": "London"},
        "session": {"actor_id": "enrich", "session_id": "enrich"},
    }
    try:
        resp = lambda_client.invoke(
            FunctionName=LAMBDA_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode(),
        )
        body = json.loads(resp["Payload"].read())
        places = body.get("tool_output", [])
        if not places or (isinstance(places, dict) and "error" in places):
            return {
                "slug": slugify(name),
                "name": name,
                "area": "",
                "cuisine": cuisine_hint or "",
                "vegetarian_friendly": None,
                "wishlist_reason": "",
                "_status": "no_google_match",
            }
        top = places[0]
        display = top.get("displayName", {})
        display_name = display.get("text", name) if isinstance(display, dict) else name
        address = top.get("formattedAddress", "")
        area = extract_area(address)
        cuisine = cuisine_hint or extract_cuisine(top)
        return {
            "slug": slugify(display_name),
            "name": display_name,
            "area": area,
            "cuisine": cuisine,
            "vegetarian_friendly": None,
            "wishlist_reason": "",
            "_google_address": address,
            "_google_primary_type": top.get("primaryType", ""),
        }
    except Exception as e:
        return {
            "slug": slugify(name),
            "name": name,
            "area": "",
            "cuisine": cuisine_hint or "",
            "_status": f"error: {str(e)[:120]}",
        }


def main():
    print(f"enriching {len(RAW_NAMES)} entries via {LAMBDA_NAME}...")
    results = [None] * len(RAW_NAMES)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(enrich_one, name, hint): i
            for i, (name, hint) in enumerate(RAW_NAMES)
        }
        done = 0
        for f in as_completed(futures):
            i = futures[f]
            results[i] = f.result()
            done += 1
            status = results[i].get("_status", "ok")
            print(f"  [{done}/{len(RAW_NAMES)}] {results[i]['name']:40s} area={results[i].get('area','?'):20s} {status}")

    OUT_PATH.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {OUT_PATH}")

    # Summary
    no_area = [r for r in results if not r.get("area")]
    no_match = [r for r in results if r.get("_status") == "no_google_match"]
    print(f"\n{len(results) - len(no_area) - len(no_match)} enriched with area, "
          f"{len(no_area) - len(no_match)} missing area only, "
          f"{len(no_match)} no google match")


if __name__ == "__main__":
    sys.exit(main() or 0)
