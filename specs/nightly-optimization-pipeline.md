# Nightly Optimization Pipeline

**Parent**: [SPEC.md](../SPEC.md)
**Created**: 2026-07-06
**Updated**: 2026-07-09
**Children**: None yet
**Motivation**: Prompt sweep currently tests on a single day (N=1, massive overfitting risk). We need 10-20 trading days of historical data with walk-forward validation to trust a winner.

---

## Purpose

Expand the nightly prompt sweep from single-day backtesting to multi-date walk-forward validation. Build the historical data pipeline that feeds it: source → cache → bars → replay harness → scoring → winner promotion. Close the gap between the existing `prepopulate_data.py` (which builds Parquet bars) and `prompt_sweep.py` (which queries raw `prices` rows).

---

## Current State (Baseline)

### What exists

| Component | Location | What it does | Used by |
|-----------|----------|-------------|---------|
| `prompt_sweep.py` | `src/` | Signal-based variant scoring on 1 date | Manual / cron |
| `nightly_optimize.py` | `scripts/` | LLM-based replays on N dates | Cron (planned) |
| `prepopulate_data.py` | `scripts/` | Fetches 5-min bars → Parquet | Cron (8:30 AM ET) |
| `replay_controller.py` | `src/` | LLM-driven replay harness | nightly_optimize |
| `replay/` (rebuild) | `../paper-trading-rebuild/src/` | Signal-based ReplayHarness | prompt_sweep |
| `shared/cache.db` | DB | 90K rows `prices` table, 2025-01 → now | prompt_sweep fallback |
| Parquet bars | `shared/cache/bars/` | 5-min OHLCV per ticker | NOTHING (unused by sweep) |

### What's wrong

1. **Single-date overfitting.** `prompt_sweep.py` calls `load_historical_ticks(date_str)` with exactly one date. A variant that wins on Friday might lose every other day that week.

2. **Data path fragmentation.** Parquet bars (from prepopulate) and DB `prices` rows (from the data bus live feed) are disconnected. The sweep uses neither reliably — it falls back to *synthetic random data* when both are empty.

3. **No walk-forward validation.** Even with multi-date data, if you train (select variant) and test (validate) on the same dates, you're still curve-fitting. Walk-forward: train on days 1-5, validate on day 6; slide forward; repeat. Only variants that win across multiple walk-forward windows get promoted.

4. **No transaction cost model.** Returns are gross, not net. A variant trading 50x/day with $0.50/share slippage looks amazing without costs and bleeds dry with them.

5. **SignalEngine vs. LLM disconnect.** `prompt_sweep.py` scores variants with `SignalEngine` (deterministic rules), but the *actual* trader uses an LLM. A variant that improves SignalEngine scores might have zero effect (or negative effect) on LLM behavior because the perturbation modifies text the LLM reads, not SignalEngine parameters.

6. **No performance persistence tracking.** When a variant beats baseline on day N, was it luck or skill? Without tracking across multiple independent windows, you can't know.

---

