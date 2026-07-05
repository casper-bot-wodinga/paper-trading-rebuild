# Paper Trading Teams — System Specification v2

> Rebuild spec. Learnings from v1 bug sweeps (32 issues filed) baked into the design.
> Every section has an executable `.verify.md`. If it's not tested, it's not done.
> **Sub-specs**: derive from this master. No sub-spec describes features not yet built in this spec.

---

## 1. Architecture

```
External APIs → Data Bus (port 5000) → Traders (OpenClaw agents)
                   ↕                        ↓
              trader.db                  Trade Grader
                                            ↓
                                       Learning Loop
                                            ↓
                                       Param Optimizer → agents repo
```

### 1.1 What Changed from v1

| v1 Problem | v2 Fix |
|---|---|
| Webhooks, circuit breaker, simulation dashboard spec'd but never built | Removed. Don't spec until we schedule the build pass. |
| 15 HTTP requests lacked `timeout=` | CI enforces `timeout=` on all HTTP calls. |
| `.verify.md` scenarios had zero executable tests | Every verify scenario IS a pytest test. |
| Config scattered across DB, env, hardcoded | YAML config + env overrides. Zero DB dependency for config. |
| Trader prompts mixed with engine code | Traders read prompts from `paper-trading-agents` repo. Self-evolvable. |
| Cron scripts vanished, nobody noticed | Auto-heal cron checker from day one. |
| 263 orphan trades from missing foreign keys | FK constraints on all trade/decision tables. |
| Test coverage lied in `.verify.md` files | CI checks coverage against verify claims. |
| Multiple CI workflows with different rules | Single workflow, single truth. |
| Data bus had config baked in | YAML-driven. |

### 1.2 Repos

- **paper-trading-teams**: Engine code — data bus, trade execution, learning loop, test harness, CI
- **paper-trading-agents**: Trader prompts, strategies, personalities, MEMORY.md — traders self-evolve by committing here

### 1.3 Architectural Invariants (carried forward from v1)

These are the laws of the system. Every change, fix, and feature MUST respect them.
Violating any of these is a bug — even if the code runs.

| # | Invariant | From postmortem |
|---|-----------|-----------------|
| **1** | Write on every transaction — DB reflects reality within the same heartbeat tick | #30, #55 |
| **2** | Every data producer has a consumer — no "deployed but not called" code | #43 |
| **3** | Async APIs need confirmation loops — never trust the initial response | #30 |
| **4** | Health checks verify **functionality**, not liveness — test sub-services, not just "Flask is up" | Data bus outage |
| **5** | Postmortems + SPEC updates mandatory for every bug (P0–P3) | All 9 postmortems |
| **6** | Deploy via git push (not scp) — single canonical branch, no drift | Process |
| **7** | Every module exports a callable `run()` that integration tests can exercise | #43, #50 |
| **8** | Gateway restarts must be coordinated — warn the other agent first | Session crashes |

---

## 2. Agents

| ID | Name | Strategy | Model (default) |
|----|------|----------|-----------------|
| `trader-kairos` | Zara Chen | Momentum + ML regime | deepseek-v4-flash |
| `trader-aldridge` | Edmund Whitfield | Value + fundamentals | deepseek-v4-flash |
| `trader-stonks` | Stan Hoolihan | Meme + community | deepseek-v4-flash |

**Heartbeat**: Every 5 min during market (09:30–16:00 ET, M–F). Each tick:
1. Read context blob (pre-aggregated by data bus)
2. Read last journal + open positions
3. Decide: trade / hold / journal
4. Write to DB

**Config**: Each agent has a YAML file in `paper-trading-agents/traders/{name}/config.yaml` defining:
- Strategy parameters (thresholds, weights, limits)
- Risk profile (max position, max exposure, stop-loss)
- Model routing (primary + fallback chain)

### 2.1 Context Blob Contract

```json
{
  "agent": {"id": "...", "cash": 9505.37, "portfolio_value": 10587.50, "pnl_pct": 5.88},
  "positions": [{"ticker": "AAPL", "qty": 2, "entry": 281.12, "current": 288.50}],
  "signals": {"social_sentiment": {"AAPL": 0.72}, "fear_greed": 45, "regime": "TRENDING"},
  "market": {"SPY_change_pct": 0.34, "VIX": 18.2},
  "recent_decisions": [{"action": "HOLD", "ticker": "AAPL", "confidence": 0.65}]
}
```

