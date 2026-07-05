"""Knowledge sharing — signal board, tool requests, cross-trader learning (§7).

Traders publish observations to a shared board. Other traders read and learn.
Tool access is gated — traders must request and earn new capabilities.
Cross-trader analysis detects herding, divergences, and regime shifts.

This creates natural divergence without artificial constraints.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Set

log = logging.getLogger("knowledge")


# ═══════════════════════════════════════════════════════════════════════════════
# Trader Skill Profiles (§7.0)
# ═══════════════════════════════════════════════════════════════════════════════


# Predefined tool sets per trader
DEFAULT_TOOLKITS: Dict[str, Set[str]] = {
    "kairos": {"momentum_tools", "rsi", "macd", "volume_profile"},
    "aldridge": {"value_tools", "pe_screening", "dividend_analysis", "sector_rotation"},
    "stonks": {"sentiment_tools", "news_scraping", "social_signals", "fear_greed"},
}

# All known tools
ALL_TOOLS: Set[str] = {
    "momentum_tools", "rsi", "macd", "volume_profile",
    "value_tools", "pe_screening", "dividend_analysis", "sector_rotation",
    "sentiment_tools", "news_scraping", "social_signals", "fear_greed",
    "bollinger_bands", "atr", "obv", "vwap", "fibonacci",
    "options_flow", "dark_pool", "institutional_holdings",
}


@dataclass
class ToolUsage:
    """Tracks how a trader uses a tool."""
    tool: str
    first_used: datetime = field(default_factory=datetime.now)
    last_used: datetime = field(default_factory=datetime.now)
    times_used: int = 0
    trades_with_tool: int = 0
    win_rate_with_tool: float = 0.0


class TraderSkillProfile:
    """A trader's toolkit with access control.

    Tools are locked by default. Traders request access and earn it.
    Unused tools are revoked after 30 days.

    Args:
        trader_id: Which trader.
        initial_tools: Starting toolkit (default from DEFAULT_TOOLKITS).
    """

    def __init__(
        self,
        trader_id: str,
        initial_tools: Optional[Set[str]] = None,
        unused_days_before_revoke: int = 30,
    ):
        self.trader_id = trader_id
        self.tools: Set[str] = set(initial_tools or DEFAULT_TOOLKITS.get(trader_id, set()))
        self._usage: Dict[str, ToolUsage] = {}
        self._pending_requests: List[ToolRequest] = []
        self.unused_days_before_revoke = unused_days_before_revoke

    def has_tool(self, tool: str) -> bool:
        return tool in self.tools

    def record_usage(self, tool: str, was_profitable: bool = False) -> None:
        """Record that a tool was used in a trade."""
        if tool not in self._usage:
            self._usage[tool] = ToolUsage(tool=tool)
        u = self._usage[tool]
        u.last_used = datetime.now()
        u.times_used += 1
        if was_profitable:
            u.trades_with_tool += 1
        u.win_rate_with_tool = (
            u.trades_with_tool / u.times_used if u.times_used > 0 else 0.0
        )

    def request_tool(self, tool: str, reason: str) -> ToolRequest:
        """Request access to a tool the trader doesn't have.

        Args:
            tool: Tool name from ALL_TOOLS.
            reason: Why the trader needs this tool.

        Returns:
            ToolRequest for Casper to review.

        Raises:
            ValueError: If tool doesn't exist or trader already has it.
        """
        if tool not in ALL_TOOLS:
            raise ValueError(f"Unknown tool: {tool}")
        if tool in self.tools:
            raise ValueError(f"Already have tool: {tool}")
        if not reason or len(reason) < 10:
            raise ValueError("Reason too short — explain why you need this tool")

        req = ToolRequest(
            trader_id=self.trader_id,
            tool=tool,
            reason=reason,
        )
        self._pending_requests.append(req)
        log.info("[%s] Requested tool: %s — %s", self.trader_id, tool, reason)
        return req

    def grant_tool(self, tool: str) -> None:
        """Grant access to a tool (called by Casper after approval)."""
        if tool not in ALL_TOOLS:
            raise ValueError(f"Unknown tool: {tool}")
        self.tools.add(tool)
        log.info("[%s] Tool granted: %s", self.trader_id, tool)

    def revoke_stale_tools(self) -> List[str]:
        """Revoke tools unused for > unused_days_before_revoke.

        Returns:
            List of revoked tool names.
        """
        now = datetime.now()
        cutoff = now - timedelta(days=self.unused_days_before_revoke)
        revoked = []

        for tool in list(self.tools):
            if tool not in DEFAULT_TOOLKITS.get(self.trader_id, set()):
                # Don't revoke default tools
                usage = self._usage.get(tool)
                if usage and usage.last_used < cutoff:
                    self.tools.discard(tool)
                    revoked.append(tool)
                    log.info("[%s] Tool revoked (unused %d days): %s",
                             self.trader_id, self.unused_days_before_revoke, tool)

        return revoked

    def tool_report(self) -> Dict[str, Any]:
        """Summary of all tools and their performance."""
        return {
            "trader": self.trader_id,
            "active_tools": sorted(self.tools),
            "tool_count": len(self.tools),
            "usage": {
                tool: {
                    "times_used": u.times_used,
                    "win_rate": round(u.win_rate_with_tool, 3),
                    "last_used_days_ago": (datetime.now() - u.last_used).days,
                }
                for tool, u in self._usage.items()
            },
            "pending_requests": len(self._pending_requests),
        }


@dataclass
class ToolRequest:
    """A trader's request for a new tool — sent to Casper for review."""
    trader_id: str
    tool: str
    reason: str
    requested_at: datetime = field(default_factory=datetime.now)
    status: str = "pending"  # pending | approved | denied
    reviewer: Optional[str] = None
    review_note: Optional[str] = None

    def approve(self, reviewer: str = "casper") -> None:
        self.status = "approved"
        self.reviewer = reviewer

    def deny(self, reviewer: str = "casper", note: str = "") -> None:
        self.status = "denied"
        self.reviewer = reviewer
        self.review_note = note


