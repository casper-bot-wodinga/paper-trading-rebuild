# Validation & Overfitting Prevention

**Parent**: [SPEC.md](../SPEC.md)
**Updated**: 2026-07-09

---

## Walk-Forward Validation

Every parameter change must pass walk-forward validation:

```
Training window: [T-90 days, T-30 days]
Validation window: [T-30 days, T today]

Acceptance criteria:
  1. Validation Sharpe > 0 (positive on unseen data)
  2. Validation Sharpe > Baseline Sharpe (improved vs current params)
  3. Validation Sharpe > Training Sharpe × 0.7 (not grossly overfit)

If criteria fail: REJECT.
If criteria pass: ACCEPT with confidence = validation_sharpe / training_sharpe.
```

## Statistical Significance

Before accepting a parameter change, compute t-test:

```
baseline_metrics = replay(trader, current_params, validation_window)
candidate_metrics = replay(trader, proposed_params, validation_window)

t_stat = (candidate_sharpe - baseline_sharpe) / pooled_std_error

if t_stat < 1.96:  # 95% confidence
    REJECT: "Improvement not statistically significant"
```

## Minimum Evaluation Period

- Parameter changes frozen for 5 trading days after acceptance
- After 5 days: evaluate — did live performance match validation prediction?
- If live performance degraded: auto-revert and flag