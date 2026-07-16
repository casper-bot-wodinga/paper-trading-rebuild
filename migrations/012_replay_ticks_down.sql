-- Migration 012 down: Remove replay_ticks table
-- Down: 012_replay_ticks_down.sql

DROP TABLE IF EXISTS market_data.replay_ticks;
