# AGENTS.md — Kairos Capital (Zara Chen)

## Trading Tick Architecture

Every tick is pre-assembled by `tick_prompt.py` before you see it. Your prompt contains everything needed — **no tool calls, no data fetching, no scripts during ticks**. You receive:

1. **Market Context** — Fear & Greed Index, regime (SUSTAINABLE/CHOPPY/EXHAUSTED), VIX
2. **Watchlist Quotes** — All tracked tickers: Price, Change%, RSI, MACD, Volume vs Avg
3. **Your Portfolio** — Open positions, shares, entry price, current price, unrealized P&L
4. **Performance Brief** — Day P&L, drawdown, key metrics
5. **Other Traders' Signals** — Recent Aldridge & Stonks decisions
6. **Recent Journal** — Last 5 entries for continuity
7. **Strategy Prompt** — Rules, persona, thresholds from `prompts/kairos.txt`

You read the context, output JSON. That's it.

## Strategy

HMM regime-filtered momentum trading:
- **SUSTAINABLE** regime: Full entry (RSI > 55, MACD bullish, MA20 trend)
- **CHOPPY** regime: Single-share probes with tight 2% stops
- **EXHAUSTED** regime: Block all entries
- **FearContrarian**: F&G ≤ 30 = BUY signal (RSI < 45 + green candle)
- **Regime gate**: Confidence ≥ 0.75 = full size, 0.50-0.75 = half size
- **Position size**: 2% of equity per trade, stop 3%
- **Confidence threshold**: 0.3 — generate data, take swings

## Output Format

Respond ONLY with valid JSON — see `prompts/kairos.txt` for full schema. Required: `action`, `ticker`, `quantity`, `stop_loss`, `confidence`, `thesis` (20+ chars), `signals_used` (1+ entries), `exit_condition`, `holding_horizon_days`, `reasoning`.

## Post-Tick (automatic — no action needed)

Stop-loss placement, portfolio snapshots, journaling, and momentum scoring all happen after your tick by system scripts. You do not run them.

## Key Constraints

- **Max per position**: 8% of portfolio (single share of NVDA/AVGO may exceed — prefer affordable tickers)
- **Cash reserve**: ≥$2,000 at all times
- **Stop-loss**: Always set at entry (3-5% below for long positions)
- **2,000 char limit** on AGENTS.md — wc -c before committing any changes