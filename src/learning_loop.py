#!/usr/bin/env python3
"""
Unified Learning Loop — trade grading, pattern analysis, param optimization,
and end-to-end loop orchestration for the Postgres-based rebuild.

Ported from paper-trading-teams/src/learning_loop.py.
Key changes from the legacy version:
  - Replaces SQLite/YAML with Postgres (psycopg2 via src.db.connection)
  - Replaces paper-trading-agents repo YAML patch writing with DB persistence
  - Config loaded from trading.virtual_traders (JSONB) instead of filesystem YAML
  - Optimization proposals stored in trading.param_history

Architecture
  grade_trade(trade, market_data, timestamp) → Grade
  analyze_patterns(trades)                   → List[PatternInsight]
  optimize_params(strategy, trades)          → ParamOptimization
  run_loop(agent_id, since_date)             → LearningLoopResult

All scoring functions are pure — they accept optional ``timestamp`` for
harness compatibility. DB I/O is isolated to config loading and result
persistence.

Usage:
    from src.learning_loop import grade_trade, analyze_patterns, optimize_params, run_loop

    grade = grade_trade(trade, market_data)
    insights = analyze_patterns(graded_trades)
    proposal = optimize_params("kairos", graded_trades)
    result = run_loop("trader-kairos", trades=[...])
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.db.connection import get_connection

log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

_CATEGORY_WEIGHTS = {
    "entry_timing": 0.30,
    "exit_timing": 0.30,
    "risk_management": 0.25,
    "conviction": 0.15,
}

_GRADE_THRESHOLDS = {
    "A": 90,
    "B": 75,
    "C": 60,
    "D": 40,
}

_DEFAULT_SCORE = 50.0  # mid-range baseline when data is sparse

# Default DB DSN for Postgres (docker.klo:5433). Override with env var.
_DEFAULT_DSN = "host=192.168.1.179 port=5433 dbname=trading user=trader"


# ═══════════════════════════════════════════════════════════════════════════
# Dataclasses
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class Grade:
    """Complete trade grade with per-category scores."""
    trade_id: str
    agent_id: str
    ticker: str
    action: str
    timestamp: str = ""
    # Per-category scores (0–100)
    entry_timing: float = 0.0
    exit_timing: float = 0.0
    risk_management: float = 0.0
    conviction: float = 0.0
    # Composite
    total_score: float = 0.0
    grade_letter: str = "F"
    # Metadata
    details: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.total_score == 0.0 and any(
            getattr(self, k) > 0 for k in _CATEGORY_WEIGHTS
        ):
            self.total_score = self._compute_total()
        if self.grade_letter == "F" and self.total_score > 0:
            self.grade_letter = self._compute_letter()

    def _compute_total(self) -> float:
        return sum(
            getattr(self, cat, 0.0) * weight
            for cat, weight in _CATEGORY_WEIGHTS.items()
        )

    def _compute_letter(self) -> str:
        for letter, threshold in _GRADE_THRESHOLDS.items():
            if self.total_score >= threshold:
                return letter
        return "F"

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "agent_id": self.agent_id,
            "ticker": self.ticker,
            "action": self.action,
            "timestamp": self.timestamp,
            "entry_timing": round(self.entry_timing, 1),
            "exit_timing": round(self.exit_timing, 1),
            "risk_management": round(self.risk_management, 1),
            "conviction": round(self.conviction, 1),
            "total_score": round(self.total_score, 1),
            "grade_letter": self.grade_letter,
            "details": self.details,
        }

    @property
    def is_win(self) -> bool:
        return self.total_score >= 60

    @property
    def category_scores(self) -> Dict[str, float]:
        return {k: getattr(self, k, 0.0) for k in _CATEGORY_WEIGHTS}


@dataclass
class PatternInsight:
    """A single pattern found in trade analysis."""
    pattern_type: str              # "winning_pattern", "losing_pattern", "drift"
    description: str
    category: str                  # which scoring category it relates to
    confidence: float = 0.5        # 0–1
    affected_trades: List[str] = field(default_factory=list)
    recommendation: str = ""
    data_snippet: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "pattern_type": self.pattern_type,
            "description": self.description,
            "category": self.category,
            "confidence": round(self.confidence, 2),
            "affected_trades": self.affected_trades,
            "recommendation": self.recommendation,
            "data_snippet": self.data_snippet,
        }


@dataclass
class ParamChange:
    """A single parameter change proposal."""
    param_path: str                # dotted path e.g. "risk.max_position_pct"
    current_value: Any
    proposed_value: Any
    justification: str
    confidence: float = 0.5
    source_insight: str = ""       # which pattern drove this change

    def to_dict(self) -> dict:
        return {
            "param_path": self.param_path,
            "current_value": self.current_value,
            "proposed_value": self.proposed_value,
            "justification": self.justification,
            "confidence": round(self.confidence, 2),
            "source_insight": self.source_insight,
        }


@dataclass
class ParamOptimization:
    """Complete param optimization result."""
    strategy: str
    timestamp: str = ""
    current_config: Dict[str, Any] = field(default_factory=dict)
    changes: List[ParamChange] = field(default_factory=list)
    summary: str = ""
    db_record_ids: List[int] = field(default_factory=list)  # IDs in trading.param_history

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "timestamp": self.timestamp,
            "num_changes": len(self.changes),
            "changes": [c.to_dict() for c in self.changes],
            "summary": self.summary,
            "db_record_ids": self.db_record_ids,
        }


@dataclass
class LearningLoopResult:
    """Complete result of one learning loop run."""
    agent_id: str
    timestamp: str = ""
    grades: List[Grade] = field(default_factory=list)
    avg_score: float = 0.0
    grade_trend: str = "flat"
    insights: List[PatternInsight] = field(default_factory=list)
    optimization: Optional[ParamOptimization] = None
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "timestamp": self.timestamp,
            "grades": [g.to_dict() for g in self.grades],
            "avg_score": round(self.avg_score, 1),
            "grade_trend": self.grade_trend,
            "insights": [i.to_dict() for i in self.insights],
            "optimization": self.optimization.to_dict() if self.optimization else None,
            "errors": self.errors,
        }

    def report(self) -> str:
        lines = [
            f"## Learning Loop — {self.agent_id}",
            f"**{self.timestamp}**",
            "",
            f"### Grades ({len(self.grades)} trades)",
            f"Average: **{self.avg_score:.0f}/100** ({self.grade_trend})",
        ]
        if self.grades:
            for cat in _CATEGORY_WEIGHTS:
                vals = [getattr(g, cat, 0) for g in self.grades]
                avg = sum(vals) / len(vals) if vals else 0
                icon = "✓" if avg >= 60 else "⚠️" if avg >= 40 else "❌"
                lines.append(f"- {icon} **{cat}**: {avg:.0f}/100")

        if self.insights:
            lines.append("")
            lines.append("### Patterns")
            for i in self.insights[:5]:
                lines.append(f"- [{i.pattern_type}] {i.description[:80]}")

        if self.optimization and self.optimization.changes:
            lines.append("")
            lines.append("### Proposed Changes")
            for c in self.optimization.changes:
                direction = "↑" if c.proposed_value > c.current_value else "↓"
                lines.append(f"- {direction} `{c.param_path}`: {c.current_value} → {c.proposed_value}")

        if self.errors:
            lines.append("")
            lines.append("### Errors")
            for e in self.errors:
                lines.append(f"- ❌ {e}")

        return "\n".join(lines)

    def save_to_db(self) -> Optional[int]:
        """Persist this loop run to the trading.param_history table.

        Writes each proposed change as a separate row. Returns the ID of
        the first record written, or None if no changes were proposed.
        Returns -1 if DB write fails.
        """
        if not self.optimization or not self.optimization.changes:
            return None

        try:
            conn = get_connection()
            cur = conn.cursor()
            first_id = None
            trader_id = _agent_to_strategy(self.agent_id)

            for change in self.optimization.changes:
                cur.execute(
                    """INSERT INTO trading.param_history
                       (agent_id, param_name, old_value, new_value,
                        before_score, after_score, changed_at,
                        source, reason, trader_id, score_metric)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING id""",
                    (
                        self.agent_id,
                        change.param_path,
                        float(change.current_value) if change.current_value is not None else None,
                        float(change.proposed_value) if change.proposed_value is not None else None,
                        float(self.avg_score) if self.avg_score else None,
                        None,  # after_score — filled later on re-evaluation
                        datetime.now(),
                        "learning_loop",
                        change.justification[:500],
                        trader_id,
                        "calmar",
                    ),
                )
                row = cur.fetchone()
                if row is not None:
                    record_id = row[0]
                    if first_id is None:
                        first_id = record_id
                    if self.optimization.db_record_ids is not None:
                        self.optimization.db_record_ids.append(record_id)

            conn.commit()
            cur.close()
            conn.close()
            log.info(
                "Saved %d param changes to DB for %s (trader=%s)",
                len(self.optimization.changes), self.agent_id, trader_id,
            )
            return first_id

        except Exception as e:
            log.error("Failed to save learning loop results to DB: %s", e)
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
            return -1


# ═══════════════════════════════════════════════════════════════════════════
# TRADE GRADER — pure function
# ═══════════════════════════════════════════════════════════════════════════


def grade_trade(
    trade: dict,
    market_data: Optional[dict] = None,
    timestamp: Optional[str] = None,
) -> Grade:
    """Grade a single trade on 4 criteria: entry, exit, risk, conviction.

    Args:
        trade: Trade dict with keys like id, agent_id, ticker, action,
               entry_price, exit_price, quantity, stop_loss, thesis,
               confidence, pnl, exit_reason, opened_at, closed_at, etc.
        market_data: Optional dict with price, ma20, ma50, rsi, atr.
        timestamp: Optional ISO timestamp for harness compatibility.

    Returns:
        Grade with per-category scores 0–100.
    """
    ts = timestamp or datetime.now().isoformat()
    trade_id = str(trade.get("id", trade.get("trade_id", "unknown")))
    agent_id = str(trade.get("agent_id", "unknown"))
    ticker = str(trade.get("ticker", "unknown")).upper()
    action = str(trade.get("action", "BUY")).upper()

    grade = Grade(
        trade_id=trade_id,
        agent_id=agent_id,
        ticker=ticker,
        action=action,
        timestamp=ts,
    )

    grade.entry_timing = _score_entry(trade, market_data)
    grade.exit_timing = _score_exit(trade)
    grade.risk_management = _score_risk(trade, market_data)
    grade.conviction = _score_conviction(trade)
    grade.total_score = grade._compute_total()
    grade.grade_letter = grade._compute_letter()

    return grade


def _score_entry(trade: dict, market_data: Optional[dict]) -> float:
    """Score entry timing 0–100."""
    action = trade.get("action", "BUY")
    if action == "HOLD":
        return 80.0

    score = 50.0  # baseline

    thesis = str(trade.get("thesis", ""))
    if len(thesis) > 50:
        score += 15
    elif len(thesis) > 20:
        score += 8
    elif len(thesis) > 0:
        score += 3

    confidence = trade.get("confidence")
    if confidence is not None:
        try:
            conf = float(confidence)
            if conf >= 0.75:
                score += 12
            elif conf >= 0.50:
                score += 6
        except (ValueError, TypeError):
            pass

    if market_data and action == "BUY":
        price = market_data.get("price")
        ma20 = market_data.get("ma20")
        ma50 = market_data.get("ma50")
        rsi = market_data.get("rsi")

        if price and ma20 and price > ma20:
            score += 8
        if price and ma50 and price > ma50:
            score += 5
        if rsi is not None:
            try:
                rsi_val = float(rsi)
                if 40 <= rsi_val <= 70:
                    score += 7
            except (ValueError, TypeError):
                pass

    if action == "SELL" and market_data:
        price = market_data.get("price")
        ma20 = market_data.get("ma20")
        if price and ma20 and price < ma20:
            score += 8

    return max(0.0, min(100.0, score))


def _score_exit(trade: dict) -> float:
    """Score exit timing 0–100."""
    action = trade.get("action", "BUY")
    pnl = trade.get("pnl")
    exit_reason = str(trade.get("exit_reason", ""))

    if action == "HOLD":
        return 70.0
    if pnl is None:
        return 50.0

    score = 50.0
    try:
        pnl_val = float(pnl)
    except (ValueError, TypeError):
        return 50.0

    if pnl_val > 0:
        score += 20
        if "target" in exit_reason.lower() or "profit" in exit_reason.lower():
            score += 10
        elif "trailing" in exit_reason.lower() or "stop" in exit_reason.lower():
            score += 5
    elif pnl_val < 0:
        if "stop" in exit_reason.lower() or "risk" in exit_reason.lower():
            score += 8
        elif pnl_val > -0.05:
            score += 3
        else:
            score -= 10

    if len(exit_reason) > 10:
        score += 5
    elif len(exit_reason) > 0:
        score += 2

    return max(0.0, min(100.0, score))


def _score_risk(trade: dict, market_data: Optional[dict]) -> float:
    """Score risk management 0–100."""
    action = trade.get("action", "BUY")
    if action in ("HOLD", "SELL"):
        return 75.0

    score = 50.0

    stop_loss = trade.get("stop_loss")
    if stop_loss is not None and float(stop_loss) > 0:
        score += 15
    else:
        score -= 10

    quantity = trade.get("quantity", 0)
    entry_price = trade.get("entry_price") or (market_data or {}).get("price", 100)
    portfolio_value = trade.get("portfolio_value", 10000)

    try:
        pos_value = float(quantity) * float(entry_price)
        pos_pct = pos_value / float(portfolio_value) if float(portfolio_value) > 0 else 0
        if pos_pct <= 0.10:
            score += 15
        elif pos_pct <= 0.20:
            score += 8
        elif pos_pct <= 0.30:
            score += 2
        else:
            score -= 10
    except (ValueError, TypeError, ZeroDivisionError):
        pass

    max_daily_loss = trade.get("max_daily_loss")
    if max_daily_loss is not None:
        score += 5

    return max(0.0, min(100.0, score))


def _score_conviction(trade: dict) -> float:
    """Score conviction/thesis-driven decision making 0–100."""
    score = 50.0

    thesis = str(trade.get("thesis", ""))
    if len(thesis) > 80:
        score += 20
    elif len(thesis) > 40:
        score += 12
    elif len(thesis) > 10:
        score += 5

    confidence = trade.get("confidence")
    if confidence is not None:
        try:
            conf = float(confidence)
            if conf >= 0.80:
                score += 15
            elif conf >= 0.60:
                score += 8
            elif conf < 0.30:
                score -= 10
        except (ValueError, TypeError):
            pass

    signals = trade.get("signals_used", trade.get("signals", []))
    if isinstance(signals, str):
        try:
            signals = json.loads(signals)
        except (json.JSONDecodeError, TypeError):
            signals = []
    if isinstance(signals, list) and len(signals) >= 3:
        score += 8
    elif isinstance(signals, list) and len(signals) >= 1:
        score += 3

    return max(0.0, min(100.0, score))


def grade_trades(
    trades: List[dict],
    market_data: Optional[dict] = None,
    timestamp: Optional[str] = None,
) -> List[Grade]:
    """Grade a batch of trades."""
    ts = timestamp or datetime.now().isoformat()
    return [grade_trade(t, market_data, ts) for t in trades]


# ═══════════════════════════════════════════════════════════════════════════
# PATTERN ANALYZER
# ═══════════════════════════════════════════════════════════════════════════


def analyze_patterns(
    trades: List[Grade],
    timestamp: Optional[str] = None,
) -> List[PatternInsight]:
    """Analyze graded trades for actionable patterns.

    Groups trades by scoring category, identifies winning and losing
    patterns, and detects parameter drift.

    Args:
        trades: List of Grade objects (already graded).
        timestamp: Optional ISO timestamp for harness compatibility.

    Returns:
        List of PatternInsight with recommendations.
    """
    if not trades:
        return [
            PatternInsight(
                pattern_type="no_data",
                description="No trades available for pattern analysis.",
                category="general",
                confidence=1.0,
                recommendation="Collect more trade data before analyzing patterns.",
            )
        ]

    insights: List[PatternInsight] = []

    # Per-category weakness analysis
    for cat in _CATEGORY_WEIGHTS:
        scores = [getattr(t, cat, 0.0) for t in trades]
        avg = sum(scores) / len(scores) if scores else 0.0

        if avg < 40:
            worst_trades = sorted(trades, key=lambda t: getattr(t, cat, 0.0))[:3]
            insights.append(PatternInsight(
                pattern_type="losing_pattern",
                description=f"Category '{cat}' is critically weak (avg {avg:.0f}/100).",
                category=cat,
                confidence=0.9,
                affected_trades=[t.trade_id for t in worst_trades],
                recommendation=_remediation_for_category(cat),
            ))
        elif avg < 60:
            insights.append(PatternInsight(
                pattern_type="losing_pattern",
                description=f"Category '{cat}' is below threshold (avg {avg:.0f}/100).",
                category=cat,
                confidence=0.7,
                affected_trades=[t.trade_id for t in trades if getattr(t, cat, 0.0) < 50],
            ))

    # Winning pattern identification
    wins = [t for t in trades if t.is_win]
    if wins and len(wins) >= 3:
        win_cats = {}
        for cat in _CATEGORY_WEIGHTS:
            win_cats[cat] = sum(getattr(t, cat, 0.0) for t in wins) / len(wins)
        best_cat = max(win_cats, key=win_cats.get)
        insights.append(PatternInsight(
            pattern_type="winning_pattern",
            description=f"Winning trades excel at '{best_cat}' (avg {win_cats[best_cat]:.0f}/100). "
                        f"Consider doubling down on this strength.",
            category=best_cat,
            confidence=0.75,
            affected_trades=[t.trade_id for t in wins[:5]],
            recommendation=f"Maintain or strengthen {best_cat}-related behaviors.",
            data_snippet={"win_avg_by_category": {k: round(v, 1) for k, v in win_cats.items()}},
        ))

    # Parameter drift detection
    if len(trades) >= 4:
        midpoint = len(trades) // 2
        early = trades[:midpoint]
        late = trades[midpoint:]
        early_avg = sum(t.total_score for t in early) / len(early)
        late_avg = sum(t.total_score for t in late) / len(late)

        drift = late_avg - early_avg
        if drift < -10:
            cat_drifts = {}
            for cat in _CATEGORY_WEIGHTS:
                early_cat = sum(getattr(t, cat, 0.0) for t in early) / len(early)
                late_cat = sum(getattr(t, cat, 0.0) for t in late) / len(late)
                if late_cat - early_cat < -5:
                    cat_drifts[cat] = round(late_cat - early_cat, 1)
            if cat_drifts:
                insights.append(PatternInsight(
                    pattern_type="drift",
                    description=f"Performance is degrading significantly "
                                f"(avg score {early_avg:.0f} → {late_avg:.0f}). "
                                f"Parameter drift detected in: {list(cat_drifts.keys())}.",
                    category="general",
                    confidence=0.8,
                    affected_trades=[t.trade_id for t in late],
                    recommendation="Review strategy configuration and consider re-optimizing parameters.",
                    data_snippet={"drift_by_category": cat_drifts},
                ))

    # Agent-specific analysis
    agent_ids = list(set(t.agent_id for t in trades))
    for agent_id in agent_ids:
        agent_trades = [t for t in trades if t.agent_id == agent_id]
        if len(agent_trades) >= 3:
            agent_avg = sum(t.total_score for t in agent_trades) / len(agent_trades)
            if agent_avg < 50:
                weaknesses = {}
                for cat in _CATEGORY_WEIGHTS:
                    cat_avg = sum(getattr(t, cat, 0.0) for t in agent_trades) / len(agent_trades)
                    weaknesses[cat] = cat_avg
                worst = min(weaknesses, key=weaknesses.get)
                insights.append(PatternInsight(
                    pattern_type="losing_pattern",
                    description=f"Agent '{agent_id}' is struggling overall (avg {agent_avg:.0f}/100). "
                                f"Biggest weakness: '{worst}' at {weaknesses[worst]:.0f}/100.",
                    category=worst,
                    confidence=0.85,
                    affected_trades=[t.trade_id for t in agent_trades],
                    recommendation=f"Focus improvement on {worst} for agent {agent_id}.",
                    data_snippet={"agent_avg": round(agent_avg, 1)},
                ))

    return insights


def _remediation_for_category(category: str) -> str:
    """Return default remediation advice for a scoring category."""
    advice = {
        "entry_timing": "Improve entry timing: require stronger signal confirmation, "
                        "use limit orders near support, wait for trend confirmation.",
        "exit_timing": "Improve exit discipline: set profit targets and stop losses "
                       "before entering. Use trailing stops to capture gains.",
        "risk_management": "Tighten risk controls: reduce position sizes, "
                           "always set stops, enforce daily loss limits.",
        "conviction": "Increase conviction threshold: require deeper thesis, "
                      "more confirming signals, and higher confidence before trading.",
    }
    return advice.get(category, "Review this category for improvement opportunities.")


# ═══════════════════════════════════════════════════════════════════════════
# PARAM OPTIMIZER
# ═══════════════════════════════════════════════════════════════════════════


def optimize_params(
    strategy: str,
    trades: List[Grade],
    timestamp: Optional[str] = None,
    persist_to_db: bool = True,
) -> ParamOptimization:
    """Analyze graded trades and propose config parameter changes.

    Args:
        strategy: Strategy name (maps to trader in DB, e.g. "kairos").
        trades: List of graded trades to analyze.
        timestamp: Optional ISO timestamp for harness compatibility.
        persist_to_db: If True (default), writes the proposed changes to the
                       trading.param_history table.

    Returns:
        ParamOptimization with proposed changes.
    """
    ts = timestamp or datetime.now().isoformat()

    current_config = _load_config_from_db(strategy)

    if not trades:
        return ParamOptimization(
            strategy=strategy,
            timestamp=ts,
            current_config=current_config,
            summary="No trades to optimize against.",
        )

    cat_avgs: Dict[str, float] = {}
    for cat in _CATEGORY_WEIGHTS:
        vals = [getattr(t, cat, 0.0) for t in trades]
        cat_avgs[cat] = sum(vals) / len(vals) if vals else 0.0

    changes: List[ParamChange] = []

    if cat_avgs.get("entry_timing", 100) < 50:
        _propose_tighter_entry(changes, current_config, cat_avgs["entry_timing"])

    if cat_avgs.get("exit_timing", 100) < 50:
        _propose_tighter_exit(changes, current_config, cat_avgs["exit_timing"])

    if cat_avgs.get("risk_management", 100) < 50:
        _propose_tighter_risk(changes, current_config, cat_avgs["risk_management"])

    if cat_avgs.get("conviction", 100) < 50:
        _propose_higher_conviction(changes, current_config, cat_avgs["conviction"])

    if not changes:
        total_avg = sum(cat_avgs.values()) / len(cat_avgs)
        if total_avg > 80:
            changes.append(ParamChange(
                param_path="risk.max_position_pct",
                current_value=current_config.get("risk", {}).get("max_position_pct", 0.10),
                proposed_value=round(
                    current_config.get("risk", {}).get("max_position_pct", 0.10) * 1.05, 3
                ),
                justification="Strong performance across all categories. "
                             "Slightly increase position size to capitalize.",
                confidence=0.6,
                source_insight="strong_all_categories",
            ))

    summary_lines = []
    if changes:
        summary_lines.append(f"Proposed {len(changes)} parameter changes:")
        for c in changes:
            direction = "↑" if c.proposed_value > c.current_value else "↓"
            summary_lines.append(
                f"  {direction} {c.param_path}: {c.current_value} → {c.proposed_value} "
                f"({c.justification})"
            )
    else:
        summary_lines.append("No parameter changes proposed. All categories are healthy.")

    opt = ParamOptimization(
        strategy=strategy,
        timestamp=ts,
        current_config=current_config,
        changes=changes,
        summary="\n".join(summary_lines),
    )

    if persist_to_db and changes:
        opt.db_record_ids = _save_changes_to_db(strategy, changes)

    return opt


def _load_config_from_db(strategy: str) -> Dict[str, Any]:
    """Load a trader's current config from the trading.virtual_traders table.

    Args:
        strategy: Strategy name (e.g. "kairos", "aldridge", "stonks").

    Returns:
        Dict with config keys. Falls back to defaults if DB is unavailable.
    """
    # Default config (mirrors what paper-trading-agents YAML would have)
    default_config: Dict[str, Any] = {
        "signals": {
            "minimum_confidence": 0.65,
            "confirmations_required": 3,
        },
        "exit_rules": {
            "profit_target_pct": 0.15,
            "max_hold_days": 7,
        },
        "risk": {
            "max_position_pct": 0.10,
            "stop_loss_pct": 0.05,
        },
    }

    try:
        conn = get_connection()
        cur = conn.cursor()
        # Look for any virtual trader whose base_trader matches strategy
        cur.execute(
            "SELECT config FROM trading.virtual_traders "
            "WHERE base_trader = %s AND status = 'active' "
            "ORDER BY name LIMIT 1",
            (strategy,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()

        if row and row[0] is not None:
            db_config = row[0]  # JSONB → dict
            if isinstance(db_config, dict) and db_config:
                # Merge DB config into defaults
                merged = dict(default_config)
                for key, value in db_config.items():
                    if isinstance(value, dict) and key in merged and isinstance(merged[key], dict):
                        merged[key].update(value)
                    else:
                        merged[key] = value
                log.debug("Loaded config from DB for strategy=%s", strategy)
                return merged

        log.debug("No DB config found for strategy=%s, using defaults", strategy)
        return dict(default_config)

    except Exception as e:
        log.warning("Failed to load config from DB for strategy=%s: %s. Using defaults.", strategy, e)
        try:
            conn.close()
        except Exception:
            pass
        return dict(default_config)


def _save_changes_to_db(strategy: str, changes: List[ParamChange]) -> List[int]:
    """Write proposed parameter changes to the trading.param_history table.

    Args:
        strategy: Strategy name (trader ID in DB).
        changes: List of proposed parameter changes.

    Returns:
        List of record IDs inserted, or empty list on failure.
    """
    record_ids: List[int] = []

    try:
        conn = get_connection()
        cur = conn.cursor()

        for change in changes:
            cur.execute(
                """INSERT INTO trading.param_history
                   (agent_id, param_name, old_value, new_value,
                    before_score, after_score, changed_at,
                    source, reason, trader_id, score_metric)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    f"trader-{strategy}",
                    change.param_path,
                    float(change.current_value) if change.current_value is not None else None,
                    float(change.proposed_value) if change.proposed_value is not None else None,
                    None,   # before_score — unknown at proposal time
                    None,   # after_score — filled later on re-evaluation
                    datetime.now(),
                    "learning_loop",
                    change.justification[:500],
                    strategy,
                    "calmar",
                ),
            )
            row = cur.fetchone()
            if row is not None:
                record_ids.append(row[0])

        conn.commit()
        cur.close()
        conn.close()

        log.info("Saved %d param changes to trading.param_history for %s", len(record_ids), strategy)
        return record_ids

    except Exception as e:
        log.error("Failed to save param changes to DB for %s: %s", strategy, e)
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        return []


