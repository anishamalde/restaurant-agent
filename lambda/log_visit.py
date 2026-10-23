"""
log_visit — inline function handler. Owner-only.

Appends to visit-log.json. Updates last_visited + visit_count on the place.
If verdict=='recommend' and the place is on want-to-go, moves it to recs.

Env:
  DATA_BUCKET             — S3 bucket
  OWNER_WHATSAPP_NUMBER   — E.164, only this actor_id can write.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DATA_BUCKET = os.environ["DATA_BUCKET"]
OWNER = os.environ["OWNER_WHATSAPP_NUMBER"]

s3 = boto3.client("s3")

RECS_KEY = "recs.json"
WISHLIST_KEY = "want-to-go.json"
VISIT_LOG_KEY = "visit-log.json"


def _via_gateway(context: Any) -> bool:
    """True when this invocation came from AgentCore Gateway (server-side dispatch)."""
    client_ctx = getattr(context, "client_context", None) if context else None
    if client_ctx is None:
        return False
    custom = getattr(client_ctx, "custom", None) or {}
    return bool(custom.get("bedrockAgentCoreToolName"))


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Dual-shape: legacy webhook path enforces actor_id auth; Gateway path trusts the caller."""
    gateway = _via_gateway(context)

    if gateway:
        args = event
    else:
        session = event.get("session") or {}
        if session.get("actor_id") != OWNER:
            logger.warning("rejected log_visit from actor_id=%r", session.get("actor_id"))
            return {"tool_output": {"logged": False, "reason": "not authorized"}}
        args = event.get("tool_input") or {}

    slug = args["place_slug"]
    visited_on = args.get("visited_on") or date.today().isoformat()
    verdict = args["verdict"]
    companions = args.get("companions") or []
    note = args.get("note", "")

    # 1. Append to the visit log
    visit_log = _load(VISIT_LOG_KEY)
    visit = {
        "id": f"visit-{visited_on}-{slug}",
        "place_slug": slug,
        "visited_on": visited_on,
        "verdict": verdict,
        "companions": companions,
        "note": note,
        "logged_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    visit_log["visits"].append(visit)
    _save(VISIT_LOG_KEY, visit_log)

    # 2. Update the place record on whichever list it lives on
    recs = _load(RECS_KEY)
    wishlist = _load(WISHLIST_KEY)

    place, source_list = _find(slug, recs, wishlist)
    if place is None:
        logger.warning("log_visit: unknown slug %s (visit still logged)", slug)
        result = {"logged": True, "moved": False, "reason": f"slug {slug!r} not on any list"}
        return result if gateway else {"tool_output": result}

    place["last_visited"] = visited_on
    place["visit_count"] = (place.get("visit_count") or 0) + 1
    place["my_verdict"] = verdict
    if note:
        place["my_note"] = note

    # 3. If they recommend a wishlist place, promote it to recs.
    #    If they skip a recs place, we DON'T auto-remove — surface the question in the reply.
    moved = False
    if source_list == "want-to-go" and verdict == "recommend":
        wishlist["places"] = [p for p in wishlist["places"] if p["slug"] != slug]
        recs["places"].append(place)
        _save(WISHLIST_KEY, wishlist)
        moved = True

    _save(RECS_KEY, recs)

    logger.info("logged visit for %s verdict=%s moved=%s (source=%s)", slug, verdict, moved, "gateway" if gateway else "legacy")
    result = {
        "logged": True,
        "moved": moved,
        "source_list": source_list,
        "target_list": "recs" if moved else source_list,
    }
    return result if gateway else {"tool_output": result}


def _find(slug: str, recs: dict, wishlist: dict) -> tuple[dict | None, str | None]:
    for p in recs["places"]:
        if p["slug"] == slug:
            return p, "recs"
    for p in wishlist["places"]:
        if p["slug"] == slug:
            return p, "want-to-go"
    return None, None


def _load(key: str) -> dict[str, Any]:
    obj = s3.get_object(Bucket=DATA_BUCKET, Key=key)
    return json.loads(obj["Body"].read())


def _save(key: str, doc: dict[str, Any]) -> None:
    s3.put_object(
        Bucket=DATA_BUCKET,
        Key=key,
        Body=json.dumps(doc, indent=2).encode(),
        ContentType="application/json",
    )
