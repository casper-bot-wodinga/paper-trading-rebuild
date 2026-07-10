# Drawdown Management

**Parent**: [SPEC.md](../SPEC.md)
**Updated**: 2026-07-09

---

## Circuit Breaker

| Drawdown | Action |
|----------|--------|
| < 5% | Normal trading |
| 5-10% | Position sizes reduced by 50% |
| 10-15% | Trading paused. Learning loop only (observe, don't act). |
| > 15% | Emergency stop. Trader disabled. Human must re-enable. |

## Cooling-Off

After 3 consecutive losing trades: skip the next 2 signals. Journal: "Cooling off after 3 consecutive losses."

## Recovery Mode

When trading is paused: observation-only mode. Process ticks, make mock decisions, journal — but orders not sent. Exits recovery when it can articulate a coherent reason for the drawdown and a plan.