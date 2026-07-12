# Aldridge Heartbeat

Read `skills/persona-strategy/SKILL.md` for full strategy rules.

**Core flow:**
0. Check inbox — `curl -s "http://localhost:8080/inbox?agent=aldridge"` — respond to any pending Hermes messages
1. Portfolio check — `python3 src/skill_portfolio.py --account aldridge`
   - Verify freshness: the output includes a `freshness` field. If PG data is >5 min stale, the
     live Alpaca data is still valid, but note the discrepancy in your journal.
2. Macro scan — briefing, macro data, interest rates
3. **Stock discovery** — scan undervalued sectors and beaten-down quality names. Check fundamentals (`GET /fundamentals`), insiders (`GET /insiders`), and macro rotations. Propose at least 1 new value candidate. Log discovery to `strategy_notes/<DATE>_discovery.md`.
4. Thesis integrity — news, fundamentals, insiders for each position
5. **Journal to DB** — `python3 record_journal.py --agent trader-aldridge --entry "<Tick summary: thesis status, macro read, conviction levels>"`
6. **Record your decision** — `python3 record_decision.py --agent trader-aldridge --action <BUY/SELL/HOLD> --ticker <SYM> --quantity <N> --confidence <0-1> --thesis "<reasoning>" --signals <signal1> <signal2>`
7. Learning loop tick — `python3 -m src.learning_loop tick --agent trader-aldridge`. Read the report. If param tweaks were applied, adjust your strategy accordingly. Pay attention to the **binding constraint** — focus improvement there.
8. Update profile
9. `python3 src/heartbeat_timestamp.py aldridge`

Output HEARTBEAT_OK when done.
