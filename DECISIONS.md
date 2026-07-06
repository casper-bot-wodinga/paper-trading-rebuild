# Decision Log

> Paired with Issues on the [project board](https://github.com/users/casper-bot-wodinga/projects/2).
> Every major design decision gets an entry. Issues track tasks; this log tracks reasoning.

---

## 2026-07-03 — Two-Phase Validation (DP-5)

**Decision:** Signal sweep → LLM validation → gated agreement before promotion.

**Why:** Phase 1 (SignalEngine) is cheap — seconds to sweep N variants. Phase 2 (replay_controller.py) is expensive — subprocess OpenRouter calls. By requiring both phases to pick the same winner, we avoid promoting a signal-only improvement that the LLM can't capitalize on. Budget gate (max 9 LLM runs/trader) keeps costs bounded at ~$0.50-$1.50/night.

**Trade-offs:** Agreement gate means we may miss genuine improvements where signal and LLM disagree. But false positives (promoting something the LLM can't use) are worse — they waste budget and pollute the config space. Divergences are logged for post-hoc analysis.

**Refs:** SPEC §6, §14 invariants. DP-5 commit `3baea86`.

---

## 2026-07-06 — Learning Mode: All Traders Start Loose

**Decision:** All three traders begin with looser restrictions, smaller-stock universes, and lower confidence thresholds. No explicit "LEARNING MODE" flag — the starting prompt IS the learning mode. The nightly learning loop tightens parameters over time.

**Why:** After 10+ days of operation, Kairos had 0 live buys, Stonks had 0 live buys, Aldridge had 1 sell. Zero data to learn from. Conservatism is the enemy of improvement — you can't optimize a system that never acts.

**Design philosophy:** The learning loop (parameter tuning, prompt evolution) should be the force that tightens restrictions, narrows watchlists, raises confidence thresholds, and reduces position sizes. The starting prompt just needs to be loose enough to generate 1-2 trades per session. Everything else should emerge from optimization.

**What changed per trader:**

| Trader | Before | After |
|--------|--------|-------|
| Kairos | $50-200 stocks, 0.55 confidence, HOLD-encouraged | $10-40 stocks, 0.30 confidence, minimum 1 BUY/SELL per session |
| Aldridge | Mega-cap only, "do nothing is underrated" | Mid-cap value included ($10-40), "do nothing" suspended |
| Stonks | Small-cap momentum, meme stocks | Same strategy but STARTING with cheap stocks, confidence 0.30 |

**Expected outcome:** 30-60 noisy trades in first 1-2 weeks → learning loop has data → parameters tighten naturally.

**Risks:** Max drawdown during learning phase could hit 5-10% (noisier trading). Mitigated by $10-40 stock range (losses are small in dollar terms) and existing circuit breaker (pauses at 10% DD).

**Refs:** `plans/learning-mode.md`, `prompts/kairos.txt`, `prompts/aldridge.txt`, `prompts/stonks.txt`

---

## 2026-07-06 — Risk Veto: Decision Quality Gate (Aldridge BAC)

**Decision:** Every BUY must have `thesis` ≥ 20 chars AND `signals_used` array with ≥ 1 entry. Missing either = trade rejected.

**Why:** Aldridge submitted `BUY BAC` with 0-character thesis and no signals_used. No rationale = no data = ungradeable. The learning loop can't score a trade with no thesis and no signals. This gate ensures every decision produces a data point the system can learn from.

**Where it lives:** On Casper's side (OpenClaw) in the decision validation layer. The Rebuild repo's risk gates (CashGate, PositionGate, etc.) are numerical; this is semantic validation in the agent execution path.

**Implementation note:** This is enforced in the output format instructions of every prompt (see `OUTPUT FORMAT` section in each prompt file). The error message template:
```
[decision_quality]: Decision quality gate: BUY <TICKER> rejected — thesis
too short (<N> chars, need 20+). Write WHY you are buying <TICKER>: what
signal, what catalyst, what edge. | signals_used missing. Record which
signals triggered this BUY (e.g. ["rsi_oversold", "macd_bullish",
"ma_crossover"]).
```

**Refs:** Issue #TBD on rebuild board.

---

## 2026-07-01 — K-Means Pushback (Regime Detection)

**Decision:** Reject naive k-means for regime detection. Require temporal features, fixed K, cluster→label mapping, and A/B evaluation gate before replacing current rule-based classifier.

**Why:** K-means ignores time (shuffles observations independently), K must be chosen arbitrarily (5? 7?), and the API surface returns `{regime, confidence}` with no mapping layer from cluster to human-readable label + statistics. Without an A/B test, we'd replace a working (if imperfect) rule-based classifier with an unvalidated alternative.

**Requirements before replacing:**
1. Temporal features: lagged t-1, t-5, t-20
2. Fixed K with data-driven selection (silhouette score)
3. Cluster→label mapping: CHOPPY→cluster_3, TRENDING→cluster_1, etc.
4. A/B test: run both classifiers 10 days, measure P&L, only replace if k-means wins

**Status:** Awaiting Casper's updated spec §5.

**Refs:** Spec §5, Issue #TBD.

---

## Template for future entries

```markdown
## YYYY-MM-DD — Short Title

**Decision:** What we decided.

**Why:** The reasoning, trade-offs considered, alternatives rejected.

**Trade-offs:** What we gain vs what we lose.

**Refs:** Issues, commits, spec sections.

```