## Target Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                      Nightly Pipeline (Cron: 22:00 ET)            │
│                                                                    │
│  ┌──────────────────┐   ┌──────────────────┐                     │
│  │ prepopulate_data  │   │ backfill_bars.py │  ← NEW              │
│  │ (daily refresh)   │   │ (deep backfill)  │                     │
│  └────────┬─────────┘   └────────┬─────────┘                     │
│           │                      │                                │
│           └──────────┬───────────┘                                │
│                      ▼                                            │
│  ┌──────────────────────────────────────┐                        │
│  │  shared/cache/bars/<ticker>.parquet  │  Unified bar store      │
│  │  (5-min OHLCV + RSI + MACD + ATR)   │                        │
│  └──────────────┬───────────────────────┘                        │
│                 │                                                 │
│                 ▼                                                 │
│  ┌──────────────────────────────────────┐                        │
│  │  BarLoader (new shared module)       │  ← NEW                  │
│  │  - load_date_range(tickers, start,   │                        │
│  │    end) → List[DayBars]              │                        │
│  │  - to_ticks(day_bars, interval)      │                        │
│  │    → List[Tick]                      │                        │
│  │  - cache in SQLite for fast query    │                        │
│  └──────────────┬───────────────────────┘                        │
│                 │                                                 │
│                 ▼                                                 │
│  ┌──────────────────────────────────────┐                        │
│  │  MultiDateSweep (extends prompt_sweep)│  ← NEW                 │
│  │  - walk_forward_windows(dates,       │                        │
│  │    train_days, val_days)             │                        │
│  │  - score_across_dates(variant,       │                        │
│  │    date_range) → aggregate score     │                        │
│  │  - apply_transaction_costs(returns)  │                        │
│  └──────────────┬───────────────────────┘                        │
│                 │                                                 │
│                 ▼                                                 │
│  ┌──────────────────────────────────────┐                        │
│  │  Winner selection + promotion        │                        │
│  │  - persistence check (win N/5        │                        │
│  │    windows)                          │                        │
│  │  - create sweep branch via git        │                        │
│  │  - write results to experiments table│                        │
│  └──────────────────────────────────────┘                        │
└──────────────────────────────────────────────────────────────────┘
```

---

## Components

### 1. Unified Bar Store (`shared/cache/bars/`)

**Current state**: `prepopulate_data.py` writes 5-min OHLCV bars as Parquet files (`shared/cache/bars/<ticker>.parquet`). The prompt sweep doesn't read them.

**Target**: Single source of truth for historical OHLCV. All backtests read from here.

**Schema per Parquet file**:
```
timestamp   datetime64[ns, UTC]
open        float64
high        float64
low         float64
close       float64
volume      int64
rsi_14      float64      # optional, computed by prepopulate
macd        float64      # optional
macd_signal float64      # optional
macd_hist   float64      # optional
atr_14      float64      # optional
```

**Backfill strategy**: The current data is lopsided (61K rows on 2026-07-01 alone, but 2 rows on other days). We need:

| Priority | Data | Source | Range | Effort |
|----------|------|--------|-------|--------|
| P0 | Core 8 tickers (SPY, AAPL, MSFT, NVDA, TSLA, META, GOOGL, AMZN) | Alpaca historical bars | Last 30 trading days | 1 hr |
| P1 | Watchlist tickers (~30 total) | Alpaca historical bars | Last 20 trading days | 2 hr |
| P2 | Full universe (200+ tickers) | Alpaca + Yahoo Finance fallback | Last 10 trading days | 1 day |

**Free tier limits**: Alpaca free tier allows 200 requests/min for historical bars. A single backfill run for 30 tickers × 20 days is 600 requests — ~3 minutes of rate-limited fetching. Use `time.sleep(0.35)` between calls.

### 2. BarLoader (`src/bar_loader.py`) — NEW

Unified interface for loading historical bars from Parquet into the Tick format the replay harness expects.

```python
class BarLoader:
    """Load OHLCV bars from Parquet store, output Ticks for replay."""

    def __init__(self, bars_dir: Path = BARS_DIR, db_path: Path = DB_PATH):
        ...

    def load_date_range(
        self,
        tickers: List[str],
        start_date: str,
        end_date: str,
        interval_minutes: int = 30,
    ) -> List[Tick]:
        """Load ticks for a date range."""

    def available_dates(self, ticker: str) -> List[str]:
        """Which dates have bars for this ticker?"""

    def missing_dates(
        self,
        tickers: List[str],
        start_date: str,
        end_date: str,
    ) -> List[Tuple[str, str]]:
        """Return (ticker, date) pairs that need backfilling."""

    def to_sqlite_cache(
        self,
        tickers: List[str],
        start_date: str,
        end_date: str,
    ) -> int:
        """Pre-load bars into SQLite for faster repeated queries.

        The prompt sweep loops over N variants × M dates — loading
        Parquet on every iteration is wasteful. Cache once, query many.
        """
