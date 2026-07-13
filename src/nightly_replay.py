#!/usr/bin/env python3
"""
Nightly Replay — after-market-close grading of all virtual trader variants.

Runs ~18:00 ET (after market close), triggered by cron.
  1. Load today's market data for all monitored tickers
  2. Replay through EVERY active virtual trader variant
  3. Grade each variant's performance (objective_score, Calmar, Sortino, etc.)
  4. Write results to trading.sweep_runs and trading.sweep_results
  5. Calls publish_sweep_results() so culling reads sweep data next Sunday

This bridges prompt_sweep winning variants → virtual_traders config,
and supplies sweep results for virtual_cull to use instead of random.

Usage:
    python3 src/nightly_replay.py                    # full run, all traders
    python3 src/nightly_replay.py --date 2026-07-13  # specific date
    python3 src/nightly_replay.py --trader kairos     # single trader
    python3 src/nightly_replay.py --dry-run           # score only, no DB writes

Pipeline flow:
    prompt_sweep → publish_sweep_results → nightly_replay → virtual_cull
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.metrics import objective_score, compute_calmar, compute_sortino, compute_profit_factor
from src.replay import ReplayHarness, Tick, Portfolio, TraderDecision, ReplayResult
from src.signals import SignalEngine, SignalParams

log = logging.getLogger("nightly_replay")

# ═══════════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════════

DB_DSN = os.getenv("VT_DB_DSN", "host=docker.klo port=5433 dbname=trading user=trader")
BASE_TRADERS = ["kairos", "aldridge", "stonks"]

DEFAULT_TICKERS = ["SPY", "AAPL", "MSFT", "NVDA", "TSLA", "META", "GOOGL", "AMZN"]

# ── Dot-notation to flat param name mapping (mirrors virtual_cull.py) ─────────
_CONFIG_KEY_MAP: Dict[str, str] = {
    "signal_params.momentum.threshold": "momentum_threshold",
    "signal_params.momentum.lookback": "momentum_lookback",
    "signal_params.momentum.decay": "momentum_decay",
    "signal_params.mean_reversion.rsi_oversold": "rsi_oversold",
    "signal_params.mean_reversion.rsi_overbought": "rsi_overbought",
    "signal_params.mean_reversion.bollinger_std": "bollinger_std",
    "signal_params.volume.threshold": "volume_threshold",
    "signal_params.volatility.regime_threshold": "vol_regime_threshold",
    "signal_params.volatility.reduction_multiplier": "vol_reduction_multiplier",
    "signal_params.position_sizing.base_size_pct": "base_size_pct",
    "signal_params.position_sizing.conviction_multiplier": "conviction_multiplier",
    "signal_params.position_sizing.max_positions": "max_positions",
    "signal_params.risk.stop_loss_pct": "stop_loss_pct",
    "signal_params.risk.take_profit_pct": "take_profit_pct",
    "signal_params.risk.trailing_stop_pct": "trailing_stop_pct",
    "signal_params.regime_weights.trending_up": "weight_trending_up",
    "signal_params.regime_weights.trending_down": "weight_trending_down",
    "signal_params.regime_weights.mean_reverting": "weight_mean_reverting",
    "signal_params.regime_weights.high_volatility": "weight_high_volatility",
}

# Reverse map for writing params back to dot notation
_FLAT_TO_DOT = {v: k for k, v in _CONFIG_KEY_MAP.items()}


# ═══════════════════════════════════════════════════════════════════════════════
# Database helpers
# ═══════════════════════════════════════════════════════════════════════════════

def get_db():
    """Return a psycopg2 connection with autocommit."""
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    return conn


# ═══════════════════════════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════════════════════════

def load_ticks_for_date(
    date_str: str,
    tickers: Optional[List[str]] = None,
) -> List[Tick]:
    """Load tick/bar data for a specific trading date from market_data.bars.

    Tries the market_data.bars table first, then falls back to synthetic data.

    Args:
        date_str: ISO date string.
        tickers: Tickers to load.

    Returns:
        List of Tick objects sorted by timestamp.
    """
    if tickers is None:
        tickers = list(DEFAULT_TICKERS)

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    all_ticks: List[Tick] = []

    try:
        # Load 5-min bars from market_data.bars for the date
        placeholders = ",".join(["%s"] * len(tickers))
        cur.execute(
            f"""SELECT timestamp, ticker, open, high, low, close, volume
                FROM market_data.bars
                WHERE ticker IN ({placeholders})
                  AND timestamp::date = %s::date
                ORDER BY timestamp, ticker""",
            (*tickers, date_str),
        )
        rows = cur.fetchall()

        if rows:
            for r in rows:
                ts = r["timestamp"]
                if isinstance(ts, datetime):
                    ts = ts
                elif isinstance(ts, date):
                    ts = datetime.combine(ts, datetime.min.time())
                else:
                    ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))

                all_ticks.append(Tick(
                    timestamp=ts,
                    ticker=r["ticker"],
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=int(r["volume"]),
                ))

            conn.close()
            all_ticks.sort(key=lambda t: (t.timestamp, t.ticker))
            log.info("  Loaded %d bars from market_data.bars for %s", len(all_ticks), date_str)
            return all_ticks
    except Exception as e:
        log.warning("  market_data.bars query failed: %s — trying bar_loader fallback", e)

    conn.close()

    # Try bar_loader as fallback
    try:
        from src.bar_loader import BarLoader
        loader = BarLoader()
        bars = loader.load_date_range(
            tickers=tickers,
            start_date=date_str,
            end_date=date_str,
        )
        if bars:
            log.info("  Loaded %d ticks via BarLoader for %s", len(bars), date_str)
            bars.sort(key=lambda t: (t.timestamp, t.ticker))
            return bars
    except ImportError:
        pass
    except Exception as e:
        log.warning("  BarLoader failed: %s", e)

    # Last resort: synthetic data
    log.warning("  No real data for %s — generating synthetic ticks", date_str)
    return _generate_synthetic_ticks(date_str, tickers)


def _generate_synthetic_ticks(date_str: str, tickers: List[str]) -> List[Tick]:
    """Generate synthetic tick data as fallback."""
    rng = np.random.default_rng(42)
    ticks: List[Tick] = []

    base_prices: Dict[str, float] = {
        "SPY": 590.0, "AAPL": 225.0, "MSFT": 450.0, "NVDA": 130.0,
        "TSLA": 340.0, "META": 700.0, "GOOGL": 185.0, "AMZN": 225.0,
    }
    base_time = datetime.strptime(f"{date_str}T09:30:00", "%Y-%m-%dT%H:%M:%S")
    n_ticks = 13  # 30-min intervals across 6.5 hours

    for ticker in tickers:
        price = base_prices.get(ticker, 100.0)
        for i in range(n_ticks):
            ts = base_time + timedelta(minutes=30 * i)
            noise = rng.normal(0, 0.005)
            price = price * (1 + noise)
            ticks.append(Tick(
                timestamp=ts,
                ticker=ticker,
                open=round(price * 0.999, 2),
                high=round(price * 1.005, 2),
                low=round(price * 0.995, 2),
                close=round(price, 2),
                volume=rng.integers(100_000, 5_000_000),
            ))

    ticks.sort(key=lambda t: (t.timestamp, t.ticker))
    return ticks


# ═══════════════════════════════════════════════════════════════════════════════
# Config normalization (mirrors virtual_cull.py)
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_config(raw_config: Dict[str, Any]) -> Dict[str, float]:
    """Convert a stored config (dot-notation or flat names) to flat param dict."""
    result: Dict[str, float] = {}
    for key, value in raw_config.items():
        try:
            fval = float(value)
        except (TypeError, ValueError):
            continue

        if key in SignalParams.param_names():
            result[key] = fval
        elif key in _CONFIG_KEY_MAP:
            result[_CONFIG_KEY_MAP[key]] = fval

    return result


def config_to_signal_params(raw_config: Dict[str, Any]) -> SignalParams:
    """Convert a stored config dict to a SignalParams instance.

    Handles both dot-notation and flat-param formats.
    Unspecified params use SignalParams defaults.
    """
    flat = normalize_config(raw_config)
    params = SignalParams()

    for name, value in flat.items():
        b = SignalParams.bound(name)
        params.set(name, b.clip(value))

    return params


# ═══════════════════════════════════════════════════════════════════════════════
# Signal-based trader function
# ═══════════════════════════════════════════════════════════════════════════════

def make_signal_trader(params: SignalParams):
    """Create a trader function using SignalEngine with given params."""
    engine = SignalEngine(params=params)

    def trader_fn(tick: Tick, portfolio: Portfolio) -> TraderDecision:
        report = engine.process(tick)

        # Already holding? Check stops
        if tick.ticker in portfolio.positions:
            pos = portfolio.positions[tick.ticker]
            if tick.close <= report.stop_loss:
                return TraderDecision(
                    ticker=tick.ticker,
                    decision="SELL",
                    conviction=report.conviction,
                    rationale=f"Stop loss hit at {tick.close:.2f}",
                    shares=pos.shares,
                    signal_override=True,
                )
            if tick.close >= report.take_profit:
                return TraderDecision(
                    ticker=tick.ticker,
                    decision="SELL",
                    conviction=report.conviction,
                    rationale=f"Take profit at {tick.close:.2f}",
                    shares=pos.shares,
                    signal_override=True,
                )
            return TraderDecision(
                ticker=tick.ticker,
                decision="HOLD",
                conviction=0.0,
                rationale="Position held",
            )

        # Entry: only on bullish signals with sufficient conviction
        if (report.momentum_signal == "BULLISH"
                and report.conviction >= 0.4
                and portfolio.position_count < report.max_positions):
            return TraderDecision(
                ticker=tick.ticker,
                decision="BUY",
                conviction=report.conviction,
                rationale=(f"Bullish signal: momentum={report.momentum_score:.2f}, "
                           f"RSI={report.rsi:.1f}, regime={report.regime}"),
                shares=0,
            )

        return TraderDecision(
            ticker=tick.ticker,
            decision="HOLD",
            conviction=0.0,
            rationale="No signal",
        )

    return trader_fn


# ═══════════════════════════════════════════════════════════════════════════════
# Virtual trader operations
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class VirtualTrader:
    """A virtual trader record from the database."""
    id: int
    name: str
    base_trader: str
    variant_type: str
    config: Dict[str, Any]
    status: str


@dataclass
class ReplayGrade:
    """Grading results for one virtual trader variant."""
    trader_id: int
    name: str
    base_trader: str
    variant_type: str
    objective_score: float
    calmar: float
    sortino: float
    profit_factor: float
    total_pnl: float
    total_return_pct: float
    max_drawdown: float
    n_trades: int
    win_rate: float
    params_hash: str
    signal_params_json: str  # JSON dump of flat params used
    n_ticks: int
    elapsed_s: float


def get_active_virtuals() -> List[VirtualTrader]:
    """Fetch all active virtual traders from the DB."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT id, name, base_trader, variant_type, config, status
           FROM trading.virtual_traders
           WHERE status IN ('active', 'probation')
           ORDER BY base_trader, name""",
    )
    rows = cur.fetchall()
    conn.close()

    return [
        VirtualTrader(
            id=r["id"],
            name=r["name"],
            base_trader=r["base_trader"],
            variant_type=r["variant_type"],
            config=r.get("config", {}),
            status=r["status"],
        )
        for r in rows
    ]


def compute_params_hash(params: SignalParams) -> str:
    """Compute a hash of the signal params for deduplication."""
    flat = {name: params.get(name) for name in params.param_names()}
    raw = json.dumps(flat, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _grade_variant(
    trader: VirtualTrader,
    ticks: List[Tick],
    start_time: datetime,
) -> ReplayGrade:
    """Replay one virtual trader through market data and compute grades.

    Args:
        trader: The virtual trader to grade.
        ticks: Market data ticks.
        start_time: When grading started (for elapsed_s).

    Returns:
        ReplayGrade with all metrics.
    """
    params = config_to_signal_params(trader.config)
    trader_fn = make_signal_trader(params)

    harness = ReplayHarness(
        initial_balance=100_000.0,
        max_position_pct=params.base_size_pct,
        require_conviction=0.3,
    )

    result = harness.run(ticks, trader_fn)

    score = float(objective_score(result.returns, result.equity_curve, result.trade_pnls))
    calmar = float(compute_calmar(result.returns, result.equity_curve))
    sortino = float(compute_sortino(result.returns))
    profit_factor = float(compute_profit_factor(result.trade_pnls))

    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()

    # Build params JSON for storage
    flat_params = {name: params.get(name) for name in params.param_names()}
    signal_params_json = json.dumps(flat_params)

    params_hex = compute_params_hash(params)

    return ReplayGrade(
        trader_id=trader.id,
        name=trader.name,
        base_trader=trader.base_trader,
        variant_type=trader.variant_type,
        objective_score=score,
        calmar=calmar,
        sortino=sortino,
        profit_factor=profit_factor,
        total_pnl=float(result.total_pnl),
        total_return_pct=float(result.total_return_pct),
        max_drawdown=float(np.max([
            (max(result.equity_curve[:i+1]) - result.equity_curve[i]) / max(result.equity_curve[:i+1])
            for i in range(1, len(result.equity_curve))
        ])) if len(result.equity_curve) > 1 else 0.0,
        n_trades=len(result.trades),
        win_rate=result.win_rate,
        params_hash=params_hex,
        signal_params_json=signal_params_json,
        n_ticks=len(ticks),
        elapsed_s=elapsed,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# DB write operations
# ═══════════════════════════════════════════════════════════════════════════════

def _create_sweep_run(
    cur: psycopg2.extras.RealDictCursor,
    trader_short: str,
    n_scenarios: int,
    date_str: str,
) -> int:
    """Create a sweep run record, return its id."""
    cur.execute(
        """INSERT INTO trading.sweep_runs
           (trader_id, started_at, n_scenarios, n_completed, n_failed)
           VALUES (%s, NOW(), %s, 0, 0)
           RETURNING id""",
        (trader_short, n_scenarios),
    )
    return cur.fetchone()["id"]


def _insert_sweep_result(
    cur: psycopg2.extras.RealDictCursor,
    run_id: int,
    grade: ReplayGrade,
    variant_id: int,
    date_str: str,
):
    """Insert one graded result into sweep_results."""
    validation_meta = json.dumps({
        "variant_name": grade.name,
        "variant_type": grade.variant_type,
        "trader_id": grade.trader_id,
        "signal_params_json": grade.signal_params_json,
        "variant_score": grade.objective_score,
        "val_date_range": f"{date_str}:{date_str}",
        "n_ticks": grade.n_ticks,
        "variant_description": (
            f"Nightly replay of {grade.name} "
            f"(type={grade.variant_type}, trader_id={grade.trader_id})"
        ),
        "signal_llm_divergence": False,
    })

    cur.execute(
        """INSERT INTO trading.sweep_results
           (run_id, trader_id, variant_id, params_hash,
            objective_score, calmar, sortino, profit_factor,
            total_pnl, total_return_pct, max_drawdown,
            n_ticks, n_trades, win_rate, elapsed_s,
            model_used, journal_sample, validation_meta)
           VALUES (%s, %s, %s, %s,
                   %s, %s, %s, %s,
                   %s, %s, %s,
                   %s, %s, %s, %s,
                   %s, %s, %s::jsonb)
           ON CONFLICT (run_id, variant_id)
           DO UPDATE SET
               objective_score = EXCLUDED.objective_score,
               calmar = EXCLUDED.calmar,
               profit_factor = EXCLUDED.profit_factor,
               total_pnl = EXCLUDED.total_pnl,
               total_return_pct = EXCLUDED.total_return_pct,
               win_rate = EXCLUDED.win_rate,
               elapsed_s = EXCLUDED.elapsed_s""",
        (run_id, grade.base_trader, variant_id, grade.params_hash,
         grade.objective_score, grade.calmar, grade.sortino, grade.profit_factor,
         grade.total_pnl, grade.total_return_pct, grade.max_drawdown,
         grade.n_ticks, grade.n_trades, grade.win_rate, grade.elapsed_s,
         "signal", "", validation_meta),
    )


def _finalize_sweep_run(
    cur: psycopg2.extras.RealDictCursor,
    run_id: int,
    grades: List[ReplayGrade],
):
    """Update sweep_run with best variant info."""
    if not grades:
        cur.execute(
            "UPDATE trading.sweep_runs SET finished_at = NOW(), n_completed = 0 WHERE id = %s",
            (run_id,),
        )
        return

    best = max(grades, key=lambda g: g.objective_score)
    worst_count = sum(1 for g in grades if g.objective_score <= 0)

    cur.execute(
        """UPDATE trading.sweep_runs
           SET finished_at = NOW(),
               n_completed = %s,
               n_failed = %s,
               best_score = %s,
               best_variant_id = %s,
               best_params_hash = %s
           WHERE id = %s""",
        (len(grades), worst_count,
         best.objective_score, grades.index(best) + 1, best.params_hash,
         run_id),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Publish sweep results to virtual_traders (imported pattern from virtual_cull)
# ═══════════════════════════════════════════════════════════════════════════════

def publish_sweep_results(
    trader_type: str,
    best_params: Dict[str, float],
    score: float,
    run_id: Optional[int] = None,
) -> int:
    """Publish sweep best params to active virtual traders config.

    This is the bridge from nightly_replay → virtual_traders config.
    Mirrors the function in virtual_cull.py for import convenience.

    Args:
        trader_type: Base trader name.
        best_params: Flat SignalParams dict of the winner.
        score: Winner's objective score.
        run_id: Sweep run ID for reference.

    Returns:
        Number of virtual traders updated.
    """
    # Delegate to virtual_cull's implementation (keeps DB logic in one place)
    from src.virtual_cull import publish_sweep_results as _publish
    return _publish(trader_type, best_params, score, run_id)


# ═══════════════════════════════════════════════════════════════════════════════
# Main replay pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def run_nightly_replay(
    date_str: Optional[str] = None,
    trader: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run the full nightly replay pipeline.

    After market close:
    1. Replay today's market data through ALL active virtual trader variants
    2. Grade each variant's performance
    3. Write results to sweep_results table
    4. Culling reads these results instead of random (next Sunday)

    Args:
        date_str: Date to replay (YYYY-MM-DD). Default: today.
        trader: Single base trader to grade. Default: all.
        dry_run: Score only, no DB writes.

    Returns:
        Summary dict with results per base trader.
    """
    if date_str is None:
        date_str = date.today().isoformat()

    log.info("═" * 60)
    log.info("Nightly Replay — %s", "DRY RUN" if dry_run else "LIVE")
    log.info("Date: %s | Trader filter: %s", date_str, trader or "all")

    # ── Step 1: Load market data ──
    log.info("Loading market data for %s...", date_str)
    ticks = load_ticks_for_date(date_str)
    if not ticks:
        log.error("No market data available for %s — aborting", date_str)
        return {"status": "error", "reason": f"No data for {date_str}"}

    log.info("Loaded %d ticks for %s", len(ticks), date_str)

    # ── Step 2: Get active virtual traders ──
    virtuals = get_active_virtuals()
    if trader:
        virtuals = [v for v in virtuals if v.base_trader == trader]

    if not virtuals:
        log.warning("No active virtual traders found — nothing to grade")
        return {"status": "empty", "total_graded": 0}

    log.info("Grading %d virtual traders...", len(virtuals))

    # ── Step 3: Grade each variant ──
    start_time = datetime.now(timezone.utc)
    grades_by_trader: Dict[str, List[ReplayGrade]] = defaultdict(list)

    for vt in virtuals:
        try:
            grade = _grade_variant(vt, ticks, start_time)
            grades_by_trader[vt.base_trader].append(grade)
            log.info(
                "  %-30s score=%.4f  pnl=$%+.2f  trades=%d  wr=%.0f%%",
                vt.name, grade.objective_score, grade.total_pnl,
                grade.n_trades, grade.win_rate * 100,
            )
        except Exception as e:
            log.error("  Failed to grade %s: %s", vt.name, e, exc_info=True)

    # ── Step 4: Write results to DB ──
    summary: Dict[str, Any] = {
        "status": "ok",
        "date": date_str,
        "total_graded": sum(len(grades) for grades in grades_by_trader.values()),
        "traders": {},
    }

    if dry_run:
        log.info("DRY RUN — skipping DB writes")
        for bt, grades in grades_by_trader.items():
            best = max(grades, key=lambda g: g.objective_score)
            summary["traders"][bt] = {
                "n_graded": len(grades),
                "best_variant": best.name,
                "best_score": best.objective_score,
            }
        return summary

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        for bt, grades in grades_by_trader.items():
            if not grades:
                continue

            # Create sweep run
            run_id = _create_sweep_run(cur, bt, len(grades), date_str)
            log.info("Created sweep run %d for %s (%d variants)", run_id, bt, len(grades))

            # Insert each graded result
            for i, grade in enumerate(grades):
                _insert_sweep_result(cur, run_id, grade, i + 1, date_str)

            # Finalize the sweep run with best variant info
            _finalize_sweep_run(cur, run_id, grades)

            # Find best variant and publish to config
            best = max(grades, key=lambda g: g.objective_score)
            best_params = json.loads(best.signal_params_json)

            updated_count = publish_sweep_results(
                bt, best_params, best.objective_score, run_id,
            )

            summary["traders"][bt] = {
                "run_id": run_id,
                "n_graded": len(grades),
                "best_variant": best.name,
                "best_score": best.objective_score,
                "virtuals_updated": updated_count,
            }

            log.info(
                "  %s: best=%s (score=%.4f), published to %d virtuals",
                bt, best.name, best.objective_score, updated_count,
            )

    except Exception as e:
        log.error("DB write failed: %s", e, exc_info=True)
        summary["status"] = "error"
        summary["error"] = str(e)
    finally:
        conn.close()

    return summary


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def print_summary(summary: Dict[str, Any]):
    """Print a human-readable summary."""
    print()
    print("═" * 72)
    print(f"  NIGHTLY REPLAY — {summary.get('date', '?')}")
    print("═" * 72)

    if summary["status"] == "error":
        print(f"\n  ❌ ERROR: {summary.get('reason', summary.get('error', 'unknown'))}")
        return

    if summary["status"] == "empty":
        print("\n  ⚠️  No active virtual traders found — nothing to grade")
        return

    print(f"\n  Total graded: {summary['total_graded']} variants")
    print()

    for bt, info in summary.get("traders", {}).items():
        print(f"  📊 {bt.upper()}")
        print(f"     Graded:     {info['n_graded']} variants")
        print(f"     Best:       {info['best_variant']}")
        print(f"     Score:      {info['best_score']:.4f}")
        if "virtuals_updated" in info:
            print(f"     Published:  {info['virtuals_updated']} virtuals")
        if "run_id" in info:
            print(f"     Run ID:     {info['run_id']}")
        print()

    print(f"{'═' * 72}")
    if summary["status"] == "ok":
        print("  ✅ Nightly replay complete — results written to sweep_results")
    else:
        print(f"  ⚠️  Status: {summary['status']}")
    print(f"{'═' * 72}\n")


def main():
    global DB_DSN

    parser = argparse.ArgumentParser(
        description="Nightly Replay — grade all virtual traders on today's market data"
    )
    parser.add_argument("--date", type=str, default=None,
                        help="Date to replay (YYYY-MM-DD). Default: today.")
    parser.add_argument("--trader", type=str, default=None,
                        help="Single base trader (kairos/aldridge/stonks). Default: all.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Score only, skip DB writes.")
    parser.add_argument("--db-dsn", type=str, default=DB_DSN,
                        help="Postgres connection string")
    args = parser.parse_args()

    DB_DSN = args.db_dsn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    summary = run_nightly_replay(
        date_str=args.date,
        trader=args.trader,
        dry_run=args.dry_run,
    )

    print_summary(summary)

    if summary["status"] != "ok":
        sys.exit(1)


if __name__ == "__main__":
    main()