def _propose_tighter_entry(changes, config, current_score):
    """Propose entry-related parameter changes."""
    signals = config.get("signals", {})
    curr_conf = signals.get("minimum_confidence", 0.65)
    curr_confirms = signals.get("confirmations_required", 3)
    new_conf = min(0.85, curr_conf + 0.05)
    if new_conf != curr_conf:
        changes.append(ParamChange(
            param_path="signals.minimum_confidence",
            current_value=curr_conf,
            proposed_value=round(new_conf, 2),
            justification=f"Entry timing is weak ({current_score:.0f}/100). "
                          f"Raising minimum confidence to filter out marginal setups.",
            confidence=0.75,
            source_insight="weak_entry_timing",
        ))
    if curr_confirms < 4:
        changes.append(ParamChange(
            param_path="signals.confirmations_required",
            current_value=curr_confirms,
            proposed_value=min(5, curr_confirms + 1),
            justification=f"Entry timing is weak ({current_score:.0f}/100). "
                          f"Requiring more signal confirmations for entry.",
            confidence=0.7,
            source_insight="weak_entry_timing",
        ))


def _propose_tighter_exit(changes, config, current_score):
    """Propose exit-related parameter changes."""
    exit_rules = config.get("exit_rules", {})
    curr_target = exit_rules.get("profit_target_pct", 0.15)
    curr_max_hold = exit_rules.get("max_hold_days", 7)
    new_target = max(0.05, curr_target - 0.025)
    if new_target != curr_target:
        changes.append(ParamChange(
            param_path="exit_rules.profit_target_pct",
            current_value=curr_target,
            proposed_value=round(new_target, 3),
            justification=f"Exit timing is weak ({current_score:.0f}/100). "
                          f"Lowering profit target to lock in gains earlier.",
            confidence=0.7,
            source_insight="weak_exit_timing",
        ))
    new_hold = max(3, curr_max_hold - 1)
    if new_hold != curr_max_hold:
        changes.append(ParamChange(
            param_path="exit_rules.max_hold_days",
            current_value=curr_max_hold,
            proposed_value=new_hold,
            justification=f"Exit timing is weak ({current_score:.0f}/100). "
                          f"Reducing max hold days to rotate capital faster.",
            confidence=0.65,
            source_insight="weak_exit_timing",
        ))


