#!/usr/bin/env python3
"""
Kairos (Momentum) Trader Agent — structured logging agent entry point.

Usage:
    python3 agents/kairos.py                    # run continuously
    python3 agents/kairos.py --once             # one tick and exit
    python3 agents/kairos.py --mock             # skip network calls

Logs:
    - logs/agents/kairos.jsonl     — structured JSON-lines log
    - logs/agents/kairos.log       — human-readable log
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.observability import setup_logging, get_logger, metrics, alert

# ── Agent identity ──────────────────────────────────────────────────────────
AGENT_ID = "kairos"
LOG_DIR = PROJECT_ROOT / "logs" / "agents"
JSON_LOG = str(LOG_DIR / f"{AGENT_ID}.jsonl")

# ── Setup structured logging ───────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
setup_logging(level="INFO", json_log=JSON_LOG, console=True)
log = get_logger(f"agent.{AGENT_ID}")
log.info("Agent starting", extra={"agent_id": AGENT_ID, "pid": os.getpid()})


def run_tick(tick_number: int) -> None:
    """Execute one tick cycle for this agent."""
    tick_start = time.time()
    try:
        # --- Agent work here ---
        log.info("Tick start", extra={"tick": tick_number, "agent": AGENT_ID})
        metrics.increment(f"agent.{AGENT_ID}.ticks")

        # Simulate work (real logic loads from virtual_runner etc.)
        time.sleep(0.1)

        elapsed_ms = (time.time() - tick_start) * 1000
        metrics.observe(f"agent.{AGENT_ID}.tick_ms", elapsed_ms)
        log.info("Tick complete", extra={"tick": tick_number, "elapsed_ms": round(elapsed_ms, 1)})

    except Exception as e:
        elapsed_ms = (time.time() - tick_start) * 1000
        log.error("Tick failed", extra={"tick": tick_number, "error": str(e), "elapsed_ms": round(elapsed_ms, 1)})
        metrics.increment(f"agent.{AGENT_ID}.errors", tags={"tick": str(tick_number)})


def main() -> None:
    parser = argparse.ArgumentParser(description=f"{AGENT_ID} trader agent")
    parser.add_argument("--once", action="store_true", help="Run one tick and exit")
    parser.add_argument("--mock", action="store_true", help="Run in mock mode (no network)")
    args = parser.parse_args()

    log.info("Agent initialized", extra={"once": args.once, "mock": args.mock})

    tick_number = 0
    while True:
        tick_number += 1
        run_tick(tick_number)
        if args.once:
            break
        time.sleep(5)  # 5 seconds between ticks in dev; 5 min in prod

    log.info("Agent exiting", extra={"agent_id": AGENT_ID, "ticks": tick_number})


if __name__ == "__main__":
    main()