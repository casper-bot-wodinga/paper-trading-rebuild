#!/usr/bin/env python3
"""
Nightly Replay — Postgres-backed prompt variant sweep.

Loads bars from market_data.bars_5min (Postgres), deduplicates to daily OHLC
bars, converts to replay.Tick, generates prompt variants, scores each via
the replay harness, and outputs a leaderboard.

Usage:
    python3 src/nightly_replay.py --dry-run --date 2026-07-10
    python3 src/nightly_replay.py --date 2026-07-10
    python3 src/nightly_replay.py --date 2026-07-10 --variants 10
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# -- Add project src to path --
_PROJECT_SRC = str(Path(__file__).resolve().parent)
if _PROJECT_SRC not in sys.path:
    sys.path.insert(0, _PROJECT_SRC)

from db.connection import get_connection
from metrics import objective_score, compute_calmar, compute_profit_factor
from replay import ReplayHarness, Tick, Portfolio, TraderDecision, TraderFn
from signals import SignalEngine, SignalParams

log = logging.getLogger("nightly_replay")

# ==============================================================================
# Data loading — Postgres -> deduplicated daily bars -> replay.Tick
# ==============================================================================

# The 6 built-in variant templates from prompt_sweep.PERTURBATION_TEMPLATES
_VARIANT_TEMPLATES: List[Dict[str, Any]] = [
    {"name": "wider_stops", "description": "Widen SL/TP by 50%",
     "param_changes": {"stop_loss_pct": 1.5, "take_profit_pct": 1.5, "trailing_stop_pct": 1.5}},
    {"name": "tighter_stops", "description": "Tighten SL/TP by 30%",
     "param_changes": {"stop_loss_pct": 0.7, "take_profit_pct": 0.7, "trailing_stop_pct": 0.7}},
    {"name": "aggressive_sizing", "description": "Increase position sizing and conviction multiplier",
     "param_changes": {"base_size_pct": 1.4, "conviction_multiplier": 1.3, "max_positions": 1.4}},
    {"name": "conservative_sizing", "description": "Reduce position sizing and max positions",
     "param_changes": {"base_size_pct": 0.6, "conviction_multiplier": 0.7, "max_positions": 0.6}},
    {"name": "momentum_focus", "description": "Increase momentum weight, reduce mean-reversion weight",
     "param_changes": {"momentum_threshold": 1.2, "weight_trending_up": 1.4, "weight_trending_down": 0.5, "weight_mean_reverting": 0.5}},
    {"name": "mean_reversion_focus", "description": "Increase mean-reversion weight, reduce momentum",
     "param_changes": {"momentum_threshold": 0.7, "weight_trending_up": 0.6, "weight_mean_reverting": 1.6, "rsi_oversold": 1.3}},
    {"name": "trend_following", "description": "Strong trend following with longer lookback",
     "param_changes": {"momentum_lookback": 1.5, "momentum_decay": 1.1, "weight_trending_up": 1.6, "weight_high_volatility": 0.3}},
    {"name": "volatility_adaptive", "description": "Adapt more aggressively to volatility regime changes",
     "param_changes": {"vol_regime_threshold": 0.8, "vol_reduction_multiplier": 0.5, "weight_high_volatility": 0.7}},
]

# Baseline params — tuned for daily OHLC bars
# Key: momentum_threshold is low because daily EMA deviations are small (~0.3-1%)
_NIGHTLY_PARAMS: Dict[str, float] = {
    "momentum_threshold": 0.008,      # Ultra-low for daily data: 0.8% EMA deviation triggers BULLISH
    "momentum_lookback": 3,           # 3 days for daily data
    "momentum_decay": 0.70,           # Faster decay for short lookback
    "rsi_oversold": 30.0,
    "rsi_overbought": 70.0,
    "bollinger_std": 2.0,
    "volume_threshold": 1.2,
    "vol_regime_threshold": 0.25,
    "vol_reduction_multiplier": 0.7,
    "base_size_pct": 0.20,            # Larger position per trade
    "conviction_multiplier": 2.0,     # Higher conviction multiplier
    "max_positions": 8,               # More positions
    "stop_loss_pct": 0.01,            # FIX: 1% stop-loss (not 5%)
    "take_profit_pct": 0.03,          # FIX: 3% take-profit (not 15%)
    "trailing_stop_pct": 0.01,        # FIX: 1% trailing stop
    "weight_trending_up": 1.2,
    "weight_trending_down": 0.5,
    "weight_mean_reverting": 0.8,
    "weight_high_volatility": 0.4,
}


@dataclass
class VariantResult:
    """Score for one variant in the nightly sweep."""
    variant_id: int
    variant_name: str
    description: str
    score: float
    calmar: float
    profit_factor: float
    win_rate: float
    n_trades: int
    total_pnl: float
    total_return_pct: float
    params: SignalParams

    @property
    def beats_baseline(self) -> bool:
        return self.score > 0.05


# ==============================================================================
# Data loading
# ==============================================================================

def load_bars_from_postgres(
    date_str: str,
    lookback_days: int = 5,
) -> pd.DataFrame:
    """Load bars from market_data.bars_5min (symbol column, NOT ticker)."""
    target_date = datetime.strptime(date_str, "%Y-%m-%d")
    start_date = target_date - timedelta(days=lookback_days)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT symbol, timestamp, open, high, low, close, volume
               FROM market_data.bars_5min
               WHERE timestamp::date >= %s AND timestamp::date <= %s
               ORDER BY symbol, timestamp""",
            (start_date.date(), target_date.date()),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    if not rows:
        raise ValueError(f"No bars found for {start_date.date()} to {target_date.date()}")

    df = pd.DataFrame(rows, columns=["symbol", "timestamp", "open", "high", "low", "close", "volume"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    log.info("Loaded %d raw rows from Postgres (%s to %s)",
             len(df), start_date.date(), target_date.date())
    return df


def daily_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Deduplicate to one row per (symbol, day) — the daily OHLC bar.

    The bars_5min table records OHLCV at ~5-min intervals. Most rows
    within a day share the same values or differ only by close. We take
    each unique OHLCV per symbol per day and assign it to market open.
    """
    df = df.copy()
    ts = pd.to_datetime(df["timestamp"])
    df["day"] = ts.dt.date

    deduped = df.drop_duplicates(
        subset=["symbol", "day", "open", "high", "low", "close", "volume"],
        keep="last",            # Keep the last (most recent) snapshot
    ).copy()

    # Keep only the LAST snapshot per symbol per day (closest to market close)
    last_per_day = deduped.groupby(["symbol", "day"], as_index=False).last()

    # Set timestamp to end-of-day (20:00 UTC = 4 PM ET)
    last_per_day["timestamp"] = pd.to_datetime(last_per_day["day"].astype(str)) + pd.Timedelta(hours=20)
    last_per_day = last_per_day.drop(columns=["day"])
    last_per_day = last_per_day.sort_values(["symbol", "timestamp"]).reset_index(drop=True)

    log.info("Daily bars: %d rows (%d symbols, %d dates)",
             len(last_per_day), last_per_day["symbol"].nunique(),
             last_per_day["timestamp"].nunique())
    return last_per_day


def bars_to_ticks(df: pd.DataFrame) -> List[Tick]:
    """Convert daily OHLC bars to Tick objects."""
    ticks: List[Tick] = []
    for _, row in df.iterrows():
        ts = row["timestamp"]
        if isinstance(ts, pd.Timestamp):
            ts = ts.to_pydatetime()

        open_price = float(row["open"])
        close_price = float(row["close"])
        daily_return = (close_price - open_price) / open_price if open_price > 0 else 0.0
        daily_range = abs(float(row["high"] - row["low"]) / open_price) if open_price > 0 else 0.0

        # Momentum set as the daily return (what the engine will compute from close series)
        ticks.append(Tick(
            timestamp=ts,
            ticker=row["symbol"],
            open=open_price,
            high=float(row["high"]),
            low=float(row["low"]),
            close=close_price,
            volume=int(row["volume"]),
            momentum=daily_return * 100.0,
            volatility=daily_range,
        ))

    ticks.sort(key=lambda t: (t.timestamp, t.ticker))
    log.info("Converted %d bars to %d Tick objects", len(df), len(ticks))
    return ticks


def load_ticks_for_date(date_str: str, lookback_days: int = 5) -> List[Tick]:
    """One-stop: load from Postgres -> daily bars -> Ticks."""
    df = load_bars_from_postgres(date_str, lookback_days=lookback_days)
    daily = daily_bars(df)
    ticks = bars_to_ticks(daily)
    return ticks


# ==============================================================================
# Multi-ticker SignalEngine wrapper
# ==============================================================================

class MultiTickerSignalEngine:
    """Wraps SignalEngine to ensure per-ticker state isolation."""

    def __init__(self, params: SignalParams):
        self.params = params
        self._engines: Dict[str, SignalEngine] = {}

    def process(self, tick: Tick) -> Any:
        if tick.ticker not in self._engines:
            self._engines[tick.ticker] = SignalEngine(params=self.params)
        return self._engines[tick.ticker].process(tick)


# ==============================================================================
# Trader function (signal-only, no LLM)
# ==============================================================================

def make_signal_trader(params: SignalParams) -> TraderFn:
    """Create signal-only trader with per-ticker engine isolation.

    FIXES applied:
      - SL: 1%, TP: 3% (from params)
      - Single conviction gate at 0.4
      - History window: 3+ days for daily momentum
    """
    engine = MultiTickerSignalEngine(params=params)
    MIN_CONVICTION = 0.4

    def trader_fn(tick: Tick, portfolio: Portfolio) -> TraderDecision:
        report = engine.process(tick)

        # Risk management: check exits on held positions
        if tick.ticker in portfolio.positions:
            pos = portfolio.positions[tick.ticker]
            if tick.close <= report.stop_loss:
                return TraderDecision(
                    ticker=tick.ticker, decision="SELL", conviction=report.conviction,
                    rationale=f"SL hit at {tick.close:.2f}", shares=pos.shares,
                    signal_override=True)
            if tick.close >= report.take_profit:
                return TraderDecision(
                    ticker=tick.ticker, decision="SELL", conviction=report.conviction,
                    rationale=f"TP at {tick.close:.2f}", shares=pos.shares,
                    signal_override=True)
            return TraderDecision(
                ticker=tick.ticker, decision="HOLD", conviction=0.0,
                rationale="Position held")

        # Entry: momentum BULLISH + single conviction gate
        if (report.momentum_signal == "BULLISH"
                and report.conviction >= MIN_CONVICTION
                and portfolio.position_count < report.max_positions):
            return TraderDecision(
                ticker=tick.ticker, decision="BUY",
                conviction=report.conviction,
                rationale=f"Bullish: mom={report.momentum_score:.2f} "
                          f"rsi={report.rsi:.1f} reg={report.regime}",
                shares=0)

        # Entry: oversold RSI contrarian
        if (report.rsi_signal == "OVERSOLD"
                and report.conviction >= MIN_CONVICTION
                and portfolio.position_count < report.max_positions):
            return TraderDecision(
                ticker=tick.ticker, decision="BUY",
                conviction=report.conviction,
                rationale=f"Oversold: rsi={report.rsi:.1f} "
                          f"mom={report.momentum_score:.2f} reg={report.regime}",
                shares=0)

        return TraderDecision(
            ticker=tick.ticker, decision="HOLD", conviction=0.0,
            rationale="No signal")

    return trader_fn


# ==============================================================================
# Variant generation
# ==============================================================================

def generate_variant_params(
    baseline_params: SignalParams,
) -> List[Tuple[str, str, SignalParams]]:
    """Generate variant parameter sets from baseline + _VARIANT_TEMPLATES."""
    from dataclasses import fields as dc_fields
    field_names = [f.name for f in dc_fields(SignalParams) if f.name != "_BOUNDS"]

    variants: List[Tuple[str, str, SignalParams]] = [
        ("baseline", "Baseline nightly parameters", baseline_params),
    ]

    for template in _VARIANT_TEMPLATES:
        vp = SignalParams(**{name: getattr(baseline_params, name) for name in field_names})
        for param_name, multiplier in template["param_changes"].items():
            if hasattr(vp, param_name):
                current = getattr(vp, param_name)
                b = SignalParams.bound(param_name)
                new_val = int(round(current * multiplier)) if b.is_int else current * multiplier
                vp.set(param_name, new_val)
        variants.append((template["name"], template["description"], vp))

    return variants


# ==============================================================================
# Scoring
# ==============================================================================

def score_variant(params: SignalParams, ticks: List[Tick]) -> Tuple[float, Any]:
    """Score a variant by replaying it through historical ticks."""
    harness = ReplayHarness(
        initial_balance=100_000.0,
        max_position_pct=params.base_size_pct,
        require_conviction=0.4,
    )
    trader_fn = make_signal_trader(params)
    result = harness.run(ticks, trader_fn)
    trade_pnls = [t.pnl for t in result.trades]
    score = objective_score(result.returns, result.equity_curve, trade_pnls)
    return float(score), result


# ==============================================================================
# Leaderboard
# ==============================================================================

def print_leaderboard(
    results: List[VariantResult],
    baseline_score: float,
    total_ticks: int,
    elapsed: float,
) -> None:
    """Print formatted leaderboard."""
    print(f"\n{'='*80}")
    print(f"  NIGHTLY REPLAY LEADERBOARD")
    print(f"  Ticks: {total_ticks}  |  Elapsed: {elapsed:.1f}s")
    print(f"{'='*80}")
    print(f"  {'Rank':<5} {'Variant':<25} {'Score':<10} {'Calmar':<10} "
          f"{'PF':<8} {'WinRate':<10} {'Trades':<7} {'P&L':<12}")
    print(f"  {'-'*5} {'-'*25} {'-'*10} {'-'*10} {'-'*8} {'-'*10} {'-'*7} {'-'*12}")

    baseline_result = results[0] if results[0].variant_name == "baseline" else None
    if baseline_result:
        print(f"  {'0':<5} {'baseline':<25} {baseline_result.score:<10.4f} "
              f"{baseline_result.calmar:<10.2f} {baseline_result.profit_factor:<8.2f} "
              f"{baseline_result.win_rate:<10.1%} {baseline_result.n_trades:<7} "
              f"${baseline_result.total_pnl:<9.2f}")

    sorted_results = sorted(
        [r for r in results if r.variant_name != "baseline"],
        key=lambda r: r.score, reverse=True,
    )

    for i, r in enumerate(sorted_results, 1):
        flag = " *" if r.score > baseline_score + 0.05 else ""
        print(f"  {i:<5} {r.variant_name:<25} {r.score:<10.4f} "
              f"{r.calmar:<10.2f} {r.profit_factor:<8.2f} "
              f"{r.win_rate:<10.1%} {r.n_trades:<7} "
              f"${r.total_pnl:<9.2f}{flag}")

    best = sorted_results[0] if sorted_results else None
    if best and best.score > baseline_score + 0.05:
        print(f"\n  WINNER: {best.variant_name} (score: {best.score:.4f} vs baseline: {baseline_score:.4f})")
    else:
        print(f"\n  No variant beat baseline significantly")
        if best:
            print(f"     Best: {best.variant_name} ({best.score:.4f}) vs baseline ({baseline_score:.4f})")
    print(f"{'='*80}\n")


# ==============================================================================
# Main
# ==============================================================================

def run_nightly_replay(
    date_str: str,
    dry_run: bool = True,
    lookback_days: int = 5,
) -> List[VariantResult]:
    """Run the full nightly replay pipeline."""
    t0 = time.time()

    # 1. Load data
    print(f"[nightly_replay] Loading bars from Postgres for {date_str} (lookback: {lookback_days}d)...")
    ticks = load_ticks_for_date(date_str, lookback_days=lookback_days)
    print(f"[nightly_replay] Loaded {len(ticks)} ticks")

    if not ticks:
        print("[nightly_replay] ERROR: No ticks loaded")
        return []

    # 2. Setup baseline params
    baseline_params = SignalParams.from_dict(_NIGHTLY_PARAMS)
    print(f"[nightly_replay] Baseline: SL={baseline_params.stop_loss_pct:.0%} "
          f"TP={baseline_params.take_profit_pct:.0%} "
          f"mom_lk={baseline_params.momentum_lookback} "
          f"mom_thr={baseline_params.momentum_threshold:.4f} "
          f"conv_gate=0.4")

    # 3. Generate variants
    variants = generate_variant_params(baseline_params)
    print(f"[nightly_replay] Variants: {len(variants)} (baseline + {len(variants) - 1})")

    # 4. Score each variant
    results: List[VariantResult] = []
    for vid, (vname, vdesc, vparams) in enumerate(variants):
        vt0 = time.time()
        score, result = score_variant(vparams, ticks)
        vt1 = time.time()
        trade_pnls = [t.pnl for t in result.trades]
        calmar = float(compute_calmar(result.returns, result.equity_curve))
        profit_factor = float(compute_profit_factor(trade_pnls))
        results.append(VariantResult(
            variant_id=vid, variant_name=vname, description=vdesc,
            score=score, calmar=calmar, profit_factor=profit_factor,
            win_rate=result.win_rate, n_trades=len(result.trades),
            total_pnl=result.total_pnl, total_return_pct=result.total_return_pct,
            params=vparams,
        ))
        print(f"  [{vid + 1}/{len(variants)}] {vname:<25} "
              f"score={score:.4f}  trades={len(result.trades):<4}  "
              f"P&L=${result.total_pnl:<8.2f}  ({vt1 - vt0:.1f}s)")

    # 5. Print leaderboard
    total_elapsed = time.time() - t0
    print_leaderboard(results, results[0].score, len(ticks), total_elapsed)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Nightly Replay — Postgres-backed prompt variant sweep",
    )
    parser.add_argument("--date", type=str, default=None, help="Target date (YYYY-MM-DD). Default: yesterday.")
    parser.add_argument("--dry-run", action="store_true", help="Signal-only trader (no LLM).")
    parser.add_argument("--lookback", type=int, default=5, help="Days of history (default: 5).")
    parser.add_argument("--variants", type=int, default=8, help="Variants including baseline (default: 8).")
    args = parser.parse_args()

    date_str = args.date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")

    print(f"[nightly_replay] {'='*60}")
    print(f"[nightly_replay] Nightly Replay")
    print(f"[nightly_replay] Date: {date_str}")
    print(f"[nightly_replay] Mode: {'DRY RUN' if args.dry_run else 'FULL'}")
    print(f"[nightly_replay] {'='*60}\n")

    run_nightly_replay(date_str=date_str, dry_run=args.dry_run, lookback_days=args.lookback)


if __name__ == "__main__":
    main()