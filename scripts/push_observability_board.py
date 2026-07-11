#!/usr/bin/env python3
"""
Push Observability Board — periodic health dashboard push to Canvas.

This script is designed to run as a cron job (every 5-30 min) to keep
the observability board up to date with real-time system health data.

Usage:
    python3 scripts/push_observability_board.py
    python3 scripts/push_observability_board.py --every 300        # every 5 min
    python3 scripts/push_observability_board.py --board trading     # board name
    python3 scripts/push_observability_board.py --once --board main  # one-shot

Cron:
    */5 * * * 1-5 cd ~/projects/paper-trading-rebuild && \
        python3 scripts/push_observability_board.py --every 300 \
        >> logs/push_observability_board.log 2>&1
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.canvas_dashboard import push_health_dashboard
from src.observability import setup_logging, get_logger, metrics


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Push observability health dashboard to Canvas"
    )
    parser.add_argument(
        "--every",
        type=int,
        default=0,
        help="Run in loop mode, pushing every N seconds (0 = one-shot)",
    )
    parser.add_argument(
        "--board",
        default="trading",
        help="Canvas board name (default: trading)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Push once and exit",
    )
    parser.add_argument(
        "--junit",
        default=None,
        help="Path to test results JUnit XML",
    )
    parser.add_argument(
        "--card-id",
        default=None,
        help="Update existing card (for card permanence)",
    )
    parser.add_argument(
        "--expires",
        type=int,
        default=1,
        help="Days until card expires (default: 1)",
    )
    args = parser.parse_args()

    # Setup logging
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(
        level="INFO",
        json_log=str(log_dir / "observability_board.jsonl"),
        console=True,
    )
    log = get_logger("observability_board")

    card_id = args.card_id
    interval = args.every
    if args.once:
        interval = 0

    log.info("Observability board pusher starting", extra={
        "board": args.board,
        "interval": interval,
        "card_id": card_id,
    })

    while True:
        log.info("Pushing health dashboard...", extra={"board": args.board})
        result = push_health_dashboard(
            board=args.board,
            card_id=card_id,
            expires_days=args.expires,
            junit_path=args.junit,
        )
        if result:
            card_id = result  # Update same card so board stays stable
            log.info("Dashboard pushed OK", extra={"card_id": card_id})
            metrics.increment("observability_board.pushes")
        else:
            log.warning("Dashboard push failed", extra={"board": args.board})
            metrics.increment("observability_board.push_failures")

        if interval <= 0:
            break
        log.debug("Sleeping %ds before next push...", interval)
        time.sleep(interval)

    log.info("Observability board pusher exiting")
    metrics.increment("observability_board.runs")


if __name__ == "__main__":
    main()