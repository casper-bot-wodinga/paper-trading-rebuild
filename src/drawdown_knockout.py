#!/usr/bin/env python3
"""
Drawdown Knockout Circuit Breaker (SPEC §8 — Drawdown Management).

Extends the base CircuitBreaker in safety.py to implement the full spec:
  | Drawdown | Action |
  |----------|--------|
  | < 5%    | Normal trading |
  | 5-10%   | Position sizes reduced by 50% |
  | 10-15%  | Trading paused. Learning loop only (observe, don't act). |
  | > 15%   | Emergency stop. Trader disabled. Human must re-enable. |

  Cooling-off: After 3 consecutive losing trades → skip next 2 signals.
  Recovery Mode: When paused → observation-only until reason articulated.

This module integrates with:
  - src.risk.stop_loss for trade-level P&L tracking (consecutive losses)
  - src.observability for alerts and metrics
  - src.risk.manager.RiskManager — blocks new orders during knockout
  - src.trader.Trader — used as a drop-in risk gate in the trade pipeline

Architecture:
  KnockoutStateMachine — pure state machine computing the current knockout tier.
  DrawdownKnockoutGate — composable risk gate (implements the Gate protocol).
  inject_knockout — helper to wire the gate into an existing RiskManager.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from src.observability import alert, metrics

log = logging.getLogger("drawdown_knockout")


# ═══════════════════════════════════════════════════════════════════════════════
# Knockout Tiers
# ═══════════════════════════════════════════════════════════════════════════════


class KnockoutLevel(Enum):
    """Drawdown tier matching the spec table exactly."""
    NORMAL = "normal"           # < 5%  — normal trading
    REDUCED = "reduced"         # 5-10% — position sizes reduced by 50%
    PAUSED = "paused"           # 10-15% — trading paused, learning only
    EMERGENCY = "emergency"     # > 15% — emergency stop, human must re-enable
    COOLING_OFF = "cooling_off"  # 3 consecutive losses → skip 2 signals

    @property
    def can_trade(self) -> bool:
        return self in (KnockoutLevel.NORMAL, KnockoutLevel.REDUCED)

    @property
    def can_open_new_positions(self) -> bool:
        """Only NORMAL allows new positions. REDUCED allows but at half size."""
        return self == KnockoutLevel.NORMAL

    @property
    def position_multiplier(self) -> float:
        """Multiplier applied to position sizing."""
        return {
            KnockoutLevel.NORMAL: 1.0,
            KnockoutLevel.REDUCED: 0.5,
            KnockoutLevel.PAUSED: 0.0,
            KnockoutLevel.EMERGENCY: 0.0,
            KnockoutLevel.COOLING_OFF: 0.0,  # skip signals entirely
        }[self]

    @property
    def allow_exits(self) -> bool:
        """
        Can we close (SELL) existing positions?
        NORMAL/REDUCED: yes.
        PAUSED: yes — can exit to reduce drawdown, cannot open new.
        EMERGENCY: no — freeze everything. Human must re-enable.
        COOLING_OFF: yes — can still manage existing positions but no NEW trades.
        """
        return self != KnockoutLevel.EMERGENCY


# ═══════════════════════════════════════════════════════════════════════════════
# State Machine
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class KnockoutState:
    """Full state for the drawdown knockout circuit breaker.

    This is the pure data — decisions are made by KnockoutStateMachine.
    """
    level: KnockoutLevel = KnockoutLevel.NORMAL
    current_drawdown: float = 0.0
    peak_equity: float = 0.0
    consecutive_losses: int = 0
    cooling_off_signals_to_skip: int = 0  # remaining signals to skip
    paused_at: Optional[datetime] = None
    emergency_at: Optional[datetime] = None
    recovery_plan: Optional[str] = None
    recovery_observation_ticks: int = 0
    journal: List[str] = field(default_factory=list)
    equity_history: List[float] = field(default_factory=list)

    @property
    def can_trade(self) -> bool:
        return self.level.can_trade

    @property
    def can_open_new_positions(self) -> bool:
        return self.level.can_open_new_positions

    @property
    def position_multiplier(self) -> float:
        return self.level.position_multiplier

    @property
    def allow_exits(self) -> bool:
        return self.level.allow_exits

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level.value,
            "current_drawdown_pct": round(self.current_drawdown * 100, 2),
            "peak_equity": round(self.peak_equity, 2),
            "consecutive_losses": self.consecutive_losses,
            "cooling_off_remaining": self.cooling_off_signals_to_skip,
            "can_trade": self.can_trade,
            "can_open_new_positions": self.can_open_new_positions,
            "position_multiplier": self.position_multiplier,
            "allow_exits": self.allow_exits,
            "paused_at": self.paused_at.isoformat() if self.paused_at else None,
            "emergency_at": self.emergency_at.isoformat() if self.emergency_at else None,
            "has_recovery_plan": self.recovery_plan is not None,
            "recovery_observation_ticks": self.recovery_observation_ticks,
        }


class KnockoutStateMachine:
    """Pure state machine for drawdown knockout logic.

    All decisions are deterministic functions of the current state + new inputs.
    No side effects — calling code handles persistence, alerts, and metrics.

    Args:
        caution_threshold: Drawdown % for REDUCED mode (default 0.05 = 5%).
        pause_threshold: Drawdown % for PAUSED mode (default 0.10 = 10%).
        emergency_threshold: Drawdown % for EMERGENCY (default 0.15 = 15%).
        max_consecutive_losses: Losses before cooling-off (default 3).
        cool_off_skip_signals: Signals to skip after cooling-off (default 2).
        min_recovery_ticks: Observation ticks before exiting PAUSED (default 10).
    """

    def __init__(
        self,
        caution_threshold: float = 0.05,
        pause_threshold: float = 0.10,
        emergency_threshold: float = 0.15,
        max_consecutive_losses: int = 3,
        cool_off_skip_signals: int = 2,
        min_recovery_ticks: int = 10,
    ):
        self.caution_threshold = caution_threshold
        self.pause_threshold = pause_threshold
        self.emergency_threshold = emergency_threshold
        self.max_consecutive_losses = max_consecutive_losses
        self.cool_off_skip_signals = cool_off_skip_signals
        self.min_recovery_ticks = min_recovery_ticks

    def compute(
        self,
        state: KnockoutState,
        equity: float,
        last_trade_pnl: Optional[float] = None,
        *,
        now: Optional[datetime] = None,
    ) -> Tuple[KnockoutLevel, KnockoutState]:
        """Compute next knockout state.

        Args:
            state: Current state.
            equity: Current total equity.
            last_trade_pnl: P&L of the most recent closed trade. None if no trade.
            now: Current timestamp (defaults to datetime.now()).

        Returns:
            (new_level, mutated_state)
        """
        now = now or datetime.now()
        peak = state.peak_equity

        # ── 1. Track peak equity ──────────────────────────────────────
        if equity > peak:
            peak = equity

        # ── 2. Compute drawdown ───────────────────────────────────────
        dd = (peak - equity) / peak if peak > 0 else 0.0

        # ── 3. Track consecutive losses ───────────────────────────────
        consecutive_losses = state.consecutive_losses
        if last_trade_pnl is not None:
            if last_trade_pnl < 0:
                consecutive_losses += 1
            else:
                consecutive_losses = 0

        # ── 4. Determine level ────────────────────────────────────────
        new_level = self._determine_level(
            state=state,
            dd=dd,
            consecutive_losses=consecutive_losses,
            now=now,
        )

        # ── 5. Build new state ────────────────────────────────────────
        new = KnockoutState(
            level=new_level,
            current_drawdown=dd,
            peak_equity=peak,
            consecutive_losses=consecutive_losses,
            cooling_off_signals_to_skip=state.cooling_off_signals_to_skip,
            paused_at=state.paused_at,
            emergency_at=state.emergency_at,
            recovery_plan=state.recovery_plan,
            recovery_observation_ticks=state.recovery_observation_ticks,
            journal=list(state.journal),
            equity_history=list(state.equity_history),
        )

        # Decrement cooling-off counter if active
        if new.cooling_off_signals_to_skip > 0:
            new.cooling_off_signals_to_skip -= 1

        return new_level, new

    def _determine_level(
        self,
        state: KnockoutState,
        dd: float,
        consecutive_losses: int,
        now: datetime,
    ) -> KnockoutLevel:
        """Core level determination logic.

        Priority (highest wins):
          1. Emergency (dd >= 15%) — sticky, human must re-enable
          2. Cooling-off (3 consecutive losses)
          3. Paused (dd >= 10%)
          4. Reduced (dd >= 5%)
          5. Normal

        Sticky rules:
          - EMERGENCY stays EMERGENCY until human calls re_enable()
          - PAUSED stays PAUSED until recovery plan accepted + DD < 10%
          - COOLING_OFF decays by one signal per tick (via decrement)

        Recovery from PAUSED:
          - Need min_recovery_ticks observations
          - Recovery plan must be submitted
          - DD must be back below caution_threshold
        """
        # 1. EMERGENCY check (highest priority, sticky)
        if dd >= self.emergency_threshold:
            return KnockoutLevel.EMERGENCY

        # If already in EMERGENCY, stay there (sticky)
        if state.level == KnockoutLevel.EMERGENCY:
            return KnockoutLevel.EMERGENCY

        # 2. Cooling-off check
        if state.cooling_off_signals_to_skip > 0:
            return KnockoutLevel.COOLING_OFF

        if consecutive_losses >= self.max_consecutive_losses:
            # Trigger cooling-off: skip next N signals
            # (Will be set in the calling code and returned as COOLING_OFF)
            return KnockoutLevel.COOLING_OFF

        # 3. Paused check
        if dd >= self.pause_threshold:
            return KnockoutLevel.PAUSED

        # If already paused, can we exit?
        if state.level == KnockoutLevel.PAUSED:
            # Check recovery plan
            if not state.recovery_plan:
                return KnockoutLevel.PAUSED  # must articulate reason
            if state.recovery_observation_ticks < self.min_recovery_ticks:
                return KnockoutLevel.PAUSED  # need more observation
            # DD must be below caution threshold to return to normal
            if dd >= self.caution_threshold:
                return KnockoutLevel.PAUSED  # still too high
            # All conditions met — can exit PAUSED
            # Falls through to reduced/normal check

        # 4. Reduced check
        if dd >= self.caution_threshold:
            return KnockoutLevel.REDUCED

        # 5. Normal
        return KnockoutLevel.NORMAL


# ═══════════════════════════════════════════════════════════════════════════════
# Drawdown Knockout Gate — composable RiskManager gate
# ═══════════════════════════════════════════════════════════════════════════════


class DrawdownKnockoutGate:
    """Composable risk gate that rejects trades during knockout conditions.

    Integrates into RiskManager's gate chain. Implements the Gate protocol:
        check(context, action, timestamp=None) -> (granted, reason)

    Rejects:
      - Any BUY/SELL during EMERGENCY (positions frozen)
      - Any BUY during PAUSED or COOLING_OFF (observation only)
      - Does NOT reject SELLs during PAUSED/COOLING_OFF (exits allowed)

    Config keys (from context):
        knockout_state: KnockoutState — required for evaluation.
    """

    def check(
        self,
        context: Dict[str, Any],
        action: Dict[str, Any],
        timestamp: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """Evaluate a trade action against the current knockout state.

        Args:
            context: Must include 'knockout_state' (KnockoutState instance).
            action: Trade action dict with 'type' (BUY/SELL/HOLD).
            timestamp: Optional timestamp (unused, gate uses state directly).

        Returns:
            (granted: bool, reason: str)
        """
        state: Optional[KnockoutState] = context.get("knockout_state")
        if state is None:
            # No knockout state available — pass-through (not this gate's job)
            return True, "DrawdownKnockoutGate: no knockout state, passed"

        action_type = str(action.get("type", action.get("action", ""))).upper()
        ticker = str(action.get("ticker", "")).upper()

        if action_type == "HOLD":
            return True, "DrawdownKnockoutGate: HOLD always allowed"

        # ── EMERGENCY: positions frozen, no trades of any kind ──
        if state.level == KnockoutLevel.EMERGENCY:
            return False, (
                f"DrawdownKnockoutGate: EMERGENCY — "
                f"all trading frozen at {state.current_drawdown:.1%} drawdown. "
                f"Human must re-enable."
            )

        # ── PAUSED or COOLING_OFF: no new positions ──
        if state.level in (KnockoutLevel.PAUSED, KnockoutLevel.COOLING_OFF):
            if action_type == "BUY":
                return False, (
                    f"DrawdownKnockoutGate: {state.level.value.upper()} — "
                    f"no new positions allowed (DD={state.current_drawdown:.1%}). "
                    f"Observation only."
                )
            # SELLs allowed during PAUSED/COOLING_OFF (reduce exposure)
            return True, (
                f"DrawdownKnockoutGate: {state.level.value.upper()} — "
                f"SELL {ticker} allowed (exits permitted)"
            )

        # ── REDUCED: position sizing reduced by 50% (applied via multiplier) ──
        if state.level == KnockoutLevel.REDUCED:
            # Gate passes — sizing reduction is handled by position_multiplier
            return True, (
                f"DrawdownKnockoutGate: REDUCED — "
                f"position sizing at 50% (DD={state.current_drawdown:.1%})"
            )

        # ── NORMAL ──
        return True, (
            f"DrawdownKnockoutGate: NORMAL — "
            f"DD={state.current_drawdown:.1%} within normal range"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# DrawdownKnockout — orchestrator wrapping state machine + gate + persistence
# ═══════════════════════════════════════════════════════════════════════════════


class DrawdownKnockout:
    """Full drawdown knockout circuit breaker.

    Wraps KnockoutStateMachine, manages KnockoutState, publishes alerts,
    and integrates with the trading pipeline.

    Usage:
        dk = DrawdownKnockout("trader-aldridge")

        # On each tick (after equity computed):
        dk.update(portfolio_equity)

        # After a trade closes:
        dk.update(portfolio_equity, last_trade_pnl=-150.0)

        # Before executing a trade:
        context = {"knockout_state": dk.state}
        granted, reason = gate.check(context, action)

        # On emergency:
        dk.emergency_status()  # returns state with "freeze" context

    Args:
        trader_id: Which trader this protects.
        caution_threshold: Drawdown threshold for REDUCED (default 0.05).
        pause_threshold: Drawdown threshold for PAUSED (default 0.10).
        emergency_threshold: Drawdown threshold for EMERGENCY (default 0.15).
        max_consecutive_losses: Losses before cooling-off (default 3).
        cool_off_skip_signals: Signals to skip (default 2).
        min_recovery_ticks: Minimum observation ticks for PAUSED recovery.
    """

    def __init__(
        self,
        trader_id: str,
        caution_threshold: float = 0.05,
        pause_threshold: float = 0.10,
        emergency_threshold: float = 0.15,
        max_consecutive_losses: int = 3,
        cool_off_skip_signals: int = 2,
        min_recovery_ticks: int = 10,
    ):
        self.trader_id = trader_id
        self.state = KnockoutState()
        self.gate = DrawdownKnockoutGate()
        self._machine = KnockoutStateMachine(
            caution_threshold=caution_threshold,
            pause_threshold=pause_threshold,
            emergency_threshold=emergency_threshold,
            max_consecutive_losses=max_consecutive_losses,
            cool_off_skip_signals=cool_off_skip_signals,
            min_recovery_ticks=min_recovery_ticks,
        )
        self._total_transitions: int = 0

    def update(
        self,
        equity: float,
        last_trade_pnl: Optional[float] = None,
        *,
        now: Optional[datetime] = None,
    ) -> KnockoutState:
        """Update knockout state with latest equity and optional trade result.

        Call this:
          - Every tick with current portfolio equity.
          - After each closed trade with the trade P&L.

        Args:
            equity: Current total equity (cash + positions at market).
            last_trade_pnl: P&L of the most recent closed trade. None if no trade.
            now: Timestamp override (for testing).

        Returns:
            Updated KnockoutState.
        """
        old_level = self.state.level

        # Track equity history for drawdown calculation
        self.state.equity_history.append(equity)
        # Keep last 1000 entries
        if len(self.state.equity_history) > 1000:
            self.state.equity_history = self.state.equity_history[-500:]

        # Run state machine
        new_level, new_state = self._machine.compute(
            self.state, equity, last_trade_pnl=last_trade_pnl, now=now,
        )

        # Handle cooling-off trigger (need to set skip count)
        if (
            new_level == KnockoutLevel.COOLING_OFF
            and self.state.level != KnockoutLevel.COOLING_OFF
            and self.state.consecutive_losses < self._machine.max_consecutive_losses
            and last_trade_pnl is not None and last_trade_pnl < 0
        ):
            # Just triggered cooling-off
            new_state.cooling_off_signals_to_skip = self._machine.cool_off_skip_signals
            msg = (
                f"[{self.trader_id}] COOLING OFF triggered: "
                f"{self.state.consecutive_losses + 1} consecutive losses. "
                f"Skipping next {self._machine.cool_off_skip_signals} signals."
            )
            new_state.journal.append(msg)
            log.warning(msg)

        # Handle transitions
        if new_level != old_level:
            self._transition(old_level, new_level, new_state)
            self._total_transitions += 1

        # Preserve recovery observation ticks
        if new_level == KnockoutLevel.PAUSED:
            new_state.recovery_observation_ticks = (
                self.state.recovery_observation_ticks + 1
            )
        elif new_level != KnockoutLevel.PAUSED and old_level == KnockoutLevel.PAUSED:
            # Just exited paused — reset recovery tracking
            new_state.recovery_observation_ticks = 0
            new_state.recovery_plan = None

        # Preserve paused/emergency timestamps
        if new_level == KnockoutLevel.PAUSED and old_level != KnockoutLevel.PAUSED:
            new_state.paused_at = now or datetime.now()
        elif new_level == KnockoutLevel.EMERGENCY and old_level != KnockoutLevel.EMERGENCY:
            new_state.emergency_at = now or datetime.now()

        self.state = new_state
        return self.state

    def _transition(
        self,
        old: KnockoutLevel,
        new: KnockoutLevel,
        state: KnockoutState,
    ) -> None:
        """Handle level transition — log and fire observability alerts."""
        dd = state.current_drawdown
        msg = (
            f"[{self.trader_id}] Knockout: {old.value} → {new.value} "
            f"(DD={dd:.1%}, consecutive_losses={state.consecutive_losses})"
        )
        log.warning(msg)
        state.journal.append(msg)

        # Fire alerts based on severity
        alert_data = {
            "trader_id": self.trader_id,
            "from_level": old.value,
            "to_level": new.value,
            "drawdown_pct": round(dd * 100, 2),
            "peak_equity": round(state.peak_equity, 2),
            "consecutive_losses": state.consecutive_losses,
        }

        if new in (KnockoutLevel.PAUSED, KnockoutLevel.EMERGENCY):
            alert.p0(
                f"Drawdown knockout: {self.trader_id} → {new.value}",
                alert_data,
            )
            metrics.increment("drawdown.knockout.breach", tags={
                "trader": self.trader_id,
                "level": new.value,
            })
        elif new == KnockoutLevel.REDUCED:
            alert.p1(
                f"Drawdown warning: {self.trader_id} → {new.value}",
                alert_data,
            )
            metrics.increment("drawdown.knockout.caution", tags={
                "trader": self.trader_id,
            })
        elif new == KnockoutLevel.COOLING_OFF:
            alert.p1(
                f"Cooling off: {self.trader_id} — {state.consecutive_losses} consecutive losses",
                alert_data,
            )
            metrics.increment("drawdown.knockout.cooling_off", tags={
                "trader": self.trader_id,
            })

    def submit_recovery_plan(self, plan: str) -> str:
        """Submit a recovery plan to exit PAUSED mode.

        Args:
            plan: Explanation of what went wrong and how to fix it.

        Returns:
            'accepted' or 'rejected: <reason>'
        """
        if self.state.level != KnockoutLevel.PAUSED:
            return f"rejected: not in PAUSED mode (current: {self.state.level.value})"

        if not plan or len(plan.strip()) < 20:
            return "rejected: plan too short — explain what went wrong and how you'll fix it"

        if self.state.recovery_observation_ticks < self._machine.min_recovery_ticks:
            return (
                f"rejected: need {self._machine.min_recovery_ticks} observation ticks, "
                f"only have {self.state.recovery_observation_ticks}"
            )

        self.state.recovery_plan = plan.strip()
        msg = f"[{self.trader_id}] Recovery plan submitted: {plan[:100]}..."
        self.state.journal.append(msg)
        log.warning(msg)
        return "accepted"

    def re_enable(self) -> None:
        """Human re-enables after emergency stop.

        Resets all state — trader starts fresh with current equity as peak.
        """
        if self.state.level != KnockoutLevel.EMERGENCY:
            log.warning(
                "[%s] re_enable() called but not in EMERGENCY (level=%s)",
                self.trader_id, self.state.level.value,
            )

        self.state.level = KnockoutLevel.NORMAL
        self.state.current_drawdown = 0.0
        self.state.peak_equity = 0.0  # resets on next update()
        self.state.consecutive_losses = 0
        self.state.cooling_off_signals_to_skip = 0
        self.state.paused_at = None
        self.state.emergency_at = None
        self.state.recovery_plan = None
        self.state.recovery_observation_ticks = 0

        msg = f"[{self.trader_id}] MANUALLY RE-ENABLED after emergency stop."
        self.state.journal.append(msg)
        log.warning(msg)

        alert.p1(
            f"Trader re-enabled: {self.trader_id}",
            {"trader_id": self.trader_id, "action": "re_enable"},
        )
        metrics.increment("drawdown.knockout.re_enabled", tags={
            "trader": self.trader_id,
        })

    def emergency_status(self) -> Dict[str, Any]:
        """Get full emergency status dict for dashboard/canvas alerts."""
        return {
            "trader_id": self.trader_id,
            **self.state.to_dict(),
            "total_transitions": self._total_transitions,
            "thresholds": {
                "caution_pct": round(self._machine.caution_threshold * 100, 1),
                "pause_pct": round(self._machine.pause_threshold * 100, 1),
                "emergency_pct": round(self._machine.emergency_threshold * 100, 1),
            },
            "freeze_positions": self.state.level == KnockoutLevel.EMERGENCY,
            "alert_required": self.state.level in (
                KnockoutLevel.PAUSED, KnockoutLevel.EMERGENCY,
            ),
            "recovery_ready": (
                self.state.level == KnockoutLevel.PAUSED
                and self.state.recovery_observation_ticks >= self._machine.min_recovery_ticks
            ),
        }

    def __repr__(self) -> str:
        return (
            f"DrawdownKnockout({self.trader_id}: "
            f"level={self.state.level.value}, "
            f"DD={self.state.current_drawdown:.1%}, "
            f"peak=${self.state.peak_equity:,.0f})"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Injection helper
# ═══════════════════════════════════════════════════════════════════════════════


def inject_knockout_gate(
    risk_manager,
    knockout: Optional[DrawdownKnockout] = None,
    trader_id: Optional[str] = None,
) -> Tuple[DrawdownKnockout, Any]:
    """Inject a DrawdownKnockoutGate into an existing RiskManager's gate chain.

    If no DrawdownKnockout is provided, creates one with default config.

    Args:
        risk_manager: RiskManager instance to inject into.
        knockout: Optional existing DrawdownKnockout instance.
        trader_id: Trader ID (required if creating new knockout).

    Returns:
        (drawdown_knockout, modified_risk_manager)
    """
    from src.risk.manager import RiskManager

    if knockout is None:
        if trader_id is None:
            raise ValueError("Must provide trader_id when creating a new DrawdownKnockout")
        knockout = DrawdownKnockout(trader_id=trader_id)

    # Insert the knockout gate at position 0 (first gate to check)
    existing_gates = list(risk_manager.gates)
    new_gates = [knockout.gate] + existing_gates

    # Build a new RiskManager with the modified gate chain
    # (Copy non-gate properties from existing manager)
    new_manager = RiskManager(gates=new_gates)
    new_manager._session_gate_blocks = risk_manager._session_gate_blocks

    return knockout, new_manager
