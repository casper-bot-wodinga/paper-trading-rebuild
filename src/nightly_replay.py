#!/usr/bin/env python3
"""
Nightly Replay — Postgres-backed prompt variant sweep.

Loads bars from market_data.bars_5min (Postgres), deduplicates to daily OHLC,
converts to replay.Tick, generates prompt variants, scores each via
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

_PROJECT_SRC = str(Path(__file__).resolve().parent)
if _PROJECT_SRC not in sys.path:
    sys.path.insert(0, _PROJECT_SRC)

from db.connection import get_connection
from metrics import objective_score, compute_calmar, compute_profit_factor
from replay import ReplayHarness, Tick, Portfolio, TraderDecision, TraderFn
from signals import SignalEngine, SignalParams
from transaction_costs import CostModel

log = logging.getLogger("nightly_replay")

# ==============================================================================
# Variant templates from prompt_sweep
# ==============================================================================

_VARIANT_TEMPLATES: List[Dict[str, Any]] = [
    {"name": "wider_stops", "description": "Widen SL/TP by 50%",
     "param_changes": {"stop_loss_pct": 1.5, "take_profit_pct": 1.5, "trailing_stop_pct": 1.5}},
    {"name": "tighter_stops", "description": "Tighten SL/TP by 30%",
     "param_changes": {"stop_loss_pct": 0.7, "take_profit_pct": 0.7, "trailing_stop_pct": 0.7}},
    {"name": "aggressive_sizing", "description": "Increase position sizing",
     "param_changes": {"base_size_pct": 1.4, "conviction_multiplier": 1.3, "max_positions": 1.4}},
    {"name": "conservative_sizing", "description": "Reduce position sizing",
     "param_changes": {"base_size_pct": 0.6, "conviction_multiplier": 0.7, "max_positions": 0.6}},
    {"name": "momentum_focus", "description": "Increase momentum weight",
     "param_changes": {"momentum_threshold": 1.2, "weight_trending_up": 1.4, "weight_trending_down": 0.5, "weight_mean_reverting": 0.5}},
    {"name": "mean_reversion_focus", "description": "Increase mean-reversion weight",
     "param_changes": {"momentum_threshold": 0.7, "weight_trending_up": 0.6, "weight_mean_reverting": 1.6, "rsi_oversold": 1.3}},
    {"name": "trend_following", "description": "Strong trend following",
     "param_changes": {"momentum_lookback": 1.5, "momentum_decay": 1.1, "weight_trending_up": 1.6, "weight_high_volatility": 0.3}},
    {"name": "volatility_adaptive", "description": "Adapt to volatility regime",
     "param_changes": {"vol_regime_threshold": 0.8, "vol_reduction_multiplier": 0.5, "weight_high_volatility": 0.7}},
]

# Baseline params — tuned for daily OHLC bars from Postgres
# Bypasses SignalParams hard-coded bounds (momentum_threshold min=0.3, etc.)
_NIGHTLY_PARAMS: Dict[str, float] = {
    "momentum_threshold": 0.008,
    "momentum_lookback": 3,
    "momentum_decay": 0.70,
    "rsi_oversold": 30.0,
    "rsi_overbought": 70.0,
    "bollinger_std": 2.0,
    "volume_threshold": 1.2,
    "vol_regime_threshold": 0.25,
    "vol_reduction_multiplier": 0.7,
    "base_size_pct": 0.20,
    "conviction_multiplier": 2.0,
    "max_positions": 10,
    "stop_loss_pct": 0.01,
    "take_profit_pct": 0.03,
    "trailing_stop_pct": 0.01,
    "weight_trending_up": 1.2,
    "weight_trending_down": 0.5,
    "weight_mean_reverting": 0.8,
    "weight_high_volatility": 0.4,
}


@dataclass
class VariantResult:
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


def _make_params(d: Dict[str, float]) -> SignalParams:
    """Create SignalParams directly bypassing from_dict clipping."""
    p = SignalParams()
    for name, value in d.items():
        if hasattr(p, name) and name != "_BOUNDS":
            setattr(p, name, value)
    return p


# ==============================================================================
# Data loading
# ==============================================================================

def load_bars_from_postgres(date_str: str, lookback_days: int = 5) -> pd.DataFrame:
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
    log.info("Loaded %d raw rows (%s to %s)", len(df), start_date.date(), target_date.date())
    return df


def compress_to_5min_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Compress to one snapshot per 5-min bucket per symbol.

    Takes the LAST snapshot in each bucket (most recent), drops consecutive
    duplicate OHLCV rows (handles EOD-data days).
    """
    df = df.copy()
    ts = pd.to_datetime(df["timestamp"])
    df["bucket"] = ts.dt.floor("5min")
    last = df.groupby(["symbol", "bucket"], as_index=False).last()

    # Drop consecutive identical OHLCV rows (EOD data duplicated every 5min)
    is_dup = (
        (last.groupby("symbol")["close"].diff().abs() < 1e-6) &
        (last.groupby("symbol")["open"].diff().abs() < 1e-6)
    )
    compressed = last[~is_dup].copy()
    compressed = compressed.drop(columns=["timestamp"])
    compressed = compressed.rename(columns={"bucket": "timestamp"})
    compressed = compressed.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
    log.info("Compressed %d rows to %d 5-min bars", len(df), len(compressed))
    return compressed


