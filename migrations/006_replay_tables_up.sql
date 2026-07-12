-- Migration 006: Replay infrastructure tables
-- Adds tick_queue for tick producer / orchestrator pipeline

CREATE TABLE IF NOT EXISTS trading.tick_queue (
    id SERIAL PRIMARY KEY,
    tick_data JSONB NOT NULL,
    status TEXT DEFAULT 'pending',
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS trading.orchestrator_log (
    id SERIAL PRIMARY KEY,
    tick_id INTEGER,
    trader TEXT NOT NULL,
    decision TEXT,
    status TEXT DEFAULT 'success',
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);