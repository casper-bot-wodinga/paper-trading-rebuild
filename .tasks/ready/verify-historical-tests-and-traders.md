---
title: "Verify historical tests, real traders, and prompt iteration"
agent: orchestrator
priority: urgent
created: 2026-07-12T18:05
depends_on:
  - "P2: Fix 4 failing tests"
repo: Tesselation-Studios/paper-trading-rebuild
labels: verification, historical, traders
---

## Goal
Verify end-to-end that:
1. Historical simulation tests work for all virtual traders
2. Real traders can execute with live data
3. Prompt/strategy permutations can iterate

## Phase 1: Historical Tests (Virtual Traders)

### 1.1 Data Bus Health
- [ ] GET /health returns ok with current market data
- [ ] GET /quotes?symbols=SPY returns fresh (not June 16) prices
- [ ] Cache is populated for all tracked symbols

### 1.2 Replay Harness Smoke Test
- [ ] `python3 -c "from src.replay import ReplayHarness; print('replay imports ok')"`
- [ ] Create synthetic 5-day market data and run through replay harness
- [ ] Verify trades are generated (not all HOLDs)

### 1.3 historical_sim.py improve — Kairos
- [ ] Run: `cd ~/projects/paper-trading-rebuild && python3 src/historical_sim.py improve --trader kairos --ticker SPY --variants 2`
- [ ] Verify output contains trades and variant comparisons
- [ ] Check DB has trader_decisions recorded

### 1.4 historical_sim.py improve — Stonks
- [ ] Same as 1.3 but --trader stonks
- [ ] Verify social sentiment signals generate trades

### 1.5 historical_sim.py improve — Aldridge
- [ ] Same as 1.3 but --trader aldridge
- [ ] Verify fundamentals filter produces trades

### 1.6 Nightly Replay Smoke
- [ ] Run: `cd ~/projects/paper-trading-rebuild && python3 src/nightly_replay.py --date 2026-07-10 --variants 2 --dry-run`
- [ ] Verify it parses dates and generates variants
- [ ] Check Postgres connection works

## Phase 2: Real Trader Verification

### 2.1 Trader Agent Configs
- [ ] Check `~/.openclaw/agents/trader-kairos/` exists and has valid config
- [ ] Check `~/.openclaw/agents/trader-stonks/` exists and has valid config
- [ ] Check `~/.openclaw/agents/trader-aldridge/` exists and has valid config
- [ ] Verify all three are enabled in openclaw.json

### 2.2 Alpaca API Keys
- [ ] Verify each trader's Alpaca keys from .env
- [ ] Test: `curl -s -H "APCA-API-KEY-ID: $KEY" -H "APCA-API-SECRET-KEY: $SECRET" https://paper-api.alpaca.markets/v2/account`
- [ ] Confirm paper trading (not live)

### 2.3 Trader Cron Ticks
- [ ] Check scheduled ticks exist in cron
- [ ] Verify tick frequency (5min for Kairos, 15min for Stonks, 30min for Aldridge)
- [ ] Check last tick timestamp
- [ ] Manually trigger a tick: `sessions_send(agentId="trader-kairos", message="tick")`

### 2.4 Postgres Data Flow
- [ ] Verify `trading.decisions` has recent rows
- [ ] Verify `trading.executed_trades` matches dashboard
- [ ] Verify `trading.portfolio_snapshots` is updating

### 2.5 Risk Gates
- [ ] Check BootstrapGate allows first N trades
- [ ] Verify HoursGate allows trades during market hours
- [ ] Check circuit breakers aren't blocking everything

## Phase 3: Prompt/Strategy Iteration

### 3.1 Prompt Variant Generation
- [ ] Run: `python3 src/prompt_sweep.py --trader kairos --date 2026-07-02 --variants 4 --bootstrap`
- [ ] Verify 4 prompt variants are generated
- [ ] Check each variant produces different signal scores

### 3.2 Prompt Versioning
- [ ] Check `src/prompt_versioning.py` works
- [ ] Verify git-based prompt history
- [ ] Check prompt tiering (boost/fallback/emergency)

### 3.3 Winner Promotion
- [ ] Run promote_sweep_winner on sweep results
- [ ] Verify winning variant gets promoted
- [ ] Check canvas notification

### 3.4 Learning Loop
- [ ] Check `src/learning_loop.py` runs without error
- [ ] Verify journal analysis produces recommendations
- [ ] Check synthesis generates improvement suggestions

## Phase 4: Report
- [ ] Compile results: what works, what's broken
- [ ] Push to canvas
- [ ] PR or commit any fixes found
- [ ] Update ROADMAP.md status

## Workers
- coder: code fixes, test runs
- homelab-wizard: infra checks, cron, systemd, Postgres
- researcher: analyze results, recommend improvements
