# Paper Trading Rebuild — Agent Instructions

**Architecture**: Three LLM traders (Kairos/momentum, Aldridge/value, Stonks/aggressive) on $10K paper portfolios. Trading ticks use **pre-assembled prompts** — `tick_prompt.py` merges live data from the data bus (quotes, signals, portfolio, journal, regime) with the trader's prompt template into one context bundle. **No tool calls during trading ticks.** The agent reads pre-assembled context and outputs JSON.

Two-speed learning: gradient descent (intraday) + nightly prompt sweeps.

---

## ⚠️ File Size Limits

| File | Hard Limit | Soft Target | Notes |
|------|-----------|-------------|-------|
| AGENTS.md | 12,000 | **2,000 chars** | Injected every tick — most expensive |
| SOUL.md | 12,000 | 3,000 chars | Persona only |
| TOOLS.md | 12,000 | 1,000 chars | Tool reminders |
| HEARTBEAT.md | 12,000 | 1,000 chars | Checklist |
| SKILL.md | Any | Any | Loaded on demand |

OpenClaw truncates files over 12,000 chars. **Rules**: `wc -c` before commit. Never append — remove before adding. Move details to SKILL.md.

---

## Tick Prompt Architecture

```
Cron → scripts/tick_prompt.py --trader <name>
  → reads prompts/<name>.txt (locked during market hours)
  → hits data bus: quotes, portfolio, regime, signals, journal
  → assembles one prompt with all injected sections
  → agent receives pre-assembled context → outputs JSON
```

Context includes: Market Context (F&G, regime, VIX), Watchlist Quotes table, Portfolio (positions + P&L), Performance Brief, Other Traders' Signals, Recent Journal (last 5), Strategy Prompt. See `specs/trader-ticks.md`.

---

## Spec Pipeline

`META-SPEC → SPEC → CODE → VERIFY → OPERATE`. Spec is source of truth. Spec changes → PR only; code → direct push (bot-owned repo).

---

## Canvas

Canvas.wodinga.studio for dev work: builds, deploys, specs, CI. No trader updates or P&L snapshots.

---

## Communication

- GitHub Issues: bugs, features, tasks
- GitHub Projects: source of truth for active work
- Hermes ↔ Casper: chat bridge (`~/projects/hermes-openclaw-bridge/`)