```

**Design decision**: Parquet for storage (compact, typed, fast for bulk reads), SQLite for hot cache (fast for filtered queries during replay). The `to_sqlite_cache()` method bridges them.

### 3. Backfill Script (`scripts/backfill_bars.py`) — NEW

**Purpose**: Fill gaps in the bar store. Runs before the sweep.

```bash
# Backfill last 20 trading days for core tickers
python3 scripts/backfill_bars.py --tickers core --days 20

# Backfill specific tickers
python3 scripts/backfill_bars.py --tickers AAPL,MSFT,NVDA --days 30

# Check what's missing (dry run)
python3 scripts/backfill_bars.py --tickers core --days 20 --check
```

**Logic**:
1. `BarLoader.missing_dates()` → get gaps
2. For each gap: `Alpaca API → get_bars(ticker, date, timeframe="5Min")`
3. Compute RSI, MACD, ATR via `pandas_ta`
4. Append to existing Parquet file (don't overwrite)
5. Run `BarLoader.to_sqlite_cache()` to refresh hot cache

### 4. Multi-Date Sweep (`src/prompt_sweep.py` — EXTEND)

Extend the existing single-date sweep to multi-date with walk-forward validation.

**New CLI**:
```bash
# Multi-date sweep with walk-forward
python3 src/prompt_sweep.py --dates 10 --train 7 --val 3

# Same, single trader
python3 src/prompt_sweep.py --trader kairos --dates 20 --train 15 --val 5

# With transaction costs
python3 src/prompt_sweep.py --dates 10 --slippage 0.001 --commission 0.0001
```

**Walk-forward algorithm**:
```
dates = [D-19, D-18, ..., D-1, D]  # last 20 trading days
windows = []
for i in range(0, len(dates) - train_days - val_days + 1):
    train = dates[i : i + train_days]
    val   = dates[i + train_days : i + train_days + val_days]
    windows.append((train, val))

for each variant:
    for each (train, val) in windows:
        train_score = score_variant(variant, train)   # fit
        val_score   = score_variant(variant, val)      # validate
        record(train_score, val_score)

    # Aggregate: a good variant wins on most validation windows
    variant.win_rate = fraction of windows where val_score > baseline_val_score
    variant.avg_val_score = mean(val_scores)
    variant.val_stability = std(val_scores)  # lower = more consistent
```

**Winner criteria** (must pass ALL):
1. `variant.win_rate >= 0.6` — beats baseline on >60% of validation windows
2. `variant.avg_val_score > baseline_val_score + 0.05` — meaningful improvement
3. `variant.val_stability < 2 × baseline_val_stability` — not wildly inconsistent

### 5. Transaction Cost Model (`src/transaction_costs.py`) — NEW

**Purpose**: Apply realistic costs to replay results. Without this, high-frequency variants always win.

```python
@dataclass
class CostModel:
    slippage_bps: float = 10.0     # 0.1% per trade
    commission_per_share: float = 0.0  # Alpaca free, but model for real
    spread_bps: float = 5.0        # 0.05% average spread
    min_trade_cost: float = 1.0    # minimum $1 cost per trade

    def apply(self, trade: Trade) -> Trade:
        """Apply costs to a completed trade."""
        notional = trade.entry_price * trade.shares + trade.exit_price * trade.shares
        slippage = notional * self.slippage_bps / 10000
        spread = notional * self.spread_bps / 10000
        commission = self.commission_per_share * trade.shares * 2
        total_cost = max(slippage + spread + commission, self.min_trade_cost)
        trade.pnl_net = trade.pnl_gross - total_cost
        return trade
