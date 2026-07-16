#!/usr/bin/env python3
"""
SPY Benchmark Overlay — SPY buy-and-hold baseline for nightly pipeline.

Computes SPY buy-and-hold performance metrics over the same period
used in variant sweeps, enabling "did we beat the market?" comparison.

Usage:
    python3 scripts/spy_benchmark.py                          # stdout
    python3 scripts/spy_benchmark.py --date 2026-07-15        # specific date
    python3 scripts/spy_benchmark.py --days 20                 # period length
    python3 scripts/spy_benchmark.py --json                    # JSON output

Spec: system-audit.md §2.2G
Issue: #204
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))


# ═══════════════════════════════════════════════════════════════════════════════
# Data fetching
# ═══════════════════════════════════════════════════════════════════════════════


def _get_trading_dates(
    end_date: str,
    n_dates: int,
    calendar: Optional[List[str]] = None,
) -> List[str]:
    """Get the last N trading dates before (and including) end_date.

    Uses the Alpaca calendar or falls back to simple business day estimation.

    Args:
        end_date: Reference date (YYYY-MM-DD).
        n_dates: Number of trading dates wanted.
        calendar: Optional pre-resolved calendar from Alpaca API.

    Returns:
        List of date strings in ascending order.
    """
    if calendar:
        # calendar is pre-sorted ascending
        filtered = [d for d in calendar if d <= end_date]
        return filtered[-n_dates:] if len(filtered) >= n_dates else filtered

    # Fallback: simple M-F filter
    from datetime import date, timedelta as td

    end = date.fromisoformat(end_date)
    dates = []
    current = end
    while len(dates) < n_dates:
        if current.weekday() < 5:  # M-F
            dates.append(current.isoformat())
        current -= td(days=1)
    return sorted(dates)


def _fetch_spy_bars(
    start_date: str,
    end_date: str,
) -> List[Dict[str, Any]]:
    """Fetch daily SPY bars for the given date range.

    Tries: 1) data bus, 2) yfinance, 3) fallback constants.
    """
    # Attempt 1: Data bus
    try:
        import urllib.request

        url = f"http://localhost:5000/api/v1/bars?symbol=SPY&start={start_date}&end={end_date}&interval=1day"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            if isinstance(data, dict) and data.get("bars"):
                return data["bars"]
            if isinstance(data, list):
                return data
    except Exception:
        pass

    # Attempt 2: yfinance
    try:
        from src.data_fetcher import fetch_bars_yfinance

        bars = fetch_bars_yfinance("SPY", start_date, end_date, interval="1day")
        if bars:
            return bars
    except Exception:
        pass

    return []


# ═══════════════════════════════════════════════════════════════════════════════
# Metrics
# ═══════════════════════════════════════════════════════════════════════════════


def _compute_returns(closes: List[float]) -> List[float]:
    """Compute daily log returns from closing prices."""
    if len(closes) < 2:
        return []
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]


def _max_drawdown(closes: List[float]) -> float:
    """Maximum drawdown as a positive fraction (0.1 = 10% drawdown)."""
    if not closes:
        return 0.0
    peak = closes[0]
    max_dd = 0.0
    for price in closes:
        if price > peak:
            peak = price
        dd = (peak - price) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _sharpe_ratio(returns: List[float], risk_free_rate: float = 0.05) -> float:
    """Annualized Sharpe ratio from daily returns."""
    if not returns or len(returns) < 2:
        return 0.0
    mean_ret = sum(returns) / len(returns)
    if mean_ret == 0:
        return 0.0
    variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
    import math

    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0:
        return 0.0
    # Annualize: 252 trading days
    return (mean_ret * 252 - risk_free_rate) / (std * math.sqrt(252))


def _calmar_ratio(total_return_pct: float, max_drawdown_pct: float) -> float:
    """Calmar ratio: annualized return / max drawdown."""
    if max_drawdown_pct == 0:
        return 0.0
    return total_return_pct / 100 / max_drawdown_pct * 100


def _win_rate(returns: List[float]) -> float:
    """Fraction of days with positive returns."""
    if not returns:
        return 0.0
    positive = sum(1 for r in returns if r > 0)
    return positive / len(returns)


def _volatility(returns: List[float]) -> float:
    """Annualized volatility from daily returns."""
    if not returns or len(returns) < 2:
        return 0.0
    mean_ret = sum(returns) / len(returns)
    import math

    variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(252)


# ═══════════════════════════════════════════════════════════════════════════════
# Main benchmark function
# ═══════════════════════════════════════════════════════════════════════════════


def compute_spy_benchmark(
    date_str: Optional[str] = None,
    n_dates: int = 20,
) -> Dict[str, Any]:
    """Compute SPY buy-and-hold benchmark metrics.

    Fetches daily SPY bars for the N trading days ending on date_str,
    then computes standard performance metrics.

    Args:
        date_str: Reference date (YYYY-MM-DD). Default: yesterday.
        n_dates: Number of trading days in the analysis period. Default: 20.

    Returns:
        Dict with keys: start_date, end_date, days, total_return_pct,
        max_drawdown_pct, sharpe_ratio, calmar_ratio, win_rate_pct,
        annualized_vol_pct, start_price, end_price, n_bars, error.
    """
    if date_str is None:
        date_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    # Get trading dates
    dates = _get_trading_dates(date_str, n_dates)
    if not dates:
        return {"error": "No trading dates resolved"}

    start_date = dates[0]
    # Add a few extra calendar days to ensure data coverage
    end_date_buffered = (
        datetime.fromisoformat(date_str) + timedelta(days=2)
    ).isoformat()

    bars = _fetch_spy_bars(start_date, end_date_buffered)

    result: Dict[str, Any] = {
        "start_date": start_date,
        "end_date": date_str,
        "trading_dates_requested": n_dates,
        "trading_dates_available": len(dates),
        "total_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "sharpe_ratio": 0.0,
        "calmar_ratio": 0.0,
        "win_rate_pct": 0.0,
        "annualized_vol_pct": 0.0,
        "start_price": 0.0,
        "end_price": 0.0,
        "n_bars": len(bars),
        "error": None,
    }

    if not bars or len(bars) < 2:
        result["error"] = (
            f"No SPY bar data for {start_date} → {date_str}"
            if not bars
            else f"Only {len(bars)} bar(s) — need ≥ 2 for metrics"
        )
        return result

    closes = [b["close"] for b in bars]
    result["start_price"] = round(closes[0], 2)
    result["end_price"] = round(closes[-1], 2)

    # Total return
    total_return = (closes[-1] - closes[0]) / closes[0]
    result["total_return_pct"] = round(total_return * 100, 2)

    # Max drawdown
    result["max_drawdown_pct"] = round(_max_drawdown(closes) * 100, 2)

    # Daily returns
    returns = _compute_returns(closes)
    result["n_daily_returns"] = len(returns)

    # Sharpe ratio
    result["sharpe_ratio"] = round(_sharpe_ratio(returns), 3)

    # Calmar ratio
    result["calmar_ratio"] = round(
        _calmar_ratio(result["total_return_pct"], result["max_drawdown_pct"]), 3
    )

    # Win rate
    result["win_rate_pct"] = round(_win_rate(returns) * 100, 1)

    # Volatility
    result["annualized_vol_pct"] = round(_volatility(returns) * 100, 2)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Report formatting
# ═══════════════════════════════════════════════════════════════════════════════


def format_benchmark(bench: Dict[str, Any]) -> str:
    """Format benchmark results as a markdown section."""
    if bench.get("error"):
        return f"#### 📊 SPY Benchmark\n\n⚠️ **{bench['error']}**\n"

    lines = [
        "#### 📊 SPY Buy-and-Hold Benchmark",
        "",
        f"**Period:** {bench['start_date']} → {bench['end_date']} "
        f"({bench.get('trading_dates_available', '?')} trading days, "
        f"{bench['n_bars']} bars)",
        f"**SPY:** ${bench['start_price']} → ${bench['end_price']} "
        f"({bench['total_return_pct']:+.2f}%)",
        "",
        "| Metric | SPY (Buy & Hold) |",
        "|--------|-----------------|",
        f"| Total Return | {bench['total_return_pct']:+.2f}% |",
        f"| Max Drawdown | -{bench['max_drawdown_pct']:.2f}% |",
        f"| Sharpe Ratio | {bench['sharpe_ratio']:.3f} |",
        f"| Calmar Ratio | {bench['calmar_ratio']:.3f} |",
        f"| Win Rate | {bench['win_rate_pct']:.1f}% |",
        f"| Ann. Volatility | {bench['annualized_vol_pct']:.2f}% |",
        "",
    ]
    return "\n".join(lines)


def format_vs_spy(bench: Dict[str, Any], variant_metrics: Dict[str, Any]) -> str:
    """Format a comparison table between a variant and SPY benchmark.

    Args:
        bench: Dict from compute_spy_benchmark().
        variant_metrics: Dict with keys matching benchmark keys
            (total_return_pct, max_drawdown_pct, sharpe_ratio, etc.)

    Returns:
        Markdown comparison table.
    """
    if bench.get("error"):
        return f"⚠️ SPY benchmark unavailable: {bench['error']}"

    lines = [
        "#### 🆚 vs. SPY",
        "",
        "| Metric | SPY | Variant | Δ |",
        "|--------|-----|---------|---|",
    ]

    metrics = [
        ("Total Return %", "total_return_pct", ">"),
        ("Max Drawdown %", "max_drawdown_pct", "<"),
        ("Sharpe Ratio", "sharpe_ratio", ">"),
        ("Calmar Ratio", "calmar_ratio", ">"),
        ("Win Rate %", "win_rate_pct", ">"),
        ("Ann. Vol %", "annualized_vol_pct", "<"),
    ]

    for label, key, better in metrics:
        spy_val = bench.get(key, 0)
        var_val = variant_metrics.get(key, 0)
        delta = var_val - spy_val
        delta_str = f"{delta:+.2f}" if abs(delta) >= 0.01 else f"{delta:+.3f}"

        # Determine if variant beats SPY on this metric
        if better == ">":
            beats = var_val > spy_val
        else:
            beats = var_val < spy_val
        icon = "✅" if beats else "❌"

        lines.append(
            f"| {label} | {spy_val:.2f} | {var_val:.2f} | {delta_str} {icon} |"
        )

    lines.append("")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="SPY Benchmark — buy-and-hold baseline for nightly pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Reference date (YYYY-MM-DD). Default: yesterday.",
    )
    parser.add_argument(
        "--days", type=int, default=20,
        help="Number of trading days in the period (default: 20)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output as JSON instead of markdown",
    )
    args = parser.parse_args()

    bench = compute_spy_benchmark(date_str=args.date, n_dates=args.days)

    if args.json:
        print(json.dumps(bench, indent=2))
    else:
        print(format_benchmark(bench))
        if bench.get("error"):
            sys.exit(1)


if __name__ == "__main__":
    main()
