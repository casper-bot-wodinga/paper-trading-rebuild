#!/usr/bin/env python3
"""
tick_cron.py — Market hours tick orchestrator for paper trading agents.

This script runs every 5 minutes during market hours (9:30-16:00 ET, Mon-Fri)
and dispatches an isolated agentTurn to each trader agent with the message
"Market tick — run your HEARTBEAT".

Modes:
  --setup       Install OpenClaw cron jobs for 5-min tick schedule
  --remove      Remove installed tick cron jobs
  --status      Show tick cron job status
  --tick        Run one tick cycle (check market, dispatch traders)
  --force       Like --tick but skip market-hours check (for testing)
  --no-cli      Skip openclaw CLI dispatch; use SQLite directly

Without flags: runs one tick cycle (market-hours gated).

Installation:
  python3 scripts/tick_cron.py --setup
"""

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, date, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
LOGS_DIR = PROJECT_DIR / "logs"

TRADERS = [
    {"id": "trader-kairos",    "name": "Kairos",   "workspace": "/home/openclaw/.openclaw/workspace-trader-kairos"},
    {"id": "trader-aldridge",  "name": "Aldridge", "workspace": "/home/openclaw/.openclaw/workspace-trader-aldridge"},
    {"id": "trader-stonks",    "name": "Stonks",   "workspace": "/home/openclaw/.openclaw/workspace-trader-stonks"},
]

# OpenClaw gateway info
GATEWAY_PORT = int(os.environ.get("OPENCLAW_GATEWAY_PORT", "18789"))
GATEWAY_URL = f"http://127.0.0.1:{GATEWAY_PORT}"

# SQLite state database (OpenClaw internal)
OPENCLAW_STATE_DIR = Path(os.environ.get("OPENCLAW_STATE_DIR", "/home/openclaw/.openclaw"))
CRON_DB_PATH = OPENCLAW_STATE_DIR / "state" / "openclaw.sqlite"

# Cron job naming
CRON_TAG = "paper-trading-tick"

# Logging
LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "tick_cron.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("tick_cron")


# ── Market Hours ──────────────────────────────────────────────────────────

def is_market_open(dt: Optional[datetime] = None) -> bool:
    """Check if US equity market is open (9:30-16:00 ET, Mon-Fri)."""
    if dt is None:
        dt = datetime.now()

    # Convert to ET (America/New_York)
    try:
        import zoneinfo
        et_tz = zoneinfo.ZoneInfo("America/New_York")
    except ImportError:
        try:
            import pytz
            et_tz = pytz.timezone("America/New_York")
        except ImportError:
            log.warning("No timezone library; using UTC fallback for market check")
            et_tz = None

    if et_tz:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=et_tz)
        else:
            dt = dt.astimezone(et_tz)
    else:
        log.warning("Cannot determine ET; market check may be incorrect")

    # Weekday check (0=Mon, 6=Sun)
    if dt.weekday() >= 5:
        return False

    # Time check (9:30-16:00 ET)
    market_open = dt_time(9, 30)
    market_close = dt_time(16, 0)
    current_time = dt.time()

    return market_open <= current_time < market_close


def next_market_close(dt: Optional[datetime] = None) -> Optional[datetime]:
    """Return the next 16:00 ET market close datetime or None if already past."""
    if dt is None:
        dt = datetime.now()

    try:
        import zoneinfo
        et_tz = zoneinfo.ZoneInfo("America/New_York")
    except ImportError:
        try:
            import pytz
            et_tz = pytz.timezone("America/New_York")
        except ImportError:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=et_tz)
    else:
        dt = dt.astimezone(et_tz)

    close_time = dt_time(16, 0)
    candidate = dt.replace(hour=16, minute=0, second=0, microsecond=0)

    if dt.time() >= close_time:
        candidate += timedelta(days=1)
        # Skip weekends
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)

    return candidate


# ── Dispatcher ────────────────────────────────────────────────────────────

