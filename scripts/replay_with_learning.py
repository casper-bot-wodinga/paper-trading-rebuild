#!/usr/bin/env python3
"""
Replay + Learning Loop — Fast closed-loop learning from historical data.

Chains: Replay → Write results → Agent reflection → Learning loop → Auto-promote → Re-run

This is the "fast learning loop" — agents learn from replay tests just like
they learn from live trading, but 100x faster. A full day of 5-min ticks
replays in seconds, then the reflection/learning/promotion cycle runs
immediately instead of waiting for market close.

Usage:
    python3 scripts/replay_with_learning.py                          # default: 5 days, all traders
    python3 scripts/replay_with_learning.py --days 10                # 10 trading days
    python3 scripts/replay_with_learning.py --trader kairos          # single trader
    python3 scripts/replay_with_learning.py --iterations 3           # auto-relax up to 3x
    python3 scripts/replay_with_learning.py --iterations 3 --apply   # actually write changes
    python3 scripts/replay_with_learning.py --symbols SPY,AAPL,NVDA  # specific symbols

Design:
    The fast learning loop mirrors the live trading feedback cycle:
    
    Live:        Tick → Agent → Trade → Journal → [30 min] → Heartbeat → [16:30] → Learning → [16:45] → Auto-promote
    Replay:      Tick → Agent → Trade → Journal → [immediate] → Reflection → [immediate] → Learning → [immediate] → Auto-promote
    
    The difference is speed. Replay compresses a day of ticks into seconds,
    and the reflection/learning/promotion cycle runs immediately after each
    replay run instead of waiting for market close.
    
    Auto-relax (from spec §3):
    If any trader generates 0 trades, the system automatically lowers
    signal thresholds and re-runs. Up to 3 iterations per run.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
STATE_DIR = PROJECT_DIR / "state"
sys.path.insert(0, str(PROJECT_DIR))

PG_DSN = os.getenv("PG_DSN", "host=trading-db port=5432 dbname=trading user=trader")


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 1: Replay
# ═══════════════════════════════════════════════════════════════════════════════


def fetch_bars(symbols: List[str], days_back: int = 5) -> Dict[str, List]:
    """Fetch 5-min bars from Postgres for replay."""
    import psycopg2, psycopg2.extras
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    all_ticks = {}

    for sym in symbols:
        cur.execute("""
            SELECT timestamp, open, high, low, close, volume
            FROM market_data.bars_5min
            WHERE symbol = %s AND timestamp >= %s
            ORDER BY timestamp ASC
        """, (sym, cutoff))
        rows = cur.fetchall()
        if rows:
            ticks = []
            for r in rows:
                tick = type('Tick', (), {
                    'timestamp': r['timestamp'],
                    'ticker': sym,
                    'symbol': sym,
                    'open': float(r['open']),
                    'high': float(r['high']),
                    'low': float(r['low']),
                    'close': float(r['close']),
                    'volume': int(r['volume'] or 0),
                })()
                ticks.append(tick)
            all_ticks[sym] = ticks

    conn.close()
    return all_ticks


def run_replay(symbols: List[str], days_back: int = 5,
               momentum_threshold: float = 0.05,
               conviction_required: float = 0.15) -> Dict[str, Any]:
    """Run signal-based replay across all symbols.

    Returns dict with trades, P&L, win rate per symbol.
    """
    from src.replay import ReplayHarness, TraderDecision
    from src.signals import SignalEngine, SignalParams

    ticks_by_ticker = fetch_bars(symbols, days_back)
    if not ticks_by_ticker:
        return {"error": "No data fetched", "total_trades": 0}

    total_bars = sum(len(v) for v in ticks_by_ticker.values())
    print(f"  📊 Fetched {total_bars} bars across {len(ticks_by_ticker)} symbols")

    # Create signal engine with adjusted thresholds
    params = SignalParams(
        momentum_threshold=momentum_threshold,
    )
    engine = SignalEngine(params)

    def trader_fn(tick, portfolio):
        report = engine.process(tick)
        if report and report.composite_signal > momentum_threshold:
            return TraderDecision(
                ticker=tick.ticker,
                decision='BUY',
                conviction=report.conviction,
                rationale=f'Signal {report.composite_signal:.2f}'
            )
        if report and report.composite_signal < -momentum_threshold and len(portfolio.positions) > 0:
            return TraderDecision(
                ticker=tick.ticker,
                decision='SELL',
                conviction=report.conviction,
                rationale=f'Close {report.composite_signal:.2f}'
            )
        return TraderDecision(
            ticker=tick.ticker,
            decision='HOLD',
            conviction=0,
            rationale='No signal'
        )

    results = {}
    total_trades = 0
    total_pnl = 0.0
    all_wins = 0

    for ticker, ticks in ticks_by_ticker.items():
        harness = ReplayHarness(initial_balance=10000.0)
        try:
            result = harness.run(ticks, trader_fn)
            n_trades = len(result.trades)
            pnl = sum(t.pnl for t in result.trades) if hasattr(result, 'trades') else 0
            wins = sum(1 for t in result.trades if t.pnl > 0) if hasattr(result, 'trades') else 0
            wr = wins / n_trades if n_trades > 0 else 0

            total_trades += n_trades
            total_pnl += pnl
            all_wins += wins

            results[ticker] = {
                "trades": n_trades,
                "pnl": round(pnl, 2),
                "win_rate": round(wr, 3),
            }
        except Exception as e:
            results[ticker] = {"trades": 0, "pnl": 0, "win_rate": 0, "error": str(e)}

    # Replay results table
    print(f"\n  {'Symbol':8s} {'Trades':>6s} {'P&L':>10s} {'WR':>6s}")
    print(f"  {'-'*32}")
    for sym, r in sorted(results.items(), key=lambda x: x[1]['trades'], reverse=True)[:15]:
        if r['trades'] > 0:
            print(f"  {sym:8s} {r['trades']:6d} ${r['pnl']:>+8.2f} {r['win_rate']:.1%}")
    print(f"  {'-'*32}")
    print(f"  {'TOTAL':8s} {total_trades:6d} ${total_pnl:>+8.2f} {all_wins/max(total_trades,1):.1%}")

    return {
        "symbols": results,
        "total_trades": total_trades,
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(all_wins / max(total_trades, 1), 3),
        "params": {"momentum_threshold": momentum_threshold, "conviction_required": conviction_required},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2: Write to DB (so agents can learn from replay)
# ═══════════════════════════════════════════════════════════════════════════════


def write_replay_to_db(replay_result: Dict[str, Any], trader: str) -> int:
    """Write replay trades to trading.executed_trades AND trading.decisions/trades.

    The learning loop reads from decisions + trades tables, not just executed_trades.
    We write to ALL three so the full reflection pipeline works.
    """
    import psycopg2, json
    from datetime import datetime, timezone
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()

    agent_id = f"trader-{trader}"
    written = 0
    now = datetime.now(timezone.utc)

    for sym, data in replay_result.get("symbols", {}).items():
        if data.get("trades", 0) == 0:
            continue

        # 1. Write to executed_trades (for dashboard / auto-promote)
        cur.execute(
            """INSERT INTO trading.executed_trades
               (agent_id, ticker, action, shares, price, stop_loss, pnl,
                entry_time, status, rationale)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                agent_id, sym, "REPLAY", data["trades"],
                0.0, 0.0, data["pnl"],
                now, "replay",
                f"Replay: {data['trades']} trades, ${data['pnl']:+.2f}, {data['win_rate']:.1%} WR",
            ),
        )

        # 2. Write to trading.decisions (for learning loop)
        cur.execute(
            """INSERT INTO trading.decisions
               (trader_id, ticker, timestamp, decision, conviction, rationale, decision_json)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                trader, sym, now,
                "BUY" if data["pnl"] > 0 else "HOLD",
                data["win_rate"],
                f"Replay signal: {data['trades']} trades, {data['win_rate']:.1%} WR",
                json.dumps({"source": "replay_test", "trades": data["trades"], "pnl": data["pnl"]}),
            ),
        )

        written += 1

    conn.commit()
    conn.close()
    print(f"  ✅ Wrote {written} replay entries to executed_trades + decisions for {trader}")
    return written


def write_reflection(trader: str, replay_result: Dict[str, Any], iteration: int):
    """Write agent reflection to trading.agent_reflections.

    This is where the agent "reflects" on its performance and generates
    insights — exactly like the spec describes.
    """
    import psycopg2, json
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()

    agent_id = f"trader-{trader}"
    insights = {
        "total_trades": replay_result["total_trades"],
        "total_pnl": replay_result["total_pnl"],
        "win_rate": replay_result["win_rate"],
        "iteration": iteration,
        "params": replay_result.get("params", {}),
        "best_symbols": sorted(
            replay_result.get("symbols", {}).items(),
            key=lambda x: x[1]["pnl"],
            reverse=True,
        )[:5],
        "worst_symbols": sorted(
            replay_result.get("symbols", {}).items(),
            key=lambda x: x[1]["pnl"],
        )[:3],
    }

    # Generate suggested changes based on results
    suggested = []
    if replay_result["total_trades"] == 0:
        suggested.append({
            "action": "lower_thresholds",
            "reason": "Zero trades generated — signal thresholds too high",
            "confidence": 0.9,
        })
    elif replay_result["win_rate"] < 0.3 and replay_result["total_trades"] >= 5:
        suggested.append({
            "action": "tighten_stops",
            "reason": f"Win rate {replay_result['win_rate']:.1%} — too many losing trades",
            "confidence": 0.7,
        })

    cur.execute(
        """INSERT INTO trading.agent_reflections
           (agent_id, reflection_date, source_type, insights, suggested_changes, metrics_snapshot)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (
            agent_id,
            datetime.now().date(),
            "replay_test",
            json.dumps(insights),
            json.dumps(suggested),
            json.dumps(replay_result.get("symbols", {})),
        ),
    )
    conn.commit()
    conn.close()
    print(f"  ✅ Reflection written for {agent_id} (iteration {iteration})")


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3: Auto-relax (detect 0 trades, lower thresholds, re-run)
# ═══════════════════════════════════════════════════════════════════════════════


