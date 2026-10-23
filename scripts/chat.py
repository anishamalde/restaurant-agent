"""
chat — talk to the deployed harness from your terminal.

Runs the same client-side tool-use loop as the WhatsApp webhook.
Full pipeline: real harness, real read/write Lambdas, real Google APIs, real S3.
Only the WhatsApp send is intercepted so it just prints to stdout.

Usage:
  # Interactive REPL
  python3 scripts/chat.py

  # Single-shot
  python3 scripts/chat.py "in Shoreditch tonight for 2, veggie friendly"

  # Verbose — print every tool call as it happens
  python3 scripts/chat.py --verbose

  # Fresh session (default keeps a stable session for this shell)
  python3 scripts/chat.py --fresh

  # Simulate a different sender
  python3 scripts/chat.py --as 447700900123
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import boto3


def load_dotenv(path: Path) -> None:
    """Populate os.environ from a KEY=VALUE .env file if present. No external deps."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv(Path(__file__).parent.parent / ".env")

# ── Config: pulled from env, populated by your SAM deployment ────────────────
HARNESS_ARN = os.environ.get("HARNESS_ARN")
if not HARNESS_ARN:
    sys.exit("HARNESS_ARN env var required — set it in .env or export it before running.")
REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-west-2")
DEFAULT_SENDER = os.environ.get("CHAT_SENDER", "447000000000")  # set CHAT_SENDER env var to your real E.164 number without +

TOOL_LAMBDA_MAP = {
    "list_by_area": "agent-fc-restaurant-read",
    "find_place": "agent-fc-restaurant-read",
    "walking_distance": "agent-fc-restaurant-read",
    "check_availability": "agent-fc-restaurant-read",
    "recent_visits": "agent-fc-restaurant-read",
    "visits_by_companion": "agent-fc-restaurant-read",
    "get_list": "agent-fc-restaurant-read",
    "search_google_places": "agent-fc-restaurant-read",
    "add_place": "agent-fc-add-place",
    "log_visit": "agent-fc-log-visit",
    # send_whatsapp intercepted below — no Lambda invocation
}

MAX_TOOL_TURNS = 10

# ── Colours ───────────────────────────────────────────────────────────────────
GREY = "\x1b[90m"
BLUE = "\x1b[34m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"

agentcore = boto3.client("bedrock-agentcore", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)


def read_stream(response):
    stop_reason = "end_turn"
    tool_calls = []
    text_parts = []
    current = None
    for event in response.get("stream", []):
        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start") or {}
            tu = start.get("toolUse")
            if tu:
                current = {"toolUseId": tu["toolUseId"], "name": tu["name"], "input_raw": ""}
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
            print(f"{RED}harness error: {event['internalServerException']}{RESET}", file=sys.stderr)
    return stop_reason, tool_calls, "".join(text_parts)


def invoke_tool(name: str, args: dict, actor_id: str, session_id: str, verbose: bool) -> Any:
    # Gateway-backed tools arrive as `<target>___<tool>`; the Lambdas switch on the bare name.
    name = name.rsplit("___", 1)[-1]
    if name == "send_whatsapp":
        # Intercept: don't actually send WhatsApp.
        msg = args.get("message", "")
        if verbose:
            print(f"{DIM}[send_whatsapp intercepted — not sending to Meta]{RESET}")
        # Return success so the harness completes
        return {"sent": True, "message_id": "LOCAL", "intercepted": True, "would_send": msg}

    fn = TOOL_LAMBDA_MAP.get(name)
    if not fn:
        return {"error": f"no Lambda mapped for {name}"}

    payload = {
        "tool_name": name,
        "tool_input": args,
        "session": {"actor_id": actor_id, "session_id": session_id},
    }

    if verbose:
        print(f"{DIM}  → {fn}({json.dumps(args)}){RESET}")

    resp = lambda_client.invoke(
        FunctionName=fn,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    body = json.loads(resp["Payload"].read())
    result = body.get("tool_output", body)

    if verbose:
        preview = json.dumps(result)[:200]
        print(f"{DIM}  ← {preview}{'...' if len(preview) == 200 else ''}{RESET}")

    return result


def send(user_text: str, session_id: str, actor_id: str, verbose: bool = False) -> str:
    """Send one user message through the harness and return the agent's reply."""
    messages = [{"role": "user", "content": [{"text": user_text}]}]
    final_reply = ""

    for turn in range(MAX_TOOL_TURNS):
        response = agentcore.invoke_harness(
            harnessArn=HARNESS_ARN,
            runtimeSessionId=session_id,
            # Memory actor IDs can't start with '+', so the harness gets bare digits.
            actorId=actor_id.lstrip("+"),
            messages=messages,
        )

        stop_reason, tool_calls, text = read_stream(response)

        if verbose and text:
            print(f"{GREY}[turn {turn}] {text[:120]}{RESET}")

        if stop_reason != "tool_use":
            final_reply = text
            break

        assistant_content = []
        tool_result_content = []
        for call in tool_calls:
            if verbose:
                print(f"{DIM}[{call['name']}]{RESET}")
            output = invoke_tool(call["name"], call["input"], actor_id, session_id, verbose)
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

            # If send_whatsapp fired, that's the agent's final answer.
            if call["name"] == "send_whatsapp":
                final_reply = output.get("would_send", "")

        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user", "content": tool_result_content})

    return final_reply


def make_session_id(sender: str, fresh: bool) -> str:
    if fresh:
        return f"chat-{sender}-{uuid.uuid4().hex[:12]}"
    # Stable per-shell session: reuse the same UUID5-derived ID our webhook uses
    return f"whatsapp-{sender}-{uuid.uuid5(uuid.NAMESPACE_DNS, sender).hex[:16]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", help="Send this message and exit. Omit for interactive REPL.")
    ap.add_argument("--verbose", "-v", action="store_true", help="Print tool calls as they happen")
    ap.add_argument("--fresh", action="store_true", help="New session for this run (no memory carryover)")
    ap.add_argument("--as", dest="sender", default=DEFAULT_SENDER, help="Simulated sender E.164 (no +)")
    args = ap.parse_args()

    session_id = make_session_id(args.sender, args.fresh)
    actor_id = f"+{args.sender}"

    print(f"{DIM}harness  {HARNESS_ARN.split('/')[-1]}{RESET}")
    print(f"{DIM}actor    {actor_id}{RESET}")
    print(f"{DIM}session  {session_id}{RESET}")
    print()

    def turn(text: str):
        print(f"{BLUE}you   > {text}{RESET}")
        reply = send(text, session_id, actor_id, args.verbose)
        print(f"{GREEN}agent > {reply}{RESET}\n")

    if args.text:
        turn(args.text)
        return

    # Interactive REPL
    print(f"{DIM}(type /quit to exit, /fresh to reset session){RESET}\n")
    while True:
        try:
            line = input(f"{BLUE}you   > {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        if line == "/fresh":
            session_id = make_session_id(args.sender, fresh=True)
            print(f"{DIM}new session: {session_id}{RESET}\n")
            continue
        reply = send(line, session_id, actor_id, args.verbose)
        print(f"{GREEN}agent > {reply}{RESET}\n")


if __name__ == "__main__":
    main()
