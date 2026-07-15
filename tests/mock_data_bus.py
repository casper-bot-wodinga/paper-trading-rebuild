#!/usr/bin/env python3
"""Lightweight mock data bus for CI/testing.

Serves fixture data for every endpoint the smoke tests hit, so CI can
run the full data_bus_smoke test suite without any external dependencies.

Usage:
    python3 tests/mock_data_bus.py [--port 15000]

The test module reads DATA_BUS_URL env var or defaults to localhost:5000.
In CI, start this first, then run:
    DATA_BUS_URL=http://localhost:15000 python3 -m pytest tests/test_data_bus_smoke.py
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

try:
    from flask import Flask, jsonify, request
except ImportError:
    print("ERROR: Flask not installed. Run: pip install flask", file=sys.stderr)
    sys.exit(1)

app = Flask(__name__)

NOW = datetime.now(timezone.utc).isoformat()

# ── Fixtures ──────────────────────────────────────────────────────────────────

HEALTH_FIXTURE = {
    "status": "ok",
    "service": "data-bus",
    "schedulers": [
        {"name": "quotes",   "interval": 5,  "mode": "market",
         "last_run": NOW, "run_count": 100},
        {"name": "crypto",   "interval": 10, "mode": "market",
         "last_run": NOW, "run_count": 60},
        {"name": "news",     "interval": 180,"mode": "market",
         "last_run": NOW, "run_count": 20},
        {"name": "congress", "interval": 1800,"mode": "market",
         "last_run": NOW, "run_count": 5},
        {"name": "signals_gc","interval": 60, "mode": "market",
         "last_run": NOW, "run_count": 100},
        {"name": "momentum",  "interval": 300,"mode": "market",
         "last_run": NOW, "run_count": 10},
        {"name": "macro",     "interval": 21600,"mode": "market",
         "last_run": NOW, "run_count": 2},
        {"name": "earnings",  "interval": 3600,"mode": "market",
         "last_run": NOW, "run_count": 3},
        {"name": "fear_greed","interval": 1800,"mode": "market",
         "last_run": NOW, "run_count": 8},
        {"name": "flow",      "interval": 300,"mode": "market",
         "last_run": NOW, "run_count": 10},
        {"name": "insiders",  "interval": 1800,"mode": "market",
         "last_run": NOW, "run_count": 4},
        {"name": "sentiment", "interval": 300,"mode": "market",
         "last_run": NOW, "run_count": 10},
    ],
    "cache_stats": {
        "entries": [
            "crypto:ETH/USD", "crypto:BTC/USD",
            "flow:latest", "sentiment:AAPL",
        ],
        "keys": 9,
    },
    "uptime_seconds": 12345,
    "tracked_symbols": 9,
    "started_at": NOW,
}

QUOTES_FIXTURE = {
    "AAPL": {
        "close": 198.50, "open": 197.20, "high": 199.80, "low": 196.90,
        "volume": 45000000, "source": "mock", "stale": False,
        "quote_age_seconds": 2.5, "cached_at": NOW,
    },
    "SPY": {
        "close": 578.30, "open": 577.10, "high": 579.00, "low": 576.80,
        "volume": 32000000, "source": "mock", "stale": False,
        "quote_age_seconds": 2.5, "cached_at": NOW,
    },
    "TSLA": {
        "close": 262.40, "open": 260.10, "high": 264.00, "low": 259.50,
        "volume": 28000000, "source": "mock", "stale": False,
        "quote_age_seconds": 2.5, "cached_at": NOW,
    },
}

CRYPTO_FIXTURE = {
    "BTC/USD": {"price": 87650.00, "timestamp": NOW, "source": "mock"},
}

SENTIMENT_FIXTURE = {
    "symbol": "AAPL",
    "sentiment": {"compound": 0.12, "positive": 0.35, "negative": 0.10, "neutral": 0.55},
    "source": "mock_sentiment",
}

FEAR_GREED_FIXTURE = {
    "fear_greed": {"value": 45, "classification": "Fear"},
    "source": "mock_fear_greed",
}

NEWS_FIXTURE = [
    {"headline": "Apple Reports Record Quarterly Revenue",
     "source": "Mock News", "url": "https://example.com/aapl-q1",
     "created_at": NOW, "symbols": ["AAPL"]},
    {"headline": "New iPhone Launch Expected Next Month",
     "source": "Mock Tech", "url": "https://example.com/iphone",
     "created_at": NOW, "symbols": ["AAPL"]},
]

SIGNALS_FIXTURE = {"signals": [], "count": 0}

MOMENTUM_FIXTURE = {
    "avg_composite_z": 0.35,
    "signal": "cross_sectional_momentum",
    "top_buys": ["AAPL", "MSFT"],
    "top_avoids": ["TSLA"],
    "num_ranked": 30,
    "market_regime": "neutral",
}

CONGRESS_FIXTURE = {
    "congress_trades": [
        {"ticker": "AAPL", "type": "buy", "amount": "15000",
         "date": "2026-07-10", "representative": "Test Rep"},
    ],
    "source": "mock_congress",
}

INSIDERS_FIXTURE = {
    "insiders": {
        "data": [{"filing_date": "2026-07-14", "issuer": "AAPL",
                  "transaction_type": "Sell", "shares": 1000}],
        "fetched_at": NOW,
    },
    "source": "mock_insiders",
}

FLOW_FIXTURE = {
    "flow": {
        "flows": [
            {"summary": "Bullish call sweep on AAPL",
             "tickers": ["AAPL"], "title": "Call Sweep"},
        ],
    },
}

EARNINGS_FIXTURE = {
    "earnings": {
        "AAPL": [
            {"date": "2026-07-28", "eps_estimate": 2.34, "eps_actual": None},
        ],
    },
    "source": "mock_earnings",
}

MACRO_FIXTURE = {
    "macro": {
        "indicators": {
            "CPI":      {"value": 3.2, "date": "2026-06", "series_id": "CPI"},
            "GDP":      {"value": 2.8, "date": "2026-Q2", "series_id": "GDP"},
            "DGS10":    {"value": 4.15, "date": "2026-07-15", "series_id": "DGS10"},
            "DGS2":     {"value": 3.85, "date": "2026-07-15", "series_id": "DGS2"},
            "FOMC_lower": {"value": 4.25, "date": "2026-07-15", "series_id": "FEDFUNDS"},
            "FOMC_upper": {"value": 4.50, "date": "2026-07-15", "series_id": "FEDFUNDS"},
        },
    },
}

SOURCE_QUALITY_FIXTURE = {
    "sources": [
        {"name": "mock_news", "accuracy": 0.85, "total_predictions": 100},
    ],
    "count": 1,
}

# ── Routes ────────────────────────────────────────────────────────────────────


@app.route("/health")
def health():
    return jsonify(HEALTH_FIXTURE)


@app.route("/quotes")
def quotes():
    symbols_str = request.args.get("symbols", "")
    symbols = [s.strip().upper() for s in symbols_str.split(",") if s.strip()]
    result = {}
    for sym in symbols:
        if sym in QUOTES_FIXTURE:
            result[sym] = dict(QUOTES_FIXTURE[sym])
    return jsonify({
        "quotes": result,
        "cached": len(result),
        "fetched_live": 0,
    })


@app.route("/crypto")
def crypto():
    symbols_str = request.args.get("symbols", "")
    symbols = [s.strip().upper() for s in symbols_str.split(",") if s.strip()]
    result = {}
    for sym in symbols:
        if sym in CRYPTO_FIXTURE:
            result[sym] = dict(CRYPTO_FIXTURE[sym])
    return jsonify({
        "crypto": result,
        "cached": len(result),
        "fetched_live": 0,
    })


@app.route("/sentiment")
def sentiment():
    symbol = request.args.get("symbol", "").strip().upper() or "AAPL"
    return jsonify({
        "symbol": symbol,
        "sentiment": dict(SENTIMENT_FIXTURE["sentiment"]),
        "source": SENTIMENT_FIXTURE["source"],
    })


@app.route("/fear_greed")
def fear_greed():
    return jsonify(FEAR_GREED_FIXTURE)


@app.route("/news")
def news():
    return jsonify({
        "symbol": request.args.get("symbol", "").strip().upper() or "all",
        "news": NEWS_FIXTURE,
        "source": "mock_news",
    })


@app.route("/fundamentals")
def fundamentals():
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol parameter required"}), 400
    # Return 404 with error shape as the test expects
    return jsonify({
        "error": f"No fundamentals available for {symbol}",
        "symbol": symbol,
        "fundamentals": None,
    }), 404


@app.route("/signals")
def signals():
    return jsonify(SIGNALS_FIXTURE)


@app.route("/momentum")
def momentum():
    return jsonify(MOMENTUM_FIXTURE)


@app.route("/congress")
def congress():
    return jsonify(CONGRESS_FIXTURE)


@app.route("/insiders")
def insiders():
    return jsonify(INSIDERS_FIXTURE)


@app.route("/flow")
def flow():
    return jsonify(FLOW_FIXTURE)


@app.route("/earnings")
def earnings():
    symbol = request.args.get("symbol", "").strip().upper()
    data = dict(EARNINGS_FIXTURE)
    return jsonify(data)


@app.route("/macro")
def macro():
    return jsonify(MACRO_FIXTURE)


@app.route("/source-quality")
def source_quality():
    return jsonify(SOURCE_QUALITY_FIXTURE)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock data bus for testing")
    parser.add_argument("--port", type=int, default=15000, help="Port to listen on")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    args = parser.parse_args()
    print(f"Mock data bus starting on {args.host}:{args.port}", file=sys.stderr)
    app.run(host=args.host, port=args.port, debug=False)
