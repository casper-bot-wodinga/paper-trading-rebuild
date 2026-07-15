import logging

log = logging.getLogger("auto_promote_prompts")
#!/usr/bin/env python3
"""
Auto-Promote Prompts — Nightly HEARTBEAT.md evolution pipeline.

After market close, this script:
  1. Queries today's executed trades per trader from PostgreSQL
  2. Calculates performance metrics (win rate, P&L, profit factor, avg conviction)
  3. Analyzes patterns: what worked (winning trades) vs what didn't (losing trades)
  4. Generates an evolved HEARTBEAT.md with adjusted strategy rules
  5. Deploys to each trader's OpenClaw workspace on .41
  6. Git-commits the prompt changes
  7. Logs the promotion to sweep_results

Usage:
    python3 scripts/auto_promote_prompts.py             # dry-run (print only)
    python3 scripts/auto_promote_prompts.py --apply     # actually deploy
    python3 scripts/auto_promote_prompts.py --trader kairos  # single trader
    python3 scripts/auto_promote_prompts.py --apply --force   # overwrite even if no changes

Design:
    The HEARTBEAT.md is the tick-time instruction the trader reads every 5 min.
    By evolving its rules nightly based on actual trade outcomes, the prompts
    adapt to market conditions and trader performance over time.

    What evolves:
    - stop_loss: Tighten if losses were big, widen if stopped out too early
    - max_positions: Adjust based on win rate (high WR = more positions)
    - sizing: Scale up on high-conviction winners, down on losses
    - ticker_universe: Add tickers that worked, remove those that didn't
    - entry_conditions: Add/remove filters based on pattern analysis
    - daily_loss_limit: Tighten if losses exceeded, loosen if conservative
    - trailing_stop: Adjust based on win hold times
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone, date as date_type
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
STATE_DIR = PROJECT_DIR / "state"
LOG_DIR = PROJECT_DIR / "logs"
TRADER_PROMPTS_DIR = PROJECT_DIR / "prompts" / "traders"

# OpenClaw workspace paths on the same machine
OPENCLAW_HOME = Path(os.getenv("OPENCLAW_HOME", "/home/openclaw"))
OPENCLAW_CONFIG = OPENCLAW_HOME / ".openclaw"

# PostgreSQL connection
PG_DSN = os.getenv(
    "PG_DSN",
    "host=trading-db port=5432 dbname=trading user=trader",
)

# ── Trader config ────────────────────────────────────────────────────────────
TRADERS = {
    "kairos": {
        "name": "Kairos",
        "personality": "Momentum + ML",
        "agent_id": "trader-kairos",
        "workspace": "workspace-trader-kairos",
        # Default strategy rules (fallback if no history)
        "defaults": {
            "stop_loss": 5.0,
            "trailing_stop": 8.0,
            "max_positions": 3,
            "daily_loss_limit": 3.0,
            "sizing": "2-5%",
            "entry_style": "momentum breakout + RSI oversold bounce + volume confirmation",
            "ticker_universe": "SPY, AAPL, NVDA, META, SOFI, PLTR, QQQ",
            "exit_style": "Stop loss 5%, trailing stop 8%, or signal reversal",
        },
        "heartbeat_template": """# {name} Heartbeat — {personality}

## Pre-Tick
1. Regime → `python3 scripts/regime_cron.py`
2. Momentum scan → `curl -s http://localhost:5000/momentum`
3. Quotes on top movers → `curl -s http://localhost:5000/quotes?symbols={ticker_universe}`
4. Check positions → `read positions/*.md`
5. ML signal → `curl -s http://localhost:5000/ml-signal?symbol=SPY`

## Tick Execution
6. Analyze: {entry_style}
7. If regime is bear/vol: smaller entries, tighter stops. If bull: standard.
8. Decide BUY/SELL/HOLD with JSON output (conviction, ticker, action, qty, price)
9. Execute → `python3 scripts/executor.py --account {trader} --action X --ticker Y --qty Z`
10. Update thesis → `positions/$TICKER.md`
11. Journal → append to `journal/$(date +%Y-%m-%d).md`

## Rules
- **Max positions:** {max_positions}
- **Stop loss:** {stop_loss}%
- **Trailing stop:** {trailing_stop}%
- **Daily loss limit:** {daily_loss_limit}% of equity
- **Sizing:** {sizing} of equity per trade
- **Exit:** {exit_style}

