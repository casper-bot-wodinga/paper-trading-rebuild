#!/usr/bin/env python3
"""
Stonks (Data Scientist / Analytics) Agent — structured logging agent entry point.

Usage:
    python3 agents/stonks.py                     # run continuously
    python3 agents/stonks.py --once              # one tick and exit

Logs:
    - logs/agents/stonks.jsonl      — structured JSON-lines log
    - logs/agents/stonks.log        — human-readable log
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.observability import setup_logging, get_logger, metrics, alert

AGENT_ID = "stonks"
LOG_DIR = PROJECT_ROOT / "logs" / "agents"
JSON_LOG = str(LOG_DIR / f"{AGENT_ID}.jsonl")

LOG_DIR.mkdir(parents=True, exist_ok=True)
setup_logging(level="INFO", json_log=JSON_LOG, console=True)
log = get_logger(f"agent.{AGENT_ID}")
log.info("Agent starting", extra={"agent_id": AGENT_ID, "pid": os.getpid()})


def run_tick(tick_number: int) -> None:
    tick_start = time.time()
    try:
        log.info("Tick start", extra={"tick": tick_number, "agent": AGENT_ID})
        metrics.increment(f"agent.{AGENT_ID}.ticks")
        time.sleep(0.1)
        elapsed_ms = (time.time() - tick_start) * 1000
        metrics.observe(f"agent.{AGENT_ID}.tick_ms", elapsed_ms)
        log.info("Tick complete", extra={"tick": tick_number, "elapsed_ms": round(elapsed_ms, 1)})
    except Exception as e:
        elapsed_ms = (time.time() - tick_start) * 1000
        log.error("Tick failed", extra={"tick": tick_number, "error": str(e)})
        metrics.increment(f"agent.{AGENT_ID}.errors", tags={"tick": str(tick_number)})


def main() -> None:
    parser = argparse.ArgumentParser(description=f"{AGENT_ID} trader agent")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    log.info("Agent initialized", extra={"once": args.once, "mock": args.mock})

    tick_number = 0
    while True:
        tick_number += 1
        run_tick(tick_number)
        if args.once:
            break
        time.sleep(5)

    log.info("Agent exiting", extra={"agent_id": AGENT_ID, "ticks": tick_number})


if __name__ == "__main__":
    main()