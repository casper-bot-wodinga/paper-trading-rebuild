# Virtual Trader Promotion Mechanism

**Parent**: [SPEC.md](../SPEC.md)  
**Issue**: [#92](https://github.com/casper-bot-wodinga/paper-trading-rebuild/issues/92)  
**Status**: Design  
**Updated**: 2026-07-12  

---

## Table of Contents

1. [Motivation](#1-motivation)
2. [Tier System](#2-tier-system)
3. [Capability Matrix](#3-capability-matrix)
4. [Promotion Criteria](#4-promotion-criteria)
5. [Demotion / Relegation Rules](#5-demotion--relegation-rules)
6. [Feedback Loop](#6-feedback-loop)
7. [Orchestrator Tick Loop Integration](#7-orchestrator-tick-loop-integration)
8. [Database Schema Changes](#8-database-schema-changes)
9. [Implementation Roadmap](#9-implementation-roadmap)
10. [Rollback / Safety](#10-rollback--safety)
11. [Appendix: Tier Transitions Reference](#11-appendix-tier-transitions-reference)

---

## 1. Motivation

Virtual traders currently have only two states — **active** (paper trading) and **live** (promoted to Alpaca orders) — plus **culled** (removed). There is no intermediate progression. A newly created virtual starts at the same resource limits and data access as a veteran virtual that has been outperforming for weeks. This wastes compute and misses the opportunity to create a **career ladder** that incentivizes sustained performance.

A promotion mechanism introduces **graduated tiers** that unlock progressively better capabilities. This makes the virtual trader ecosystem more realistic, competitive, and data-efficient:

| Problem | Solution |
|---------|----------|
| All virtuals get same model | Higher tiers unlock better LLM models |
| All virtuals get same data | Higher tiers get more tickers, shorter intervals |
| All virtuals have same capital | Higher tiers get larger portfolio limits |
| No differentiation between veteran & rookie | Tiers encode earned trust |
| No feedback from promotion → P&L | Broader access drives better decisions |

---

## 2. Tier System

### 2.1 Overview

Six tiers, named after trading progression stages:

| Tier | Code | Name | Max Traders | Typical Duration |
|------|------|------|-------------|------------------|
| 0 | `probation` | Prospect | ∞ | 2 trading days |
| 1 | `rookie` | Rookie | 12 per base | ≥5 days |
| 2 | `veteran` | Veteran | 8 per base | ≥2 weeks |
| 3 | `expert` | Expert | 4 per base | ≥1 month |
| 4 | `elite` | Elite | 2 per base | ≥2 months |
| 5 | `live` | Live (Alpaca) | 1 per base | Indefinite |

**Max Traders** is a soft cap per `base_trader` (kairos, aldridge, stonks). Above that, promotion is blocked until a slot opens (via demotion or culling).

### 2.2 Tier Transitions

```
                    ┌──────────────┐
                    │   PROBATION  │  (Tier 0)
                    └──────┬───────┘
                           │ Survive 2 days without errors
                           ▼
                    ┌──────────────┐
                    │    ROOKIE    │  (Tier 1)
                    └──────┬───────┘
                           │ 7-day P&L > 0 AND > 5 closed trades
                           ▼
                    ┌──────────────┐
                    │   VETERAN    │  (Tier 2)
                    └──────┬───────┘
                           │ 14-day P&L > baseline × 1.05
                           │ AND win rate > 50%
                           ▼
                    ┌──────────────┐
                    │   EXPERT     │  (Tier 3)
                    └──────┬───────┘
                           │ 30-day Sharpe > 0.8
                           │ AND beats live trader × 1.10
                           ▼
                    ┌──────────────┐
                    │   ELITE      │  (Tier 4)
                    └──────┬───────┘
                           │ Champ belt model:
                           │ challenger.wins > main.wins
                           ▼
                    ┌──────────────┐
                    │   LIVE       │  (Tier 5)
                    └──────────────┘
```

Tiers 0–4 are **paper-only**. Tier 5 (`live`) controls Alpaca orders. This follows the existing `virtual_rotate.py` championship belt model for the final promotion step.

### 2.3 Eligibility Summary

| Promotion | Minimum Age | Minimum Trades | Performance Gate |
|-----------|-------------|----------------|------------------|
| Probation → Rookie | 2 days | 1 closed | No runtime errors |
| Rookie → Veteran | 5 days | 5 closed | 7d P&L > 0 |
| Veteran → Expert | 14 days | 10 closed | Win rate > 50%, 14d P&L > baseline × 1.05 |
| Expert → Elite | 30 days | 20 closed | 30d Sharpe > 0.8, P&L > live × 1.10 |
| Elite → Live | — | 10 closed | `challenger.wins > main.wins` (belt model) |

---

## 3. Capability Matrix

Each tier unlocks progressively more resources and capabilities.

### 3.1 Trading Capabilities

| Capability | Probation | Rookie | Veteran | Expert | Elite | Live |
|-----------|:---------:|:------:|:-------:|:------:|:-----:|:----:|
| **Max portfolio** | $2,000 | $10,000 | $25,000 | $50,000 | $100,000 | Current equity |
| **Max open positions** | 2 | 3 | 5 | 7 | 10 | Unlimited |
| **Position size (max %)** | 10% | 15% | 20% | 25% | 30% | 100% |
| **Tracked symbols** | 5 | 10 | 20 | 40 | All tracked | All tracked |
| **Tick interval** | 30 min | 15 min | 10 min | 5 min | 5 min | 5 min |
| **Stop-loss required** | Yes | Yes | Yes | Recommended | Recommended | Managed |
| **Max daily trades** | 3 | 5 | 10 | 20 | 40 | Unlimited |
| **Real orders (Alpaca)** | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |

### 3.2 Model & Data Access

| Capability | Probation | Rookie | Veteran | Expert | Elite | Live |
|-----------|:---------:|:------:|:-------:|:------:|:-----:|:----:|
| **LLM model** | flash | flash | flash | pro | pro | pro |
| **Signal engine** | Default | Default | Default | Tuned | Tuned | Tuned |
| **Fundamentals data** | ❌ | Price only | + RSI/MACD | + Volume | + Sentiment | All |
| **Alternative data** | ❌ | ❌ | ❌ | ❌ | Sentiment | All |
| **Historical replay access** | ❌ | Last 5d | Last 14d | Last 30d | Last 90d | All |
| **Journal context (decisions)** | Last 3 | Last 5 | Last 10 | Last 20 | Last 30 | Last 30 |
| **Prompt personalization** | Template | Template | Variant | Custom | Custom+ | Best available |
| **Model temperature** | 0.3 | 0.3 | 0.3 | 0.4 | 0.5 | 0.3 |

### 3.3 Autonomy & Self-Improvement

| Capability | Probation | Rookie | Veteran | Expert | Elite | Live |
|-----------|:---------:|:------:|:-------:|:------:|:-----:|:----:|
| **Parameter evolution** | ❌ | ❌ | Bounded (±10%) | Bounded (±20%) | Full-range | Veto |
| **Prompt variant generation** | ❌ | ❌ | ❌ | ✅ | ✅ | ❌ (locked) |
| **Replay-driven optimization** | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ |
| **Cull immunity** | ❌ | ❌ | ❌ | ❌ | ✅ | N/A |
| **Dashboard visibility** | Minimal | Basic | Detailed | Full | Full | Full |
| **Journal reflection** | ❌ | ❌ | ❌ | Weekly | Daily | Daily |

### 3.4 Rationale for Each Lock

- **Pro models reserved for Expert+**: Flash is fast and cheap; running 20 virtuals on pro would be prohibitively expensive. Only proven performers get the premium model.
- **Smaller tick intervals for higher tiers**: Frequent ticks consume more LLM credits. Lower tiers learn at a slower cadence, reducing cost while they prove themselves.
- **No alternative data for lower tiers**: Sentiment and alternative data add noise until a trader has a proven signal-processing baseline.
- **Parameter evolution restricted by tier**: Rookies should focus on executing a baseline strategy, not tuning parameters they don't understand yet.

---

## 4. Promotion Criteria

### 4.1 Core Scoring Metrics

All promotion decisions are based on **rolling window** metrics (not cumulative since inception). This prevents early success from masking recent decline.

| Metric | Symbol | Window | Used For |
|--------|--------|--------|----------|
| Net P&L | `pnl` | 7d / 14d / 30d | Rookie→Veteran, Veteran→Expert |
| Win rate | `wr` | Rolling 20 trades | Veteran→Expert |
| Sharpe ratio | `sr` | 30d daily returns | Expert→Elite |
| Consecutive daily wins | `streak` | Rotation log | Elite→Live (belt model) |
| Benchmark outperformance | `alpha` | 30d vs base trader | Expert→Elite |
| Max drawdown | `mdd` | Full history | All demotions |

### 4.2 Promotion Gate Checks

Each promotion runs the following checks. ALL must pass.

```
def check_promotion(virtual_id: str) -> PromotionResult:
    vt = load_virtual(virtual_id)
    current_tier = vt.tier

    # Common checks for all promotions
    if vt.status == 'culled':
        return FAIL("Culled traders cannot be promoted")
    if vt.errors_last_24h > 3:
        return FAIL(f"Too many runtime errors: {vt.errors_last_24h}")

    # Tier-specific gates
    if current_tier == 'probation':
        return _gate_probation_to_rookie(vt)
    if current_tier == 'rookie':
        return _gate_rookie_to_veteran(vt)
    if current_tier == 'veteran':
        return _gate_veteran_to_expert(vt)
    if current_tier == 'expert':
        return _gate_expert_to_elite(vt)
    if current_tier == 'elite':
        return _gate_elite_to_live(vt)  # delegates to virtual_rotate
```

### 4.3 Individual Gate Logic

#### Probation → Rookie

```python
def _gate_probation_to_rookie(vt):
    # Soft gate — almost automatic
    if vt.age_trading_days < 2:
        return FAIL("Probation minimum 2 trading days")
    if vt.closed_trades < 1:
        return FAIL("Must have at least 1 closed trade")
    # Runtime health
    if vt.llm_errors > 0:
        return FAIL("Had LLM errors during probation")
    return PASS()
```

#### Rookie → Veteran

```python
def _gate_rookie_to_veteran(vt):
    if vt.age_trading_days < 5:
        return FAIL("Rookie minimum 5 trading days")
    if vt.closed_trades < 5:
        return FAIL("Minimum 5 closed trades")
    pnl_7d = compute_7d_pnl(vt.name)
    if pnl_7d <= 0:
        return FAIL(f"7d P&L must be positive (got ${pnl_7d:.2f})")
    return PASS()
```

#### Veteran → Expert

```python
def _gate_veteran_to_expert(vt):
    if vt.age_trading_days < 14:
        return FAIL("Veteran minimum 14 trading days")
    if vt.closed_trades < 10:
        return FAIL("Minimum 10 closed trades")
    wr = compute_win_rate(vt.name, window=20)
    if wr < 0.50:
        return FAIL(f"Win rate below 50% (got {wr:.1%})")
    pnl_14d = compute_14d_pnl(vt.name)
    baseline_pnl = compute_14d_pnl(vt.base_trader)  # live trader P&L
    if pnl_14d < baseline_pnl * 1.05:
        return FAIL(f"14d P&L must exceed baseline by 5% "
                     f"(got ${pnl_14d:.2f} vs ${baseline_pnl:.2f})")
    return PASS()
```

#### Expert → Elite

```python
def _gate_expert_to_elite(vt):
    if vt.age_trading_days < 30:
        return FAIL("Expert minimum 30 trading days")
    if vt.closed_trades < 20:
        return FAIL("Minimum 20 closed trades")
    sharpe_30d = compute_30d_sharpe(vt.name)
    if sharpe_30d < 0.8:
        return FAIL(f"30d Sharpe below 0.8 (got {sharpe_30d:.2f})")
    alpha = compute_alpha(vt.name, vt.base_trader, window=30)
    if alpha < 1.10:
        return FAIL(f"30d alpha below 1.10x (got {alpha:.2f}x)")
    return PASS()
```

#### Elite → Live

```python
def _gate_elite_to_live(vt):
    # Delegates to existing virtual_rotate.should_promote() with
    # championship belt model. Reuses existing guardrails:
    #   - MIN_CLOSED_TRADES (10)
    #   - MAIN_MIN_DAYS (3)
    #   - LOCK_STREAK_DAYS (3)
    #   - ALL_NEGATIVE_FREEZE_DAYS (3)
    return virtual_rotate.should_promote(
        base_trader=vt.base_trader,
        main_id=find_current_main(vt.base_trader),
        challenger_id=vt.name,
        main_pnl=compute_daily_pnl([find_current_main(vt.base_trader)]),
        challenger_pnl=compute_daily_pnl([vt.name]),
    )
```

### 4.4 Scoring Windows — Configuration

All windows and thresholds are configurable via a JSON config block, stored as a system parameter:

```json
{
  "promotion": {
    "probation_to_rookie": {
      "min_days": 2,
      "min_trades": 1,
      "max_llm_errors": 0
    },
    "rookie_to_veteran": {
      "min_days": 5,
      "min_trades": 5,
      "pnl_window_days": 7,
      "pnl_threshold": 0.0
    },
    "veteran_to_expert": {
      "min_days": 14,
      "min_trades": 10,
      "win_rate_window": 20,
      "win_rate_threshold": 0.50,
      "pnl_window_days": 14,
      "pnl_baseline_multiplier": 1.05
    },
    "expert_to_elite": {
      "min_days": 30,
      "min_trades": 20,
      "sharpe_window_days": 30,
      "sharpe_threshold": 0.8,
      "alpha_window_days": 30,
      "alpha_multiplier": 1.10
    },
    "elite_to_live": {
      "min_closed_trades": 10,
      "main_min_days": 3,
      "lock_streak_days": 3,
      "max_lock_days": 5,
      "all_negative_freeze_days": 3
    }
  }
}
```

---

## 5. Demotion / Relegation Rules

Traders can move **down** tiers just as they can move up. This prevents a single lucky streak from locking in premium resources forever.

### 5.1 Automatic Demotion Triggers

| Trigger | Action | Grace Period |
|---------|--------|-------------|
| Runtime errors > 5 in 24h | Demote 1 tier (min: probation) | Immediate |
| 14d P&L < -10% of starting capital | Demote 1 tier | 3 days warning |
| Consecutive negative P&L days >= 10 | Demote 1 tier | 5 days warning |
| Win rate < 30% over last 20 trades | Demote 1 tier | 10 trades warning |
| Sharpe < 0.0 over 30 days (Expert+) | Demote 1 tier | 7 days warning |
| Beat by >1 tier below for 5 consecutive days | Demote 1 tier | 3 days warning |
| Inactivity > 5 trading days (no decisions) | Demote to probation | 2 days warning |

### 5.2 Demotion Flow

```
┌──────────────────────────────────────────────┐
│              ELITE (Tier 4)                   │
└──────────────────┬───────────────────────────┘
                   │ Demotion triggers hit
                   ▼
┌──────────────────────────────────────────────┐
│ Warning logged to promotion_log table         │
│ "Elite trader X failing: win rate 28%,        │
│  14d P&L -12%. Demoting to Expert in 3 days  │
│  unless performance improves."                │
└──────────────────┬───────────────────────────┘
                   │ Grace period expires
                   ▼
┌──────────────────────────────────────────────┐
│ Execute demotion:                             │
│ 1. Update tier in virtual_traders             │
│ 2. Adjust capabilities per matrix             │
│ 3. Log to promotion_log with reason           │
│ 4. Free slot at higher tier for others        │
└──────────────────────────────────────────────┘
```

### 5.3 Grace Period Behavior

During the grace period:

- The trader stays at its current tier with current capabilities
- A warning is emitted to the dashboard / logs
- Automated notification is sent via Telegram alert
- If performance recovers before grace expires, the demotion is cancelled
- The promoted trader that would have taken the slot waits for the grace period

### 5.4 Post-Demotion Recovery

A demoted trader is **not immediately promotion-eligible**:

```
After demotion:
  → 5 trading day cooldown before any promotion can be considered
  → Must re-pass the full promotion gate for the tier it was demoted FROM
  → Original capabilities restore on re-promotion
  → If demoted 3 times within 30 days: auto-culled
```

### 5.5 Probation Violations

Since probation is the bottom tier, violations here trigger culling directly:

| Violation | Action |
|-----------|--------|
| Any LLM error during probation | Culled |
| No trades within 5 days | Culled |
| Negative P&L at end of 2-day probation | Culled |
| Config validation failure | Culled |

### 5.6 Culling Protection for Elite

Elite traders (Tier 4) are **immune from the weekly cull** (`virtual_cull.py`). However, they can still be demoted for performance degradation (section 5.1). If demoted to Expert, cull protection is lost.

---

## 6. Feedback Loop

### 6.1 The Core Loop

The promotion mechanism is not just a reward system — it creates a **self-reinforcing feedback loop**:

```
                  ┌───────────────────────────────────────┐
                  │         PROMOTION UNLOCKS              │
                  │  - Better LLM model                    │
                  │  - More data access                    │
                  │  - Larger portfolio                    │
                  │  - Shorter tick intervals              │
                  └──────────────┬────────────────────────┘
                                 │
                                 ▼
                  ┌───────────────────────────────────────┐
                  │       IMPROVED DECISION QUALITY        │
                  │  - Better reasoning (pro model)        │
                  │  - More signals to consider (data)     │
                  │  - More capital to deploy (size)       │
                  │  - More reps = faster learning (tick)  │
                  └──────────────┬────────────────────────┘
                                 │
                                 ▼
                  ┌───────────────────────────────────────┐
                  │          BETTER RESULTS                │
                  │  - Higher P&L                          │
                  │  - Better Sharpe                       │
                  │  - Lower drawdown                      │
                  │  - More consistent wins                │
                  └──────────────┬────────────────────────┘
                                 │
                                 ▼
                  ┌───────────────────────────────────────┐
                  │      NEXT PROMOTION GATE IN REACH      │
                  │  - Metrics improve → thresholds met    │
                  │  - Confidence in trader grows          │
                  │  - Higher tier → more resources        │
                  └───────────────────────────────────────┘
```

### 6.2 Empirical Validation

This loop is testable. For each promotion, we can measure the "promotion boost":

```python
def measure_promotion_boost(virtual_id: str, promotion_date: date) -> dict:
    """Compare performance 14 days before vs 14 days after promotion."""
    before = compute_metrics(virtual_id, promotion_date - 14, promotion_date)
    after = compute_metrics(virtual_id, promotion_date, promotion_date + 14)
    return {
        "pnl_delta": after["pnl"] - before["pnl"],
        "sharpe_delta": after["sharpe"] - before["sharpe"],
        "win_rate_delta": after["win_rate"] - before["win_rate"],
        "decision_quality_delta": after["avg_conviction"] - before["avg_conviction"],
    }
```

If the promotion boost is **consistently positive** across the fleet, the mechanism is working. If not, the capability unlocks may need recalibration.

### 6.3 Anti-Pattern: Over-Promotion

**Risk**: A trader gets promoted beyond its ability, unlocks capabilities it can't use effectively, then crashes.

**Mitigation**: Track the promotion boost over time. If a trader's performance **drops** within 7 days of promotion (relative to the 7 days before), flag for human review:

```
PROMOTION_REGIME_CHECK(vt):
  pre_pnl = 7d_pnl BEFORE promotion
  post_pnl = 7d_pnl AFTER promotion
  if post_pnl < pre_pnl * 0.8:
    ALERT("Promotion may have been premature: "
          f"{vt.name} -> {vt.tier} (post/pre = {post_pnl/pre_pnl:.2%})")
```

### 6.4 Cross-Trader Learning

When a trader is promoted to Expert or Elite, its successful parameters can **seed** new virtuals at lower tiers:

```
Expert promotion event:
  → Extract top-3 signal params from expert's config
  → Create 3 rookie virtuals seeded with those params
  → Label them "descendant of <expert_name>"
  → Track: do descendants outperform random variants?
```

This creates an evolutionary tree where the best ideas cascade down.

---

## 7. Orchestrator Tick Loop Integration

### 7.1 Current Flow

The existing orchestrator (`orchestrator.py`) processes ticks like this:

```
1. Fetch pending ticks from tick_queue
2. Dispatch to 3 live traders
3. Run virtual_runner.run_once() for all virtuals
4. Log to orchestrator_log
```

### 7.2 Promotion-Aware Flow

After integration, the flow becomes:

```
1. Fetch pending ticks from tick_queue
2. Dispatch to 3 live traders (Tier 5)
3. Run virtual_runner.run_once() — but per-tier:
   a. Elite (Tier 4): full signal + LLM pipeline, 5-min tick
   b. Expert (Tier 3): full signal + LLM pipeline, 5-min tick
   c. Veteran (Tier 2): full pipeline, 10-min tick (skip every other cycle)
   d. Rookie (Tier 1): full pipeline, 15-min tick (skip 2 of 3 cycles)
   e. Probation (Tier 0): no tick processing until eventual cycle
4. Log to orchestrator_log with tier info
```

### 7.3 Tier-Aware Tick Routing

Add a `tier` column to the virtual_traders query in `virtual_runner.py`:

```python
def load_virtual_traders(names=None):
    """Load virtual traders with tier info for routing."""
    cur.execute(
        """SELECT id, name, base_trader, variant_type, config, status, tier
           FROM trading.virtual_traders
           WHERE status = 'active' AND tier_status = 'active'
           ORDER BY base_trader, name"""
    )
```

Then in `run_once()`, filter by tier to determine tick frequency:

```python
TIER_SKIP_MAP = {
    "probation": 999,  # never (runs on next_cycle timer)
    "rookie": 2,       # run every 3rd cycle (15 min at 5-min cycle)
    "veteran": 1,       # run every other cycle (10 min)
    "expert": 0,        # run every cycle (5 min)
    "elite": 0,         # run every cycle (5 min)
    "live": 0,          # run every cycle (5 min)
}

cycle_counter = 0  # increments each run_once() call

def should_skip(tier: str) -> bool:
    skip_every = TIER_SKIP_MAP.get(tier, 0)
    if skip_every <= 0:
        return False  # always run
    return (cycle_counter % (skip_every + 1)) != 0
```

### 7.4 Promotion Check Cadence

Promotion evaluations happen at specific cadences, not every tick:

| Evaluation | Cadence | Actor |
|-----------|---------|-------|
| Probation → Rookie | End of each trading day | Daily promotion check |
| Rookie → Veteran | End of each trading day | Daily promotion check |
| Veteran → Expert | End of each trading day | Daily promotion check |
| Expert → Elite | End of each trading day | Daily promotion check |
| Elite → Live | End of each trading day | `virtual_rotate.py` (existing) |
| All demotions | End of each trading day | Daily promotion check |
| Culling | Sunday 23:00 ET | `virtual_cull.py` (existing) |

The daily promotion check runs at **20:00 ET** (after market close), as a unified step:

```python
# scripts/promotion_check.py
"""
Runs at 20:00 ET daily after market close.

1. For ALL active virtual traders across all tiers:
   a. Compute promotion eligibility (section 4)
   b. Compute demotion eligibility (section 5)
   c. If either triggers → execute tier change
2. Log all decisions to promotion_log
3. Update virtual_traders.tier and tier_status
"""
```

### 7.5 Capacity Limits per Cycle

To keep LLM costs predictable, the orchestrator enforces tier-based capacity:

```
Max LLM calls per 5-min cycle:
  Tier 4 (Elite):   2 × call per cycle   = 2
  Tier 3 (Expert):  4 × call per cycle   = 4
  Tier 2 (Veteran): 2 × call every 2nd   = 1 avg
  Tier 1 (Rookie):  2 × call every 3rd   = 0.67 avg
  Tier 0 (Probation): 0 (batch later)    = 0
                                    Total ≈ 7.7 per 5-min cycle

At 78 cycles/day (market hours), total daily cost:
  ≈ 600 LLM calls/day
  ≈ $3–9/day at flash pricing ($0.005–$0.015 per decision)
```

Costs can be adjusted by modifying `TIER_SKIP_MAP` or max traders per tier.

---

## 8. Database Schema Changes

### 8.1 Existing Schema

The current `trading.virtual_traders` table (from `src/db/migrations/virtual_traders.sql` + `002_virtual_wins.sql`):

```sql
CREATE TABLE IF NOT EXISTS trading.virtual_traders (
  id SERIAL PRIMARY KEY,
  name VARCHAR(64) NOT NULL,
  base_trader VARCHAR(32) NOT NULL,
  variant_type VARCHAR(16) NOT NULL,
  config JSONB NOT NULL,
  status VARCHAR(16) DEFAULT 'active',   -- active, live, culled, probation
  live_dates DATE[],
  created_at DATE DEFAULT CURRENT_DATE,
  culled_at DATE,
  wins INTEGER DEFAULT 0
);
```

And `trading.promotion_log` (from `migrations/007_promotion_log_up.sql`):

```sql
CREATE TABLE IF NOT EXISTS trading.promotion_log (
    id SERIAL PRIMARY KEY,
    virtual_name TEXT NOT NULL,
    base_trader TEXT NOT NULL,
    live_trader_before TEXT NOT NULL,
    virtual_score REAL,
    live_score REAL,
    metric TEXT DEFAULT 'pnl',
    threshold REAL DEFAULT 10.0,
    improvement_pct REAL,
    was_rolled_back BOOLEAN DEFAULT FALSE,
    rollback_at TIMESTAMPTZ,
    notes TEXT,
    promoted_at TIMESTAMPTZ DEFAULT NOW()
);
```

### 8.2 New Migration: `008_promotion_tiers_up.sql`

```sql
-- Migration 008: Virtual trader promotion tiers
-- Adds tier tracking to virtual_traders, extends promotion_log
-- for multi-tier promotions and demotions.

-- ══════════════════════════════════════════════════════════════════════
-- 1. Add tier columns to virtual_traders
-- ══════════════════════════════════════════════════════════════════════

ALTER TABLE trading.virtual_traders
  ADD COLUMN IF NOT EXISTS tier VARCHAR(16) DEFAULT 'probation',
  ADD COLUMN IF NOT EXISTS tier_promoted_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS tier_demoted_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS tier_cooldown_until DATE,       -- demotion cooldown
  ADD COLUMN IF NOT EXISTS tier_warning_count INTEGER DEFAULT 0,  -- consecutive demotion warnings
  ADD COLUMN IF NOT EXISTS total_demotions INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS error_count_24h INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_decision_at TIMESTAMPTZ;

-- Upgrade existing records with appropriate tiers
-- 'live' status → tier='live'
-- 'active' status → tier='veteran' (best guess for existing)
-- 'probation' status → tier='probation'
UPDATE trading.virtual_traders
  SET tier = CASE
    WHEN status = 'live' THEN 'live'
    WHEN status = 'probation' THEN 'probation'
    WHEN status = 'active' THEN 'veteran'
    WHEN status = 'culled' THEN 'probation'
    ELSE 'probation'
  END
  WHERE tier IS NULL;

-- ══════════════════════════════════════════════════════════════════════
-- 2. Indexes for promotion query performance
-- ══════════════════════════════════════════════════════════════════════

CREATE INDEX IF NOT EXISTS idx_vt_tier ON trading.virtual_traders (tier);
CREATE INDEX IF NOT EXISTS idx_vt_base_tier ON trading.virtual_traders (base_trader, tier);
CREATE INDEX IF NOT EXISTS idx_vt_status_tier ON trading.virtual_traders (status, tier);

-- ══════════════════════════════════════════════════════════════════════
-- 3. Extend promotion_log for multi-tier tracking
-- ══════════════════════════════════════════════════════════════════════

ALTER TABLE trading.promotion_log
  ADD COLUMN IF NOT EXISTS tier_from VARCHAR(16),
  ADD COLUMN IF NOT EXISTS tier_to VARCHAR(16),
  ADD COLUMN IF NOT EXISTS promotion_type VARCHAR(16)  -- 'promotion', 'demotion', 'rollback'
    DEFAULT 'promotion',
  ADD COLUMN IF NOT EXISTS warnings_sent INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS grace_expires_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS suppression_reason TEXT;    -- if promotion was blocked

-- Upgrade existing rows
UPDATE trading.promotion_log
  SET tier_from = 'elite', tier_to = 'live', promotion_type = 'promotion'
  WHERE tier_from IS NULL AND was_rolled_back = FALSE;

UPDATE trading.promotion_log
  SET tier_from = 'live', tier_to = 'elite', promotion_type = 'rollback'
  WHERE tier_from IS NULL AND was_rolled_back = TRUE;

-- ══════════════════════════════════════════════════════════════════════
-- 4. Daily tier snapshot (for dashboards)
-- ══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS trading.tier_snapshots (
  id SERIAL PRIMARY KEY,
  snapshot_date DATE NOT NULL,
  base_trader VARCHAR(32) NOT NULL,
  tier VARCHAR(16) NOT NULL,
  trader_count INTEGER NOT NULL,
  avg_7d_pnl NUMERIC,
  avg_win_rate NUMERIC,
  avg_sharpe_30d NUMERIC,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tier_snap_date ON trading.tier_snapshots (snapshot_date);

-- ══════════════════════════════════════════════════════════════════════
-- 5. Daily promotion summary (single row per check run)
-- ══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS trading.promotion_summary (
  id SERIAL PRIMARY KEY,
  run_date DATE NOT NULL,
  evaluated_count INTEGER DEFAULT 0,
  promoted_count INTEGER DEFAULT 0,
  demoted_count INTEGER DEFAULT 0,
  warnings_issued INTEGER DEFAULT 0,
  errors_count INTEGER DEFAULT 0,
  duration_ms INTEGER,
  details JSONB,  -- full breakdown of decisions
  created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE trading.promotion_summary
  ADD CONSTRAINT uq_promotion_summary_date UNIQUE (run_date);
```

### 8.3 Rollback: `008_promotion_tiers_down.sql`

```sql
-- Migration 008 rollback
DROP TABLE IF EXISTS trading.tier_snapshots;
DROP TABLE IF EXISTS trading.promotion_summary;

ALTER TABLE trading.promotion_log
  DROP COLUMN IF EXISTS tier_from,
  DROP COLUMN IF EXISTS tier_to,
  DROP COLUMN IF EXISTS promotion_type,
  DROP COLUMN IF EXISTS warnings_sent,
  DROP COLUMN IF EXISTS grace_expires_at,
  DROP COLUMN IF EXISTS suppression_reason;

DROP INDEX IF EXISTS idx_vt_tier;
DROP INDEX IF EXISTS idx_vt_base_tier;
DROP INDEX IF EXISTS idx_vt_status_tier;

ALTER TABLE trading.virtual_traders
  DROP COLUMN IF EXISTS tier,
  DROP COLUMN IF EXISTS tier_promoted_at,
  DROP COLUMN IF EXISTS tier_demoted_at,
  DROP COLUMN IF EXISTS tier_cooldown_until,
  DROP COLUMN IF EXISTS tier_warning_count,
  DROP COLUMN IF EXISTS total_demotions,
  DROP COLUMN IF EXISTS error_count_24h,
  DROP COLUMN IF EXISTS last_decision_at;
```

---

## 9. Implementation Roadmap

### Phase 1 — Schema & Models (Days 1–2)

| Item | Effort | Files |
|------|--------|-------|
| Create migration 008 (up + down) | 1h | `migrations/008_promotion_tiers_up.sql`, `008_promotion_tiers_down.sql` |
| Add `Tier` enum to Python code | 30m | `src/trader.py` or new `src/tier.py` |
| Create `PromotionConfig` dataclass | 30m | `src/tier.py` |
| Add tier field to VirtualTrader dataclass | 30m | `src/virtual_runner.py` (update `load_virtual_traders`) |
| Add tier to DB DDL migration ref | 15m | `src/db/migrations/virtual_traders.sql` |

### Phase 2 — Promotion Checker (Days 3–4)

| Item | Effort | Files |
|------|--------|-------|
| Create `scripts/promotion_check.py` | 3h | New file |
| Implement tier gate functions | 2h | `scripts/promotion_check.py` |
| Implement demotion check functions | 2h | `scripts/promotion_check.py` |
| Add grace period logic | 1h | `scripts/promotion_check.py` |
| Wire to Telegram alert on promotion/demotion | 30m | `scripts/promotion_check.py` + `src/observability/telegram.py` |

### Phase 3 — Tier-Aware Runner (Days 5–6)

| Item | Effort | Files |
|------|--------|-------|
| Add tier skip map to `virtual_runner.py` | 1h | `src/virtual_runner.py` |
| Update `run_once()` for tier-based tick skipping | 2h | `src/virtual_runner.py` |
| Add regular-cycle counter | 30m | `src/virtual_runner.py` |
| Update capability injection per tier | 1h | `src/virtual_runner.py` (model, symbols, params) |
| Update orchestrator to pass tier context | 30m | `src/orchestrator.py` |

### Phase 4 — Promotion Boost Tracking (Days 7–8)

| Item | Effort | Files |
|------|--------|-------|
| Create `measure_promotion_boost()` | 1h | `scripts/promotion_check.py` |
| Add tier snapshot table population | 1h | `scripts/promotion_check.py` |
| Add dashboard endpoint for tier distribution | 2h | `src/leaderboard_api.py` |
| Add post-promotion regression alert | 30m | `scripts/promotion_check.py` |

### Phase 5 — Cross-Trader Learning (Days 9–10)

| Item | Effort | Files |
|------|--------|-------|
| Create descendant seeding on promotion | 1h | `scripts/promotion_check.py` |
| Add tracking label for descendants | 30m | Update migration + runner |
| Wire to `virtual_cull.py` (use descendant label as sort key) | 1h | `src/virtual_cull.py` |

### Phase 6 — Testing & Shadow Mode (Days 11–12)

| Item | Effort | Files |
|------|--------|-------|
| Write unit tests for gate functions | 2h | `tests/test_promotion.py` |
| Write integration test for full promotion cycle | 2h | `tests/test_promotion_e2e.py` |
| Shadow mode: run promotion_check but never write tiers | 1h | `--dry-run` flag |
| 1-week shadow observation | — | Dashboard review |
| Enable live | 15m | Remove dry-run flag |

### Phase 7 — Hardening (Days 13–14)

| Item | Effort | Files |
|------|--------|-------|
| Add rate limits / cost controls per tier | 1h | `virtual_runner.py` |
| Add dashboard tier visualization | 2h | `src/pg_dashboard.py` |
| Write rollback script for tier data | 1h | `scripts/rollback_promotion.py` |
| Document tier transitions in `docs/` | 30m | `docs/promotion.md` |

### Effort Summary

| Phase | Days | Total Effort |
|-------|------|-------------|
| 1 — Schema & Models | 2 | ~3h |
| 2 — Promotion Checker | 2 | ~8h |
| 3 — Tier-Aware Runner | 2 | ~5h |
| 4 — Boost Tracking | 2 | ~4.5h |
| 5 — Cross-Trader Learning | 2 | ~2.5h |
| 6 — Testing & Shadow | 2 | ~5h |
| 7 — Hardening | 2 | ~4.5h |
| **Total** | **14 days** | **~32h** |

---

## 10. Rollback / Safety

### 10.1 Rollback Mechanisms

| Scenario | Trigger | Action |
|----------|---------|--------|
| Promotion causes performance drop >20% | 7d post-promotion check | Automated demotion to previous tier |
| Demotion was unjustified (trader recovers) | 5d post-demotion check | Automated re-promotion (skip gates) |
| Entire tier system is wrong | Flag | `008_promotion_tiers_down.sql` migration |
| Cost overrun | Tier-5 cost alert | Reduce max traders per tier in config |

### 10.2 Safety Limits

| Limit | Value | Enforcement |
|-------|-------|-------------|
| Max promotions per base per day | 1 | Promotion check script |
| Max demotions per base per day | 2 | Promotion check script |
| Cooldown after demotion | 5 trading days | `tier_cooldown_until` |
| Max total demotions before cull | 3 within 30 days | Promotion check script |
| Max warnings before auto-demotion | 3 consecutive days | Grace period + warning count |
| Shadow mode duration | 7 days minimum | CLI flag `--dry-run` |

### 10.3 Manual Overrides

```bash
# Force promote a virtual to any tier
python3 scripts/promote_virtual_to_live.py --name kairos-aggro \
    --force --target-tier expert

# Force demote a virtual
python3 scripts/promotion_check.py --demote kairos-aggro \
    --to rookie

# Rollback last promotion for a base trader
python3 scripts/promote_virtual_to_live.py --rollback \
    --base kairos

# Freeze all promotions (emergency)
python3 scripts/promotion_check.py --freeze-all
```

The freeze flag sets a system parameter that suppresses all promotion/demotion activity until cleared:

```sql
INSERT INTO trading.system_params (key, value)
VALUES ('promotions_frozen', 'true')
ON CONFLICT (key) DO UPDATE SET value = 'true';
```

---

## 11. Appendix: Tier Transitions Reference

### 11.1 Full Transition Matrix

```
From \ To     Probation  Rookie  Veteran  Expert  Elite  Live
─────────────────────────────────────────────────────────────
Probation        —        Auto      —       —       —      —
Rookie           Demote     —      Gate2    —       —      —
Veteran          Demote   Demote     —     Gate3    —      —
Expert           Demote   Demote   Demote    —     Gate4    —
Elite            Demote   Demote   Demote  Demote    —    Gate5
Live             Demote   Demote   Demote  Demote  Demote   —
```

**Gate key**:
- `Auto` — automatic after 2 days + 1 trade
- `Gate2` — 5d + 5 trades + positive 7d P&L
- `Gate3` — 14d + 10 trades + win rate > 50% + 5% above baseline
- `Gate4` — 30d + 20 trades + Sharpe > 0.8 + 10% alpha
- `Gate5` — Belt model (challenger.wins > main.wins)
- `Demote` — automatic demotion on trigger conditions

### 11.2 Default Tier for New Virtuals

| Creation Type | Starting Tier | Notes |
|--------------|--------------|-------|
| Initial seed from cull | Probation | 2-day wait |
| Param variant from top performer | Rookie | Bypass probation (seeded from proven config) |
| Prompt variant from sweep | Probation | Untested prompt |
| Wildcard random | Probation | Full safety period |
| Descendant of Expert+ | Rookie | Bypass probation (inherits good params) |
| Manual creation | Probation | Always start at bottom |

### 11.3 Visualization (Dashboard)

Each base trader's virtual stable should be visualized as a tiered pyramid:

```
kairos base:
  Live:    [trader-kairos]  ← 1 slot
  Elite:   [kairos-elite-1]  ← 2 slots, 1 filled
  Expert:  [kairos-exp-1][kairos-exp-2]  ← 4 slots, 2 filled
  Veteran: [kairos-vee-1]...[kairos-vee-5]  ← 8 slots, 5 filled
  Rookie:  [kairos-rk-1]...[kairos-rk-7]  ← 12 slots, 7 filled
  Probation: [kairos-pro-1][kairos-pro-2]  ← unlimited
```

Slot utilization shown as progress bars per tier. Color-coded by performance (green > baseline, yellow = baseline, red < baseline).