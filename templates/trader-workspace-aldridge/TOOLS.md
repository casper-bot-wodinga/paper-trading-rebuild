## Data Bus (built-in OpenClaw tools)
- `data-bus__get_quotes` — OHLCV + RSI for watchlist symbols
- `data-bus__get_technical_scan` — multi-TF (15m/1h/4h/1d) scan
- `data-bus__get_market_regime` — ML regime classifier
- `data-bus__get_macro` — fear & greed, rates, macro overlay
- `data-bus__get_sentiment` — FinBERT + bilingual sentiment
- `data-bus__get_portfolio` — live positions, P&L, cash
- `data-bus__get_self_stats` — own win rates by signal/sector/regime

## Alpaca Executor (via exec tool)
`python3 ~/projects/paper-trading-rebuild/scripts/executor.py --account {trader} --action BUY|SELL --ticker SYM --qty N`

## Bankroll
- `read bankroll.md` — current ceiling at start of tick
- Win a closed trade → ceiling × 1.01 (1% increase)
- Lose a closed trade → ceiling × 0.99 (1% decrease)
- Ceiling is the max, not the target. $0 is valid.

## Journal (append-only workspace file)
Append to `journal/YYYY-MM-DD.md` in workspace. Never edit.

## Data Bus HTTP (fallback)
`curl -s "http://docker.klo:5000/quotes?symbols=SPY,NVDA,AAPL"`

## Skills Loaded
- stock-analysis: RSI, MACD, value metrics
- trade-execution: buy/sell with risk checks
- market-data: data bus conventions
- fundamentals: P/E, EPS, balance sheet
- trading-hours: market open/close check