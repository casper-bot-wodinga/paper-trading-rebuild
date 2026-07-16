#!/usr/bin/env python3
"""
sync_exits.py — Poll Alpaca for closed positions and sync exit data to the trades table.

Works by querying Alpaca's order history for filled SELL orders, then matching those to
open BUY trades in the shared trades table. When a match is found, the trade is closed with
the sell order's fill price and timestamp. All data is written exclusively to shared/trader.db.

Usage:
    python3 src/sync_exits.py --agent trader-kairos
    python3 src/sync_exits.py --agent kairos        # shorthand
    python3 src/sync_exits.py --all                   # sync all three traders
    python3 src/sync_exits.py --backfill kairos       # recompute performance

Call this from the heartbeat after each quick/daily run to ensure trade exits are tracked.
"""

import os
import sys
import json
import argparse
import sqlite3
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# ── Paths ──
PROJECT_DIR = Path(__file__).resolve().parent.parent
SHARED_DB = PROJECT_DIR / "shared" / "trader.db"

# ── Env ──
load_dotenv(Path.home() / ".openclaw" / ".env", override=True)
local_env = PROJECT_DIR / ".env"
if local_env.exists():
    load_dotenv(local_env, override=True)

# ── Agent mapping ──
# Maps CLI-friendly names → (trader_id, agent_id, api_key_env_names, secret_env_names)
AGENT_CONFIG = {
    "trader-kairos": {
        "trader_id": "kairos",
        "agent_id": "trader-kairos",
        "key_vars": ["ALPACA_KAIROS_KEY", "KAIROS_API_KEY"],
        "secret_vars": ["ALPACA_KAIROS_SECRET", "KAIROS_SECRET_KEY"],
    },
    "trader-stonks": {
        "trader_id": "stonks",
        "agent_id": "trader-stonks",
        "key_vars": ["ALPACA_STONKS_KEY", "STONKS_API_KEY"],
        "secret_vars": ["ALPACA_STONKS_SECRET", "STONKS_SECRET_KEY"],
    },
    "trader-aldridge": {
        "trader_id": "aldridge",
        "agent_id": "trader-aldridge",
        "key_vars": ["ALPACA_ALDRIDGE_KEY", "ALDRIDGE_API_KEY"],
        "secret_vars": ["ALPACA_ALDRIDGE_SECRET", "ALDRIDGE_SECRET_KEY"],
    },
    # Shorthand aliases
    "kairos": {"trader_id": "kairos", "agent_id": "trader-kairos",
               "key_vars": ["ALPACA_KAIROS_KEY", "KAIROS_API_KEY"],
               "secret_vars": ["ALPACA_KAIROS_SECRET", "KAIROS_SECRET_KEY"]},
    "stonks": {"trader_id": "stonks", "agent_id": "trader-stonks",
               "key_vars": ["ALPACA_STONKS_KEY", "STONKS_API_KEY"],
               "secret_vars": ["ALPACA_STONKS_SECRET", "STONKS_SECRET_KEY"]},
    "aldridge": {"trader_id": "aldridge", "agent_id": "trader-aldridge",
                 "key_vars": ["ALPACA_ALDRIDGE_KEY", "ALDRIDGE_API_KEY"],
                 "secret_vars": ["ALPACA_ALDRIDGE_SECRET", "ALDRIDGE_SECRET_KEY"]},
}


def get_agent_creds(agent_name: str):
    """Resolve API key and secret for an agent from env vars."""
    cfg = AGENT_CONFIG[agent_name]
    api_key = None
    secret_key = None
    for v in cfg["key_vars"]:
        api_key = os.getenv(v)
        if api_key:
            break
    for v in cfg["secret_vars"]:
        secret_key = os.getenv(v)
        if secret_key:
            break
    return cfg["agent_id"], cfg["trader_id"], api_key, secret_key


def get_filled_sell_orders(api_key: str, secret_key: str) -> list:
    """Fetch all filled SELL orders from Alpaca for the given account."""
    from alpaca.trading.client import TradingClient
    client = TradingClient(api_key, secret_key, paper=True)

    sells = []
    after_ts = None
    all_orders = []

    # Fetch up to 500 recent orders (Alpaca max per page)
    for _ in range(5):  # Max 5 pages = 2500 orders
        params = {"status": "closed", "limit": 500, "direction": "desc"}
        if after_ts:
            params["after"] = after_ts

        try:
            from alpaca.trading.requests import GetOrdersRequest
            page = client.get_orders(filter=GetOrdersRequest(**params))
            if not page:
                break
            all_orders.extend(page)

            # If we got fewer than limit, we've exhausted history
            if len(page) < 500:
                break
            after_ts = page[-1].submitted_at
        except Exception as e:
            print(f"[sync_exits] WARNING: Failed to fetch orders page: {e}", file=sys.stderr)
            break

    # Filter for filled SELL orders only
    for order in all_orders:
        side = order.side.value if hasattr(order.side, 'value') else str(order.side)
        status = order.status.value if hasattr(order.status, 'value') else str(order.status)
        if side.upper() == 'SELL' and status.upper() == 'FILLED':
            filled_price = float(order.filled_avg_price) if order.filled_avg_price else None
            if filled_price and filled_price > 0:
                sells.append({
                    "symbol": order.symbol,
                    "qty": float(order.qty),
                    "filled_price": filled_price,
                    "filled_qty": float(order.filled_qty) if order.filled_qty else float(order.qty),
                    "filled_at": str(order.filled_at) if order.filled_at else str(order.submitted_at),
                    "order_id": str(order.id),
                })

    return sells


