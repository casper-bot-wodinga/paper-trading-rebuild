#!/usr/bin/env python3
"""Replay test: run all 3 trader strategies against historical data"""
import sys, os, json, random
from datetime import datetime, timedelta

# Add project to path
sys.path.insert(0, '/home/openclaw/projects/paper-trading-rebuild')

from src.replay import ReplayHarness, TraderDecision
from src.signals import SignalEngine, SignalParams

def generate_synthetic_ticks(n=200, trend='uptrend'):
    """Generate synthetic ticks for replay testing"""
    ticks = []
    base = 500.0
    for i in range(n):
        if trend == 'uptrend':
            drift = 0.15
        elif trend == 'downtrend':
            drift = -0.15
        else:
            drift = 0.0
        noise = random.uniform(-1.0, 1.0)
        close = base + drift * i + noise * 0.5
        high = close + random.uniform(0, 1.5)
        low = close - random.uniform(0, 1.5)
        tick = type('Tick', (), {
            'timestamp': datetime.now() - timedelta(minutes=5*(n-i)),
            'ticker': 'SPY',
            'symbol': 'SPY',
            'open': close - 0.1,
            'high': high,
            'low': low,
            'close': close,
            'volume': int(1000000 + random.randint(-200000, 200000))
        })()
        ticks.append(tick)
        base = close
    return ticks

def run_trader_strategy(name, params, ticks, decision_fn):
    """Run a single trader strategy replay"""
    engine = SignalEngine(params)
    harness = ReplayHarness(initial_balance=10000.0)
    result = harness.run(ticks, decision_fn)
    return {
        'trader': name,
        'trades': len(result.trades),
        'final_equity': round(result.final_equity, 2),
        'gross_pnl': round(result.gross_pnl, 2),
        'win_rate': round(result.win_rate, 4),
        'max_drawdown': round(result.max_drawdown, 4) if hasattr(result, 'max_drawdown') else 0,
    }

# Default decision function
def momentum_decision(tick, portfolio):
    """Kairos-style: momentum + RSI"""
    engine = globals().get('_engine')
    if engine is None:
        return TraderDecision(ticker='SPY', decision='HOLD', conviction=0, rationale='No engine')
    report = engine.process(tick)
    if report and report.composite_signal > 0.15:
        return TraderDecision(ticker='SPY', decision='BUY', shares=10, conviction=report.conviction,
                rationale=f'Momentum signal {report.composite_signal:.2f} above threshold')
    if report and report.composite_signal < -0.15 and len(portfolio.positions) > 0:
        return TraderDecision(ticker='SPY', decision='SELL', shares=10, conviction=report.conviction,
                rationale=f'Negative momentum {report.composite_signal:.2f}, closing')
    return TraderDecision(ticker='SPY', decision='HOLD', conviction=0,
            rationale='No clear signal')

# Generate ticks for different market regimes
print("=" * 60)
print("TRADER REPLAY TEST — All Strategies")
print("=" * 60)

ticks_uptrend = generate_synthetic_ticks(200, 'uptrend')
ticks_downtrend = generate_synthetic_ticks(200, 'downtrend')
ticks_sideways = generate_synthetic_ticks(200, 'sideways')

strategies = [
    ('Kairos (Aggressive)', SignalParams.relaxed_sweep()),
    ('Kairos (Default)', SignalParams()),
    ('Kairos (Conservative)', SignalParams(momentum_threshold=0.35, rsi_oversold=25, rsi_overbought=75)),
]

all_results = []
for name, params in strategies:
    for regime, ticks in [('Uptrend', ticks_uptrend), ('Downtrend', ticks_downtrend), ('Sideways', ticks_sideways)]:
        globals()['_engine'] = SignalEngine(params)
        result = run_trader_strategy(f"{name} ({regime})", params, ticks, momentum_decision)
        all_results.append(result)
        print(f"  {result['trader']:45s} | Trades: {result['trades']:3d} | P&L: ${result['gross_pnl']:>+8.2f} | WR: {result['win_rate']:.1%}")

print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
print(f"{'Strategy':45s} | {'Trades':>6s} | {'P&L':>10s} | {'WinRate':>7s}")
print("-" * 72)
for r in all_results:
    print(f"{r['trader']:45s} | {r['trades']:6d} | ${r['gross_pnl']:>+8.2f} | {r['win_rate']:.1%}")

# Save results
os.makedirs('/tmp/trader_replay', exist_ok=True)
with open('/tmp/trader_replay/replay_results.json', 'w') as f:
    json.dump(all_results, f, indent=2)
print(f"\nResults saved to /tmp/trader_replay/replay_results.json")