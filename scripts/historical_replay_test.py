#!/usr/bin/env python3
"""Real historical replay test using Postgres data from last 3 trading days"""
import sys, os, json
sys.path.insert(0, '/home/openclaw/projects/paper-trading-rebuild')

from src.replay import ReplayHarness, TraderDecision
from src.signals import SignalEngine, SignalParams
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import psycopg2

load_dotenv()

# Connect to Postgres
PG_DSN = os.environ.get('PG_DSN', 'host=trading-db port=5432 dbname=trading user=trader password=trader-dev-2026')

def fetch_bars(days_back=5, ticker='SPY'):
    """Fetch real 5-min bars from Postgres"""
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    # Get the last N trading days of 5-min bars
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    cur.execute("""
        SELECT timestamp, open, high, low, close, volume
        FROM market_data.bars_5min
        WHERE symbol = %s AND timestamp >= %s
        ORDER BY timestamp ASC
    """, (ticker, cutoff))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    ticks = []
    for row in rows:
        tick = type('Tick', (), {
            'timestamp': row[0],
            'ticker': ticker,
            'symbol': ticker,
            'open': float(row[1]),
            'high': float(row[2]),
            'low': float(row[3]),
            'close': float(row[4]),
            'volume': int(row[5]) if row[5] else 0
        })()
        ticks.append(tick)
    return ticks

def fetch_multi_ticker_bars(days_back=5, tickers=None):
    """Fetch bars for multiple tickers"""
    if tickers is None:
        tickers = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'JPM']
    
    all_ticks = {}
    for t in tickers:
        try:
            ticks = fetch_bars(days_back, t)
            if ticks:
                all_ticks[t] = ticks
                print(f"  {t}: {len(ticks)} bars")
        except Exception as e:
            print(f"  {t}: ERROR - {e}")
    return all_ticks

def momentum_decision(tick, portfolio):
    """Kairos momentum strategy decision with low bootstrap threshold"""
    engine = globals().get('_engine')
    if engine is None:
        return TraderDecision(ticker=tick.ticker, decision='HOLD', conviction=0, rationale='No engine')
    report = engine.process(tick)
    # Bootstrap threshold: 0.05 for composite_signal
    if report and report.composite_signal > 0.05:
        return TraderDecision(ticker=tick.ticker, decision='BUY', shares=10, conviction=report.conviction,
                rationale=f'Signal {report.composite_signal:.2f}')
    if report and report.composite_signal < -0.05 and len(portfolio.positions) > 0:
        return TraderDecision(ticker=tick.ticker, decision='SELL', shares=10, conviction=report.conviction,
                rationale=f'Close {report.composite_signal:.2f}')
    return TraderDecision(ticker=tick.ticker, decision='HOLD', conviction=0, rationale='No signal')

def run_per_ticker(name, params, ticks_by_ticker, initial_balance=10000.0):
    """Run simulation per-ticker (not interleaved) for better signal history"""
    engine = SignalEngine(params)
    total_trades = 0
    total_pnl = 0.0
    total_ticks = 0
    trade_count = 0
    
    for ticker, ticks in ticks_by_ticker.items():
        harness = ReplayHarness(initial_balance=initial_balance)
        result = harness.run(ticks, momentum_decision)
        total_ticks += len(ticks)
        total_trades += len(result.trades)
        total_pnl += result.gross_pnl
        trade_count += len(result.trades)
    
    return {
        'trader': name,
        'ticks': total_ticks,
        'trades': total_trades,
        'gross_pnl': round(total_pnl, 2),
        'win_rate': round(trade_count / max(total_trades, 1), 4) if total_trades > 0 else 0,
    }

print("=" * 70)
print("HISTORICAL TRADER REPLAY TEST")
print("=" * 70)

# Fetch real data
print("\n📊 Fetching historical bars...")
ticks_by_ticker = fetch_multi_ticker_bars(days_back=5)
total_bars = sum(len(v) for v in ticks_by_ticker.values())
print(f"   Total: {total_bars} bars across {len(ticks_by_ticker)} tickers")

if total_bars == 0:
    print("\n❌ No historical data found. Check DB connection.")
    sys.exit(1)

# Run strategies
print("\n📈 Running simulations...")
strategies = [
    ('Kairos (Aggressive)', SignalParams.relaxed_sweep()),
    ('Kairos (Default)', SignalParams()),
    ('Kairos (Conservative)', SignalParams(momentum_threshold=0.35, rsi_oversold=25, rsi_overbought=75)),
]

results = []
for name, params in strategies:
    globals()['_engine'] = SignalEngine(params)
    result = run_per_ticker(name, params, ticks_by_ticker, momentum_decision)
    results.append(result)
    print(f"  {name:35s} | Ticks: {result['ticks']:5d} | Trades: {result['trades']:3d} | P&L: ${result['gross_pnl']:>+8.2f} | WR: {result['win_rate']:.1%}")

print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"{'Strategy':35s} | {'Ticks':>5s} | {'Trades':>6s} | {'P&L':>10s} | {'WinRate':>7s}")
print("-" * 70)
for r in results:
    print(f"{r['trader']:35s} | {r['ticks']:5d} | {r['trades']:6d} | ${r['gross_pnl']:>+8.2f} | {r['win_rate']:.1%}")

os.makedirs('/tmp/trader_replay', exist_ok=True)
with open('/tmp/trader_replay/historical_results.json', 'w') as f:
    json.dump(results, f, indent=2)
print(f"\nResults saved to /tmp/trader_replay/historical_results.json")