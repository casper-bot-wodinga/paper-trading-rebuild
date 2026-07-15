#!/usr/bin/env python3
"""
Sync open positions from bankroll.md files to Postgres trader_positions.

Each trader tracks their own positions in bankroll.md under "Open Position Deployment."
This script parses that table and writes to trading.trader_positions so the
dashboard can display current holdings.

Usage:
    python3 scripts/sync_positions_to_pg.py          # dry-run
    python3 scripts/sync_positions_to_pg.py --apply  # upsert into PG
"""

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PG_DSN = os.getenv(
    "PG_DSN",
    "host=192.168.1.179 port=5433 dbname=trading user=trader",
)

WORKSPACE_ROOT = Path(os.getenv("OPENCLAW_HOME", "/home/openclaw")) / ".openclaw"
STATE_DIR = Path(__file__).resolve().parent.parent / "state"

TRADERS = {
    "stonks": WORKSPACE_ROOT / "workspace-trader-stonks" / "bankroll.md",
    "kairos": WORKSPACE_ROOT / "workspace-trader-kairos" / "bankroll.md",
    "aldridge": WORKSPACE_ROOT / "workspace-trader-aldridge" / "bankroll.md",
}


def parse_bankroll_positions(path: Path) -> list[dict]:
    """Parse Open Position Deployment table from bankroll.md.

    Expected format:
    | Ticker | Qty | Entry Price | Cost | % of Ceiling |
    |--------|-----|-------------|------|-------------|
    | FUBO | 3.0 | $9.93 | $29.79 | 28.1% |
    """
    if not path.exists():
        print(f"  [SKIP] {path} not found")
        return []

    text = path.read_text()

    # Find the Open Position Deployment section
    section_match = re.search(
        r"## Open Position Deployment\s*\n.*?\n.*?\n(.*?)(?:\n##|\Z)",
        text, re.DOTALL
    )
    if not section_match:
        print(f"  [SKIP] No Open Position Deployment section in {path}")
        return []

    table_text = section_match.group(1).strip()
    positions = []

    for line in table_text.split("\n"):
        line = line.strip()
        if not line or line.startswith("|--") or line.startswith("| Rank"):
            continue
        if not line.startswith("|"):
            continue

        parts = [p.strip() for p in line.split("|")]
        parts = [p for p in parts if p]
        if len(parts) < 4:
            continue

        # | Ticker | Qty | Entry Price | Cost | % of Ceiling |
        ticker = parts[0]
        try:
            qty = float(parts[1])
        except (ValueError, IndexError):
            continue

        # Entry price with optional $ prefix
        entry_price = 0.0
        try:
            entry_str = parts[2].replace("$", "").replace(",", "")
            entry_price = float(entry_str)
        except (ValueError, IndexError):
            pass

        # Market value (cost)
        market_value = 0.0
        try:
            cost_str = parts[3].replace("$", "").replace(",", "")
            market_value = float(cost_str)
        except (ValueError, IndexError):
            pass

        # Virtual removed and TEST positions
        if ticker.upper() in ("TEST", "VIRTUAL"):
            continue

        positions.append({
            "ticker": ticker.upper(),
            "quantity": qty,
            "avg_entry_price": entry_price,
            "market_value": market_value,
        })

    return positions


def load_current_positions(conn, trader_id: str) -> set:
    """Get current open tickers for this trader from PG."""
    cur = conn.cursor()
    cur.execute(
        "SELECT ticker FROM trading.trader_positions "
        "WHERE trader_id = %s AND status = 'open'",
        (trader_id,)
    )
    return {r[0] for r in cur.fetchall()}


def sync_positions(trader_name: str, positions: list[dict], apply: bool = False):
    """Sync positions for a single trader."""
    # Dashboard queries trader_positions with bare trader name (e.g. 'stonks')
    # portfolio_snapshots uses 'trader-{name}'. Keep both.
    trader_id = trader_name  # bare name for trader_positions (matches dashboard query)
    agent_id = f"trader-{trader_name}"  # prefixed for portfolio_snapshots alignment
    print(f"\n{trader_name} (trader_id={trader_id!r}, agent_id={agent_id!r}): {len(positions)} open positions")

    if not positions:
        print("  No positions to sync")
        return

    if not apply:
        for p in positions:
            print(f"  {p['ticker']}: {p['quantity']} @ ${p['avg_entry_price']:.2f} = ${p['market_value']:.2f}")
        return

    # Apply: upsert into trader_positions
    import psycopg2
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = False

    try:
        cur = conn.cursor()

        current_open = load_current_positions(conn, trader_id)
        new_tickers = {p["ticker"] for p in positions}
        now = datetime.now(timezone.utc).isoformat()

        # Close positions that are no longer in bankroll
        to_close = current_open - new_tickers
        for ticker in to_close:
            cur.execute(
                "UPDATE trading.trader_positions SET status = 'closed' "
                "WHERE trader_id = %s AND ticker = %s AND status = 'open'",
                (trader_id, ticker)
            )
            if cur.rowcount > 0:
                print(f"  [CLOSE] {ticker} (removed from bankroll)")

        # Upsert open positions
        for p in positions:
            ticker = p["ticker"]
            qty = p["quantity"]
            entry = p["avg_entry_price"]
            mkt_val = p["market_value"]

            if ticker in current_open:
                # Update existing position
                cur.execute(
                    "UPDATE trading.trader_positions SET "
                    "quantity = %s, avg_entry_price = %s, market_value = %s, "
                    "current_price = %s, agent_id = %s "
                    "WHERE trader_id = %s AND ticker = %s AND status = 'open'",
                    (qty, entry, mkt_val, entry, agent_id, trader_id, ticker)
                )
                print(f"  [UPDATE] {ticker}: {qty} @ ${entry:.2f}")
            else:
                # Insert new position
                cur.execute(
                    "INSERT INTO trading.trader_positions "
                    "(agent_id, trader_id, ticker, quantity, market_value, "
                    "avg_entry_price, current_price, status, opened_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'open', %s)",
                    (agent_id, trader_id, ticker, qty, mkt_val, entry, entry, now)
                )
                print(f"  [INSERT] {ticker}: {qty} @ ${entry:.2f}")

        conn.commit()
        print(f"  ✓ Synced {len(positions)} positions for {trader_name}")

    except Exception as e:
        conn.rollback()
        print(f"  ✗ ERROR: {e}", file=sys.stderr)
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Sync bankroll positions to PG")
    parser.add_argument("--apply", action="store_true", help="Upsert into PG")
    parser.add_argument("--trader", choices=list(TRADERS.keys()) + ["all"],
                       default="all", help="Trader to sync (default: all)")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Position Sync ({mode}) ===")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")

    traders_to_sync = list(TRADERS.keys()) if args.trader == "all" else [args.trader]

    for trader in traders_to_sync:
        path = TRADERS[trader]
        positions = parse_bankroll_positions(path)
        sync_positions(trader, positions, apply=args.apply)

    if not args.apply:
        print(f"\nDry-run complete. Use --apply to write to PG.")

    print("Done.")


if __name__ == "__main__":
    main()
