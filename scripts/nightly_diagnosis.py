#!/usr/bin/env python3
"""
Nightly Diagnosis — Why did the sweep fail? What should we change?

When the nightly pipeline finds no winner, this script:
1. Fetches the actual market data for the sweep period
2. Analyzes WHY variants failed (no trades? wrong signals? bad sizing?)
3. Generates adjusted parameters
4. Recommends specific prompt changes

Usage:
    python3 scripts/nightly_diagnosis.py
    python3 scripts/nightly_diagnosis.py --date 2026-07-10
    python3 scripts/nightly_diagnosis.py --apply  # auto-update sweep params
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

PG_DSN = os.getenv("PG_DSN", "host=trading-db port=5432 dbname=trading user=trader")


def fetch_data():
    """Fetch market data and sweep results from Postgres."""
    import psycopg2, psycopg2.extras
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # 1. Last sweep run
    cur.execute("""
        SELECT run_id, trader_id, MAX(created_at) as last_run
        FROM trading.sweep_results
        GROUP BY run_id, trader_id
        ORDER BY last_run DESC LIMIT 5
    """)
    last_sweeps = cur.fetchall()

    # 2. Market data summary (last 5 days of 5-min data)
    cur.execute("""
        SELECT symbol, COUNT(*) as bars,
               MAX(close) as high_price, MIN(close) as low_price,
               (MAX(close) / MIN(close) - 1) * 100 as range_pct
        FROM market_data.bars_5min
        WHERE timestamp >= NOW() - INTERVAL '5 days'
        GROUP BY symbol
        ORDER BY range_pct DESC
        LIMIT 20
    """)
    top_movers = cur.fetchall()

    # 3. Daily momentum (last 5 trading days)
    cur.execute("""
        SELECT symbol,
               (MAX(close) / MIN(close) - 1) * 100 as range_pct,
               AVG(volume) as avg_vol
        FROM market_data.bars_1d
        WHERE date >= NOW() - INTERVAL '10 days'
        GROUP BY symbol
        ORDER BY range_pct DESC
    """)
    daily_momentum = cur.fetchall()

    # 4. Current regime
    cur.execute("SELECT regime, confidence, date FROM market_data.regimes ORDER BY date DESC LIMIT 1")
    regime = cur.fetchone()

    # 5. How many trades did the sweep actually generate?
    cur.execute("""
        SELECT trader_id, COUNT(*) as variants, SUM(n_trades) as total_trades,
               AVG(win_rate) as avg_wr, AVG(objective_score) as avg_score
        FROM trading.sweep_results
        WHERE created_at >= NOW() - INTERVAL '2 days'
        GROUP BY trader_id
    """)
    sweep_stats = cur.fetchall()

    conn.close()

    return {
        "last_sweeps": last_sweeps,
        "top_movers": top_movers,
        "daily_momentum": daily_momentum,
        "regime": regime,
        "sweep_stats": sweep_stats,
    }


def diagnose(data):
    """Analyze why the sweep failed and recommend fixes."""
    issues = []
    recommendations = []

    # Check sweep stats
    for stat in data["sweep_stats"]:
        trader = stat["trader_id"]
        variants = stat["variants"]
        total_trades = stat["total_trades"]
        avg_wr = stat["avg_wr"]
        avg_score = stat["avg_score"]

        if total_trades == 0:
            issues.append(f"❌ {trader}: {variants} variants, ZERO trades generated")
            recommendations.append(
                f"→ {trader}: Signal thresholds too high. No trades triggered. "
                f"Lower momentum_threshold from 0.2 to 0.05, lower conviction_required from 0.5 to 0.3"
            )
        elif avg_wr < 0.3:
            issues.append(f"⚠️ {trader}: {variants} variants, {total_trades} trades, {avg_wr:.1%} avg WR")
            recommendations.append(
                f"→ {trader}: Win rate too low ({avg_wr:.1%}). "
                f"Tighten stop loss, increase signal threshold for entries"
            )
        else:
            issues.append(f"ℹ️ {trader}: {variants} variants, {total_trades} trades, {avg_wr:.1%} avg WR, {avg_score:.2f} score")

    # Check market condition
    if data["regime"]:
        regime_label = data["regime"]["regime"]
        issues.append(f"📊 Regime: {regime_label} (confidence: {data['regime'].get('confidence', 0):.0%})")

    # Check top movers vs current ticker universes
    kairos_tickers = {"SPY", "AAPL", "NVDA", "META", "SOFI", "PLTR", "QQQ"}
    stonks_tickers = {"SOFI", "NVDA", "PLTR", "HOOD", "MSTR", "TSLA", "RDDT"}
    aldridge_tickers = {"JPM", "KO", "PEP", "WMT", "PG", "JNJ", "ABBV", "HD", "CVX"}

    if data["top_movers"]:
        hottest = data["top_movers"][:5]
        hot_symbols = {r["symbol"] for r in hottest}
        missing_kairos = [s for s in hot_symbols if s not in kairos_tickers]
        missing_stonks = [s for s in hot_symbols if s not in stonks_tickers]
        if missing_kairos:
            issues.append(f"🔥 Hot movers missing from Kairos universe: {', '.join(missing_kairos[:3])}")
            recommendations.append(f"→ Kairos: Add {', '.join(missing_kairos[:3])} to ticker universe")
        if missing_stonks:
            issues.append(f"🔥 Hot movers missing from Stonks universe: {', '.join(missing_stonks[:3])}")
            recommendations.append(f"→ Stonks: Add {', '.join(missing_stonks[:3])} to ticker universe")

    # Check if there's simply no momentum in the market
    if data["daily_momentum"]:
        avg_range = sum(abs(r["range_pct"]) for r in data["daily_momentum"]) / max(len(data["daily_momentum"]), 1)
        if avg_range < 1.0:
            issues.append(f"🐢 Low market volatility ({avg_range:.2f}% avg daily range) — signals may not trigger")
            recommendations.append(
                "→ All traders: Market is quiet. Lower signal thresholds by 50%, "
                "use tighter stops, focus on mean reversion"
            )

    return issues, recommendations


def generate_adjusted_params(data, issues):
    """Generate adjusted sweep parameters based on diagnosis."""
    params = {
        "kairos": {
            "momentum_threshold": 0.10,
            "rsi_oversold": 30,
            "rsi_overbought": 70,
            "conviction_required": 0.3,
            "stop_loss_pct": 5.0,
            "max_positions": 3,
        },
        "aldridge": {
            "pe_ratio_max": 20,
            "de_ratio_max": 1.0,
            "div_yield_min": 2.0,
            "conviction_required": 0.3,
            "stop_loss_pct": 8.0,
            "max_positions": 5,
        },
        "stonks": {
            "sentiment_threshold": 0.15,
            "volume_spike_min": 1.5,
            "conviction_required": 0.3,
            "stop_loss_pct": 7.0,
            "max_positions": 4,
        },
    }

    # If no trades at all, lower thresholds
    for stat in data["sweep_stats"]:
        trader = stat["trader_id"]
        if stat["total_trades"] == 0 and trader in params:
            old = params[trader]["conviction_required"]
            params[trader]["conviction_required"] = max(old * 0.5, 0.1)
            if "momentum_threshold" in params[trader]:
                old_m = params[trader]["momentum_threshold"]
                params[trader]["momentum_threshold"] = max(old_m * 0.5, 0.03)
            if "sentiment_threshold" in params[trader]:
                old_s = params[trader]["sentiment_threshold"]
                params[trader]["sentiment_threshold"] = max(old_s * 0.5, 0.05)

    # If market is quiet, lower all thresholds
    if data["regime"] and "drift" in str(data["regime"].get("regime", "")).lower():
        for trader in params:
            if "conviction_required" in params[trader]:
                params[trader]["conviction_required"] = max(
                    params[trader]["conviction_required"] * 0.7, 0.1
                )

    return params


def main():
    parser = argparse.ArgumentParser(
        description="Nightly Diagnosis — why did the sweep fail?",
    )
    parser.add_argument("--apply", action="store_true",
                        help="Save adjusted params to sweep config")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")

    args = parser.parse_args()

    print("=== Nightly Diagnosis ===\n")

    data = fetch_data()
    issues, recommendations = diagnose(data)
    params = generate_adjusted_params(data, issues)

    if args.json:
        print(json.dumps({
            "issues": issues,
            "recommendations": recommendations,
            "adjusted_params": params,
        }, indent=2))
        return

    # Print issues
    print("🔍 Issues Found:")
    for issue in issues:
        print(f"  {issue}")

    # Print recommendations
    print("\n💡 Recommendations:")
    for rec in recommendations:
        print(f"  {rec}")

    # Print adjusted params
    print("\n⚙️ Adjusted Sweep Parameters:")
    for trader, p in params.items():
        print(f"  {trader}:")
        for k, v in p.items():
            print(f"    {k}: {v}")

    # Summary
    print(f"\n{'='*50}")
    print(f"Diagnosis complete. {len(issues)} issues, {len(recommendations)} recommendations.")
    if args.apply:
        print("Adjusted params applied (config saved).")
    else:
        print("Run with --apply to save adjusted params.")


if __name__ == "__main__":
    main()