def bars_to_ticks(df: pd.DataFrame) -> List[Tick]:
    """Convert 5-min OHLC bars to Tick objects.

    Filters to US market hours (13:30-20:00 UTC) to avoid pre/post market.
    For days where OHLCV is static (EOD data), keeps one bar per day.
    """
    ticks: List[Tick] = []
    for _, row in df.iterrows():
        ts = row["timestamp"]
        if isinstance(ts, pd.Timestamp):
            ts = ts.to_pydatetime()

        # Filter to US market hours (9:30 AM - 4:00 PM ET = 13:30-20:00 UTC)
        # Bars exactly at 20:00 UTC (4 PM ET close) are included.
        hour = ts.hour + ts.minute / 60.0
        if hour < 13.5 or hour > 20.0:
            continue

        open_price = float(row["open"])
        close_price = float(row["close"])
        bar_return = (close_price - open_price) / open_price if open_price > 0 else 0.0
        bar_range = abs(float(row["high"] - row["low"]) / open_price) if open_price > 0 else 0.0

        ticks.append(Tick(
            timestamp=ts,
            ticker=row["symbol"],
            open=open_price,
            high=float(row["high"]),
            low=float(row["low"]),
            close=close_price,
            volume=int(row["volume"]),
            momentum=bar_return * 100.0,
            volatility=bar_range,
        ))

    # Add EOD bars as daily ticks for symbols that lack intraday data
    daily_df = _get_daily_bars(df)
    for _, row in daily_df.iterrows():
        ts = row["timestamp"]
        if isinstance(ts, pd.Timestamp):
            ts = ts.to_pydatetime()
        # Only add if this symbol doesn't already have ticks at this time
        already = any(
            t.ticker == row["symbol"] and t.timestamp.date() == ts.date()
            for t in ticks
        )
        if already:
            continue
        open_price = float(row["open"])
        close_price = float(row["close"])
        daily_return = (close_price - open_price) / open_price if open_price > 0 else 0.0
        daily_range = abs(float(row["high"] - row["low"]) / open_price) if open_price > 0 else 0.0
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
    log.info("Converted to %d Tick objects (%d unique symbols, %d dates)",
             len(ticks), len(set(t.ticker for t in ticks)),
             len(set(t.timestamp.date() for t in ticks)))
    return ticks


