# LLM Trader — Ticks & Heartbeat

**Parent**: [SPEC.md](../SPEC.md)
**Updated**: 2026-07-09

---

## Two Execution Modes

| Mode | Trigger | What | Reads |
|------|---------|------|-------|
| **Trading tick** | Cron every 5-15 min during market | BUY/SELL/HOLD decision | Pre-assembled prompt (signals + portfolio + journal) |
| **Heartbeat** | Separate cron, less frequent | Post-trade review, param tuning, reflection | HEARTBEAT.md |

**HEARTBEAT.md is NOT for trading.** Trading and reflection are different workloads on different cadences.

## Trader Tick Architecture

Each trading tick is a **stateless** cron job. A pre-tick script assembles the complete prompt:

```
Cron fires
  → scripts/tick_prompt.py --trader kairos
    → reads prompt template
    → hits data bus for live state (portfolio, quotes, signals)
    → renders everything into one prompt string
  → Agent receives fully-loaded context
  → First thought is about trading
  → Outputs JSON: BUY/SELL/HOLD with thesis + signals
  → Tick done. Session discarded.
```

**The LLM never touches a tool during a trading tick.** All context arrives pre-assembled.

### What's Stable vs Dynamic

| Layer | Source | Changes |
|-------|--------|---------|
| Strategy, persona, rules | `prompts/{trader}.txt` | Nightly via sweep |
| Portfolio, quotes, signals | Data bus (live) | Every tick |
| Output format (JSON schema) | Prompt template | Rarely |
| Journal context (last 5 entries) | DB | Every tick |

**Prompt text is LOCKED during market hours** (9:30-4:00 ET).

### Intervals

| Trader | Interval | Model | Thinking | Why |
|--------|----------|-------|----------|-----|
| Kairos | 5 min | flash | low | Momentum needs fresh data |
| Stonks | 15 min | flash | low | Sentiment scanning |
| Aldridge | 30 min | pro | medium | Value — deliberate |

**Timeout:** 600s for all ticks. Typical completion 30-90s with pre-assembled context.

## Decision Output

```json
{
  "decision": "BUY | SELL | HOLD",
  "ticker": "AAPL",
  "conviction": 0.72,
  "rationale": "Momentum signal 0.81, RSI at 42, SPY trending up.",
  "signal_override": false,
  "override_reason": null
}
```

## Prompt Structure

```
You are {trader_name}, a paper trading agent.
Strategy: {strategy_description}
Current regime: {regime}, confidence: {regime_confidence}
Signal Report: {signal_report}
Portfolio: {portfolio_state}
Your recent journal: {journal_entries}
Make a trading decision. You CAN override the signal engine.
```

## Nightly Prompt Sweep

After market close (16:00 ET):

1. Record yesterday's full market data (prices, signals, decisions, outcomes)
2. Generate N prompt variants via LLM (N = 20-100)
3. Fan out to workers: Docker (.179), possibly Ollama
4. Each worker replays yesterday's data with its variant
5. Rank all variants by objective_score on replay
6. If best variant > original + threshold (1% improvement): auto-PR to agents repo
7. If no variant beats original: journal "no improvement found"

## Cold Start Eliminated

Pre-assembled prompts eliminate the cold start overhead that made cron ticks impractical:

| Phase | Before (cold cron) | After (pre-assembled) |
|-------|--------------------|----------------------|
| Session init | 10-20s | 0s (tools not needed) |
| Read context | 30-60s | 0s (pre-assembled) |
| Model thinking | 1-5 min | 10-30s |
| Tool calls | 30-60s | 0s (data in prompt) |
| **Total** | **2-7 min** | **20-50s** |