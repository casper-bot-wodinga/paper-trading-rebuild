# LLM Trader — Ticks & Heartbeat

**Parent**: [SPEC.md](../SPEC.md)
**Updated**: 2026-07-14 (v3 — tool-based execution architecture)
**Branch**: `v3`

---

## Two Execution Modes

| Mode | Trigger | What | DB Target |
|------|---------|------|-----------|
| **LIVE (market hours)** | Per-trader 5-min tick cron | BUY/SELL/HOLD + Alpaca execution | `trading.decisions` |
| **HISTORICAL (off-hours)** | Per-trader 5-min tick cron | BUY/SELL/HOLD sim only | `trading.historical_decisions` |

Mode flips automatically at 9:30 AM ET (→ LIVE) and 4:00 PM ET (→ HISTORICAL) via dedicated crons.

## v3 Tick Architecture

Per-trader tick crons (one per trader, offset by 1 minute):

```
Cron fires (e.g., 10:00 AM for Stonks)
  → Tick runner checks mode via mode_manager.py
  → If LIVE:
    → sessions_send MARKET TICK to trader's main session
    → Trader uses data-bus tools (get_portfolio, get_quotes, get_sentiment)
    → Trader decides BUY/SELL/HOLD
    → Trader uses exec to place Alpaca order via place_order.py
    → Trader saves decision to trading.decisions
  → If HISTORICAL:
    → Same flow, but writes to trading.historical_decisions
    → No real Alpaca orders
  → Tick runner reads reply via sessions_history
  → Tick done.
```

**Unlike the v2 spec, traders DO use tools during ticks.** The v3 architecture embraces tool-based trading because:
1. Pre-assembly requires maintaining a separate prompt builder + data fetcher (extra code to break)
2. Tool-based trading lets the LLM decide WHICH tools to call based on context
3. Self-healing: if a tool call fails, the trader can retry or fall back

### Tick Cron Details

| Trader | Cron Schedule | Offset | Why offset |
|--------|--------------|--------|-----------|
| Stonks | `*/5 9-16 * * 1-5` | 0 min | First to fire |
| Kairos | `1-56/5 9-16 * * 1-5` | +1 min | Prevents concurrent Alpaca calls |
| Aldridge | `2-57/5 9-16 * * 1-5` | +2 min | Staggers API load |

### What's Stable vs Dynamic

| Layer | Source | Changes |
|-------|--------|---------|
| Strategy, persona, rules | `agents/{trader}/AGENTS.md` + `prompt.txt` | Nightly via sweep |
| Portfolio, quotes, sentiment | Data bus (live via tools) | Every tick |
| Trade execution | `scripts/place_order.py` | Rarely |
| Mode state | `state/mode_{trader}.json` | Twice daily |
| Trader | Interval | Model | Reasoning |
|--------|----------|-------|-----------|
| Stonks | 5 min | flash | Sentiment-momentum hybrid, needs fresh data |
| Kairos | 5 min | flash | Momentum-focused, tightly coupled to regime |
| Aldridge | 5 min | flash | Value-oriented, deliberate but still 5-min tick |

**Timeout:** 180s per tick cron. Trader inference runs on the trader's main session (persistent), with the tick cron as an isolated relay.

## Decision Output (v3)

Traders output decisions as they execute, not just as text. The `place_order.py` script returns execution confirmation:

```json
{
  "decision": "BUY | SELL | HOLD",
  "ticker": "NVDA",
  "quantity": 1,
  "conviction": 0.75,
  "rationale": "MACD positive 11 ticks, volume surging, Keybanc PT $330",
  "order_id": "daa3983d-4c9e-4c06-9b97-f51c435fa3fc",
  "filled_price": 207.05,
  "stop_loss": 203.00,
  "take_profit": 215.00
}
```

## Prompt Structure (v3)

```
You are {trader_name}, a paper trading agent.
Strategy: {strategy_description}

TOOLS AVAILABLE:
  - get_portfolio(trader_id) → current positions + cash
  - get_quotes([symbols]) → OHLCV + RSI + MACD + BB + regime
  - get_sentiment(symbol) → FinBERT sentiment
  - get_market_regime() → SPY regime + signal
  - exec: python3 ~/.openclaw/workspace/scripts/place_order.py {trader} BUY TICKER QTY

FLOW:
  1. Check portfolio with get_portfolio
  2. Get quotes for your positions + watchlist
  3. Check sentiment on candidates
  4. Decide BUY/SELL/HOLD
  5. If BUY or SELL: use exec to place the order
  6. BRIEF response with decision JSON
```

## Self-Check Protocol (v3)

Each trader has a readiness check (`scripts/trader_check.py`):

```
1. API keys found?
2. Alpaca account accessible?
3. Portfolio readable?
4. Data bus healthy?
5. Order API reachable?
6. Database accessible?
7. Open orders clean?
```

Runs as part of the 5 AM nightly maintenance pipeline.

## Trader Self-Healing During Ticks

Traders are expected to ITERATE when they hit errors:
1. Try to place order → error (e.g., "bracket orders require take_profit")
2. Read the error message
3. Fix the issue (add take_profit field)
4. Retry the order
5. Confirm fill

This is the "iterate until it works" pattern — no giving up on first failure.