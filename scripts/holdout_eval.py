#!/usr/bin/env python3
"""
Holdout Evaluation — quarterly out-of-sample evaluation against the 15% holdout set.

This script:
  1. Creates/refreshes the holdout set (15% most recent trading dates)
  2. Runs evaluation on all holdout dates for specified traders
  3. Computes cost-adjusted metrics (objective score, Calmar, profit factor)
  4. Stores results in data/holdout.json for trend tracking
  5. Pushes a summary to Canvas

Usage:
    python3 scripts/holdout_eval.py                                              # default: all traders
    python3 scripts/holdout_eval.py --trader kairos                              # single trader
    python3 scripts/holdout_eval.py --update                                     # refresh holdout dates
    python3 scripts/holdout_eval.py --force                                      # overwrite holdout set
    python3 scripts/holdout_eval.py --dates 120                                  # larger date pool
    python3 scripts/holdout_eval.py --dry-run                                    # no canvas push

Spec: SPEC-v3 §7 — True Holdout Set
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from src.holdout import HoldoutManager
from src.transaction_costs import CostModel
from src.prompt_sweep import (
    SHORT_NAMES,
    TRADER_IDS,
    get_trading_days,
    load_historical_ticks,
)
from src.metrics import objective_score, compute_calmar, compute_profit_factor
from src.replay import ReplayHarness


def evaluate_trader(
    trader_short: str,
    holdout_mgr: HoldoutManager,
    cost_model: CostModel,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run holdout evaluation for a single trader.

    Args:
        trader_short: Trader short name (e.g., 'kairos').
        holdout_mgr: HoldoutManager with the holdout set.
        cost_model: Cost model to apply.
        dry_run: If True, only report what would be done.

    Returns:
        Dict with evaluation results.
    """
    holdout_dates = holdout_mgr.get_holdout_dates()
    if not holdout_dates:
        print(f"  [holdout_eval] No holdout dates configured for {trader_short} — skipping")
        return {"trader": trader_short, "status": "skipped", "reason": "no holdout dates"}

    print(f"\n  Evaluating {trader_short} on {len(holdout_dates)} holdout dates...")

    per_date_results: List[Dict[str, Any]] = []
    total_pnl = 0.0
    total_cost_pnl = 0.0
    total_trades = 0
    scores: List[float] = []
    calmars: List[float] = []
    profit_factors: List[float] = []
    win_rates: List[float] = []

    for date_str in holdout_dates:
        if dry_run:
            print(f"    DRY RUN: would evaluate {date_str}")
            continue

        ticks = load_historical_ticks(date_str)
        if not ticks:
            print(f"    ⚠️  No tick data for {date_str} — skipping")
            continue

        harness = ReplayHarness(
            initial_balance=100_000.0,
            cost_model=cost_model,
        )

        # Build a signal trader for this trader's params
        from src.signals import SignalEngine, SignalParams
        engine = SignalEngine(params=SignalParams())

        def trader_fn(tick, portfolio):
            return engine.process(tick)

        result = harness.run(ticks, trader_fn)

        trade_pnls = [getattr(t, "pnl_net", t.pnl) for t in result.trades]
        score = objective_score(result.returns, result.equity_curve, trade_pnls)
        calmar = float(compute_calmar(result.returns, result.equity_curve))
        pf = float(compute_profit_factor(trade_pnls))
        wr = result.net_win_rate if hasattr(result, "net_win_rate") else result.win_rate

        scores.append(score)
        calmars.append(calmar)
        profit_factors.append(pf)
        win_rates.append(wr)
        total_pnl += result.gross_pnl
        total_cost_pnl += result.total_pnl if hasattr(result, "total_pnl") else result.gross_pnl
        total_trades += len(result.trades)

        per_date_results.append({
            "date": date_str,
            "score": round(score, 4),
            "calmar": round(calmar, 4),
            "profit_factor": round(pf, 4),
            "win_rate": round(wr, 4),
            "trades": len(result.trades),
            "pnl": round(result.gross_pnl, 2),
            "net_pnl": round(result.total_pnl, 2),
        })

        print(f"    {date_str}: score={score:.4f}, calmar={calmar:.2f}, "
              f"pf={pf:.2f}, wr={wr:.1%}, trades={len(result.trades)}")

    if dry_run:
        return {"trader": trader_short, "status": "dry_run"}

    n_with_data = len(per_date_results)
    if n_with_data == 0:
        print(f"  ❌ No holdout dates had usable data for {trader_short}")
        return {"trader": trader_short, "status": "no_data"}

    summary = {
        "trader": trader_short,
        "status": "ok",
        "n_holdout_dates": len(holdout_dates),
        "n_with_data": n_with_data,
        "mean_score": round(sum(scores) / n_with_data, 4),
        "mean_calmar": round(sum(calmars) / n_with_data, 4),
        "mean_profit_factor": round(sum(profit_factors) / n_with_data, 4),
        "mean_win_rate": round(sum(win_rates) / n_with_data, 4),
        "total_pnl": round(total_pnl, 2),
        "total_cost_adjusted_pnl": round(total_cost_pnl, 2),
        "total_trades": total_trades,
        "per_date": per_date_results,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    print(f"\n  ✅ {trader_short} holdout summary:")
    print(f"     Dates: {summary['n_with_data']}/{summary['n_holdout_dates']}")
    print(f"     Score: {summary['mean_score']:.4f}")
    print(f"     Calmar: {summary['mean_calmar']:.2f}")
    print(f"     Profit factor: {summary['mean_profit_factor']:.2f}")
    print(f"     Win rate: {summary['mean_win_rate']:.1%}")
    print(f"     PnL (gross/net): ${summary['total_pnl']:.2f} / ${summary['total_cost_adjusted_pnl']:.2f}")
    print(f"     Trades: {summary['total_trades']}")

    return summary


def push_canvas_card(all_results: Dict[str, Any], elapsed: float, dry_run: bool = False) -> None:
    """Push a summary card to Canvas."""
    if dry_run:
        print(f"\n[canvas] DRY RUN — would push card to Canvas")
        return

    try:
        from src.canvas_dashboard import _push_to_canvas
    except ImportError:
        print("[canvas] canvas_dashboard not available — skipping")
        return

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"## 📊 Quarterly Holdout Evaluation — {date_str}",
        f"",
        f"**Duration:** {elapsed:.1f}s",
        f"**Holdout dates:** {all_results.get('n_holdout_dates', 0)}",
        f"**Cost model:** {all_results.get('slippage_bps', 10.0)} bps slippage + spread",
        f"",
        f"### Results per Trader",
        f"",
        f"| Trader | Score | Calmar | PF | Win Rate | PnL | Trades |",
        f"|--------|-------|--------|----|----------|-----|--------|",
    ]

    for trader_key, tr in all_results.get("traders", {}).items():
        if tr.get("status") != "ok":
            lines.append(f"| {trader_key} | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {trader_key} | {tr['mean_score']:.4f} | {tr['mean_calmar']:.2f} | "
            f"{tr['mean_profit_factor']:.2f} | {tr['mean_win_rate']:.1%} | "
            f"${tr['total_cost_adjusted_pnl']:.2f} | {tr['total_trades']} |"
        )

    content = "\n".join(lines)

    try:
        _push_to_canvas(
            title=f"📊 Holdout Eval — {date_str}",
            content=content,
            board="main",
            agent="coder",
            emoji="📊",
            expires_days=7,
        )
        print("[canvas] ✅ Card pushed")
    except Exception as e:
        print(f"[canvas] ⚠️  Failed to push card: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="Quarterly holdout evaluation — test against 15% unseen data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--trader", type=str, default=None,
        help="Trader short name (e.g., 'kairos'). Default: all traders.",
    )
    parser.add_argument(
        "--dates", type=int, default=120,
        help="Total trading days to consider when building holdout (default: 120).",
    )
    parser.add_argument(
        "--fraction", type=float, default=0.15,
        help="Fraction of dates to reserve as holdout (default: 0.15).",
    )
    parser.add_argument(
        "--update", action="store_true",
        help="Recompute holdout dates from the latest data.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Force overwrite existing holdout set.",
    )
    parser.add_argument(
        "--slippage", type=float, default=10.0,
        help="Slippage in basis points (default: 10.0).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip DB writes and Canvas push.",
    )

    args = parser.parse_args()

    start = time.time()

    # ── Traders to evaluate ─────────────────────────────────────────────
    if args.trader:
        traders = [args.trader]
    else:
        traders = [SHORT_NAMES[tid] for tid in TRADER_IDS]

    # ── Holdout manager ─────────────────────────────────────────────────
    mgr = HoldoutManager()

    # Get trading dates going back far enough
    from datetime import timedelta
    from src.prompt_sweep import get_trading_days

    # We need a large lookback to have meaningful 15%
    # Estimate: 120 calendar days ≈ 84 trading days → 15% ≈ 12 holdout days
    all_trading_dates = get_trading_days(n_days=args.dates)

    if not all_trading_dates:
        print(f"❌ No trading dates available in the last {args.dates} days")
        sys.exit(1)

    print(f"{'='*60}")
    print(f"  Quarterly Holdout Evaluation")
    print(f"{'='*60}")
    print(f"  Date pool: {all_trading_dates[0]} → {all_trading_dates[-1]} "
          f"({len(all_trading_dates)} trading days)")
    print(f"  Traders: {', '.join(traders)}")
    print(f"  Holdout fraction: {args.fraction:.0%}")
    print(f"  Cost model: {args.slippage} bps slippage")

    # ── Create or verify holdout set ────────────────────────────────────
    existing = mgr.get_holdout_dates()
    if existing and not args.update and not args.force:
        print(f"\n  Using existing holdout set: {len(existing)} dates "
              f"({existing[0]} → {existing[-1]})")
    else:
        action = "Updating" if args.update else "Creating"
        print(f"\n  {action} holdout set...")
        try:
            holdout_dates = mgr.create_holdout(
                all_dates=all_trading_dates,
                fraction=args.fraction,
                force=args.force or args.update,
            )
            print(f"  ✅ Holdout set: {len(holdout_dates)} dates "
                  f"({holdout_dates[0]} → {holdout_dates[-1]})")
        except ValueError as e:
            print(f"  ⚠️  {e}")
            # Fall back to existing
            holdout_dates = mgr.get_holdout_dates()
            if not holdout_dates:
                print("  ❌ Could not create or load holdout set")
                sys.exit(1)

    holdout_dates = mgr.get_holdout_dates()
    print(f"  Holdout dates: {len(holdout_dates)} "
          f"({holdout_dates[0]} → {holdout_dates[-1]})")

    # ── Cost model ──────────────────────────────────────────────────────
    cost_model = CostModel(
        slippage_bps=args.slippage,
        spread_bps=5.0,
    )

    # ── Evaluate each trader ────────────────────────────────────────────
    all_results: Dict[str, Any] = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "slippage_bps": args.slippage,
        "n_holdout_dates": len(holdout_dates),
        "n_total_dates": len(all_trading_dates),
        "traders": {},
    }

    for trader_short in traders:
        result = evaluate_trader(
            trader_short=trader_short,
            holdout_mgr=mgr,
            cost_model=cost_model,
            dry_run=args.dry_run,
        )
        all_results["traders"][trader_short] = result

        # Record eval in holdout manager
        if result.get("status") == "ok" and not args.dry_run:
            from src.holdout import QuarterlyEvalSummary
            summary = QuarterlyEvalSummary(
                trader=trader_short,
                holdout_dates=holdout_dates,
                n_dates=len(holdout_dates),
                n_dates_with_data=result["n_with_data"],
                mean_objective_score=result["mean_score"],
                mean_calmar=result["mean_calmar"],
                mean_profit_factor=result["mean_profit_factor"],
                mean_win_rate=result["mean_win_rate"],
                total_pnl=result["total_pnl"],
                total_cost_adjusted_pnl=result["total_cost_adjusted_pnl"],
                n_trades=result["total_trades"],
            )
            mgr.record_eval(trader_short, summary)

    # ── Canvas card ─────────────────────────────────────────────────────
    elapsed = time.time() - start
    all_results["duration_seconds"] = elapsed

    if not args.dry_run:
        push_canvas_card(all_results, elapsed, dry_run=args.dry_run)

    # ── Summary ─────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  Holdout Evaluation Complete ({elapsed:.1f}s)")
    print(f"{'='*60}")

    for trader_short, tr in all_results["traders"].items():
        status_icon = "✅" if tr.get("status") == "ok" else "⚠️"
        if tr.get("status") == "ok":
            print(f"  {status_icon} {trader_short}: "
                  f"score={tr['mean_score']:.4f}, "
                  f"calmar={tr['mean_calmar']:.2f}, "
                  f"pnl(net)=${tr['total_cost_adjusted_pnl']:.2f}")
        else:
            print(f"  {status_icon} {trader_short}: {tr.get('reason', tr.get('status', '?'))}")

    # Print evaluation history
    eval_history = mgr.get_evaluation_history()
    if eval_history and not args.dry_run:
        print(f"\n  Evaluation History ({len(eval_history)} runs):")
        for ev in eval_history[-3:]:
            print(f"    {ev['timestamp'][:10]}: {ev['trader']} — "
                  f"score={ev['mean_objective_score']:.4f}, "
                  f"pnl=${ev['total_pnl']:.2f}")


if __name__ == "__main__":
    main()