def get_open_buy_trades(agent_id: str) -> list:
    """Get all open BUY trades for an agent from the shared trades table."""
    conn = sqlite3.connect(str(SHARED_DB), timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, agent_id, ticker, quantity, entry_price, entry_timestamp
               FROM trades
               WHERE agent_id = ? AND action = 'buy' AND status = 'open'
               ORDER BY entry_timestamp ASC""",
            (agent_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def close_trade(trade_id: int, exit_price: float, exit_timestamp: str, exit_reason: str):
    """Close a trade with P&L calculation in shared/trader.db."""
    conn = sqlite3.connect(str(SHARED_DB), timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        # Re-read trade for latest entry_price
        cursor.execute("SELECT agent_id, ticker, entry_price, quantity FROM trades WHERE id = ?", (trade_id,))
        row = cursor.fetchone()
        if not row:
            print(f"[sync_exits] WARNING: Trade {trade_id} not found", file=sys.stderr)
            return False

        trade = dict(row)
        entry_price = trade.get("entry_price", 0)
        quantity = trade.get("quantity", 0)
        if not entry_price or quantity <= 0:
            print(f"[sync_exits] WARNING: Trade {trade_id} has invalid entry_price={entry_price} or qty={quantity}", file=sys.stderr)
            return False

        pnl = round((exit_price - entry_price) * quantity, 4)
        pnl_pct = round(((exit_price - entry_price) / entry_price * 100), 4) if entry_price != 0 else 0

        cursor.execute("""
            UPDATE trades
            SET status = 'closed', exit_price = ?, exit_timestamp = ?,
                exit_reason = ?, pnl = ?, pnl_pct = ?
            WHERE id = ? AND status = 'open'
        """, (exit_price, exit_timestamp, exit_reason, pnl, pnl_pct, trade_id))

        if cursor.rowcount == 0:
            print(f"[sync_exits] INFO: Trade #{trade_id} was already closed (no rows updated)", file=sys.stderr)
            return False

        conn.commit()
        print(f"[sync_exits] Closed trade #{trade_id}: {trade['agent_id']} {trade['ticker']} "
              f"entry=${entry_price:.2f} exit=${exit_price:.2f} pnl=${pnl:.2f} ({pnl_pct:.1f}%) reason={exit_reason}")
        return True
    except Exception as e:
        print(f"[sync_exits] ERROR closing trade #{trade_id}: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return False
    finally:
        conn.close()


def _write_learning_entry(agent_id: str, ticker: str, trade: dict,
                         exit_price: float, exit_reason: str):
    """Write a P&L learning journal entry after a trade closes.

    The trader's next tick picks this up via the journal table and learns
    whether this decision made money go up or down. Writes to shared/trader.db.
    """
    entry_price = trade.get("entry_price", 0)
    quantity = trade.get("quantity", 0)
    pnl = round((exit_price - entry_price) * quantity, 2)
    pnl_pct = round((exit_price - entry_price) / entry_price * 100, 1) if entry_price else 0
    outcome = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN"

    mood = "satisfied" if outcome == "WIN" else "frustrated" if outcome == "LOSS" else "neutral"
    entry = (
        f"[LEARNING] Closed {ticker}: bought at ${entry_price:.2f}, "
        f"sold at ${exit_price:.2f} ({exit_reason}). "
        f"P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%). "
        f"Outcome: {outcome}."
    )
    try:
        conn = sqlite3.connect(str(SHARED_DB), timeout=10)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "INSERT OR IGNORE INTO journal (agent_id, timestamp, entry, mood) VALUES (?, datetime('now'), ?, ?)",
            (agent_id, entry, mood),
        )
        conn.commit()
        conn.close()
        print(f"[sync_exits] Learning entry for {agent_id}: {outcome} on {ticker} (${pnl:+.2f})")
    except Exception as e:
        print(f"[sync_exits] WARNING: Failed to write learning entry: {e}", file=sys.stderr)


def match_and_close(agent_id: str, sells: list):
    """
    Match filled SELL orders to open BUY trades and close them.

    Strategy:
    - For each filled SELL, look for an open BUY trade with matching ticker+quantity.
    - If a match is found, close the trade with the sell fill price.
    - If multiple open trades match the same sell (e.g. multiple buys), match the oldest
      open trade first (FIFO).
    """
    open_trades = get_open_buy_trades(agent_id)
    if not open_trades:
        print(f"[sync_exits] No open BUY trades for {agent_id}")
        return

    closed_count = 0
    # Track which trades we've matched to avoid double-matching
    matched_trade_ids = set()

    for sell in sells:
        ticker = sell["symbol"]
        qty = sell["filled_qty"]
        price = sell["filled_price"]
        filled_at = sell["filled_at"]

        # Find matching open BUY trades — FIFO: oldest first
        matching = [
            t for t in open_trades
            if t["ticker"].upper() == ticker.upper()
            and abs(t["quantity"] - qty) < 0.001  # float tolerance
            and t["id"] not in matched_trade_ids
        ]
        if not matching:
            continue

        trade = matching[0]  # FIFO
        matched_trade_ids.add(trade["id"])

        # Determine exit reason
        exit_reason = "stop_loss"  # default assumption for automated closes

        if close_trade(trade["id"], price, filled_at, exit_reason):
            update_performance(agent_id)

            # ── Learning loop: feed P&L back to trader ──
            _write_learning_entry(agent_id, ticker, trade, price, exit_reason)

            closed_count += 1

    print(f"[sync_exits] {agent_id}: closed {closed_count} trade(s) — {len(open_trades) - closed_count} remain open")


def sync_agent(agent_name: str):
    """Run exit sync for a single agent."""
    if agent_name not in AGENT_CONFIG:
        print(f"[sync_exits] ERROR: Unknown agent '{agent_name}'", file=sys.stderr)
        sys.exit(1)

    agent_id, trader_id, api_key, secret_key = get_agent_creds(agent_name)

    if not api_key or not secret_key:
        print(f"[sync_exits] ERROR: Missing credentials for {agent_name}", file=sys.stderr)
        sys.exit(1)

    print(f"[sync_exits] Syncing exits for {agent_id} ({trader_id})...")
    sells = get_filled_sell_orders(api_key, secret_key)
    print(f"[sync_exits] {agent_id}: found {len(sells)} filled SELL orders on Alpaca")
    match_and_close(agent_id, sells)


def update_performance(agent_id: str):
    """Compute win/loss from closed trades and update agent_profile.performance
    in the shared trader.db (NOT a workspace-local DB).

    Writes exclusively to shared/trader.db → agent_profile.performance.
    """
    # Query all closed trades for this agent from the shared DB
    conn = sqlite3.connect(str(SHARED_DB), timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        rows = conn.execute(
            """SELECT pnl FROM trades
               WHERE agent_id = ? AND status = 'closed' AND pnl IS NOT NULL""",
            (agent_id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print(f"[sync_exits] No closed trades for {agent_id}, skipping performance update")
        return

    pnls = [r[0] for r in rows]
    total_closed = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    win_rate = round(wins / total_closed, 4) if total_closed > 0 else 0.0
    total_realized_pnl = round(sum(pnls), 2)
    wins_list = [p for p in pnls if p > 0]
    losses_list = [p for p in pnls if p <= 0]
    avg_win = round(sum(wins_list) / len(wins_list), 2) if wins_list else 0.0
    avg_loss = round(sum(losses_list) / len(losses_list), 2) if losses_list else 0.0

    performance = {
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_closed_trades": total_closed,
        "total_realized_pnl": total_realized_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "updated_at": datetime.now().strftime("%Y-%m-%d"),
    }

    # Write to shared/trader.db — agent_profile table (NOT workspace-local DBs)
    try:
        shared_conn = sqlite3.connect(str(SHARED_DB), timeout=10)
        shared_conn.execute("PRAGMA busy_timeout=5000")
        shared_conn.execute(
            "UPDATE agent_profile SET performance = json(?), updated_at = datetime('now') WHERE agent_id = ?",
            (json.dumps(performance), agent_id),
        )
        shared_conn.commit()
        print(f"[sync_exits] Performance updated in shared/trader.db for {agent_id}: "
              f"{wins}W/{losses}L ({win_rate*100:.0f}%), PnL=${total_realized_pnl:.2f}")
    except Exception as e:
        print(f"[sync_exits] WARNING: Failed to update performance for {agent_id}: {e}", file=sys.stderr)
    finally:
        shared_conn.close()


def backfill_agent(agent_name: str):
    """Backfill performance stats by querying all closed trades from the shared DB."""
    if agent_name not in AGENT_CONFIG:
        print(f"[sync_exits] ERROR: Unknown agent '{agent_name}'", file=sys.stderr)
        sys.exit(1)

    cfg = AGENT_CONFIG[agent_name]
    agent_id = cfg["agent_id"]

    print(f"[sync_exits] Backfilling performance for {agent_id}...")
    update_performance(agent_id)
    print(f"[sync_exits] Backfill complete for {agent_id}")


def main():
    parser = argparse.ArgumentParser(description="Sync closed Alpaca positions to trades table")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--agent", help="Agent name: trader-kairos, kairos, trader-stonks, stonks, trader-aldridge, aldridge")
    group.add_argument("--all", action="store_true", help="Sync all three traders")
    group.add_argument("--backfill", help="Backfill performance stats for a specific agent", metavar="AGENT")
    args = parser.parse_args()

    if args.all:
        for name in ["trader-kairos", "trader-stonks", "trader-aldridge"]:
            sync_agent(name)
    elif args.backfill:
        backfill_agent(args.backfill)
    else:
        sync_agent(args.agent)


if __name__ == "__main__":
    main()