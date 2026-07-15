#!/usr/bin/env python3
"""
sync_alpaca_positions.py — Fetch live positions from Alpaca and sync to Postgres + workspace files.

Problem: The dashboard /api/positions reads from trading.trader_positions (Postgres),
which was only populated by a one-time migration from SQLite. When traders buy/sell
through Alpaca, the positions table falls out of date. This script bridges the gap.

What it does:
  1. Fetches live positions from Alpaca API for all 3 traders
  2. UPSERTs into trading.trader_positions (Postgres on docker.klo:5433)
  3. Marks positions no longer in Alpaca as 'closed'
  4. Updates position markdown files in each trader's workspace
  5. Outputs a summary of what changed

Usage:
    python3 src/sync_alpaca_positions.py              # sync all 3 traders
    python3 src/sync_alpaca_positions.py --trader kairos   # sync one trader
    python3 src/sync_alpaca_positions.py --dry-run          # preview only
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# ── Paths & config ────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
PG_DSN = os.getenv("PG_DSN", "host=192.168.1.179 port=5433 dbname=trading user=trader")

load_dotenv(Path.home() / ".openclaw" / ".env", override=True)
local_env = PROJECT_DIR / ".env"
if local_env.exists():
    load_dotenv(local_env, override=True)

TRADER_CONFIG = {
    "kairos": {
        "agent_id": "trader-kairos",
        "trader_id": "kairos",
        "name": "Kairós Capital",
        "workspace": Path.home() / ".openclaw" / "workspace-trader-kairos",
        "key_vars": ["ALPACA_KAIROS_KEY", "KAIROS_API_KEY"],
        "secret_vars": ["ALPACA_KAIROS_SECRET", "KAIROS_SECRET_KEY"],
    },
    "aldridge": {
        "agent_id": "trader-aldridge",
        "trader_id": "aldridge",
        "name": "Aldridge & Partners",
        "workspace": Path.home() / ".openclaw" / "workspace-trader-aldridge",
        "key_vars": ["ALPACA_ALDRIDGE_KEY", "ALDRIDGE_API_KEY"],
        "secret_vars": ["ALPACA_ALDRIDGE_SECRET", "ALDRIDGE_SECRET_KEY"],
    },
    "stonks": {
        "agent_id": "trader-stonks",
        "trader_id": "stonks",
        "name": "Stonks Capital",
        "workspace": Path.home() / ".openclaw" / "workspace-trader-stonks",
        "key_vars": ["ALPACA_STONKS_KEY", "STONKS_API_KEY"],
        "secret_vars": ["ALPACA_STONKS_SECRET", "STONKS_SECRET_KEY"],
    },
}


# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn


# ── Alpaca client ─────────────────────────────────────────────────────────────

def get_alpaca_positions(trader_id: str) -> list[dict]:
    """Fetch live positions from Alpaca paper trading API."""
    cfg = TRADER_CONFIG[trader_id]
    api_key = secret_key = None
    for v in cfg["key_vars"]:
        api_key = os.getenv(v)
        if api_key:
            break
    for v in cfg["secret_vars"]:
        secret_key = os.getenv(v)
        if secret_key:
            break

    if not api_key or not secret_key:
        raise RuntimeError(f"Missing Alpaca credentials for {trader_id}")

    from alpaca.trading.client import TradingClient
    client = TradingClient(api_key, secret_key, paper=True)

    positions = []
    try:
        for p in client.get_all_positions():
            pl_pct = float(p.unrealized_plpc) * 100
            positions.append({
                "ticker": p.symbol,
                "quantity": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pl": round(float(p.unrealized_pl), 2),
                "unrealized_plpc": round(pl_pct, 2),
            })
    except Exception as e:
        raise RuntimeError(f"Failed to fetch positions for {trader_id}: {e}")

    return positions


def get_alpaca_account(trader_id: str) -> dict:
    """Fetch account summary from Alpaca."""
    cfg = TRADER_CONFIG[trader_id]
    api_key = secret_key = None
    for v in cfg["key_vars"]:
        api_key = os.getenv(v)
        if api_key:
            break
    for v in cfg["secret_vars"]:
        secret_key = os.getenv(v)
        if secret_key:
            break

    if not api_key or not secret_key:
        raise RuntimeError(f"Missing Alpaca credentials for {trader_id}")

    from alpaca.trading.client import TradingClient
    client = TradingClient(api_key, secret_key, paper=True)

    acct = client.get_account()
    return {
        "cash": float(acct.cash),
        "portfolio_value": float(acct.equity),
        "buying_power": float(acct.buying_power),
    }


# ── Postgres sync ─────────────────────────────────────────────────────────────

def _cleanup_duplicates(conn, agent_id: str):
    """Remove duplicate open positions, keeping only the most recent per ticker."""
    cur = conn.cursor()
    try:
        # Find duplicate (agent_id, ticker) pairs where status='open'
        cur.execute(
            """DELETE FROM trading.trader_positions
               WHERE id IN (
                   SELECT id FROM (
                       SELECT id,
                              ROW_NUMBER() OVER (
                                  PARTITION BY agent_id, ticker
                                  ORDER BY id DESC
                              ) as rn
                       FROM trading.trader_positions
                       WHERE agent_id = %s AND status = 'open'
                   ) sub WHERE rn > 1
               )""",
            (agent_id,),
        )
        removed = cur.rowcount
        conn.commit()
        if removed:
            print(f"  Cleaned up {removed} duplicate position rows for {agent_id}")
    except Exception as e:
        conn.rollback()
        print(f"  [WARN] Duplicate cleanup failed for {agent_id}: {e}", file=sys.stderr)


def sync_positions_to_pg(trader_id: str, alpaca_positions: list[dict],
                         dry_run: bool = False) -> dict:
    """
    Sync Alpaca positions to trading.trader_positions in Postgres.

    Strategy:
      1. Close ALL open positions for the agent
      2. Insert fresh rows from Alpaca
      3. Preserve exit_condition, stop_loss, holding_horizon_days from closed rows

    This is a "replace" approach that avoids the complexity of tracking
    individual upserts and guarantees Postgres matches Alpaca exactly.

    Returns summary dict with {added, closed, skipped, errors}.
    """
    cfg = TRADER_CONFIG[trader_id]
    agent_id = cfg["agent_id"]
    now_utc = datetime.now(timezone.utc).isoformat()

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # 1. Capture exit metadata BEFORE closing (preserves stop_loss, etc.)
    cur.execute(
        """SELECT DISTINCT ON (ticker) ticker, exit_condition, stop_loss,
                  holding_horizon_days, opened_at
           FROM trading.trader_positions
           WHERE agent_id = %s AND status = 'open'
           ORDER BY ticker, id DESC""",
        (agent_id,),
    )
    existing_meta = {r["ticker"]: {
        "exit_condition": r.get("exit_condition", ""),
        "stop_loss": r.get("stop_loss"),
        "holding_horizon_days": r.get("holding_horizon_days"),
        "opened_at": r.get("opened_at"),
    } for r in cur.fetchall()}

    # 2. Close ALL open positions for this agent
    close_count = 0
    if not dry_run:
        # First cleanup duplicates
        _cleanup_duplicates(conn, agent_id)

        # Then close all remaining open
        cur.execute(
            """UPDATE trading.trader_positions
               SET status = 'closed', closed_at = %s
               WHERE agent_id = %s AND status = 'open'""",
            (now_utc, agent_id),
        )
        close_count = cur.rowcount
        conn.commit()

    # 3. Insert fresh positions from Alpaca
    added = 0
    for pos in alpaca_positions:
        ticker = pos["ticker"]
        meta = existing_meta.get(ticker, {})

        # Use original opened_at if available, otherwise now
        opened = meta.get("opened_at") or now_utc

        if not dry_run:
            cur.execute(
                """INSERT INTO trading.trader_positions
                   (agent_id, trader_id, ticker, quantity, avg_entry_price,
                    current_price, market_value, unrealized_pl,
                    stop_loss, exit_condition, holding_horizon_days,
                    status, opened_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s)""",
                (
                    agent_id, cfg["trader_id"], ticker,
                    pos["quantity"], pos["avg_entry_price"],
                    pos["current_price"], pos["market_value"],
                    pos["unrealized_pl"],
                    meta.get("stop_loss"),
                    meta.get("exit_condition", ""),
                    meta.get("holding_horizon_days"),
                    opened,
                ),
            )
        added += 1

    conn.commit()
    conn.close()

    return {
        "added": added,
        "closed": close_count,
        "skipped": 0,
        "errors": [],
    }


# ── Workspace position files ──────────────────────────────────────────────────

def sync_positions_files(trader_id: str, alpaca_positions: list[dict],
                         account: dict, dry_run: bool = False) -> int:
    """
    Write position markdown files to the trader's workspace positions/ directory.

    Each file is named <TICKER>.md and contains standardized position details.
    Returns count of files written/updated.
    """
    cfg = TRADER_CONFIG[trader_id]
    positions_dir = cfg["workspace"] / "positions"
    written = 0

    # Build ticker set from Alpaca
    alpaca_tickers = {p["ticker"] for p in alpaca_positions}

    # Build position files for each Alpaca position
    for pos in alpaca_positions:
        ticker = pos["ticker"]
        md_path = positions_dir / f"{ticker}.md"

        qty = pos["quantity"]
        entry = pos["avg_entry_price"]
        cur = pos["current_price"]
        mkt_val = pos["market_value"]
        u_pl = pos["unrealized_pl"]
        u_pl_pct = pos["unrealized_plpc"]
        change_pct = round(((cur - entry) / entry * 100), 2) if entry else 0

        content = f"""# {ticker} Position Thesis

