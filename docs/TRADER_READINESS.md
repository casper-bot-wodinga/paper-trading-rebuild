# Trader Readiness Checklist

> Last updated: 2026-07-15

## Infrastructure

| Component | Status | Notes |
|-----------|--------|-------|
| Data-bus (.41:5000) | ✅ Healthy | All collectors running (social, momentum, macro, sentiment, flow, insiders, technical_scan, risk) |
| Data-bus (docker.klo) | ✅ Healthy | Up 7+ hours, serving dashboard |
| PostgreSQL (docker.klo) | ✅ Running | `trading.decisions`, `trades`, `portfolio_snapshots`, `trader_positions` tables populated |
| Dashboard (.179:5002) | ✅ Live | 3 traders visible, /api/decisions, /api/pnl, /api/summary, /api/trades endpoints working |
| Dashboard (.131:5002) | ✅ Live | Same codebase, also serving |
| Traefik (docker.klo) | ✅ Live | HTTPS routing for all services |

## Trader Agents

| Agent | Config | Alpaca Keys | Exec Access | Ticks | Status |
|-------|--------|------------|-------------|-------|--------|
| Kairos (Zara Chen) | ✅ | ✅ | ✅ | */5 min | ✅ Active |
| Stonks (Stan Hoolihan) | ✅ | ✅ | ✅ | */5 min | ✅ Active |
| Aldridge (Edmund Whitfield) | ✅ | ✅ | ✅ | */5 min | ✅ Active |

## Pipeline

| Component | Status | Notes |
|-----------|--------|-------|
| CI pipeline | ✅ | 2-phase: unit tests + integration (Docker Compose + Playwright E2E) |
| Webhook | ✅ | `hello.wodinga.studio/hooks/github` — sends all events |
| Auto-merge | ✅ | Bot PRs auto-merge on green CI |
| Tick cron | ✅ | `*/5 9-16 * * 1-5` — dispatches to all traders |
| Decision sync | ✅ | `sync_decisions_to_pg.py` with --agent and --dry-run flags |
| Position sync | ✅ | `sync_alpaca_positions.py` loads .env, runs every 5 min |
| Price refresh | ✅ | Dashboard queries data-bus for fresh prices |
| Historical sim | ✅ | `historical_sim.py` works for all 3 traders |
| Nightly replay | ✅ | `nightly_replay.py` — 252k rows → 3697 bars functional |
| Learning loop | 🟡 Pending | Needs Postgres wiring (P1 task) |
| Meta-cog loop | 🟡 Pending | Depends on learning loop (P1 task) |
| Agent reflection | 🟡 Pending | End-of-day meta-cognition (P2 task) |
| News pipeline | 🟡 Pending | News sentiment discovery (P2 task) |
| Virtual trader loop | 🟡 Pending | Compete/compare/evolve pipeline (P2 task) |

## Risk Gates

| Gate | Status | Notes |
|------|--------|-------|
| BootstrapGate | ✅ | Allows first N trades |
| HoursGate | ✅ | Blocks trades outside 9:30-16:00 ET |
| Bankroll ceiling | ✅ | `portfolio_value * 0.01` per trader |
| Circuit breakers | ✅ | Active |

## Known Issues

| Issue | Status | Notes |
|-------|--------|-------|
| #132: Trade PnL not persisted | ✅ Fixed | Backfill from entry/exit prices, 634 sweep entries deleted |
| #131: Aldridge buys rejected | ✅ Fixed | Pre-trade cash validation, proper StopOrderRequest |
| #130: Legacy DB entries | ✅ Fixed | Deleted sweep entries, dashboard filters to 3 live traders |
| #129: Aldridge PnL missing | ✅ Fixed | Backfilled from entry/exit prices |
| #135: Agent IDs mixed | ✅ Fixed | Cleaned decisions table |
| #134: Duplicate portfolio snapshots | ✅ Fixed | Unique constraint added |
| #133: HOLD noise | ✅ Fixed | Filtered from dashboard |
| #111: pandas_ta import | ✅ Fixed | Catch-all Exception handling |
| #113: CI tests | ✅ Fixed | Dockerfile.test + yahooquery for containerized CI |
| Homelab deploy runner | 🟡 Jet fixing | PR #221 — stoat caddy-config permission issue |