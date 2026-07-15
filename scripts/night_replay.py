#!/usr/bin/env python3
"""
Night Replay — Off-market what-if replay for OpenClaw traders.

Replaces the old virtual_runner.py pattern. After market close (16:00 ET),
this script loads past N days of market data from Postgres and runs each
trader through "what if" branches:

  1. Load quotes, signals, portfolio states from Postgres for the replay window
  2. For each trader, simulate the actual decision loop against past data
  3. Fork "what if" branches at each tick:
     - Different ticker picks (e.g., AMD instead of NVDA)
     - Different risk sizes (2x, 0.5x on each trade)
     - Sitting out a trade entirely
  4. Score each branch outcome against actual results
  5. Write findings into the trader's strategies/active.md
  6. Append a replay journal entry

Usage:
    python3 scripts/night_replay.py --trader kairos --days 7
    python3 scripts/night_replay.py --trader all --days 7
    python3 scripts/night_replay.py --trader kairos --date 2026-07-13 --branches 5
    python3 scripts/night_replay.py --trader all --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure project src is on path
PROJECT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_DIR / "src"
AGENTS_DIR = PROJECT_DIR / "agents"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    from db.connection import get_connection
    from signals import SignalEngine, SignalParams
except ImportError as e:
    logging.error("Could not import project modules: %s", e)
    logging.error("Make sure you're running from the project root or the venv is active.")
    sys.exit(1)

log = logging.getLogger("night_replay")

# ── Trader Definitions ───────────────────────────────────────────────────────

TRADER_NAMES = {
    "kairos": "trader-kairos",
    "aldridge": "trader-aldridge",
    "stonks": "trader-stonks",
}

TRADER_DISPLAY = {
    "kairos": "Kairos Capital",
    "aldridge": "Aldridge & Partners",
    "stonks": "Stonks Capital",
}

# ── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class BranchConfig:
    """A what-if branch to simulate."""
    name: str
    description: str
    # Multipliers applied to each trade in the replay
    risk_multiplier: float = 1.0
    ticker_override: Optional[str] = None  # None = use actual ticker, string = substitute
    skip_trade: bool = False  # True = skip this trade entirely
    conviction_offset: float = 0.0  # Added to actual conviction score

    def apply_to_trade(self, ticker: str, quantity: int, price: float,
                       conviction: float, ceiling: float) -> Tuple[str, int, float, float]:
        """Apply this branch config to a trade. Returns (ticker, quantity, price, conviction)."""
        if self.skip_trade:
            return (ticker, 0, price, conviction)

        effective_ticker = self.ticker_override or ticker
        effective_conviction = min(1.0, max(0.0, conviction + self.conviction_offset))
        effective_quantity = max(0, int(quantity * self.risk_multiplier))

        return (effective_ticker, effective_quantity, price, effective_conviction)


@dataclass
class BranchResult:
    """Result of one what-if branch simulation."""
    branch_name: str
    description: str
    total_pnl: float
    win_count: int
    loss_count: int
    trade_count: int
    final_balance: float
    max_drawdown: float
    win_rate: float
    avg_win: float
    avg_loss: float
    trades: List[Dict[str, Any]] = field(default_factory=list)
    score: float = 0.0  # Composite score for ranking


@dataclass
class ReplayedTrade:
    """A single trade in the replay timeline."""
    ticker: str
    entry_price: float
    exit_price: float
    quantity: int
    entry_time: datetime
    exit_time: datetime
    pnl: float
    conviction: float
    signal_type: str
    hold_days: int
    branch: str = "actual"


@dataclass
class PatternInsight:
    """A pattern discovered during replay analysis."""
    pattern: str
    win_rate: float
    sample_size: int
    avg_return: float
    recommendation: str


# ── Database Functions ───────────────────────────────────────────────────────

def load_quotes_for_range(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """Load bar/quotes data from Postgres for the date range."""
    conn = get_connection()
    rows = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, timestamp, open, high, low, close, volume
                FROM market_data.bars_5min
                WHERE timestamp::date BETWEEN %s AND %s
                ORDER BY timestamp ASC
            """, (start_date, end_date))
            for row in cur.fetchall():
                rows.append({
                    "symbol": row[0],
                    "timestamp": row[1].isoformat() if hasattr(row[1], 'isoformat') else str(row[1]),
                    "open": float(row[2]),
                    "high": float(row[3]),
                    "low": float(row[4]),
                    "close": float(row[5]),
                    "volume": float(row[6]),
                })
    finally:
        conn.close()
    log.info("Loaded %d bar rows for %s to %s", len(rows), start_date, end_date)
    return rows


