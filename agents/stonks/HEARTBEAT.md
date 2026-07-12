# Stonks Heartbeat

Read `skills/persona-strategy/SKILL.md` for full strategy rules.

**Core flow:**
0. Check inbox — `curl -s "http://localhost:8080/inbox?agent=stonks"` — respond to any pending Hermes messages
1. Portfolio check — `python3 src/skill_portfolio.py --account stonks`
   - Verify freshness: the output includes a `freshness` field. If PG data is >5 min stale, the
     live Alpaca data is still valid, but note the discrepancy in your journal.
2. Social pulse — scan community chatter on positions and watchlist
3. **Stock discovery** — scan Reddit/Bluesky/Stocktwits for trending tickers. Check unusual options flow (`GET /flow`). Propose at least 1 new ticker with community momentum. Log discovery to `strategy_notes/<DATE>_discovery.md`.
4. Data bus — flow, fear & greed, earnings calendar
5. Pre-trade gate — `python3 src/stonks_entry_gate.py` enforces entry rules
6. **Journal to DB** — `python3 record_journal.py --agent trader-stonks --entry "<Tick summary: what you traded, what you're watching, social pulse>"`
7. **Record your decision** — `python3 record_decision.py --agent trader-stonks --action <BUY/SELL/HOLD> --ticker <SYM> --quantity <N> --confidence <0-1> --thesis "<reasoning>" --signals <signal1> <signal2>`
8. Update profile
9. Learning loop tick — `python3 -m src.learning_loop tick --agent trader-stonks`. Read the report. If param tweaks were applied, adjust your strategy accordingly. Pay attention to the **binding constraint** — focus improvement there.
10. `python3 src/heartbeat_timestamp.py stonks`

Output HEARTBEAT_OK when done.
