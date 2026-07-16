-- ============================================================================
-- Migration 017: Create signal_performance view
-- Issue #250: Build all 4 DB tables from agent-reflection-loop spec
--
-- Creates:
--   trading.signal_performance — aggregated signal win-rate analytics
--
-- Depends on: trading.decisions (buy_decision_id join to trading.trades)
-- ============================================================================

-- ═══════════════════════════════════════════════════════════════════════════════
-- signal_performance — signal accuracy tracking view
-- Aggregates per-trader, per-signal-type win rates from BUY decisions
-- ═══════════════════════════════════════════════════════════════════════════════
CREATE OR REPLACE VIEW trading.signal_performance AS
SELECT
    d.trader_id,
    sig.signal_type,
    COUNT(*)                                                                          AS trades,
    COUNT(CASE WHEN t.pnl > 0 THEN 1 END)                                             AS wins,
    ROUND(AVG(t.pnl), 2)                                                              AS avg_pnl,
    ROUND(
        COUNT(CASE WHEN t.pnl > 0 THEN 1 END)::numeric
        / NULLIF(COUNT(*), 0),
        3
    )                                                                                 AS win_rate
FROM trading.decisions d
CROSS JOIN LATERAL jsonb_array_elements_text(
    CASE
        WHEN jsonb_typeof(d.decision_json->'signals_used') = 'array'
        THEN d.decision_json->'signals_used'
        ELSE '[]'::jsonb
    END
) AS sig(signal_type)
JOIN trading.trades t ON t.buy_decision_id = d.id
WHERE d.decision = 'BUY'
GROUP BY d.trader_id, sig.signal_type;

-- ═══════════════════════════════════════════════════════════════════════════════
-- Also add UNIQUE constraint on agent_reflections if missing
-- (the existing table uses agent_id + reflection_date, add proper index)
-- ═══════════════════════════════════════════════════════════════════════════════
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_reflection_agent_date'
          AND conrelid = 'trading.agent_reflections'::regclass
    ) THEN
        ALTER TABLE trading.agent_reflections
        ADD CONSTRAINT uq_reflection_agent_date UNIQUE (agent_id, reflection_date);
    END IF;
END $$;
