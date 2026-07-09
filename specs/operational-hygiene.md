# Operational Hygiene

**Parent**: [SPEC.md](../SPEC.md)
**Updated**: 2026-07-09

---

## Prompt Deployment Path

Traders run inside OpenClaw workspaces. The full deployment path:

```
paper-trading-prompts/{trader}/prompt.txt  (git source of truth)
  → openclaw-workspace-trader-{name}/AGENTS.md (output format + workflow)
  → openclaw-workspace-trader-{name}/skills/persona-strategy/SKILL.md (strategy)
```

**Invariant:** After any prompt change, verify the workspace files match. A prompt in git that the trader never reads is dead code.

## Cron Hygiene

- **Inline prompts MUST preserve format rules.** "TRADE MORE. LOOSER." without "thesis 20+ chars" causes risk veto cascades.
- **Inline prompts should be minimal.** Preferred: "Follow your system prompt (AGENTS.md). Reminder: thesis 20+ chars."
- **No duplicate crons per trader.** One cron firing at overlapping times = double-inference.
- **Cron timeout ≥ model P99 × 3.**

## Intraday Monitoring

| Slot | Owner | Schedule |
|------|-------|----------|
| 9:30, 11:30, 1:30, 3:30 ET | Hermes | Odd hours |
| 10:00, 12:00, 2:00 ET | Casper | Even hours |

Both monitor all three traders and attempt to fix — not just report. Watchdog checks: stale decisions (all HOLDs), journal freshness, risk state warnings, thesis quality, timeout patterns.

## After-Hours Format Validation (8 AM ET, Mon-Fri)

Before every market open, each trader's prompt + HEARTBEAT is tested through the actual model to verify it can produce valid JSON with all required fields:

- Parses as valid JSON
- Contains all required BUY fields (thesis 20+ chars, signals_used ≥ 1, exit_condition, holding_horizon_days, stop_loss, confidence)
- OR produces a valid HOLD/SELL

**Script:** `scripts/validate_prompt_format.py`. If any trader fails, block market open. A day with broken prompts = a day with zero data.

## Change Budget

Per trader, per month: maximum 5 parameter changes at the code level. The optimizer must choose which changes matter most.

## Rollback

Every accepted code change creates a rollback point. If live performance degrades > 10% within 10 days of merge, auto-revert and journal why.