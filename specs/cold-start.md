# Cold Start & Bootstrap Phase

**Parent**: [SPEC.md](../SPEC.md)
**Updated**: 2026-07-09

---

## Bootstrap Principle (Invariant #9)

Every new trading agent or strategy begins with cheap stocks ($10-40), small positions (1-2% equity), low confidence thresholds (0.30), and permissive filters. The learning loop tightens parameters — not the starting prompt. A loose start lets the optimizer discover what works, then narrow toward it.

**This applies to any new strategy** (options, swing trading, sector rotation). Always begin noisy, let the data teach precision.

## Bootstrap Gates (§30.4)

During bootstrap (first 30 closed trades OR +5% equity, whichever comes first):

| Requirement | Bootstrap behavior | After bootstrap |
|-------------|-------------------|-----------------|
| thesis | WARNING only (log, proceed) | VETO (< 20 chars = reject) |
| signals_used | WARNING only (log, proceed) | VETO (empty array = reject) |
| exit_condition | WARNING only (default: "time_stop") | VETO |
| holding_horizon_days | Default to 5 if missing | VETO |

This prevents the death spiral: too conservative → no trades → risk veto → still no data.

## Warm-Up Period

First 10 trading days per trader:
- Position sizes: 50% of normal
- Stop losses: 1.5x wider
- No parameter tuning (insufficient data)
- No prompt evolution (insufficient data)

**Minimum thresholds before full operation:**
- 20 closed trades OR 30 trading days
- Positive expectancy in warm-up