No webhooks. No push notifications. Agents pull. Keep it simple.

### 2.2 Weekend / Off-Hours Behavior

Traders run heartbeats **24/7** — weekends and holidays included. The only difference:

| Time | Trading | Learning | Journal |
|------|:-------:|:--------:|:-------:|
| Market hours (M–F 09:30–16:00 ET) | ✅ | ✅ | ✅ |
| Off-hours (nights, weekends, holidays) | ❌ | ✅ | ✅ |

Off-hours heartbeats run the learning loop, review past trades, tune parameters, and journal
observations. No orders are submitted. The hours gate (§5) enforces this automatically.

This prevents the "sat idle for 3 weeks" failure mode from v1.

---

## 3. Data Bus

Single HTTP service at `localhost:5000`. Rate-limits all external APIs centrally.

### 3.1 Endpoints

| Method | Path | Cache | Notes |
|--------|------|-------|-------|
| GET | `/health` | live | Includes per-endpoint freshness + circuit state |
| GET | `/quotes?symbols=A,B` | 5s | Price + RSI/MACD/MA20. Field is `price` not `close`. |
| GET | `/macro` | 6h | FRED + yield curve |
| GET | `/sentiment?symbol=A` | 5m | FinBERT NLP |
| GET | `/insiders?symbols=A,B` | 30m | SEC Form 4 |
| GET | `/fear_greed` | 30m | 0–100 |
| GET | `/social?source=all` | 3m | Reddit/Bluesky/Stocktwits |
| GET | `/signals` | live | Inter-agent signal board |

### 3.2 Requirements

- **Every HTTP call has `timeout=` parameter** (CI-enforced)
- **No silent `except: pass`** (CI-enforced)
- **No `sys.exit()` on import failure** — graceful degradation
- **All endpoints respond within 5s** or return stale cache + warning
- **Health endpoint includes per-source freshness**: `{"fear_greed": {"age_s": 45, "status": "fresh"}}`

---

## 4. Config System

All config lives in `config/` directory as YAML files. Environment variables override YAML values.

```
config/
  data_bus.yaml      — endpoints, cache TTLs, API keys (from env)
  traders.yaml        — agent model routing, heartbeat intervals
  risk.yaml           — risk gate parameters, thresholds
  paper.yaml          — paper trading account config
```

**Rules:**
- No DB dependency for config
- No hardcoded values in source
- Every config key has a test
- Secrets come from env vars or `.env`, never in YAML

---

## 5. Risk System

Composable gates. Each gate is a pure function: `(context, action) → bool`.

```
Risk Pipeline:
  Proposed Action → [Cash Gate] → [Position Gate] → [Exposure Gate] → [PDT Gate] → Approved/Rejected
```

| Gate | Rules |
|------|-------|
| Cash | Can't spend > available |
| Position | Max 20% portfolio in single position |
| Exposure | Max 100% total exposure |
| PDT | Pattern day trader rule (≤3 day trades/5 days) |
| Hours | Only trade 09:30–16:00 ET |

All gates accept optional `timestamp` parameter for historical replay.

### 5.1 Order Execution

Orders go through Alpaca's paper trading API. Every order MUST follow this lifecycle:

```
submit_order() → poll_for_fill() → confirm_filled_price → write_to_db
```

