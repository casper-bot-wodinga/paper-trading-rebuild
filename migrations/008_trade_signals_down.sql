-- Migration 008: Revert
DROP TABLE IF EXISTS trading.signal_win_rates;
DROP TABLE IF EXISTS trading.daily_reflections;
DROP TABLE IF EXISTS trading.trade_signals;