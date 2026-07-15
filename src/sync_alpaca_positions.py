#!/usr/bin/env python3
"""
Sync Alpaca positions to Postgres trader_positions.

Pulls current positions from each trader's Alpaca paper trading account
and writes them to trading.trader_positions in Postgres.

This is the bridge between the agents' Alpaca trades and the dashboard.

Usage:
    python3 src/sync_alpaca_positions.py                    # dry-run (print)
    python3 src/sync_alpaca_positions.py --apply            # upsert into PG
    python3 src/sync_alpaca_positions.py --apply --account stonks  # single trader
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

PG_DSN = os.getenv(
    "PG_DSN",
    "host=192.168.1.179 port=5433 dbname=trading user=trader",
)

# Account credentials — same pattern as skill_alpaca.py
ACCOUNT_ENV = {
    "stonks": {
        "key": "STONKS_API_KEY",
        "secret": "STONKS_SECRET_KEY",
        "alt_key": "ALPACA_STONKS_KEY",
        "alt_secret": "ALPACA_STONKS_SECRET",
    },
    "kairos": {
        "key": "KAIROS_API_KEY",
        "secret": "KAIROS_SECRET_KEY",
        "alt_key": "ALPACA_KAIROS_KEY",
        "alt_secret": "ALPACA_KAIROS_SECRET",
    },
    "aldridge": {
        "key": "ALDRIDGE_API_KEY",
        "secret": "ALDRIDGE_SECRET_KEY",
        "alt_key": "ALPACA_ALDRIDGE_KEY",
        "alt_secret": "ALPACA_ALDRIDGE_SECRET",
    },
}


def get_alpaca_positions(account: str) -> tuple[list[dict], dict]:
    """Fetch positions + account info from Alpaca paper trading."""
    env = ACCOUNT_ENV[account]
    api_key = os.getenv(env["key"]) or os.getenv(env["alt_key"])
    secret_key = os.getenv(env["secret"]) or os.getenv(env["alt_secret"])

    if not api_key or not secret_key:
        return [], {"error": f"no credentials for {account}"}

    try:
        from alpaca.trading.client import TradingClient

        client = TradingClient(api_key, secret_key, paper=True)
        acc = client.get_account()
        positions = client.get_all_positions()

        result = []
        for p in positions:
            result.append({
                "ticker": p.symbol,
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
                "cost_basis": float(p.cost_basis),
            })

        account_info = {
            "cash": float(acc.cash),
            "portfolio_value": float(acc.portfolio_value),
            "buying_power": float(acc.buying_power),
            "equity": float(acc.equity),
        }

        return result, account_info

    except Exception as e:
        return [], {"error": str(e)}


def sync_positions(account: str, apply: bool = False):
    """Sync Alpaca positions to PG for one trader."""
    positions, account_info = get_alpaca_positions(account)

    if "error" in account_info:
        print(f"  [{account}] SKIP: {account_info['error']}")
        return

    print(f"\n{account}: {len(positions)} positions "
          f"(portfolio=${account_info.get('portfolio_value',0):,.2f})")

    if not positions:
        print("  No open positions in Alpaca")
        # Don't close existing DB positions — they might be from bankroll.md
        return

    if not apply:
        for p in positions:
            print(f"  {p['ticker']}: {p['qty']} @ ${p['avg_entry_price']:.2f} "
                  f"(current: ${p['current_price']:.2f}, P&L: ${p['unrealized_pl']:+.2f})")
        return

    # Upsert into PG
    import psycopg2
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = False
    now = datetime.now(timezone.utc).isoformat()

    try:
        cur = conn.cursor()
        trader_id = account  # bare name (matches dashboard query)
        agent_id = f"trader-{account}"

        # Get current open positions in DB for this trader
        cur.execute(
            "SELECT ticker FROM trading.trader_positions "
            "WHERE trader_id = %s AND status = 'open'",
            (trader_id,)
        )
        db_open = {r[0] for r in cur.fetchall()}
        alpaca_tickers = {p["ticker"] for p in positions}

        # Close positions in DB that Alpaca no longer has
        to_close = db_open - alpaca_tickers
        for ticker in to_close:
            cur.execute(
                "UPDATE trading.trader_positions SET status = 'closed', closed_at = %s "
                "WHERE trader_id = %s AND ticker = %s AND status = 'open'",
                (now, trader_id, ticker)
            )
            if cur.rowcount > 0:
                print(f"  [CLOSE] {ticker} (no longer in Alpaca)")

        # Upsert positions from Alpaca
        for p in positions:
            ticker = p["ticker"]
            if ticker in db_open:
                cur.execute(
                    "UPDATE trading.trader_positions SET "
                    "quantity = %s, avg_entry_price = %s, current_price = %s, "
                    "market_value = %s, unrealized_pl = %s, agent_id = %s "
                    "WHERE trader_id = %s AND ticker = %s AND status = 'open'",
                    (p["qty"], p["avg_entry_price"], p["current_price"],
                     p["market_value"], p["unrealized_pl"], agent_id,
                     trader_id, ticker)
                )
                print(f"  [UPDATE] {ticker}: {p['qty']} @ ${p['avg_entry_price']:.2f}")
            else:
                cur.execute(
                    "INSERT INTO trading.trader_positions "
                    "(agent_id, trader_id, ticker, quantity, market_value, "
                    "avg_entry_price, current_price, unrealized_pl, status, opened_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'open', %s)",
                    (agent_id, trader_id, ticker, p["qty"], p["market_value"],
                     p["avg_entry_price"], p["current_price"], p["unrealized_pl"], now)
                )
                print(f"  [INSERT] {ticker}: {p['qty']} @ ${p['avg_entry_price']:.2f}")

        conn.commit()
        print(f"  ✓ Synced {len(positions)} positions for {account}")

    except Exception as e:
        conn.rollback()
        print(f"  ✗ ERROR: {e}", file=sys.stderr)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Sync Alpaca positions to Postgres")
    parser.add_argument("--apply", action="store_true", help="Upsert into PG")
    parser.add_argument("--account", choices=list(ACCOUNT_ENV.keys()) + ["all"],
                       default="all", help="Trader to sync (default: all)")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Alpaca Position Sync ({mode}) ===")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")

    accounts = list(ACCOUNT_ENV.keys()) if args.account == "all" else [args.account]

    for account in accounts:
        sync_positions(account, apply=args.apply)

    if not args.apply:
        print(f"\nDry-run complete. Use --apply to write to PG.")

    print("Done.")


if __name__ == "__main__":
    main()