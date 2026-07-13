-- Migration 008: Trade signals tracking + daily reflections
-- Tracks signals active at time of trade for win-rate analysis

-- Track signals active at time of trade
CREATE TABLE IF NOT EXISTS trading.trade_signals (
    id SERIAL PRIMARY KEY,
    trade_id INTEGER NOT NULL REFERENCES trading.trades(id),
    signal_name TEXT NOT NULL,       -- e.g. 'rsi', 'volume_spike', 'momentum_score', 'sentiment'
    signal_value FLOAT,
    confidence_at_time FLOAT,        -- what the agent scored their confidence
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trade_signals_trade ON trading.trade_signals(trade_id);
CREATE INDEX IF NOT EXISTS idx_trade_signals_name ON trading.trade_signals(signal_name);

-- Daily reflection storage
CREATE TABLE IF NOT EXISTS trading.daily_reflections (
    id SERIAL PRIMARY KEY,
    agent_id TEXT NOT NULL,
    date DATE NOT NULL,
    reflection_text TEXT NOT NULL,
    suggestions JSONB DEFAULT '[]',   -- structured suggestions for strategy changes
    win_rate FLOAT,
    total_pnl FLOAT,
    num_trades INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_reflections_agent_date ON trading.daily_reflections(agent_id, date);

-- Per-signal win rate cache (updated by reflection cron)
CREATE TABLE IF NOT EXISTS trading.signal_win_rates (
    id SERIAL PRIMARY KEY,
    agent_id TEXT NOT NULL,
    signal_name TEXT NOT NULL,
    total_trades INT NOT NULL DEFAULT 0,
    wins INT NOT NULL DEFAULT 0,
    win_rate FLOAT,
    last_updated DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_win_rates_agent_signal ON trading.signal_win_rates(agent_id, signal_name);