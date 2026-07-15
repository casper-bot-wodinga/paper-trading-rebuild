# SPEC: Self-Improving Paper Trading System

> **META-SPEC**: [ai-project-system v0.22](https://github.com/openclaw/openclaw/blob/main/docs/ai-project-system/META-SPEC.md)
> **Repo**: `Tesselation-Studios/paper-trading-rebuild`
> **Status**: Built + evolving — 3 traders live, Postgres native, webhook comms active
> **Updated**: 2026-07-14
> **Branch**: `v3` — current development
>
> This file is the **master index**. Each subsystem has its own spec in `specs/` with
> implementation details. Details here are architecture-wide — everything else lives downstream.

---

## Purpose

Three AI traders (Kairos, Aldridge, Stonks) running $10K paper portfolios, competing in a tournament ending 12/31/2026 to maximize portfolio value. Two-speed learning: intraday parameter tuning + nightly prompt sweeps. Virtual traders (param/prompt variants) shadow live traders to discover improvements. All validated against out-of-sample data.

---

## Architecture

### Two-Speed Learning

```
MARKET DATA (Alpaca → Data Bus)
        │
        ▼
┌──────────────────┐     ┌──────────────────┐
│   SIGNAL ENGINE   │     │   LLM TRADER      │
│ (gradient-tuned)  │────▶│ (OpenClaw agent)  │
│                   │     │                   │
│ • Momentum, RSI   │     │ Reads signals +   │
│ • Vol filters     │     │   context         │
│ • Position sizing │     │ Makes BUY/SELL/   │
│ • Regime weights  │     │   HOLD decision   │
│ • Conviction       │     │   Writes decision │
│   calibration     │     │   journal         │
│                   │     │ CAN override      │
│        ↑          │     │   signals         │
│        │ gradient │     │        ↑          │
│        │ descent  │     │        │ prompt   │
│        │(intraday)│     │        │ sweep    │
│                   │     │        │(nightly) │
└──────────────────┘     └──────────────────┘
        │                                      │
        └──────────────┬───────────────────────┘
                       ▼
             ┌──────────────────────┐
             │  OBJECTIVE FUNCTION  │
             │  Calmar + PF + Exp   │
             │  Scores every trade  │
             └──────────────────────┘
```

### Three Speeds of Improvement

| Speed | What | When | How |
|-------|------|------|-----|
| **1. Gradient descent** | Tune numeric signal params (momentum threshold, RSI, sizing) | Every tick | Finite-difference, small steps |
| **2. Prompt sweep** | Test N prompt variants on historical replay | Nightly | Virtual traders compete, winner promoted |
| **3. Code changes** | Structural improvements to signal engine, risk gate, etc. | Weekly or as needed | Bot-owned repo, direct push to main |

### Hardware

| Machine | IP | Role |
|---------|----|------|
| **Hermes** | .131 | Orchestrator, spec-keeper, PR reviewer |
| **OpenClaw** | .41 | Agent host — traders live here |
| **Docker** | .179 | Backtest workers, replay harness |

Data flows through Postgres on Docker (.179:5433) and the data bus (.41:5000). TrueNAS (.96) stores historical archives.

---

## Architectural Invariants

These cannot be violated. All code and PRs are audited against them.

1. **Write-on-transaction**: Every state change writes to DB before returning. No in-memory-only state.
2. **Async confirmation**: No blocking on external APIs. Fire, record intent, confirm later.
3. **No dead code**: Every code path exercised by at least one test. Coverage gate in CI.
4. **Config from files, not DB**: All trader configs live in git. DB holds data, not settings.
5. **Trader-as-learner**: Agents do inference in their own ticks. No separate learning pipeline feeding results back.
6. **Ground truth is P&L**: All improvement measured by realized P&L outcomes. Heuristic scores are diagnostic only.
7. **Out-of-sample validation**: No parameter change accepted without validation on unseen data.
8. **Idempotent ticks**: Running the same tick twice produces the same result.
9. **Bootstrap fast and small**: Every new strategy begins loose (cheap stocks $10-40, 1-2% equity, 0.30 confidence). Learning loop tightens — not the starting prompt.
10. **Risk gate mirrors prompts**: Risk gate config is a derivative of trader prompts, not independent policy. Changing one requires changing the other.
11. **Cron is trigger, not instruction**: Cron messages are nudges. They don't specify strategy, stock universe, or entry rules — those live in AGENTS.md.
12. **Decision quality gates are warning-only during bootstrap**: First 30 trades or +5% equity — thesis/signals_used/exit_condition are WARN only, not VETO.
13. **Cron timeout must exceed model inference time × 3**: Minimum: model's P99 latency × 3.
14. **Mode-gated execution**: Traders must check mode (`LIVE`/`HISTORICAL`) before executing. LIVE → real Alpaca trades + `trading.decisions`. HISTORICAL → sim trades → `trading.historical_decisions` only. No real money moves outside market hours.
15. **Per-trader cron isolation**: Each trader gets its own tick cron (not one shared cron). Prevents cascading timeouts and allows per-trader timeout tuning.
16. **Trader self-verification**: Each trader must pass a self-check (API keys, Alpaca, DB, execution) before being considered ready. Nightly maintenance runs these checks and fixes failures.

---

## Mode System (v3 — 2026-07-14)

Traders operate in one of two modes, flipped by cron at market open/close:

| Time | Mode | Execution Target | DB Table |
|------|------|-----------------|----------|
| 9:30 AM ET | **LIVE** | Real Alpaca trades | `trading.decisions` |
| 4:00 PM ET | **HISTORICAL** | Simulated only | `trading.historical_decisions` |

**Mode flip crons:**
- `Mode Flip: LIVE (market open)` — 9:30 AM ET Mon-Fri → `python3 scripts/mode_manager.py all live`
- `Mode Flip: HISTORICAL (market close)` — 4:00 PM ET Mon-Fri → `python3 scripts/mode_manager.py all historical`

**Mode state file** per trader: `state/mode_{trader}.json`

## Tick Architecture (v3 — 2026-07-14)

Three separate per-trader tick crons, offset by 1 minute to prevent concurrent Alpaca calls:

| Cron | Schedule | Trader |
|------|----------|--------|
| `Stonks Tick (5-min)` | `*/5 9-16 * * 1-5` | trader-stonks |
| `Kairos Tick (5-min)` | `1-56/5 9-16 * * 1-5` | trader-kairos |
| `Aldridge Tick (5-min)` | `2-57/5 9-16 * * 1-5` | trader-aldridge |

**Each tick flow:**
1. Check mode → LIVE or HISTORICAL
2. `sessions_send` MARKET TICK to trader
3. Trader analyzes (data-bus tools: `get_portfolio`, `get_quotes`, `get_sentiment`)
4. Trader decides BUY/SELL/HOLD
5. Trader executes trade via `exec` using `scripts/place_order.py`
6. Tick runner reads reply via `sessions_history` and saves to DB

## Nightly Maintenance Pipeline (v3 — 2026-07-14)

Runs at 5:00 AM ET Mon-Fri via cron `Nightly Pre-Market Maintenance`.

**`scripts/nightly_check.py`** verifies:
1. PostgreSQL connectivity
2. Alpaca API connectivity (all 3 traders)
3. Data bus health (port 5000)
4. Database schema integrity (`trading.decisions`, `trader_decisions`, `trades`)
5. Recent decisions present (last 24h)
6. Gateway config valid (all 3 traders configured)
7. All tick crons present and enabled
8. Trader workspaces exist
9. Portfolio consistency (open positions, buying power, cash %)
10. Trade execution smoke test (`place_order.py` functional)
11. Backtest data available
12. Log files present

**Per-trader self-check**: `scripts/trader_check.py <trader>` — runs 7 checks (API keys, Alpaca, portfolio, data bus, order API, DB, open orders). Generates readiness report.

**Auto-fix capability**: The 5 AM cron agent has `exec` + `cron` + `gateway` tools and a 2-hour budget to iterate on fixes before re-checking.

## Trade Execution Pipeline (v3 — 2026-07-14)

`scripts/place_order.py <trader_id> <BUY|SELL> <ticker> <qty>` places market orders via Alpaca paper trading API. Checks existing positions for SELL availability. Supports bracket orders (stop loss + take profit).

API keys convention (checked in order):
- `{TRADER}_API_KEY` / `{TRADER}_SECRET_KEY`
- `ALPACA_{TRADER}_KEY` / `ALPACA_{TRADER}_SECRET`

## Execution Scripts

| Script | Purpose |
|--------|---------|
| `scripts/place_order.py` | Place Alpaca market orders |
| `scripts/mode_manager.py` | Get/set/auto-detect trader mode |
| `scripts/nightly_check.py` | Full pre-market readiness check |
| `scripts/trader_check.py` | Per-trader self-check ("can I work?") |

---

## Agent Communication

Native webhooks power bidirectional agent-to-agent communication:

| Endpoint | Host | Purpose |
|----------|------|---------|
| `/hooks/wake` | OpenClaw .41:18789 | Hermes messages Casper (Bearer: `hermes-hook-2026`) |
| `/hooks/agent` | OpenClaw .41:18789 | Agent-to-agent dispatch |
| `POST /webhooks/main` | Hermes .131:8644 | Casper messages Hermes (HMAC signed) |

**Chat bridge (fallback):** `localhost:8644` server when webhooks are unavailable.

---

## Sub-Spec Index

| Spec File | Covers | Status |
|-----------|--------|--------|
| [`specs/architecture.md`](specs/architecture.md) | Detailed hardware, data flow, agent comms | 🟢 Active |
| [`specs/objective-function.md`](specs/objective-function.md) | Calmar, Sortino, PF, composite score, benchmarks | 🟢 Active |
| [`specs/signal-engine.md`](specs/signal-engine.md) | Gradient descent tuning, XGBoost classifier, params | 🟢 Active |
| [`specs/trader-ticks.md`](specs/trader-ticks.md) | Tick architecture, heartbeat, prompt structure, cold start | 🟢 Active |
| [`specs/kmeans-regime.md`](specs/kmeans-regime.md) | K-Means regime detection, feature vectors, cluster analysis | 🟢 Active |
| [`specs/validation.md`](specs/validation.md) | Walk-forward validation, statistical significance | 🟢 Active |
| [`specs/virtual-traders.md`](specs/virtual-traders.md) | Virtual trader system — param/prompt variants | 🟢 Active |
| [`specs/virtual-trader-rotation.md`](specs/virtual-trader-rotation.md) | Weekly culling/promotion of virtual traders | 🟢 Active |
| [`specs/nightly-optimization-pipeline.md`](specs/nightly-optimization-pipeline.md) | Nightly sweep pipeline, worker orchestration | 🟢 Active |
| [`specs/drawdown-management.md`](specs/drawdown-management.md) | Circuit breaker, cooling-off, recovery mode | 🟢 Active |
| [`specs/cold-start.md`](specs/cold-start.md) | Warm-up period, default params, bootstrap phase | 🟢 Active |
| [`specs/learning-simulation.md`](specs/learning-simulation.md) | Simulation engine, hypothesis generation, tool learning | 🟢 Active |
| [`specs/agent-files.md`](specs/agent-files.md) | OpenClaw file types, size limits, prompt assembly | 🟢 Active |
| [`specs/operational-hygiene.md`](specs/operational-hygiene.md) | Prompt deployment, cron hygiene, monitoring, bootstrap gates | 🟢 Active |
| `scripts/place_order.py` | Alpaca trade execution | 🟢 v3 NEW |
| `scripts/mode_manager.py` | LIVE/HISTORICAL mode management | 🟢 v3 NEW |
| `scripts/nightly_check.py` | Pre-market 12-point readiness check | 🟢 v3 NEW |
| `scripts/trader_check.py` | Per-trader 7-point self-check | 🟢 v3 NEW |

**Future / aspirational** (not implemented, tracked for later):
- Regime-scoped parameter overrides
- Multi-timeframe evaluation (5-min, 1-hour, daily)
- Counterfactual analysis (hold-longer simulation)
- Cross-trader learning / signal board
- DB migration system (Phase 2)
- HMM regime model integration
- Genetic algorithm for variant generation

---

## Current Bootstrap State (Kairos, Jul 9)

| Parameter | Value | Notes |
|-----------|-------|-------|
| Confidence threshold | 0.15 | Low bar to get trades flowing |
| Position size | 5% of equity | Generates meaningful signal |
| Stop loss | 5% | Standard |
| Ticker universe | 20 | Broad enough for opportunity |
| Shorts allowed | Yes | Generates data both directions |
| Risk gate behavior | WARN (not VETO) | First 30 trades |
| PG reader | Direct PG reads | Fully migrated from SQLite |

---

## Quick Reference

```bash
# Run tests
python3 -m pytest tests/ -v

# Historical replay
python3 src/replay.py --date 2026-07-01

# Pre-market format validation (8 AM ET)
python3 scripts/validate_prompt_format.py

# Nightly sweep
python3 scripts/night_pipeline_v2.py

# Create issue
gh issue create --repo Tesselation-Studios/paper-trading-rebuild --title "..." --label "bug"
```

---

## Current State Assessment (2026-07-14 v3)

### ✅ v3 Architecture Complete

| Subsystem | Status | Details |
|-----------|--------|---------|
| Mode system (LIVE/HISTORICAL) | ✅ Deployed | Auto-flip at 9:30AM/4:00PM via crons |
| Per-trader tick crons | ✅ Deployed | 3 separate 5-min crons, offset by 1 min |
| Trade execution via exec | ✅ Deployed | `place_order.py` + Alpaca API |
| Nightly maintenance | ✅ Deployed | 5 AM ET, 12-point check + auto-fix |
| Per-trader self-checks | ✅ Deployed | 7-point verification per trader |
| Data bus (port 5000) | ✅ Live | SPY $751, regime: CHOPPY |
| PostgreSQL | ✅ Live | 2520 decisions, 1304 trades |

### 🔴 Known Issues

| Issue | Impact | Plan |
|-------|--------|------|
| Stonks 83% cash — underinvested | Missing opportunity | Monitor, not urgent |
| No backtest data directory | No historical validation | Rebuild data pipeline |
| Dashboard disconnected from DB | No live portfolio view | Investigate dashboard-DB link |
| Aldridge max DD >15% | Circuit breaker not triggered | Implement knockout rule |
| Prompt bloat in AGENTS.md | Risk of silent truncation | Move synthesis to separate file |

### Live Trader State (2026-07-14 ~11:00 AM ET)

| Trader | Portfolio | Positions | Mode | Status |
|--------|-----------|-----------|------|--------|
| **Stonks** | $10,596 | NVDA (2), HOOD (12) | LIVE | 🟢 Trading |
| **Kairos** | $9,274 | AAPL, BAC, CSCO, IWM, JNJ, KHC, KO, META, MSFT, PG, PLTR, PR | LIVE | 🟢 Trading |
| **Aldridge** | $10,271 | AVGO (1) + 14 others | LIVE | 🟢 Trading |

### Last Trade Executed
- Stonks: BUY 1 NVDA @ $207.05 (bracket: SL $203, TP $215) — 10:52 AM
- Aldridge: BUY 1 AVGO @ $395.18 — 10:53 AM

### Operational Crons

| Cron | Schedule | Status |
|------|----------|--------|
| Stonks Tick (5-min) | `*/5 9-16 * * 1-5` | ✅ |
| Kairos Tick (5-min) | `1-56/5 9-16 * * 1-5` | ✅ |
| Aldridge Tick (5-min) | `2-57/5 9-16 * * 1-5` | ✅ |
| Mode Flip: LIVE | 9:30 AM Mon-Fri | ✅ |
| Mode Flip: HISTORICAL | 4:00 PM Mon-Fri | ✅ |
| Nightly Pre-Market Maintenance | 5:00 AM Mon-Fri | ✅ |