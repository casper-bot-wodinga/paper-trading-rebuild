-- ============================================================================
-- Migration 010: Cleanup orphaned positions, decisions, and malformed timestamps
-- ============================================================================
-- 1. Cleanup 70 orphaned positions in trader_positions with empty trader_id
-- 2. Cleanup 3887 orphaned decisions in trader_decisions with empty trader_id
-- 3. Fix 2 malformed journal timestamps with 'journalT' prefix
-- 4. Add NOT NULL constraint on trader_id for future data integrity
-- ============================================================================

BEGIN;

-- ── 1. Cleanup orphaned positions (trader_id IS NULL or '') ────────────────
-- These positions have ticker data but no trader_id. They're invisible to the
-- dashboard because /api/positions filters by agent_id IN (LIVE_AGENTS).
-- We have two options: delete them or try to assign them to the right trader.
-- Since we can't know which trader they belong to (no agent_id set either),
-- and the data is stale, we DELETE them.

DO $$
DECLARE
    orphaned_positions INTEGER;
BEGIN
    SELECT COUNT(*) INTO orphaned_positions
    FROM trading.trader_positions
    WHERE (trader_id IS NULL OR trader_id = '');

    RAISE NOTICE 'Found % orphaned positions to delete', orphaned_positions;

    DELETE FROM trading.trader_positions
    WHERE (trader_id IS NULL OR trader_id = '');

    RAISE NOTICE 'Deleted % orphaned positions', orphaned_positions;
END $$;

-- ── 2. Cleanup orphaned decisions (trader_id IS NULL or '') ────────────────
-- These 3887 entries have no trader_id. Decisions without a trader are useless
-- for analysis and pollute the table with noise. DELETE them.

DO $$
DECLARE
    orphaned_decisions INTEGER;
BEGIN
    SELECT COUNT(*) INTO orphaned_decisions
    FROM trading.trader_decisions
    WHERE (trader_id IS NULL OR trader_id = '');

    RAISE NOTICE 'Found % orphaned decisions to delete', orphaned_decisions;

    DELETE FROM trading.trader_decisions
    WHERE (trader_id IS NULL OR trader_id = '');

    RAISE NOTICE 'Deleted % orphaned decisions', orphaned_decisions;
END $$;

-- ── 3. Fix malformed journal timestamps (journalT prefix) ──────────────────
-- These entries have timestamps like 'journalT16:23:00' or 'journalT12:29:00'
-- instead of proper ISO timestamps. The API has a workaround, but we should
-- fix the data at the source.
-- Strategy: replace 'journalT' with today's date + 'T' to make a valid ISO timestamp.
-- Since the original date is lost, we use the most recent known date from other
-- entries as a reasonable approximation.

DO $$
DECLARE
    malformed_count INTEGER;
    ref_date TEXT;
BEGIN
    -- Find a reference date from well-formed entries for this agent
    SELECT INTO ref_date
        substring(max(timestamp) from '^\d{4}-\d{2}-\d{2}')
    FROM trading.trader_journal
    WHERE timestamp ~ '^\d{4}-\d{2}-\d{2}T';

    IF ref_date IS NULL THEN
        ref_date := '2026-07-15';  -- safe fallback based on audit findings
    END IF;

    SELECT COUNT(*) INTO malformed_count
    FROM trading.trader_journal
    WHERE timestamp LIKE 'journalT%';

    RAISE NOTICE 'Found % malformed journal timestamps (journalT prefix)', malformed_count;

    -- Fix by replacing 'journalT' with the reference date + 'T'
    UPDATE trading.trader_journal
    SET timestamp = ref_date || 'T' || substring(timestamp FROM 'journalT(.+)$')
    WHERE timestamp LIKE 'journalT%';

    RAISE NOTICE 'Fixed malformed timestamps using reference date %', ref_date;
END $$;

-- ── 4. Add NOT NULL constraint on trader_id for future data integrity ──────
-- This prevents future inserts from omitting trader_id.
-- Only add if all NULL/empty values are cleaned up.

DO $$
DECLARE
    null_positions INTEGER;
    null_decisions INTEGER;
BEGIN
    SELECT COUNT(*) INTO null_positions
    FROM trading.trader_positions
    WHERE trader_id IS NULL OR trader_id = '';

    SELECT COUNT(*) INTO null_decisions
    FROM trading.trader_decisions
    WHERE trader_id IS NULL OR trader_id = '';

    IF null_positions = 0 THEN
        -- Can't ALTER COLUMN SET NOT NULL directly on text columns easily;
        -- instead add a CHECK constraint that prevents empty/blank trader_id
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_trader_positions_trader_id_not_empty'
        ) THEN
            ALTER TABLE trading.trader_positions
            ADD CONSTRAINT ck_trader_positions_trader_id_not_empty
            CHECK (trader_id IS NOT NULL AND trader_id != '');
        END IF;
        RAISE NOTICE 'Added NOT EMPTY constraint on trader_positions.trader_id';
    ELSE
        RAISE WARNING 'Skipping constraint: still % NULL/empty trader_id in trader_positions', null_positions;
    END IF;

    IF null_decisions = 0 THEN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_trader_decisions_trader_id_not_empty'
        ) THEN
            ALTER TABLE trading.trader_decisions
            ADD CONSTRAINT ck_trader_decisions_trader_id_not_empty
            CHECK (trader_id IS NOT NULL AND trader_id != '');
        END IF;
        RAISE NOTICE 'Added NOT EMPTY constraint on trader_decisions.trader_id';
    ELSE
        RAISE WARNING 'Skipping constraint: still % NULL/empty trader_id in trader_decisions', null_decisions;
    END IF;
END $$;

-- ── Verify ──────────────────────────────────────────────────────────────────
DO $$
DECLARE
    remaining_pos INTEGER;
    remaining_dec INTEGER;
    remaining_journal INTEGER;
BEGIN
    SELECT COUNT(*) INTO remaining_pos
    FROM trading.trader_positions
    WHERE (trader_id IS NULL OR trader_id = '');
    SELECT COUNT(*) INTO remaining_dec
    FROM trading.trader_decisions
    WHERE (trader_id IS NULL OR trader_id = '');
    SELECT COUNT(*) INTO remaining_journal
    FROM trading.trader_journal
    WHERE timestamp LIKE 'journalT%';

    RAISE NOTICE 'Cleanup complete: % orphaned positions, % orphaned decisions, % malformed journal entries remain',
        remaining_pos, remaining_dec, remaining_journal;
END $$;

COMMIT;