# ═══════════════════════════════════════════════════════════════════════════════
# Signal Board (§7.1)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class Signal:
    """One observation published to the shared signal board."""
    trader: str
    ticker: str
    observation: str
    signal_type: str = "observation"  # observation | lesson | alert
    regime: str = "UNKNOWN"
    confidence: float = 0.5
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)


class SignalBoard:
    """Shared board where traders publish and read observations.

    Each trader publishes observations each tick. Other traders read them
    for cross-trader learning. The board keeps recent signals only.
    """

    def __init__(self, max_signals: int = 500, max_age_hours: int = 24):
        self.max_signals = max_signals
        self.max_age_hours = max_age_hours
        self._signals: List[Signal] = []

    def publish(self, signal: Signal) -> None:
        """Publish an observation to the board."""
        self._signals.append(signal)
        self._prune()

    def publish_observation(
        self, trader: str, ticker: str, observation: str,
        regime: str = "UNKNOWN", confidence: float = 0.5,
    ) -> Signal:
        """Convenience: publish an observation."""
        sig = Signal(
            trader=trader, ticker=ticker, observation=observation,
            signal_type="observation", regime=regime, confidence=confidence,
        )
        self.publish(sig)
        return sig

    def publish_lesson(
        self, trader: str, ticker: str, lesson: str,
    ) -> Signal:
        """Convenience: publish a lesson learned."""
        sig = Signal(
            trader=trader, ticker=ticker, observation=lesson,
            signal_type="lesson", confidence=1.0,
        )
        self.publish(sig)
        return sig

    def publish_alert(
        self, trader: str, alert: str, confidence: float = 1.0,
    ) -> Signal:
        """Convenience: publish an urgent alert."""
        sig = Signal(
            trader=trader, ticker="*", observation=alert,
            signal_type="alert", confidence=confidence,
        )
        self.publish(sig)
        return sig

    def recent(self, n: int = 10, trader: Optional[str] = None,
               signal_type: Optional[str] = None) -> List[Signal]:
        """Get recent signals, optionally filtered."""
        signals = self._signals
        if trader:
            signals = [s for s in signals if s.trader == trader]
        if signal_type:
            signals = [s for s in signals if s.signal_type == signal_type]
        return signals[-n:]

    def recent_for_ticker(self, ticker: str, n: int = 5) -> List[Signal]:
        """Get recent signals about a specific ticker."""
        return [s for s in self._signals if s.ticker == ticker][-n:]

    def alerts(self) -> List[Signal]:
        """Get all active alerts."""
        return [s for s in self._signals if s.signal_type == "alert"]

    def lessons(self) -> List[Signal]:
        """Get all shared lessons."""
        return [s for s in self._signals if s.signal_type == "lesson"]

    def _prune(self) -> None:
        """Remove old or excess signals."""
        now = datetime.now()
        cutoff = now - timedelta(hours=self.max_age_hours)

        # Remove old
        self._signals = [s for s in self._signals if s.timestamp > cutoff]

        # Trim excess
        if len(self._signals) > self.max_signals:
            self._signals = self._signals[-self.max_signals:]

    def __len__(self) -> int:
        return len(self._signals)

    def summary(self) -> Dict[str, Any]:
        """Board summary: activity per trader, recent topics."""
        traders = {}
        tickers = {}
        for s in self._signals:
            traders[s.trader] = traders.get(s.trader, 0) + 1
            tickers[s.ticker] = tickers.get(s.ticker, 0) + 1

        return {
            "total_signals": len(self._signals),
            "active_traders": list(traders.keys()),
            "signals_per_trader": traders,
            "top_tickers": sorted(tickers.items(), key=lambda x: -x[1])[:5],
            "recent_alerts": len(self.alerts()),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-Trader Analysis (§7.2)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class CrossTraderInsight:
    """Result of cross-trader analysis."""
    type: str  # divergence | herding | regime_shift
    severity: str  # info | warning | critical
    description: str
    traders_involved: List[str]
    ticker: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)