def dispatch_trader_tick(trader_id: str, trader_name: str, workspace: str) -> dict:
    """
    Dispatch an isolated agentTurn to a trader agent.

    Uses OpenClaw's gateway via SQLite (primary) to create a one-shot
    cron job that runs an isolated agent turn with the tick message.

    Returns dict with status and result info.
    """
    log.info("Dispatching tick to %s (%s)", trader_name, trader_id)
    job_name = f"{CRON_TAG}-{trader_name}-{uuid.uuid4().hex[:8]}"

    # Primary: SQLite dispatch (most reliable for direct gateway integration)
    no_cli = "--no-cli" in set(sys.argv)
    if not no_cli:
        try:
            cli_paths = ["/home/openclaw/.npm-global/bin/openclaw", "/usr/local/bin/openclaw", "/usr/bin/openclaw"]
            cli = None
            for p in cli_paths:
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    cli = p
                    break
            if cli:
                result = _dispatch_via_cli(cli, job_name, trader_id, trader_name)
                if result["status"] == "ok":
                    return result
                log.warning("CLI dispatch failed: %s", result.get("error"))
        except Exception as e:
            log.warning("CLI dispatch error: %s", e)

    # Fallback / primary: Direct SQLite insert
    return _dispatch_via_sqlite(job_name, trader_id, trader_name)


def _dispatch_via_cli(cli: str, job_name: str, trader_id: str, trader_name: str) -> dict:
    """Dispatch via openclaw CLI."""
    cmd = [
        cli, "cron", "create", "now",
        "--name", job_name,
        "--session", "isolated",
        "--agent", trader_id,
        "--message", "Market tick — run your HEARTBEAT",
        "--delete-after-run",
    ]

    log.debug("Running: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=15,
        env={
            **os.environ,
            "OPENCLAW_GATEWAY_PORT": str(GATEWAY_PORT),
            "OPENCLAW_STATE_DIR": str(OPENCLAW_STATE_DIR),
            "OPENCLAW_CONFIG_PATH": str(OPENCLAW_STATE_DIR / "openclaw.json"),
        },
    )

    if proc.returncode == 0:
        log.info("✓ Dispatched %s tick (job: %s)", trader_name, job_name)
        return {"status": "ok", "trader": trader_name, "job": job_name, "cli_output": proc.stdout.strip()}
    else:
        error = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
        return {"status": "error", "trader": trader_name, "error": error}


