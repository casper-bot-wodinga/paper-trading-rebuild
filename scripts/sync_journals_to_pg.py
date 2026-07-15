#!/usr/bin/env python3
"""Sync OpenClaw workspace journal/HEARTBEAT files to Postgres trading.trader_journal.

Runs as a cron job (every 5 min) on .41. Reads journal files from each
trader's OpenClaw workspace and inserts entries into the shared Postgres DB
on docker.klo so the trading dashboard sees fresh data.

Usage:
    python3 scripts/sync_journals_to_pg.py          # dry-run (print only)
    python3 scripts/sync_journals_to_pg.py --apply  # actually insert
    python3 scripts/sync_journals_to_pg.py --apply --no-heartbeat  # skip heartbeat upsert
"""

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

TRADERS = ["kairos", "aldridge", "stonks"]
AGENT_IDS = {t: f"trader-{t}" for t in TRADERS}

# Postgres — docker.klo:5433
PG_DSN = os.getenv(
    "PG_DSN",
    "host=trading-db port=5432 dbname=trading user=trader",
)

# OpenClaw workspace root on .41
WORKSPACE_ROOT = Path(os.getenv("OPENCLAW_HOME", "/home/openclaw")) / ".openclaw"


def parse_journal_file(path: Path) -> list[dict]:
    """Parse a single journal markdown file into entries.

    Returns list of {timestamp, mood, entry, confidence} dicts.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    entries = []
    current_section = ""
    current_lines = []

    # Grab the date from the filename
    date_str = path.stem  # e.g. "2026-07-14"

    for line in lines:
        # Section headers like "## 11:20 Tick — HOLD" or "## Trades"
        if line.startswith("## "):
            if current_lines:
                entry_text = "\n".join(current_lines).strip()
                if entry_text:
                    # Extract time from header if present
                    ts = _extract_timestamp(current_section, date_str)
                    entries.append({
                        "timestamp": ts,
                        "mood": _extract_mood(current_section),
                        "entry": entry_text[:2000],
                        "confidence": _extract_confidence(entry_text),
                    })
            current_section = line.strip("# ").strip()
            current_lines = []
        elif line.strip():
            current_lines.append(line.strip())

    # Flush last section
    if current_lines:
        entry_text = "\n".join(current_lines).strip()
        if entry_text:
            ts = _extract_timestamp(current_section, date_str)
            entries.append({
                "timestamp": ts,
                "mood": _extract_mood(current_section),
                "entry": entry_text[:2000],
                "confidence": _extract_confidence(entry_text),
            })

    return entries


def _extract_timestamp(section: str, date_str: str) -> str:
    """Extract ISO timestamp from a section header like '11:20 Tick — HOLD'."""
    # Try HH:MM format
    m = re.search(r"(\d{1,2}):(\d{2})", section)
    if m:
        return f"{date_str}T{m.group(1).zfill(2)}:{m.group(2)}:00"
    # Fallback: use noon on that date
    return f"{date_str}T12:00:00"


def _extract_mood(section: str) -> str:
    """Extract mood keyword from section."""
    moods = ["bullish", "bearish", "neutral", "cautious", "confident", "optimistic",
             "pessimistic", "anxious", "excited", "patient", "aggressive", "defensive"]
    for mood in moods:
        if mood in section.lower():
            return mood
    return ""


def _extract_confidence(text: str) -> float | None:
    """Extract confidence value from text like 'Conviction: 0.65' or 'Confidence: 72/100'."""
    m = re.search(r"(?:conviction|confidence)[:\s]*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        if val > 1:
            val = val / 100  # e.g. 72/100 → 0.72
        return round(val, 2)
    return None


def read_heartbeat(workspace: Path, trader: str) -> dict | None:
    """Read HEARTBEAT_OK file and return {timestamp, entry} or None."""
    hb_file = workspace / "HEARTBEAT_OK"
    if not hb_file.exists():
        return None
    text = hb_file.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None

    # Format: "OK 2026-07-14T12:21 ET | BUY 2 SOFI @ 8.50 | ..."
    ts_match = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text)
    timestamp = ts_match.group(0) if ts_match else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    return {
        "timestamp": timestamp,
        "entry": text[:2000],
        "mood": "",
        "confidence": None,
    }


def get_latest_db_ts(conn, agent_id: str) -> str | None:
    """Get the most recent timestamp in the DB for this agent."""
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT MAX(timestamp) FROM trading.trader_journal WHERE agent_id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
        cur.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def insert_entries(conn, agent_id: str, entries: list[dict], latest_db_ts: str | None):
    """Insert new entries into trading.trader_journal, skipping duplicates."""
    if not entries:
        return 0

    cur = conn.cursor()
    inserted = 0
    for entry in entries:
        ts = entry["timestamp"]

        # Skip if we already have this timestamp (or earlier) in the DB
        if latest_db_ts and ts <= latest_db_ts:
            continue

        try:
            cur.execute(
                """INSERT INTO trading.trader_journal
                   (agent_id, timestamp, mood, entry, confidence, source)
                   VALUES (%s, %s, %s, %s, %s, 'openclaw_sync')""",
                (agent_id, ts, entry["mood"], entry["entry"], entry["confidence"]),
            )
            inserted += 1
        except Exception as e:
            print(f"  [WARN] insert failed for {agent_id} @ {ts}: {e}", file=sys.stderr)
    conn.commit()
    cur.close()
    return inserted


def upsert_heartbeat(conn, agent_id: str, hb: dict):
    """Insert or update a heartbeat marker so _get_last_activity returns a fresh timestamp."""
    if not hb:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO trading.trader_journal
               (agent_id, timestamp, mood, entry, confidence, source)
               VALUES (%s, %s, %s, %s, %s, 'heartbeat')
               ON CONFLICT DO NOTHING""",
            (agent_id, hb["timestamp"], hb["mood"], hb["entry"], hb["confidence"]),
        )
        conn.commit()
    except Exception as e:
        print(f"  [WARN] heartbeat upsert failed for {agent_id}: {e}", file=sys.stderr)
    finally:
        cur.close()


