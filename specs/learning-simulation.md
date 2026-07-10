# Simulation & Learning Engine

**Parent**: [SPEC.md](../SPEC.md)
**Updated**: 2026-07-09

---

The overnight training system. Tests hundreds of prompt × parameter scenarios on historical data. Proposes hypotheses, tests them, promotes what works.

## Scale

| Pipeline | Scenarios | Data Window | Model | Cost Est. |
|----------|-----------|-------------|-------|-----------|
| **Fast screen** (nightly) | ~150-300 | 5 days | v4-flash | ~$0.10 |
| **Deep validation** (nightly, top candidates) | ~15-25 | 30 days | v4-flash | ~$0.08 |
| **Weekend sweep** (top performers) | ~5-10 | 90 days | v4-pro + flash | ~$0.15 |

## What Gets Tested

**Prompt axis:** Decision rules, tool usage, risk posture, journal style, skill reference order.

**Parameter axis:** momentum_threshold, rsi_oversold, base_size_pct, stop_loss_pct, conviction_multiplier, etc.

**Regime axis:** Run scenarios filtered by market regime to discover regime-specific optimal configs.

## Autonomous Hypothesis Generation

The system doesn't just test human-proposed variants. It proposes its own:

1. Load last night's results (all scenarios, all scores)
2. Group by: trader, regime, prompt variant, param config
3. Find patterns: "Kairos scores +0.15 better when momentum_threshold > 0.55 in TRENDING_UP"
4. Generate hypotheses for next night's scenarios

Winners persist, losers drop out, new ideas generated from patterns. Human review optional.

## Growing Data Window

| Day | Window | Ticks |
|-----|--------|-------|
| 1 | Yesterday | 5 |
| 7 | Last 5 days | 25 |
| 14 | Last 10 days | 50 |
| 30 | Last 20 days | 100 |
| 90 | Last 60 days | 300 (weekend) |

## Three-Phase Night Pipeline

| Phase | What | Duration |
|-------|------|----------|
| **1. Backfill** | yfinance → Postgres, all tickers, 30 days | ~5 min |
| **2. Sweep (relaxed)** | All traders × relaxed thresholds × variants | ~60 min |
| **3. Auto-relax & re-sweep** | Detect 0-trade runs, lower thresholds, re-run | ~120 min |

Max 3 relaxation iterations per night. Threshholds converge to the level where trades actually happen, then optimize for quality.

## CLI

```bash
python3 -m src.simulator sweep --all                    # nightly: all traders
python3 -m src.simulator sweep --trader kairos          # single trader
python3 -m src.simulator deep --trader kairos           # top candidates, 30-day
python3 -m src.simulator weekend                        # 90-day deep sweep
python3 -m src.simulator analyze --trader kairos        # generate hypotheses
python3 -m src.simulator promote --trader kairos        # auto-promote if thresholds met
```