#!/usr/bin/env python3
"""
scoring.py — Composite agent scoring for the paper trading leaderboard.

Computes a risk-adjusted score for each trader agent based on:
- Total return (portfolio value vs starting capital)
- Drawdown penalty (max drawdown reduces score)
- Violation penalties (risk gate vetoes, margin calls, etc.)
- Win rate bonus (consistent winners get a small boost)

Usage:
    from src.scoring import compute_score
    result = compute_score("trader-kairos")
    # Returns: {"score": 0.85, "ending_value": 10423.50, ...}

Score formula:
    base_score = (ending_value / starting_value) - 1.0
    drawdown_penalty = min(0, max_drawdown_pct) * 0.5
    violation_penalty = sum(vetoes * 0.02 + margin_events * 0.05)
    score = round(base_score + drawdown_penalty - violation_penalty, 4)
"""

import json
import os
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# ── config ────────────────────────────────────────────────────────────────────
load_dotenv(Path.home() / ".openclaw" / ".env")
load_dotenv(Path(".env"), override=False)

PG_DSN = os.getenv("PG_DSN", "host=192.168.1.179 port=5433 dbname=trading user=trader")
STARTING_VALUE = 10_000.0

# Active agent IDs for scoring
LIVE_AGENTS = ["trader-kairos", "trader-aldridge", "trader-stonks"]


# ── helpers ────────────────────────────────────────────────────────────────────

def _get_db():
    """Get a Postgres connection."""
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    return conn


def _get_latest_portfolio(agent_id: str) -> Optional[dict]:
    """Get the latest portfolio snapshot for an agent."""
    try:
        conn = _get_db()
    except Exception:
        return None
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT portfolio_value, cash, daily_pnl, timestamp
               FROM trading.portfolio_snapshots
               WHERE trader_id = %s
               ORDER BY timestamp DESC LIMIT 1""",
            (agent_id,),
        )
        row = cur.fetchone()
        if row:
            return {
                "portfolio_value": float(row["portfolio_value"]),
                "cash": float(row["cash"]),
                "daily_pnl": float(row["daily_pnl"]) if row["daily_pnl"] else 0.0,
                "timestamp": row["timestamp"],
            }
        return None
    except Exception:
        return None
    finally:
        conn.close()


def _get_max_drawdown(agent_id: str) -> float:
    """Compute max drawdown from portfolio_snapshots over the last 30 days.

    Drawdown = (current_value - peak_value) / peak_value
    We track the rolling peak and compute the max drawdown percentage.
    """
    try:
        conn = _get_db()
    except Exception:
        return 0.0
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT portfolio_value, timestamp
               FROM trading.portfolio_snapshots
               WHERE trader_id = %s
                 AND timestamp > NOW() - INTERVAL '30 days'
               ORDER BY timestamp ASC""",
            (agent_id,),
        )
        rows = cur.fetchall()
        if not rows:
            return 0.0

        peak = 0.0
        max_drawdown = 0.0
        for r in rows:
            pv = float(r["portfolio_value"])
            if pv > peak:
                peak = pv
            drawdown = (pv - peak) / peak if peak > 0 else 0.0
            if drawdown < max_drawdown:
                max_drawdown = drawdown

        return round(max_drawdown, 4)
    except Exception:
        return 0.0
    finally:
        conn.close()


def _get_violation_count(agent_id: str) -> int:
    """Count recent risk gate vetoes for this agent."""
    try:
        conn = _get_db()
    except Exception:
        return 0
    try:
        cur = conn.cursor()
        # Count risk_events where the agent was vetoed
        cur.execute(
            """SELECT COUNT(*) as cnt
               FROM trading.risk_events
               WHERE trader_id = %s
                 AND vetoed = true
                 AND timestamp > NOW() - INTERVAL '7 days'""",
            (agent_id,),
        )
        row = cur.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


def _get_margin_events(agent_id: str) -> int:
    """Count recent margin-related events for this agent."""
    try:
        conn = _get_db()
    except Exception:
        return 0
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT COUNT(*) as cnt
               FROM trading.risk_events
               WHERE trader_id = %s
                 AND (risk_rule ILIKE '%%margin%%' OR reason ILIKE '%%margin%%')
                 AND timestamp > NOW() - INTERVAL '7 days'""",
            (agent_id,),
        )
        row = cur.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


def _get_win_rate(agent_id: str) -> float:
    """Compute win rate from executed_trades PnL data."""
    try:
        conn = _get_db()
    except Exception:
        return 0.0
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                      SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses
               FROM trading.trades
               WHERE trader_id = %s
                 AND pnl IS NOT NULL""",
            (agent_id,),
        )
        row = cur.fetchone()
        if row and row["total"] and row["total"] > 0:
            total = int(row["total"])
            wins = int(row["wins"]) if row["wins"] else 0
            return round(wins / total, 4)
        return 0.0
    except Exception:
        return 0.0
    finally:
        conn.close()


