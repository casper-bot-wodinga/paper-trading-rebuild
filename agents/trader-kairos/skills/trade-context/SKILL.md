---
name: trade-context
description: Gather current positions, portfolio state, and market data — the callable "gather tool" the spec promises
---

# Trade Context Skill

Call this skill to gather your current trading context during a tick. This is the callable "tool" described in the architecture spec — you proactively fetch live data, not just receive pre-assembled context from `tick_prompt.py`.

## Tool Calls (use these during tick evaluation)

### 1. Portfolio State
`data-bus__get_portfolio(trader_id="kairos")`
Returns: cash, equity, buying_power, positions, daily P&L, drawdown

### 2. Market Quotes (with RSI/OHLCV)
`data-bus__get_quotes(symbols=["KO","F","INTC","PFE","WBD","VZ","CSCO","HPQ","KHC","WBA"])`
Returns: RSI, MACD, MA20, price, volume, volume_ratio for each symbol

### 3. Technical Scan (multi-timeframe)
`data-bus__get_technical_scan(symbol="KO")`
Returns: 15m/1h/4h/1d RSI, MACD, Bollinger Bands per symbol

### 4. Market Regime (ML signal)
`data-bus__get_market_regime()`
Returns: regime (SUSTAINABLE/CHOPPY/EXHAUSTED/UNREACHABLE), confidence

### 5. Sentiment
`data-bus__get_sentiment(symbol="KO")`
Returns: FinBERT sentiment score, label, confidence

### 6. Macro / Fear & Greed
`data-bus__get_macro()`
Returns: Fear & Greed index, yield curve, FOMC rates

### 7. Options Flow
`data-bus__get_flow(symbol="KO")`
Returns: unusual options activity (sweeps, dark pool, blocks)

### 8. Insider Trading
`data-bus__get_insiders(symbol="KO")`
Returns: recent SEC Form 4 filings

### 9. Self-Stats
`data-bus__get_self_stats(agent_id="kairos")`
Returns: today's P&L, win rates by signal/sector, confidence calibration

### 10. Risk Scoring
`data-bus__get_risk(symbol="KO")`
Returns: concentration, VaR, correlation scores

## Formatting Your Context

After gathering data, format it as structured text for your decision:

```
## Portfolio
Cash: $X,XXX.XX | Equity: $X,XXX.XX | Daily PnL: +$XX.XX (+X.X%)
Positions: TICKER (entry $X.XX, now $X.XX, PnL +$XX.XX, held X days)

## Market
Regime: TRENDING (confidence 0.85)
Watchlist: KO RSI 62 MACD bullish vol 1.3x, F RSI 45 MACD neutral vol 0.9x

## Signals
Sentiment: KO +0.32 (bullish), INTC -0.15 (bearish)
F&G: 35 (Fear) — contrarian opportunity
```

## When to Call

- **Every tick**: You receive pre-assembled context from `tick_prompt.py`. That's your baseline.
- **Mid-tick refresh**: If you need fresh data (price moved significantly), call the tools directly.
- **Heartbeat maintenance**: During Cycle 2, call `data-bus__get_self_stats` to assess performance.
- **New position research**: Call `data-bus__get_technical_scan` + `data-bus__get_sentiment` for tickers you're considering.

## Pre-Assembled Context

`tick_prompt.py` pre-assembles context for every tick. The data above is the same data injected into each tick. Call these tools directly when you need fresh data or deeper analysis beyond the pre-assembled snapshot.