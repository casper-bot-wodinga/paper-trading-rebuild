#!/usr/bin/env python3
"""Train Kairos: replay real market data through a momentum strategy."""

import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta
from src.replay import ReplayHarness, TraderDecision
from src.db.connection import get_connection


def compute_rsi(prices, period=14):
    """Compute RSI from price series."""
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    
    # Simple SMA-based RSI
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    rsis = [50.0] * period
    for i in range(period, len(prices)):
        avg_gain = (avg_gain * (period - 1) + gains[i-1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i-1]) / period
        if avg_loss == 0:
            rsis.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsis.append(100.0 - (100.0 / (1.0 + rs)))
    return rsis


def momentum_trader(tick, portfolio):
    """Simple RSI-based momentum strategy."""
    rsi = tick.indicators.get("rsi_14", 50) if hasattr(tick, "indicators") else 50
    holding = any(p.ticker == tick.ticker for p in portfolio.positions.values())

    if holding and rsi > 70:
        return TraderDecision(ticker=tick.ticker, decision="SELL", conviction=0.7, shares=1)
    if not holding and rsi < 35 and portfolio.cash > tick.close:
        qty = min(10, int(portfolio.cash * 0.1 / tick.close))
        if qty > 0:
            return TraderDecision(ticker=tick.ticker, decision="BUY", conviction=0.8, shares=qty)
    return TraderDecision(ticker=tick.ticker, decision="HOLD", conviction=0.5)


def main():
    start_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
    tickers = ["SPY", "AAPL", "MSFT", "NVDA", "GOOGL", "META"]
    
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT timestamp, ticker, open, high, low, close, volume
            FROM market_data.bars
            WHERE timestamp >= %s AND ticker = ANY(%s)
            ORDER BY ticker, timestamp ASC
        """, (f"{start_date} 00:00:00", tickers))
        rows = cur.fetchall()
    conn.close()
    
    # Group by ticker and compute RSI for each
    by_ticker = {}
    for row in rows:
        t = row[1]
        if t not in by_ticker:
            by_ticker[t] = []
        by_ticker[t].append(row)
    
    rsi_map = {}
    for ticker, bars in by_ticker.items():
        closes = [float(b[5]) for b in bars]
        rsi_map[ticker] = compute_rsi(closes)
    
    # Build ticks with computed RSI
    tick_idx = {t: 0 for t in tickers}
    ticks = []
    for row in rows:
        t = row[1]
        idx = tick_idx[t]
        rsi = rsi_map[t][idx] if idx < len(rsi_map[t]) else 50.0
        tick_idx[t] = idx + 1
        
        ticks.append(type("Tick", (), {
            "timestamp": row[0], "ticker": t,
            "open": float(row[2]), "high": float(row[3]),
            "low": float(row[4]), "close": float(row[5]),
            "volume": float(row[6]),
            "indicators": {"rsi_14": rsi},
        })())
    
    print(f"Loaded {len(ticks)} ticks for {len(tickers)} tickers over 10 days")
    
    harness = ReplayHarness(initial_balance=10000.0)
    result = harness.run(ticks, momentum_trader)
    
    print(json.dumps({
        "final_equity": round(result.final_equity, 2),
        "win_rate": round(result.win_rate, 3),
        "total_trades": len(result.trades),
        "total_return_pct": round(result.total_return_pct, 2),
    }, indent=2))
    
    # Print trade details
    if result.trades:
        print(f"\nTrade details:")
        for t in result.trades[:10]:
            pnl = getattr(t, 'pnl', 0) or getattr(t, 'net_pnl', 0)
            print(f"  {t.ticker} {t.side} {t.shares}sh @ ${t.price:.2f} | PnL: ${pnl:.2f}")
        if len(result.trades) > 10:
            print(f"  ... and {len(result.trades)-10} more")
    
    return result


if __name__ == "__main__":
    main()