## Learning
- After each trade: log what signal triggered it and why it won/lost
- {learning_note}
- HEARTBEAT_OK
""",
    },
    "aldridge": {
        "name": "Aldridge",
        "personality": "Value / Fundamentals",
        "agent_id": "trader-aldridge",
        "workspace": "workspace-trader-aldridge",
        "defaults": {
            "stop_loss": 8.0,
            "trailing_stop": 12.0,
            "max_positions": 8,
            "daily_loss_limit": 3.0,
            "sizing": "3-8%",
            "entry_style": "P/E < 20, D/E < 1, div yield > 2%, dividend aristocrats",
            "ticker_universe": "JPM, KO, PEP, WMT, PG, JNJ, ABBV, HD, CVX",
            "exit_style": "Stop loss 8%, P/E > 30, or earnings miss",
        },
        "heartbeat_template": """# {name} Heartbeat — {personality}

## Pre-Tick
1. Regime → `python3 scripts/regime_cron.py` (bear = defensive, bull = deploy)
2. Screen dividend aristocrats → `curl -s http://localhost:5000/fundamentals?symbols={ticker_universe}`
3. Valuation data → `curl -s http://localhost:5000/valuation?symbols={ticker_universe}`
4. Check positions → `read positions/*.md`

## Tick Execution
5. Analyze: {entry_style}
6. Rebalance: if cash > 30% and market not in bear, deploy
7. If bear regime: defensive mode (reduce exposure, rotate to staples)
8. Decide BUY/SELL/HOLD with JSON output
9. Execute → `python3 scripts/executor.py --account {trader} --action X --ticker Y --qty Z`
10. Journal → append to `journal/$(date +%Y-%m-%d).md`

## Rules
- **Max positions:** {max_positions}
- **Stop loss:** {stop_loss}%
- **Trailing stop:** {trailing_stop}%
- **Daily loss limit:** {daily_loss_limit}% of equity
- **Sizing:** {sizing} of equity per trade
- **Exit:** {exit_style}
- **Hold style:** Buy & hold weeks, patience over speed

## Learning
- {learning_note}
- HEARTBEAT_OK
""",
    },
    "stonks": {
        "name": "Stonks",
        "personality": "Sentiment / Momentum",
        "agent_id": "trader-stonks",
        "workspace": "workspace-trader-stonks",
        "defaults": {
            "stop_loss": 10.0,
            "trailing_stop": 10.0,
            "max_positions": 5,
            "daily_loss_limit": 5.0,
            "sizing": "3-7%",
            "entry_style": "sentiment + volume spike alignment, fear & greed extremes",
            "ticker_universe": "SOFI, NVDA, PLTR, HOOD, MSTR, TSLA, RDDT",
            "exit_style": "Trailing stop 10%, sentiment reversal, or volume exhaustion",
        },
        "heartbeat_template": """# {name} Heartbeat — {personality}

## Pre-Tick
1. Regime → `python3 scripts/regime_cron.py` (vol_spike = opportunity!)
2. Sentiment scan → `curl -s http://localhost:5000/sentiment?symbols={ticker_universe}`
3. Momentum → `curl -s http://localhost:5000/momentum`
4. Fear & Greed → `curl -s http://localhost:5000/fear_greed`
5. Check positions → `read positions/*.md`

## Tick Execution
6. Analyze: {entry_style}
7. If sentiment + volume spike align: aggressive entry
8. If regime is vol_spike: look for reversals, be first to detect shifts
9. Decide BUY/SELL/HOLD with JSON output
10. Execute → `python3 scripts/executor.py --account {trader} --action X --ticker Y --qty Z`
11. Journal → append to `journal/$(date +%Y-%m-%d).md`

## Rules
- **Max positions:** {max_positions}
- **Stop loss:** {stop_loss}%
- **Trailing stop:** {trailing_stop}%
- **Daily loss limit:** {daily_loss_limit}% of equity
- **Sizing:** {sizing} of equity per trade
- **Exit:** {exit_style}
- **First to detect:** sentiment shifts, volume spikes, news catalysts

