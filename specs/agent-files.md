# Agent File Architecture

**Parent**: [SPEC.md](../SPEC.md)
**Updated**: 2026-07-09

---

## File Types and Purposes

| File | Where | Purpose | Change Frequency |
|------|-------|---------|-----------------|
| `AGENTS.md` | `agent/AGENTS.md` | Operating manual: what the agent owns, its principles, how it operates | Occasionally |
| `SOUL.md` | `agent/SOUL.md` | Personality and voice: how the agent thinks, speaks, journals | Rarely |
| `IDENTITY.md` | `agent/IDENTITY.md` | Metadata: name, emoji, creature type, vibe | Almost never |
| `TOOLS.md` | `agent/TOOLS.md` | Local notes: SSH hosts, API endpoints, device nicknames | When infra changes |
| `SKILL.md` | `skills/<name>/SKILL.md` | Tool procedures: how to use Alpaca, how to compute RSI | When better procedure found |

## Size Limits

| File | Hard Limit | Soft Target | Why |
|------|-----------|-------------|-----|
| **AGENTS.md** | 12,000 chars | **2,000 chars** | Injected EVERY tick — most expensive |
| **SOUL.md** | 12,000 chars | 3,000 chars | Persona only |
| **TOOLS.md** | 12,000 chars | 1,000 chars | Tool reminders |
| **HEARTBEAT.md** | 12,000 chars | 1,000 chars | Checklist |
| **SKILL.md** | No limit | Any size | ✅ Loaded on demand |

OpenClaw truncates files over 12,000 chars (keeps 70% head + 20% tail). Instructions in the middle silently disappear.

## Prompt Assembly Order

When an OpenClaw agent receives a tick:

```
[IDENTITY.md]    → "I am Kairos. I trade momentum."
[AGENTS.md]      → "My job: read signals, decide BUY/SELL/HOLD, journal."
[SOUL.md]        → "I'm confident. I stick to what's proven."
[SKILL.md files] → "Here's how to use Alpaca. Here's how to compute RSI."
[TOOLS.md]       → "Alpaca endpoint: https://paper-api.alpaca.markets"
[MEMORY.md]      → "Yesterday AAPL broke out. Watching for follow-through."
[JOURNAL entries]→ Last 10 tick decisions and rationales
[TICK DATA]      → Current price, RSI, momentum, regime, portfolio state
```

## Prompt Size Constraint

- **Target:** Under 3,000 tokens for the full assembled prompt
- **Strategy:** Push technical detail into skills (loaded on demand). Keep AGENTS.md and SOUL.md tight.
- **Journal:** Cap at last 10 entries. Trim rationales to one sentence.
- **Skills:** Reference by name with 1-line summary, not full procedure text.

## What Gets Tweaked Overnight

| File | Tweaked? | How |
|------|----------|-----|
| AGENTS.md | Yes | Rule changes: thresholds, entry conditions |
| SOUL.md | Rarely | Emphasis shifts: "be aggressive" → "be patient" |
| TOOLS.md | No | Only when infra changes |
| MEMORY.md | Read-only | Used during simulation, not written back |
| SKILL.md | Yes | Procedure improvements |
| IDENTITY.md | No | Never changes |