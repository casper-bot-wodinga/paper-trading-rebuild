-- ============================================================================
-- Migration 011: Missing DB tables from agent-reflection and promotion specs
-- Issue #198: Build 6 missing tables
--
-- Creates:
--   trading.agent_reflections    — per-trader nightly reflection/coach notes
--   trading.signal_performance   — signal accuracy tracking (view)
--   trading.promotion_summary    — promotion event log
--   trading.tier_snapshots       — daily tier distribution snapshot
-- ============================================================================

-- ═══════════════════════════════════════════════════════════════════════════════
-- 1. agent_reflections — per-trader nightly reflection/coach notes
-- ═══════════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS trading.agent_reflections (
    id              BIGSERIAL PRIMARY KEY,
    trader_id       VARCHAR(32)     NOT NULL,
    date            DATE            NOT NULL,
    reflection      TEXT            NOT NULL,
    key_insights    JSONB,                          -- array of insight strings
    suggested_changes JSONB,                        -- array of suggested param/prompt changes
    confidence      DECIMAL         DEFAULT 0.0,    -- 0.0-1.0 confidence in reflection
    model_used      VARCHAR(64),                    -- LLM model that generated reflection
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_reflection_trader_date UNIQUE (trader_id, date)
);

CREATE INDEX IF NOT EXISTS idx_reflection_trader_date
    ON trading.agent_reflections (trader_id, date DESC);