def main():
    parser = argparse.ArgumentParser(description="Sync OpenClaw journal entries to Postgres")
    parser.add_argument("--apply", action="store_true", help="Actually insert (default: dry-run)")
    parser.add_argument("--no-heartbeat", action="store_true", help="Skip heartbeat upsert")
    args = parser.parse_args()

    try:
        import psycopg2
    except ImportError:
        print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary", file=sys.stderr)
        sys.exit(1)

    if not WORKSPACE_ROOT.exists():
        print(f"ERROR: Workspace root not found: {WORKSPACE_ROOT}", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True

    total_inserted = 0
    for trader in TRADERS:
        agent_id = AGENT_IDS[trader]
        workspace = WORKSPACE_ROOT / f"workspace-trader-{trader}"
        journal_dir = workspace / "journal"

        if not workspace.exists():
            print(f"[SKIP] {trader} — workspace not found: {workspace}")
            continue

        print(f"\n── {trader} ({agent_id}) ──")

        # 1. Sync journal entries
        latest_db_ts = get_latest_db_ts(conn, agent_id)
        print(f"  Latest DB timestamp: {latest_db_ts or '(none)'}")

        if journal_dir.exists():
            journal_files = sorted(journal_dir.glob("*.md"))
            print(f"  Journal files: {len(journal_files)}")

            if args.apply:
                # Only process files newer than the latest DB entry
                for jf in journal_files:
                    entries = parse_journal_file(jf)
                    n = insert_entries(conn, agent_id, entries, latest_db_ts)
                    if n > 0:
                        print(f"  Inserted {n} entries from {jf.name}")
                        total_inserted += n
            else:
                # Dry-run: show what would be inserted
                for jf in journal_files:
                    entries = parse_journal_file(jf)
                    new_entries = [e for e in entries if not latest_db_ts or e["timestamp"] > latest_db_ts]
                    if new_entries:
                        print(f"  Would insert {len(new_entries)} entries from {jf.name} (newest: {new_entries[-1]['timestamp']})")
        else:
            print(f"  No journal dir: {journal_dir}")

        # 2. Sync heartbeat
        if not args.no_heartbeat:
            hb = read_heartbeat(workspace, trader)
            if hb:
                if args.apply:
                    upsert_heartbeat(conn, agent_id, hb)
                    print(f"  Heartbeat synced: {hb['timestamp']}")
                else:
                    print(f"  Would sync heartbeat: {hb['timestamp']} | {hb['entry'][:80]}...")

    conn.close()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n=== {mode}: {total_inserted} entries inserted total ===")

    if not args.apply:
        print("\nRun with --apply to actually insert.")


if __name__ == "__main__":
    main()