# Containerization & Testing Methodology

> **Spec**: Containerization & Testing Methodology
> **Status**: Active — v1.0.0
> **Date**: 2026-07-15
> **Author**: Raf
> **Motivation**: Make the trading stack fully containerized, deterministically testable, and CI-repeatable — no homelab dependencies, no flaky tests, no "works on my machine."

---

## 1. Why Containerize?

### 1.1 The Problem Before

The paper trading system grew organically across three machines:

| Service | Machine | Access | Dependencies |
|---------|---------|--------|-------------|
| Data Bus | OpenClaw (.41) | `docker.klo:5000` | Alpaca API keys, SQLite, MCP |
| Postgres | Docker (.179) | `docker.klo:5433` | pgvector/pg15 image |
| Dashboard | OpenClaw (.41) | `docker.klo:5004` | Postgres DSN, live data |
| Simulator | Docker (.179) | N/A | Parquet cache, yfinance |

Testing required:
- The homelab to be up
- API keys to be present
- Network connectivity between .41, .179, and .96 (TrueNAS)
- Market hours for live data
- Real databases with real trading history

This meant:
- **No offline testing** — you couldn't run tests on a plane, at a coffee shop, or in a fresh CI runner
- **No deterministic results** — live market data changes between runs
- **No CI pipeline** — GitHub Actions runners can't reach `docker.klo`
- **No regression safety** — changes shipped with "I tested it in prod" confidence

### 1.2 The Solution

All three services (databus, database, trading dashboard) live in a single Docker Compose stack that:

1. **Runs on any Docker host** — laptop, CI runner, Raspberry Pi
2. **Needs zero external dependencies** — no API keys, no homelab network, no market data
3. **Produces deterministic results** — seeded test data, fixed random seeds, pinned container versions
4. **Tests the same binary that runs in production** — same Dockerfile, same entrypoint, same config

---

## 2. Service Architecture

### 2.1 Service Map

```
┌──────────────────────────────────────────────────────────────────┐
│                    docker-compose.test.yml                        │
│                                                                  │
│  ┌──────────────┐    ┌──────────────────┐                      │
│  │  trading-db   │    │     seed         │  (one-shot, exits)   │
│  │  (Postgres)   │◄───│  seed_test_data  │                      │
│  │  :5432        │    │  .py (determin-  │                      │
│  └──────┬───────┘    │   istic)          │                      │
│         │            └──────────────────┘                      │
│         │                                                      │
│         │            ┌──────────────────┐                      │
│         ├───────────►│   data-bus       │                      │
│         │            │  :5000           │                      │
│         │            │  (no external    │                      │
│         │            │   API keys)      │                      │
│         │            └────────┬─────────┘                      │
│         │                     │                                │
│         │            ┌────────▼─────────┐                      │
│         └───────────►│   dashboard       │                      │
│                      │  :5002            │                      │
│                      │  (leaderboard_api │                      │
│                      │   or pg_dashboard)│                      │
│                      └────────┬─────────┘                      │
│                               │                                │
│                      ┌────────▼─────────┐                      │
│                      │   test-runner     │  (one-shot, exits)  │
│                      │  pytest +         │                      │
│                      │  Playwright       │                      │
│                      └──────────────────┘                      │
└──────────────────────────────────────────────────────────────────┘
```

### 2.2 Service Descriptions

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| `trading-db` | `postgres:15-alpine` | 5432 | Postgres with `trading` and `market_data` schemas |
| `seed` | Custom (Dockerfile) | — | One-shot deterministic data loader |
| `data-bus` | Custom (Dockerfile) | 5000 | Market data HTTP service (no API keys) |
| `dashboard` | Custom (Dockerfile) | 5002 | API + static frontend for trading UI |
| `test-runner` | Custom (Dockerfile) | — | One-shot test suite runner |

### 2.3 Key Design Decisions

**1. `tmpfs` for Postgres data**
- `tmpfs: /var/lib/postgresql/data` means every test run starts with a clean database
- No volume persistence = no state leakage between runs
- Set `POSTGRES_HOST_AUTH_METHOD=trust` so no passwords needed

**2. `--abort-on-container-exit` for test-runner**
- When the test runner exits non-zero, the whole stack shuts down immediately
- `docker compose up --exit-code-from test-runner` returns the test runner's exit code

**3. Seed service uses `random.seed(42)`**
- Every run produces identical data
- Tests assert on exact values, not ranges
- `random.seed()` is deterministic across Python versions on the same platform

**4. Data-bus starts without API keys**
- All endpoints that would hit external APIs return empty/fallback data
- Internal endpoints (health, quotes from cache, etc.) work without any keys
- The `PAPER=true` env var is set to suppress key loading