## Learning
- {learning_note}
- HEARTBEAT_OK
""",
    },
}

# ═══════════════════════════════════════════════════════════════════════════════
# Database Queries
# ═══════════════════════════════════════════════════════════════════════════════

def get_db_conn():
    """Get a PostgreSQL connection."""
    import psycopg2
    return psycopg2.connect(PG_DSN)

def query_trades(conn, agent_id: str, date_str: str) -> List[Dict[str, Any]]:
    """Query all executed trades for an agent on a given date."""
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                """SELECT ticker, action, shares, price, stop_loss, pnl,
                          entry_time, exit_time, status, rationale
                   FROM trading.executed_trades
                   WHERE agent_id = %s
                     AND entry_time::date = %s::date
                   ORDER BY entry_time""",
                (agent_id, date_str),
            )
            rows = cur.fetchall()
            return [
                {
                    "ticker": r[0] or "?",
                    "action": r[1] or "",
                    "shares": int(r[2] or 0),
                    "price": float(r[3] or 0),
                    "stop_loss": float(r[4] or 0) if r[4] else 0,
                    "pnl": float(r[5] or 0),
                    "entry_time": str(r[6]) if r[6] else "",
                    "exit_time": str(r[7]) if r[7] else "",
                    "status": r[8] or "unknown",
                    "rationale": r[9] or "",
                }
                for r in rows
            ]
        except Exception:
            return []
        finally:
            cur.close()
            conn.rollback()
    except Exception:
        conn.rollback()
    return []

def query_journal(conn, agent_id: str, date_str: str) -> List[Dict[str, Any]]:
    """Query journal entries for a trader on a given date."""
    try:
        cur = conn.cursor()
        try:
            # timestamp is a text column, so use LIKE prefix matching
            cur.execute(
                """SELECT timestamp, mood, entry, confidence
                   FROM trading.trader_journal
                   WHERE agent_id = %s
                     AND timestamp LIKE %s
                   ORDER BY timestamp""",
                (agent_id, f"{date_str}%"),
            )
            rows = cur.fetchall()
            return [
                {
                    "timestamp": str(r[0] or ""),
                    "mood": r[1] or "",
                    "entry": r[2] or "",
                    "confidence": float(r[3]) if r[3] else 0.5,
                }
                for r in rows
            ]
        except Exception:
            return []
        finally:
            cur.close()
            conn.rollback()
    except Exception:
        conn.rollback()
    return []

def query_decisions(conn, trader_id: str, date_str: str) -> List[Dict[str, Any]]:
    """Query decisions for a trader on a given date."""
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                """SELECT ticker, decision, conviction, rationale, created_at
                   FROM trading.decisions
                   WHERE trader_id = %s
                     AND created_at::date = %s::date
                   ORDER BY created_at""",
                (trader_id, date_str),
            )
            rows = cur.fetchall()
            return [
                {
                    "ticker": r[0] or "?",
                    "decision": r[1] or "",
                    "conviction": float(r[2] or 0),
                    "rationale": r[3] or "",
                    "created_at": str(r[4]) if r[4] else "",
                }
                for r in rows
            ]
        except Exception:
            return []
        finally:
            cur.close()
            conn.rollback()
    except Exception:
        conn.rollback()
    return []

def query_regime(conn) -> Dict[str, Any]:
    """Query the current market regime from the regimes table."""
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                """SELECT regime, confidence, date
                   FROM market_data.regimes
                   ORDER BY date DESC LIMIT 1"""
            )
            row = cur.fetchone()
            if row:
                return {
                    "regime": str(row[0] or "unknown"),
                    "label": str(row[0] or "unknown"),
                    "confidence": float(row[1] or 0),
                    "detected_at": str(row[2] or ""),
                }
        except Exception as e:
            log.warning("query_regime: %s", e)

        finally:
            cur.close()
            conn.rollback()  # Reset transaction state
    except Exception:
        conn.rollback()
    return {"regime": "unknown", "label": "unknown", "confidence": 0.0}

def query_historical_performance(
    conn, agent_id: str, days: int = 5
) -> Dict[str, float]:
    """Query historical performance metrics for the last N days."""
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                """SELECT
                      COUNT(*) as n_trades,
                      COALESCE(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 0) as win_rate,
                      COALESCE(SUM(pnl), 0) as total_pnl,
                      COALESCE(AVG(pnl), 0) as avg_pnl,
                      COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0) as avg_win,
                      COALESCE(AVG(CASE WHEN pnl < 0 THEN ABS(pnl) ELSE 0 END), 0) as avg_loss,
                      COUNT(*) FILTER (WHERE pnl > 0) as wins,
                      COUNT(*) FILTER (WHERE pnl < 0) as losses
                   FROM trading.executed_trades
                   WHERE agent_id = %s
                     AND entry_time >= NOW() - INTERVAL '%s days'
                     AND status = 'closed'""",
                (agent_id, str(days)),
            )
            row = cur.fetchone()
            if row and row[0] > 0:
                avg_loss = float(row[6] or 0.01)
                return {
                    "n_trades": int(row[0] or 0),
                    "win_rate": float(row[1] or 0),
                    "total_pnl": float(row[2] or 0),
                    "avg_pnl": float(row[3] or 0),
                    "avg_win": float(row[4] or 0),
                    "avg_loss": avg_loss,
                    "profit_factor": float(row[4] or 0) / avg_loss if avg_loss > 0 else 0,
                    "wins": int(row[7] or 0),
                    "losses": int(row[8] or 0),
                }
        except Exception as e:
            log.warning("operation: %s", e)

        finally:
            cur.close()
            conn.rollback()
    except Exception:
        conn.rollback()
    return {"n_trades": 0, "win_rate": 0, "total_pnl": 0, "avg_pnl": 0,
            "avg_win": 0, "avg_loss": 0, "profit_factor": 0, "wins": 0, "losses": 0}

# ═══════════════════════════════════════════════════════════════════════════════
# Trade Analysis
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_trades(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze a list of trades and return performance metrics + patterns."""
    if not trades:
        return {
            "n_trades": 0, "n_wins": 0, "n_losses": 0,
            "win_rate": 0.0, "total_pnl": 0.0, "avg_pnl": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "profit_factor": 0.0,
            "winning_tickers": [], "losing_tickers": [],
            "best_tickers": [], "worst_tickers": [],
            "loss_reasons": {}, "patterns": {},
        }

    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) <= 0]
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    win_rate = len(wins) / len(trades) if trades else 0
    avg_win = sum(t.get("pnl", 0) for t in wins) / len(wins) if wins else 0
    avg_loss = abs(sum(t.get("pnl", 0) for t in losses)) / len(losses) if losses else 0.01
    profit_factor = avg_win / avg_loss if avg_loss > 0 else 0

    # What tickers won/lost
    ticker_wins = defaultdict(list)
    ticker_losses = defaultdict(list)
    for t in wins:
        ticker_wins[t["ticker"]].append(t)
    for t in losses:
        ticker_losses[t["ticker"]].append(t)

    # Exit reasons on losses
    loss_reasons = defaultdict(int)
    for t in losses:
        reason = t.get("rationale", "unknown")[:50]
        loss_reasons[reason] += 1

    # Win tickers (sorted by frequency)
    winning_tickers = sorted(
        ticker_wins.keys(), key=lambda k: len(ticker_wins[k]), reverse=True
    )
    losing_tickers = sorted(
        ticker_losses.keys(), key=lambda k: len(ticker_losses[k]), reverse=True
    )

    # Best and worst tickers by P&L
    ticker_pnl = defaultdict(float)
    for t in trades:
        ticker_pnl[t["ticker"]] += t.get("pnl", 0)
    best_tickers = sorted(ticker_pnl.keys(), key=lambda k: ticker_pnl[k], reverse=True)[:5]
    worst_tickers = sorted(ticker_pnl.keys(), key=lambda k: ticker_pnl[k])[:3]

    return {
        "n_trades": len(trades),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "avg_pnl": total_pnl / len(trades) if trades else 0,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "winning_tickers": winning_tickers,
        "losing_tickers": losing_tickers,
        "best_tickers": best_tickers,
        "worst_tickers": worst_tickers,
        "loss_reasons": dict(loss_reasons.most_common(3)) if loss_reasons else {},
    }

