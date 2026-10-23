"""
local_run — invoke the webhook handler locally against deployed AWS resources.

Fastest local iteration loop:
  1. Change whatsapp_webhook.py or the prompt in harness.json
  2. `python3 scripts/local_run.py "your test message"`
  3. See the full tool-use loop happen against real deployed Lambdas + harness
  4. Reply is sent to your real WhatsApp (or --dry-run to skip the send)

Env vars needed (or paste as .env — see below):
  HARNESS_ARN, META_VERIFY_TOKEN, META_APP_SECRET (used to sign the fake payload)

Usage:
  # Simplest — uses default test prompt
  python3 scripts/local_run.py

  # Custom prompt
  python3 scripts/local_run.py "in Soho tonight for 3, no strong preferences"

  # Fresh session — different sender number so harness starts new conversation
  python3 scripts/local_run.py "went to Gloria last night" --sender 447700900123

  # Dry run — process the message but don't send the final WhatsApp reply
  python3 scripts/local_run.py "in Shoreditch tonight" --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path


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

# Harmless defaults for locals the webhook imports at module-load but the local
# runner never actually uses (real send is intercepted or dispatched to the
# deployed Lambda, which reads its own env).
HARMLESS_DEFAULTS = {
    "AWS_DEFAULT_REGION": "eu-west-2",
    "META_TOKEN": "not-used-locally",
    "META_PHONE_ID": "not-used-locally",
}

REQUIRED_ENV = ("HARNESS_ARN", "META_VERIFY_TOKEN", "META_APP_SECRET")


def load_env():
    """Populate harmless defaults; fail loud if the real ones are missing."""
    for k, v in HARMLESS_DEFAULTS.items():
        os.environ.setdefault(k, v)
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        sys.exit(f"missing env vars: {', '.join(missing)} — set them before running.")


def build_payload(sender: str, text: str) -> dict:
    """Meta-shaped webhook payload, signed the way Meta signs it."""
    body = json.dumps({
            "entry": [{
                "changes": [{
                    "value": {
                        "messages": [{
                            "from": sender,
                            "type": "text",
                            "text": {"body": text},
                        }]
                    }
                }]
            }]
        })
    signature = hmac.new(os.environ["META_APP_SECRET"].encode(), body.encode(), hashlib.sha256).hexdigest()
    return {
        "requestContext": {"http": {"method": "POST"}},
        "headers": {"x-hub-signature-256": f"sha256={signature}"},
        "body": body,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", default="in Shoreditch tonight for 2, veggie friendly",
                    help="What to send to the agent")
    ap.add_argument("--sender", default=os.environ.get("CHAT_SENDER", "447000000000"),
                    help="E.164 number (no +) of the WhatsApp sender to simulate. Or set CHAT_SENDER env var.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Skip the final WhatsApp send")
    args = ap.parse_args()

    load_env()

    # Import the webhook handler *after* env is set — it reads env at import time
    sys.path.insert(0, str(Path(__file__).parent.parent / "lambda"))
    import whatsapp_webhook  # noqa: E402

    if args.dry_run:
        # Rewire send_whatsapp on the webhook's own Lambda client so we DON'T
        # actually WhatsApp the phone.
        original_invoke = whatsapp_webhook.lambda_client.invoke

        def spy(**kwargs):
            fn = kwargs.get("FunctionName", "")
            if "whatsapp-send" in fn:
                payload = json.loads(kwargs.get("Payload", b"{}"))
                msg = payload.get("tool_input", {}).get("message", "")
                print(f"\n[DRY RUN] Would send WhatsApp:\n  {msg}\n")
                return {
                    "Payload": type("R", (), {
                        "read": lambda self=None: b'{"tool_output":{"sent":true,"message_id":"DRY"}}'
                    })()
                }
            return original_invoke(**kwargs)
        whatsapp_webhook.lambda_client.invoke = spy  # type: ignore

    payload = build_payload(args.sender, args.text)

    print(f"\n▶ Sender: +{args.sender}")
    print(f"▶ Message: {args.text}")
    if args.dry_run:
        print("▶ Mode: DRY RUN (no WhatsApp send)\n")
    else:
        print("▶ Mode: LIVE (will WhatsApp your phone)\n")

    result = whatsapp_webhook.handler(payload, None)

    print(f"\n▶ Webhook returned: {result}")
    print("\nCheck CloudWatch for the full tool-use trace:")
    print(f"  aws logs tail /aws/lambda/agent-fc-whatsapp-webhook --region {os.environ['AWS_DEFAULT_REGION']} --since 1m")


if __name__ == "__main__":
    main()
