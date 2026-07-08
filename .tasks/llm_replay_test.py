#!/usr/bin/env python3
"""LLM-based virtual trader test — runs actual OpenRouter calls on historical data.

Usage:
    python3 .tasks/llm_replay_test.py --ticker SPY --date 2026-07-07 --variants 3
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import psycopg2
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.replay import ReplayHarness, ReplayResult, Tick, Portfolio, TraderDecision
from src.signals import SignalEngine, SignalParams, SignalReport

log = logging.getLogger("llm_replay")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PG_DSN = os.environ.get("PG_DSN", "host=docker.klo port=5433 dbname=trading user=trader")
BARS_TABLE = "market_data.bars_5min"

# ── DB helpers ────────────────────────────────────────────────────────────────


def load_ticks_for_ticker(symbol: str, date_str: str) -> List[Tick]:
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    cur.execute(
        f"SELECT symbol, timestamp, open, high, low, close, volume "
        f"FROM {BARS_TABLE} WHERE symbol=%s AND timestamp::date=%s "
        f"ORDER BY timestamp ASC",
        (symbol, date_str),
    )
    ticks = []
    for row in cur.fetchall():
        ticks.append(Tick(
            ticker=row[0],
            timestamp=row[1].replace(tzinfo=timezone.utc) if row[1].tzinfo is None else row[1],
            open=float(row[2]), high=float(row[3]), low=float(row[4]),
            close=float(row[5]), volume=int(row[6]),
        ))
    conn.close()
    log.info("Loaded %d ticks for %s on %s", len(ticks), symbol, date_str)
    return ticks


# ── LLM trader ────────────────────────────────────────────────────────────────


def make_llm_trader(model: str = "google/gemini-2.5-flash-lite") -> callable:
    """Create a trader that calls OpenRouter LLM for each decision."""

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    engine = SignalEngine()

    def trader_fn(tick: Tick, portfolio: Portfolio) -> TraderDecision:
        report = engine.process(tick)
        price = tick.close
        positions_str = ", ".join(
            f"{t}: {p.shares}sh @ ${p.entry_price}"
            for t, p in portfolio.positions.items()
        ) or "none"

        prompt = f"""You are a paper trader. Analyze this tick and decide: BUY, SELL, or HOLD.

TICKER: {tick.ticker}
PRICE: ${price:.2f}
TIME: {tick.timestamp}
CASH: ${portfolio.cash:.2f}
EQUITY: ${portfolio.total_equity:.2f}
POSITIONS: {positions_str}

SIGNALS:
  composite: {report.composite_signal:.3f}
  conviction: {report.conviction:.3f}
  momentum: {report.momentum_signal} ({report.momentum_score:.3f})
  RSI: {report.rsi:.0f} ({report.rsi_signal})
  regime: {report.regime} (conf={report.regime_confidence:.2f})
  rec_size: {report.recommended_size_pct:.1%}

Reply with EXACTLY one line:
  BUY <shares> shares — <1 sentence reason>
  SELL <ticker> — <1 sentence reason>
  HOLD — <1 sentence reason>"""

        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 100,
                    "temperature": 0.2,
                },
                timeout=30,
            )
            if resp.status_code != 200:
                log.warning("LLM error %d: %s", resp.status_code, resp.text[:100])
                return TraderDecision(ticker=tick.ticker, decision="HOLD", conviction=0.0)

            text = resp.json()["choices"][0]["message"]["content"].strip()
            log.debug("LLM: %s", text)

            # Parse
            upper = text.upper()
            if upper.startswith("BUY"):
                parts = text.split()
                shares = 0
                for p in parts:
                    try:
                        shares = int(p)
                        break
                    except ValueError:
                        continue
                if shares > 0:
                    cost = shares * price
                    if cost <= portfolio.cash:
                        return TraderDecision(
                            ticker=tick.ticker, decision="BUY",
                            conviction=report.conviction,
                            rationale=text, shares=shares,
                        )
            elif upper.startswith("SELL"):
                for t in portfolio.positions:
                    if t.upper() in upper:
                        pos = portfolio.positions[t]
                        return TraderDecision(
                            ticker=t, decision="SELL",
                            conviction=report.conviction,
                            rationale=text, shares=pos.shares,
                        )
        except Exception as e:
            log.warning("LLM error: %s", e)

        return TraderDecision(ticker=tick.ticker, decision="HOLD", conviction=0.0)

    return trader_fn


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default="SPY")
    parser.add_argument("--date", default="2026-07-07")
    parser.add_argument("--variants", type=int, default=1)
    parser.add_argument("--model", default="google/gemini-2.5-flash-lite")
    args = parser.parse_args()

    ticks = load_ticks_for_ticker(args.ticker, args.date)
    if len(ticks) < 10:
        log.error("Only %d ticks — need at least 10", len(ticks))
        sys.exit(1)

    # Test different signal params as "variants"
    variants = {
        "baseline": SignalParams(),
        "aggro": SignalParams(momentum_threshold=0.15, base_size_pct=0.25),
        "patient": SignalParams(momentum_threshold=0.70, stop_loss_pct=0.03),
    }

    count = 0
    for name, params in list(variants.items())[:args.variants]:
        log.info("=== Running %s on %s (%s) ===", name, args.ticker, args.date)
        harness = ReplayHarness(initial_balance=10_000)
        trader = make_llm_trader(args.model)
        result = harness.run(ticks, trader)
        log.info(
            "%s: %d trades, P&L $%.2f, %.0f%% win, final equity $%.2f",
            name, len(result.trades), result.total_pnl,
            result.win_rate * 100, result.final_equity,
        )
        count += 1
        if count >= args.variants:
            break

    log.info("Done — %d variants tested", count)


if __name__ == "__main__":
    main()