def _get_daily_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Get one daily bar per symbol per day (last unique snapshot per day)."""
    df = df.copy()
    ts = pd.to_datetime(df["timestamp"])
    df["day"] = ts.dt.date
    deduped = df.drop_duplicates(
        subset=["symbol", "day", "open", "high", "low", "close", "volume"],
        keep="last",
    ).copy()
    last = deduped.groupby(["symbol", "day"], as_index=False).last()
    last["timestamp"] = pd.to_datetime(last["day"].astype(str)) + pd.Timedelta(hours=20)
    last["timestamp"] = last["timestamp"].dt.tz_localize("UTC")
    last = last.drop(columns=["day"])
    last = last.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
    return last


def load_ticks_for_date(date_str: str, lookback_days: int = 5) -> List[Tick]:
    """One-stop: load Postgres -> compress -> bar conversion."""
    df = load_bars_from_postgres(date_str, lookback_days=lookback_days)
    compressed = compress_to_5min_bars(df)
    ticks = bars_to_ticks(compressed)
    return ticks


# ==============================================================================
# Multiticker engine
# ==============================================================================

class MultiTickerSignalEngine:
    def __init__(self, params: SignalParams):
        self.params = params
        self._engines: Dict[str, SignalEngine] = {}

    def process(self, tick: Tick) -> Any:
        if tick.ticker not in self._engines:
            self._engines[tick.ticker] = SignalEngine(params=self.params)
        return self._engines[tick.ticker].process(tick)


# ==============================================================================
# Trader
# ==============================================================================

def make_signal_trader(params: SignalParams, ticks: List[Tick]) -> TraderFn:
    """Signal-only trader with per-ticker engine isolation.

    FIXES: SL=1%/TP=3%, single conviction gate at 0.2, history=3+d.
    Closes all positions on the last trading day (forced exit).
    """
    engine = MultiTickerSignalEngine(params=params)
    MIN_CONVICTION = 0.20

    # Find last date in data for forced exit
    last_date = max(t.timestamp.date() for t in ticks) if ticks else None

    def trader_fn(tick: Tick, portfolio: Portfolio) -> TraderDecision:
        report = engine.process(tick)
        is_last_day = last_date and tick.timestamp.date() == last_date
        pos = portfolio.positions.get(tick.ticker)

        # Exit held positions
        if pos:
            # Forced exit on last day (close all positions)
            if is_last_day:
                return TraderDecision(
                    ticker=tick.ticker, decision="SELL",
                    conviction=report.conviction,
                    rationale=f"EOD close at {tick.close:.2f}",
                    shares=pos.shares, signal_override=True)

            # SL/TP exits
            if tick.close <= report.stop_loss:
                return TraderDecision(
                    ticker=tick.ticker, decision="SELL",
                    conviction=report.conviction,
                    rationale=f"SL at {tick.close:.2f}",
                    shares=pos.shares, signal_override=True)
            if tick.close >= report.take_profit:
                return TraderDecision(
                    ticker=tick.ticker, decision="SELL",
                    conviction=report.conviction,
                    rationale=f"TP at {tick.close:.2f}",
                    shares=pos.shares, signal_override=True)
            return TraderDecision(
                ticker=tick.ticker, decision="HOLD", conviction=0.0,
                rationale="Held")

        # Don't enter on last day (would close immediately for 0 P&L)
        if is_last_day:
            return TraderDecision(
                ticker=tick.ticker, decision="HOLD", conviction=0.0,
                rationale="Last day, no entry")

        # Entry on BULLISH momentum
        if (report.momentum_signal == "BULLISH"
                and report.conviction >= MIN_CONVICTION
                and portfolio.position_count < report.max_positions):
            return TraderDecision(
                ticker=tick.ticker, decision="BUY",
                conviction=report.conviction,
                rationale=f"Bullish: mom={report.momentum_score:.2f} "
                          f"rsi={report.rsi:.1f} reg={report.regime}",
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
    from dataclasses import fields as dc_fields
    field_names = [f.name for f in dc_fields(SignalParams) if f.name != "_BOUNDS"]

    variants: List[Tuple[str, str, SignalParams]] = [
        ("baseline", "Baseline nightly parameters", baseline_params),
    ]
    for template in _VARIANT_TEMPLATES:
        vp = SignalParams(**{name: getattr(baseline_params, name) for name in field_names})
        for param_name, multiplier in template["param_changes"].items():
            if hasattr(vp, param_name) and param_name in field_names:
                current = getattr(vp, param_name)
                b = SignalParams.bound(param_name)
                new_val = int(round(current * multiplier)) if b.is_int else current * multiplier
                setattr(vp, param_name, new_val)
        variants.append((template["name"], template["description"], vp))
    return variants


# ==============================================================================
# Scoring
# ==============================================================================

def score_variant(params: SignalParams, ticks: List[Tick]) -> Tuple[float, Any]:
    harness = ReplayHarness(
        initial_balance=100_000.0,
        max_position_pct=params.base_size_pct,
        require_conviction=0.20,
        cost_model=CostModel.default(),
    )
    trader_fn = make_signal_trader(params, ticks)
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
              f"{r.win_rate:<10.1%} {r.n_trades:<7} ${r.total_pnl:<9.2f}{flag}")
    best = sorted_results[0] if sorted_results else None
    if best and best.score > baseline_score + 0.05:
        print(f"\n  WINNER: {best.variant_name} (score: {best.score:.4f} vs baseline: {baseline_score:.4f})")
    else:
        if best:
            print(f"\n  Best: {best.variant_name} ({best.score:.4f}) vs baseline ({baseline_score:.4f})")
    print(f"{'='*80}\n")


# ==============================================================================
# Main
# ==============================================================================

def run_nightly_replay(date_str: str, dry_run: bool = True, lookback_days: int = 5) -> List[VariantResult]:
    t0 = time.time()

    # 1. Load data
    print(f"[nightly_replay] Loading Postgres {date_str} ({lookback_days}d lookback)...")
    ticks = load_ticks_for_date(date_str, lookback_days=lookback_days)
    print(f"[nightly_replay] Loaded {len(ticks)} ticks")

    if not ticks:
        print("[nightly_replay] No ticks")
        return []

    # 2. Baseline params
    baseline_params = _make_params(_NIGHTLY_PARAMS)
    print(f"[nightly_replay] Baseline: SL={baseline_params.stop_loss_pct:.0%} "
          f"TP={baseline_params.take_profit_pct:.0%} "
          f"mom_lk={baseline_params.momentum_lookback} "
          f"mom_thr={baseline_params.momentum_threshold:.4f}")

    # 3. Variants
    variants = generate_variant_params(baseline_params)
    print(f"[nightly_replay] {len(variants)} variants (baseline + {len(variants) - 1})")

    # 4. Score
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
              f"score={score:.4f} trades={len(result.trades):<4} "
              f"P&L=${result.total_pnl:<8.2f} ({vt1 - vt0:.1f}s)")

    # 5. Leaderboard
    print_leaderboard(results, results[0].score, len(ticks), time.time() - t0)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Nightly Replay")
    parser.add_argument("--date", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--lookback", type=int, default=5)
    parser.add_argument("--variants", type=int, default=8)
    args = parser.parse_args()
    date_str = args.date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
    print(f"[nightly_replay] Date: {date_str} | {'DRY RUN' if args.dry_run else 'FULL'}")
    run_nightly_replay(date_str=date_str, dry_run=args.dry_run, lookback_days=args.lookback)


if __name__ == "__main__":
    main()