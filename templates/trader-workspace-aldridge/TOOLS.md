## Data Bus (built-in OpenClaw tools — all data flow)
The data bus handles ALL market data. No external API calls needed.

- `data-bus__get_quotes` — OHLCV + RSI + MACD + BB for symbols
- `data-bus__get_technical_scan` — multi-TF scan (15m/1h/4h/1d)
- `data-bus__get_market_regime` — ML regime classifier (bullish/bearish/choppy)
- `data-bus__get_macro` — fear & greed, rates, macro overlay
- `data-bus__get_sentiment` — FinBERT + bilingual sentiment
- `data-bus__get_sentiment_divergence` — EN vs ZH sentiment gap
- `data-bus__get_insiders` — SEC Form 4 filings
- `data-bus__get_flow` — unusual options flow
- `data-bus__get_risk` — concentration, VaR, correlation
- `data-bus__get_portfolio` — live positions, P&L, cash
- `data-bus__get_self_stats` — win rates by signal/sector/regime

## Trade Execution (copy-on-write)
```
python3 ~/.openclaw/workspace/scripts/place_order.py {trader} BUY TICKER QTY
```
- Places Alpaca market order (paper trading)
- **Auto-saves decision to `trading.decisions`** — copy-on-write
- API keys: `{TRADER}_API_KEY` / `{TRADER}_SECRET_KEY`
- Bracket orders supported (add stop_loss + take_profit)

## Mode Management
```
python3 ~/.openclaw/workspace/scripts/mode_manager.py {trader}        # check mode
python3 ~/.openclaw/workspace/scripts/mode_manager.py {trader} auto   # auto-detect
```
- LIVE (9:30-4:00 ET): real trades → `trading.decisions`
- HISTORICAL (off-hours): sim trades → `trading.historical_decisions`

## Self-Check
```
python3 ~/.openclaw/workspace/scripts/trader_check.py {trader}
```
7-point readiness check: API keys, Alpaca, portfolio, data bus, order API, DB, open orders.

## Bankroll
- `read bankroll.md` — current ceiling at start of tick
- Win a closed trade → ceiling × 1.01 (1% increase)
- Lose a closed trade → ceiling × 0.99 (1% decrease)
- Ceiling is max, not target. $0 is valid.

## Journal (append-only workspace file)
Append to `journal/YYYY-MM-DD.md`. Never edit. Character-flavored.

## Skills Loaded
- trade-execution — Alpaca order placement via exec
- market-data — data bus conventions
- trading-hours — market open/close check
- persona-strategy — trader-specific strategy rules
