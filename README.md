# Paper Trading Rebuild

Clean-slate, spec-driven rebuild of the paper trading system. Green CI from commit 1.

## Spec

**[SPEC.md](SPEC.md)** — canonical specification. Three AI traders (Kairos, Aldridge, Stonks) that measurably improve over time through two-speed learning, validated by rigorous out-of-sample testing, running on distributed hardware.

- Purpose, architecture, components, and verification criteria
- 25 sections covering signal engine, LLM trader, RL, regime detection, risk, and more
- 20+ verification scenarios with acceptance criteria

## Phases

| Phase | What | Status |
|-------|------|--------|
| 0 | Foundation & CI | 🚧 |
| 1 | Config isolation | ⬜ |
| 2 | Test harness | ⬜ |
| 3 | Learning loop | ⬜ |
| 4 | Risk system | ⬜ |
| 5 | Integration | ⬜ |

## Quick Start

```bash
pip install -r requirements.txt
pytest
```
