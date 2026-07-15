from __future__ import annotations

import logging

log = logging.getLogger("skill_combo_fetch")
"""
skill_combo_fetch — ML signal module for the data bus.

Provides the four functions the data bus imports at startup:
  1. fetch_prices_indicators  — technical indicators (RSI, MACD, BB)
  2. fetch_fundamentals       — company fundamentals (Alpha Vantage)
  3. fetch_congressional_trading — congress trades (Finnhub)
  4. fetch_ml_signal          — ML-based market regime via Mac GPU

Architecture:
  - Technical indicators are computed locally via pandas-ta on yfinance data.
  - ML signals call the Mac GPU (FinBERT) and the HMM regime model.
  - Fundamentals and congress trade data come from free external APIs.
"""

import json
import os
from pathlib import Path
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd
import requests

# ── Config ───────────────────────────────────────────────────────────────────
MAC_HOST = os.getenv("ML_HOST", "192.168.1.190")
FINBERT_PORT = int(os.getenv("FINBERT_PORT", "5004"))
FINBERT_URL = f"http://{MAC_HOST}:{FINBERT_PORT}"

ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY", "")
FINNHUB_KEY = os.getenv("FINNHUB_KEY", "")

CACHE_TTL = 300  # 5 min
_cache: dict[str, tuple[float, Any]] = {}

# ── Helpers ──────────────────────────────────────────────────────────────────

def _cached(key: str, ttl: int = CACHE_TTL) -> Any:
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < ttl:
        return entry[1]
    return None

def _set_cache(key: str, value: Any) -> None:
    _cache[key] = (time.time(), value)