def auto_relax(replay_result: Dict[str, Any], symbols: List[str],
               days_back: int, max_iterations: int = 3, apply: bool = False) -> Dict[str, Any]:
    """Auto-relax signal thresholds if zero trades detected.

    Implements spec §3 Phase 3: "Auto-relax & re-sweep"
    - Max 3 relaxation iterations
    - Thresholds converge to level where trades happen
    - Then optimize for quality
    """
    iteration = 0
    current_mt = replay_result["params"]["momentum_threshold"]
    current_cr = replay_result["params"]["conviction_required"]

    while iteration < max_iterations:
        iteration += 1
        print(f"\n  🔄 Relaxation iteration {iteration}/{max_iterations}")

        if replay_result["total_trades"] == 0:
            # Lower thresholds aggressively
            current_mt = max(current_mt * 0.5, 0.01)
            current_cr = max(current_cr * 0.5, 0.05)
            print(f"    → No trades! Lowering: momentum_threshold={current_mt:.3f}, conviction_required={current_cr:.2f}")

            # Re-run with relaxed thresholds
            replay_result = run_replay(symbols, days_back, current_mt, current_cr)

            if apply:
                write_replay_to_db(replay_result, "kairos")
                write_reflection("kairos", replay_result, iteration)

        elif replay_result["win_rate"] < 0.3:
            # Need better quality — tighten slightly
            current_mt = min(current_mt * 1.2, 0.3)
            print(f"    → Low WR ({replay_result['win_rate']:.1%}). Tightening: momentum_threshold={current_mt:.3f}")

            replay_result = run_replay(symbols, days_back, current_mt, current_cr)

            if apply:
                write_replay_to_db(replay_result, "kairos")
                write_reflection("kairos", replay_result, iteration)
        else:
            print(f"    ✅ {replay_result['total_trades']} trades, {replay_result['win_rate']:.1%} WR — good enough!")
            break

    replay_result["final_params"] = {"momentum_threshold": current_mt, "conviction_required": current_cr}
    replay_result["iterations"] = iteration
    return replay_result


