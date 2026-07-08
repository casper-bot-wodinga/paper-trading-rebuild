-- ============================================================================
-- Data Backfill Schema — dedicated tables for backfilled market data
-- Used by scripts/backfill_market_data.py and scripts/sync_bars_to_pg.py
-- ============================================================================

-- Historical 5-minute OHLCV bars
CREATE TABLE IF NOT EXISTS market_data.bars_5min (
    symbol      VARCHAR(16)     NOT NULL,
    timestamp   TIMESTAMPTZ     NOT NULL,
    open        NUMERIC         NOT NULL,
    high        NUMERIC         NOT NULL,
    low         NUMERIC         NOT NULL,
    close       NUMERIC         NOT NULL,
    volume      BIGINT          NOT NULL,
    PRIMARY KEY (symbol, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_bars_5min_symbol_ts
    ON market_data.bars_5min (symbol, timestamp DESC);

-- Daily OHLCV bars
CREATE TABLE IF NOT EXISTS market_data.bars_1d (
    symbol      VARCHAR(16)     NOT NULL,
    date        DATE            NOT NULL,
    open        NUMERIC         NOT NULL,
    high        NUMERIC         NOT NULL,
    low         NUMERIC         NOT NULL,
    close       NUMERIC         NOT NULL,
    volume      BIGINT          NOT NULL,
    PRIMARY KEY (symbol, date)
);

CREATE INDEX IF NOT EXISTS idx_bars_1d_symbol_date
    ON market_data.bars_1d (symbol, date DESC);

-- Sentiment history (news + social)
CREATE TABLE IF NOT EXISTS market_data.news_sentiment (
    id          SERIAL PRIMARY KEY,
    symbol      VARCHAR(16)     NOT NULL,
    timestamp   TIMESTAMPTZ     NOT NULL,
    source      VARCHAR(32)     NOT NULL,
    headline    TEXT            NOT NULL,
    sentiment   NUMERIC,
    UNIQUE (symbol, timestamp, source)
);

CREATE INDEX IF NOT EXISTS idx_news_sentiment_symbol_ts
    ON market_data.news_sentiment (symbol, timestamp DESC);

-- Data bus cache mirror (snapshot of current cache state)
CREATE TABLE IF NOT EXISTS market_data.cache_snapshots (
    id          SERIAL PRIMARY KEY,
    endpoint    VARCHAR(64)     NOT NULL,
    data        JSONB           NOT NULL,
    fetched_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cache_snapshots_endpoint
    ON market_data.cache_snapshots (endpoint, fetched_at DESC);