**5. Dashboard uses `leaderboard_api.py` by default**
- Serves both the API and the static frontend from a single process
- Simpler than `pg_dashboard.py` for testing (no real-time WebSocket concerns)

---

## 3. Testing Strategy

### 3.1 Test Pyramid

```
         ╱╲
        ╱  ╲
       ╱ E2E╲          ← Playwright: full browser tests
      ╱────────╲
     ╱ Integration╲     ← pytest: running services, real DB
    ╱──────────────╲
   ╱   Unit Tests    ╲  ← pytest: pure logic, mocked I/O
  ╱────────────────────╲
```

### 3.2 Test Layers

#### Layer 1: Unit Tests (fast, no Docker)

| What | Framework | Run | Examples |
|------|-----------|-----|----------|
| Pure logic | pytest | `pytest tests/ -m "not integration and not e2e"` | Signal parsing, format validation, config loading, math helpers |
| Mocked I/O | pytest + unittest.mock | Same command | Risk gates, decision scoring, parameter optimization |
| Guardrails | pytest | `pytest tests/test_guardrails.py` | No bare except, no hardcoded IPs, no f-string SQL |

**File pattern**: `tests/test_*.py` (existing, ~40 test files)
**No external deps needed**: Mocked database, mocked HTTP, mocked time

#### Layer 2: Integration Tests (Docker Compose required)

| What | Framework | Run | Examples |
|------|-----------|-----|----------|
| DB queries | pytest + psycopg2 | Inside test-runner container | Schema exists, seed data matches, queries return expected rows |
| Data-bus API | pytest + requests | Inside test-runner container | Health endpoint, cache stats, signal format |
| Dashboard API | pytest + requests | Inside test-runner container | /api/traders, /api/positions, /api/journal |
| Data flow | pytest + requests | Inside test-runner container | Data-bus → Postgres → Dashboard consistency |

**File pattern**: `tests/integration/test_*.py`
**Marked with**: `@pytest.mark.integration`

#### Layer 3: E2E Tests (Docker Compose + Playwright)

| What | Framework | Run | Examples |
|------|-----------|-----|----------|
| Dashboard UI | Playwright | Inside test-runner container | Page loads, trader cards render, modal opens/closes |
| Data rendering | Playwright | Inside test-runner container | Portfolio values shown, positions listed, journal entries visible |
| Browser behavior | Playwright | Inside test-runner container | Status transitions, auto-refresh, error states |

**File pattern**: `tests/e2e/test_*.spec.js` (existing)
**Marked with**: `@pytest.mark.e2e` (in the orchestrator)

### 3.3 Running Tests

```bash
# ── Unit tests only (no Docker needed) ────────────────────────────
pytest tests/ -m "not integration and not e2e"

# ── Full stack: unit + integration + E2E in Docker ────────────────
make test

# ── Integration tests only (start stack, run tests, tear down) ────
make test-integration

# ── E2E tests only ────────────────────────────────────────────────
make test-e2e

# ── Start stack in background for manual testing ──────────────────
make up
# ... run tests manually ...
make down
```

---

## 4. Deterministic Test Data

### 4.1 Principles

1. **`random.seed(42)`** — fixed seed for all seeded data generation
2. **No live data** — all data is synthetic, generated by the seed service
3. **Known values** — tests assert on exact known values, not ranges or patterns
4. **Idempotent** — running the seed service twice produces identical data

### 4.2 Data Sets

| Dataset | Tables | Size | Purpose |
|---------|--------|------|---------|
| **Traders** | `agent_profile`, `agent_state` | 3 traders | 3 trader personas (Kairos, Aldridge, Stonks) |
| **Portfolio history** | `portfolio_snapshots`, `equity_snapshots` | 30 days × 3 traders | Equity curve for score calculation |
| **Positions** | `trader_positions` | 2-4 per trader | Open positions in the dashboard |
| **Decisions** | `trader_decisions` | 3 per trader | Activity feed content |
| **Journal** | `trader_journal` | 3 per trader | Journal feed content |
| **Orders** | `orders` | 5 per trader | Trade history |
| **Benchmarks** | `market_data.bars` | SPY, QQQ | Benchmark comparison |
| **Signals** | `signals` | 7 tickers | Signal panel data |
| **Watchlists** | `trader_watchlist` | 10 per trader | Watchlist panel |
| **Risk events** | `risk_events` | 5 total | Vetoes panel |
| **Quotes cache** | In-memory (data-bus) | 3 tickers | API response data |

### 4.3 Known Values (for assertions)

With `random.seed(42)`, the following values are deterministic:

