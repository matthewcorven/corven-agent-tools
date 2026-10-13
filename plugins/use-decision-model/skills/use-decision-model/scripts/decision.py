#!/usr/bin/env python3
"""CLI wrapper for a decision model served on the OpenRouter decisions endpoint.

A decision model is not a chat model: it takes a ``state`` plus typed ``questions``
(``noul`` / ``choice`` / ``score``) and returns typed numerical ``answers``. This
helper POSTs a payload and prints the raw JSON response.

Usage:
    python3 decision.py --file payload.json
    echo '{"state": "...", "questions": {...}}' | python3 decision.py --stdin

Reads OPENROUTER_API_KEY from the environment.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "microsoft/microsoft-decision-1"


def main() -> None:
    ap = argparse.ArgumentParser(description="Send a payload to a decision model.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="Path to a JSON payload with 'state' and 'questions'")
    src.add_argument("--stdin", action="store_true", help="Read the JSON payload from stdin")
    ap.add_argument("--model", default=MODEL, help=f"Decision model id (default: {MODEL})")
    ap.add_argument("--endpoint", default=ENDPOINT, help=f"Decisions endpoint (default: {ENDPOINT})")
    args = ap.parse_args()

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("OPENROUTER_API_KEY not set")

    if args.stdin:
        payload = json.load(sys.stdin)
    else:
        with open(args.file) as f:
            payload = json.load(f)
    payload["model"] = args.model

    req = urllib.request.Request(
        args.endpoint,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print(r.read().decode())
    except urllib.error.HTTPError as e:
        print(e.read().decode(), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