def _propose_tighter_risk(changes, config, current_score):
    """Propose risk-related parameter changes."""
    risk = config.get("risk", {})
    curr_pos = risk.get("max_position_pct", 0.10)
    curr_stop = risk.get("stop_loss_pct", 0.05)
    new_pos = max(0.02, curr_pos - 0.02)
    if new_pos != curr_pos:
        changes.append(ParamChange(
            param_path="risk.max_position_pct",
            current_value=curr_pos,
            proposed_value=round(new_pos, 3),
            justification=f"Risk management is weak ({current_score:.0f}/100). "
                          f"Reducing max position size.",
            confidence=0.8,
            source_insight="weak_risk_management",
        ))
    new_stop = max(0.02, curr_stop - 0.01)
    if new_stop != curr_stop:
        changes.append(ParamChange(
            param_path="risk.stop_loss_pct",
            current_value=curr_stop,
            proposed_value=round(new_stop, 3),
            justification=f"Risk management is weak ({current_score:.0f}/100). "
                          f"Tightening stop loss to cut losses faster.",
            confidence=0.75,
            source_insight="weak_risk_management",
        ))


def _propose_higher_conviction(changes, config, current_score):
    """Propose conviction-related parameter changes."""
    signals = config.get("signals", {})
    curr_confidence = signals.get("minimum_confidence", 0.65)
    new_confidence = min(0.90, curr_confidence + 0.10)
    if new_confidence != curr_confidence:
        changes.append(ParamChange(
            param_path="signals.minimum_confidence",
            current_value=curr_confidence,
            proposed_value=round(new_confidence, 2),
            justification=f"Conviction is weak ({current_score:.0f}/100). "
                          f"Raising minimum confidence threshold significantly.",
            confidence=0.8,
            source_insight="weak_conviction",
        ))