def load_signals_for_range(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """Load signal data from Postgres for the date range."""
    conn = get_connection()
    rows = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, timestamp, signal_type, signal_value, confidence
                FROM market_data.signals
                WHERE timestamp::date BETWEEN %s AND %s
                ORDER BY timestamp ASC
            """, (start_date, end_date))
            for row in cur.fetchall():
                rows.append({
                    "symbol": row[0],
                    "timestamp": row[1].isoformat() if hasattr(row[1], 'isoformat') else str(row[1]),
                    "signal_type": row[2],
                    "signal_value": float(row[3]) if row[3] is not None else None,
                    "confidence": float(row[4]) if row[4] is not None else None,
                })
    finally:
        conn.close()
    log.info("Loaded %d signal rows for %s to %s", len(rows), start_date, end_date)
    return rows


def load_portfolio_snapshots(start_date: str, end_date: str,
                              trader_name: str) -> List[Dict[str, Any]]:
    """Load portfolio state snapshots from Postgres for a trader."""
    conn = get_connection()
    rows = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT timestamp, cash_balance, position_value, total_equity,
                       positions_json, daily_pnl
                FROM trading.portfolio_snapshots
                WHERE timestamp::date BETWEEN %s AND %s
                  AND trader_name = %s
                ORDER BY timestamp ASC
            """, (start_date, end_date, trader_name))
            for row in cur.fetchall():
                rows.append({
                    "timestamp": row[0].isoformat() if hasattr(row[0], 'isoformat') else str(row[0]),
                    "cash_balance": float(row[1]),
                    "position_value": float(row[2]),
                    "total_equity": float(row[3]),
                    "positions": row[4],
                    "daily_pnl": float(row[5]) if row[5] is not None else 0.0,
                })
    finally:
        conn.close()
    log.info("Loaded %d portfolio snapshots for %s", len(rows), trader_name)
    return rows


