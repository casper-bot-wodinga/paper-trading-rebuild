VERIFICATION — learning_loop.py Postgres migration
=================================================

## Files Changed

### 1. src/learning_loop.py — MIGRATED to Postgres
- Replaced sqlite3 imports with psycopg2
- get_db() now returns a psycopg2 Postgres connection
- get_agents() reads from trading.agent_profile
- get_decisions() queries trading.decisions, maps columns (trader_id→agent_id, decision→action, conviction→confidence, rationale→thesis)
- get_trades() queries trading.trades (fallback to trading.executed_trades)
- get_journal() queries trading.journal
- inject_test_data() writes to Postgres tables
- health_check() now checks Postgres schema
- Added uuid import for test data trade_id generation

### 2. scripts/tick_prompt.py — MIGRATED to Postgres
- get_journal_entries() now reads from trading.journal via psycopg2
- Fallback: if Postgres fails, returns empty list (no more SQLite dependency)
- --db-path CLI param kept for backward compat but ignored

### 3. agents/trader-kairos/HEARTBEAT.md — Updated
- Changed sync_exits.py → sync_exits_pg.py reference
- Now points to Postgres-native sync script

### 4. scripts/seed_agents.py — MIGRATED to Postgres
- Now seeds agent profiles to Postgres trading.agent_profile
- Falls back to SQLite if Postgres unavailable
- Uses ON CONFLICT upsert pattern

## Not Changed (low priority / legacy)
- src/config_loader.py — legacy agent param store, not trader decision data
- src/historical_sim.py — legacy simulation, not part of live pipeline
- tests/ — use :memory: SQLite, not production data
- scripts/migrate_sqlite_to_pg.py — migration tool, will be retired
- scripts/quick_migrate_pg.py — migration tool
- scripts/nightly_pipeline.py — references shared/trader.db for reading
- scripts/verify_learning_loop.py — test verification utility

## .env Verification
DATABASE_URL=postgresql://trader:@192.168.1.179:5433/trading ✓
PG_DSN=host=192.168.1.179 port=5433 dbname=trading user=trader ✓

## Alpaca Keys (all three traders)
ALPACA_KAIROS_KEY=PK6V4QE55ANSVOY6T6GNLHSCTT ✓
ALPACA_ALDRIDGE_KEY=PK76XUBDB5E4NYCR53H4NB6OP3 ✓
ALPACA_STONKS_KEY=PKYJYUVAD2RY4HBJ4AMJDFF3LO ✓
All three have matching secret keys ✓

## Schema Mapping (SQLite → Postgres)
decisions → trading.decisions (mapped via SELECT aliases)
trades → trading.trades / trading.executed_trades
journal → trading.journal
agent_profile → trading.agent_profile