# ═══════════════════════════════════════════════════════════════════════════
# LEARNING LOOP ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════


def run_loop(
    agent_id: str,
    since_date: Optional[str] = None,
    trades: Optional[List[dict]] = None,
    market_data: Optional[dict] = None,
    timestamp: Optional[str] = None,
    skip_optimization: bool = False,
    persist_to_db: bool = True,
) -> LearningLoopResult:
    """Run the full learning loop: fetch → grade → analyze → optimize.

    This is the main entry point. It orchestrates all four components in
    sequence, producing a complete LearningLoopResult.

    Args:
        agent_id: Agent identifier (e.g. "trader-kairos").
        since_date: Optional ISO date to filter trades (inclusive).
        trades: Optional pre-fetched trade list.
        market_data: Optional market data for grading context.
        timestamp: Optional ISO timestamp for harness compatibility.
        skip_optimization: If True, skip the param optimization step.
        persist_to_db: If True (default), persists optimization proposals to
                       the trading.param_history table.

    Returns:
        LearningLoopResult with grades, insights, and optimization.
    """
    ts = timestamp or datetime.now().isoformat()
    errors: List[str] = []

    if trades is None:
        trades = []
        errors.append("No trades provided. Pass a list of trade dicts or use a fetcher.")
    elif len(trades) == 0:
        errors.append("Empty trades list provided — nothing to analyze.")

    if since_date and trades:
        try:
            since_dt = datetime.fromisoformat(since_date)
            trades = [
                t for t in trades
                if _trade_timestamp(t) and datetime.fromisoformat(_trade_timestamp(t)) >= since_dt
            ]
        except (ValueError, TypeError):
            errors.append(f"Invalid since_date format: {since_date}")

    grades = grade_trades(trades, market_data, ts)

    avg_score = sum(g.total_score for g in grades) / len(grades) if grades else 0.0
    grade_trend = _compute_trend(grades)

    insights = analyze_patterns(grades, ts)

    optimization = None
    if not skip_optimization and trades:
        strategy = _agent_to_strategy(agent_id)
        optimization = optimize_params(
            strategy=strategy,
            trades=grades,
            timestamp=ts,
            persist_to_db=persist_to_db,
        )

    result = LearningLoopResult(
        agent_id=agent_id,
        timestamp=ts,
        grades=grades,
        avg_score=avg_score,
        grade_trend=grade_trend,
        insights=insights,
        optimization=optimization,
        errors=errors,
    )

    return result


