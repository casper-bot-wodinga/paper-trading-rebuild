#!/usr/bin/env python3
"""
historical_sim — Paper trading historical simulation & parameter optimization.

CLI modes:
  improve   Run multi-variant parameter sweeps to find optimal trading params.
            Usage: historical_sim improve --trader <NAME> --ticker <TICKER> --variants <N>

subcommands:
  backtest  Single backtest run (not yet implemented).
  compare   Compare multiple trader strategies (not yet implemented).

Full-scale param sweeps (expected runtime):
  2 variants × 5 ticks × 63 days             ~ 20s  (this test)
  10 variants × 1 tick × 252 days            ~ 2 min
  50 variants × 5 tickers × 252 days         ~ 15 min
  200 variants × 20 tickers × 252 days       ~ 1 hr  (ProcessPoolExecutor workers=4)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf


# ---------------------------------------------------------------------------
# DB Path
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _REPO_ROOT / "shared" / "trader.db"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class TraderParam:
    """One set of trading parameters for a given trader personality."""

    name: str
    label: str
    type: str  # "float" | "int" | "choice"
    default: Any
    min_val: Any = None
    max_val: Any = None
    choices: list = field(default_factory=list)
    description: str = ""


TRADER_PARAM_SCHEMAS: dict[str, list[TraderParam]] = {
    "stonks": [
        TraderParam("rsi_overbought", "RSI Overbought", "int", 70, 60, 85, description="RSI threshold for sell signal"),
        TraderParam("rsi_oversold", "RSI Oversold", "int", 30, 15, 45, description="RSI threshold for buy signal"),
        TraderParam("volume_multiplier", "Volume Multiplier", "float", 1.5, 1.0, 3.0, description="Min volume relative to avg"),
        TraderParam("stop_loss_pct", "Stop Loss %", "float", 8.0, 3.0, 15.0, description="Max loss before forced exit"),
        TraderParam("max_position_pct", "Max Position %", "float", 25.0, 5.0, 50.0, description="Max portfolio % per position"),
        TraderParam("conviction", "Conviction Threshold", "float", 0.6, 0.3, 0.95, description="Min confidence to enter trade"),
    ],
    "aldridge": [
        TraderParam("rsi_oversold", "RSI Oversold (Value)", "int", 40, 20, 55, description="RSI threshold for value entry"),
        TraderParam("pe_max", "Max P/E Ratio", "float", 20.0, 8.0, 50.0, description="Max P/E for value screening"),
        TraderParam("stop_loss_pct", "Stop Loss %", "float", 8.0, 3.0, 15.0, description="Max loss before forced exit"),
        TraderParam("take_profit_pct", "Take Profit %", "float", 15.0, 5.0, 30.0, description="Profit target to exit"),
        TraderParam("max_position_pct", "Max Position %", "float", 25.0, 5.0, 50.0, description="Max portfolio % per position"),
    ],
    "kairos": [
        TraderParam("rsi_momentum", "RSI Momentum", "int", 60, 40, 80, description="Min RSI for momentum entry"),
        TraderParam("trailing_stop_pct", "Trailing Stop %", "float", 7.0, 2.0, 15.0, description="Trailing stop loss percentage"),
        TraderParam("stop_loss_pct", "Stop Loss %", "float", 7.0, 3.0, 15.0, description="Max loss before forced exit"),
        TraderParam("max_position_pct", "Max Position %", "float", 20.0, 5.0, 40.0, description="Max portfolio % per position"),
        TraderParam("conviction", "Conviction Threshold", "float", 0.63, 0.3, 0.95, description="Min confidence to enter trade"),
    ],
}


# ---------------------------------------------------------------------------
# Carry-over state — multi-day portfolio persistence
# ---------------------------------------------------------------------------


@dataclass
class Position:
    """An open position carried across trading days."""

    ticker: str
    shares: int
    entry_price: float
    entry_date: str  # ISO-8601 date string

    def current_value(self, current_price: float) -> float:
        return self.shares * current_price

    def unrealized_pnl(self, current_price: float) -> float:
        return self.shares * (current_price - self.entry_price)

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "shares": self.shares,
                "entry_price": self.entry_price, "entry_date": self.entry_date}

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(ticker=d["ticker"], shares=d["shares"],
                   entry_price=d["entry_price"], entry_date=d["entry_date"])


@dataclass
class SimState:
    """Complete simulation state carried across days.

    Holds cash, open positions, cumulative P&L, and the full trade log.
    Designed to be serializable to/from a plain dict for checkpoint/resume.
    """

    cash: float
    positions: dict[str, Position]  # ticker -> Position (max 1 per ticker)
    trade_log: list[dict]
    cumulative_realized_pnl: float
    initial_capital: float
    trader_type: str
    params: dict[str, Any]
    current_date: str = ""
    trade_count: int = 0
    portfolio_value_history: list[dict] = field(default_factory=list)

    def total_equity(self, prices: dict[str, float]) -> float:
        """Cash + market value of all open positions at given prices."""
        pos_value = sum(
            pos.shares * prices.get(pos.ticker, pos.entry_price)
            for pos in self.positions.values()
        )
        return self.cash + pos_value

    def total_unrealized_pnl(self, prices: dict[str, float]) -> float:
        return sum(
            pos.unrealized_pnl(prices.get(pos.ticker, pos.entry_price))
            for pos in self.positions.values()
        )

    def to_dict(self) -> dict:
        return {
            "cash": self.cash,
            "positions": {t: p.to_dict() for t, p in self.positions.items()},
            "trade_log": self.trade_log,
            "cumulative_realized_pnl": self.cumulative_realized_pnl,
            "initial_capital": self.initial_capital,
            "trader_type": self.trader_type,
            "params": self.params,
            "current_date": self.current_date,
            "trade_count": self.trade_count,
            "portfolio_value_history": self.portfolio_value_history,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SimState":
        return cls(
            cash=d["cash"],
            positions={t: Position.from_dict(p) for t, p in d["positions"].items()},
            trade_log=d["trade_log"],
            cumulative_realized_pnl=d["cumulative_realized_pnl"],
            initial_capital=d["initial_capital"],
            trader_type=d["trader_type"],
            params=d["params"],
            current_date=d.get("current_date", ""),
            trade_count=d.get("trade_count", 0),
            portfolio_value_history=d.get("portfolio_value_history", []),
        )

    def serialize(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def deserialize(cls, raw: str) -> "SimState":
        return cls.from_dict(json.loads(raw))

    def clone(self) -> "SimState":
        """Deep copy via serialize/deserialize."""
        return SimState.deserialize(self.serialize())


def create_initial_state(
    capital: float,
    trader_type: str,
    params: dict[str, Any],
    current_date: str = "",
) -> SimState:
    """Create a fresh SimState with no positions."""
    return SimState(
        cash=capital,
        positions={},
        trade_log=[],
        cumulative_realized_pnl=0.0,
        initial_capital=capital,
        trader_type=trader_type,
        params=params,
        current_date=current_date,
        trade_count=0,
        portfolio_value_history=[],
    )


# ---------------------------------------------------------------------------
# Strategy simulation
# ---------------------------------------------------------------------------
def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Compute RSI indicator."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def compute_sma(series: pd.Series, period: int) -> pd.Series:
    """Compute simple moving average."""
    return series.rolling(window=period, min_periods=period).mean()


def backtest_trader(
    ticker: str,
    trader_type: str,
    params: dict[str, Any],
    period: str = "3mo",
    initial_cash: float = 100_000.0,
) -> dict[str, Any]:
    """
    Run a historical backtest for a trader type with given parameters.

    Returns a dict of performance metrics.
    """
    # Download data
    try:
        data = yf.download(ticker, period=period, progress=False, auto_adjust=True)
    except Exception as e:
        return {"ticker": ticker, "error": f"yfinance download failed: {e}", "total_return": -999}
    if data is None or (hasattr(data, 'empty') and data.empty) or (hasattr(data, '__len__') and len(data) < 20):
        return {"ticker": ticker, "error": f"Insufficient data for {ticker}", "total_return": -999}

    # Flatten MultiIndex columns if present
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    close = data["Close"].squeeze()
    volume = data["Volume"].squeeze() if "Volume" in data.columns else pd.Series(0, index=close.index)
    high = data["High"].squeeze() if "High" in data.columns else close
    low = data["Low"].squeeze() if "Low" in data.columns else close

    rsi = compute_rsi(close)
    sma20 = compute_sma(close, 20)
    sma50 = compute_sma(close, 50)
    avg_volume = volume.rolling(20).mean()

    cash = initial_cash
    position = 0  # shares held
    entry_price = 0.0
    trades: list[dict] = []
    portfolio_values: list[float] = [initial_cash]

    extract = {
        "stonks": _strategy_stonks,
        "aldridge": _strategy_aldridge,
        "kairos": _strategy_kairos,
    }

    strategy_fn = extract.get(trader_type)
    if strategy_fn is None:
        return {"ticker": ticker, "error": f"Unknown trader type: {trader_type}", "total_return": -999}

    for i in range(20, len(close)):  # skip warm-up
        date = close.index[i]
        cur_close = float(close.iloc[i])
        cur_high = float(high.iloc[i])
        cur_low = float(low.iloc[i])
        cur_rsi = float(rsi.iloc[i]) if not pd.isna(rsi.iloc[i]) else 50.0
        cur_sma20 = float(sma20.iloc[i]) if not pd.isna(sma20.iloc[i]) else cur_close
        cur_sma50 = float(sma50.iloc[i]) if not pd.isna(sma50.iloc[i]) else cur_close
        cur_vol = float(volume.iloc[i]) if not pd.isna(volume.iloc[i]) else 0
        cur_avg_vol = float(avg_volume.iloc[i]) if not pd.isna(avg_volume.iloc[i]) else 1

        if position > 0:
            # Check stop-loss / take-profit
            stop_pct = params.get("stop_loss_pct", 8.0) / 100.0
            tp_pct = params.get("take_profit_pct", 0) / 100.0
            trailing_pct = params.get("trailing_stop_pct", 0) / 100.0

            # Simple stop loss
            if cur_low <= entry_price * (1 - stop_pct):
                # Sell on stop
                proceeds = position * cur_close
                pnl = proceeds - (position * entry_price)
                cash += proceeds
                trades.append({"date": str(date.date()), "action": "SELL", "price": cur_close,
                               "shares": position, "pnl": pnl, "reason": "stop_loss"})
                position = 0
                entry_price = 0.0
            elif tp_pct > 0 and cur_high >= entry_price * (1 + tp_pct):
                proceeds = position * cur_close
                pnl = proceeds - (position * entry_price)
                cash += proceeds
                trades.append({"date": str(date.date()), "action": "SELL", "price": cur_close,
                               "shares": position, "pnl": pnl, "reason": "take_profit"})
                position = 0
                entry_price = 0.0

        if position == 0:
            # Look for entry signal
            signal = strategy_fn(i, cur_close, cur_rsi, cur_sma20, cur_sma50, cur_vol, cur_avg_vol, params)
            if signal == "BUY":
                # Determine position size
                max_pct = params.get("max_position_pct", 25.0) / 100.0
                invest_amount = cash * max_pct
                shares = int(invest_amount / cur_close)
                if shares > 0 and cash >= shares * cur_close:
                    cost = shares * cur_close
                    cash -= cost
                    position = shares
                    entry_price = cur_close
                    trades.append({"date": str(date.date()), "action": "BUY", "price": cur_close,
                                   "shares": shares, "pnl": 0, "reason": "entry"})

        # Portfolio value
        pv = cash + position * cur_close
        portfolio_values.append(pv)

    # Close any remaining position at last price
    final_close = float(close.iloc[-1])
    if position > 0:
        proceeds = position * final_close
        pnl = proceeds - (position * entry_price)
        cash += proceeds
        trades.append({"date": str(close.index[-1].date()), "action": "SELL", "price": final_close,
                       "shares": position, "pnl": pnl, "reason": "close"})
        position = 0

    portfolio_values.append(cash)
    pv_series = pd.Series(portfolio_values)

    # Metrics
    total_return_pct = ((cash - initial_cash) / initial_cash) * 100
    daily_returns = pv_series.pct_change().dropna()

    # Sharpe ratio (assuming risk-free rate of 0)
    sharpe = np.nan
    if len(daily_returns) > 1 and daily_returns.std() > 0 and not daily_returns.empty:
        sharpe = float((daily_returns.mean() / daily_returns.std()) * np.sqrt(252))

    # Max drawdown
    cum_max = pv_series.cummax()
    drawdowns = (pv_series - cum_max) / cum_max
    max_dd = float(drawdowns.min() * 100) if len(drawdowns) > 0 else 0.0

    # Win rate & profit factor
    closed_trades = [t for t in trades if t["action"] == "SELL"]
    wins = [t for t in closed_trades if t["pnl"] > 0]
    win_rate = (len(wins) / len(closed_trades) * 100) if closed_trades else 0.0

    gross_profit = sum(t["pnl"] for t in closed_trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in closed_trades if t["pnl"] < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0)

    return {
        "ticker": ticker,
        "total_return": round(total_return_pct, 2),
        "sharpe": round(sharpe, 3) if not (isinstance(sharpe, float) and np.isnan(sharpe)) else "N/A",
        "max_drawdown": round(max_dd, 2),
        "win_rate": round(win_rate, 1),
        "num_trades": len(closed_trades),
        "profit_factor": round(profit_factor, 2),
        "final_cash": round(cash, 2),
        "params": params,
    }


def _strategy_stonks(
    i: int, close: float, rsi: float, sma20: float, sma50: float,
    volume: float, avg_vol: float, params: dict,
) -> str:
    """Aggressive momentum + sentiment strategy."""
    rsi_ob = params.get("rsi_overbought", 70)
    rsi_os = params.get("rsi_oversold", 30)
    vol_mult = params.get("volume_multiplier", 1.5)
    conviction = params.get("conviction", 0.6)

    # Buy: oversold bounce + volume confirmation
    if rsi < rsi_os and volume > avg_vol * vol_mult:
        return "BUY"
    return None


def _strategy_aldridge(
    i: int, close: float, rsi: float, sma20: float, sma50: float,
    volume: float, avg_vol: float, params: dict,
) -> str:
    """Value-based long-only strategy."""
    rsi_os = params.get("rsi_oversold", 40)

    # Buy: oversold with price near/above 50-day SMA (not in freefall)
    if rsi < rsi_os and close >= sma50 * 0.95 and sma20 > sma50 * 0.98:
        return "BUY"
    return None


def _strategy_kairos(
    i: int, close: float, rsi: float, sma20: float, sma50: float,
    volume: float, avg_vol: float, params: dict,
) -> str:
    """Momentum strategy."""
    rsi_mom = params.get("rsi_momentum", 60)
    conviction = params.get("conviction", 0.63)

    # Buy: momentum — RSI rising into strength, bullish SMA cross
    if rsi >= rsi_mom and sma20 > sma50 and volume > 0:
        return "BUY"
    return None


# ---------------------------------------------------------------------------
# Carry-over simulation — day-by-day state threading
# ---------------------------------------------------------------------------


def _prepare_market_data(ticker: str, period: str = "3mo") -> Optional[pd.DataFrame]:
    """Download and flatten market data. Returns None on failure."""
    try:
        data = yf.download(ticker, period=period, progress=False, auto_adjust=True)
    except Exception:
        return None
    if data is None or (hasattr(data, 'empty') and data.empty) or (hasattr(data, '__len__') and len(data) < 20):
        return None
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    return data


def _compute_indicators(data: pd.DataFrame) -> dict[str, pd.Series]:
    """Pre-compute indicator series for the full dataset.

    Returns dict of named Series (close, volume, high, low, rsi, sma20, sma50, avg_volume).
    """
    close = data["Close"].squeeze()
    volume = data["Volume"].squeeze() if "Volume" in data.columns else pd.Series(0, index=close.index)
    high = data["High"].squeeze() if "High" in data.columns else close
    low = data["Low"].squeeze() if "Low" in data.columns else close

    return {
        "close": close,
        "volume": volume,
        "high": high,
        "low": low,
        "rsi": compute_rsi(close),
        "sma20": compute_sma(close, 20),
        "sma50": compute_sma(close, 50),
        "avg_volume": volume.rolling(20).mean(),
    }


def _step_day(
    state: SimState,
    date: pd.Timestamp,
    indicators: dict[str, pd.Series],
    idx: int,
) -> SimState:
    """Process one trading day and return the updated state.

    Args:
        state: Current simulation state (will be modified in-place for efficiency).
        date: The current trading date.
        indicators: Dict of pre-computed indicator Series.
        idx: Index into the indicator Series for this day.

    Returns:
        The same state object (mutated) with updated positions, cash, and log.
    """
    close = float(indicators["close"].iloc[idx])
    high = float(indicators["high"].iloc[idx])
    low = float(indicators["low"].iloc[idx])
    cur_rsi = float(indicators["rsi"].iloc[idx]) if not pd.isna(indicators["rsi"].iloc[idx]) else 50.0
    cur_sma20 = float(indicators["sma20"].iloc[idx]) if not pd.isna(indicators["sma20"].iloc[idx]) else close
    cur_sma50 = float(indicators["sma50"].iloc[idx]) if not pd.isna(indicators["sma50"].iloc[idx]) else close
    cur_vol = float(indicators["volume"].iloc[idx]) if not pd.isna(indicators["volume"].iloc[idx]) else 0
    cur_avg_vol = float(indicators["avg_volume"].iloc[idx]) if not pd.isna(indicators["avg_volume"].iloc[idx]) else 1

    ticker = indicators["close"].name if hasattr(indicators["close"], "name") else ""
    date_str = str(date.date())
    state.current_date = date_str

    params = state.params
    trader_type = state.trader_type
    stop_pct = params.get("stop_loss_pct", 8.0) / 100.0
    tp_pct = params.get("take_profit_pct", 0) / 100.0
    trailing_pct = params.get("trailing_stop_pct", 0) / 100.0

    # --- Check exits on open positions ---
    for ticker_key in list(state.positions.keys()):
        pos = state.positions[ticker_key]

        # Simple stop loss
        if low <= pos.entry_price * (1 - stop_pct):
            proceeds = pos.shares * close
            pnl = proceeds - (pos.shares * pos.entry_price)
            state.cash += proceeds
            state.cumulative_realized_pnl += pnl
            state.trade_count += 1
            state.trade_log.append({
                "date": date_str, "action": "SELL", "price": close,
                "shares": pos.shares, "pnl": round(pnl, 2), "reason": "stop_loss",
                "ticker": ticker_key,
            })
            del state.positions[ticker_key]
            continue

        # Take profit
        if tp_pct > 0 and high >= pos.entry_price * (1 + tp_pct):
            proceeds = pos.shares * close
            pnl = proceeds - (pos.shares * pos.entry_price)
            state.cash += proceeds
            state.cumulative_realized_pnl += pnl
            state.trade_count += 1
            state.trade_log.append({
                "date": date_str, "action": "SELL", "price": close,
                "shares": pos.shares, "pnl": round(pnl, 2), "reason": "take_profit",
                "ticker": ticker_key,
            })
            del state.positions[ticker_key]
            continue

    # --- Entry signal (only if no position for this ticker) ---
    if ticker and ticker not in state.positions:
        extract = {
            "stonks": _strategy_stonks,
            "aldridge": _strategy_aldridge,
            "kairos": _strategy_kairos,
        }
        strategy_fn = extract.get(trader_type)
        if strategy_fn:
            signal = strategy_fn(
                idx, close, cur_rsi, cur_sma20, cur_sma50, cur_vol, cur_avg_vol, params
            )
            if signal == "BUY":
                max_pct = params.get("max_position_pct", 25.0) / 100.0
                invest_amount = state.cash * max_pct
                shares = int(invest_amount / close)
                if shares > 0 and state.cash >= shares * close:
                    cost = shares * close
                    state.cash -= cost
                    state.positions[ticker] = Position(
                        ticker=ticker, shares=shares, entry_price=close, entry_date=date_str
                    )
                    state.trade_count += 1
                    state.trade_log.append({
                        "date": date_str, "action": "BUY", "price": close,
                        "shares": shares, "pnl": 0, "reason": "entry",
                        "ticker": ticker,
                    })

    # Record portfolio value snapshot
    pv = state.cash + sum(
        p.shares * close for p in state.positions.values()
    )
    state.portfolio_value_history.append({"date": date_str, "value": round(pv, 2)})

    return state


def _close_remaining_positions(state: SimState, final_close: float, final_date: str) -> SimState:
    """Close all open positions at the end of simulation."""
    for ticker_key in list(state.positions.keys()):
        pos = state.positions[ticker_key]
        proceeds = pos.shares * final_close
        pnl = proceeds - (pos.shares * pos.entry_price)
        state.cash += proceeds
        state.cumulative_realized_pnl += pnl
        state.trade_count += 1
        state.trade_log.append({
            "date": final_date, "action": "SELL", "price": final_close,
            "shares": pos.shares, "pnl": round(pnl, 2), "reason": "close",
            "ticker": ticker_key,
        })
        del state.positions[ticker_key]
    return state


def run_backtest_carryover(
    ticker: str,
    trader_type: str,
    params: dict[str, Any],
    period: str = "3mo",
    initial_capital: float = 100_000.0,
    initial_state: Optional[SimState] = None,
) -> SimState:
    """Run a multi-day backtest with state carried across days.

    Unlike backtest_trader() which resets state each invocation, this function
    accepts and returns a SimState, allowing callers to:
      - Save/restore state mid-simulation
      - Inspect state after any trading day
      - Resume from a previous state

    Args:
        ticker: The ticker symbol to trade.
        trader_type: Which strategy to use ("stonks", "aldridge", "kairos").
        params: Trading parameters dict.
        period: yfinance period string (default "3mo").
        initial_capital: Starting cash if creating fresh state.
        initial_state: Optional pre-existing state to resume from.

    Returns:
        Final SimState after processing all days.
    """
    data = _prepare_market_data(ticker, period)
    if data is None:
        raise ValueError(f"Failed to download or insufficient data for {ticker}")

    indicators = _compute_indicators(data)

    if initial_state is not None:
        state = initial_state.clone()
    else:
        state = create_initial_state(initial_capital, trader_type, params,
                                     current_date=str(data.index[0].date()))

    start_idx = max(20, 0)  # skip warm-up
    for i in range(start_idx, len(data)):
        _step_day(state, data.index[i], indicators, i)

    # Close any remaining position
    final_close = float(indicators["close"].iloc[-1])
    final_date = str(data.index[-1].date())
    _close_remaining_positions(state, final_close, final_date)

    return state


def compute_metrics_from_state(state: SimState) -> dict[str, Any]:
    """Compute performance metrics from a final SimState.

    Returns same metric dict shape as backtest_trader() for compatibility.
    """
    total_return_pct = ((state.cash - state.initial_capital) / state.initial_capital) * 100

    pv_list = [v["value"] for v in state.portfolio_value_history]
    pv_series = pd.Series(pv_list)

    daily_returns = pv_series.pct_change().dropna()

    sharpe = np.nan
    if len(daily_returns) > 1 and daily_returns.std() > 0 and not daily_returns.empty:
        sharpe = float((daily_returns.mean() / daily_returns.std()) * np.sqrt(252))

    cum_max = pv_series.cummax()
    drawdowns = (pv_series - cum_max) / cum_max
    max_dd = float(drawdowns.min() * 100) if len(drawdowns) > 0 else 0.0

    closed_trades = [t for t in state.trade_log if t["action"] == "SELL"]
    wins = [t for t in closed_trades if t["pnl"] > 0]
    win_rate = (len(wins) / len(closed_trades) * 100) if closed_trades else 0.0

    gross_profit = sum(t["pnl"] for t in closed_trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in closed_trades if t["pnl"] < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0)

    return {
        "total_return": round(total_return_pct, 2),
        "sharpe": round(sharpe, 3) if not (isinstance(sharpe, float) and np.isnan(sharpe)) else "N/A",
        "max_drawdown": round(max_dd, 2),
        "win_rate": round(win_rate, 1),
        "num_trades": len(closed_trades),
        "profit_factor": round(profit_factor, 2),
        "final_cash": round(state.cash, 2),
        "cumulative_realized_pnl": round(state.cumulative_realized_pnl, 2),
        "trade_log": state.trade_log,
    }


# ---------------------------------------------------------------------------
# Parameter sweep (improve mode)
# ---------------------------------------------------------------------------
def _generate_variant_params(trader_type: str, variant_idx: int, total_variants: int) -> dict[str, Any]:
    """
    Generate the variant-th parameter set by interpolating values within the
    allowed ranges for each param.
    """
    schemas = TRADER_PARAM_SCHEMAS.get(trader_type, [])
    params: dict[str, Any] = {}
    for sp in schemas:
        if total_variants <= 1:
            params[sp.name] = sp.default
        else:
            if sp.type == "float":
                val = sp.min_val + (sp.max_val - sp.min_val) * (variant_idx / (total_variants - 1))
                params[sp.name] = round(val, 2)
            elif sp.type == "int":
                val = int(sp.min_val + (sp.max_val - sp.min_val) * (variant_idx / (total_variants - 1)))
                params[sp.name] = val
            else:
                params[sp.name] = sp.default
    return params


def _run_variant(args: tuple) -> list[dict]:
    """Run one variant across all tickers. Picklable for ProcessPoolExecutor."""
    trader_type, variant_idx, total_variants, tickers, period = args
    params = _generate_variant_params(trader_type, variant_idx, total_variants)
    results = []
    for tkr in tickers:
        result = backtest_trader(tkr, trader_type, params, period=period)
        result["variant"] = variant_idx
        results.append(result)
    return results


def _build_comparison_row(
    variant_idx: int, ticker_results: list[dict], params: dict, trader_type: str
) -> dict:
    """Aggregate one variant's results across all tickers."""
    valid = [r for r in ticker_results if "error" not in r]
    if not valid:
        return {"variant": variant_idx, "error": "No valid results"}

    avg_return = np.mean([r["total_return"] for r in valid])
    avg_sharpe = np.mean([r["sharpe"] for r in valid if isinstance(r.get("sharpe"), (int, float))])
    avg_dd = np.mean([r["max_drawdown"] for r in valid])
    avg_win = np.mean([r["win_rate"] for r in valid])
    total_trades = sum(r["num_trades"] for r in valid)
    avg_pf = np.mean([r["profit_factor"] for r in valid])

    param_str = "; ".join(f"{k}={v}" for k, v in sorted(params.items()))
    return {
        "variant": variant_idx,
        "avg_return": round(avg_return, 2),
        "avg_sharpe": round(avg_sharpe, 3) if isinstance(avg_sharpe, float) and not np.isnan(avg_sharpe) else "N/A",
        "avg_max_dd": round(avg_dd, 2),
        "avg_win_rate": round(avg_win, 1),
        "total_trades": total_trades,
        "avg_profit_factor": round(avg_pf, 2),
        "params": param_str,
    }


