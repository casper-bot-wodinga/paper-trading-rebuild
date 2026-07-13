# Heartbeat — Tick Checklist

1. Market open? Check trading-hours skill. If closed → skip.
2. Portfolio → `data-bus__get_portfolio(trader_id="kairos")`
3. Market snapshot → quotes for watchlist, regime, macro
4. Read bankroll → `read bankroll.md`
5. Self stats → `data-bus__get_self_stats` — check win rates
6. Research candidates → spawn research subagent if cash > 20%
7. Decide → BUY/SELL/HOLD with thesis (respect ceiling)
8. Execute → executor.py script (via exec)
9. Update bankroll + thesis files
10. Journal entry → append to `journal/YYYY-MM-DD.md`
11. HEARTBEAT_OK