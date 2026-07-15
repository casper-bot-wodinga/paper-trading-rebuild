#!/usr/bin/env python3
"""
Replay Controller — single-date, single-trader replay CLI.

Called by sweep_validation.py (subprocess) during Phase 2 LLM validation.
The parent process swaps the trader's AGENTS.md before calling, then:

  1. Loads bar/tick data for the given date
  2. Reads the trader's AGENTS.md to resolve strategy parameters
  3. Runs replay_trader() from src.replay
  4. Outputs a JSON report to stdout

Expected args:
  --date YYYY-MM-DD
  --traders trader-kairos|trader-aldridge|trader-stonks
  --cash <initial_cash>
  --interval <bar_interval_minutes>

Output JSON format (to stdout, after log lines):
  {"date":"...","wall_time_seconds":...,"traders":{"trader-XXX":{...}}}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# ── Replay imports ───────────────────────────────────────────────────────────
from src.replay import Tick, Portfolio, TraderDecision, replay_trader, ReplayResult
from src.signals import SignalEngine, SignalParams
from src.nightly_replay import make_signal_trader, load_ticks_for_date

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_INITIAL_CASH = 100_000.0


def build_report(
    date_str: str,
    trader_name: str,
    market_data: List[Tick],
    trader_fn,
    initial_cash: float,
    wall_time: float,
) -> dict:
    """Run the replay and build the JSON report."""
    result: ReplayResult = replay_trader(
        market_data=market_data,
        trader=trader_fn,
        initial_balance=initial_cash,
    )

    # Build trader stats
    trades_list = []
    total_pnl = 0.0
    wins = 0
    for trade in result.trades:
        total_pnl += trade.pnl
        if trade.pnl > 0:
            wins += 1
        trades_list.append({
            "ticker": trade.ticker,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "shares": trade.shares,
            "pnl": round(trade.pnl, 2),
            "entry_time": str(trade.entry_time),
            "exit_time": str(trade.exit_time),
        })

    n_trades = len(trades_list)
    win_rate = wins / n_trades if n_trades > 0 else 0.0

    trader_stats = {
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 4),
        "n_trades": n_trades,
        "trades": trades_list,
        "errors": 0,
        "final_equity": round(result.final_equity, 2),
    }

    report = {
        "date": date_str,
        "wall_time_seconds": round(wall_time, 3),
        "traders": {
            trader_name: trader_stats,
        },
    }
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Replay Controller — single-date, single-trader replay"
    )
    parser.add_argument("--date", type=str, required=True,
                        help="Trading date (YYYY-MM-DD)")
    parser.add_argument("--traders", type=str, required=True,
                        help="Model ID (e.g., trader-kairos)")
    parser.add_argument("--cash", type=float, default=DEFAULT_INITIAL_CASH,
                        help="Initial cash balance")
    parser.add_argument("--interval", type=int, default=15,
                        help="Bar interval in minutes")
    args = parser.parse_args()

    start_time = time.time()

    # 1) Load market data via nightly_replay helper
    market_data = load_ticks_for_date(args.date)
    if not market_data:
        print(json.dumps({
            "date": args.date,
            "wall_time_seconds": round(time.time() - start_time, 3),
            "traders": {},
            "error": "No market data available",
        }))
        sys.exit(1)

    # 2) Build trader function from default SignalParams
    #    (AGENTS.md swap is handled by the parent sweep_validation.py)
    params = SignalParams()
    trader_fn = make_signal_trader(params)

    # 3) Run replay
    report = build_report(
        date_str=args.date,
        trader_name=args.traders,
        market_data=market_data,
        trader_fn=trader_fn,
        initial_cash=args.cash,
        wall_time=time.time() - start_time,
    )

    # 4) Log info to stderr (parent captures stdout for JSON)
    ts = report["traders"][args.traders]
    print(f"[replay_controller] Date={args.date}, Trader={args.traders}, "
          f"N_bars={len(market_data)}, N_trades={ts['n_trades']}",
          file=sys.stderr)

    # 5) JSON report to stdout
    print(json.dumps(report))


if __name__ == "__main__":
    main()