```
Kairos portfolio:  $10,423.15
Aldridge portfolio:  $9,874.62
Stonks portfolio:  $11,246.88

Positions:
  Kairos: AAPL (10 shares @ $218.50), MSFT (5 shares @ $425.30)
  Aldridge: AAPL (10 shares @ $218.50), MSFT (5 shares @ $425.30)
  Stonks: AAPL (10 shares @ $218.50), MSFT (5 shares @ $425.30),
          TSLA (25 shares @ $245.00), NVDA (8 shares @ $130.20)

First journal entry:
  Kairos: "AAPL showing strong momentum on daily timeframe..."
  Aldridge: "AAPL's P/E of 28 feels rich..."
  Stonks: "GME to the moon!..."
```

---

## 5. CI Pipeline

### 5.1 Pipeline Architecture

```
GitHub PR/Push
      │
      ▼
┌─────────────────────┐
│  Phase 1: ci         │
│  ┌─────────────────┐│
│  │ guardrails       ││  ← Static analysis
│  │ bug-regression   ││  ← Known bug regression tests
│  │ js-syntax        ││  ← JavaScript syntax check
│  │ no-bare-except   ││  ← Guardrails enforcement
│  │ docker-build     ││  ← Docker image builds
│  └─────────────────┘│
└─────────┬───────────┘
          │ (pass)
          ▼
┌─────────────────────┐
│  Phase 2: integration│
│  ┌─────────────────┐│
│  │ Docker Compose   ││  ← Spin up test stack
│  │ up --build -d    ││
│  ├─────────────────┤│
│  │ Verify seed data ││  ← Health check API
│  │ Integration tests││  ← pytest integration tests
│  │ E2E tests        ││  ← Playwright browser tests
│  └─────────────────┘│
└─────────┬───────────┘
          │ (pass)
          ▼
    Auto-merge (bot PRs)
```

### 5.2 CI Rules

1. **Phase 1 must pass before Phase 2 starts** (reduces wasted Docker build time)
2. **Phase 2 uses `--exit-code-from test-runner`** — test exit code propagates to CI
3. **All services use `tmpfs` for Postgres** — no state between runs
4. **Artifacts always uploaded** — Playwright report, test logs, service logs on failure
5. **Failure dumps all service logs** — seed, data-bus, dashboard, Postgres

---

## 6. File Map

```
paper-trading-rebuild/
├── docker-compose.yml            # Production stack
├── docker-compose.test.yml       # CI/test stack (self-contained)
├── Dockerfile                    # Base image for all Python services
├── Makefile                      # Convenience targets (test, up, down, clean)
├── requirements.txt              # Python dependencies
├── pytest.ini                    # pytest configuration
├── playwright.config.js          # Playwright configuration
├── scripts/
│   └── seed_test_data.py         # Deterministic data seeder
├── tests/
│   ├── conftest.py               # Shared fixtures (mocks, temp_db)
│   ├── test_*.py                 # Unit tests (40+ files)
│   ├── integration/
│   │   ├── test_db_schema.py     # Database schema & seed data
│   │   ├── test_data_bus_api.py  # Data-bus endpoint integration
│   │   ├── test_dashboard_api.py # Dashboard API integration
│   │   └── test_data_flow.py     # Cross-service data flow
│   └── e2e/
│       └── test_dashboard.spec.js # Playwright browser tests
├── specs/
│   └── containerization-testing-methodology.md  # ← This file
└── .github/workflows/
    ├── ci.yml                    # CI pipeline
    └── auto-merge.yml            # Auto-merge for bot PRs
```

---

## 7. Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Test data drifts from seed output | Pin seed data to a known hash. CI asserts seed output matches expected values. |
| Integration tests depend on specific port availability | All services use fixed internal ports in Docker network. Test runner connects via container names. |
| Playwright requires Chromium in test container | Multi-stage Dockerfile with playwright browsers. Or use `mcr.microsoft.com/playwright` base image. |
| Docker Compose test stack is slow to start | GitHub Actions caches Docker layers. `tmpfs` Postgres starts in <1s. Seed service runs in <2s. |
| Unit tests in CI don't match Docker environment | Phase 1 runs on host Python (not Docker) for speed. Phase 2 runs inside Docker for fidelity. Both pass before merge. |

---

## 8. Invariants

1. **Every test must be containerizable** — zero homelab dependencies
2. **Deterministic seed** — `random.seed(42)` must be the only source of randomness
3. **No API keys in test stack** — `PAPER=true` environment variable blocks key loading
4. **Clean state per run** — `tmpfs` Postgres, no volumes, no persisted state
5. **Phase 1 gates Phase 2** — CI pipeline stages: unit → integration → E2E → merge
6. **Test exit code propagates** — `docker compose --exit-code-from test-runner`
7. **Seed data is versioned** — changing seed data requires updating assertion values