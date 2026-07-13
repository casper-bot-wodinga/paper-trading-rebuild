---
name: market-data
description: Fetch quotes, technicals, and regime signals via the data bus
---

# Market Data

All market data comes from the data bus at `localhost:5000`. Do NOT fetch your own quotes or technicals directly from APIs.

## Primary Endpoints

```bash
# Quotes with OHLCV + RSI
curl -s "http://localhost:5000/quotes?symbols=SPY,NVDA,AAPL"

# Multi-timeframe technical scan (15m/1h/4h/1d)
curl -s "http://localhost:5000/technical-scan?symbol=NVDA"

# Market regime (bullish/bearish/choppy/sustainable/exhausted)
curl -s "http://localhost:5000/market-regime"

# Fear & Greed index
curl -s "http://localhost:5000/macro"

# News (Alpaca API — live, searchable by ticker)
curl -s "http://localhost:5000/news?symbol=AAPL&limit=5"

# News cache (RSS aggregation — all feeds, stored in Postgres)
curl -s "http://localhost:5000/news-cache?limit=10&source=marketwatch"

# News search (full-text in RSS cache)
curl -s "http://localhost:5000/news/search?q=AAPL"
```

## When to Call

- Every tick: `/quotes` for watchlist prices
- Entry decision: `/technical-scan` on candidate
- Regime check: `/market-regime` at start of each tick
- Macro context: `/macro` once per session

## What NOT to Do

- Don't call these more than once per tick (data bus has server-side cache)
- Don't fetch quotes from Alpaca/Finnhub directly
- Don't compute RSI/MACD yourself — data bus provides them
