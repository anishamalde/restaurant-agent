"""
merge_recs — take recs_enrichment_out.json, resolve postcodes + odd
Google address segments to real neighbourhood names, drop closed
places (kept only temporarily_closed=true), and merge into
recs.json. Also removes the 4 places that got promoted from
want-to-go to recs. Uploads both files to S3.

Usage:
  python3 scripts/merge_recs.py           # writes locally + uploads both
  python3 scripts/merge_recs.py --dry-run # writes locally only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import boto3

REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-west-2")
BUCKET = os.environ.get("DATA_BUCKET", "")
RECS_KEY = "recs.json"
WANT_KEY = "want-to-go.json"

REPO = Path(__file__).parent.parent.parent
ENRICHMENT = Path(__file__).parent / "recs_enrichment_out.json"
RECS_JSON = REPO / "data" / "recs.json"
WANT_JSON = REPO / "data" / "want-to-go.json"

POSTCODE_DISTRICT_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$")

# Extended postcode map — includes both want-to-go and recs districts.
POSTCODE_TO_AREA: dict[str, str] = {
    # W (West End / West London)
    "W1A": "Marylebone",
    "W1B": "Mayfair",
    "W1D": "Soho",
    "W1F": "Soho",
    "W1H": "Marylebone",
    "W1K": "Mayfair",
    "W1S": "Mayfair",
    "W1T": "Fitzrovia",
    "W1U": "Marylebone",
    "W1W": "Fitzrovia",
    "W8":  "Kensington",
    "W9":  "Maida Vale",
    "W10": "North Kensington",
    "W11": "Notting Hill",
    # WC
    "WC2E": "Covent Garden",
    "WC2H": "Covent Garden",
    # EC
    "EC1V": "Old Street",
    "EC2A": "Shoreditch",
    "EC2N": "City",
    "EC4N": "City",
    # E
    "E1":   "Shoreditch",
    "E2":   "Bethnal Green",
    "E3":   "Bow",
    "E8":   "Hackney",
    "E9":   "Hackney",
    "E11":  "Leytonstone",
    "E12":  "Manor Park",
    "E17":  "Walthamstow",
    # N
    "N1":   "Islington",
    "N5":   "Highbury",
    "N15":  "Tottenham",
    "N16":  "Stoke Newington",
    # NW
    "NW1":  "Camden",
    "NW5":  "Kentish Town",
    # SE
    "SE1":  "Southwark",
    "SE4":  "Brockley",
    "SE5":  "Camberwell",
    "SE15": "Peckham",
    "SE16": "Bermondsey",
    "SE19": "Crystal Palace",
    "SE22": "East Dulwich",
    "SE24": "Herne Hill",
    # SW
    "SW1A": "Westminster",
    "SW1W": "Belgravia",
    "SW1Y": "St James's",
    "SW3":  "Chelsea",
    "SW6":  "Fulham",
    "SW10": "Chelsea",
    "SW11": "Battersea",
    "SW15": "Putney",
}

# Manual overrides for Google addresses that put a street or estate name
# in the neighbourhood slot, keyed by the enrichment's `_google_address`
# substring match.
ADDRESS_OVERRIDES: dict[str, str] = {
    # slug -> area  (used only when the auto-extracted area isn't good)
}

# Names or slugs whose auto-detected area needs replacing.
NAME_AREA_OVERRIDES: dict[str, str] = {
    "Two Twins Tacos": "Brixton",                          # Granville Arcade = Brixton Village
    "Spring Street Pizza": "Bankside",                     # Southwark St, SE1
    "A Bar with Shapes for a Name": "Hoxton",              # Whitmore Estate, N1
    "The Dusty Knuckle Bakery": "Dalston",                 # Abbot St, E8
    "Osteria Angelina": "Bermondsey",                      # Nicholls Clarke Yard, SE1
    "Acme Fire Cult": "Dalston",                           # Abbot St, E8
    "The Water House Project": "Bethnal Green",            # Cambridge Heath
    "Normah's": "Bayswater",                               # Queensway
    "Lenny's Apizza": "Finsbury Park",                     # already OK, keep
}


def resolve_area(existing_area: str, name: str) -> tuple[str, str]:
    override = NAME_AREA_OVERRIDES.get(name)
    if override:
        return override, "manual_override"
    if not existing_area:
        return "", "empty"
    if POSTCODE_DISTRICT_RE.match(existing_area):
        resolved = POSTCODE_TO_AREA.get(existing_area)
        if resolved:
            return resolved, f"postcode_map:{existing_area}"
        return existing_area, f"unmapped:{existing_area}"
    return existing_area, "already_name"


# Places to move OUT of want-to-go (slugs match what merge_want_to_go wrote)
MOVED_TO_RECS_SLUGS = {
    "mambow-clapton",   # original seed slug
    # The others were added in yesterday's merge with different slugs:
    # "2210-by-nattycancook", "hanbaagaasuuteeki", "shankeys"
    "2210-by-nattycancook",
    "hanbaagaasuuteeki",
    "shankeys",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Skip S3 upload")
    args = ap.parse_args()

    enriched = json.loads(ENRICHMENT.read_text())
    recs = json.loads(RECS_JSON.read_text())
    wants = json.loads(WANT_JSON.read_text())

    existing_slugs = {p["slug"] for p in recs["places"]}
    new_places = []
    unresolved: list[str] = []

    for entry in enriched:
        if entry["slug"] in existing_slugs:
            print(f"  skipped: {entry['name']} (already in recs)")
            continue

        area, source = resolve_area(entry.get("area", ""), entry["name"])
        if source.startswith("unmapped"):
            unresolved.append(f"  UNRESOLVED area for {entry['name']}: {entry['area']}")

        place = {
            "slug": entry["slug"],
            "name": entry["name"],
            "area": area,
            "cuisine": entry["cuisine"],
            "vegetarian_friendly": entry["vegetarian_friendly"],
            "my_note": entry.get("my_note", ""),
        }
        if entry.get("temporarily_closed"):
            place["temporarily_closed"] = True
        new_places.append(place)

    # Move the 4 duplicates out of want-to-go
    before_want = len(wants["places"])
    wants["places"] = [p for p in wants["places"] if p["slug"] not in MOVED_TO_RECS_SLUGS]
    removed_from_want = before_want - len(wants["places"])

    merged_recs = {
        "list_name": recs["list_name"],
        "description": recs["description"],
        "places": recs["places"] + new_places,
    }

    RECS_JSON.write_text(json.dumps(merged_recs, indent=2))
    WANT_JSON.write_text(json.dumps(wants, indent=2))

    print(f"\nwrote {RECS_JSON}")
    print(f"  existing: {len(recs['places'])}")
    print(f"  added:    {len(new_places)}")
    print(f"  total:    {len(merged_recs['places'])}")
    print(f"\nwrote {WANT_JSON}")
    print(f"  removed dupes: {removed_from_want}")
    print(f"  remaining:     {len(wants['places'])}")

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
        Key=RECS_KEY,
        Body=RECS_JSON.read_bytes(),
        ContentType="application/json",
    )
    s3.put_object(
        Bucket=BUCKET,
        Key=WANT_KEY,
        Body=WANT_JSON.read_bytes(),
        ContentType="application/json",
    )
    print(f"\nuploaded to s3://{BUCKET}/{RECS_KEY}")
    print(f"uploaded to s3://{BUCKET}/{WANT_KEY}")


if __name__ == "__main__":
    sys.exit(main() or 0)
