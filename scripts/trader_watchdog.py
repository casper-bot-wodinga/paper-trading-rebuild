#!/usr/bin/env python3
"""Trader activity watchdog — bridges .41 cron timestamps to the tick flasher.

Runs every 60s. Queries the OpenClaw gateway DB on .41 for cron last-run
timestamps, then updates the local heartbeat-state.json so the dashboard
shows live activity.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

TRADERS = ["kairos", "aldridge", "stonks"]
STATE_PATH = os.path.expanduser(
    "~/paper-trading-rebuild-v3/state/heartbeat-state.json"
)


def get_cron_timestamps() -> dict:
    """SSH to .41 and query the gateway DB for cron last_run timestamps."""
    script = (
        "sudo sqlite3 /home/openclaw/.openclaw/state/openclaw.sqlite "
        '"SELECT agent_id, last_run_at_ms FROM cron_jobs '
        "WHERE agent_id LIKE 'trader-%' AND enabled=1\""
    )
    cmd = [
        "ssh", "-o", "ConnectTimeout=10",
        "raf@192.168.1.41",
        script,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        print("[watchdog] SSH timeout", file=sys.stderr)
        return {}

    if result.returncode != 0:
        print(f"[watchdog] SSH error: {result.stderr.strip()}", file=sys.stderr)
        return {}

    timestamps = {}
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|", 1)
        agent_id = parts[0].replace("trader-", "")
        try:
            last_run_ms = int(parts[1])
            if last_run_ms:
                timestamps[agent_id] = last_run_ms / 1000.0
        except ValueError:
            continue
    return timestamps


def get_heartbeat_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def update_tick_flasher(trader: str, timestamp: float):
    ts_iso = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(
        timespec="seconds"
    )
    hb = get_heartbeat_state()
    hb[f"last_{trader}"] = ts_iso
    hb[f"ts_{trader}"] = ts_iso
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(hb, f, indent=2)


def main():
    timestamps = get_cron_timestamps()
    if not timestamps:
        print("[watchdog] No cron timestamps from .41")
        return

    hb = get_heartbeat_state()
    now = time.time()
    updated = 0

    for trader in TRADERS:
        ts = timestamps.get(trader)
        if not ts:
            continue

        current_key = f"ts_{trader}"
        current_ts_str = hb.get(current_key, "")
        current_ts = 0
        if current_ts_str:
            try:
                dt = datetime.fromisoformat(current_ts_str)
                current_ts = dt.timestamp()
            except (ValueError, TypeError):
                pass

        if ts > current_ts:
            update_tick_flasher(trader, ts)
            age_s = int(now - ts)
            print(f"[watchdog] {trader}: run {age_s}s ago -> flasher updated")
            updated += 1

    if updated == 0:
        print(f"[watchdog] No new timestamps (all {len(TRADERS)} current)")


if __name__ == "__main__":
    main()