"""
whatsapp_webhook — Lambda that:
  1) Receives Meta WhatsApp Cloud API events and verifies their signature
  2) Invokes the AgentCore harness
  3) Handles the harness's inline function tool-use pause loop:
       harness pauses -> we invoke the right Lambda -> we re-invoke the harness
       with the tool result -> repeat until stopReason='end_turn'.

Env:
  HARNESS_ARN               — the deployed harness to invoke
  META_VERIFY_TOKEN         — token you configured in Meta's app dashboard
  META_APP_SECRET           — Meta app secret; used to verify X-Hub-Signature-256 on
                              every POST. If unset, all inbound messages are rejected.
  TOOL_LAMBDA_MAP           — JSON string mapping tool_name -> Lambda function name.
                              Defaults if not set.

Reference:
  https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks
  https://developers.facebook.com/docs/graph-api/webhooks/getting-started#event-notifications
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import uuid
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HARNESS_ARN = os.environ["HARNESS_ARN"]
VERIFY_TOKEN = os.environ["META_VERIFY_TOKEN"]
APP_SECRET = os.environ.get("META_APP_SECRET", "")

agentcore = boto3.client("bedrock-agentcore")
lambda_client = boto3.client("lambda")

# Which Lambda handles each tool.
#
# The agent calls tools by two possible names:
#   1. Legacy names (list_by_area, add_place, ...) — from the old inline_function
#      config. Some pre-migration sessions may still emit these.
#   2. Gateway-prefixed names (restaurantRead___list_by_area, addPlace___add_place,
#      logVisit___log_visit) — emitted since we migrated 10 tools to AgentCore
#      Gateway. Gateway prefixes tool names with `<target>___` when exposing them.
#
# Both routes eventually invoke the same backing Lambdas. The Lambdas are
# dual-shape and handle either input format.
_READ = "agent-fc-restaurant-read"
_ADD = "agent-fc-add-place"
_LOG = "agent-fc-log-visit"
_SEND = "agent-fc-whatsapp-send"

DEFAULT_TOOL_MAP: dict[str, str] = {
    # ── Legacy tool names ────────────────────────────────────────────────
    "list_by_area":                        _READ,
    "find_place":                          _READ,
    "walking_distance":                    _READ,
    "check_availability":                  _READ,
    "recent_visits":                       _READ,
    "visits_by_companion":                 _READ,
    "get_list":                            _READ,
    "search_google_places":                _READ,
    "add_place":                           _ADD,
    "log_visit":                           _LOG,
    "send_whatsapp":                       _SEND,
    # ── Gateway-prefixed names (post-migration) ──────────────────────────
    "restaurantRead___list_by_area":       _READ,
    "restaurantRead___find_place":         _READ,
    "restaurantRead___walking_distance":   _READ,
    "restaurantRead___check_availability": _READ,
    "restaurantRead___recent_visits":      _READ,
    "restaurantRead___visits_by_companion": _READ,
    "restaurantRead___get_list":           _READ,
    "restaurantRead___search_google_places": _READ,
    "addPlace___add_place":                _ADD,
    "logVisit___log_visit":                _LOG,
}
TOOL_MAP = json.loads(os.environ.get("TOOL_LAMBDA_MAP") or json.dumps(DEFAULT_TOOL_MAP))

MAX_TOOL_TURNS = 10  # safety cap


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    method = event.get("requestContext", {}).get("http", {}).get("method", "")

    if method == "GET":
        return _verify_webhook(event)
    if method == "POST":
        return _handle_inbound(event)

    return {"statusCode": 405, "body": "method not allowed"}


def _verify_webhook(event: dict[str, Any]) -> dict[str, Any]:
    params = event.get("queryStringParameters") or {}
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and token and hmac.compare_digest(token, VERIFY_TOKEN):
        logger.info("webhook verified")
        return {"statusCode": 200, "body": challenge}

    logger.warning("webhook verify failed")
    return {"statusCode": 403, "body": "verification failed"}


def _raw_body(event: dict[str, Any]) -> bytes:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body)
    return body.encode()


def _signature_valid(event: dict[str, Any], raw_body: bytes) -> bool:
    """
    Meta signs every webhook POST with HMAC-SHA256 of the raw body, keyed by the
    app secret. Without this check anyone who finds the URL can forge a message
    "from" the owner's number and trigger writes or outbound sends. Fails closed.
    """
    if not APP_SECRET:
        logger.error("META_APP_SECRET not set; rejecting inbound POST")
        return False
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    received = headers.get("x-hub-signature-256", "")
    expected = "sha256=" + hmac.new(APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


def _handle_inbound(event: dict[str, Any]) -> dict[str, Any]:
    raw_body = _raw_body(event)
    if not _signature_valid(event, raw_body):
        logger.warning("rejected inbound POST: bad or missing X-Hub-Signature-256")
        return {"statusCode": 401, "body": "invalid signature"}

    try:
        body = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "invalid json"}

    message = _extract_message(body)
    if not message:
        return {"statusCode": 200, "body": "ok"}

    sender = message["from"]              # E.164 without +
    inbound_text = message["text"]["body"]
    session_id = _session_for(sender)
    actor_id = f"+{sender}"

    logger.info("harness call for %s (session %s): %r", sender, session_id, inbound_text)

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"text": inbound_text}]}
    ]

    send_whatsapp_fired = False
    last_assistant_text = ""

    for turn in range(MAX_TOOL_TURNS):
        response = agentcore.invoke_harness(
            harnessArn=HARNESS_ARN,
            runtimeSessionId=session_id,
            # Memory actor IDs can't start with '+', so the harness gets bare digits.
            actorId=actor_id.lstrip("+"),
            messages=messages,
        )

        stop_reason, tool_calls, assistant_text = _read_stream(response)
        if assistant_text:
            last_assistant_text = assistant_text
        logger.info(
            "turn %d stopReason=%s tool_calls=%d text=%r",
            turn, stop_reason, len(tool_calls), assistant_text[:400] if assistant_text else "",
        )

        if stop_reason != "tool_use":
            # end_turn / max_tokens / timeout — the harness is done.
            # If the agent forgot to call send_whatsapp but produced text, send it ourselves.
            break

        # Dispatch each tool call, build the assistant + tool result messages
        assistant_content = []
        tool_result_content = []
        for call in tool_calls:
            if call["name"] == "send_whatsapp":
                # Replies only ever go back to whoever messaged us. Stops a
                # prompt-injected "text +44... instead" from reaching anyone else.
                call["input"] = {**call["input"], "to": actor_id}
                send_whatsapp_fired = True
            output = _invoke_tool(call["name"], call["input"], actor_id, session_id)
            assistant_content.append({
                "toolUse": {
                    "toolUseId": call["toolUseId"],
                    "name": call["name"],
                    "input": call["input"],
                }
            })
            tool_result_content.append({
                "toolResult": {
                    "toolUseId": call["toolUseId"],
                    "content": [{"text": json.dumps(output)}],
                    "status": "success",
                }
            })

        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user", "content": tool_result_content})

    # Fallback: if the agent never called send_whatsapp but did produce a reply,
    # dispatch it ourselves so the user actually gets a message back.
    if not send_whatsapp_fired and last_assistant_text.strip():
        logger.warning(
            "send_whatsapp not called; falling back with plain assistant text (%d chars)",
            len(last_assistant_text),
        )
        _invoke_tool(
            "send_whatsapp",
            {"to": actor_id, "message": last_assistant_text},
            actor_id,
            session_id,
        )
    elif not send_whatsapp_fired:
        logger.error("send_whatsapp not called AND no assistant text produced")

    return {"statusCode": 200, "body": "ok"}


def _read_stream(response: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str]:
    """
    Consume the harness response stream.
    Returns (stopReason, tool_calls, assistant_text) — the plain-text answer
    the agent produced (empty if the turn ended with tool calls only).
    """
    stop_reason = "end_turn"
    tool_calls: list[dict[str, Any]] = []
    text_parts: list[str] = []
    current: dict[str, Any] | None = None

    for event in response.get("stream", []):
        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start") or {}
            tu = start.get("toolUse")
            if tu:
                current = {
                    "toolUseId": tu["toolUseId"],
                    "name": tu["name"],
                    "input_raw": "",
                }
        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta") or {}
            tu = delta.get("toolUse")
            if tu and current is not None:
                current["input_raw"] += tu.get("input", "")
            elif "text" in delta:
                text_parts.append(delta["text"])
        elif "contentBlockStop" in event:
            if current is not None:
                raw = current.pop("input_raw", "")
                try:
                    current["input"] = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    current["input"] = {}
                tool_calls.append(current)
                current = None
        elif "messageStop" in event:
            stop_reason = event["messageStop"].get("stopReason", stop_reason)
        elif "internalServerException" in event:
            logger.error("harness error: %s", event["internalServerException"])

    return stop_reason, tool_calls, "".join(text_parts)


def _invoke_tool(tool_name: str, tool_input: dict[str, Any], actor_id: str, session_id: str) -> Any:
    """Invoke the right Lambda for a given tool call. Returns the tool_output payload."""
    fn = TOOL_MAP.get(tool_name)
    if not fn:
        logger.error("no Lambda mapped for tool %s", tool_name)
        return {"error": f"tool {tool_name!r} not implemented"}

    # Gateway prefixes tool names with `<target>___`. Strip so the backing
    # Lambda's internal dispatcher (which switches on the bare name) matches.
    bare_name = tool_name.rsplit("___", 1)[-1]

    payload = {
        "tool_name": bare_name,
        "tool_input": tool_input,
        "session": {"actor_id": actor_id, "session_id": session_id},
    }
    logger.info("invoking %s for tool %s (bare=%s)", fn, tool_name, bare_name)

    try:
        resp = lambda_client.invoke(
            FunctionName=fn,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode(),
        )
        body = json.loads(resp["Payload"].read())
        return body.get("tool_output", body)
    except Exception as exc:
        logger.exception("tool invoke failed")
        return {"error": str(exc)[:200]}


def _extract_message(body: dict[str, Any]) -> dict[str, Any] | None:
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []) or []:
                if msg.get("type") == "text":
                    return msg
    return None


def _session_for(sender: str) -> str:
    return f"whatsapp-{sender}-{uuid.uuid5(uuid.NAMESPACE_DNS, sender).hex[:16]}"
