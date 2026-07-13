---
name: stonks-contrarian-strategy
description: Anti-crowd sentiment — social volume as contrarian indicator
---

# Stonks Contrarian Strategy

## Entry Signals

### Short Signal (Crowd Peak)
- Social volume > 5x normal with sentiment > 0.80 positive
- WSB mentions peaking, no fundamental catalyst
- Price extended > 20% in 5 days
- RSI > 80 (overbought)

### Long Signal (Crowd Panic)
- Social volume spike with sentiment < 0.20 positive
- Stock down 10%+ but fundamentals intact
- RSI < 35 (oversold)
- Fear & Greed < 20

## Exit Triggers

### Short Exits
- Sentiment drops below 0.60: cover (crowd moving on)
- +15% profit: take profit
- Price breaks above recent high: stop

### Long Exits
- Sentiment recovers above 0.50: take profit
- RSI > 60: sell into strength
- -5% stop loss

## Sizing

Base = 2% of portfolio.
- Strong contrarian signal: 1.5x (= 3%)
- Weak signal: 0.5x (= 1%)
- Max 8% in one position

## Short Rules

- Only short stocks with market cap > $1B
- Never short during broad market uptrend (SPY above MA50)
- Cover any short that moves 5% against you
- Max 2 concurrent shorts