# ═══════════════════════════════════════════════════════════════════════════════
# HEARTBEAT.md Evolution Engine
# ═══════════════════════════════════════════════════════════════════════════════

def evolve_rules(
    defaults: Dict[str, Any],
    today_analysis: Dict[str, Any],
    historical: Dict[str, Any],
    regime: Dict[str, Any],
    trader: str,
) -> Dict[str, Any]:
    """Evolve strategy rules based on today's performance.

    This is the core intelligence of the auto-promotion. It adjusts:
    - stop_loss based on actual loss sizes
    - max_positions based on win rate trend
    - sizing based on conviction vs outcome correlation
    - ticker_universe to favor winning tickers
    - entry_conditions based on what worked
    """
    rules = dict(defaults)
    label = regime.get("label", "unknown")

    # ── Stop loss evolution ───────────────────────────────────────────────
    if today_analysis["n_trades"] >= 3:
        avg_loss = today_analysis["avg_loss"]
        pnl_volatility = abs(today_analysis["avg_pnl"]) if today_analysis["avg_pnl"] != 0 else 0.5

        # If losses are big, tighten stop loss
        if avg_loss > 2.0 and defaults["stop_loss"] > 3.0:
            new_sl = max(round(defaults["stop_loss"] * 0.85, 1), 3.0)
            rules["stop_loss"] = new_sl
        # If stopped out too early (lots of small losses), widen stop
        elif today_analysis["avg_loss"] < 0.3 and today_analysis["win_rate"] < 0.3:
            new_sl = min(round(defaults["stop_loss"] * 1.15, 1), 15.0)
            rules["stop_loss"] = new_sl

    # ── Trailing stop evolution ───────────────────────────────────────────
    if today_analysis["n_trades"] >= 3:
        # If profit factor is good but trailing could capture more, adjust
        if today_analysis["profit_factor"] > 1.5 and today_analysis["avg_win"] > 1.0:
            # Keep trailing stop consistent with stop loss
            rules["trailing_stop"] = round(defaults["stop_loss"] * 1.5, 1)

    # ── Max positions evolution ───────────────────────────────────────────
    if historical["n_trades"] >= 5:
        his_wr = historical["win_rate"]
        if his_wr > 0.6:
            # High win rate → can handle more positions
            rules["max_positions"] = min(defaults["max_positions"] + 1, 10)
        elif his_wr < 0.3:
            # Low win rate → reduce exposure
            rules["max_positions"] = max(defaults["max_positions"] - 1, 1)

    # ── Daily loss limit evolution ────────────────────────────────────────
    if today_analysis["n_trades"] >= 3:
        if today_analysis["total_pnl"] < -5.0:
            # Had a bad day → tighten loss limit
            rules["daily_loss_limit"] = max(round(defaults["daily_loss_limit"] * 0.8, 1), 1.0)
        elif today_analysis["total_pnl"] > 5.0 and today_analysis["win_rate"] > 0.6:
            # Good day with high win rate → can loosen slightly
            rules["daily_loss_limit"] = min(round(defaults["daily_loss_limit"] * 1.1, 1), 10.0)

    # ── Ticker universe evolution ─────────────────────────────────────────
    winning_tickers = today_analysis.get("winning_tickers", [])
    losing_tickers = today_analysis.get("losing_tickers", [])
    current_tickers = [t.strip() for t in defaults["ticker_universe"].split(",")]

    if winning_tickers and len(winning_tickers) >= 2:
        # Add winning tickers that aren't already in the universe
        new_tickers = [t for t in winning_tickers if t not in current_tickers]
        if new_tickers:
            # Keep the most winning ones, max 2 additions
            for t in new_tickers[:2]:
                if t not in current_tickers:
                    current_tickers.append(t)

    if losing_tickers and len(losing_tickers) >= 2:
        # Remove persistently losing tickers (but keep core ones)
        core_tickers = {"SPY", "AAPL", "NVDA", "QQQ", "JPM", "KO", "PEP", "WMT"}
        to_remove = [
            t for t in losing_tickers[:2]
            if t in current_tickers and t not in core_tickers
        ]
        for t in to_remove:
            if t in current_tickers:
                current_tickers.remove(t)

    rules["ticker_universe"] = ", ".join(current_tickers)

    # ── Regime-specific adjustments ───────────────────────────────────────
    if label == "bear" or label == "momentum_bear":
        # Bear market → tighten everything
        rules["daily_loss_limit"] = min(rules["daily_loss_limit"], 2.0)
        rules["max_positions"] = max(rules["max_positions"] // 2, 1)
        if "defensive" not in rules.get("entry_style", ""):
            rules["entry_style"] = defaults["entry_style"] + ", defensive mode"
    elif label == "bull" or label == "momentum_bull":
        # Bull market → more aggressive
        if "aggressive" not in rules.get("entry_style", ""):
            rules["entry_style"] = defaults["entry_style"] + ", standard sizing"

    # ── Learning note (changes every day!) ────────────────────────────────
    if today_analysis["n_trades"] > 0:
        wr = today_analysis["win_rate"] * 100
        pnl = today_analysis["total_pnl"]
        if wr >= 60 and pnl > 0:
            rules["learning_note"] = (
                f"Today was strong ({wr:.0f}% win rate, ${pnl:.2f} P&L). "
                f"Keep following the current signals — they're working under {label} regime."
            )
        elif wr >= 40 and pnl > 0:
            rules["learning_note"] = (
                f"Today was decent ({wr:.0f}% win rate, ${pnl:.2f} P&L). "
                f"Some signals worked, others didn't. Focus on {', '.join(today_analysis.get('winning_tickers', [])[:3])}."
            )
        elif pnl <= 0:
            rules["learning_note"] = (
                f"Today was rough ({wr:.0f}% win rate, ${pnl:.2f} P&L). "
                f"Tighten stops ({rules['stop_loss']}%), reduce positions ({rules['max_positions']} max). "
                f"Losses mostly on: {', '.join(today_analysis.get('loss_reasons', {}).keys())[:100]}"
            )
        else:
            rules["learning_note"] = (
                f"Mixed day ({wr:.0f}% win rate, ${pnl:.2f} P&L). "
                f"Regime: {label}. Stay disciplined with current rules."
            )
    else:
        rules["learning_note"] = (
            f"No trades yet today. Regime: {label}. "
            f"Ready to deploy when signals align."
        )

    # Ensure trailing_stop >= stop_loss
    if rules["trailing_stop"] < rules["stop_loss"]:
        rules["trailing_stop"] = rules["stop_loss"] + 1.0

    return rules

def generate_heartbeat(
    trader_key: str, rules: Dict[str, Any], regime_label: str
) -> str:
    """Generate the full HEARTBEAT.md content from template and evolved rules."""
    config = TRADERS[trader_key]
    template = config["heartbeat_template"]

    # Build the template params
    params = {
        "name": config["name"],
        "personality": config["personality"],
        "trader": trader_key,
        "stop_loss": rules["stop_loss"],
        "trailing_stop": rules["trailing_stop"],
        "max_positions": int(rules["max_positions"]),
        "daily_loss_limit": rules["daily_loss_limit"],
        "sizing": rules["sizing"],
        "entry_style": rules.get("entry_style", config["defaults"]["entry_style"]),
        "exit_style": rules.get("exit_style", config["defaults"]["exit_style"]),
        "ticker_universe": rules.get("ticker_universe", config["defaults"]["ticker_universe"]),
        "learning_note": rules.get("learning_note", f"Regime: {regime_label}. Stay disciplined."),
    }

    return template.format(**params)

# ═══════════════════════════════════════════════════════════════════════════════
# Deployment
# ═══════════════════════════════════════════════════════════════════════════════

def deploy_heartbeat(trader_key: str, content: str, dry_run: bool = False) -> bool:
    """Deploy the HEARTBEAT.md to the OpenClaw workspace."""
    config = TRADERS[trader_key]
    workspace = OPENCLAW_CONFIG / config["workspace"]
    target = workspace / "HEARTBEAT.md"

    if dry_run:
        print(f"  [DRY-RUN] Would write to {target}")
        print(f"  [DRY-RUN] Content preview:\n{content[:500]}...\n")
        return True

    # Ensure workspace exists
    if not workspace.exists():
        print(f"  [WARN] Workspace not found: {workspace}")
        return False

    # Write the file
    try:
        target.write_text(content, encoding="utf-8")
        print(f"  ✅ Deployed to {target}")
        return True
    except Exception as e:
        print(f"  ❌ Failed to write {target}: {e}")
        return False

def git_commit(trader_key: str, summary: Dict[str, Any], dry_run: bool = False) -> bool:
    """Git-commit the prompt changes."""
    if dry_run:
        print(f"  [DRY-RUN] Would git commit changes for {trader_key}")
        return True

    try:
        date_str = datetime.now().strftime("%Y-%m-%d")
        msg = f"auto-promote: {trader_key} HEARTBEAT.md for {date_str}"
        if summary.get("metrics"):
            wr = summary["metrics"].get("win_rate", 0)
            pnl = summary["metrics"].get("total_pnl", 0)
            msg += f" — {wr*100:.0f}% WR, ${pnl:+.2f}"

        git_dir = str(OPENCLAW_CONFIG)
        result = subprocess.run(
            ["git", "-C", git_dir, "add", f"workspace-trader-{trader_key}/HEARTBEAT.md"],
            capture_output=True, text=True, timeout=30,
        )
        result2 = subprocess.run(
            ["git", "-C", git_dir, "commit", "-m", msg],
            capture_output=True, text=True, timeout=30,
        )
        if result2.returncode == 0:
            print(f"  ✅ Git committed: {msg}")
            return True
        elif "nothing to commit" in result2.stdout or "nothing to commit" in result2.stderr:
            print(f"  ℹ️  No changes to commit")
            return True
        else:
            print(f"  ⚠️  Git commit: {result2.stdout.strip()}")
            print(f"  ⚠️  Git stderr: {result2.stderr.strip()}")
            return False
    except Exception as e:
        print(f"  ❌ Git commit failed: {e}")
        return False

def log_promotion(trader_key: str, rules: Dict[str, Any], analysis: Dict[str, Any],
                  dry_run: bool = False) -> bool:
    """Log the promotion to a local JSON file."""
    if dry_run:
        print(f"  [DRY-RUN] Would log promotion")
        return True

    try:
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "trader": trader_key,
            "date": datetime.now().strftime("%Y-%m-%d"),
            "n_trades": analysis.get("n_trades", 0),
            "win_rate": analysis.get("win_rate", 0),
            "total_pnl": analysis.get("total_pnl", 0),
            "profit_factor": analysis.get("profit_factor", 0),
            "rules": {
                "stop_loss": rules.get("stop_loss"),
                "max_positions": rules.get("max_positions"),
                "trailing_stop": rules.get("trailing_stop"),
                "daily_loss_limit": rules.get("daily_loss_limit"),
            },
            "learning_note": rules.get("learning_note", ""),
        }

        log_file = STATE_DIR / "auto_promote_log.json"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        # Read existing log
        existing = []
        if log_file.exists():
            try:
                existing = json.loads(log_file.read_text())
            except (json.JSONDecodeError, Exception):
                existing = []

        # Append new entry
        existing.append(log_entry)

        # Keep last 100 entries
        if len(existing) > 100:
            existing = existing[-100:]

        log_file.write_text(json.dumps(existing, indent=2))
        print(f"  ✅ Promotion logged to {log_file}")
        return True
    except Exception as e:
        print(f"  ⚠️  Failed to log promotion: {e}")
        return False