```

**Integration**: Insert between `ReplayHarness.run()` and `objective_score()`. The `ReplayResult` gets a `trades_net` list alongside `trades`.

### 6. SignalEngine ↔ LLM Bridge (`src/sweep_validation.py`) — NEW

**The problem**: SignalEngine is deterministic. LLM traders are stochastic. A variant that improves SignalEngine parameters may not improve LLM behavior.

**The fix**: Two-phase validation.

**Phase 1 — Signal sweep (cheap, fast)**:
- Test all N variants with SignalEngine across all dates
- Filter to top K variants (K=3, the cheap filter)
- Cost: ~10 seconds per variant × N dates = minutes

**Phase 2 — LLM validation (expensive, accurate)**:
- Run only the top K variants through the actual LLM replay harness
- Each LLM replay burns tokens, so only validate the best candidates
- 3 variants × 3 validation windows × 1 trader = 9 LLM runs
- Cost: ~$0.50-1.50 per night across all traders

**Gate**: If Phase 1 winner doesn't beat baseline in Phase 2 → no promotion. Log the divergence as a `signal_llm_divergence` event for analysis.

### 7. Results Database (`experiments` table)

Extend the `experiments` table from the fusion review (Gap 2) to track sweep results:

```sql
CREATE TABLE IF NOT EXISTS sweep_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,              -- ISO timestamp
    trader TEXT NOT NULL,
    variant_name TEXT NOT NULL,
    variant_description TEXT,
    train_date_range TEXT,             -- '2026-06-20:2026-06-26'
    val_date_range TEXT,               -- '2026-06-27:2026-06-30'
    baseline_score REAL,
    variant_score REAL,                -- SignalEngine score
    variant_llm_score REAL,            -- LLM replay score (Phase 2)
    calmar REAL,
    profit_factor REAL,
    win_rate REAL,
    n_trades INTEGER,
    cost_adjusted_pnl REAL,
    promoted BOOLEAN DEFAULT FALSE,
    branch_name TEXT,
    signal_params_json TEXT,           -- JSON of SignalParams
    notes TEXT
);
CREATE INDEX idx_sweep_trader_date ON sweep_results(trader, run_at);
```

---

## Data Pipeline Integration

### Nightly Cron (replaces existing prompt sweep cron)

```
# 22:00 ET — Nightly optimization pipeline
0 22 * * 1-5 cd ~/projects/paper-trading-teams && python3 scripts/nightly_pipeline.py --dates 20 --train 15 --val 5

# 06:00 ET — Pre-market data refresh (existing, keep)
30 6 * * 1-5 cd ~/projects/paper-trading-teams && python3 scripts/prepopulate_data.py --days 5
```

### `nightly_pipeline.py` (orchestrator) — NEW

Replaces ad-hoc cron combining. Single entry point:

```python
def nightly_pipeline(dates, train_days, val_days, dry_run=False):
    # 1. Backfill any missing bars
    backfill_bars(tickers="core", days=dates)

    # 2. Hot-cache bars into SQLite
    BarLoader().to_sqlite_cache(tickers="core", days=dates)

    # 3. Phase 1: Signal sweep (cheap, filter K variants)
    candidates = signal_sweep(dates, train_days, val_days)

    # 4. Phase 2: LLM validation (expensive, top K only)
    winner = llm_validate(candidates, val_dates)

    # 5. Promote winner (git branch + experiments table)
    if winner and not dry_run:
        promote_winner(winner)

    # 6. Write summary to canvas
    post_summary_card(winner)