def _find_best_variant(rows: list[dict]) -> Optional[dict]:
    """Find the best variant by composite score (return - drawdown/2)."""
    scored = []
    for r in rows:
        if "error" in r:
            continue
        ret = r.get("avg_return", -999)
        dd = abs(r.get("avg_max_dd", 0))
        pf = r.get("avg_profit_factor", 0)
        score = ret - dd * 0.5 + pf * 2
        scored.append((score, r))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _init_trader_decisions_table(db_path: Path) -> None:
    """Create trader_decisions table if it doesn't exist."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trader_decisions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id        TEXT NOT NULL,
                ticker          TEXT,
                action          TEXT NOT NULL CHECK(action IN ('BUY','SELL','HOLD')),
                quantity        REAL NOT NULL DEFAULT 0,
                confidence      REAL NOT NULL DEFAULT 0.5,
                thesis          TEXT DEFAULT '',
                signals         TEXT DEFAULT '[]',
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _log_improve_result(
    db_path: Path,
    trader_type: str,
    tickers: list[str],
    variants: int,
    raw_results: list[dict],
    best_variant: Optional[dict],
) -> None:
    """Log improve run results to the DB strategy_notes table."""
    conn = sqlite3.connect(str(db_path))
    try:
        note = json.dumps({
            "mode": "improve",
            "trader": trader_type,
            "tickers": tickers,
            "variants": variants,
            "best_variant": best_variant,
            "num_runs": len(raw_results),  # total result rows
        })
        conn.execute(
            "INSERT INTO strategy_notes (agent_id, timestamp, note, category, source) VALUES (?, ?, ?, ?, ?)",
            (f"trader-{trader_type}", pd.Timestamp.now().isoformat(), note, "strategy_change", "historical_sim"),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def add_improve_subparser(subparsers) -> None:
    p = subparsers.add_parser("improve", help="Run multi-variant parameter optimization")
    p.add_argument("--trader", required=True, choices=list(TRADER_PARAM_SCHEMAS.keys()),
                   help="Trader personality to optimize")
    p.add_argument("--ticker", required=True,
                   help="Comma-separated ticker symbols (e.g. AAPL,MSFT,GOOG)")
    p.add_argument("--variants", type=int, default=5,
                   help="Number of parameter variants to test (default: 5)")
    p.add_argument("--period", default="3mo",
                   help="Historical period for yfinance (default: 3mo)")
    p.add_argument("--workers", type=int, default=4,
                   help="Parallel workers (default: 4)")


def cmd_improve(args: argparse.Namespace) -> int:
    """Execute the improve mode."""
    trader_type = args.trader.lower()
    tickers = [t.strip().upper() for t in args.ticker.split(",")]
    n_variants = args.variants
    period = args.period
    workers = min(args.workers, n_variants) if n_variants > 0 else 1

    if n_variants < 2:
        print("ERROR: --variants must be >= 2", file=sys.stderr)
        return 1

    print(f"\n{'='*70}")
    print(f"  PARAMETER OPTIMIZATION — {trader_type.upper()}")
    print(f"  Tickers: {', '.join(tickers)}")
    print(f"  Variants: {n_variants}")
    print(f"  Period: {period}")
    print(f"  Workers: {workers}")
    print(f"{'='*70}\n")

    # Show parameter grid
    schemas = TRADER_PARAM_SCHEMAS.get(trader_type, [])
    print("  Parameter ranges:")
    for sp in schemas:
        rng = f"[{sp.min_val}..{sp.max_val}]" if sp.min_val is not None else str(sp.default)
        print(f"    {sp.name:25s} {sp.type:6s} {rng:15s}  {sp.description}")
    print()

    # Build variant tasks
    tasks = []
    for v in range(n_variants):
        tasks.append((trader_type, v, n_variants, tickers, period))

    # Run variants (parallel)
    all_results: list[dict] = []
    start_time = time.time()
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_run_variant, t): t[1] for t in tasks}
        for fut in as_completed(futures):
            variant_results = fut.result()
            all_results.extend(variant_results)
            v_idx = futures[fut]
            # Quick summary for each variant
            valid = [r for r in variant_results if "error" not in r]
            avg_ret = np.mean([r["total_return"] for r in valid]) if valid else -999
            print(f"  ✓ Variant {v_idx+1:2d}/{n_variants} — avg return: {avg_ret:+.2f}%")

    elapsed = time.time() - start_time
    print(f"\n  Completed in {elapsed:.1f}s\n")

    # Build comparison table
    comp_rows: list[dict] = []
    for v_idx in range(n_variants):
        ticker_results = [r for r in all_results if r.get("variant") == v_idx]
        params = _generate_variant_params(trader_type, v_idx, n_variants)
        row = _build_comparison_row(v_idx, ticker_results, params, trader_type)
        comp_rows.append(row)

    # Find best
    best = _find_best_variant(comp_rows)

    # Print comparative results
    print(f"\n{'='*70}")
    print(f"  COMPARATIVE RESULTS — {trader_type.upper()}")
    print(f"{'='*70}")
    header = f"  {'Var':>4s} | {'Avg Ret%':>8s} | {'Sharpe':>7s} | {'Max DD%':>7s} | {'Win%':>5s} | {'Trades':>6s} | {'ProfFact':>8s}"
    sep = f"  {'-'*4}-+-{'-'*8}-+-{'-'*7}-+-{'-'*7}-+-{'-'*5}-+-{'-'*6}-+-{'-'*8}"
    print(header)
    print(sep)
    for row in comp_rows:
        if "error" in row:
            print(f"  {row['variant']:4d}  ERROR: {row['error']}")
            continue
        best_mark = " ← BEST" if best and row["variant"] == best["variant"] else ""
        print(f"  {row['variant']+1:4d} | {row['avg_return']:>+8.2f} | {str(row['avg_sharpe']):>7s} | {row['avg_max_dd']:>+7.2f} | {row['avg_win_rate']:>5.1f} | {row['total_trades']:6d} | {row['avg_profit_factor']:>8.2f}{best_mark}")

    print()

    # Best params detail
    if best:
        print(f"  BEST VARIANT: Variant {best['variant']+1}")
        print(f"    Composite score: return - |DD|/2 + 2×profit_factor")
        print(f"    Avg Return:       {best['avg_return']:+.2f}%")
        print(f"    Avg Sharpe:       {best['avg_sharpe']}")
        print(f"    Avg Max DD:       {best['avg_max_dd']:+.2f}%")
        print(f"    Avg Win Rate:     {best['avg_win_rate']:.1f}%")
        print(f"    Total Trades:     {best['total_trades']}")
        print(f"    Avg Profit Factor:{best['avg_profit_factor']:.2f}")
        print(f"    Parameters:       {best['params']}")
    else:
        print("  No valid results to determine best variant.\n")
        return 1

    print(f"\n  Detailed per-ticker results available in DB (strategy_notes).")
    print(f"{'='*70}\n")

    # Persist
    _init_trader_decisions_table(_DB_PATH)
    _log_improve_result(_DB_PATH, trader_type, tickers, n_variants, all_results, best)
    print(f"  Results logged to {_DB_PATH}\n")

    return 0


# ---------------------------------------------------------------------------
# CLI — backtest
# ---------------------------------------------------------------------------
def cmd_backtest(args: argparse.Namespace) -> int:
    """Execute a backtest run with optional carry-over state."""
    trader_type = args.trader.lower()
    ticker = args.ticker.upper()

    # Use default params
    schemas = TRADER_PARAM_SCHEMAS.get(trader_type, [])
    params = {sp.name: sp.default for sp in schemas}

    print(f"\n{'='*70}")
    print(f"  BACKTEST — {trader_type.upper()} on {ticker}")
    print(f"  Period: {args.period}  |  Capital: ${args.capital:,.0f}")
    print(f"  Carry-over: {'YES' if args.carryover else 'NO (single-pass)'}")
    print(f"{'='*70}\n")

    # Load state if resuming
    initial_state = None
    if args.load_state:
        load_path = Path(args.load_state)
        if not load_path.exists():
            print(f"ERROR: state file not found: {load_path}", file=sys.stderr)
            return 1
        with open(load_path) as f:
            initial_state = SimState.deserialize(f.read())
        print(f"  Loaded state from {load_path}")
        print(f"    Cash: ${initial_state.cash:,.2f}")
        print(f"    Positions: {len(initial_state.positions)}")
        print(f"    Cumulative P&L: ${initial_state.cumulative_realized_pnl:+,.2f}")
        print()

    start_time = time.time()

    if args.carryover or args.load_state or args.save_state:
        # Carry-over mode: state threaded across days
        state = run_backtest_carryover(
            ticker=ticker,
            trader_type=trader_type,
            params=params,
            period=args.period,
            initial_capital=args.capital,
            initial_state=initial_state,
        )
        metrics = compute_metrics_from_state(state)
        elapsed = time.time() - start_time

        print(f"  Completed in {elapsed:.1f}s")
        print(f"\n  RESULTS:")
        print(f"    Total Return:      {metrics['total_return']:+.2f}%")
        print(f"    Sharpe:            {metrics['sharpe']}")
        print(f"    Max Drawdown:      {metrics['max_drawdown']:+.2f}%")
        print(f"    Win Rate:          {metrics['win_rate']:.1f}%")
        print(f"    Trades:            {metrics['num_trades']}")
        print(f"    Profit Factor:     {metrics['profit_factor']:.2f}")
        print(f"    Realized P&L:      ${metrics['cumulative_realized_pnl']:+,.2f}")
        print(f"    Final Cash:        ${metrics['final_cash']:,.2f}")

        # Save state if requested
        if args.save_state:
            save_path = Path(args.save_state)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, "w") as f:
                f.write(state.serialize())
            print(f"\n  State saved to {save_path}")
    else:
        # Legacy mode: single-pass (no state threading)
        result = backtest_trader(ticker, trader_type, params, period=args.period, initial_cash=args.capital)
        elapsed = time.time() - start_time

        if "error" in result:
            print(f"  ERROR: {result['error']}")
            return 1

        print(f"  Completed in {elapsed:.1f}s")
        print(f"\n  RESULTS:")
        print(f"    Total Return:      {result['total_return']:+.2f}%")
        print(f"    Sharpe:            {result['sharpe']}")
        print(f"    Max Drawdown:      {result['max_drawdown']:+.2f}%")
        print(f"    Win Rate:          {result['win_rate']:.1f}%")
        print(f"    Trades:            {result['num_trades']}")
        print(f"    Profit Factor:     {result['profit_factor']:.2f}")
        print(f"    Final Cash:        ${result['final_cash']:,.2f}")

    print(f"\n{'='*70}\n")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="historical_sim",
        description="Paper trading historical simulation & parameter optimization.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True, help="Sub-command")

    add_improve_subparser(subparsers)

    # Backtest subcommand (carry-over enabled)
    bp = subparsers.add_parser("backtest", help="Run historical backtest with carry-over state")
    bp.add_argument("--trader", required=True, choices=list(TRADER_PARAM_SCHEMAS.keys()),
                    help="Trader personality")
    bp.add_argument("--ticker", required=True, help="Ticker symbol (e.g. AAPL)")
    bp.add_argument("--period", default="3mo", help="Historical period (default: 3mo)")
    bp.add_argument("--capital", type=float, default=100_000.0,
                    help="Initial capital (default: 100000)")
    bp.add_argument("--carryover", action="store_true",
                    help="Use carry-over state (thread state across days)")
    bp.add_argument("--save-state", type=str, default=None,
                    help="Path to save final SimState as JSON")
    bp.add_argument("--load-state", type=str, default=None,
                    help="Path to load initial SimState from JSON")

    cp = subparsers.add_parser("compare", help="Compare traders (not implemented)")
    cp.add_argument("--tickers", required=True)

    args = parser.parse_args(argv)

    if args.mode == "improve":
        return cmd_improve(args)
    elif args.mode == "backtest":
        return cmd_backtest(args)
    elif args.mode == "compare":
        print("compare mode not yet implemented", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())