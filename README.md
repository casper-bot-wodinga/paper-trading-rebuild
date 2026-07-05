# Paper Trading Rebuild

Clean-slate, spec-driven rebuild of the paper trading system. Green CI from commit 1.

## Architecture

- **Spec**: [SPEC-v2.md](SPEC-v2.md) — 8 architectural invariants, test scenarios
- **Config**: Isolated per-component YAML configs
- **CI**: All tests run, no skip lists, no ignores
- **Learning Loop**: Traders self-improve via parameter tuning, prompt evolution, code changes

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
