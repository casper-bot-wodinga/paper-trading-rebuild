# Kairos Heartbeat

Read `skills/skill-kairos-strategy/SKILL.md` for full strategy rules.

**Core flow:**
0. Check inbox — `curl -s "http://localhost:8080/inbox?agent=kairos"` — respond to any pending Hermes messages
1. Portfolio check — `python3 src/skill_portfolio.py --account kairos`
2. Data bus pulse — rides, regime, sentiment, fear & greed, news
3. **Stock discovery** — systematic screener:
   - Primary: fetch momentum-ranked universe: `GET /momentum` (cross-sectional ranking)
   - Filter by price: run `python3 scripts/stock_discovery.py --agent kairos --save`
   - Read `state/discovery_kairos_<DATE>.md` for price-filtered candidates
   - Cross-reference: news headlines (`GET /news` or `GET /news-cache`), sector rotation, unusual volume breakouts
   - Propose at least 1 new ticker. Log discovery to `strategy_notes/<DATE>_discovery.md`.
4. Time-based exit check — flag positions >5 days or >3 days stale
5. Scoreboard sync — `python3 src/sync_exits_pg.py --backfill kairos`  # writes to Postgres trading.trades
6. Sync decisions to Postgres — `python3 scripts/sync_decisions_to_pg.py --apply`  # writes decisions + journal to trading.trader_decisions + trading.trader_journal
7. Log your read, journal a note, update profile
8. Learning loop — `python3 -m src.learning_loop --agent trader-kairos`. Read the report. If param tweaks were applied, adjust your strategy accordingly. Pay attention to the **binding constraint** — focus improvement there.
9. `python3 src/heartbeat_timestamp.py kairos`
10. Tick flasher — `curl -s -X POST http://localhost:5002/api/tick/kairos -H 'Content-Type: application/json' -d '{}' > /dev/null`

Output HEARTBEAT_OK when done.