def _dispatch_via_sqlite(job_name: str, trader_id: str, trader_name: str) -> dict:
    """Fallback: create a one-shot cron job via direct SQLite insert."""
    if not CRON_DB_PATH.exists():
        return {"status": "error", "trader": trader_name, "error": f"SQLite DB not found at {CRON_DB_PATH}"}

    store_key = "main"
    job_id = job_name
    now_ms = int(time.time() * 1000)
    run_at_ms = now_ms + 2000  # fire in 2 seconds

    job_json = json.dumps({
        "name": job_name,
        "agentId": trader_id,
        "sessionTarget": "isolated",
        "scheduleKind": "at",
        "at": datetime.fromtimestamp(run_at_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "payloadKind": "agentTurn",
        "payloadMessage": "Market tick — run your HEARTBEAT",
        "deleteAfterRun": True,
        "enabled": True,
    })

    try:
        conn = sqlite3.connect(str(CRON_DB_PATH))
        cur = conn.cursor()
        cur.execute(
            """INSERT OR REPLACE INTO cron_jobs (
                store_key, job_id, name, display_name, enabled, delete_after_run,
                created_at_ms, agent_id, session_target, schedule_kind, at,
                payload_kind, payload_message, next_run_at_ms,
                wake_mode, job_json, updated_at
            ) VALUES (?, ?, ?, ?, 1, 1, ?, ?, 'isolated', 'at', ?, 'agentTurn',
                      'Market tick — run your HEARTBEAT', ?, 'now', ?, ?)""",
            (
                store_key, job_id, job_name, job_name,
                now_ms, trader_id,
                datetime.fromtimestamp(run_at_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                run_at_ms, job_json, now_ms,
            ),
        )
        conn.commit()
        conn.close()
        log.info("✓ Dispatched %s tick via SQLite (job: %s)", trader_name, job_name)
        return {"status": "ok", "trader": trader_name, "job": job_name, "method": "sqlite"}
    except Exception as e:
        return {"status": "error", "trader": trader_name, "error": str(e)}


# ── Setup / Remove Cron Jobs ──────────────────────────────────────────────

CRON_CRON_EXPR = "*/5 9-15 * * 1-5"  # every 5 min Mon-Fri 9AM-3:59PM (script guards 9:30 start)


def setup_cron_jobs() -> list[dict]:
    """Install 5-min tick cron jobs for all traders using OpenClaw cron system."""
    results = []

    for trader in TRADERS:
        trader_id = trader["id"]
        trader_name = trader["name"]
        job_name = f"{CRON_TAG}-{trader_name}"

        try:
            # Use openclaw CLI to create the recurring cron job
            cli_paths = [
                "/home/openclaw/.npm-global/bin/openclaw",
                "/usr/local/bin/openclaw",
                "/usr/bin/openclaw",
            ]
            cli = None
            for p in cli_paths:
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    cli = p
                    break

            if cli:
                cmd = [
                    cli, "cron", "create", CRON_CRON_EXPR,
                    "--name", job_name,
                    "--session", "isolated",
                    "--agent", trader_id,
                    "--message", "Market tick — run your HEARTBEAT",
                    "--tz", "America/New_York",
                ]

                log.info("Creating cron job for %s...", trader_name)
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=45,
                    env={
                        **os.environ,
                        "OPENCLAW_GATEWAY_PORT": str(GATEWAY_PORT),
                        "OPENCLAW_STATE_DIR": str(OPENCLAW_STATE_DIR),
                    },
                )

                if proc.returncode == 0:
                    log.info("✓ Cron job created for %s", trader_name)
                    results.append({"trader": trader_name, "status": "ok", "output": proc.stdout.strip()})
                else:
                    error = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
                    log.warning("CLI cron create failed for %s: %s", trader_name, error)
                    # Fall back to SQLite
                    r = _setup_via_sqlite(trader, job_name)
                    results.append(r)
            else:
                log.warning("openclaw CLI not found, using SQLite setup for %s", trader_name)
                r = _setup_via_sqlite(trader, job_name)
                results.append(r)

        except Exception as e:
            log.error("Failed to setup cron for %s: %s", trader_name, e)
            results.append({"trader": trader_name, "status": "error", "error": str(e)})

    return results


def _setup_via_sqlite(trader: dict, job_name: str) -> dict:
    """Fallback: create recurring cron job via SQLite insert."""
    if not CRON_DB_PATH.exists():
        return {"trader": trader["name"], "status": "error",
                "error": f"SQLite DB not found at {CRON_DB_PATH}"}

    store_key = "main"
    job_id = f"{job_name}-sqlite"
    now_ms = int(time.time() * 1000)

    job_json = json.dumps({
        "name": job_name,
        "agentId": trader["id"],
        "sessionTarget": "isolated",
        "scheduleKind": "cron",
        "scheduleExpr": CRON_CRON_EXPR,
        "scheduleTz": "America/New_York",
        "payloadKind": "agentTurn",
        "payloadMessage": "Market tick — run your HEARTBEAT",
        "enabled": True,
    })

    try:
        conn = sqlite3.connect(str(CRON_DB_PATH))
        cur = conn.cursor()

        # Remove any existing job with this name
        cur.execute(
            "DELETE FROM cron_jobs WHERE store_key = ? AND name = ?",
            (store_key, job_name),
        )

        cur.execute(
            """INSERT INTO cron_jobs (
                store_key, job_id, name, display_name, enabled, delete_after_run,
                created_at_ms, agent_id, session_target, schedule_kind,
                schedule_expr, schedule_tz,
                payload_kind, payload_message, wake_mode, job_json, updated_at
            ) VALUES (?, ?, ?, ?, 1, 0, ?, ?, 'isolated', 'cron',
                      ?, 'America/New_York',
                      'agentTurn', 'Market tick — run your HEARTBEAT', 'now', ?, ?)""",
            (
                store_key, job_id, job_name, job_name,
                now_ms, trader["id"],
                CRON_CRON_EXPR,
                job_json, now_ms,
            ),
        )
        conn.commit()
        conn.close()
        log.info("✓ Cron job created for %s via SQLite", trader["name"])
        return {"trader": trader["name"], "status": "ok", "method": "sqlite"}
    except Exception as e:
        return {"trader": trader["name"], "status": "error", "error": str(e)}


def remove_cron_jobs() -> list[dict]:
    """Remove all tick cron jobs for paper trading agents."""
    results = []

    # Method 1: CLI
    cli_paths = [
        "/home/openclaw/.npm-global/bin/openclaw",
        "/usr/local/bin/openclaw",
        "/usr/bin/openclaw",
    ]
    cli = None
    for p in cli_paths:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            cli = p
            break

    if cli:
        for trader in TRADERS:
            try:
                cmd = [cli, "cron", "list", "--json"]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                      env={**os.environ, "OPENCLAW_GATEWAY_PORT": str(GATEWAY_PORT)})
                if proc.returncode == 0 and proc.stdout.strip():
                    jobs = json.loads(proc.stdout)
                    for job in jobs:
                        job_name = job.get("name", "")
                        if CRON_TAG in job_name:
                            job_id = job.get("id", "")
                            remove_cmd = [cli, "cron", "remove", job_id]
                            subprocess.run(remove_cmd, capture_output=True, timeout=15,
                                           env={**os.environ, "OPENCLAW_GATEWAY_PORT": str(GATEWAY_PORT)})
                            log.info("Removed cron job %s", job_name)
                            results.append({"trader": trader["name"], "job": job_name, "status": "removed"})
            except Exception as e:
                log.warning("CLI remove failed for %s: %s", trader["name"], e)

    # Method 2: SQLite fallback
    if CRON_DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(CRON_DB_PATH))
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM cron_jobs WHERE store_key = 'main' AND name LIKE ?",
                (f"%{CRON_TAG}%",),
            )
            deleted = cur.rowcount
            conn.commit()
            conn.close()
            if deleted > 0:
                log.info("Removed %d cron jobs via SQLite", deleted)
                results.append({"method": "sqlite", "removed": deleted})
        except Exception as e:
            log.error("SQLite remove failed: %s", e)

    return results