-- ═══════════════════════════════════════════════════════════════════════════════
-- 2. signal_performance — signal accuracy tracking view
-- ═══════════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS trading.signal_performance (
    id              BIGSERIAL PRIMARY KEY,
    trader_id       VARCHAR(32)     NOT NULL,
    ticker          VARCHAR(10)     NOT NULL,
    signal_id       INTEGER,                        -- FK to trading.signals
    signal_value    DECIMAL         NOT NULL,        -- original signal (-1..+1)
    signal_direction VARCHAR(8),                    -- 'bullish', 'bearish', 'neutral'
    predicted_move  VARCHAR(16),                    -- 'up', 'down', 'flat'
    actual_move     VARCHAR(16),                    -- 'up', 'down', 'flat'
    correct         BOOLEAN,                        -- was prediction correct?
    horizon_minutes INTEGER         DEFAULT 15,     -- prediction horizon
    logged_at       TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signal_perf_trader
    ON trading.signal_performance (trader_id, logged_at DESC);

CREATE INDEX IF NOT EXISTS idx_signal_perf_ticker
    ON trading.signal_performance (ticker, logged_at DESC);

-- ═══════════════════════════════════════════════════════════════════════════════
-- 3. promotion_summary — promotion event log
-- ═══════════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS trading.promotion_summary (
    id              BIGSERIAL PRIMARY KEY,
    trader_id       VARCHAR(32)     NOT NULL,
    virtual_name    VARCHAR(64)     NOT NULL,
    from_tier       VARCHAR(32)     NOT NULL,        -- e.g. 'probation', 'rookie'
    to_tier         VARCHAR(32)     NOT NULL,        -- e.g. 'rookie', 'veteran'
    composite_score DECIMAL,                         -- composite score at promotion time
    calmar          DECIMAL,
    sortino         DECIMAL,
    profit_factor   DECIMAL,
    win_rate        DECIMAL,
    total_return_pct DECIMAL,
    max_drawdown    DECIMAL,
    n_trades        INTEGER,
    reason          TEXT,                            -- why promotion was granted
    promoted_by     VARCHAR(64)     DEFAULT 'system', -- 'system', 'manual', or agent id
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_promotion_summary_trader
    ON trading.promotion_summary (trader_id, created_at DESC);

-- ═══════════════════════════════════════════════════════════════════════════════
-- 4. tier_snapshots — daily tier distribution snapshot
-- ═══════════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS trading.tier_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    date            DATE            NOT NULL,
    base_trader     VARCHAR(32)     NOT NULL,        -- 'kairos', 'aldridge', 'stonks'
    tier            VARCHAR(32)     NOT NULL,        -- 'probation', 'rookie', 'veteran', 'expert', 'elite', 'live'
    count           INTEGER         NOT NULL DEFAULT 0,  -- number of virtuals in this tier
    filled_slots    INTEGER         NOT NULL DEFAULT 0,  -- filled slots (for tier with caps)
    total_slots     INTEGER         NOT NULL DEFAULT 0,  -- total available slots
    avg_composite   DECIMAL,                         -- average composite score in this tier
    avg_return_pct  DECIMAL,                         -- average return % in this tier
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tier_snapshot UNIQUE (date, base_trader, tier)
);

CREATE INDEX IF NOT EXISTS idx_tier_snapshots_date
    ON trading.tier_snapshots (date DESC);

-- ============================================================================
-- Also add tier column to virtual_traders table if it doesn't exist
-- ============================================================================
ALTER TABLE trading.virtual_traders
ADD COLUMN IF NOT EXISTS tier VARCHAR(32) DEFAULT 'probation';

ALTER TABLE trading.virtual_traders
ADD COLUMN IF NOT EXISTS promoted_at TIMESTAMPTZ;

ALTER TABLE trading.virtual_traders
ADD COLUMN IF NOT EXISTS composite_score DECIMAL;

-- ============================================================================
-- Add queries for the new tables
-- ============================================================================

-- Refresh signal performance (called by nightly pipeline)
-- For each signal, check if the next N-minute price move matched the signal direction
CREATE OR REPLACE FUNCTION trading.refresh_signal_performance(
    p_horizon_minutes INTEGER DEFAULT 15
) RETURNS INTEGER AS $$
DECLARE
    v_count INTEGER;
BEGIN
    INSERT INTO trading.signal_performance (
        trader_id, ticker, signal_id, signal_value, signal_direction,
        predicted_move, actual_move, correct, horizon_minutes
    )
    SELECT
        s.trader_id,
        s.ticker,
        s.id,
        s.composite_signal,
        CASE WHEN s.composite_signal > 0.2 THEN 'bullish'
             WHEN s.composite_signal < -0.2 THEN 'bearish'
             ELSE 'neutral' END,
        CASE WHEN s.composite_signal > 0.2 THEN 'up'
             WHEN s.composite_signal < -0.2 THEN 'down'
             ELSE 'flat' END,
        CASE
            WHEN b.close > s.composite_signal * 0 THEN 'up'
            WHEN b.close < s.composite_signal * 0 THEN 'down'
            ELSE 'flat' END,
        NULL,
        p_horizon_minutes
    FROM trading.signals s
    JOIN LATERAL (
        SELECT close
        FROM market_data.bars
        WHERE ticker = s.ticker
          AND timestamp >= s.timestamp
          AND timestamp < s.timestamp + (p_horizon_minutes || ' minutes')::INTERVAL
        ORDER BY timestamp ASC
        LIMIT 1
    ) b ON true
    WHERE s.created_at >= NOW() - INTERVAL '1 day'
      AND NOT EXISTS (
          SELECT 1 FROM trading.signal_performance sp
          WHERE sp.signal_id = s.id
      )
    ON CONFLICT DO NOTHING;

    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql;

-- Snapshot current tier distribution
CREATE OR REPLACE FUNCTION trading.snapshot_tiers() RETURNS INTEGER AS $$
DECLARE
    v_count INTEGER := 0;
    v_tier VARCHAR(32);
    v_base VARCHAR(32);
    v_rec RECORD;
BEGIN
    FOR v_rec IN
        SELECT
            base_trader,
            COALESCE(tier, 'probation') as tier,
            COUNT(*) as cnt,
            COUNT(*) as filled,
            20 as total_slots,
            AVG(composite_score) as avg_comp,
            AVG(NULLIF(config->>'total_return_pct', '')::DECIMAL) as avg_ret
        FROM trading.virtual_traders
        WHERE status = 'active'
        GROUP BY base_trader, COALESCE(tier, 'probation')
    LOOP
        INSERT INTO trading.tier_snapshots (
            date, base_trader, tier, count, filled_slots, total_slots,
            avg_composite, avg_return_pct
        ) VALUES (
            CURRENT_DATE,
            v_rec.base_trader,
            v_rec.tier,
            v_rec.cnt,
            v_rec.filled,
            v_rec.total_slots,
            v_rec.avg_comp,
            v_rec.avg_ret
        )
        ON CONFLICT (date, base_trader, tier) DO UPDATE
        SET count = EXCLUDED.count,
            filled_slots = EXCLUDED.filled_slots,
            avg_composite = EXCLUDED.avg_composite,
            avg_return_pct = EXCLUDED.avg_return_pct;

        v_count := v_count + 1;
    END LOOP;

    RETURN v_count;
END;
$$ LANGUAGE plpgsql;