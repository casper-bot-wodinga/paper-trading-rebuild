#!/usr/bin/env python3
"""Send a message to Hermes via the chat bridge.

Usage: echo "message" | python3 scripts/bridge_send.py [--topic topic] [--priority normal|high]
   or: python3 scripts/bridge_send.py "message" [--topic topic]
"""
import json, os, sys, urllib.request

TOKEN_FILE = os.path.expanduser("~/projects/hermes-openclaw-bridge/.casper_chat_token")
BRIDGE_URL = "http://localhost:8644/send"


def main():
    topic = "status"
    priority = "normal"

    # Collect message from arg or stdin
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    msg = " ".join(args) if args else sys.stdin.read().strip()

    # Parse flags
    for a in sys.argv[1:]:
        if a == "--topic" and sys.argv.index("--topic") + 1 < len(sys.argv):
            topic = sys.argv[sys.argv.index("--topic") + 1]
        if a == "--priority":
            priority = sys.argv[sys.argv.index("--priority") + 1]

    if not msg:
        print("No message provided", file=sys.stderr)
        sys.exit(1)

    token = open(TOKEN_FILE).read().strip()
    payload = json.dumps({
        "to": "Hermes",
        "from": "casper",
        "message": msg,
        "topic": topic,
        "priority": priority,
    }).encode()

    req = urllib.request.Request(
        BRIDGE_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