```

---

## Design Tasks (from Honest Systems Review 2026-07-03)

These are the fusion review gaps, now actionable. Tasks marked with `[BUILD]` go straight to the coder. `[DESIGN]` needs spec/plan first.

### P0: Must Fix Before Relying on Traders

| # | Task | Type | Effort | Depends on |
|---|------|------|--------|-----------|
| DP-1 | Backfill bars script (`backfill_bars.py`) | BUILD | 2 hr | None |
| DP-2 | BarLoader module (`bar_loader.py`) | BUILD | 1 hr | DP-1 |
| DP-3 | Extend prompt_sweep to multi-date + walk-forward | BUILD | 3 hr | DP-2 |
| DP-4 | Transaction cost model (`transaction_costs.py`) | BUILD | 1 hr | DP-3 |
| DP-5 | Signal→LLM two-phase validation | BUILD | 2 hr | DP-3 |

### P1: Fusion Review Gaps

| # | Task | Type | Effort | Depends on |
|---|------|------|--------|-----------|
| FR-1 | Structured [LEARNING] journal entries (JSON) | BUILD | 1 hr | None |
| FR-2 | `params_history` table + logging in param_optimizer | BUILD | 2 hr | None |
| FR-3 | Wire LearningLoop as daily cron with apply=True | BUILD | 1 hr | FR-1, FR-2 |
| FR-4 | `daily_performance` metrics table (Sharpe, drawdown, etc.) | BUILD | 3 hr | None |
| FR-5 | Experiment runner with replay validation | BUILD | 1 day | DP-3, FR-3 |
| FR-6 | Trader prompt tiering (SOUL/STRATEGY/CONTEXT split) | BUILD | 2 hr | None |
| FR-7 | Fix sync_exits DB drift (workspace → shared only) | BUILD | 30 min | None |
| FR-8 | Proxmox D-state heartbeat alert | BUILD | 1 hr | None |

### P2: Architecture (future)

| # | Task | Type | Effort | 
|---|------|------|--------|
| FR-9 | MCP tools for data bus endpoints | BUILD | 1 day |
| FR-10 | Orchestrator agent (persistent session) | BUILD | 2 days |
| FR-11 | Webhook → turn injection reactivity | BUILD | 2 days |
| FR-12 | Custom OpenClaw trading plugin (TypeScript) | BUILD | 3 days |

---

## Verification

- [ ] `backfill_bars.py` successfully fetches 20 days of 5-min bars for 8 core tickers
- [ ] `BarLoader.load_date_range()` returns ticks for all requested dates with no gaps
- [ ] Multi-date sweep produces different rankings than single-date sweep (confirms single-date was misleading)
- [ ] Walk-forward winner selection correctly identifies persistent performers (same variant wins multiple windows)
- [ ] Transaction costs reduce PF of high-frequency variants by >20%
- [ ] Phase 2 LLM validation can veto a Phase 1 winner
- [ ] `sweep_results` table populated and queryable
- [ ] Nightly pipeline runs end-to-end in <30 minutes (target: <15 min for signal phase, <10 min for LLM phase)

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Alpaca rate limiting on backfill | Use `time.sleep(0.35)` between calls, 200/min rate limit, fallback to Yahoo Finance for non-critical tickers |
| LLM validation too expensive | Phase 1 filter reduces LLM runs to K=3 per trader. Nightly budget: ~$1.50 total across all 3 traders. If budget >$3, reduce K or skip LLM phase. |
| Walk-forward windows too small (high variance) | Minimum 3 validation windows. If <20 trading days available, reduce train days, not val windows. |
| Parquet files corrupt from append errors | Always write to temp file, atomic rename. Verify after write. |
| SignalEngine parameters don't map to prompt text | Track `signal_llm_divergence` events. If divergence >50%, deprecate signal phase and go LLM-only. |

---

## Open Questions

1. **Free-tier Alpaca bar limit**: How far back can the free tier go? (Likely 1-2 years of daily, but 5-min may be limited to last 30 days.) → Test before building backfill.
2. **Should we use Yahoo Finance as the primary source?** It's free, unlimited, and has 5-min bars going back years. Alpaca is faster but rate-limited. → Yahoo for backfill, Alpaca for live.
3. **Weighted or equal validation windows?** Recent windows (closer to today) may be more predictive. Weight by recency or keep equal? → Start with equal, measure if recency-weighted works better after 2 weeks of data.
