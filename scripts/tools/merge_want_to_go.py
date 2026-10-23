"""
merge_want_to_go — take enrichment_out.json, resolve postcodes to
neighbourhoods, drop non-London matches, and merge into want-to-go.json.
Then upload the merged file to S3.

Usage:
  python3 scripts/merge_want_to_go.py           # writes locally + uploads
  python3 scripts/merge_want_to_go.py --dry-run # writes locally only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import boto3

# UK London postcode district: 1-2 letters + digit + optional letter/digit
POSTCODE_DISTRICT_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$")

REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-west-2")
BUCKET = os.environ.get("DATA_BUCKET", "")
KEY = "want-to-go.json"

REPO = Path(__file__).parent.parent.parent
ENRICHMENT = Path(__file__).parent / "enrichment_out.json"
LOCAL_JSON = REPO / "data" / "want-to-go.json"

# London postcode district -> neighbourhood name.
# These are the districts that appeared in our enrichment (plus common ones).
# When multiple neighbourhoods share a district, pick the best-known.
POSTCODE_TO_AREA: dict[str, str] = {
    # W (West End / West London)
    "W1A": "Marylebone",       # Selfridges — Oxford Street
    "W1B": "Mayfair",           # Regent St
    "W1D": "Soho",
    "W1H": "Marylebone",
    "W1K": "Mayfair",
    "W1T": "Fitzrovia",
    "W1U": "Marylebone",
    "W8":  "Kensington",
    "W9":  "Maida Vale",
    "W10": "North Kensington",
    "W11": "Notting Hill",
    # WC (West Central)
    "WC2E": "Covent Garden",
    "WC2H": "Covent Garden",
    # EC (East Central / City)
    "EC1V": "Old Street",
    "EC2A": "Shoreditch",
    # E (East)
    "E1":   "Shoreditch",       # Shoreditch High St side
    "E2":   "Bethnal Green",
    "E8":   "Hackney",
    "E9":   "Hackney",
    "E11":  "Leytonstone",
    "E17":  "Walthamstow",
    # N (North)
    "N1":   "Islington",
    "N5":   "Highbury",
    "N15":  "Tottenham",
    "N16":  "Stoke Newington",
    # NW
    "NW1":  "Camden",
    "NW5":  "Kentish Town",
    # SE (South East)
    "SE1":  "Southwark",
    "SE4":  "Brockley",
    "SE5":  "Camberwell",
    "SE15": "Peckham",
    "SE22": "East Dulwich",
    "SE24": "Herne Hill",
    # SW
    "SW1W": "Belgravia",
    "SW3":  "Chelsea",
    "SW6":  "Fulham",
    "SW10": "Chelsea",
    "SW11": "Battersea",
    "SW15": "Putney",
}

# Names to skip after review (see Assistant's earlier flag)
SKIP_NAMES = {
    "Restaurant Henne",  # matched Moreton-in-Marsh, Gloucestershire — not London
}


def resolve_area(existing_area: str) -> tuple[str, str]:
    """
    If area looks like a postcode district, try the map.
    Otherwise treat as a neighbourhood name and keep it.
    Returns (resolved_area, source).
    """
    if not existing_area:
        return "", "empty"
    if POSTCODE_DISTRICT_RE.match(existing_area):
        resolved = POSTCODE_TO_AREA.get(existing_area)
        if resolved:
            return resolved, f"postcode_map:{existing_area}"
        return existing_area, f"unmapped:{existing_area}"
    return existing_area, "already_name"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Skip S3 upload")
    args = ap.parse_args()

    enriched = json.loads(ENRICHMENT.read_text())
    existing = json.loads(LOCAL_JSON.read_text())

    existing_slugs = {p["slug"] for p in existing["places"]}
    new_places = []
    skipped: list[str] = []
    unresolved: list[str] = []

    for entry in enriched:
        if entry["name"] in SKIP_NAMES:
            skipped.append(f"  skipped: {entry['name']} (in SKIP_NAMES)")
            continue
        if entry["slug"] in existing_slugs:
            skipped.append(f"  skipped: {entry['name']} (duplicate slug)")
            continue

        area, source = resolve_area(entry.get("area", ""))
        if source.startswith("unmapped"):
            unresolved.append(f"  UNRESOLVED area for {entry['name']}: {entry['area']}")

        new_places.append({
            "slug": entry["slug"],
            "name": entry["name"],
            "area": area,
            "cuisine": entry["cuisine"],
            "vegetarian_friendly": entry["vegetarian_friendly"],
            "wishlist_reason": entry.get("wishlist_reason", ""),
        })

    merged = {
        "list_name": existing["list_name"],
        "description": existing["description"],
        "places": existing["places"] + new_places,
    }

    LOCAL_JSON.write_text(json.dumps(merged, indent=2))
    print(f"\nwrote {LOCAL_JSON}")
    print(f"  existing: {len(existing['places'])}")
    print(f"  added:    {len(new_places)}")
    print(f"  total:    {len(merged['places'])}")

    if skipped:
        print("\n" + "\n".join(skipped))
    if unresolved:
        print("\n" + "\n".join(unresolved))

    if args.dry_run:
        print("\n[dry-run] skipping S3 upload")
        return

    if not BUCKET:
        sys.exit("DATA_BUCKET env var required for upload (or pass --dry-run).")
    s3 = boto3.client("s3", region_name=REGION)
    s3.put_object(
        Bucket=BUCKET,
        Key=KEY,
        Body=LOCAL_JSON.read_bytes(),
        ContentType="application/json",
    )
    print(f"\nuploaded to s3://{BUCKET}/{KEY}")


if __name__ == "__main__":
    sys.exit(main() or 0)
