"""
whatsapp_send — inline function handler that sends a WhatsApp reply.

Wired to the `send_whatsapp` inline function tool declared in harness.json.
Every flow ends with a call to this.

Env:
  META_TOKEN      — WhatsApp Cloud API access token
  META_PHONE_ID   — the sender phone-number ID from Meta

Reference:
  https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger()
logger.setLevel(logging.INFO)

META_TOKEN = os.environ["META_TOKEN"]
META_PHONE_ID = os.environ["META_PHONE_ID"]
GRAPH_URL = f"https://graph.facebook.com/v21.0/{META_PHONE_ID}/messages"


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """
    Event shape from the harness inline function invocation:
      {
        "tool_input": {"to": "+447700900001", "message": "Try Gloria..."},
        "session": {"actor_id": "+447700900001", "session_id": "..."}
      }
    """
    tool_input = event.get("tool_input") or {}
    session = event.get("session") or {}

    to = tool_input.get("to") or session.get("actor_id")
    message = tool_input.get("message")

    if not to or not message:
        logger.error("missing to/message: to=%r message_len=%s", to, len(message or ""))
        return {"tool_output": {"sent": False, "reason": "missing to or message"}}

    # WhatsApp wants the number without a leading +
    normalized_to = to.lstrip("+")

    payload = {
        "messaging_product": "whatsapp",
        "to": normalized_to,
        "type": "text",
        "text": {"body": message[:4096]},  # WhatsApp max message length
    }
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=10.0) as client:
        r = client.post(GRAPH_URL, headers=headers, json=payload)

    if r.is_success:
        logger.info("sent %d chars", len(message))
        return {"tool_output": {"sent": True, "message_id": r.json().get("messages", [{}])[0].get("id")}}

    logger.error("send failed: status=%s body=%s", r.status_code, r.text)
    return {"tool_output": {"sent": False, "reason": f"meta_api_error_{r.status_code}"}}