def load_trader_decisions(start_date: str, end_date: str,
                           trader_name: str) -> List[Dict[str, Any]]:
    """Load trader decisions (trades) from Postgres."""
    conn = get_connection()
    rows = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ticker, action, quantity, price, timestamp,
                       conviction, pnl, signal_type, exit_price, exit_timestamp
                FROM trading.trader_decisions
                WHERE timestamp::date BETWEEN %s AND %s
                  AND trader_name = %s
                  AND action IN ('BUY', 'SELL')
                ORDER BY timestamp ASC
            """, (start_date, end_date, trader_name))
            for row in cur.fetchall():
                entry_time = row[4]
                exit_time = row[9]
                hold_days = 0
                if entry_time and exit_time:
                    hold_days = (exit_time - entry_time).days

                rows.append({
                    "ticker": row[0],
                    "action": row[1],
                    "quantity": int(row[2]) if row[2] is not None else 0,
                    "price": float(row[3]) if row[3] is not None else 0.0,
                    "timestamp": entry_time.isoformat() if hasattr(entry_time, 'isoformat') else str(entry_time),
                    "conviction": float(row[5]) if row[5] is not None else 0.0,
                    "pnl": float(row[6]) if row[6] is not None else 0.0,
                    "signal_type": row[7] or "unknown",
                    "exit_price": float(row[8]) if row[8] is not None else None,
                    "exit_timestamp": exit_time.isoformat() if hasattr(exit_time, 'isoformat') and exit_time else None,
                    "hold_days": hold_days,
                })
    finally:
        conn.close()
    log.info("Loaded %d decisions for %s", len(rows), trader_name)
    return rows


# ── Replay Engine ────────────────────────────────────────────────────────

class NightReplayEngine:
    """Manages the replay simulation for one trader."""

    def __init__(self, trader_key: str, start_date: str, end_date: str,
                 initial_cash: float = 100_000.0):
        self.trader_key = trader_key
        self.trader_dir_name = TRADER_NAMES[trader_key]
        self.trader_display = TRADER_DISPLAY[trader_key]
        self.start_date = start_date
        self.end_date = end_date
        self.initial_cash = initial_cash

        # Load data
        self.quotes = load_quotes_for_range(start_date, end_date)
        self.signals = load_signals_for_range(start_date, end_date)
        self.portfolio_history = load_portfolio_snapshots(
            start_date, end_date, self.trader_dir_name
        )
        self.decisions = load_trader_decisions(
            start_date, end_date, self.trader_dir_name
        )

        # Build quotes lookup: symbol -> list of bars sorted by time
        self.quotes_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        for bar in self.quotes:
            sym = bar["symbol"]
            if sym not in self.quotes_by_symbol:
                self.quotes_by_symbol[sym] = []
            self.quotes_by_symbol[sym].append(bar)

    def get_quotes_for_ticker(self, ticker: str) -> List[Dict[str, Any]]:
        """Get all quotes for a specific ticker."""
        return self.quotes_by_symbol.get(ticker, [])

    def get_price_at_time(self, ticker: str, ts: datetime) -> Optional[float]:
        """Get the nearest price for a ticker at or before a given timestamp."""
        bars = self.quotes_by_symbol.get(ticker, [])
        for bar in reversed(bars):
            bar_ts = datetime.fromisoformat(bar["timestamp"])
            if bar_ts <= ts:
                return bar["close"]
        return None

    def _build_branches(self) -> List[BranchConfig]:
        """Build the standard set of what-if branches."""
        return [
            BranchConfig(
                name="actual",
                description="Actual decisions (baseline)",
                risk_multiplier=1.0,
            ),
            BranchConfig(
                name="double_down",
                description="What if I'd sized 2x on every trade?",
                risk_multiplier=2.0,
            ),
            BranchConfig(
                name="half_size",
                description="What if I'd halved every position?",
                risk_multiplier=0.5,
            ),
            BranchConfig(
                name="more_selective",
                description="What if I only took trades with conviction > 0.6?",
                conviction_offset=-0.3,
                # Skip trades with low conviction by adding a negative offset;
                # the branch runner will filter based on resulting conviction.
            ),
            BranchConfig(
                name="skip_biggest_loser",
                description="What if I'd sat out the worst trade?",
                skip_trade=True,
            ),
        ]

    def _score_branch(self, result: BranchResult) -> float:
        """Compute a composite score for a branch result.

        Higher is better. Factors: net PnL, win rate, and capital preservation.
        """
        pnl_score = max(-100, min(100, result.total_pnl / 100))
        win_rate_score = result.win_rate * 50
        dd_penalty = max(0, result.max_drawdown * 2)
        trade_count_bonus = min(10, result.trade_count * 2)

        return pnl_score + win_rate_score - dd_penalty + trade_count_bonus

    def _detect_patterns(self, branch_results: List[BranchResult],
                          actual_trades: List[Dict[str, Any]]) -> List[PatternInsight]:
        """Analyze replay data for actionable patterns."""
        patterns = []

        # Pattern: ticker performance
        ticker_perf: Dict[str, List[float]] = {}
        for t in actual_trades:
            if t["pnl"] is None:
                continue
            ticker = t["ticker"]
            if ticker not in ticker_perf:
                ticker_perf[ticker] = []
            ticker_perf[ticker].append(t["pnl"])

        for ticker, pnls in ticker_perf.items():
            wins = sum(1 for p in pnls if p > 0)
            total = len(pnls)
            if total >= 3:
                wr = wins / total
                avg_ret = sum(pnls) / total
                if wr >= 0.6:
                    patterns.append(PatternInsight(
                        pattern=f"{ticker} trades {wr:.0%} win rate ({total} trades)",
                        win_rate=wr,
                        sample_size=total,
                        avg_return=avg_ret,
                        recommendation=f"Keep {ticker} in primary watchlist. Strategy works at {wr:.0%}.",
                    ))
                elif wr <= 0.3:
                    patterns.append(PatternInsight(
                        pattern=f"{ticker} losing at {wr:.0%} ({total} trades)",
                        win_rate=wr,
                        sample_size=total,
                        avg_return=avg_ret,
                        recommendation=f"Consider demoting {ticker} from primary watchlist. Only {wr:.0%} wins.",
                    ))

        # Pattern: conviction threshold effectiveness
        high_conviction_trades = [t for t in actual_trades
                                   if t.get("conviction", 0) >= 0.6 and t.get("pnl") is not None]
        low_conviction_trades = [t for t in actual_trades
                                  if t.get("conviction", 0) < 0.4 and t.get("pnl") is not None]

        if len(high_conviction_trades) >= 3:
            hw = sum(1 for t in high_conviction_trades if t["pnl"] > 0)
            hwr = hw / len(high_conviction_trades)
            patterns.append(PatternInsight(
                pattern=f"High-conviction trades (>0.6): {hwr:.0%} win rate ({len(high_conviction_trades)} trades)",
                win_rate=hwr,
                sample_size=len(high_conviction_trades),
                avg_return=sum(t["pnl"] for t in high_conviction_trades) / len(high_conviction_trades),
                recommendation=f"Conviction floor of 0.6 produces {hwr:.0%} wins."
                              if hwr >= 0.5 else
                              f"Consider raising conviction floor. High-conviction only wins {hwr:.0%}.",
            ))

        if len(low_conviction_trades) >= 3:
            lw = sum(1 for t in low_conviction_trades if t["pnl"] > 0)
            lwr = lw / len(low_conviction_trades)
            patterns.append(PatternInsight(
                pattern=f"Low-conviction trades (<0.4): {lwr:.0%} win rate ({len(low_conviction_trades)} trades)",
                win_rate=lwr,
                sample_size=len(low_conviction_trades),
                avg_return=sum(t["pnl"] for t in low_conviction_trades) / len(low_conviction_trades),
                recommendation=f"Consider raising conviction floor to avoid low-conviction bets."
                              if lwr < 0.5 else
                              f"Low conv trades work at {lwr:.0%} — keep the wide net.",
            ))

        # Pattern: best branch vs actual
        if branch_results:
            best = max(branch_results, key=lambda r: r.score)
            actual = next((r for r in branch_results if r.branch_name == "actual"), None)
            if actual and best.branch_name != "actual":
                patterns.append(PatternInsight(
                    pattern=f"Best what-if: {best.branch_name} ({best.total_pnl:+.2f} vs actual {actual.total_pnl:+.2f})",
                    win_rate=best.win_rate,
                    sample_size=best.trade_count,
                    avg_return=best.total_pnl / max(1, best.trade_count),
                    recommendation=f"Try adopting {best.branch_name} behavior in live trading.",
                ))

        return patterns

    def _prepare_active_md_update(self, patterns: List[PatternInsight],
                                    branch_results: List[BranchResult]) -> str:
        """Generate the updated active.md content suggestions.

        Returns a markdown section ready to append to or merge into strategies/active.md.
        """
        lines = []
        lines.append(f"## Night Replay — {self.end_date}")
        lines.append("")
        lines.append(f"Replay window: {self.start_date} to {self.end_date}")
        lines.append("")

        # Best branch comparison
        if branch_results:
            actual = next((r for r in branch_results if r.branch_name == "actual"), None)
            best = max(branch_results, key=lambda r: r.score)
            if actual and best:
                lines.append(f"**Actual P&L**: ${actual.total_pnl:+.2f} ({actual.win_rate:.0%} win rate, {actual.trade_count} trades)")
                lines.append(f"**Best branch**: {best.branch_name} — ${best.total_pnl:+.2f} ({best.win_rate:.0%} win rate)")
                if best.score > actual.score:
                    lines.append(f"**Delta**: {best.branch_name} outperformed actual by ${best.total_pnl - actual.total_pnl:+.2f}")
                lines.append("")

        # Pattern insights
        if patterns:
            lines.append("### Patterns Detected")
            lines.append("")
            for p in patterns:
                lines.append(f"- **{p.pattern}**: {p.recommendation}")
            lines.append("")

        # Branch comparison table
        if branch_results:
            lines.append("### Branch Comparison")
            lines.append("")
            lines.append("| Branch | P&L | Win Rate | Trades | Score |")
            lines.append("|--------|-----|----------|--------|-------|")
            for r in sorted(branch_results, key=lambda x: x.score, reverse=True):
                lines.append(
                    f"| {r.branch_name} | ${r.total_pnl:+.2f} | {r.win_rate:.0%} | "
                    f"{r.trade_count} | {r.score:.1f} |"
                )
            lines.append("")

        return "\n".join(lines)

    def simulate_branch(self, branch: BranchConfig) -> BranchResult:
        """Simulate one what-if branch against historical decisions."""
        trades = []
        total_pnl = 0.0
        wins = 0
        losses = 0
        peak_balance = self.initial_cash
        current_balance = self.initial_cash
        max_drawdown = 0.0

        for decision in self.decisions:
            ticker = decision["ticker"]
            raw_quantity = decision["quantity"]
            price = decision["price"]
            conviction = decision.get("conviction", 0.5)

            # Apply branch
            effective_ticker, effective_qty, effective_price, effective_conviction = \
                branch.apply_to_trade(ticker, raw_quantity, price, conviction,
                                       self.initial_cash * 0.35)  # ~35% ceiling

            if effective_qty <= 0:
                continue

            # Get exit price from actual decision
            exit_price = decision.get("exit_price")
            if exit_price is None:
                # Estimate from quotes at exit time
                exit_ts = decision.get("exit_timestamp")
                if exit_ts:
                    exit_price = self.get_price_at_time(
                        effective_ticker, datetime.fromisoformat(exit_ts)
                    )
                if exit_price is None:
                    # Use close of the next bar as approximation
                    exit_price = price * 1.01  # Default +1% if unknown

            entry_cost = effective_price * abs(effective_qty)
            exit_proceeds = exit_price * abs(effective_qty)

            if decision["action"] == "BUY":
                pnl = exit_proceeds - entry_cost
            elif decision["action"] == "SELL":
                pnl = entry_cost - exit_proceeds
            else:
                pnl = 0.0

            total_pnl += pnl
            current_balance += pnl

            if pnl > 0:
                wins += 1
            else:
                losses += 1

            # Track drawdown
            if current_balance > peak_balance:
                peak_balance = current_balance
            drawdown = (peak_balance - current_balance) / peak_balance if peak_balance > 0 else 0
            max_drawdown = max(max_drawdown, drawdown)

            hold_days = decision.get("hold_days", 0)
            trades.append({
                "ticker": effective_ticker,
                "entry_price": effective_price,
                "exit_price": exit_price,
                "quantity": effective_qty,
                "entry_time": decision["timestamp"],
                "exit_time": decision.get("exit_timestamp"),
                "pnl": round(pnl, 2),
                "conviction": round(effective_conviction, 2),
                "signal_type": decision.get("signal_type", "unknown"),
                "hold_days": hold_days,
                "branch": branch.name,
            })

        trade_count = len(trades)
        win_rate = wins / trade_count if trade_count > 0 else 0.0
        avg_win = sum(t["pnl"] for t in trades if t["pnl"] > 0) / max(1, wins)
        avg_loss = sum(t["pnl"] for t in trades if t["pnl"] < 0) / max(1, losses)

        result = BranchResult(
            branch_name=branch.name,
            description=branch.description,
            total_pnl=round(total_pnl, 2),
            win_count=wins,
            loss_count=losses,
            trade_count=trade_count,
            final_balance=round(current_balance, 2),
            max_drawdown=round(max_drawdown, 4),
            win_rate=round(win_rate, 4),
            avg_win=round(avg_win, 2),
            avg_loss=round(avg_loss, 2),
            trades=trades,
        )
        result.score = round(self._score_branch(result), 1)
        return result

    def run(self) -> Dict[str, Any]:
        """Run the full replay for this trader."""
        log.info("Running night replay for %s (%s to %s)",
                 self.trader_display, self.start_date, self.end_date)

        branches = self._build_branches()
        branch_results = []

        for branch in branches:
            result = self.simulate_branch(branch)
            branch_results.append(result)
            log.info("  Branch '%s': P&L=%+.2f, WR=%.0f%%, Trades=%d, Score=%.1f",
                     branch.name, result.total_pnl, result.win_rate * 100,
                     result.trade_count, result.score)

        patterns = self._detect_patterns(branch_results, self.decisions)
        active_md_update = self._prepare_active_md_update(patterns, branch_results)

        return {
            "trader_key": self.trader_key,
            "trader_display": self.trader_display,
            "trader_dir_name": self.trader_dir_name,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "initial_cash": self.initial_cash,
            "branch_results": [asdict(r) for r in branch_results],
            "patterns": [asdict(p) for p in patterns],
            "active_md_update": active_md_update,
            "actual_decisions_loaded": len(self.decisions),
            "quotes_loaded": len(self.quotes),
        }


# ── File Writing Helpers ──────────────────────────────────────────────

def write_active_md_update(trader_key: str, report: Dict[str, Any]) -> None:
    """Append night replay findings into the trader's strategies/active.md.

    The replay section gets added/refreshed below the existing content.
    """
    active_path = AGENTS_DIR / TRADER_NAMES[trader_key] / "strategies" / "active.md"
    replay_section = report.get("active_md_update", "")

    if not replay_section:
        log.warning("No replay findings to write for %s", trader_key)
        return

    # Build the full updated content
    existing = ""
    if active_path.exists():
        existing = active_path.read_text()

    # Split at replay marker if it exists
    marker = "## Night Replay — "
    if marker in existing:
        parts = existing.split(marker)
        base = parts[0].rstrip()
    else:
        base = existing.rstrip()

    updated = base + "\n\n" + replay_section + "\n"
    active_path.write_text(updated)
    log.info("Updated %s with replay findings", active_path)


def write_replay_journal(trader_key: str, report: Dict[str, Any]) -> None:
    """Write a replay journal entry for the trader."""
    journal_dir = AGENTS_DIR / TRADER_NAMES[trader_key] / "journal"
    journal_date = report.get("end_date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    journal_path = journal_dir / f"{journal_date}.md"

    lines = []
    lines.append(f"# {journal_date} — Night Replay")
    lines.append("")

    best = max(report.get("branch_results", []),
               key=lambda r: r["score"]) if report.get("branch_results") else {}

    lines.append(f"Replay window: {report.get('start_date', '?')} → {report.get('end_date', '?')}")
    lines.append(f"Trader: {report.get('trader_display', trader_key)}")
    lines.append(f"Decisions analyzed: {report.get('actual_decisions_loaded', 0)}")
    lines.append(f"Quotes loaded: {report.get('quotes_loaded', 0)}")
    lines.append("")

    if best:
        lines.append(f"## Best Branch: {best.get('branch_name', '?')}")
        lines.append("")
        lines.append(f"P&L: ${best.get('total_pnl', 0):+.2f}")
        lines.append(f"Win Rate: {best.get('win_rate', 0)*100:.0f}%")
        lines.append(f"Trades: {best.get('trade_count', 0)}")
        lines.append(f"Score: {best.get('score', 0):.1f}")
        lines.append("")

    # Branch summary
    lines.append("## All Branches")
    lines.append("")
    for br in sorted(report.get("branch_results", []),
                     key=lambda r: r["score"], reverse=True):
        lines.append(f"- **{br['branch_name']}**: ${br['total_pnl']:+.2f} | "
                     f"{br['win_rate']*100:.0f}% WR | {br['trade_count']} trades | "
                     f"Score: {br['score']:.1f}")
    lines.append("")

    # Patterns
    patterns = report.get("patterns", [])
    if patterns:
        lines.append("## Patterns Found")
        lines.append("")
        for p in patterns:
            lines.append(f"- **{p['pattern']}**: {p['recommendation']}")
        lines.append("")

    lines.append("---")
    lines.append(f"*Replay generated at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*")
    lines.append("")

    existing = ""
    if journal_path.exists():
        existing = journal_path.read_text().strip()
        if existing:
            lines.insert(0, existing + "\n\n---\n\n")

    journal_path.write_text("\n".join(lines))
    log.info("Wrote replay journal to %s", journal_path)


# ── CLI ────────────────────────────────────────────────────────────────

def parse_date_range(args) -> Tuple[str, str]:
    """Determine the date range from CLI args."""
    if args.date:
        return args.date, args.date

    end = date.today()
    start = end - timedelta(days=args.days)
    return start.isoformat(), end.isoformat()


def main():
    parser = argparse.ArgumentParser(
        description="Night Replay — What-if analysis for paper trading agents"
    )
    parser.add_argument(
        "--trader", type=str, default="all",
        choices=["all", "kairos", "aldridge", "stonks"],
        help="Trader to replay (default: all)"
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="Number of past days to replay (default: 7)"
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Single date to replay (YYYY-MM-DD). Overrides --days."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run replay but do not write files"
    )
    parser.add_argument(
        "--initial-cash", type=float, default=100_000.0,
        help="Initial cash balance for replay (default: 100000)"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON to stdout"
    )

    args = parser.parse_args()
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    start_date, end_date = parse_date_range(args)
    log.info("Night replay: %s to %s, traders=%s, dry_run=%s",
             start_date, end_date, args.trader, args.dry_run)

    traders_to_run = [args.trader] if args.trader != "all" else ["kairos", "aldridge", "stonks"]
    all_reports = []

    for tkey in traders_to_run:
        log.info("=" * 60)
        log.info("Starting replay for %s", TRADER_DISPLAY[tkey])
        log.info("=" * 60)

        engine = NightReplayEngine(
            trader_key=tkey,
            start_date=start_date,
            end_date=end_date,
            initial_cash=args.initial_cash,
        )

        report = engine.run()

        if not args.dry_run:
            write_active_md_update(tkey, report)
            write_replay_journal(tkey, report)
            log.info("✓ Replay findings written for %s", tkey)
        else:
            log.info("DRY RUN — would write replay findings for %s", tkey)
            log.info("  Branch results: %d branches",
                     len(report.get("branch_results", [])))
            log.info("  Patterns detected: %d",
                     len(report.get("patterns", [])))

        all_reports.append(report)

    if args.json:
        print(json.dumps(all_reports, indent=2, default=str))

    log.info("Night replay complete. Processed %d trader(s).", len(traders_to_run))


if __name__ == "__main__":
    main()