# ── public API ────────────────────────────────────────────────────────────────

def compute_score(agent_id: str) -> dict:
    """Compute the composite score for a single agent.

    Returns a dict with:
        score: float (risk-adjusted composite score)
        ending_value: float (latest portfolio value)
        total_return: float (percentage return vs starting capital)
        max_drawdown: float (max drawdown percentage, negative)
        drawdown_penalty: float (penalty applied for drawdown)
        violation_count: int (recent risk gate vetoes)
        margin_events: int (recent margin events)
        violation_penalties: float (total penalty for violations)
        win_rate: float (percentage of winning trades)
        score_components: dict (debug breakdown of score calculation)
    """
    portfolio = _get_latest_portfolio(agent_id)
    if not portfolio:
        return {
            "score": 0.0,
            "ending_value": 0.0,
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "drawdown_penalty": 0.0,
            "violation_count": 0,
            "margin_events": 0,
            "violation_penalties": 0.0,
            "win_rate": 0.0,
            "score_components": {"error": "no portfolio data"},
        }

    ending_value = portfolio["portfolio_value"]
    total_return = (ending_value / STARTING_VALUE) - 1.0
    max_drawdown = _get_max_drawdown(agent_id)
    violation_count = _get_violation_count(agent_id)
    margin_events = _get_margin_events(agent_id)
    win_rate = _get_win_rate(agent_id)

    # ── Score calculation ──────────────────────────────────────────────────
    # Base: total return (e.g. 0.05 for 5% return)
    base_score = total_return

    # Drawdown penalty: half of the max drawdown magnitude
    # e.g. -20% drawdown → -10% penalty
    drawdown_penalty = max_drawdown * 0.5

    # Violation penalties: 2% per veto, 5% per margin event
    violation_penalties = -(violation_count * 0.02 + margin_events * 0.05)

    # Win rate bonus: small boost for consistent winners
    win_rate_bonus = 0.0
    if win_rate > 0.5:
        # Bonus scales from 0 at 50% to 0.02 at 100%
        win_rate_bonus = (win_rate - 0.5) * 0.04

    # Composite score
    score = round(base_score + drawdown_penalty + violation_penalties + win_rate_bonus, 4)

    return {
        "score": score,
        "ending_value": round(ending_value, 2),
        "total_return": round(total_return, 4),
        "max_drawdown": round(max_drawdown, 4),
        "drawdown_penalty": round(drawdown_penalty, 4),
        "violation_count": violation_count,
        "margin_events": margin_events,
        "violation_penalties": round(violation_penalties, 4),
        "win_rate": round(win_rate, 4),
        "score_components": {
            "base_score": round(base_score, 4),
            "drawdown_penalty": round(drawdown_penalty, 4),
            "violation_penalties": round(violation_penalties, 4),
            "win_rate_bonus": round(win_rate_bonus, 4),
            "formula": "base + drawdown_penalty + violation_penalties + win_rate_bonus",
        },
    }


def compute_all_scores() -> list[dict]:
    """Compute scores for all live agents. Returns a list of score dicts."""
    results = []
    for agent_id in LIVE_AGENTS:
        score = compute_score(agent_id)
        score["agent_id"] = agent_id
        results.append(score)
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Compute agent scores")
    parser.add_argument("--agent", choices=["trader-kairos", "trader-aldridge", "trader-stonks"],
                        help="Score a single agent")
    args = parser.parse_args()

    if args.agent:
        result = compute_score(args.agent)
        print(f"\n=== Score: {args.agent} ===")
        print(f"  Score:          {result['score']:+.4f}")
        print(f"  Portfolio:      ${result['ending_value']:,.2f}")
        print(f"  Total Return:   {result['total_return']:+.2%}")
        print(f"  Max Drawdown:   {result['max_drawdown']:.2%}")
        print(f"  Drawdown Penalty: {result['drawdown_penalty']:.4f}")
        print(f"  Violations:     {result['violation_count']}")
        print(f"  Margin Events:  {result['margin_events']}")
        print(f"  Violation Penalty: {result['violation_penalties']:.4f}")
        print(f"  Win Rate:       {result['win_rate']:.2%}")
    else:
        results = compute_all_scores()
        print(f"\n{'Agent':<25} {'Score':<10} {'Portfolio':<15} {'Return':<10} {'Drawdown':<10} {'Win Rate':<10}")
        print("-" * 80)
        for r in sorted(results, key=lambda x: x["score"], reverse=True):
            agent = r.get("agent_id", "?")
            print(f"{agent:<25} {r['score']:<+10.4f} ${r['ending_value']:<10,.2f} "
                  f"{r['total_return']:<+9.2%} {r['max_drawdown']:<9.2%} {r['win_rate']:<9.2%}")