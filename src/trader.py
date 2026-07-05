"""Trader integration — wires signal engine, safety, replay, and decisions together.

This is the runtime trader loop that will run on Monday. It composes:
  - SignalEngine (§3): computes indicators from market data
  - CircuitBreaker (§8): enforces drawdown limits
  - ChangeGovernor (§12): controls parameter evolution
  - ReplayHarness (§2): backtests parameter changes
  - SignalBoard (§7): publishes observations

Lifecycle:
  tick arrives → signal engine → safety check → decision → execution → journal
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from src.replay import ReplayResult, Tick, Portfolio, TraderDecision
from src.safety import BreakerLevel, CircuitBreaker, ChangeGovernor
from src.signals import SignalEngine, SignalParams, SignalReport
from src.metrics import objective_score

log = logging.getLogger("trader")


# ═══════════════════════════════════════════════════════════════════════════════
# Trader State
# ═══════════════════════════════════════════════════════════════════════════════


class TraderMode(Enum):
    WARMUP = "warmup"       # First 10 days: half positions, no tuning
    LIVE = "live"            # Normal trading
    SHADOW = "shadow"        # A/B testing, no real orders
    PAUSED = "paused"        # Drawdown triggered pause
    EMERGENCY = "emergency"  # Human must re-enable


@dataclass
class TraderJournal:
    """Per-tick journal entry — what the trader was thinking."""
    timestamp: datetime
    ticker: str
    price: float
    regime: str
    signal_composite: float
    signal_conviction: float
    breaker_level: str
    decision: str
    rationale: str
    position_count: int
    equity: float
    drawdown: float


@dataclass
class TraderState:
    """Full trader state at any point in time."""
    mode: TraderMode = TraderMode.WARMUP
    equity: float = 100_000.0
    cash: float = 100_000.0
    positions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    trades_closed: int = 0
    trades_won: int = 0
    total_pnl: float = 0.0
    ticks_processed: int = 0
    warmup_ticks_remaining: int = 10  # 10 trading days
    journal: List[TraderJournal] = field(default_factory=list)
    equity_history: List[float] = field(default_factory=list)
    return_history: List[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        if self.trades_closed == 0:
            return 0.0
        return self.trades_won / self.trades_closed

    @property
    def drawdown(self) -> float:
        if not self.equity_history:
            return 0.0
        peak = max(self.equity_history)
        if peak <= 0:
            return 0.0
        return (peak - self.equity) / peak

    @property
    def ready_for_live(self) -> bool:
        """Can this trader exit warmup and go live?"""
        return (
            self.trades_closed >= 20
            and self.warmup_ticks_remaining <= 0
            and self.total_pnl > 0  # positive expectancy
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Trader Runner
# ═══════════════════════════════════════════════════════════════════════════════


class Trader:
    """A self-improving paper trading agent.

    Composes signal engine, circuit breaker, and change governor into
    a complete trading loop. Designed to run autonomously on OpenClaw
    as an agent, receiving ticks from the data bus.

    Args:
        trader_id: Name (kairos, aldridge, stonks).
        params: Signal engine parameters.
        initial_balance: Starting cash.
        max_journal_entries: Keep last N journal entries.
    """

    def __init__(
        self,
        trader_id: str,
        params: Optional[SignalParams] = None,
        initial_balance: float = 100_000.0,
        max_journal_entries: int = 100,
    ):
        self.trader_id = trader_id
        self.signal_engine = SignalEngine(params=params)
        self.breaker = CircuitBreaker(trader_id)
        self.governor = ChangeGovernor(trader_id)
        self.max_journal_entries = max_journal_entries

        self.state = TraderState(
            equity=initial_balance,
            cash=initial_balance,
        )

        # Decision function — can be swapped for LLM agent or Q-learning
        self._decider: Optional[Callable] = None

    # ── Tick processing ─────────────────────────────────────────────────────

    def process_tick(
        self,
        tick: Any,  # Tick-like: has .ticker, .close, .timestamp
        market_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[TraderDecision]:
        """Process one market tick — the main trading loop.

        Args:
            tick: Market data for this moment.
            market_context: Optional SPY trend, VIX, sector data.

        Returns:
            TraderDecision if action taken, None if HOLD or blocked.
        """
        ticker = tick.ticker
        price = tick.close

        self.state.ticks_processed += 1

        # 1. Signal engine: compute indicators
        signal: SignalReport = self.signal_engine.process(tick)
        self.breaker.update(self.state.equity)
        if not self.breaker.state.can_trade:
            self._journal(tick, signal, "BLOCKED", f"Breaker: {self.breaker.state.level.value}")
            return None

        # 3. Position sizing: apply breaker multiplier + warmup reduction
        size_mult = self.breaker.state.position_multiplier
        if self.state.mode == TraderMode.WARMUP:
            size_mult *= 0.5  # half size during warmup

        # 4. Make decision
        if self._decider:
            decision = self._decider(tick, signal, self.state, market_context)
        else:
            decision = self._default_decider(tick, signal)

        if decision is None or decision.decision == "HOLD":
            self._journal(tick, signal, "HOLD", "No signal or conviction too low")
            return None

        # 5. Apply position sizing
        if decision.decision == "BUY":
            max_cost = self.state.equity * signal.recommended_size_pct * size_mult
            decision.shares = int(max_cost / price) if price > 0 else 0

        # 6. Execute (simulated in paper; real would call Alpaca)
        self._simulate_fill(tick, decision)

        # 7. Journal
        self._journal(tick, signal, decision.decision, decision.rationale)

        # 8. Warmup check
        if self.state.mode == TraderMode.WARMUP:
            self.state.warmup_ticks_remaining -= 1
            if self.state.ready_for_live:
                self.state.mode = TraderMode.LIVE
                log.info("[%s] Exiting warmup — going LIVE", self.trader_id)

        return decision

    def _default_decider(
        self, tick: Any, signal: SignalReport
    ) -> Optional[TraderDecision]:
        """Default rule-based decider. Replace with LLM agent or Q-learning.

        Simple rules:
          - Composite > 0.3 AND conviction > 0.3 → BUY
          - Composite < -0.3 AND have position → SELL
          - Otherwise → HOLD
        """
        ticker = tick.ticker
        has_position = ticker in self.state.positions

        if signal.conviction < 0.3:
            return TraderDecision(
                ticker=ticker, decision="HOLD", conviction=signal.conviction,
                rationale=f"Conviction too low ({signal.conviction:.2f})",
            )

        if signal.composite_signal > 0.3 and not has_position:
            return TraderDecision(
                ticker=ticker, decision="BUY", conviction=signal.conviction,
                rationale=f"Bullish signal ({signal.composite_signal:.2f}), regime={signal.regime}",
            )

        if signal.composite_signal < -0.3 and has_position:
            return TraderDecision(
                ticker=ticker, decision="SELL", conviction=signal.conviction,
                rationale=f"Bearish signal ({signal.composite_signal:.2f}), taking profit/cutting loss",
            )

        return TraderDecision(
            ticker=ticker, decision="HOLD", conviction=signal.conviction,
        )

    def set_decider(self, fn: Callable) -> None:
        """Replace the decision function (e.g., with LLM agent or Q-learning)."""
        self._decider = fn

    # ── Execution ────────────────────────────────────────────────────────────

    def _simulate_fill(self, tick: Any, decision: TraderDecision) -> None:
        """Simulate order fill at close price (paper trading)."""
        ticker = decision.ticker or tick.ticker
        price = tick.close

        if decision.decision == "BUY":
            cost = decision.shares * price
            if cost > self.state.cash:
                decision.shares = int(self.state.cash / price)
                cost = decision.shares * price
            if decision.shares <= 0:
                return

            self.state.cash -= cost

            if ticker in self.state.positions:
                # Average in
                pos = self.state.positions[ticker]
                total_shares = pos["shares"] + decision.shares
                total_cost = pos["shares"] * pos["entry_price"] + cost
                pos["shares"] = total_shares
                pos["entry_price"] = total_cost / total_shares
            else:
                self.state.positions[ticker] = {
                    "shares": decision.shares,
                    "entry_price": price,
                    "entry_time": tick.timestamp,
                }

        elif decision.decision == "SELL":
            if ticker not in self.state.positions:
                return

            pos = self.state.positions[ticker]
            shares = min(decision.shares, pos["shares"]) if decision.shares > 0 else pos["shares"]
            proceeds = shares * price
            pnl = shares * (price - pos["entry_price"])

            self.state.cash += proceeds
            self.state.total_pnl += pnl
            self.state.trades_closed += 1
            if pnl > 0:
                self.state.trades_won += 1

            # Update breaker with trade result
            self.breaker.update(self.state.equity, last_trade_pnl=pnl)

            if shares >= pos["shares"]:
                del self.state.positions[ticker]
            else:
                pos["shares"] -= shares

        # Update equity
        self._update_equity()

    def _update_equity(self) -> None:
        """Recalculate equity from cash + positions."""
        position_value = sum(
            pos["shares"] * pos.get("current_price", pos["entry_price"])
            for pos in self.state.positions.values()
        )
        prev = self.state.equity
        self.state.equity = self.state.cash + position_value
        self.state.equity_history.append(self.state.equity)
        if prev > 0:
            self.state.return_history.append((self.state.equity - prev) / prev)

    def update_position_prices(self, prices: Dict[str, float]) -> None:
        """Update mark-to-market prices for open positions."""
        for ticker, price in prices.items():
            if ticker in self.state.positions:
                self.state.positions[ticker]["current_price"] = price
        self._update_equity()

    # ── Journal ──────────────────────────────────────────────────────────────

    def _journal(
        self, tick: Any, signal: SignalReport,
        decision: str, rationale: str,
    ) -> None:
        entry = TraderJournal(
            timestamp=tick.timestamp if hasattr(tick, "timestamp") else datetime.now(),
            ticker=tick.ticker,
            price=tick.close,
            regime=signal.regime,
            signal_composite=signal.composite_signal,
            signal_conviction=signal.conviction,
            breaker_level=self.breaker.state.level.value,
            decision=decision,
            rationale=rationale,
            position_count=len(self.state.positions),
            equity=self.state.equity,
            drawdown=self.state.drawdown,
        )
        self.state.journal.append(entry)
        if len(self.state.journal) > self.max_journal_entries:
            self.state.journal.pop(0)

    def recent_journal(self, n: int = 10) -> List[TraderJournal]:
        """Get last N journal entries."""
        return self.state.journal[-n:]

    # ── Metrics ──────────────────────────────────────────────────────────────

    def compute_objective(self) -> float:
        """Compute current objective score from equity/trade history."""
        if len(self.state.return_history) < 2:
            return 0.0

        returns = np.array(self.state.return_history, dtype=np.float64)
        equity = np.array(self.state.equity_history, dtype=np.float64)

        # Build trade PnL list (approximate from position delta + closed trades)
        trade_pnls = []
        # For now, use the trade history from breaker/state
        # In production this would come from a trade DB

        return objective_score(returns, equity, trade_pnls)

    # ── Status ───────────────────────────────────────────────────────────────

    def status_report(self) -> Dict[str, Any]:
        """One-shot status report for dashboard/canvas."""
        return {
            "trader": self.trader_id,
            "mode": self.state.mode.value,
            "equity": round(self.state.equity, 2),
            "cash": round(self.state.cash, 2),
            "total_pnl": round(self.state.total_pnl, 2),
            "drawdown_pct": round(self.state.drawdown * 100, 2),
            "positions": len(self.state.positions),
            "trades_closed": self.state.trades_closed,
            "win_rate": round(self.state.win_rate, 3),
            "ticks_processed": self.state.ticks_processed,
            "breaker_level": self.breaker.state.level.value,
            "monthly_changes": self.governor.state.monthly_changes,
            "max_monthly_changes": self.governor.max_monthly_changes,
            "signal_params": self.signal_engine.params.to_dict(),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Trader Factory
# ═══════════════════════════════════════════════════════════════════════════════


def create_trader(
    trader_id: str,
    initial_balance: float = 100_000.0,
    params_override: Optional[Dict[str, float]] = None,
) -> Trader:
    """Create a trader with sensible defaults.

    Args:
        trader_id: One of kairos, aldridge, stonks.
        initial_balance: Starting paper balance.
        params_override: Optional parameter overrides.

    Returns:
        Configured Trader ready to process ticks.
    """
    params = SignalParams()

    # Trader-specific defaults (conservative starting points)
    if trader_id == "kairos":
        # Momentum trader — higher momentum sensitivity
        params.set("momentum_threshold", 0.50)
        params.set("base_size_pct", 0.18)
    elif trader_id == "aldridge":
        # Value trader — more positions, smaller sizes
        params.set("base_size_pct", 0.12)
        params.set("max_positions", 7)
        params.set("weight_mean_reverting", 1.0)
    elif trader_id == "stonks":
        # Sentiment trader — responsive to volatility
        params.set("vol_reduction_multiplier", 0.5)
        params.set("weight_high_volatility", 0.6)

    # Apply user overrides last (they take precedence)
    if params_override:
        for name, value in params_override.items():
            params.set(name, value)

    return Trader(trader_id=trader_id, params=params, initial_balance=initial_balance)


def create_fleet(
    initial_balance: float = 100_000.0,
) -> Dict[str, Trader]:
    """Create all three traders (kairos, aldridge, stonks).

    Returns:
        Dict of trader_id → Trader.
    """
    return {
        name: create_trader(name, initial_balance=initial_balance)
        for name in ("kairos", "aldridge", "stonks")
    }
