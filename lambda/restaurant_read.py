"""
restaurant_read — one Lambda serving all read tools for the restaurant agent.

Exposed to the harness as 8 tools on the `restaurantRead` Gateway target (list_by_area, find_place,
walking_distance, check_availability, recent_visits, visits_by_companion,
get_list, search_google_places). Invoked by Gateway or by the webhook's
tool-use loop; we dispatch on the tool name.

Env:
  DATA_BUCKET           — S3 bucket with recs.json / want-to-go.json / visit-log.json / availability.json
  GOOGLE_MAPS_API_KEY   — Google Cloud API key (Places API New + Distance Matrix)
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Any

import boto3
import httpx

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DATA_BUCKET = os.environ["DATA_BUCKET"]
API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]

s3 = boto3.client("s3")

PLACES_BASE = "https://places.googleapis.com/v1/places"
DISTANCE_BASE = "https://maps.googleapis.com/maps/api/distancematrix/json"
PLACE_FIELDS = "id,displayName,formattedAddress,location,priceLevel,rating,userRatingCount,regularOpeningHours,primaryType"


def _extract(event: dict[str, Any], context: Any) -> tuple[str | None, dict[str, Any], str]:
    """
    Return (tool_name, tool_input, source) supporting both invocation shapes:

    Gateway shape (server-side dispatch via AgentCore Gateway):
      event = {"name": "recs", "area": "Shoreditch"}     # flat inputs
      context.client_context.custom["bedrockAgentCoreToolName"] = "restaurantRead___list_by_area"

    Legacy shape (webhook Lambda dispatching directly):
      event = {"tool_name": "list_by_area", "tool_input": {...}, "session": {...}}
    """
    client_ctx = getattr(context, "client_context", None) if context else None
    if client_ctx is not None:
        custom = getattr(client_ctx, "custom", None) or {}
        gw_name = custom.get("bedrockAgentCoreToolName")
        if gw_name:
            return gw_name.split("___")[-1], event, "gateway"
    tool_name = event.get("tool_name") or (event.get("tool") or {}).get("name")
    return tool_name, event.get("tool_input") or {}, "legacy"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Dual-shape: accepts both Gateway and legacy webhook invocations."""
    tool_name, tool_input, source = _extract(event, context)
    logger.info("read source=%s tool=%s input=%s", source, tool_name, tool_input)

    try:
        result = _dispatch(tool_name, tool_input)
        # Gateway consumes plain JSON; legacy callers expect {tool_output: ...}
        return result if source == "gateway" else {"tool_output": result}
    except Exception as exc:
        logger.exception("read tool failed")
        err = {"error": str(exc)[:200]}
        return err if source == "gateway" else {"tool_output": err}


def _dispatch(tool_name: str, args: dict[str, Any]) -> Any:
    if tool_name == "get_list":
        return _load(_list_key(args["name"]))

    if tool_name == "list_by_area":
        doc = _load(_list_key(args["name"]))
        q = args["area"].lower()
        return [p for p in doc["places"] if q in p["area"].lower()]

    if tool_name == "find_place":
        return _find_place(args["query"], args.get("threshold", 0.6))

    if tool_name == "search_google_places":
        return _google_search(args["query"], args["area"])

    if tool_name == "walking_distance":
        return _walking_distance(args["from_area"], args["to_query"])

    if tool_name == "check_availability":
        return _check_availability(
            args["place_name"], args["area"], args["when"], args["party_size"]
        )

    if tool_name == "recent_visits":
        return _recent_visits(args.get("days", 30))

    if tool_name == "visits_by_companion":
        return _visits_by_companion(args["companion"])

    raise ValueError(f"unknown tool_name: {tool_name!r}")


# ─── Saved lists ────────────────────────────────────────────────────────────

def _list_key(name: str) -> str:
    if name == "recs":
        return "recs.json"
    if name == "want-to-go":
        return "want-to-go.json"
    raise ValueError(f"unknown list {name!r}, use 'recs' or 'want-to-go'")


def _find_place(query: str, threshold: float) -> list[dict[str, Any]]:
    q = query.lower()
    hits: list[dict[str, Any]] = []
    for list_name, key in (("recs", "recs.json"), ("want-to-go", "want-to-go.json")):
        for place in _load(key)["places"]:
            score = SequenceMatcher(None, q, place["name"].lower()).ratio()
            if score >= threshold:
                hits.append({**place, "list": list_name, "match_score": round(score, 3)})
    return sorted(hits, key=lambda h: h["match_score"], reverse=True)


# ─── Google Places ───────────────────────────────────────────────────────────

def _google_search(query: str, area: str) -> list[dict[str, Any]]:
    body = {"textQuery": f"{query} in {area} London", "maxResultCount": 5}
    headers = {
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": "places." + ",places.".join(PLACE_FIELDS.split(",")),
    }
    with httpx.Client(timeout=10.0) as client:
        r = client.post(f"{PLACES_BASE}:searchText", json=body, headers=headers)
        r.raise_for_status()
        return r.json().get("places", [])


def _walking_distance(from_area: str, to_query: str) -> dict[str, Any]:
    params = {
        "origins": f"{from_area}, London, UK",
        "destinations": f"{to_query}, London, UK",
        "mode": "walking",
        "key": API_KEY,
    }
    with httpx.Client(timeout=10.0) as client:
        r = client.get(DISTANCE_BASE, params=params)
        r.raise_for_status()
        data = r.json()
    element = data["rows"][0]["elements"][0]
    if element["status"] != "OK":
        return {"status": element["status"], "walking_minutes": None}
    return {
        "status": "OK",
        "walking_minutes": round(element["duration"]["value"] / 60),
        "distance_meters": element["distance"]["value"],
    }


# ─── Availability (mocked) ───────────────────────────────────────────────────

def _check_availability(place_name: str, area: str, when: str, party_size: int) -> dict[str, Any]:
    slug = re.sub(r"[^a-z0-9]+", "-", f"{place_name}-{area}".lower()).strip("-")
    data = _load("availability.json")["availability"]

    if slug not in data:
        return {"available": False, "slots": [], "reason": f"no availability data for {slug}"}

    slots = data[slug].get(when, [])
    if not slots:
        walk_in = data[slug].get("_walk_in_only_note")
        return {"available": False, "slots": [], "reason": walk_in or "no availability"}

    matching = [s for s in slots if s["party_size_max"] >= party_size]
    return {
        "available": bool(matching),
        "slots": matching,
        "reason": "ok" if matching else "party too big for available slots",
    }


# ─── Visit history ───────────────────────────────────────────────────────────

def _recent_visits(days: int) -> list[dict[str, Any]]:
    cutoff = date.today() - timedelta(days=days)
    visits = _load("visit-log.json")["visits"]
    return sorted(
        [v for v in visits if date.fromisoformat(v["visited_on"]) >= cutoff],
        key=lambda v: v["visited_on"],
        reverse=True,
    )


def _visits_by_companion(companion: str) -> list[dict[str, Any]]:
    visits = _load("visit-log.json")["visits"]
    return sorted(
        [v for v in visits if companion in (v.get("companions") or [])],
        key=lambda v: v["visited_on"],
        reverse=True,
    )


# ─── S3 helper ───────────────────────────────────────────────────────────────

def _load(key: str) -> dict[str, Any]:
    obj = s3.get_object(Bucket=DATA_BUCKET, Key=key)
    return json.loads(obj["Body"].read())
