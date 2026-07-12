# Kairos Heartbeat

Read `skills/persona-strategy/SKILL.md` for full strategy rules.

**Core flow:**
0. Check inbox — `curl -s "http://localhost:8080/inbox?agent=kairos"` — respond to any pending Hermes messages
1. Portfolio check — `python3 src/skill_portfolio.py --account kairos`
   - Verify freshness: the output includes a `freshness` field. If PG data is >5 min stale, the
     live Alpaca data is still valid, but note the discrepancy in your journal.
2. Data bus pulse — rides, regime, sentiment, fear & greed
3. **Stock discovery** — check momentum rankings (`GET /momentum`), scan for sector rotation and unusual volume breakouts. Propose at least 1 new ticker for the watchlist. Log discovery to `strategy_notes/<DATE>_discovery.md`.
4. Time-based exit check — flag positions >5 days or >3 days stale
5. Scoreboard sync — `python3 src/sync_exits.py --backfill kairos`
6. **Journal to DB** — `python3 record_journal.py --agent trader-kairos --entry "<Tick summary: what you did, what you're watching, market read>"`
7. Update profile
8. **Record your decision** — `python3 record_decision.py --agent trader-kairos --action <BUY/SELL/HOLD> --ticker <SYM> --quantity <N> --confidence <0-1> --thesis "<reasoning>" --signals <signal1> <signal2>`
8. Learning loop tick — `python3 -m src.learning_loop tick --agent trader-kairos`. Read the report. If param tweaks were applied, adjust your strategy accordingly. Pay attention to the **binding constraint** — focus improvement there.
9. `python3 src/heartbeat_timestamp.py kairos`

Output HEARTBEAT_OK when done.
