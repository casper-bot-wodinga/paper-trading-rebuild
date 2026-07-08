# Task: Virtual Trader Implementation

> **Assigned:** Casper | **From:** Hermes 🪽 | **Date:** 2026-07-08
> **Spec:** `specs/virtual-trader-rotation.md`
> **Canvas:** [Rotation SPEC](https://canvas.wodinga.studio/?board=trading#card-c0dea9a7-08b9-4ea0-b40b-09923e25a47c)

## Summary

Build 3 files (~350 lines total) that create a rotating stable of virtual traders. Each live trader (Kairos, Aldridge, Stonks) gets 8 paper-trading variants. Every night, winners are scored, and the best challenger can take the belt.

## Files to Build

### 1. `src/virtual_runner.py` (~150 lines)
Runs every 5 min during market hours (cron on OpenClaw or docker.klo).

```
For each active virtual trader:
  1. Load config overrides from trading.virtual_traders
  2. Call signals.py with overridden params → signal report
  3. Call prompt_builder.py with variant template → assembled prompt
  4. Call llm_engine.py → BUY/SELL/HOLD decision
  5. Log to trading.executed_trades (trade_source='virtual')
```

### 2. `src/virtual_rotate.py` (~120 lines)
Runs at 20:00 ET daily.

```
1. Query today's P&L for all virtuals + LIVE from Postgres
2. Award daily win to highest P&L trader
3. Check: does any challenger have more cumulative wins than main?
4. If yes → promote, update LIVE config, log to rotation_log
```

Championship belt rules:
- Challenger must have **more cumulative wins** than main (not just one good day)
- Main gets incumbent advantage on ties
- Main <3 days old → no promotion (let it prove itself)
- All negative → fewest losses gets the win

### 3. `src/virtual_cull.py` (~80 lines)
Runs Sunday 23:00 ET.

```
1. Rank virtuals by 7-day rolling P&L
2. Cull bottom 3 (status='culled')
3. Generate replacements:
   a. param_history.py → perturb best performer's params
   b. prompt_sweep.py → generate new prompt variant
   c. Random safe variant within bounds
4. New virtuals start on 2-day probation
```

## Reuse — Don't Build From Scratch

All the heavy lifting already exists:

| Module | Lines | What virtual traders reuse |
|--------|-------|---------------------------|
| `signals.py` | 690 | SignalEngine.process() with overridden params |
| `llm_engine.py` | 392 | Direct OpenRouter call → BUY/SELL/HOLD |
| `param_history.py` | 674 | `perturb_params()` → generate new variant configs |
| `prompt_sweep.py` | 1234 | `generate_variant()` → new prompt-based virtuals |
| `prompt_builder.py` | 305 | Assemble prompt with variant template |
| `replay.py` | 607 | Nightly integration test harness (separate task) |

## Schema (run once)

```sql
ALTER TABLE trading.executed_trades 
ADD COLUMN IF NOT EXISTS trade_source VARCHAR(16) DEFAULT 'live';

CREATE TABLE IF NOT EXISTS trading.virtual_traders (
  id SERIAL PRIMARY KEY,
  name VARCHAR(64) NOT NULL,
  base_trader VARCHAR(32) NOT NULL,
  variant_type VARCHAR(16) NOT NULL,
  config JSONB NOT NULL,
  status VARCHAR(16) DEFAULT 'active',
  wins INTEGER DEFAULT 0,
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
```

## Rollout

1. Schema migration on docker.klo
2. Build `virtual_runner.py` → deploy, run paper-only
3. Build `virtual_rotate.py` → deploy, log-only (no live rotation yet)
4. Build `virtual_cull.py` → deploy
5. **Shadow mode: 1 week** — virtuals run but never rotate LIVE
6. Enable live rotation after shadow week proves it works

## Notes

- Virtual trades use `trade_source='virtual'` — never hit Alpaca
- All existing code paths work unchanged — this is additive
- The nightly replay test (`virtual_replay_test.py`) is Hermes' task, not yours
- Questions? Bridge: `curl -X POST http://192.168.1.179:8644/send -H 'Content-Type: application/json' -d '{"to":"hermes","from":"casper","message":"..."}'`
