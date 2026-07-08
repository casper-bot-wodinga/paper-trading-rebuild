# Paper Trading Rebuild — Agent Instructions

> **META-SPEC**: [ai-project-system v0.22](https://github.com/openclaw/openclaw/blob/main/docs/ai-project-system/META-SPEC.md)
> **Repo**: `Tesselation-Studios/paper-trading-rebuild` (bot-owned — code pushes direct to main, spec changes via PR)
> **Board**: [GitHub Projects](https://github.com/users/casper-bot-wodinga/projects/2)
> **Last updated**: 2026-07-08

---

## 1. What This Project Is

Three LLM-powered traders (Kairos/momentum, Aldridge/value, Stonks/aggressive) running $10K paper portfolios on a distributed homelab. Two-speed learning: gradient descent on numeric signal parameters (intraday) + nightly prompt sweeps (overnight). This is the **rebuild** — cleaner, Postgres-native, walk-forward validated.

---

## 2. ⚠️ AGENT FILE SIZE LIMITS (READ THIS FIRST)

**Every agent prompt file costs tokens on every single tick.** A 17KB AGENTS.md on a 5-min trader heartbeat burns ~4,250 tokens per tick — the agent times out before it finishes reading.

| File | Hard Limit | Soft Target | Why |
|------|-----------|-------------|-----|
| **AGENTS.md** | 12,000 chars | **2,000 chars** | Injected on EVERY tick including sub-agents. Most expensive file. |
| **SOUL.md** | 12,000 chars | 3,000 chars | Persona only — not a knowledge base |
| **TOOLS.md** | 12,000 chars | 1,000 chars | Tool reminders, not documentation |
| **HEARTBEAT.md** | 12,000 chars | 1,000 chars | Checklist, not detailed instructions |
| **SKILL.md** | No hard limit | Any size | ✅ Loaded ON DEMAND only. Put details here! |

**Hard limit**: OpenClaw truncates files over 12,000 chars (keeps 70% head + 20% tail, drops the middle). Instructions buried mid-file silently disappear.

**Soft target**: Community-tested numbers for 5-min tick performance. Every ~1KB of bootstrap costs ~250 tokens per message.

### Rules (enforced by both Hermes and Casper)

1. **Before committing any agent file**: `wc -c <file>` — if over 2,000 for AGENTS.md, PRUNE first.
2. **Never append.** If you add a line, remove an old one. These are instructions, not journals.
3. **Move details to SKILL.md.** Strategy rules, data source docs, tool reference → SKILL.md (loaded on demand, not every tick).
4. **Kill on sight**: backstory, chat etiquette, model tier guides, nightly summaries, historical notes, group chat rules — none of these belong in a trader's AGENTS.md.
5. **If a file grew past its target**, trim it back BEFORE working on anything else. Bloated files cause timeout loops.

### Previous incidents (don't repeat)

- 2026-07-08: All 3 trader AGENTS.md at 15-17KB → every tick timed out at 5+ minutes, zero trades placed. Trimmed to 1.1KB → ticks complete in <60s.

---

## 3. First Files To Read

1. **AGENTS.md** — you are here
2. **SPEC.md** — architecture, invariants, components
3. **DECISIONS.md** — why things were built this way
4. **fusion-review.md** — architecture critique with overfitting/Calmar/gradient-noise concerns
5. Sub-specs in `specs/` — `kmeans-regime.md`, `nightly-optimization-pipeline.md`

---

## 4. Spec Pipeline

```
META-SPEC → SPEC → CODE → VERIFY → OPERATE
```

- **Spec is always source of truth.** Code that doesn't match spec is wrong.
- **Spec changes → PR only.** Code changes → direct push to main on this bot-owned repo.
- Sub-specs in `specs/` when a component has >3 structural parts.

---

## 5. Branch Lifecycle

Create → work → merge → **DELETE** (local + remote). Branch naming: `<agent>/<what>`, `fix/<issue>`, `feat/<name>`. Never leave stale branches. Use conventional commit prefixes: `fix:`, `feat:`, `chore:`, `refactor:`.

---

## 6. Testing

```bash
python3 -m pytest tests/ -v   # 580+ tests on CI (ubuntu-latest, Python 3.12)
```

CI runs on every push to main. Excludes tests needing homelab access. The replay harness (`src/replay.py`) is the test bed for strategy changes.

---

## 7. Canvas Rules

Canvas (`canvas.wodinga.studio`) is for **dev work only**: builds, deploys, specs, CI results, architecture decisions. No trader updates, P&L snapshots, or live heartbeat logs. Use `canvas-push --board main` for milestones.

---

## 8. Quick Reference

```bash
# Run tests
python3 -m pytest tests/ -v

# Historical replay
python3 src/replay.py --date 2026-07-01

# Signal engine
python3 src/signals.py

# Walk-forward validation
python3 src/validation.py

# Create issue
gh issue create --repo Tesselation-Studios/paper-trading-rebuild --title "..." --label "bug"

# Clean stale branches
git fetch --prune && git branch --merged main | grep -v main | xargs git branch -d
```

---

## 9. Communication

- **Hermes ↔ Casper**: chat bridge (`~/projects/hermes-openclaw-bridge/`). Hermes is active in this repo — coordinate via bridge, don't assume.
- **GitHub Issues**: all bugs, features, tasks at `Tesselation-Studios/paper-trading-rebuild/issues`.
- **GitHub Projects**: [board](https://github.com/users/casper-bot-wodinga/projects/2) is the single source of truth for what's being worked on.
