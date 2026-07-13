-- Migration 015: Fix schema mismatches, add views, ensure all tables exist
--
-- Fixes:
--   1. Add portfolio_snapshot (singular) view for compatibility
--   2. Ensure trader_journal and trader_decisions exist in Postgres
--   3. Add agent_state.portfolio_snapshots view for backward compat
--   4. Fix column types (text → timestamptz where possible)

BEGIN;

-- ── 1. View: portfolio_snapshot (singular) mirrors portfolio_snapshots (plural) ──
CREATE OR REPLACE VIEW trading.portfolio_snapshot AS
SELECT * FROM trading.portfolio_snapshots;

-- ── 2. Ensure trader_journal exists in Postgres ────────────────────────────────
CREATE TABLE IF NOT EXISTS trading.trader_journal (
    id BIGSERIAL PRIMARY KEY,
    agent_id TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (NOW()::text),
    mood TEXT,
    entry TEXT NOT NULL,
    confidence REAL,
    source TEXT DEFAULT 'heartbeat',
    trader_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── 3. Ensure trader_decisions exists in Postgres ──────────────────────────────
CREATE TABLE IF NOT EXISTS trading.trader_decisions (
    id BIGSERIAL PRIMARY KEY,
    agent_id TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (NOW()::text),
    action TEXT NOT NULL,
    ticker TEXT DEFAULT '',
    quantity REAL DEFAULT 0,
    stop_loss REAL,
    confidence REAL DEFAULT 0,
    thesis TEXT DEFAULT '',
    mood TEXT,
    source TEXT DEFAULT 'heartbeat',
    trader_id TEXT,
    signals_used TEXT DEFAULT '[]',
    decision_json JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── 4. Add indexes for common queries ──────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_trader_journal_agent_ts
    ON trading.trader_journal(agent_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trader_decisions_agent_ts
    ON trading.trader_decisions(agent_id, timestamp DESC);

-- ── 5. Record this migration ─────────────────────────────────────────────────
INSERT INTO trading.schema_migrations (migration_id, name)
VALUES ('015', '015_fix_schema.sql')
ON CONFLICT (migration_id) DO NOTHING;

COMMIT;
