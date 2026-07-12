-- Migration 007: Promotion log table
-- Tracks virtual-to-live promotion / demotion events

CREATE TABLE IF NOT EXISTS trading.promotion_log (
    id SERIAL PRIMARY KEY,
    virtual_name TEXT NOT NULL,
    base_trader TEXT NOT NULL,
    live_trader_before TEXT NOT NULL,
    virtual_score REAL,
    live_score REAL,
    metric TEXT DEFAULT 'pnl',
    threshold REAL DEFAULT 10.0,
    improvement_pct REAL,
    was_rolled_back BOOLEAN DEFAULT FALSE,
    rollback_at TIMESTAMPTZ,
    notes TEXT,
    promoted_at TIMESTAMPTZ DEFAULT NOW()
);