def status_cron_jobs() -> list[dict]:
    """Show tick cron job status from SQLite."""
    results = []
    if not CRON_DB_PATH.exists():
        log.warning("SQLite DB not found at %s", CRON_DB_PATH)
        return results

    try:
        conn = sqlite3.connect(str(CRON_DB_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """SELECT name, agent_id, enabled, schedule_kind, schedule_expr,
                      next_run_at_ms, last_run_at_ms, last_run_status,
                      consecutive_errors, consecutive_skipped
               FROM cron_jobs
               WHERE store_key = 'main' AND name LIKE ?
               ORDER BY name""",
            (f"%{CRON_TAG}%",),
        )
        for row in cur.fetchall():
            results.append(dict(row))
        conn.close()
    except Exception as e:
        log.error("Status query failed: %s", e)

    return results


# ── Tick Runner ───────────────────────────────────────────────────────────

def run_tick(force: bool = False) -> dict:
    """Run one tick cycle: check market, dispatch all traders."""
    now = datetime.now()
    results = {
        "timestamp": now.isoformat(),
        "market_open": is_market_open(now),
        "traders_dispatched": [],
        "traders_skipped": [],
    }

    if not force and not results["market_open"]:
        log.info("Market is closed. Skipping tick dispatch.")
        return results

    log.info("=== Market Tick %s ===", now.strftime("%H:%M"))

    for trader in TRADERS:
        result = dispatch_trader_tick(trader["id"], trader["name"], trader["workspace"])
        if result.get("status") == "ok":
            results["traders_dispatched"].append(trader["name"])
        else:
            results["traders_skipped"].append({
                "name": trader["name"],
                "error": result.get("error", "unknown"),
            })

    log.info(
        "Tick complete: %d dispatched, %d skipped",
        len(results["traders_dispatched"]),
        len(results["traders_skipped"]),
    )
    return results


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    args = set(sys.argv[1:])

    if "--setup" in args:
        log.info("Installing tick cron jobs for %d traders...", len(TRADERS))
        results = setup_cron_jobs()
        print("\n── Setup Results ──")
        for r in results:
            status = "✓" if r.get("status") == "ok" else "✗"
            method = r.get("method", "cli")
            print(f"  {status} {r['trader']:10s} ({method})")
        print("──────────────────")
        return 0

    if "--remove" in args:
        log.info("Removing tick cron jobs...")
        results = remove_cron_jobs()
        print("\n── Removal Results ──")
        for r in results:
            print(f"  {r}")
        print("─────────────────────")
        return 0

    if "--status" in args:
        jobs = status_cron_jobs()
        print("\n── Tick Cron Jobs ──")
        if jobs:
            for j in jobs:
                status = "✓" if j.get("enabled") else "✗"
                agent = j.get("agent_id", "?")
                last = j.get("last_run_status", "never")
                next_run = j.get("next_run_at_ms")
                if next_run:
                    next_dt = datetime.fromtimestamp(next_run / 1000)
                    next_str = next_dt.strftime("%H:%M")
                else:
                    next_str = "?"
                print(f"  {status} {j['name']:30s} agent={agent:20s} last={last:8s} next={next_str}")
        else:
            print("  No tick cron jobs found.")
        print("────────────────────")
        return 0

    # Default: run one tick
    force = "--force" in args
    results = run_tick(force=force)

    if args & {"--json", "-j"}:
        print(json.dumps(results, indent=2))
    else:
        if results["market_open"] or force:
            if results["traders_dispatched"]:
                print(f"Tick OK — dispatched: {', '.join(results['traders_dispatched'])}")
            if results["traders_skipped"]:
                print(f"Tick partial — skipped: {results['traders_skipped']}")
        else:
            print("Market closed — no tick dispatched.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