- **Entry**: ${entry:.2f} avg | {qty:.0f} shares
- **Current**: ${cur:.2f} ({change_pct:+.2f}% unrealized)
- **Market Value**: ${mkt_val:,.2f}
- **Unrealized P&L**: ${u_pl:+.2f} ({u_pl_pct:+.2f}%)
- **Synced from**: Alpaca paper trading
- **Last sync**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}

This position is managed through the Alpaca paper trading API.
Entry, exit, and sizing are handled by the trader agent via executor.py.
Stop-loss and take-profit levels are maintained in the trading.trader_positions table.
"""

        if dry_run:
            written += 1
            continue

        try:
            positions_dir.mkdir(parents=True, exist_ok=True)
            with open(md_path, "w") as f:
                f.write(content)
            written += 1
        except OSError as e:
            print(f"  [WARN] Failed to write {md_path}: {e}", file=sys.stderr)

    # Remove stale position files (tickers no longer in Alpaca)
    if positions_dir.exists() and not dry_run:
        for md_file in positions_dir.glob("*.md"):
            ticker = md_file.stem
            if ticker not in alpaca_tickers:
                try:
                    md_file.unlink()
                    print(f"  Removed stale position file: {ticker}.md")
                except OSError:
                    pass

    return written


# ── Portfolio snapshot ────────────────────────────────────────────────────────

def write_portfolio_snapshot(trader_id: str, account: dict,
                             positions: list[dict], dry_run: bool = False):
    """Write a portfolio snapshot to Postgres portfolio_snapshots table."""
    cfg = TRADER_CONFIG[trader_id]
    agent_id = cfg["agent_id"]
    now_utc = datetime.now(timezone.utc)

    pv = account["portfolio_value"]
    cash = account["cash"]

    # Calculate unrealized P&L
    u_pl = sum(p["unrealized_pl"] for p in positions)

    if dry_run:
        return

    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO trading.portfolio_snapshots
               (agent_id, trader_id, timestamp, cash, portfolio_value,
                unrealized_pl, daily_pnl, open_positions, source)
               VALUES (%s, %s, %s, %s, %s, %s, 0, %s, 'alpaca_sync')
               ON CONFLICT (agent_id, timestamp) DO NOTHING""",
            (agent_id, agent_id, now_utc.isoformat(), cash, pv, u_pl, len(positions)),
        )
        conn.commit()
    except Exception as e:
        print(f"  [WARN] Failed to write portfolio snapshot: {e}", file=sys.stderr)
    finally:
        conn.close()


