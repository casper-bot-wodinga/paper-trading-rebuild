# Heartbeat — Tick Checklist

**Mode awareness**: Check mode first — `python3 ~/.openclaw/workspace/scripts/mode_manager.py {trader}`

## LIVE Mode (market hours 9:30-4:00)

1. Portfolio → `data-bus__get_portfolio(trader_id="{trader}")`
2. Market snapshot → `data-bus__get_quotes` for positions + watchlist
3. Regime → `data-bus__get_market_regime`
4. Sentiment on candidates → `data-bus__get_sentiment`
5. Read bankroll → `read bankroll.md` — respect the ceiling
6. Check thesis files → `read positions/*.md` if present
7. Self stats → `data-bus__get_self_stats` — calibrate conviction
8. Decide → BUY/SELL/HOLD
9. **Execute** → `python3 ~/.openclaw/workspace/scripts/place_order.py {trader} BUY TICKER QTY`
   - place_order.py auto-saves to `trading.decisions` (copy-on-write)
   - API keys: `{TRADER}_API_KEY` / `{TRADER}_SECRET_KEY`
10. Update bankroll + thesis files
11. Journal → append to `journal/YYYY-MM-DD.md`
12. HEARTBEAT_OK

## HISTORICAL Mode (off-hours)

1. Same analysis flow (data-bus tools still work)
2. Decide what you WOULD trade
3. **Do NOT execute real trades**
4. Save to `trading.historical_decisions` via exec:
   `python3 -c "import psycopg2; conn=psycopg2.connect('host=192.168.1.179 port=5433 dbname=trading user=trader'); ..."`
5. Deeper reflection → update MEMORY.md, strategies/active.md
6. HEARTBEAT_OK

## Self-Check

Run `python3 ~/.openclaw/workspace/scripts/trader_check.py {trader}` to verify readiness.
