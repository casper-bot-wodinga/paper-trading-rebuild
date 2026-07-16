---
name: trade-context
description: Gather current positions, portfolio state, and market data — the callable "gather tool" the spec promises
---

# Trade Context Skill

Call this skill to gather your current trading context during a tick. This is the callable "tool" described in the architecture spec — you proactively fetch live data, not just receive pre-assembled context from `tick_prompt.py`.

## Tool Calls (use these during tick evaluation)

### 1. Portfolio State
`data-bus__get_portfolio(trader_id="stonks")`
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
Returns: FinBERT + Praesentire bilingual sentiment score, label, confidence

### 6. Sentiment Divergence (EN vs ZH)
`data-bus__get_sentiment_divergence(symbol="KO")`
Returns: cross-language sentiment divergence from Praesentire

### 7. Macro / Fear & Greed
`data-bus__get_macro()`
Returns: Fear & Greed index, yield curve, FOMC rates

### 8. Options Flow
`data-bus__get_flow(symbol="KO")`
Returns: unusual options activity (sweeps, dark pool, blocks) — crowd sentiment proxy

### 9. Insider Trading
`data-bus__get_insiders(symbol="KO")`
Returns: recent SEC Form 4 filings

### 10. Self-Stats
`data-bus__get_self_stats(agent_id="stonks")`
Returns: today's P&L, win rates by signal/sector, confidence calibration

### 11. Risk Scoring
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
Watchlist: KO RSI 62 MACD bullish vol 1.3x, F RSI 45 MACD neutral vol 0.9x, ...

## Signals
Social: KO trending bullish (0.72), INTC bearish (-0.31)
Sentiment Divergence: KO EN +0.32 / ZH -0.15 — divergence alert
Fear & Greed: 52 (Neutral)
```

## When to Call

- **Every tick**: You receive pre-assembled context from `tick_prompt.py`. That's your baseline.
- **Mid-tick refresh**: If social sentiment shifts suddenly, call tools for fresh data.
- **Heartbeat maintenance**: During Cycle 2, call `data-bus__get_self_stats` to assess performance.
- **Social divergence**: Call `data-bus__get_sentiment_divergence` when crowd sentiment seems off.
- **New position research**: Call `data-bus__get_quotes` + `data-bus__get_flow` for crowd momentum.

## Pre-Assembled Context

`tick_prompt.py` pre-assembles context for every tick. The data above is the same data injected into each tick. Call these tools directly when you need fresh data or deeper analysis beyond the pre-assembled snapshot.