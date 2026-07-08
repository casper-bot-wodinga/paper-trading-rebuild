-- Add cumulative daily win count to virtual trader tracking
-- Required for the championship belt rotation model (virtual_rotate.py)
ALTER TABLE trading.virtual_traders
ADD COLUMN IF NOT EXISTS wins INTEGER DEFAULT 0;
