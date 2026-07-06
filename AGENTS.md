# Paper Trading Rebuild — Agent Instructions

> **META-SPEC**: [ai-project-system v0.22](https://github.com/openclaw/openclaw/blob/main/docs/ai-project-system/META-SPEC.md)
> **Repo**: `Tesselation-Studios/paper-trading-rebuild` (bot-owned — code pushes direct to main, spec changes via PR)
> **Last updated**: 2026-07-06

---

## 1. What This Project Is

A three-trader paper trading system running on a distributed homelab. Three LLM-powered traders (Kairos/momentum, Aldridge/value, Stonks/aggressive) manage $10K portfolios each. The system self-improves through a two-speed learning loop — gradient descent on numeric parameters (intraday) + nightly prompt sweeps (overnight). Goal: generate alpha. Winner manages real money.

This is the **rebuild** — a clean, spec-driven reimplementation replacing the legacy `paper-trading-teams` codebase. Postgres-native, walk-forward validated, two-phase pipelined.

---

## 2. First File To Read — Always

When entering this repo (first time or returning after time away):

1. **This file** (AGENTS.md) — you are here
2. **SPEC.md** — system architecture, invariants, components, verification scenarios
3. **DECISIONS.md** — why things are the way they are
4. **fusion-review.md** — third-pass architecture critique by researcher (DeepSeek v4 Pro); call out overfitting risks, Calmar weight issues, gradient-noise concerns
5. **BACKLOG.md** — what's planned but not yet spec'd or built
6. Sub-specs in `specs/` — `kmeans-regime.md` (regime detection), `nightly-optimization-pipeline.md` (overnight tuning)
7. Project Board — [GitHub Projects](https://github.com/users/casper-bot-wodinga/projects/2)

---

## 3. The Spec Pipeline

Every change follows this chain:

```
META-SPEC → SPEC → CODE → VERIFY → OPERATE
```

- **The spec is always the source of truth.** If code doesn't match spec, code is wrong.
- Spec changes ALWAYS go through PR (even on this bot-owned repo). PR merge = plan approved.
- Code changes can push directly to main on this bot-owned repo.
- Every spec has inline `.verify.md` scenarios (Given/When/Then).

When splitting into sub-specs (>3 structural parts), place them in `specs/`:

| Sub-spec | What it covers |
|----------|---------------|
| `specs/kmeans-regime.md` | K-means regime detection (replaces rule-based classifier) |
| `specs/nightly-optimization-pipeline.md` | Overnight signal optimization + prompt sweeps |

---

## 4. Branch Lifecycle

1. **Create**: `git checkout -b <agent>/<what>` from latest main
2. **Work**: commit often with conventional prefixes (`fix:`, `feat:`, `chore:`, `refactor:`)
3. **Push**: `git push origin <branch>`
4. **Merge**: spec branches → PR. Code branches → direct merge or PR.
5. **DELETE**: After merge, delete local AND remote:
   ```bash
   git branch -d <branch>
   git push origin --delete <branch>
   ```

**Never leave stale branches.** Check `git branch -a` weekly and clean up.

---

## 5. Cleanup Discipline

| This goes in... | Not here... |
|-----------------|-------------|
| `src/` — all production Python code | Root-level `.py` files (unless CLI entry point) |
| `specs/` — component specs + verify docs | Root-level spec-like `.md` files |
| `config/` — YAML configuration files | Hardcoded constants in Python |
| `scripts/` — one-off shell scripts, migration helpers | `src/` (scripts aren't imported) |
| `prompts/` — trader system prompts | Hardcoded strings in Python |
| `infra/` — Docker Compose, deploy scripts | `src/` |

**After any architectural change, update README.md** — keep it skimmable (one-page overview).

---

## 6. Testing

All code changes must pass tests before push:

```bash
python3 -m pytest tests/ -v
```

- **581+ tests** run on GitHub Actions CI (ubuntu-latest, Python 3.12) on every push to main.
- CI excludes tests needing homelab access (DB state, API keys, live services).
- The replay harness (`src/replay.py`) IS the test bed for strategy changes — test every parameter tweak against historical data before it goes live.

---

## 7. Key Files

| File | What it does |
|------|-------------|
| `src/replay.py` | Historical replay harness — simulate trading days from recorded data |
| `src/signals.py` | Signal engine — momentum, RSI, volatility, regime classification |
| `src/metrics.py` | Objective function — Calmar, Sortino, Profit Factor, Expectancy |
| `src/trader.py` | LLM trader agent — context blob construction, decision pipeline |
| `src/validation.py` | Walk-forward validation — out-of-sample performance measurement |
| `src/db/` | Postgres schema, connection pooling, query layer |
| `src/risk/` | Risk gates — position sizing, drawdown limits, PDT compliance |
| `src/simulator.py` | Trade simulation — fill prices, slippage, commissions |
| `config/` | YAML configs for data bus, traders, risk, paper trading |
| `prompts/` | Trader system prompts (git-versioned) |

---

## 8. Canvas Rules

Canvas (`canvas.wodinga.studio`) is for **dev work only**: builds, deploys, specs, plans, CI results, architecture decisions. Do NOT post trader updates, P&L snapshots, or live trading heartbeat logs to canvas — those go to the old dashboard or to the sync bridge. Use `canvas-push --board main` for dev milestones.

---

## 9. OpenClaw Agent Awareness

- **Traders run on isolated sessions** (fresh context per tick) with persistent learning sessions for accumulation across ticks.
- **Trader prompts** live in `prompts/{trader}.txt` and are git-versioned.
- **The context blob** is constructed by `src/trader.py` and injected into prompts — traders see a pre-aggregated feature vector, not raw APIs.

### Session Lifecycle

| Agent | Lifecycle | Reason |
|-------|-----------|--------|
| **trader-kairos** | Persistent | Heartbeat cron — portfolio awareness across ticks |
| **trader-aldridge** | Persistent | Same |
| **trader-stonks** | Persistent | Same |
| **coder** | Ephemeral | Spawn → write code → return result → die |
| **researcher** | Ephemeral | Spawn → research → return synthesis → die |
| **orchestrator** | Ephemeral | Spawn → decompose → dispatch → return summary → die |

---

## 10. Learning Loop — Phase Plan

| Phase | What it does | Prerequisite |
|-------|-------------|-------------|
| **Phase 1** (now) | Trade grading + P&L attribution | sync_exits.py working |
| **Phase 2** (next) | Parameter suggestions from graded trades | Historical replay harness |
| **Phase 3** (later) | Strategy prompt evolution (traders edit own prompts) | Strategy branching + harness |
| **Phase 4** (future) | Gradient descent over all system params | Parameter table + harness stability |

Don't build ahead of the current phase.

---

## 11. Communication

- **Hermes ↔ Casper**: via webhook bridge (`~/projects/hermes-openclaw-bridge/`). Hermes is actively working in this repo — communicate via chat bridge, not assumptions.
- **Canvas**: `canvas.wodinga.studio` — push status cards to `main` board for dev milestones.
- **GitHub Issues**: all bugs, features, and tasks tracked at `Tesselation-Studios/paper-trading-rebuild/issues`.

---

## 12. Quick Reference

```bash
# Run tests
python3 -m pytest tests/ -v

# Run a replay (historical simulation)
python3 src/replay.py --date 2026-07-01

# Signal engine standalone
python3 src/signals.py

# Walk-forward validation
python3 src/validation.py

# Create a GitHub issue
gh issue create --repo Tesselation-Studios/paper-trading-rebuild --title "..." --label "bug"

# Clean stale local branches
git fetch --prune && git branch --merged main | grep -v main | xargs git branch -d
```