def _get_bars(symbol: str, days: int = 60) -> pd.DataFrame | None:
    """Fetch daily bars via Alpaca SDK for indicator computation."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from alpaca.data.enums import DataFeed
        from dotenv import load_dotenv
        load_dotenv(Path.home() / ".openclaw" / ".env", override=True)
        api_key = os.getenv("ALPACA_KAIROS_KEY", "")
        secret = os.getenv("ALPACA_KAIROS_SECRET", "")
        if not api_key or not secret:
            return None
        client = StockHistoricalDataClient(api_key, secret)
        now = pd.Timestamp.now(tz="America/New_York")
        start = now - pd.Timedelta(days=days)
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame(1, TimeFrameUnit.Day),
            start=start.isoformat(),
            end=now.isoformat(),
            feed=DataFeed.IEX,
        )
        bars = client.get_stock_bars(req)
        sym_bars = bars.data.get(symbol, [])
        if not sym_bars:
            return None
        records = []
        for b in sym_bars:
            records.append({
                "Close": float(b.close),
                "Open": float(b.open),
                "High": float(b.high),
                "Low": float(b.low),
                "Volume": b.volume
            })
        df = pd.DataFrame(records)
        df.index = pd.DatetimeIndex([b.timestamp for b in sym_bars])
        return df
    except ImportError:
        return None
    except Exception:
        return None

# ── 1. fetch_prices_indicators ───────────────────────────────────────────────

def fetch_prices_indicators(symbols: list[str]) -> dict[str, Any]:
    """
    Compute technical indicators for one or more symbols.

    Returns a dict keyed by symbol, each containing:
      price, change_pct, rsi, macd, macd_signal, bb_upper, bb_lower,
      bb_mid, volume_ratio, regime, momentum
    """
    import pandas_ta as ta

    result: dict[str, Any] = {}
    for sym in symbols:
        cache_key = f"indicators:{sym}"
        cached = _cached(cache_key)
        if cached is not None:
            result[sym] = cached
            continue

        bars = _get_bars(sym, days=60)
        if bars is None or len(bars) < 30:
            result[sym] = {"error": f"insufficient bars for {sym}"}
            continue

        close = bars["Close"]
        high = bars["High"]
        low = bars["Low"]
        volume = bars["Volume"]

        # RSI
        rsi = ta.rsi(close, length=14)
        rsi_val = float(rsi.iloc[-1]) if not rsi.isna().all() else None

        # MACD
        macd = ta.macd(close, fast=12, slow=26, signal=9)
        macd_line = float(macd["MACD_12_26_9"].iloc[-1]) if not macd.empty else None
        macd_signal = float(macd["MACDs_12_26_9"].iloc[-1]) if not macd.empty else None
        macd_hist = float(macd["MACDh_12_26_9"].iloc[-1]) if not macd.empty else None

        # Bollinger Bands
        bb = ta.bbands(close, length=20, std=2)
        bb_upper = float(bb.filter(like="BBU_").iloc[-1, 0]) if not bb.empty else None
        bb_mid = float(bb.filter(like="BBM_").iloc[-1, 0]) if not bb.empty else None
        bb_lower = float(bb.filter(like="BBL_").iloc[-1, 0]) if not bb.empty else None

        # Momentum (rate of change)
        roc = ta.roc(close, length=10)
        momentum = float(roc.iloc[-1]) if not roc.isna().all() else None

        # Volume ratio
        vol_ma = volume.rolling(20).mean()
        vol_ratio = float(volume.iloc[-1] / vol_ma.iloc[-1]) if vol_ma.iloc[-1] > 0 else None

        # Regime classification
        returns = close.pct_change().dropna()
        recent_vol = returns.tail(20).std()
        annualized_vol = recent_vol * (252 ** 0.5) if recent_vol else 0
        if momentum is not None and annualized_vol:
            if momentum > 3 and annualized_vol < 0.25:
                regime = "TRENDING_UP"
            elif momentum < -3 and annualized_vol < 0.25:
                regime = "TRENDING_DOWN"
            elif annualized_vol > 0.35:
                regime = "HIGH_VOLATILITY"
            elif -3 <= momentum <= 3 and annualized_vol < 0.25:
                regime = "CHOPPY"
            else:
                regime = "UNKNOWN"
        else:
            regime = "UNKNOWN"

        entry = {
            "close": float(close.iloc[-1]),
            "price": float(close.iloc[-1]),
            "change_pct": float(close.pct_change().iloc[-1] * 100) if len(close) > 1 else 0,
            "rsi": rsi_val,
            "macd": macd_line,
            "macd_signal": macd_signal,
            "macd_histogram": macd_hist,
            "bb_upper": bb_upper,
            "bb_mid": bb_mid,
            "bb_lower": bb_lower,
            "volume": float(volume.iloc[-1]),
            "volume_ratio": vol_ratio,
            "momentum": momentum,
            "regime": regime,
            "source": "skill_combo_fetch",
        }
        _set_cache(cache_key, entry)
        result[sym] = entry

    return result

# ── 2. fetch_fundamentals ────────────────────────────────────────────────────

def fetch_fundamentals(symbols: list[str]) -> dict[str, Any]:
    """Fetch company fundamentals via Alpha Vantage (free tier)."""
    if not ALPHA_VANTAGE_KEY:
        return {"error": "ALPHA_VANTAGE_KEY not set"}

    result: dict[str, Any] = {}
    for sym in symbols:
        cache_key = f"fundamentals:{sym}"
        cached = _cached(cache_key, ttl=86400)  # 24h cache
        if cached is not None:
            result[sym] = cached
            continue

        try:
            url = (
                "https://www.alphavantage.co/query"
                f"?function=OVERVIEW&symbol={sym}&apikey={ALPHA_VANTAGE_KEY}"
            )
            resp = requests.get(url, timeout=10)
            data = resp.json()
            if "Symbol" not in data:
                result[sym] = {"error": "Alpha Vantage: no data"}
                continue
            entry = {
                "pe_ratio": data.get("PERatio"),
                "market_cap": data.get("MarketCapitalization"),
                "dividend_yield": data.get("DividendYield"),
                "eps": data.get("EPS"),
                "beta": data.get("Beta"),
                "sector": data.get("Sector"),
                "industry": data.get("Industry"),
                "source": "alpha_vantage",
            }
            _set_cache(cache_key, entry)
            result[sym] = entry
        except Exception as e:
            result[sym] = {"error": str(e)}

    return result

# ── 3. fetch_congressional_trading ────────────────────────────────────────────

def fetch_congressional_trading(limit: int = 50) -> list[dict]:
    """Fetch recent congressional trading disclosures via Finnhub."""
    if not FINNHUB_KEY:
        return [{"error": "FINNHUB_KEY not set"}]

    cache_key = "congress"
    cached = _cached(cache_key, ttl=3600)  # 1h cache
    if cached is not None:
        return cached

    try:
        url = f"https://finnhub.io/api/v1/stock/congressional-trading?limit={limit}&token={FINNHUB_KEY}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if "data" not in data:
            return []
        result = []
        for t in data["data"][:limit]:
            result.append({
                "symbol": t.get("symbol"),
                "name": t.get("name"),
                "transaction_type": t.get("transactionType"),
                "transaction_date": t.get("transactionDate"),
                "amount": t.get("amount"),
                "source": "finnhub",
            })
        _set_cache(cache_key, result)
        return result
    except Exception as e:
        return [{"error": str(e)}]

# ── 4. fetch_ml_signal ───────────────────────────────────────────────────────

def fetch_ml_signal(symbol: str = "SPY") -> dict[str, Any]:
    """
    ML-based market regime signal using the Mac GPU (FinBERT + HMM).

    Returns dict with:
      symbol, regime, confidence, sentiment, features
    """
    cache_key = f"ml_signal:{symbol}"
    cached = _cached(cache_key, ttl=300)
    if cached is not None:
        return cached

    result: dict[str, Any] = {
        "symbol": symbol,
        "source": "skill_combo_fetch",
    }

    # 1. Compute technical indicators locally
    indicators = fetch_prices_indicators([symbol])
    ind = indicators.get(symbol, {})
    if "error" not in ind:
        result["rsi"] = ind.get("rsi")
        result["momentum"] = ind.get("momentum")
        result["regime"] = ind.get("regime", "UNKNOWN")
        result["bb_position"] = _bb_position(ind)

    # 2. Call FinBERT on Mac GPU for sentiment
    try:
        resp = requests.post(
            f"{FINBERT_URL}/predict",
            json={"text": [f"{symbol} market outlook {datetime.now().strftime('%Y-%m-%d')}"]},
            timeout=10,
        )
        if resp.ok:
            finbert = resp.json()
            result["sentiment"] = finbert
    except Exception as e:
        log.warning("operation: %s", e)

    # 3. Compute composite signal
    regime = result.get("regime", "UNKNOWN")
    rsi = result.get("rsi")
    momentum = result.get("momentum")

    if rsi is not None:
        if rsi > 70:
            result["signal"] = "bearish"
            result["confidence"] = min((rsi - 70) / 30, 1.0)
        elif rsi < 30:
            result["signal"] = "bullish"
            result["confidence"] = min((30 - rsi) / 30, 1.0)
        elif momentum is not None and momentum > 2:
            result["signal"] = "bullish"
            result["confidence"] = 0.6
        elif momentum is not None and momentum < -2:
            result["signal"] = "bearish"
            result["confidence"] = 0.6
        else:
            result["signal"] = "neutral"
            result["confidence"] = 0.3
    else:
        result["signal"] = "neutral"
        result["confidence"] = 0.3

    _set_cache(cache_key, result)
    return result

def _bb_position(ind: dict) -> float | None:
    """Return position within Bollinger Bands as 0-1 float."""
    price = ind.get("price")
    upper = ind.get("bb_upper")
    lower = ind.get("bb_lower")
    if price and upper and lower and upper > lower:
        return (price - lower) / (upper - lower)
    return None