def detect_herding(
    positions: Dict[str, Dict[str, float]],  # trader → {ticker → position_size}
    threshold: int = 2,
) -> List[CrossTraderInsight]:
    """Detect when multiple traders hold the same ticker.

    Args:
        positions: Per-trader position map.
        threshold: Min traders holding same ticker to flag (default 2).

    Returns:
        List of herding warnings.
    """
    ticker_traders: Dict[str, List[str]] = {}
    for trader, pos in positions.items():
        for ticker in pos:
            if ticker not in ticker_traders:
                ticker_traders[ticker] = []
            ticker_traders[ticker].append(trader)

    insights = []
    for ticker, traders in ticker_traders.items():
        if len(traders) >= threshold:
            insights.append(CrossTraderInsight(
                type="herding",
                severity="warning",
                description=f"Multiple traders holding {ticker}: {', '.join(traders)}. Risk of correlated losses.",
                traders_involved=traders,
                ticker=ticker,
            ))

    return insights


def detect_divergence(
    alphas: Dict[str, float],  # trader → alpha (excess return)
    threshold: int = 3,
) -> Optional[CrossTraderInsight]:
    """Detect when ALL traders have negative alpha simultaneously.

    This is a strong regime-shift signal — if everyone's losing, the
    market itself may have changed.

    Args:
        alphas: Per-trader alpha values.
        threshold: Min traders needed to trigger (default 3).

    Returns:
        Divergence insight if detected, None otherwise.
    """
    if len(alphas) < threshold:
        return None

    negative_traders = [t for t, a in alphas.items() if a < 0]

    if len(negative_traders) >= threshold:
        return CrossTraderInsight(
            type="divergence",
            severity="critical",
            description=(
                f"All {len(negative_traders)} traders have negative alpha: "
                f"{', '.join(negative_traders)}. "
                f"Possible regime shift — consider reducing exposure."
            ),
            traders_involved=negative_traders,
        )

    return None


def check_correlation_risk(
    positions: Dict[str, Dict[str, float]],
    max_overlap_pct: float = 0.50,
) -> List[CrossTraderInsight]:
    """Check if trader portfolios are too correlated.

    Args:
        positions: Per-trader position map.
        max_overlap_pct: Max overlap before warning (0.50 = 50%).

    Returns:
        List of correlation warnings.
    """
    trader_list = list(positions.keys())
    insights = []

    for i in range(len(trader_list)):
        for j in range(i + 1, len(trader_list)):
            t1, t2 = trader_list[i], trader_list[j]
            tickers1 = set(positions.get(t1, {}).keys())
            tickers2 = set(positions.get(t2, {}).keys())

            if not tickers1 or not tickers2:
                continue

            overlap = tickers1 & tickers2
            overlap_pct = len(overlap) / max(len(tickers1), len(tickers2))

            if overlap_pct > max_overlap_pct:
                insights.append(CrossTraderInsight(
                    type="herding",
                    severity="warning",
                    description=(
                        f"{t1} and {t2} portfolios {overlap_pct:.0%} correlated "
                        f"({len(overlap)} shared tickers: {', '.join(sorted(overlap))})"
                    ),
                    traders_involved=[t1, t2],
                ))

    return insights
