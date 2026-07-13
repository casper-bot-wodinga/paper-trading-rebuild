#!/usr/bin/env python3
"""
historical_sim — Historical simulation using data bus API.

Pulls historical bar data through the running data bus API (192.168.1.41:5000)
instead of loading parquet files directly. Runs replay-based simulations
similar to src/simulator.py but optimized for CLI parameter sweeps.

Usage:
    python3 -m src.historical_sim sweep --trader kairos --ticker AAPL --days 5
    python3 -m src.historical_sim backtest --trader kairos --ticker AAPL --days 30
    python3 -m src.historical_sim findings --trader kairos
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import numpy as np

# ── Path setup ────────────────────────────────────────────────────────────────
SRC_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SRC_DIR.parent
for d in [str(SRC_DIR), str(PROJECT_DIR)]:
    if d not in sys.path:
        sys.path.insert(0, d)

log = logging.getLogger("historical_sim")

# ── Data bus URL ──────────────────────────────────────────────────────────────
DATA_BUS_URL = os.getenv("DATA_BUS_URL", "http://192.168.1.41:5000")


# ── Data fetching via data bus ────────────────────────────────────────────────

def fetch_bars_from_databus(
    tickers: List[str],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    interval: str = "daily",
) -> Dict[str, List[dict]]:
    """Fetch historical OHLCV bars from the data bus API.

    Args:
        tickers: List of ticker symbols.
        start_date: ISO date filter (e.g. "2026-06-01").
        end_date: ISO date filter (e.g. "2026-07-02").
        interval: "daily" or "intraday".

    Returns:
        Dictionary mapping ticker -> list of OHLCV dicts.
    """
    params = {
        "symbols": ",".join(tickers),
        "interval": interval,
    }
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date

    try:
        resp = requests.get(f"{DATA_BUS_URL}/bars", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("symbols", {})
    except requests.RequestException as e:
        log.warning("Data bus /bars request failed: %s", e)
        return {}
    except json.JSONDecodeError as e:
        log.warning("Data bus /bars returned invalid JSON: %s", e)
        return {}


def fetch_quotes_from_databus(tickers: List[str]) -> Dict[str, dict]:
    """Fetch latest quote data from the data bus API.

    Args:
        tickers: List of ticker symbols.

    Returns:
        Dictionary mapping ticker -> quote dict with OHLCV + indicators.
    """
    params = {"symbols": ",".join(tickers)}
    try:
        resp = requests.get(f"{DATA_BUS_URL}/quotes", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("quotes", {})
    except requests.RequestException as e:
        log.warning("Data bus /quotes request failed: %s", e)
        return {}
    except json.JSONDecodeError as e:
        log.warning("Data bus /quotes returned invalid JSON: %s", e)
        return {}


# ── Backtest engine ───────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    """Result of a single backtest run."""
    ticker: str
    trader: str
    start_date: str
    end_date: str
    n_bars: int
    n_trades: int
    total_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    win_rate: float
    profit_factor: float
    final_equity: float
    params: Dict[str, Any] = field(default_factory=dict)


def compute_rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
    """Compute RSI indicator."""
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.zeros_like(prices)
    avg_loss = np.zeros_like(prices)
    avg_gain[period] = np.mean(gains[:period])
    avg_loss[period] = np.mean(losses[:period])
    for i in range(period + 1, len(prices)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i - 1]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i - 1]) / period
    rs = np.divide(avg_gain, avg_loss, out=np.zeros_like(avg_gain), where=avg_loss != 0)
    rsi = 100 - (100 / (1 + rs))
    rsi[:period] = 50.0
    return rsi


def run_backtest(
    bars: List[dict],
    ticker: str,
    trader: str,
    params: Dict[str, Any],
    initial_cash: float = 100_000.0,
) -> BacktestResult:
    """Run a simple backtest on bar data.

    Uses a momentum/RSI strategy that varies by trader type.
    """
    if len(bars) < 20:
        return BacktestResult(
            ticker=ticker, trader=trader,
            start_date=bars[0]["timestamp"][:10] if bars else "",
            end_date=bars[-1]["timestamp"][:10] if bars else "",
            n_bars=len(bars), n_trades=0,
            total_return_pct=0.0, sharpe=0.0, max_drawdown_pct=0.0,
            win_rate=0.0, profit_factor=0.0, final_equity=initial_cash,
            params=params,
        )

    closes = np.array([b["close"] for b in bars], dtype=np.float64)
    rsi = compute_rsi(closes)

    # Trader-specific thresholds
    if trader == "kairos":
        rsi_overbought = params.get("rsi_overbought", 65)
        rsi_oversold = params.get("rsi_oversold", 35)
        trailing_stop_pct = params.get("trailing_stop_pct", 7.0)
        max_pos_pct = params.get("max_position_pct", 20.0)
        conviction = params.get("conviction", 0.63)
    elif trader == "stonks":
        rsi_overbought = params.get("rsi_overbought", 70)
        rsi_oversold = params.get("rsi_oversold", 30)
        volume_mult = params.get("volume_multiplier", 1.5)
        trailing_stop_pct = params.get("stop_loss_pct", 8.0)
        max_pos_pct = params.get("max_position_pct", 25.0)
        conviction = params.get("conviction", 0.6)
    elif trader == "aldridge":
        rsi_overbought = 80
        rsi_oversold = params.get("rsi_oversold", 40)
        trailing_stop_pct = params.get("stop_loss_pct", 8.0)
        max_pos_pct = params.get("max_position_pct", 25.0)
    else:
        rsi_overbought = 70
        rsi_oversold = 30
        trailing_stop_pct = 8.0
        max_pos_pct = 20.0

    # Run the backtest
    cash = initial_cash
    position = 0  # shares held
    entry_price = 0.0
    entry_bar = 0
    trades = []
    equity_curve = [initial_cash]
    high_water_mark = initial_cash

    for i in range(20, len(bars)):
        bar = bars[i]
        price = bar["close"]
        rsi_val = rsi[i]
        equity = cash + position * price
        high_water_mark = max(high_water_mark, equity)

        # Update trailing stop if in position
        if position > 0:
            mkt_val = position * price

            # Check trailing stop
            if trailing_stop_pct > 0:
                stop_price = entry_price * (1 - trailing_stop_pct / 100)
                if price <= stop_price:
                    proceeds = position * price
                    pnl = proceeds - (position * entry_price)
                    trades.append({
                        "entry_bar": entry_bar, "exit_bar": i,
                        "entry_price": entry_price, "exit_price": price,
                        "shares": position, "pnl": pnl,
                        "return_pct": (price - entry_price) / entry_price * 100,
                    })
                    cash += proceeds
                    position = 0

        # Entry signals
        if position == 0:
            # Buy signal: RSI oversold (oversold for aldridge)
            if (trader == "aldridge" and rsi_val <= rsi_oversold and rsi_val > 0) or \
               (trader != "aldridge" and rsi_val <= rsi_oversold and rsi_val > 0):
                max_cost = equity * (max_pos_pct / 100)
                shares = int(max_cost / price)
                if shares > 0 and cash >= shares * price:
                    position = shares
                    entry_price = price
                    entry_bar = i
                    cash -= shares * price
            elif trader == "kairos" and rsi_val >= rsi_overbought and rsi_val < 100:
                # Kairos enters on momentum (RSI overbought)
                max_cost = equity * (max_pos_pct / 100) * conviction
                shares = int(max_cost / price)
                if shares > 0 and cash >= shares * price:
                    position = shares
                    entry_price = price
                    entry_bar = i
                    cash -= shares * price

        # Exit signals (take profit or RSI overbought exit for non-kairos)
        elif position > 0:
            take_profit_pct = params.get("take_profit_pct", 15.0)
            if trader != "kairos" and rsi_val >= rsi_overbought and rsi_val < 100:
                proceeds = position * price
                pnl = proceeds - (position * entry_price)
                trades.append({
                    "entry_bar": entry_bar, "exit_bar": i,
                    "entry_price": entry_price, "exit_price": price,
                    "shares": position, "pnl": pnl,
                    "return_pct": (price - entry_price) / entry_price * 100,
                })
                cash += proceeds
                position = 0
            elif take_profit_pct > 0 and price >= entry_price * (1 + take_profit_pct / 100):
                proceeds = position * price
                pnl = proceeds - (position * entry_price)
                trades.append({
                    "entry_bar": entry_bar, "exit_bar": i,
                    "entry_price": entry_price, "exit_price": price,
                    "shares": position, "pnl": pnl,
                    "return_pct": (price - entry_price) / entry_price * 100,
                })
                cash += proceeds
                position = 0

        # Record equity
        equity_val = cash + position * price
        equity_curve.append(equity_val)

    # Close any open position at last price
    if position > 0:
        last_price = bars[-1]["close"]
        proceeds = position * last_price
        pnl = proceeds - (position * entry_price)
        trades.append({
            "entry_bar": entry_bar,
            "exit_bar": len(bars) - 1,
            "entry_price": entry_price,
            "exit_price": last_price,
            "shares": position,
            "pnl": pnl,
            "return_pct": (last_price - entry_price) / entry_price * 100,
        })
        cash += proceeds
        position = 0
        equity_curve[-1] = cash

    final_equity = equity_curve[-1]
    total_return_pct = (final_equity - initial_cash) / initial_cash * 100

    # Compute Sharpe ratio from daily returns
    equity_arr = np.array(equity_curve, dtype=np.float64)
    daily_returns = np.diff(equity_arr) / equity_arr[:-1]
    sharpe = 0.0
    if len(daily_returns) > 1 and np.std(daily_returns) > 0:
        sharpe = np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(252)

    # Max drawdown
    if len(equity_arr) > 0:
        peak = np.maximum.accumulate(equity_arr)
        drawdown = (equity_arr - peak) / peak * 100
        max_drawdown_pct = float(np.min(drawdown))
    else:
        max_drawdown_pct = 0.0

    # Win rate and profit factor
    n_trades = len(trades)
    if n_trades > 0:
        wins = sum(1 for t in trades if t["pnl"] > 0)
        win_rate = wins / n_trades
        gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    else:
        win_rate = 0.0
        profit_factor = 0.0

    return BacktestResult(
        ticker=ticker,
        trader=trader,
        start_date=bars[0]["timestamp"][:10] if bars else "",
        end_date=bars[-1]["timestamp"][:10] if bars else "",
        n_bars=len(bars),
        n_trades=n_trades,
        total_return_pct=round(total_return_pct, 2),
        sharpe=round(sharpe, 4),
        max_drawdown_pct=round(max_drawdown_pct, 2),
        win_rate=round(win_rate, 4),
        profit_factor=round(profit_factor, 2),
        final_equity=round(final_equity, 2),
        params=params,
    )


# ── CLI Commands ──────────────────────────────────────────────────────────────

def cmd_sweep(args):
    """Run parameter sweep across multiple tickers."""
    trader = args.trader
    tickers_str = args.ticker or "AAPL,MSFT,SPY"
    tickers = [t.strip().upper() for t in tickers_str.split(",")]
    days = args.days
    variants = args.variants or 3
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    log.info("Sweep: trader=%s tickers=%s days=%d variants=%d",
             trader, tickers, days, variants)
    log.info("  Data range: %s to %s", start_date, end_date)
    log.info("  Data source: %s/bars", DATA_BUS_URL)

    # Fetch data from data bus
    print(f"\n{'='*60}")
    print(f"  📊 HISTORICAL SIM — Data Bus Pull")
    print(f"{'='*60}")
    print(f"  Fetching bars: {tickers}")
    print(f"  Range: {start_date} to {end_date}")
    print(f"  Source: {DATA_BUS_URL}/bars")
    print()

    bar_data = fetch_bars_from_databus(tickers, start_date, end_date, interval="daily")

    found = list(bar_data.keys())
    missing = [t for t in tickers if t not in bar_data]
    total_bars = sum(len(v) for v in bar_data.values())
    print(f"  Found {len(found)}/{len(tickers)} tickers, {total_bars} bars total")
    for t in found:
        print(f"    {t}: {len(bar_data[t])} bars")
    if missing:
        print(f"  Missing (no data): {missing}")
    print()

    if not found:
        log.error("No data returned from data bus for any ticker")
        return 1

    # Generate variant params
    param_sets = _generate_variants(trader, variants)

    results = []
    for ticker in found:
        bars = bar_data[ticker]
        print(f"  Backtesting {ticker} with {variants} param variants...")
        for vid, params in enumerate(param_sets):
            result = run_backtest(bars, ticker, trader, params)
            results.append(result)
            _print_result(result, vid)

    # Best result across all
    if results:
        sorted_results = sorted(results, key=lambda r: r.total_return_pct, reverse=True)
        best = sorted_results[0]
        print(f"\n{'='*60}")
        print(f"  🏆 BEST RESULT: {best.ticker} | {best.trader}")
        print(f"  Return: {best.total_return_pct:+.2f}% | Sharpe: {best.sharpe:.4f}")
        print(f"  Max DD: {best.max_drawdown_pct:.2f}% | Win Rate: {best.win_rate:.1%}")
        print(f"  Trades: {best.n_trades} | Profit Factor: {best.profit_factor:.2f}")
        print(f"  Params: {best.params}")
        print(f"{'='*60}\n")

        # Persist results to shared trader.db
        _persist_sweep_results(results, trader, start_date, end_date)

    return 0


def cmd_backtest(args):
    """Run a single backtest."""
    trader = args.trader
    tickers_str = args.ticker.upper() if args.ticker else "AAPL"
    tickers = [t.strip() for t in tickers_str.split(",")]
    days = args.days or 30

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"  📈 BACKTEST: {trader} on {tickers}")
    print(f"  Range: {start_date} to {end_date}")
    print(f"  Source: {DATA_BUS_URL}/bars")
    print(f"{'='*60}\n")

    all_results = []
    for ticker in tickers:
        bar_data = fetch_bars_from_databus([ticker], start_date, end_date, interval="daily")
        bars = bar_data.get(ticker, [])
        if not bars:
            print(f"  ⚠️ No data returned for {ticker}")
            continue

        print(f"  {ticker}: Loaded {len(bars)} bars")

        # Use default params for trader
        schema = TRADER_PARAM_SCHEMAS.get(trader, TRADER_PARAM_SCHEMAS["kairos"])
        params = {p["name"]: p["default"] for p in schema}
        result = run_backtest(bars, ticker, trader, params)
        _print_result(result, 0)
        all_results.append((ticker, result))

    if not all_results:
        print(f"  ❌ No data returned for any ticker")
        return 1

    return 0


def cmd_findings(args):
    """Display sim findings — latest sweep results from DB."""
    trader = args.trader

    print(f"\n{'='*60}")
    print(f"  🔍 SIM FINDINGS: {trader or 'all traders'}")
    print(f"{'='*60}\n")

    # Read from shared/trader.db
    db_path = PROJECT_DIR / "shared" / "trader.db"
    if not db_path.exists():
        print(f"  No DB found at {db_path}")
        return 0

    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Check for sweep_results table
        tables = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [r["name"] for r in tables]

        if "sweep_results" in table_names:
            query = "SELECT * FROM sweep_results"
            params = []
            conditions = []
            if trader:
                conditions.append("trader = ?")
                params.append(trader)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY timestamp DESC, sharpe DESC LIMIT 30"

            rows = cur.execute(query, params).fetchall()
            if rows:
                print(f"  Found {len(rows)} sweep result(s):\n")
                for r in rows:
                    ts = r['timestamp'][:19] if r['timestamp'] else '?'
                    print(f"  [{ts}] {r['trader']} on {r['ticker']} (variant {r['variant_id']}):")
                    print(f"    Return: {r['total_return_pct']:+.2f}% | Sharpe: {r['sharpe']:.4f} | MaxDD: {r['max_drawdown_pct']:.2f}%")
                    print(f"    Trades: {r['n_trades']} | WR: {r['win_rate']*100:.0f}% | PF: {r['profit_factor']:.2f}")
                    if r['params']:
                        try:
                            bp = json.loads(r['params'])
                            print(f"    Params: {bp}")
                        except (json.JSONDecodeError, TypeError):
                            print(f"    Params: {r['params']}")
                    print()
            else:
                print(f"  No sweep results found{ ' for ' + trader if trader else ''}.")
        else:
            print(f"  No sweep_results table in DB.")
            print(f"  Available tables: {table_names}")

        conn.close()
    except Exception as e:
        print(f"  Error reading DB: {e}")

    return 0


# ── Helpers ───────────────────────────────────────────────────────────────────

TRADER_PARAM_SCHEMAS: Dict[str, List[Dict[str, Any]]] = {
    "kairos": [
        {"name": "rsi_overbought", "default": 65, "min": 55, "max": 80},
        {"name": "rsi_oversold", "default": 35, "min": 20, "max": 45},
        {"name": "trailing_stop_pct", "default": 7.0, "min": 3.0, "max": 15.0},
        {"name": "max_position_pct", "default": 20.0, "min": 5.0, "max": 40.0},
        {"name": "conviction", "default": 0.63, "min": 0.3, "max": 0.95},
    ],
    "stonks": [
        {"name": "rsi_overbought", "default": 70, "min": 60, "max": 85},
        {"name": "rsi_oversold", "default": 30, "min": 15, "max": 45},
        {"name": "volume_multiplier", "default": 1.5, "min": 1.0, "max": 3.0},
        {"name": "stop_loss_pct", "default": 8.0, "min": 3.0, "max": 15.0},
        {"name": "max_position_pct", "default": 25.0, "min": 5.0, "max": 50.0},
        {"name": "conviction", "default": 0.6, "min": 0.3, "max": 0.95},
    ],
    "aldridge": [
        {"name": "rsi_oversold", "default": 40, "min": 20, "max": 55},
        {"name": "pe_max", "default": 20.0, "min": 8.0, "max": 50.0},
        {"name": "stop_loss_pct", "default": 8.0, "min": 3.0, "max": 15.0},
        {"name": "take_profit_pct", "default": 15.0, "min": 5.0, "max": 30.0},
        {"name": "max_position_pct", "default": 25.0, "min": 5.0, "max": 50.0},
    ],
}


def _generate_variants(trader: str, n: int) -> List[Dict[str, Any]]:
    """Generate N parameter variants for sweeps."""
    schema = TRADER_PARAM_SCHEMAS.get(trader, TRADER_PARAM_SCHEMAS["kairos"])
    base = {p["name"]: p["default"] for p in schema}

    if n <= 1:
        return [base]

    variants = [base]
    rng = np.random.default_rng(42 + n)

    for v in range(1, n):
        variant = dict(base)
        for p in schema:
            if "min" in p and "max" in p and p["min"] != p["max"]:
                ptype = type(p["default"])
                if ptype == int:
                    variant[p["name"]] = int(rng.integers(p["min"], p["max"] + 1))
                elif ptype == float:
                    val = rng.uniform(p["min"], p["max"])
                    variant[p["name"]] = round(val, 2)
        variants.append(variant)

    return variants


def _print_result(result: BacktestResult, variant_id: int):
    """Print a single backtest result."""
    ret_color = "+" if result.total_return_pct >= 0 else ""
    print(f"    [{variant_id}] {result.ticker} | "
          f"Return: {ret_color}{result.total_return_pct:+.2f}% | "
          f"Sharpe: {result.sharpe:.4f} | "
          f"MaxDD: {result.max_drawdown_pct:.2f}% | "
          f"WR: {result.win_rate:.1%} | "
          f"Trades: {result.n_trades} | "
          f"PF: {result.profit_factor:.2f}")


def _persist_sweep_results(results: List[BacktestResult], trader: str,
                           start_date: str, end_date: str):
    """Write sweep results to shared/trader.db for dashboard consumption."""
    db_path = PROJECT_DIR / "shared" / "trader.db"
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

        # Create sweep_results table if not exists
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sweep_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                trader TEXT NOT NULL,
                ticker TEXT NOT NULL,
                variant_id INTEGER,
                start_date TEXT,
                end_date TEXT,
                n_bars INTEGER,
                n_trades INTEGER,
                total_return_pct REAL,
                sharpe REAL,
                max_drawdown_pct REAL,
                win_rate REAL,
                profit_factor REAL,
                final_equity REAL,
                params TEXT,
                data_source TEXT DEFAULT 'databus'
            )
        """)

        for r in results:
            cur.execute(
                """INSERT INTO sweep_results
                   (trader, ticker, variant_id, start_date, end_date,
                    n_bars, n_trades, total_return_pct, sharpe,
                    max_drawdown_pct, win_rate, profit_factor,
                    final_equity, params, data_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (r.trader, r.ticker, 0 if r.params else None,
                 r.start_date, r.end_date,
                 r.n_bars, r.n_trades, r.total_return_pct, r.sharpe,
                 r.max_drawdown_pct, r.win_rate, r.profit_factor,
                 r.final_equity, json.dumps(r.params), "databus"),
            )
        conn.commit()
        conn.close()
        log.info("Persisted %d sweep results to %s", len(results), db_path)
    except Exception as e:
        log.warning("Failed to persist sweep results: %s", e)


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    """Entry point with subcommand dispatch."""
    parser = argparse.ArgumentParser(
        prog="historical_sim",
        description="Historical simulation via data bus API.",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    # sweep
    sp = sub.add_parser("sweep", help="Run multi-variant parameter sweep")
    sp.add_argument("--trader", default="kairos", help="Trader type")
    sp.add_argument("--ticker", default="AAPL,MSFT,SPY", help="Ticker(s) comma-separated")
    sp.add_argument("--days", type=int, default=5, help="Days of history")
    sp.add_argument("--variants", type=int, default=3, help="Number of param variants")

    # backtest
    bp = sub.add_parser("backtest", help="Single backtest run")
    bp.add_argument("--trader", default="kairos")
    bp.add_argument("--ticker", default="AAPL")
    bp.add_argument("--days", type=int, default=30)

    # findings
    fp = sub.add_parser("findings", help="Show sim findings from DB")
    fp.add_argument("--trader", default=None, help="Filter by trader (default: all)")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [historical_sim] %(levelname)s %(message)s",
    )

    if args.mode == "sweep":
        return cmd_sweep(args)
    elif args.mode == "backtest":
        return cmd_backtest(args)
    elif args.mode == "findings":
        return cmd_findings(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())