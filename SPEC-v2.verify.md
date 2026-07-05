# SPEC-v2 Verification

> Every scenario in this file maps to an executable pytest test.
> Run: `pytest tests/test_spec_verify.py -v`
> CI runs this on every PR. Red = no merge.

## 1. Architecture

- [ ] **ARCH-001**: Both repos (paper-trading-teams, paper-trading-agents) exist and are clonable
- [ ] **ARCH-002**: `import paper_trading` succeeds without side effects (no sys.exit, no network)
- [ ] **ARCH-003**: All 8 architectural invariants (§1.3) are enforceable — CI checks for async confirmation loops, no deployed-but-not-called code, git-only deploys

## 2. Agents

- [ ] **AGENT-001**: All 3 agent configs load from YAML without error
- [ ] **AGENT-002**: Context blob matches JSON schema (all required fields present)
- [ ] **AGENT-003**: Agent heartbeat runs to completion without unhandled exception (dry run)
- [ ] **AGENT-004**: Hours gate blocks trading outside 09:30–16:00 ET (weekend/off-hours test)
- [ ] **AGENT-005**: Off-hours heartbeats run learning loop but submit zero orders

## 3. Data Bus

- [ ] **BUS-001**: `/health` returns 200 with JSON body including per-source freshness
- [ ] **BUS-002**: `/quotes?symbols=AAPL` returns `price` field (not `close`)
- [ ] **BUS-003**: All 10 endpoints respond within 5 seconds
- [ ] **BUS-004**: All HTTP calls in data_bus.py have `timeout=` parameter
- [ ] **BUS-005**: No `except: pass` in data_bus.py
- [ ] **BUS-006**: `/health` shows `status: "degraded"` when any source is stale >5min

## 4. Config

- [ ] **CFG-001**: All YAML config files parse without error
- [ ] **CFG-002**: Environment variables override YAML values
- [ ] **CFG-003**: No hardcoded values in source (search for magic numbers)
- [ ] **CFG-004**: Secrets not stored in YAML (only in env vars)

## 5. Risk

- [ ] **RISK-001**: Cash gate rejects order exceeding available cash
- [ ] **RISK-002**: Position gate rejects >20% portfolio in single position
- [ ] **RISK-003**: Exposure gate rejects orders that push exposure >100%
- [ ] **RISK-004**: PDT gate rejects 4th day trade in 5-day window
- [ ] **RISK-005**: Hours gate rejects orders outside 09:30–16:00 ET
- [ ] **RISK-006**: All gates accept optional `timestamp` parameter for replay
- [ ] **RISK-007**: Order fill polling runs (submit → poll 2s/30s → confirm filled_price → write DB)
- [ ] **RISK-008**: `filled_avg_price` never read before fill confirmation (postmortem #30 guard)

## 6. Learning Loop

- [ ] **LOOP-001**: Grader scores a completed trade with valid scores (0–100)
- [ ] **LOOP-002**: Learning loop produces actionable parameter suggestions
- [ ] **LOOP-003**: Optimizer writes config changes to agents repo
- [ ] **LOOP-004**: All loop components accept `timestamp` for replay

## 7. Replay Harness

- [ ] **REPLAY-001**: VirtualClock returns correct timestamp for any date
- [ ] **REPLAY-002**: HistoricalDataFeeder replays recorded data without live API calls
- [ ] **REPLAY-003**: HistoricalExecutor runs trader decision pipeline against recorded data
- [ ] **REPLAY-004**: Replay produces deterministic output (same input → same result)

## 8. Nightly Pipeline

- [ ] **PIPE-001**: EOD job runs to completion without unhandled exception (dry run)
- [ ] **PIPE-002**: Pipeline produces valid config change proposal
- [ ] **PIPE-003**: Pipeline output is idempotent (same input → same proposal)

## 9. CI/CD

- [ ] **CI-001**: `pytest tests/` passes with 0 failures on ubuntu-latest
- [ ] **CI-002**: `pytest tests/ --cov=src/` reports coverage ≥80%
- [ ] **CI-003**: HTTP `timeout=` enforcement check exists in CI
- [ ] **CI-004**: No `except: pass` in any source file
- [ ] **CI-005**: Foreign key constraints enforced on all DB tables
- [ ] **CI-006**: `.verify.md` claims are validated against actual test coverage

## 10. Auto-Heal

- [ ] **HEAL-001**: `cron_watchdog.py` detects missing crons and alerts
- [ ] **HEAL-002**: `data_freshness.py` detects stale data and alerts
- [ ] **HEAL-003**: `process_alive.py` detects dead processes and alerts
- [ ] **HEAL-004**: `db_integrity.py` detects orphan trades and alerts
- [ ] **HEAL-005**: Alert severity levels (info/warning/critical/emergency) route to correct destinations
- [ ] **HEAL-006**: Auto-heal monitors run as `no_agent` cron (zero tokens) — agent spawned only on critical+
- [ ] **HEAL-007**: Test for each invariant violation — confirm the system detects and alerts
