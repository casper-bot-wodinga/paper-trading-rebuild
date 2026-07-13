#!/usr/bin/env python3
"""Postgres-backed trading dashboard — reads from Postgres with graceful fallback.

Runs as the 'dashboard' Docker service on port 5004.
Reads from Postgres (trading schema) with try/except graceful degradation
when Postgres is unreachable — no hard crashes.

Midnight commits / dashboard-only metrics are served from:
    GET /api/summary       — trader summaries (total_trades, pnl, decisions_today)
    GET /api/trades         — recent trades (7 days)
    GET /api/decisions      — recent decisions (24 hours)
    GET /api/pnl            — daily PnL (30 days) 
    GET /health             — service health (includes Postgres status)
    GET /health/dashboard   — dashboard + Postgres health detail
"""
import json
import os
import traceback
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

import psycopg2, psycopg2.extras
from flask import Flask, jsonify

app = Flask(__name__)

PG_DSN = os.getenv("PG_DSN", "host=192.168.1.179 port=5433 dbname=trading user=trader")

TRADER_META = [
    {"id": "trader-kairos",   "name": "Kairós Capital",     "manager": "Zara Chen"},
    {"id": "trader-aldridge", "name": "Aldridge & Partners", "manager": "Edmund Whitfield"},
    {"id": "trader-stonks",   "name": "Stonks Capital",      "manager": "Stan Hoolihan"},
]


# ── DB Connection ─────────────────────────────────────────────────────────────

@contextmanager
def get_db():
    """Context manager for Postgres connection. Handles connection errors gracefully."""
    conn = None
    try:
        conn = psycopg2.connect(PG_DSN, connect_timeout=5)
        conn.autocommit = True
        yield conn
    except psycopg2.OperationalError as e:
        yield None
        app.logger.warning("Postgres connection failed: %s", e)
    except Exception as e:
        yield None
        app.logger.error("Postgres error: %s", e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _check_pg_health() -> dict:
    """Check Postgres connectivity and stats. Always returns a dict — never raises."""
    result = {"connected": False, "error": None, "total_trades": 0, "decisions_last_hour": 0}
    try:
        conn = psycopg2.connect(PG_DSN, connect_timeout=3)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.execute("SELECT count(*) FROM trading.executed_trades")
        result["total_trades"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM trading.decisions WHERE timestamp > now() - interval '1 hour'")
        result["decisions_last_hour"] = cur.fetchone()[0]
        result["connected"] = True
        conn.close()
    except Exception as e:
        result["error"] = str(e)
    return result


# ── API Routes ────────────────────────────────────────────────────────────────

@app.route("/api/summary")
def api_summary():
    """Trader summaries — total_trades, pnl, decisions_today."""
    traders = []
    with get_db() as conn:
        if conn is None:
            return jsonify({"error": "Postgres unavailable", "traders": TRADER_META}), 503
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            for t in TRADER_META:
                tid = t["id"]
                cur.execute(
                    "SELECT count(*) as total, coalesce(sum(pnl), 0) as pnl "
                    "FROM trading.executed_trades WHERE agent_id = %s", (tid,))
                trades = cur.fetchone()
                cur.execute(
                    "SELECT count(*) as today FROM trading.decisions "
                    "WHERE agent_id = %s AND timestamp > now() - interval '12 hours'", (tid,))
                decisions = cur.fetchone()
                traders.append({
                    **t,
                    "total_trades": trades["total"],
                    "total_pnl": round(float(trades["pnl"]), 2),
                    "decisions_today": decisions["today"],
                })
        except Exception as e:
            return jsonify({"error": str(e), "traders": TRADER_META}), 500
    return jsonify(traders)


@app.route("/api/trades")
def api_trades():
    """Recent trades (7 days)."""
    with get_db() as conn:
        if conn is None:
            return jsonify({"error": "Postgres unavailable", "trades": []}), 503
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                "SELECT agent_id, ticker, action, quantity, entry_price, exit_price, "
                "pnl, entry_time, exit_time, status "
                "FROM trading.executed_trades "
                "WHERE entry_time > now() - interval '7 days' "
                "ORDER BY entry_time DESC LIMIT 200")
            rows = cur.fetchall()
            return jsonify([{**r,
                             "pnl": round(float(r["pnl"]), 2) if r["pnl"] else None,
                             "entry_price": round(float(r["entry_price"]), 2),
                             "exit_price": round(float(r["exit_price"]), 2) if r["exit_price"] else None}
                            for r in rows])
        except Exception as e:
            return jsonify({"error": str(e), "trades": []}), 500


@app.route("/api/decisions")
def api_decisions():
    """Recent decisions (24 hours)."""
    with get_db() as conn:
        if conn is None:
            return jsonify({"error": "Postgres unavailable", "decisions": []}), 503
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                "SELECT agent_id, ticker, action, conviction, thesis, timestamp "
                "FROM trading.decisions "
                "WHERE timestamp > now() - interval '24 hours' "
                "ORDER BY timestamp DESC LIMIT 100")
            rows = cur.fetchall()
            return jsonify([dict(r) for r in rows])
        except Exception as e:
            return jsonify({"error": str(e), "decisions": []}), 500


@app.route("/api/pnl")
def api_pnl():
    """Daily PnL (30 days)."""
    with get_db() as conn:
        if conn is None:
            return jsonify({"error": "Postgres unavailable", "pnl": []}), 503
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                "SELECT agent_id, date(entry_time) as day, "
                "count(*) as trades, round(sum(coalesce(pnl,0))::numeric, 2) as pnl "
                "FROM trading.executed_trades "
                "WHERE entry_time > now() - interval '30 days' "
                "GROUP BY 1,2 ORDER BY 2 desc, 1")
            rows = cur.fetchall()
            return jsonify([dict(r) for r in rows])
        except Exception as e:
            return jsonify({"error": str(e), "pnl": []}), 500


@app.route("/health")
def health():
    """Service health — returns Postgres connection status and stats."""
    pg = _check_pg_health()
    return jsonify({
        "status": "ok" if pg["connected"] else "degraded",
        "service": "dashboard",
        "postgres": pg,
        "checked_at": datetime.now().isoformat(),
    })


@app.route("/health/dashboard")
def health_dashboard():
    """Dashboard + Postgres health detail — for external health check aggregation."""
    pg = _check_pg_health()
    return jsonify({
        "status": "ok" if pg["connected"] else "degraded",
        "service": "dashboard",
        "postgres": {
            "connected": pg["connected"],
            "error": pg["error"],
        },
        "metrics": {
            "total_trades": pg["total_trades"],
            "decisions_last_hour": pg["decisions_last_hour"],
        },
        "checked_at": datetime.now().isoformat(),
    })


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5004, debug=False)