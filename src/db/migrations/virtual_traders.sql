-- Virtual trader rotation system
ALTER TABLE trading.trades 
ADD COLUMN IF NOT EXISTS trade_source VARCHAR(16) DEFAULT 'live';

CREATE TABLE IF NOT EXISTS trading.virtual_traders (
  id SERIAL PRIMARY KEY,
  name VARCHAR(64) NOT NULL,
  base_trader VARCHAR(32) NOT NULL,
  variant_type VARCHAR(16) NOT NULL,
  config JSONB NOT NULL,
  status VARCHAR(16) DEFAULT 'active',
  live_dates DATE[],
  created_at DATE DEFAULT CURRENT_DATE,
  culled_at DATE
);

CREATE TABLE IF NOT EXISTS trading.rotation_log (
  id SERIAL PRIMARY KEY,
  date DATE NOT NULL,
  base_trader VARCHAR(32) NOT NULL,
  live_virtual VARCHAR(64),
  live_pnl NUMERIC,
  top_virtual VARCHAR(64),
  top_virtual_pnl NUMERIC,
  promoted BOOLEAN,
  reason TEXT
);
