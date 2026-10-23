"""
enrich_recs — batch-enrich the pasted "places I like" list via
search_google_places Lambda. Writes scripts/recs_enrichment_out.json
for review before merge.

Skips Sambal Shiok (permanently closed).
Marks Lenny's Apizza + Water House Project as temporarily_closed=true.

Usage:
  python3 scripts/enrich_recs.py
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
OUT_PATH = Path(__file__).parent / "recs_enrichment_out.json"

# (name, cuisine_hint, note, temporarily_closed)
# Sambal Shiok (permanently closed) already excluded.
RAW: list[tuple[str, str | None, str, bool]] = [
    ("Oranj", "Restaurant", "", False),
    ("Two Twins Tacos", None, "", False),
    ("Spring Street Pizza", "Pizza", "", False),
    ("Scully", "Fine dining", "", False),
    ("Kioku by Endo Restaurant", "Japanese", "", False),
    ("Taste of India Pure Vegetarian", "Vegetarian", "The most authentic South Indian!", False),
    ("Lauretta's Pizzeria", "Pizza", "", False),
    ("Plaza Khao Gaeng Borough Yards", "Thai", "", False),
    ("Pockets", "Takeaway", "The best pita pocket in the world", False),
    ("Brigadiers", "Indian", "", False),
    ("Trinco Dulwich", None, "", False),
    ("Lenny's Apizza", "Pizza", "", True),
    ("2210 by NattyCanCook", "Caribbean", "", False),  # moved from want-to-go
    ("Hanbaagaasuuteeki", "Hamburger", "", False),      # moved from want-to-go
    ("Jikoni", "Asian fusion", "", False),
    ("Kamara Bar Soho", "Bar", "", False),
    ("Mambow", "Malaysian", "", False),                 # moved from want-to-go
    ("Shankeys", "Irish", "", False),                   # moved from want-to-go
    ("Mexican Seoul", None, "", False),
    ("manteca", "Italian", "", False),
    ("Flour & Grape", "Italian", "Great cheap pasta", False),
    ("Kricket Shoreditch", None, "Great breakfast too", False),
    ("The Kernel Spa Road", "Brewery", "End your tour of the beer mile here - food is really good", False),
    ("SOMA Soho", "Bar", "", False),
    ("Black Rock", "Bar", "", False),
    ("A Bar with Shapes for a Name", "Cocktail bar", "", False),
    ("Khao So-i", "Thai", "", False),
    ("TOAD Bakery", "Bakery", "", False),
    ("Chatsworth Bakehouse", "Bakery", "", False),
    ("Mr Bao Taiwanese Restaurant Peckham", "Taiwanese", "Best bottomless brunch + great food", False),
    ("The Dusty Knuckle Bakery", "Bakery", "Breakfast sandwiches", False),
    ("Nandine", "Middle Eastern", "", False),
    ("Acme Fire Cult", "Grill", "BBQ but still has decent veg options", False),
    ("Osteria Angelina", "Italian", "Italian Japanese", False),
    ("Kolamba East Sri Lankan Restaurant Liverpool Street", "Sri Lankan", "", False),
    ("The Devonshire", "Pub", "Classic British pub", False),
    ("The Water House Project", None, "Amazing seasonal set menu", True),
    ("Casa Fofo", "Fine dining", "", False),
    ("BAO City", None, "", False),
    ("Hakata Ramen + Bar", "Ramen", "", False),
    ("Pophams London Fields", "Bakery", "Bakery + Great Pasta", False),
    ("Normah's", "Malaysian", "", False),
    ("Gymkhana", "Indian", "Michelin Indian but worth it", False),
    ("AGORA souvla bar", None, "One of my fav restaurants in London", False),
    ("Crisp W1", "Pizza", "Best NY Style Pizza", False),
    ("Tangra", None, "Fully Veg", False),
    ("Bubala Soho", "Middle Eastern", "", False),
]

lambda_client = boto3.client("lambda", region_name=REGION)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def extract_area(address: str) -> str:
    if not address:
        return ""
    parts = [p.strip() for p in address.split(",")]
    for i, part in enumerate(parts):
        if part.lower().startswith("london ") or part.lower() == "london":
            if i > 0:
                candidate = parts[i - 1]
                if not re.match(r"^\d+", candidate) and len(candidate) < 40:
                    return candidate
            match = re.search(r"London\s+([A-Z]{1,2}\d[A-Z\d]?)", part)
            if match:
                return match.group(1)
    return ""


def extract_cuisine(place: dict) -> str:
    ptype = place.get("primaryType", "")
    if not ptype:
        return ""
    cleaned = ptype.replace("_restaurant", "").replace("_", " ").strip()
    return cleaned.title()


def enrich_one(name: str, cuisine_hint: str | None, note: str, closed: bool) -> dict:
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
                "my_note": note,
                "temporarily_closed": closed,
                "_status": "no_google_match",
            }
        top = places[0]
        display = top.get("displayName", {})
        display_name = display.get("text", name) if isinstance(display, dict) else name
        address = top.get("formattedAddress", "")
        area = extract_area(address)
        cuisine = cuisine_hint or extract_cuisine(top)
        result = {
            "slug": slugify(display_name),
            "name": display_name,
            "area": area,
            "cuisine": cuisine,
            "vegetarian_friendly": None,
            "my_note": note,
            "temporarily_closed": closed,
            "_google_address": address,
            "_google_primary_type": top.get("primaryType", ""),
        }
        # Drop temporarily_closed if False to keep output clean
        if not closed:
            result.pop("temporarily_closed")
        return result
    except Exception as e:
        return {
            "slug": slugify(name),
            "name": name,
            "area": "",
            "cuisine": cuisine_hint or "",
            "my_note": note,
            "_status": f"error: {str(e)[:120]}",
        }


def main():
    print(f"enriching {len(RAW)} recs via {LAMBDA_NAME}...")
    results = [None] * len(RAW)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(enrich_one, n, c, note, closed): i
            for i, (n, c, note, closed) in enumerate(RAW)
        }
        done = 0
        for f in as_completed(futures):
            i = futures[f]
            results[i] = f.result()
            done += 1
            r = results[i]
            status = r.get("_status", "ok")
            print(f"  [{done}/{len(RAW)}] {r['name']:45s} area={r.get('area','?'):20s} {status}")

    OUT_PATH.write_text(json.dumps(results, indent=2))
    print(f"\nwrote {OUT_PATH}")

    no_area = [r for r in results if not r.get("area")]
    no_match = [r for r in results if r.get("_status") == "no_google_match"]
    print(f"\n{len(results) - len(no_area) - len(no_match)} enriched with area, "
          f"{len(no_area) - len(no_match)} missing area only, "
          f"{len(no_match)} no google match")


if __name__ == "__main__":
    sys.exit(main() or 0)