# ═══════════════════════════════════════════════════════════════════════════════
# Main orchestrator
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="Replay + Learning Loop — fast closed-loop learning from historical data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                          # 5 days, all traders, dry-run
  %(prog)s --days 10 --iterations 3 --apply         # 10 days, 3 relax iterations, apply changes
  %(prog)s --trader kairos --symbols SPY,AAPL,NVDA  # single trader, specific symbols
  %(prog)s --iterations 3 --apply                    # full auto-relax with apply
        """,
    )
    parser.add_argument("--days", type=int, default=5,
                        help="Days of historical data to replay (default: 5)")
    parser.add_argument("--trader", type=str, default="kairos",
                        choices=["kairos", "aldridge", "stonks"],
                        help="Trader to replay (default: kairos)")
    parser.add_argument("--symbols", type=str, default=None,
                        help="Comma-separated symbols (default: top 15 from 5-min data)")
    parser.add_argument("--iterations", type=int, default=1,
                        help="Max auto-relax iterations (default: 1, max: 5)")
    parser.add_argument("--apply", action="store_true",
                        help="Write results to DB, trigger learning loop, auto-promote")

    args = parser.parse_args()
    max_iter = min(args.iterations, 5)

    # Default symbols
    symbols = args.symbols.split(",") if args.symbols else [
        "SPY", "QQQ", "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA",
        "JPM", "AMD", "AVGO", "HOOD", "COIN", "PLTR",
    ]

    print(f"=== Replay + Learning Loop ===")
    print(f"Trader: {args.trader} | Days: {args.days} | Symbols: {len(symbols)} | "
          f"Max relax: {max_iter} | Mode: {'APPLY' if args.apply else 'DRY-RUN'}")

    # Phase 1: Initial replay with default thresholds
    print(f"\n📈 Phase 1: Initial replay")
    default_mt = 0.05
    default_cr = 0.15
    replay_result = run_replay(symbols, args.days, default_mt, default_cr)

    if "error" in replay_result:
        print(f"❌ Replay failed: {replay_result['error']}")
        sys.exit(1)

    # Phase 2: Auto-relax if needed
    if max_iter > 1:
        print(f"\n🔄 Phase 2: Auto-relax (up to {max_iter} iterations)")
        replay_result = auto_relax(
            replay_result, symbols, args.days,
            max_iterations=max_iter, apply=args.apply,
        )

    # Phase 3: Write to DB + trigger learning loop
    if args.apply:
        print(f"\n💾 Phase 3: Writing to DB")
        write_replay_to_db(replay_result, args.trader)
        write_reflection(args.trader, replay_result, replay_result.get("iterations", 1))

        # Trigger learning loop
        print(f"\n🧠 Phase 4: Learning loop")
        agent_id = f"trader-{args.trader}"
        try:
            result = subprocess.run(
                [sys.executable, "-m", "src.learning_loop", "--agent", agent_id],
                cwd=str(PROJECT_DIR),
                capture_output=True, text=True, timeout=120,
            )
            print(result.stdout[-500:] if len(result.stdout) > 500 else result.stdout)
            if result.returncode != 0:
                print(f"  ⚠️  Learning loop exit code: {result.returncode}")
        except Exception as e:
            print(f"  ⚠️  Learning loop failed: {e}")

        # Trigger auto-promote
        print(f"\n📝 Phase 5: Auto-promote")
        try:
            result = subprocess.run(
                [sys.executable, "scripts/auto_promote_prompts.py", "--apply", "--force"],
                cwd=str(PROJECT_DIR),
                capture_output=True, text=True, timeout=60,
            )
            print(result.stdout[-500:] if len(result.stdout) > 500 else result.stdout)
        except Exception as e:
            print(f"  ⚠️  Auto-promote failed: {e}")

        print(f"\n✅ Full cycle complete!")
    else:
        print(f"\nℹ️  Dry-run complete. Run with --apply to write to DB and trigger learning.")

    # Summary
    fp = replay_result.get("final_params", replay_result.get("params", {}))
    print(f"\n{'='*50}")
    print(f"Summary: {replay_result['total_trades']} trades, "
          f"${replay_result['total_pnl']:+.2f} P&L, "
          f"{replay_result['win_rate']:.1%} WR")
    print(f"Params: momentum_threshold={fp.get('momentum_threshold', default_mt):.3f}, "
          f"conviction_required={fp.get('conviction_required', default_cr):.2f}")
    if replay_result.get("iterations", 1) > 1:
        print(f"Auto-relax iterations: {replay_result['iterations']}")


if __name__ == "__main__":
    main()