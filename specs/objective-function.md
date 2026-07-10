# Objective Function

**Parent**: [SPEC.md](../SPEC.md)
**Updated**: 2026-07-09

---

## Metrics

| Metric | Formula | Weight | Why |
|--------|---------|--------|-----|
| **Calmar ratio** | annualized_return ÷ abs(max_drawdown) | 0.40 | Balances return and risk |
| **Sortino ratio** | (return - risk_free) ÷ downside_deviation | 0.15 | Only penalizes downside vol |
| **Profit factor** | gross_profit ÷ gross_loss | 0.30 | Edge detection |
| **Expectancy** | total_pnl ÷ num_trades | 0.15 | Dollar per trade |

**Knockout condition**: If max_drawdown > 15%, objective_score = 0 regardless. Trader is paused.

## Composite Score

```
objective_score(trader, window_days=30):
  1. Compute rolling metrics over window
  2. Z-score expectancy against trader's own history
  3. Apply weights
  4. Apply knockout (drawdown > 15% → score = 0)
```

## Benchmarks

Every night, compute the same metrics for SPY buy-and-hold over the identical period:

| Condition | Action |
|-----------|--------|
| Calmar < SPY Calmar | Strategy underperforms — optimizer proposes larger changes |
| Calmar > SPY Calmar, improving | Strategy has edge — optimizer fine-tunes |
| Profit factor < 1.0 for 30 days | No edge — trader journals "do I have a real strategy?" |