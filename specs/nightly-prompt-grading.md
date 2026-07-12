# Nightly Replay → Prompt Variant Grading Pipeline

**Issue**: [#93](https://github.com/Tesselation-Studios/paper-trading-rebuild/issues/93)
**Parent**: [SPEC.md](../SPEC.md), [nightly-optimization-pipeline.md](nightly-optimization-pipeline.md)
**State**: Design (pending P0 dependencies: #84, #91, #90)
**Updated**: 2026-07-12

---

## 1. Purpose

After the historical trading simulator is operational, grade prompt variants systematically every night. For each live trader (Kairos, Aldridge, Stonks), run N prompt variants through historical replay, score each on a composite metric, and produce a ranked leaderboard. The top variant may be promoted to the live trader's prompt.

This is **the grading engine** — the subsystem that answers "which prompt variant performed best?" The broader promotion lifecycle (tiering, versioning, weekly culling) lives in sibling specs.

---

## 2. Architecture

### 2.1 Context Diagram

```
                         ┌──────────────────────┐
                         │   Data Bus (:5000)    │
                         │ (Alpaca → OHLCV bars) │
                         └──────────┬───────────┘
                                    │
                         ┌──────────▼───────────┐
                         │  BarLoader / SQLite   │
                         │  Hot Cache            │
                         └──────────┬───────────┘
                                    │ ticks
                                    │
┌────────────────────────────────────▼───────────────────────────────────────┐
│                  NIGHTLY GRADING PIPELINE (22:00 ET cron)                   │
│                                                                             │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐ │
│  │  Variant Gen  │──▶│  Replay Loop │──▶│  Scorer      │──▶│  Leaderboard │ │
│  │  (expand N    │   │  (M dates ×  │   │  (composite  │   │  (ranked     │ │
│  │   variants)   │   │   N variants)│   │   metric)    │   │   output)    │ │
│  └──────┬───────┘   └──────┬──────┘   └──────┬──────┘   └──────┬───────┘ │
│         │                  │                 │                 │          │
│         ▼                  ▼                 ▼                 ▼          │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐ │
│  │prompts/ dir  │   │ReplayHarness │   │objective_fn  │   │Canvas card   │ │
│  │PromptRegistry│   │CostModel     │   │metrics.py    │   │trading.db    │ │
│  └──────────────┘   └──────────────┘   └──────────────┘   └──────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
         ┌──────────────────────────┴───────────────────────────┐
         │                      Outputs                         │
         ▼                                                      ▼
   ┌─────────────┐                                    ┌─────────────┐
   │  Canvas     │                                    │  Postgres   │
   │  Leaderboard│                                    │  sweep_     │
   │  Card       │                                    │  results    │
   └─────────────┘                                    └─────────────┘
```

### 2.2 Data Flow (Step by Step)

| Step | Component | Input | Output | Cost |
|------|-----------|-------|--------|------|
| 0 | `step_backfill()` | Missing date-ticker pairs | Populated Parquet bars | ~3 min API rate-limited |
| 1 | `BarLoader.to_sqlite_cache()` | Parquet files | SQLite hot cache (`replay_ticks` table) | ~10 sec |
| 2 | `VariantGenerator()` | Base prompt from `prompts/{trader}.txt` | N `PromptVariant` objects | ~0 sec (in-memory) |
| 3 | `SignalEngine.sweep()` | N variants × M dates × ticker ticks | Scored `SweepResult` per variant | ~30 sec per trader |
| 4 | `LLMReplayHarness.run()` | Top-K variants from step 3 × val dates | Trade journal, PnL per variant | ~$0.50/trader |
| 5 | `GradingScorer.score()` | Raw trade data from step 4 | Composite grade + per-metric breakdown | ~1 sec |
| 6 | `LeaderboardBuilder.build()` | All variant scores | Ranked leaderboard (JSON + markdown) | ~0 sec |
| 7 | `step_canvas_card()` | Leaderboard markdown | Canvas card on `trading` board | ~0.5 sec |

**Total wall-clock target**: ~15 min per trader (signal phase ~2 min, LLM phase ~13 min depending on model latency).

### 2.3 Existing Assets (what's already built)

| Asset | File | Status for #93 |
|-------|------|----------------|
| Nightly pipeline orchestrator | `scripts/nightly_pipeline.py` | 🟢 Core — drives steps 0-4, 7 |
| Replay harness | `src/replay.py` | 🟢 Core — engine for step 4 |
| Prompt sweep / variant gen | `src/prompt_sweep.py` | 🟢 Core — generates N variants, SignalEngine sweep |
| Two-phase validation | `src/sweep_validation.py` | 🟢 Core — links signal → LLM |
| Cost model | `src/transaction_costs.py` | 🟢 Core — realistic PnL |
| Prompt tiering registry | `src/prompt_tiering.py` | 🟢 Core — PROD/CANDIDATE/RETIRED lifecycle |
| Prompt versioning | `src/prompt_versioning.py` | 🟢 Core — git branches, tags |
| Canvas dashboard | `src/canvas_dashboard.py` | 🟢 Core — pushes results to Canvas |
| Scorer / metrics | `src/metrics.py`, `specs/objective-function.md` | 🟢 Core — objective score, Calmar, etc. |
| Base prompt files | `prompts/{trader}.txt` | 🟢 Core — source of truth |
| Prompt builder | `src/prompt_builder.py` | 🟢 Core — assembles final prompt from parts |
| Virtual trader runner | `src/virtual_runner.py` | 🟡 Integration needed — grading feeds into virtual rotation |

---

## 3. Prompt Variant Management

### 3.1 Variant Generation

Variants are generated by perturbing the base prompt from `prompts/{trader}.txt` using the existing perturbation templates in `src/prompt_sweep.py`.

**Current perturbation dimensions** (from `prompt_sweep.py` `PERTURBATION_TEMPLATES`):

| Variant Name | What It Changes | Signal Params Affected |
|---|---|---|
| `wider_stops` | Stop-loss from 3% → 5% | `stop_loss_pct` |
| `tighter_stops` | Stop-loss from 3% → 2% | `stop_loss_pct` |
| `momentum_focus` | ↑ momentum weight, ↓ mean-reversion weight | `momentum_threshold`, weight multipliers |
| `aggressive_sizing` | Position size 2% → 4% | `base_size_pct` |
| `conservative_sizing` | Position size 2% → 1% | `base_size_pct` |
| `long_biased` | ↑ conviction on BUY signals | `conviction_multiplier` |
| `short_biased` | Enable shorts more aggressively | `max_short_pct`, conviction override |

**Future dimensions** (not yet in templates, tracked for Phase 2):
- `timeframe_shift` — Holding horizon multiplier (1× → 3×, 0.5×)
- `regime_override` — Specific regime rules (e.g., "only trade in TRENDING")
- `sector_skew` — Overweight/underweight specific sectors
- `risk_profile` — Conservative vs aggressive entry criteria phrasing

### 3.2 Variant Lifecycle

Prompts flow through the tiering registry (`src/prompt_tiering.py`):

```
prompts/{trader}.txt  ── (base, unchanged)
       │
       ▼
PERTURBATION_TEMPLATES ── generate N CANDIDATE variants
       │
       ▼
Grading Pipeline ── score each variant
       │
       ▼
               ┌── Top 1 variant → PROD (after gate checks)
Leaderboard ───┼── Top 2-3 → retain as CANDIDATE for next nightly
               └── Bottom N → culled (never registered)
```

**Gate checks before PROD promotion** (existing from `prompt_tiering.py`):
1. Walk-forward validation — Val Sharpe > 0, > baseline, > train × 0.7
2. Two-phase agreement — Signal engine winner ≈ LLM replay winner
3. Minimum evaluation — At least 5 trading days of data (or earliest available)
4. No divergence — Top signal variant within 10% of top LLM variant

### 3.3 Storage Schema

Variants are stored in three places:

1. **`prompts/{trader}.txt`** — The **PROD** prompt file. Only one per trader lives here. Updated on promotion.

2. **`src/prompt_tiering.py` → `PromptRegistry`** — Full lifecycle registry (PROD/CANDIDATE/RETIRED). Persisted to JSON at `config/prompt_registry.json`.

   ```json
   {
     "trader-kairos": {
       "prod": {
         "id": "uuid-abc-123",
         "version": "v1.2.0",
         "created_at": "2026-07-10T22:00:00Z",
         "source_branch": "sweep/2026-07-10/kairos/variant-003",
         "grade_snapshot": { "composite": 0.82, "calmar": 1.4, "pf": 1.6 }
       },
       "candidates": [
         {
           "id": "uuid-def-456",
           "variant_name": "momentum_focus",
           "created_at": "2026-07-11T22:00:00Z",
           "grade": { "composite": 0.91, "calmar": 1.8, "pf": 2.1 }
         }
       ],
       "retired": [ ... ]
     }
   }
   ```

3. **`trading.sweep_results` (Postgres)** — Full per-run data for analytics.

   ```sql
   CREATE TABLE IF NOT EXISTS trading.sweep_results (
       id          SERIAL PRIMARY KEY,
       trader      VARCHAR(32) NOT NULL,
       run_date    DATE NOT NULL,
       variant_name VARCHAR(64) NOT NULL,
       variant_id  INTEGER NOT NULL,         -- from PromptVariant
       tier        VARCHAR(16) DEFAULT 'candidate',
       composite   REAL,
       pnl_total   REAL,
       win_rate    REAL,
       calmar      REAL,
       sortino     REAL,
       profit_factor REAL,
       expectancy  REAL,
       max_drawdown REAL,
       avg_hold_bars INTEGER,
       n_trades    INTEGER,
       n_wins      INTEGER,
       n_losses    INTEGER,
       run_duration_sec REAL,
       cost_adjusted BOOLEAN DEFAULT TRUE,
       model       VARCHAR(128),              -- e.g. "google/gemini-3.5-flash"
       signal_params_json JSONB,
       base_prompt_hash VARCHAR(64),
       runner_ip   VARCHAR(16),              -- which Docker node ran it
       created_at  TIMESTAMPTZ DEFAULT NOW(),

       UNIQUE(trader, run_date, variant_name)
   );
   CREATE INDEX idx_sweep_trader_date ON trading.sweep_results(trader, run_date);
   CREATE INDEX idx_sweep_composite ON trading.sweep_results(composite DESC);
   ```

   Migration: `migrations/004_sweep_results.sql`

---

## 4. Grading Formula

### 4.1 Composite Score

The composite score is the **weighted sum of rank-normalized sub-metrics**, not raw values. This prevents a single metric with wide dynamic range (e.g., PnL) from dominating.

```
composite(variant) = Σ( w_i × rank_normalize(metric_i, baseline) )
```

| Metric | Weight | Why |
|--------|--------|-----|
| **PnL (net)** | 0.25 | Ground truth — realized profit after costs |
| **Calmar ratio** | 0.20 | Risk-adjusted return — annualized return / max DD |
| **Profit factor** | 0.20 | Edge — gross profit / gross loss (\(\geq 1.5\) = real edge) |
| **Win rate** | 0.10 | Consistency — fraction of winning trades |
| **Sortino ratio** | 0.10 | Downside-only volatility penalty |
| **Expectancy** | 0.10 | $ per trade — (avg_win × win_rate) - (avg_loss × loss_rate) |
| **Max drawdown** | 0.05 | Penalty — applied as (1 - max_drawdown / equity) capped at [0,1] |

**Baseline** is the current PROD prompt's score over the **same replay window**. This means variants are always graded relative to the current best.

### 4.2 Rank Normalization

For each metric `m`, every variant's raw value is transformed to a [0, 1] score against the baseline:

```
rank_normalize(variant_m) = 0.5 + 0.5 × tanh( (variant_m - baseline_m) / σ_m )
```

Where `σ_m` is the standard deviation of metric `m` across all variants. This:
- Centers at 0.5 (equivalent to baseline)
- Ranges [0, 1) for outperforming, (0, 0.5] for underperforming
- Uses tanh to compress outliers (prevents a single lucky run from dominating)
- Penalizes underperformers symmetrically

### 4.3 Knockout Conditions

Any variant that triggers ANY of these gets `composite = 0` regardless of other metrics:

| Condition | Threshold | Rationale |
|-----------|-----------|-----------|
| Max drawdown | > 25% of starting equity | Portfolio destruction — no recovery path |
| Win rate | < 20% and n_trades > 10 | Not learning — statistically likely to be noise |
| Profit factor | < 0.5 and n_trades > 10 | Negative edge — systematically losing money |
| n_trades | 0 | Didn't trade at all — no signal generated |
| Cost-adjusted PnL | < -30% of starting equity | Bleeding out — costs alone destroy the account |

### 4.4 Walk-Forward Window Weights

When grading across M dates split into walk-forward windows:

```
final_composite = 0.7 × mean(composite_val_windows) + 0.3 × mean(composite_train_windows)
```

This weights **validation** performance (unseen data) 2.3× more than training performance, preventing overfitting.

### 4.5 SPY Benchmark Overlay

Every run also grades SPY buy-and-hold over the identical replay window:

```
variant_beats_SPY = composite(variant) > composite(SPY_BH)
```

If no variant beats SPY, the pipeline logs a warning but still promotes the best variant. Persistence checks (same variant wins 2/3 consecutive nights) override this.

---

## 5. Leaderboard Output

### 5.1 Canvas Card Format

After each nightly run, a leaderboard card is pushed to the `trading` board on Canvas. Format:

```
## 📊 Nightly Prompt Leaderboard — 2026-07-11

### 🏆 Kairos — 5 variants, 3 val windows
| Rank | Variant       | Composite | PnL    | Calmar | PF   | WR   | DD   |
|------|---------------|-----------|--------|--------|------|------|------|
| 🥇   | momentum_focus| **0.91**  | +$342  | 1.8    | 2.1  | 62%  | 4.2% |
| 🥈   | wider_stops   | 0.78      | +$187  | 1.4    | 1.7  | 55%  | 3.1% |
| 🥉   | (baseline)    | 0.50      | +$95   | 1.2    | 1.5  | 48%  | 5.0% |
| 4    | aggro_sizing  | 0.34      | -$212  | 0.6    | 0.9  | 38%  | 12%  |
| 5    | short_biased  | 0.12      | -$478  | 0.2    | 0.4  | 29%  | 22%  |

**Winner**: momentum_focus ✅ promoted to CANDIDATE tier
**Beats SPY**: Yes (SPY PF=1.2, Calmar=1.0)

### Aldridge — 4 variants, 2 val windows (limited data)
...

⏱ Runtime: 13m 42s | Data window: 2026-06-20 → 2026-07-10
```

### 5.2 Leaderboard JSON (for Dashboard API)

The same data is written to `reports/nightly-{YYYY-MM-DD}/leaderboard.json`:

```json
{
  "run_date": "2026-07-11",
  "run_at": "2026-07-11T22:15:00Z",
  "traders": [
    {
      "trader": "kairos",
      "n_variants": 5,
      "n_dates": 15,
      "n_windows": 3,
      "baseline_composite": 0.50,
      "winner": "momentum_focus",
      "promoted": true,
      "beats_spy": true,
      "variants": [
        {
          "variant_name": "momentum_focus",
          "rank": 1,
          "composite": 0.91,
          "pnl_total": 342.17,
          "win_rate": 0.62,
          "calmar": 1.80,
          "sortino": 1.25,
          "profit_factor": 2.10,
          "expectancy": 17.50,
          "max_drawdown_pct": 4.2,
          "n_trades": 24,
          "n_wins": 15,
          "avg_hold_bars": 3.2,
          "promotion_gates_passed": true,
          "knockout": false
        }
      ]
    }
  ],
  "pipeline": {
    "duration_seconds": 822,
    "errors": [],
    "warnings": ["aldridge: only 8 trading days available — results may be noisy"],
    "skip_phase2": false
  }
}
```

### 5.3 Historical Trends

A secondary dashboard card is pushed weekly (Sunday) showing **7-night trend**:
- Which variants consistently appear in top-3
- Composite score trendline (is the system improving or plateauing?)
- Prompt version history (v1.2.0 → v1.2.1 → v1.3.0)

---

## 6. Integration with Virtual Trader Infrastructure

### 6.1 What Already Exists

| Component | File | Role |
|-----------|------|------|
| Virtual trader runner | `src/virtual_runner.py` | Runs shadow traders every 5 min during market hours |
| Virtual trader schema | `trading.virtual_traders` table | `variant_type = 'params'` or `'prompt'`, with `variant JSONB` |
| Weekly culling | `src/virtual_cull.py` | Scores virtuals, promotes top-3, culls bottom-3 |
| Virtual rotation | `src/virtual_rotate.py` | Creates new variants from param space |

### 6.2 Integration Points

The grading pipeline feeds into the virtual trader system at two points:

**Point A — Candidate Variants → Virtual Traders** (nightly)
After the nightly grading run, the top-3 variants (by composite score) are registered as new virtual traders:

```
Nightly Grading Pipeline
       │
       ▼
Top-3 variants per trader
       │
       ▼
INSERT INTO trading.virtual_traders
  (name, base_trader, variant_type, variant)
VALUES
  ('kairos-momentum-20260711', 'kairos', 'prompt', '{"prompt_text": "...", "grade": 0.91}')
```

These virtual traders then shadow the live trader for the next 24h, collecting real market data. Their performance feeds into the **next** nightly grading run.

**Point B — Virtual Results → Grading Inputs** (nightly)
Before the nightly grading run, check if any virtual trader has accumulated enough real trades (>10 trades) to include in the grading:

```
Virtual Runner (every 5 min)
       │
       ▼
Virtual traders accumulate trades
       │
       ▼
Nightly grading: check virtual_traders with n_trades > 10
       │
       ▼
Include as candidate variants in the sweep
```

### 6.3 Lifecycle Summary

```
Night N:
  ┌─ Nightly grading: score N variants on historical replay
  │   → Top-3 registered as virtual traders
  │   → Virtual traders shadow live trader next day
  │   → Virtual traders accumulate real trades
  │
Night N+1:
  ├─ Nightly grading: score NEW variants + existing virtuals with >10 trades
  │   → Merge historical replay score + real trade score (weighted)
  │   → Top-3 registered as NEW virtual traders
  │   → Old virtuals either promoted (if persistently top-3) or culled
  │
Sunday (weekly):
  └─ virtual_cull.py: promote virtuals that outperformed live for 2+ weeks
```

---

## 7. Cron Integration

### 7.1 Cron Schedule

The nightly pipeline already has a cron entry in `scripts/nightly_pipeline.py`. Issue #93 simply formalizes the grading-specific aspects:

```
# ── Grading pipeline (every weekday at 22:00 ET after market close) ──
0 22 * * 1-5 cd ~/projects/paper-trading-rebuild && \
  python3 scripts/nightly_pipeline.py \
    --dates 20 \
    --train 15 \
    --val 5 \
    --variants 5 \
    --phase2 \
    >> logs/nightly_pipeline.log 2>&1

# ── Weekend summary (Sunday 22:00 ET — trend analysis + weekly promotion) ──
0 22 * * 0 cd ~/projects/paper-trading-rebuild && \
  python3 scripts/nightly_pipeline.py \
    --dates 20 \
    --train 15 \
    --val 5 \
    --variants 5 \
    --phase2 \
    --weekly-summary \
    >> logs/nightly_pipeline.log 2>&1
```

### 7.2 Trigger Contract

The cron message respects invariant #11 ("Cron is trigger, not instruction"):

```
22:00 ET → Hermes sends: "/hooks/agent" with body:
  {"action": "run_pipeline", "phase": "grading", "trader": null}

22:05 ET → Grading pipeline starts (Docker worker on .179)
22:20-22:30 ET → Leaderboard pushed to Canvas
```

### 7.3 Timeout Budget

| Phase | Max Duration | Notes |
|-------|-------------|-------|
| Data prep (backfill + cache) | 5 min | Typically 1-2 min |
| Phase 1 signal sweep | 5 min | 5 variants × 20 dates × 3 traders |
| Phase 2 LLM validation | 20 min | Top-3 variants × 3 val windows × 3 traders |
| Canvas push + DB write | 1 min | Typically < 1 sec |
| **Total** | **~31 min** | Well within 60 min cron window |

Cron timeout (invariant #13) must be ≥ 3 × 31 min = **93 min**. Set `TimeoutSec=5400` in systemd unit or 5400s in cron wrapper.

---

## 8. Grading Reports

### 8.1 Report Directory Structure

```
reports/
  nightly-2026-07-11/
    leaderboard.json         ← Machine-readable ranked results
    leaderboard.md           ← Markdown (same as Canvas card)
    variants/
      kairos-momentum-focus/
        trades.csv           ← All trades from replay
        equity_curve.csv     ← Equity over time
        decision_log.json    ← Every LLM decision with signal context
        scorecard.json       ← Full metric breakdown
      kairos-wider-stops/
        ...
    diagnostic/
      signal_llm_divergence.md  ← If Phase 2 vetoed Phase 1 winner
      missing_dates.csv         ← Dates with insufficient data
      benchmark_spy.csv         ← SPY buy-and-hold comparison
```

### 8.2 Scorecard Format (per variant)

```json
{
  "variant_name": "momentum_focus",
  "trader": "kairos",
  "run_date": "2026-07-11",
  "composite": 0.91,
  "metrics": {
    "pnl": {
      "gross": 412.50,
      "costs": 70.33,
      "net": 342.17
    },
    "win_rate": {
      "value": 0.62,
      "n_wins": 15,
      "n_losses": 9
    },
    "calmar": 1.80,
    "sortino": 1.25,
    "profit_factor": 2.10,
    "expectancy": 17.50,
    "max_drawdown_pct": 4.2,
    "avg_hold_bars": 3.2,
    "n_trades": 24
  },
  "knockout": false,
  "walk_forward": {
    "n_windows": 3,
    "train_avg_composite": 0.87,
    "val_avg_composite": 0.93,
    "win_rate_win": 1.0,
    "stability": 0.04
  },
  "baseline_delta": {
    "composite": 0.41,
    "pnl_net": 247.17
  },
  "beats_spy": true,
  "gates": {
    "walk_forward": "pass",
    "two_phase": "pass",
    "min_eval_days": "pass",
    "no_divergence": "pass"
  }
}
```

---

## 9. Error Handling & Edge Cases

### 9.1 Data-Related

| Scenario | Detection | Handling |
|----------|-----------|----------|
| **No historical data available** (new ticker universe) | `BarLoader.available_dates()` returns < 3 trading days | Skip grading for this trader. Log warning. Fall back to signal-only sweep (no LLM phase). |
| **Partial data — some dates missing** | `missing_dates()` returns entries | Run on available dates only. Tag run as `partial_data` in sweep_results. |
| **All variants score 0** (knockout) | Every variant triggers a knockout condition | Log `ALL_VARIANTS_KNOCKED_OUT`. Do NOT promote. Escalate to Hermes via Canvas alert card. |
| **Baseline itself is knocked out** (live trader in >15% DD) | PROD prompt's score is 0 | Grade variants on absolute metrics (not relative to baseline). Winner still gets promoted if composite > 0.3. |

### 9.2 Execution-Related

| Scenario | Detection | Handling |
|----------|-----------|----------|
| **LLM API timeout** | HTTP request exceeds 60s timeout | Retry once with exponential backoff (2s wait). If still fails, skip LLM phase for that variant. Log as `llm_timeout`. |
| **LLM returns malformed JSON** | JSON parse error | Retry once with prompt: "Respond ONLY with valid JSON." If still malformed, score variant as 0 with reason `llm_parse_error`. |
| **Docker worker OOM** | Worker exits with code 137 | Restart worker with `--memory=4g` limit. If same worker OOMs twice, skip to next variant. |
| **Postgres unavailable** | Connection refused | Write results to local SQLite fallback (`reports/nightly-*/results.db`). Retry Postgres on next cron cycle. |
| **Canvas push fails** | HTTP 401/403/5xx | Log to file. Do NOT block pipeline. Next cron cycle retries with last successful data. |
| **Night overlaps with market hours** (holiday early close) | Run time is 22:00 ET — always after market close | No special handling. If market is closed all day (Christmas), skip grading. |

### 9.3 Configuration Guardrails

| Guardrail | Implementation |
|-----------|----------------|
| **Max variants per run** | Hard cap at 20 variant per trader. CLI `--variants` arg validates ≤ 20. |
| **Max LLM runs per night** | Phase 2 limit: top K=3 variants × 3 val windows × 3 traders = 27 max. Budget cap: $3.00 across all traders. |
| **Minimum data window** | Require ≥ 5 trading days for any variant grading. Otherwise skip and log. |
| **Variant name collision** | Variant names are scoped per trader per date. `variant_name` field is `{template}_{YYYYMMDD}`. |
| **Duplicate grading** | `sweep_results` has `UNIQUE(trader, run_date, variant_name)` — safe to re-run same night. |
| **Run locking** | `/tmp/nightly_pipeline.lock` prevents concurrent runs. Exit with code 75 (temporary failure) if lock held. |

### 9.4 Graceful Degradation Order

If system resources are constrained:

1. **Low resources** (CPU > 80%, memory > 80%) → Skip Phase 2 (LLM). Signal-only grading.
2. **Very low resources** (CPU > 90%, memory > 90%) → Skip all backfill. Use existing cached data only.
3. **Critical** (CPU > 95%) → Skip full pipeline. Push "skipped" card to Canvas.

---

## 10. Non-Goals (Out of Scope for #93)

| Item | Rationale | Tracked Where |
|------|-----------|---------------|
| Virtual trader culling/promotion | #93 grades variants; #92 handles weekly promotion | issue #92, `specs/virtual-trader-rotation.md` |
| Multi-trader signal sharing | Beyond the scope of prompt variant grading | ROADMAP.md "Future" |
| Genetic algorithm for variant generation | Initial N=5 templates is sufficient; GA is Phase 3 | `ROADMAP.md` P3 |
| Live A/B testing | Shadow mode is handled by virtual traders, not this pipeline | issue #26, `specs/nightly-optimization-pipeline.md` |
| Data backfill bar store reliability | #93 expects BarLoader to work; data quality is separate | issue #17, `specs/nightly-optimization-pipeline.md` DP-1 |

---

## 11. Acceptance Criteria Verification

| # | Criteria | How Verified | Evidence |
|---|----------|-------------|----------|
| AC1 | Nightly cron triggers grading pipeline | Cron entry installed and tested | `crontab -l` shows `0 22 * * 1-5 nightly_pipeline.py` |
| AC2 | Multiple prompt variants tested per run | N variants each scored on M dates × K windows | `leaderboard.json` contains ≥ N entries per trader |
| AC3 | Performance grading with defined metrics | Composite score computed from PnL, Calmar, PF, WR, Sortino, Expectancy | Scorecard JSON contains all 7 metrics |
| AC4 | Results posted to Canvas/Dashboard | Leaderboard card visible on `trading` board | Canvas card UUID logged to `sweep_results` |
| AC5 | Winner promotion path defined | Top variant passes 4 promotion gates → registered as CANDIDATE | `PromptRegistry.list_candidates()` shows promoted variant |
| AC6 | Error handling for edge cases | All scenarios in §9 handled without crash | Pipeline exits 0 even with partial failures |
| AC7 | Virtual trader integration | Top-3 variants registered as virtual traders | `trading.virtual_traders` has new rows after each run |

---

## 12. Implementation Roadmap

### Phase 1 — Minimum Viable (depends on #84, #91, #90)
- [ ] Scorecard JSON format defined (this spec)
- [ ] `GradingScorer` class in `src/grading.py` — composite score calculation
- [ ] `LeaderboardBuilder` class — leaderboard assembly and markdown rendering
- [ ] Wire Phase 1 (signal-only) grading into existing nightly pipeline
- [ ] Canvas card with leaderboard table

### Phase 2 — Full Pipeline
- [ ] Full composite with all 7 metrics and rank normalization
- [ ] Walk-forward window weighting
- [ ] Knockout conditions enforced
- [ ] Phase 2 LLM integration (already exists, just needs grading at the end)
- [ ] Virtual trader integration (register top-3 as virtuals)

### Phase 3 — Polish
- [ ] Historical trend tracking (7-night summary card)
- [ ] SPY benchmark overlay
- [ ] Diagnostic reports (equity curves, divergence analysis)
- [ ] Dashboard API reader for leaderboard data