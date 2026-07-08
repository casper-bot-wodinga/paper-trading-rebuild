#!/usr/bin/env python3
"""Nightly historical replay test — runs all virtual trader variants on Postgres bars.

Loads 5-min OHLCV bars from market_data.bars_5min, converts to Tick objects,
feeds through ReplayHarness with signal-engine-based trader strategies.

Usage:
    python3 .tasks/nightly_replay_test.py
    python3 .tasks/nightly_replay_test.py --tickers SPY,AAPL
    python3 .tasks/nightly_replay_test.py --llm   # also test LLM-based traders
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.replay import (
    ReplayHarness, ReplayResult, Tick, Portfolio,
    TraderDecision, Trade,
)
from src.signals import SignalEngine, SignalParams, SignalReport

log = logging.getLogger("nightly_replay")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Config ────────────────────────────────────────────────────────────────────

PG_DSN = os.environ.get("PG_DSN", "host=docker.klo port=5433 dbname=trading user=trader")
BARS_TABLE = "market_data.bars_5min"
INITIAL_BALANCE = 10_000.0

# ── DB Helpers ────────────────────────────────────────────────────────────────


def load_ticks_from_pg(
    symbols: List[str],
    start_date: str = "2026-07-06",
    end_date: str = "2026-07-08",
) -> List[Tick]:
    """Load 5-min bars from Postgres and convert to Tick objects."""
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()

    placeholders = ",".join(["%s"] * len(symbols))
    cur.execute(
        f"""
        SELECT symbol, timestamp, open, high, low, close, volume
        FROM {BARS_TABLE}
        WHERE symbol IN ({placeholders})
          AND timestamp >= %s::date
          AND timestamp < (%s::date + interval '1 day')
        ORDER BY timestamp ASC, symbol ASC
        """,
        [*symbols, start_date, end_date],
    )

    ticks: List[Tick] = []
    for row in cur.fetchall():
        ticks.append(Tick(
            ticker=row[0],
            timestamp=row[1].replace(tzinfo=timezone.utc) if row[1].tzinfo is None else row[1],
            open=float(row[2]),
            high=float(row[3]),
            low=float(row[4]),
            close=float(row[5]),
            volume=int(row[6]),
        ))
    conn.close()
    log.info("Loaded %d ticks for %d symbols (%s → %s)", len(ticks), len(symbols), start_date, end_date)
    return ticks


# ── Trader Strategies ─────────────────────────────────────────────────────────


def make_signal_trader(params: SignalParams, name: str = "default") -> Callable:
    """Create a trader function that uses SignalEngine with given params."""

    engine = SignalEngine(params)

    def trader_fn(tick: Tick, portfolio: Portfolio) -> TraderDecision:
        report = engine.process(tick)
        price = tick.close

        # BUY: positive composite signal, reasonable conviction, have cash
        if report.composite_signal > 0.15 and report.conviction > 0.2 and portfolio.cash > price * 10:
            allocation = portfolio.total_equity * report.recommended_size_pct
            shares = max(1, int(allocation / price))
            cost = shares * price
            if cost <= portfolio.cash and shares > 0:
                return TraderDecision(
                    ticker=tick.ticker,
                    decision="BUY",
                    conviction=report.conviction,
                    rationale=f"sig={report.composite_signal:.3f} reg={report.regime}",
                    shares=shares,
                )
        # SELL: negative composite signal and we hold it
        elif report.composite_signal < -0.10 and tick.ticker in portfolio.positions:
            pos = portfolio.positions[tick.ticker]
            return TraderDecision(
                ticker=tick.ticker,
                decision="SELL",
                conviction=report.conviction,
                rationale=f"sig={report.composite_signal:.3f}",
                shares=pos.shares,
            )

        return TraderDecision(ticker=tick.ticker, decision="HOLD", conviction=0.0)

    return trader_fn


# ── Virtual Trader Variants ───────────────────────────────────────────────────


VARIANTS: Dict[str, SignalParams] = {
    "baseline":     SignalParams(),                              # defaults
    "looser":       SignalParams(momentum_threshold=0.30, rsi_oversold=25),
    "tighter":      SignalParams(momentum_threshold=0.65, rsi_oversold=35),
    "aggro":        SignalParams(momentum_threshold=0.15, base_size_pct=0.25),
    "patient":      SignalParams(momentum_threshold=0.70, stop_loss_pct=0.03),
    "rsi-wide":     SignalParams(rsi_oversold=20, rsi_overbought=80),
    "rsi-tight":    SignalParams(rsi_oversold=35, rsi_overbought=65),
    "big-bets":     SignalParams(base_size_pct=0.25, max_positions=3),
    "small-bets":   SignalParams(base_size_pct=0.05, max_positions=10),
    "fast-exit":    SignalParams(stop_loss_pct=0.02, momentum_threshold=0.30),
}


# ── Main ──────────────────────────────────────────────────────────────────────


def run_replay(
    ticks: List[Tick],
    variant_name: str,
    params: SignalParams,
) -> ReplayResult:
    """Run one variant through the replay harness."""
    harness = ReplayHarness(initial_balance=INITIAL_BALANCE)
    trader = make_signal_trader(params, variant_name)
    result = harness.run(ticks, trader)
    return result


def print_results(results: Dict[str, ReplayResult]):
    """Print comparison table."""
    print(f"\n{'='*80}")
    print(f"  NIGHTLY HISTORICAL REPLAY — {len(results)} variants on {len(next(iter(results.values())).tickers_seen)} symbols")
    print(f"{'='*80}")
    print(f"{'Variant':<16s} {'Trades':>6s} {'Win %':>7s} {'P&L':>9s} {'Return':>8s} {'Equity':>10s}")
    print(f"{'-'*16} {'-'*6} {'-'*7} {'-'*9} {'-'*8} {'-'*10}")

    for name in sorted(results.keys()):
        r = results[name]
        print(
            f"{name:<16s} {len(r.trades):>6d} {r.win_rate:>6.0%} "
            f"${r.total_pnl:>8.2f} {r.total_return_pct:>7.2f}% ${r.final_equity:>9.2f}"
        )

    # Rank by P&L
    print(f"\n{'─'*80}")
    print("  RANKED BY P&L:")
    ranked = sorted(results.items(), key=lambda x: x[1].total_pnl, reverse=True)
    for i, (name, r) in enumerate(ranked, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i:2d}."
        print(f"  {medal} {name:<16s} ${r.total_pnl:>8.2f} ({r.total_return_pct:>+.2f}%) — {len(r.trades)} trades, {r.win_rate:.0%} win")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default="SPY,AAPL,MSFT,NVDA,TSLA,META,GOOGL,AMZN,QQQ")
    parser.add_argument("--start", default="2026-07-06")
    parser.add_argument("--end", default="2026-07-08")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.tickers.split(",")]
    log.info("Loading bars for %s...", symbols)

    ticks = load_ticks_from_pg(symbols, args.start, args.end)
    if not ticks:
        log.error("NO TICKS LOADED — check Postgres data")
        sys.exit(1)

    log.info("Running %d variants on %d ticks...", len(VARIANTS), len(ticks))

    results: Dict[str, ReplayResult] = {}
    for name, params in VARIANTS.items():
        log.info("  Running %s...", name)
        result = run_replay(ticks, name, params)
        results[name] = result
        log.info("    → %d trades, P&L $%.2f, %.0f%% win", len(result.trades), result.total_pnl, result.win_rate * 100)

    print_results(results)

    # Check for failures: any variant with zero trades?
    zero_trade = [name for name, r in results.items() if len(r.trades) == 0]
    if zero_trade:
        log.warning("⚠️  Variants with ZERO trades: %s", ", ".join(zero_trade))
    else:
        log.info("✅ All variants produced trades")

    # Winner
    winner = max(results.items(), key=lambda x: x[1].total_pnl)
    log.info("🏆 Winner: %s ($%.2f)", winner[0], winner[1].total_pnl)


if __name__ == "__main__":
    main()