# ═══════════════════════════════════════════════════════════════════════════════
# Main Orchestrator
# ═══════════════════════════════════════════════════════════════════════════════

def auto_promote(
    trader: Optional[str] = None,
    date_str: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    """Run the full auto-promotion pipeline.

    Args:
        trader: Specific trader short name, or None for all.
        date_str: Date to analyze (YYYY-MM-DD). Default: today.
        dry_run: If True, print what would happen without deploying.
        force: If True, deploy even if no trades today.

    Returns:
        Dict with results per trader.
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    else:
        # Validate date format
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            print(f"❌ Invalid date format: {date_str}. Use YYYY-MM-DD.")
            return {"error": f"Invalid date: {date_str}"}

    traders = [trader] if trader else list(TRADERS.keys())
    results = {}

    print(f"=== Auto-Promote Prompts: {date_str} ===\n")
    mode = "DRY-RUN" if dry_run else "LIVE"
    print(f"Mode: {mode} | Traders: {', '.join(traders)}\n")

    # Connect to DB
    try:
        conn = get_db_conn()
        conn.rollback()  # Clear any stale transaction state
    except Exception as e:
        print(f"❌ DB connection failed: {e}")
        return {"error": f"DB connection failed: {e}"}

    # Get today's regime
    regime = query_regime(conn)
    regime_label = regime.get("label", "unknown")
    print(f"Current regime: {regime_label} (confidence: {regime.get('confidence', 0):.1%})\n")

    for trader_key in traders:
        config = TRADERS[trader_key]
        agent_id = config["agent_id"]
        print(f"── {config['name']} ({config['personality']}) ──")

        # 1. Query today's data
        trades = query_trades(conn, agent_id, date_str)
        journal = query_journal(conn, agent_id, date_str)
        decisions = query_decisions(conn, trader_key, date_str)

        print(f"  Trades: {len(trades)} | Journal entries: {len(journal)} | Decisions: {len(decisions)}")

        # 2. Analyze today's trades
        today_analysis = analyze_trades(trades)
        print(f"  Win rate: {today_analysis['win_rate']:.1%} | P&L: ${today_analysis['total_pnl']:.2f} | "
              f"Profit factor: {today_analysis['profit_factor']:.2f}")

        # 3. Get historical performance (last 5 days)
        historical = query_historical_performance(conn, agent_id, days=5)
        print(f"  Historical (5d): {historical['n_trades']} trades, {historical['win_rate']:.1%} WR, "
              f"${historical['total_pnl']:.2f} P&L")

        # 4. Skip if no trades today and not forced
        if today_analysis["n_trades"] == 0 and not force:
            print(f"  ℹ️  No trades today — skipping (use --force to overwrite anyway)")
            print(f"  Keeping learning note: No trades yet today. Regime: {regime_label}.")
            results[trader_key] = {"status": "skipped", "reason": "no_trades"}
            continue

        # 5. Evolve the rules
        rules = evolve_rules(
            defaults=config["defaults"],
            today_analysis=today_analysis,
            historical=historical,
            regime=regime,
            trader=trader_key,
        )

        # 6. Print what changed
        print(f"  Evolved rules:")
        for key in ["stop_loss", "trailing_stop", "max_positions", "daily_loss_limit"]:
            old = config["defaults"].get(key)
            new = rules.get(key)
            if old != new:
                print(f"    {key}: {old} → {new}")
        old_tickers = set(t.strip() for t in config["defaults"]["ticker_universe"].split(","))
        new_tickers = set(t.strip() for t in rules["ticker_universe"].split(","))
        added = new_tickers - old_tickers
        removed = old_tickers - new_tickers
        if added:
            print(f"    ticker_universe: +{', '.join(added)}")
        if removed:
            print(f"    ticker_universe: -{', '.join(removed)}")
        print(f"    learning_note: {rules['learning_note'][:120]}...")

        # 7. Generate the HEARTBEAT.md content
        content = generate_heartbeat(trader_key, rules, regime_label)

        # 8. Deploy
        deployed = deploy_heartbeat(trader_key, content, dry_run=dry_run)

        # 9. Git commit
        if deployed:
            commit_result = git_commit(
                trader_key,
                {"metrics": today_analysis},
                dry_run=dry_run,
            )

        # 10. Log promotion
        if deployed:
            log_promotion(trader_key, rules, today_analysis, dry_run=dry_run)

        # Store result
        results[trader_key] = {
            "status": "deployed" if deployed else "failed",
            "n_trades": today_analysis["n_trades"],
            "win_rate": today_analysis["win_rate"],
            "total_pnl": today_analysis["total_pnl"],
            "rules": {k: rules.get(k) for k in ["stop_loss", "trailing_stop", "max_positions", "daily_loss_limit"]},
            "ticker_universe": rules["ticker_universe"],
            "learning_note": rules["learning_note"],
        }

        print()

    conn.close()

    # Summary
    print(f"=== Summary ===")
    n_deployed = sum(1 for r in results.values() if r.get("status") == "deployed")
    n_skipped = sum(1 for r in results.values() if r.get("status") == "skipped")
    print(f"Deployed: {n_deployed} | Skipped: {n_skipped} | "
          f"Total: {len(results)}")
    if dry_run:
        print(f"\nRun with --apply to actually deploy.")

    return results

# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Auto-Promote Prompts — nightly HEARTBEAT.md evolution pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                    # dry-run for all traders
  %(prog)s --apply                            # actually deploy
  %(prog)s --trader kairos                    # single trader, dry-run
  %(prog)s --apply --trader stonks             # single trader, deploy
  %(prog)s --apply --force                     # deploy even if no trades
  %(prog)s --date 2026-07-14 --apply           # specific date
        """,
    )
    parser.add_argument(
        "--trader", type=str, default=None,
        help="Trader short name (kairos, aldridge, stonks). Default: all.",
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Date to analyze (YYYY-MM-DD). Default: today.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually deploy the evolved prompts (default: dry-run).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Deploy even if no trades today (uses historical data).",
    )

    args = parser.parse_args()

    auto_promote(
        trader=args.trader,
        date_str=args.date,
        dry_run=not args.apply,
        force=args.force,
    )

if __name__ == "__main__":
    main()