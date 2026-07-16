-- Migration 010: Down — remove constraints added in 010_up

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_trader_positions_trader_id_not_empty'
    ) THEN
        ALTER TABLE trading.trader_positions DROP CONSTRAINT ck_trader_positions_trader_id_not_empty;
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_trader_decisions_trader_id_not_empty'
    ) THEN
        ALTER TABLE trading.trader_decisions DROP CONSTRAINT ck_trader_decisions_trader_id_not_empty;
    END IF;

    RAISE NOTICE 'Migration 010 down complete: removed NOT EMPTY constraints';
END $$;

COMMIT;
