-- ============================================================================
-- Migration 012: market_data.replay_ticks — replaces SQLite in bar_loader.py
-- Issue #197: Unify data storage — Postgres as single source of truth
--
-- Creates a replay_ticks cache table in Postgres to replace the SQLite
-- replay_ticks table in bar_loader.py. This is a transient cache that
-- gets recreated on each to_cache() call.
-- ============================================================================

CREATE TABLE IF NOT EXISTS market_data.replay_ticks (
    id          BIGSERIAL PRIMARY KEY,
    timestamp   TIMESTAMPTZ     NOT NULL,
    ticker      VARCHAR(10)     NOT NULL,
    open        DECIMAL         NOT NULL,
    high        DECIMAL         NOT NULL,
    low         DECIMAL         NOT NULL,
    close       DECIMAL         NOT NULL,
    volume      BIGINT          NOT NULL DEFAULT 0,
    rsi         DECIMAL,
    momentum    DECIMAL,
    volatility  DECIMAL,
    regime      VARCHAR(32)
);

CREATE INDEX IF NOT EXISTS idx_replay_ticks_ts_ticker
    ON market_data.replay_ticks (timestamp, ticker);

CREATE INDEX IF NOT EXISTS idx_replay_ticks_ticker
    ON market_data.replay_ticks (ticker);