**Rules:**
- Never read `filled_avg_price` before confirming fill (postmortem #30)
- Poll every 2s for up to 30s after submission
- On fill: update DB status from `submitted` → `filled` with actual fill price
- On timeout: update DB status to `timeout` and alert
- All orders recorded with `str(order.id)` (UUID → string conversion)

---

## 6. Learning Loop

Single unified module (`src/learning_loop.py`). Merges what was previously `trade_grader.py`, `learning_loop.py`, and `param_optimizer.py`.

1. **Grader**: Scores each closed trade on entry timing, exit timing, risk management, conviction
2. **Loop**: Analyzes trade patterns → identifies parameter adjustments
3. **Optimizer**: Proposes config changes → writes to agents repo → PR for review

Harness-compatible: accepts `timestamp` parameter to run on historical data.

---

## 7. Replay Harness

Integration test bed. Feeds historical market data to traders and compares decisions.

```
Historical Data → DataFeeder → Virtual Clock → Executor → Trader Decision → Assertions
```

- Records live data bus responses for replay
- Simulates any date/time with `VirtualClock`
- Runs traders against recorded data
- Compares outputs against expected decisions
- Used in CI as integration test suite

**What's built v2:** Only the core executor + feeder + clock. What was spec'd but not built in v1 (simulation_manager, simulation_dashboard, replay_prefetch) stays OUT of v2 until Phase 3+.

---

## 8. Nightly Pipeline

Single EOD job that runs after market close. Merges what was previously separate: nightly reflection, strategy evolution, weekly scoring.

1. Fetch day's trade data
2. Grade all closed trades
3. Run learning loop
4. Propose config changes → commit to agents repo → open PR
5. Generate EOD card for dashboard

Runs on historical data. Produces actionable outputs.

---

## 9. CI/CD

Single workflow: `.github/workflows/test.yml`. No `--ignore` flags. No special cases.

```yaml
on:
  pull_request:
    branches: [main, hermes/spec-driven-rebuild]
  push:
    branches: [main, hermes/spec-driven-rebuild]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: pip install -r requirements.txt
      - run: pytest tests/ -v --tb=short
      - run: pytest tests/ --cov=src/ --cov-report=term
```

**Enforced checks in CI:**
- No HTTP call without `timeout=`
- No `except: pass` (use `except Exception:`)
- No `sys.exit()` on import
- Foreign key constraints active on all DB writes
- `.verify.md` claims match actual test coverage

### 9.1 Incremental CI Strategy

The rebuild doesn't port all 117 test files at once. CI uses `ci_skip.txt` to track which
tests haven't been ported yet. Each phase removes entries as it ports tests:

| Phase | Removes from ci_skip.txt | Tests remaining |
|-------|--------------------------|:---:|
| 0 (start) | — | 56 patterns skipped |
| 1a (test harness) | P1/P2 unit tests: config_loader, personality, ticker_discovery, etc. | ~40 |
| 1b (config) | Config-dependent tests | ~35 |
| 3 (learning loop) | learning_loop, scoring, param_optimizer | ~30 |
| 2 (risk) | risk_manager, risk_gate, execute, stop_check | ~20 |
| 4 (nightly) | nightly_reflection, strategy_evolution | ~15 |
| 5 (CI done) | **All remaining** — file is empty, no skips | **0** |

This is NOT the old `--ignore` hell — it's a burn-down list. Every PR shrinks it.

---

## 10. Auto-Heal

Cron jobs that monitor system health from day one:

| Monitor | Frequency | What It Checks |
|---------|-----------|----------------|
| `cron_watchdog` | 15 min | All trader crons exist and are enabled |
| `data_freshness` | 5 min | Data bus /health shows all sources fresh |
| `process_alive` | 5 min | Data bus process running, traders responsive |
| `db_integrity` | 1 hour | No orphan trades, foreign keys intact |

Alerts to canvas (not Telegram spam). Only escalate to Telegram on critical failure.

### 10.1 Alert Protocol

| Severity | Destination | Example | Action |
|----------|------------|---------|--------|
| **Info** | Canvas only | "Data bus /social is 2m stale" | Log, no action |
| **Warning** | Canvas + log | "Trader heartbeat 10m late" | Auto-retry next tick |
| **Critical** | Canvas + Telegram | "Data bus DOWN, all traders blocked" | Hermes/Casper alerted, auto-fix attempted |
| **Emergency** | Telegram + both agents woken | "trader.db corruption detected" | Stop all trading, human required |

Auto-heal monitors run as `no_agent` cron jobs (zero token cost). They only spawn an agent
when critical/emergency escalation is needed.

---

## 11. Build Phases

| Phase | What | When |
|-------|------|------|
| **0** | Infrastructure — CI, replay harness, agents repo, branch | Tonight |
| **1** | Test harness + config system | Tonight (parallel) |
| **2** | Risk system rewrite | Saturday AM |
| **3** | Learning loop unification | Saturday PM |
| **4** | Nightly pipeline | Saturday night |
| **5** | CI unification + hardening | Sunday |

Every phase ships with tests. Every phase is harness-compatible. Green CI = merge.
