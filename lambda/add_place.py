"""
add_place — inline function handler. Owner-only.

Appends a new place to recs.json or want-to-go.json in S3. Refuses
if the caller isn't the owner, even if the model somehow triggered it.

Env:
  DATA_BUCKET             — S3 bucket
  OWNER_WHATSAPP_NUMBER   — E.164, e.g. +447700900000. Only this actor_id can write.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DATA_BUCKET = os.environ["DATA_BUCKET"]
OWNER = os.environ["OWNER_WHATSAPP_NUMBER"]

s3 = boto3.client("s3")

FILE_FOR = {
    "recs": "recs.json",
    "want-to-go": "want-to-go.json",
}


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
        args = event  # Gateway passes inputs flat
    else:
        # Legacy path — WhatsApp inbound. Belt-and-braces: only the owner's number can write.
        session = event.get("session") or {}
        if session.get("actor_id") != OWNER:
            logger.warning("rejected add_place from actor_id=%r (expected %r)", session.get("actor_id"), OWNER)
            return {"tool_output": {"added": False, "reason": "not authorized"}}
        args = event.get("tool_input") or {}

    target_list = args.get("target_list", "want-to-go")
    if target_list not in FILE_FOR:
        result = {"added": False, "reason": f"unknown target_list {target_list!r}"}
        return result if gateway else {"tool_output": result}

    key = FILE_FOR[target_list]
    doc = _load(key)

    slug = _slugify(args["name"], args["area"])
    if any(p["slug"] == slug for p in doc["places"]):
        result = {"added": False, "reason": f"{slug} already on {target_list}"}
        return result if gateway else {"tool_output": result}

    new_place = {
        "slug": slug,
        "name": args["name"],
        "cuisine": args.get("cuisine", "unknown"),
        "area": args["area"],
        "google_place_id": args["google_place_id"],
        "added_on": date.today().isoformat(),
        "my_note": args.get("note", ""),
    }
    doc["places"].append(new_place)
    _save(key, doc)

    logger.info("added %s to %s (source=%s)", slug, target_list, "gateway" if gateway else "legacy")
    result = {"added": True, "slug": slug, "list": target_list}
    return result if gateway else {"tool_output": result}


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


def _slugify(name: str, area: str) -> str:
    """gloria-shoreditch etc — lower, ascii-ish, dashed."""
    combined = f"{name}-{area}"
    return re.sub(r"[^a-z0-9]+", "-", combined.lower()).strip("-")