# ── Main sync logic ───────────────────────────────────────────────────────────

def sync_trader(trader_id: str, dry_run: bool = False,
                skip_files: bool = False) -> dict:
    """Sync one trader's positions from Alpaca to Postgres and workspace files.

    Returns a summary dict with counts and any errors.
    """
    cfg = TRADER_CONFIG[trader_id]
    name = cfg["name"]
    agent_id = cfg["agent_id"]

    result = {
        "trader": trader_id,
        "name": name,
        "agent_id": agent_id,
        "positions_count": 0,
        "pg_added": 0,
        "pg_closed": 0,
        "files_written": 0,
        "errors": [],
    }

    try:
        # 1. Fetch live data from Alpaca
        positions = get_alpaca_positions(trader_id)
        account = get_alpaca_account(trader_id)
        result["positions_count"] = len(positions)
        result["portfolio_value"] = account["portfolio_value"]
        result["cash"] = account["cash"]

        # 2. Sync to Postgres
        pg_summary = sync_positions_to_pg(trader_id, positions, dry_run=dry_run)
        result["pg_added"] = pg_summary["added"]
        result["pg_closed"] = pg_summary["closed"]

        # 3. Write portfolio snapshot
        write_portfolio_snapshot(trader_id, account, positions, dry_run=dry_run)

        # 4. Sync workspace files
        if not skip_files:
            result["files_written"] = sync_positions_files(
                trader_id, positions, account, dry_run=dry_run,
            )

    except Exception as e:
        result["errors"].append(str(e))

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sync Alpaca paper trading positions to Postgres + workspace files"
    )
    parser.add_argument(
        "--trader", "-t",
        choices=["kairos", "aldridge", "stonks"],
        help="Sync a single trader (default: all three)",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Preview only — don't write to DB or files",
    )
    parser.add_argument(
        "--skip-files",
        action="store_true",
        help="Skip writing position markdown files to workspaces",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply sync (no-op, sync always applies; added for cron compatibility)",
    )
    args = parser.parse_args()

    traders = [args.trader] if args.trader else ["kairos", "aldridge", "stonks"]
    mode = "DRY RUN" if args.dry_run else "SYNC"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    print(f"=== {mode}: Alpaca positions → Postgres + workspace files ===")
    print(f"    Time: {now}")
    print()

    results = []
    total_positions = 0

    for trader_id in traders:
        result = sync_trader(trader_id, dry_run=args.dry_run,
                            skip_files=args.skip_files)
        results.append(result)

        name = result["name"]
        n = result["positions_count"]
        pv = result.get("portfolio_value", 0)
        cash = result.get("cash", 0)
        total_positions += n

        status = "✓" if not result["errors"] else "✗"
        print(f"  {status} {name} ({trader_id}): {n} positions, "
              f"PV=${pv:,.2f}, Cash=${cash:,.2f}")

        if result["pg_added"] or result["pg_closed"]:
            print(f"     DB changes: {result['pg_added']} inserted, "
                  f"{result['pg_closed']} old rows closed")

        if result["files_written"]:
            print(f"     Files: {result['files_written']} position files written")

        if result["errors"]:
            for err in result["errors"]:
                print(f"     ERROR: {err}")

        print()

    print(f"=== {mode} complete: {total_positions} total positions across "
          f"{len(results)} traders ===")

    has_errors = any(r["errors"] for r in results)
    return 1 if has_errors else 0


if __name__ == "__main__":
    sys.exit(main())
