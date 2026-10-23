# One-off data bootstrap scripts

These built the seed data in `data/` from my Google Maps saved places. Kept as reference for how to enrich a plain list of restaurant names into structured data the agent can query.

## What they do

| Script | Purpose |
|---|---|
| `enrich_want_to_go.py` | Reads a hard-coded list of names, fans out to `agent-fc-restaurant-read` Lambda (`search_google_places`), writes `enrichment_out.json` with the extracted area, cuisine and address. |
| `enrich_recs.py` | Same, for the recs list. Preserves user notes and flags temporarily-closed places. |
| `merge_want_to_go.py` | Reads the enrichment JSON, resolves London postcodes to neighbourhood names, merges into `data/want-to-go.json`, uploads to S3. |
| `merge_recs.py` | Same for recs. Also moves promoted duplicates out of `want-to-go`. |

## Running them again

The input lists are hard-coded (they're my places, so swap in yours). To add more places:

1. Edit the `RAW` / `RAW_NAMES` list in `enrich_*.py`
2. `python3 scripts/tools/enrich_recs.py` (or `enrich_want_to_go.py`)
3. Review the `*enrichment_out.json` output and spot-check Google's matches
4. `python3 scripts/tools/merge_recs.py --dry-run` to preview
5. `DATA_BUCKET=<bucket> python3 scripts/tools/merge_recs.py` to write locally and upload to S3

## Non-obvious bits

- **Postcode-to-neighbourhood map** lives in `merge_want_to_go.py` and `merge_recs.py`. When Google's `formattedAddress` doesn't include a named neighbourhood (common for W1, EC2 etc.), we resolve the postcode district manually. Extend the map when new districts appear.
- **`NAME_AREA_OVERRIDES`** in `merge_recs.py` handles Google's occasional weird address segments (`Granville Arcade` → Brixton, `Abbot St` → Dalston).
- **Merge is safe to re-run**: it skips existing slugs. But the enrichment step calls Google Places for every entry every time, so bill accordingly.