def _trade_timestamp(trade: dict) -> str:
    """Extract a timestamp from a trade dict for filtering."""
    return str(
        trade.get("opened_at") or trade.get("timestamp") or trade.get("closed_at") or ""
    )


def _agent_to_strategy(agent_id: str) -> str:
    """Map agent_id to strategy name."""
    mapping = {
        "trader-kairos": "kairos",
        "trader-aldridge": "aldridge",
        "trader-stonks": "stonks",
    }
    return mapping.get(agent_id, agent_id.replace("trader-", ""))


def _compute_trend(grades: List[Grade]) -> str:
    """Compute grade trend from chronologically ordered grades."""
    if len(grades) < 3:
        return "flat"
    mid = len(grades) // 2
    early = sum(g.total_score for g in grades[:mid]) / mid
    late = sum(g.total_score for g in grades[mid:]) / (len(grades) - mid)
    diff = late - early
    if diff > 5:
        return "improving"
    elif diff < -5:
        return "declining"
    return "flat"


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Unified Learning Loop — grade, analyze, optimize, and loop."
    )
    sub = parser.add_subparsers(dest="cmd")

    grade_p = sub.add_parser("grade", help="Grade a single trade")
    grade_p.add_argument("--trade-id", default="test-1")
    grade_p.add_argument("--agent", default="trader-kairos")
    grade_p.add_argument("--ticker", default="AAPL")
    grade_p.add_argument("--action", default="BUY")
    grade_p.add_argument("--thesis", default="Strong momentum buy")
    grade_p.add_argument("--confidence", type=float, default=0.75)
    grade_p.add_argument("--pnl", type=float, default=0.0)
    grade_p.add_argument("--stop-loss", type=float, default=None)
    grade_p.add_argument("--json", action="store_true")

    analyze_p = sub.add_parser("analyze", help="Analyze pattern from JSON")
    analyze_p.add_argument("input_file", help="JSON file with list of graded trades")

    opt_p = sub.add_parser("optimize", help="Optimize params for a strategy")
    opt_p.add_argument("strategy", help="Strategy name (kairos, aldridge, stonks)")
    opt_p.add_argument("input_file", help="JSON file with list of graded trades")
    opt_p.add_argument("--no-db", action="store_true", help="Don't persist to DB")

    loop_p = sub.add_parser("loop", help="Run full learning loop")
    loop_p.add_argument("--agent", default="trader-kairos")
    loop_p.add_argument("--trades-json", help="JSON file with trades list")
    loop_p.add_argument("--since", help="ISO date filter")
    loop_p.add_argument("--json", action="store_true")
    loop_p.add_argument("--no-db", action="store_true", help="Don't persist to DB")

    args = parser.parse_args()

    if args.cmd == "grade":
        trade = {
            "id": args.trade_id,
            "agent_id": args.agent,
            "ticker": args.ticker,
            "action": args.action,
            "thesis": args.thesis,
            "confidence": args.confidence,
            "pnl": args.pnl,
            "stop_loss": args.stop_loss,
        }
        g = grade_trade(trade)
        if args.json:
            print(json.dumps(g.to_dict(), indent=2))
        else:
            print(f"Trade: {g.ticker} {g.action} — {g.total_score:.0f}/100 ({g.grade_letter})")
            for cat in _CATEGORY_WEIGHTS:
                print(f"  {cat}: {getattr(g, cat, 0.0):.0f}/100")

    elif args.cmd == "analyze":
        with open(args.input_file) as f:
            data = json.load(f)
        grades = [
            Grade(**{k: v for k, v in item.items() if k in Grade.__dataclass_fields__})
            if isinstance(item, dict) else item
            for item in data
        ] if isinstance(data, list) else []
        if grades and isinstance(grades[0], dict):
            grades = [Grade(**{k: v for k, v in g.items() if k in Grade.__dataclass_fields__})
                      for g in grades]
        insights = analyze_patterns(grades)
        for i in insights:
            print(f"[{i.pattern_type}] {i.description} (confidence: {i.confidence:.0%})")
            if i.recommendation:
                print(f"  → {i.recommendation}")

    elif args.cmd == "optimize":
        with open(args.input_file) as f:
            data = json.load(f)
        grades = [Grade(**{k: v for k, v in g.items() if k in Grade.__dataclass_fields__})
                  for g in data] if isinstance(data, list) else []
        opt = optimize_params(args.strategy, grades, persist_to_db=not args.no_db)
        print(opt.summary)
        if opt.db_record_ids:
            print(f"\nPersisted to DB: record IDs {opt.db_record_ids}")

    elif args.cmd == "loop":
        trades = []
        if args.trades_json:
            with open(args.trades_json) as f:
                trades = json.load(f)
        result = run_loop(
            agent_id=args.agent,
            since_date=args.since,
            trades=trades,
            persist_to_db=not args.no_db,
        )
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(result.report())

    else:
        parser.print_help()

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())