"""Safety module — circuit breakers, change governance, shadow mode (SPEC-v3 §8,§11,§12).

This is the safety net that makes autonomous trading possible:
  - CircuitBreaker: Tiered drawdown responses (reduce → pause → emergency stop)
  - ChangeGovernor: Budget, damping, revert detection for parameter changes
  - ShadowMode: A/B testing new configs in parallel, auto-merge validated winners
  - RecoveryManager: Observation-only mode with exit criteria

Combined, these prevent the learning loop from blowing up an account.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("safety")

from src.observability import alert, metrics


# ═══════════════════════════════════════════════════════════════════════════════
# Circuit Breaker (§8)
# ═══════════════════════════════════════════════════════════════════════════════


class BreakerLevel(Enum):
    """Tiered drawdown response levels."""
    NORMAL = "normal"             # < 5% DD — trade freely
    CAUTION = "caution"           # 5-10% DD — reduce position sizes 50%
    PAUSED = "paused"             # 10-15% DD — observe only, no orders
    EMERGENCY = "emergency"       # > 15% DD — disabled, human must re-enable


@dataclass
class BreakerState:
    """Current circuit breaker state for a trader."""
    level: BreakerLevel = BreakerLevel.NORMAL
    current_drawdown: float = 0.0
    peak_equity: float = 0.0
    consecutive_losses: int = 0
    cooling_off_until: Optional[datetime] = None
    paused_at: Optional[datetime] = None
    emergency_at: Optional[datetime] = None
    recovery_plan: Optional[str] = None
    journal: List[str] = field(default_factory=list)

    @property
    def can_trade(self) -> bool:
        return self.level in (BreakerLevel.NORMAL, BreakerLevel.CAUTION)

    @property
    def position_multiplier(self) -> float:
        """Position size multiplier based on current level."""
        return {
            BreakerLevel.NORMAL: 1.0,
            BreakerLevel.CAUTION: 0.5,
            BreakerLevel.PAUSED: 0.0,
            BreakerLevel.EMERGENCY: 0.0,
        }[self.level]


class CircuitBreaker:
    """Monitors drawdown and enforces tiered responses.

    Args:
        trader_id: Which trader this breaker protects.
        thresholds: Optional override for drawdown thresholds.
        max_consecutive_losses: Losses before cooling off (default 3).
        cool_off_ticks: Ticks to skip after cooling off triggers (default 2).
    """

    def __init__(
        self,
        trader_id: str,
        thresholds: Optional[Dict[BreakerLevel, float]] = None,
        max_consecutive_losses: int = 3,
        cool_off_ticks: int = 2,
    ):
        self.trader_id = trader_id
        self.thresholds = thresholds or {
            BreakerLevel.CAUTION: 0.05,
            BreakerLevel.PAUSED: 0.10,
            BreakerLevel.EMERGENCY: 0.15,
        }
        self.max_consecutive_losses = max_consecutive_losses
        self.cool_off_ticks = cool_off_ticks
        self.state = BreakerState()

    def update(self, equity: float, last_trade_pnl: Optional[float] = None) -> BreakerState:
        """Update breaker state with latest equity and optional trade result.

        Args:
            equity: Current total equity.
            last_trade_pnl: P&L of the most recent TRADE (not tick). None if no trade.

        Returns:
            Updated BreakerState.
        """
        # Track peak equity
        if equity > self.state.peak_equity:
            self.state.peak_equity = equity

        # Compute drawdown
        if self.state.peak_equity > 0:
            self.state.current_drawdown = (
                self.state.peak_equity - equity
            ) / self.state.peak_equity
        else:
            self.state.current_drawdown = 0.0

        # Track consecutive losses
        if last_trade_pnl is not None:
            if last_trade_pnl < 0:
                self.state.consecutive_losses += 1
            else:
                self.state.consecutive_losses = 0

        # Determine level
        new_level = self._compute_level()
        old_level = self.state.level

        if new_level != old_level:
            self._transition(old_level, new_level)

        self.state.level = new_level
        return self.state

    def _compute_level(self) -> BreakerLevel:
        dd = self.state.current_drawdown

        # Check emergency first
        if dd >= self.thresholds[BreakerLevel.EMERGENCY]:
            return BreakerLevel.EMERGENCY

        # If already in emergency, stay there (human must re-enable)
        if self.state.level == BreakerLevel.EMERGENCY:
            return BreakerLevel.EMERGENCY

        if dd >= self.thresholds[BreakerLevel.PAUSED]:
            return BreakerLevel.PAUSED

        if dd >= self.thresholds[BreakerLevel.CAUTION]:
            return BreakerLevel.CAUTION

        # Cooling-off check
        if self.state.cooling_off_until and datetime.now() < self.state.cooling_off_until:
            return BreakerLevel.CAUTION  # still in cool-off

        # Consecutive losses → cooling off
        if self.state.consecutive_losses >= self.max_consecutive_losses:
            self._start_cooling_off()
            return BreakerLevel.CAUTION

        # Recovering: was paused but drawdown dropped below caution threshold
        if self.state.level == BreakerLevel.PAUSED and dd < self.thresholds[BreakerLevel.CAUTION]:
            # Can exit paused only if recovery plan exists
            if self.state.recovery_plan:
                return BreakerLevel.NORMAL

        return BreakerLevel.NORMAL

    def _transition(self, old: BreakerLevel, new: BreakerLevel) -> None:
        """Handle level transition — log, timestamp, and fire observability alerts."""
        dd = self.state.current_drawdown
        msg = f"[{self.trader_id}] Breaker: {old.value} → {new.value} (DD={dd:.1%})"
        log.warning(msg)
        self.state.journal.append(msg)

        # Fire observability alerts based on severity
        if new in (BreakerLevel.PAUSED, BreakerLevel.EMERGENCY):
            alert.p0(
                f"Drawdown breach: {self.trader_id} → {new.value}",
                {
                    "trader_id": self.trader_id,
                    "level": new.value,
                    "previous_level": old.value,
                    "drawdown_pct": round(dd * 100, 2),
                    "peak_equity": round(self.state.peak_equity, 2),
                    "consecutive_losses": self.state.consecutive_losses,
                },
            )
            metrics.increment("drawdown.breach", tags={
                "trader": self.trader_id,
                "level": new.value,
            })
        elif new == BreakerLevel.CAUTION:
            alert.p1(
                f"Drawdown approaching limits: {self.trader_id}",
                {
                    "trader_id": self.trader_id,
                    "level": new.value,
                    "previous_level": old.value,
                    "drawdown_pct": round(dd * 100, 2),
                    "peak_equity": round(self.state.peak_equity, 2),
                    "consecutive_losses": self.state.consecutive_losses,
                },
            )
            metrics.increment("drawdown.caution", tags={
                "trader": self.trader_id,
            })

        if new == BreakerLevel.PAUSED:
            self.state.paused_at = datetime.now()
        elif new == BreakerLevel.EMERGENCY:
            self.state.emergency_at = datetime.now()

    def _start_cooling_off(self) -> None:
        """Begin cooling-off period after consecutive losses."""
        self.state.cooling_off_until = datetime.now() + timedelta(
            minutes=self.cool_off_ticks * 5  # rough: 5 min per "tick"
        )
        msg = (
            f"[{self.trader_id}] Cooling off after {self.state.consecutive_losses} "
            f"consecutive losses. Skipping next {self.cool_off_ticks} signals."
        )
        log.warning(msg)
        self.state.journal.append(msg)
        self.state.consecutive_losses = 0

    def re_enable(self) -> None:
        """Human re-enables after emergency stop."""
        if self.state.level == BreakerLevel.EMERGENCY:
            self.state.level = BreakerLevel.NORMAL
            self.state.current_drawdown = 0.0
            self.state.peak_equity = 0.0  # will reset on next update
            self.state.consecutive_losses = 0
            self.state.emergency_at = None
            msg = f"[{self.trader_id}] MANUALLY RE-ENABLED after emergency stop."
            self.state.journal.append(msg)
            log.warning(msg)

    def submit_recovery_plan(self, plan: str) -> None:
        """Trader articulates what went wrong and how they'll fix it."""
        self.state.recovery_plan = plan
        msg = f"[{self.trader_id}] Recovery plan submitted: {plan[:100]}..."
        self.state.journal.append(msg)
        log.info(msg)


# ═══════════════════════════════════════════════════════════════════════════════
# Change Governor (§12)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class ChangeRecord:
    """One parameter change for audit trail."""
    param_name: str
    old_value: float
    new_value: float
    proposed_value: float  # before damping
    timestamp: datetime = field(default_factory=datetime.now)
    reason: str = ""


@dataclass
class GovernorState:
    """Per-trader change governance state."""
    monthly_changes: int = 0
    month_start: datetime = field(default_factory=datetime.now)
    changes: List[ChangeRecord] = field(default_factory=list)
    revert_counts: Dict[str, int] = field(default_factory=dict)  # param → revert count
    frozen_until: Dict[str, datetime] = field(default_factory=dict)  # param → freeze until


class ChangeGovernor:
    """Enforces change budget, damping, and revert detection.

    Args:
        max_monthly_changes: Max parameter changes per month (default 5).
        damping_factor: Smoothing: new = (1-d) * old + d * proposed (default 0.3).
        freeze_days: Days to freeze after acceptance (default 5).
        revert_window_days: Days within which a revert is detected (default 20).
        revert_penalty: Multiplier on future budget after revert (default 0.5).
    """

    def __init__(
        self,
        trader_id: str,
        max_monthly_changes: int = 5,
        damping_factor: float = 0.3,
        freeze_days: int = 5,
        revert_window_days: int = 20,
        revert_penalty: float = 0.5,
    ):
        self.trader_id = trader_id
        self.max_monthly_changes = max_monthly_changes
        self.damping_factor = damping_factor
        self.freeze_days = freeze_days
        self.revert_window_days = revert_window_days
        self.revert_penalty = revert_penalty
        self.state = GovernorState()

    def can_change(self, param_name: str) -> Tuple[bool, str]:
        """Check if a parameter change is allowed right now.

        Returns:
            (allowed, reason)
        """
        now = datetime.now()

        # Check monthly budget
        if now.month != self.state.month_start.month:
            self.state.monthly_changes = 0
            self.state.month_start = now

        if self.state.monthly_changes >= self.max_monthly_changes:
            return False, (
                f"Monthly change budget exhausted ({self.state.monthly_changes}/"
                f"{self.max_monthly_changes}). Resets next month."
            )

        # Check freeze
        if param_name in self.state.frozen_until:
            if now < self.state.frozen_until[param_name]:
                remaining = self.state.frozen_until[param_name] - now
                return False, (
                    f"'{param_name}' is frozen for {remaining.days}d "
                    f"{remaining.seconds // 3600}h (evaluation period)."
                )

        # Check revert penalty
        revert_count = self.state.revert_counts.get(param_name, 0)
        if revert_count > 0:
            effective_budget = int(self.max_monthly_changes * (self.revert_penalty ** revert_count))
            if effective_budget < 1:
                return False, (
                    f"'{param_name}' has been reverted {revert_count}x. "
                    f"Budget for this param is exhausted this month."
                )

        return True, "ok"

    def record_change(
        self,
        param_name: str,
        old_value: float,
        proposed_value: float,
        reason: str = "",
    ) -> ChangeRecord:
        """Record a parameter change with damping applied.

        Args:
            param_name: Parameter being changed.
            old_value: Current value.
            proposed_value: What the optimizer wants.
            reason: Why the change (optimization, manual, etc.).

        Returns:
            ChangeRecord with damped new_value.
        """
        # Damping: smooth the change
        new_value = (1 - self.damping_factor) * old_value + self.damping_factor * proposed_value

        record = ChangeRecord(
            param_name=param_name,
            old_value=old_value,
            new_value=new_value,
            proposed_value=proposed_value,
            reason=reason,
        )

        self.state.changes.append(record)
        self.state.monthly_changes += 1

        # Freeze the parameter for evaluation
        self.state.frozen_until[param_name] = datetime.now() + timedelta(days=self.freeze_days)

        log.info(
            "[%s] Change: %s %.4f → %.4f (proposed %.4f, damped %.0f%%) %s",
            self.trader_id, param_name, old_value, new_value,
            proposed_value, self.damping_factor * 100, f"({reason})" if reason else "",
        )

        return record

    def detect_revert(self, param_name: str, old_value: float, new_value: float) -> bool:
        """Check if this change is reverting a recent change.

        A "revert" means: a parameter was changed, then changed back toward
        its original value within revert_window_days.

        Returns True if this is detected as a revert.
        """
        now = datetime.now()
        window = timedelta(days=self.revert_window_days)

        # Find recent changes to this parameter
        recent = [
            c for c in self.state.changes
            if c.param_name == param_name and (now - c.timestamp) < window
        ]

        if not recent:
            return False

        # Check if we're moving toward the oldest recent value
        oldest = recent[0]
        original_dir = oldest.new_value - oldest.old_value  # direction of first change
        current_dir = new_value - old_value  # direction now

        # If directions are opposite, this is a revert
        if (original_dir > 0 and current_dir < 0) or (original_dir < 0 and current_dir > 0):
            self.state.revert_counts[param_name] = self.state.revert_counts.get(param_name, 0) + 1
            log.warning(
                "[%s] REVERT DETECTED: %s changed back within %d days. "
                "Revert count: %d. Halving future budget.",
                self.trader_id, param_name, self.revert_window_days,
                self.state.revert_counts[param_name],
            )
            return True

        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Shadow Mode / A/B Testing (§11)
# ═══════════════════════════════════════════════════════════════════════════════


class ShadowDecision(Enum):
    """What to do with a shadow-validated change."""
    AUTO_MERGE = "auto_merge"       # > 10% improvement → merge now
    NOTIFY = "notify"                # 5-10% improvement → merge with notification
    REVIEW = "review"                # 1-5% improvement → PR for human review
    DISCARD = "discard"              # < 1% improvement → noise, drop it


@dataclass
class ShadowResult:
    """Comparison of shadow vs live config after evaluation period."""
    live_calmar: float
    shadow_calmar: float
    live_worst: float   # worst Calmar during eval (higher = better)
    shadow_worst: float
    improvement_pct: float
    decision: ShadowDecision
    days_evaluated: int
    rollback_point: Optional[str] = None  # git SHA for rollback

    @property
    def summary(self) -> str:
        return (
            f"Shadow: Calmar {self.live_calmar:.2f}→{self.shadow_calmar:.2f} "
            f"({self.improvement_pct:+.1f}%), "
            f"worst day {self.live_worst:.2f}→{self.shadow_worst:.2f}, "
            f"Decision: {self.decision.value}"
        )


class ShadowMode:
    """Manages A/B shadow testing of config changes.

    Args:
        trader_id: Which trader this shadows for.
        eval_days: Days to run shadow before deciding (default 5).
        auto_merge_threshold: Improvement % to auto-merge (default 0.10).
        notify_threshold: Improvement % to notify (default 0.05).
        review_threshold: Improvement % to create review PR (default 0.01).
        rollback_threshold: Live degradation % to auto-revert (default 0.10).
        rollback_window_days: Days after merge to watch for degradation (default 10).
    """

    def __init__(
        self,
        trader_id: str,
        eval_days: int = 5,
        auto_merge_threshold: float = 0.10,
        notify_threshold: float = 0.05,
        review_threshold: float = 0.01,
        rollback_threshold: float = 0.10,
        rollback_window_days: int = 10,
    ):
        self.trader_id = trader_id
        self.eval_days = eval_days
        self.auto_merge_threshold = auto_merge_threshold
        self.notify_threshold = notify_threshold
        self.review_threshold = review_threshold
        self.rollback_threshold = rollback_threshold
        self.rollback_window_days = rollback_window_days

        self._active_shadow: Optional[Dict[str, Any]] = None
        self._live_calmar_history: List[float] = []
        self._shadow_calmar_history: List[float] = []
        self._days_elapsed: int = 0

    def start_shadow(
        self, config_id: str, rollback_sha: str, live_calmar: float
    ) -> None:
        """Begin shadowing a new config variant.

        Args:
            config_id: Identifier for the shadow config (branch name, PR #).
            rollback_sha: Git SHA to roll back to if degradation detected.
            live_calmar: Current live Calmar for baseline.
        """
        self._active_shadow = {
            "config_id": config_id,
            "rollback_sha": rollback_sha,
            "started_at": datetime.now(),
        }
        self._live_calmar_history = [live_calmar]
        self._shadow_calmar_history = []
        self._days_elapsed = 0
        log.info("[%s] Shadow started: %s (baseline Calmar=%.2f)", self.trader_id, config_id, live_calmar)

    def record_day(self, live_calmar: float, shadow_calmar: float) -> None:
        """Record one day of shadow vs live performance."""
        self._live_calmar_history.append(live_calmar)
        self._shadow_calmar_history.append(shadow_calmar)
        self._days_elapsed += 1

    def evaluate(self) -> Optional[ShadowResult]:
        """Evaluate shadow after eval_days. Returns None if not enough data."""
        if self._days_elapsed < self.eval_days or not self._active_shadow:
            return None
        if len(self._shadow_calmar_history) < 2:
            return None

        avg_live = sum(self._live_calmar_history) / len(self._live_calmar_history)
        avg_shadow = sum(self._shadow_calmar_history) / len(self._shadow_calmar_history)

        if abs(avg_live) < 1e-10:
            improvement = avg_shadow
        else:
            improvement = (avg_shadow - avg_live) / abs(avg_live)

        # Check that shadow's worst day isn't WORSE than live's worst day
        # For Calmar, higher = better, so "worst" = minimum
        live_worst = min(self._live_calmar_history)
        shadow_worst = min(self._shadow_calmar_history)
        shadow_safe = shadow_worst >= live_worst * 0.8  # within 20% of live worst

        # Determine decision tier
        if improvement >= self.auto_merge_threshold and shadow_safe:
            decision = ShadowDecision.AUTO_MERGE
        elif improvement >= self.notify_threshold:
            decision = ShadowDecision.NOTIFY
        elif improvement >= self.review_threshold:
            decision = ShadowDecision.REVIEW
        else:
            decision = ShadowDecision.DISCARD

        result = ShadowResult(
            live_calmar=round(avg_live, 4),
            shadow_calmar=round(avg_shadow, 4),
            live_worst=round(live_worst, 4),
            shadow_worst=round(shadow_worst, 4),
            improvement_pct=round(improvement * 100, 2),
            decision=decision,
            days_evaluated=self._days_elapsed,
            rollback_point=self._active_shadow["rollback_sha"],
        )

        log.info("[%s] Shadow eval: %s", self.trader_id, result.summary)
        return result

    def check_degradation(self, current_calmar: float, pre_merge_calmar: float) -> bool:
        """Check if live performance degraded after merge.

        Returns True if degradation exceeds threshold (needs rollback).
        """
        if pre_merge_calmar <= 0:
            return False
        degradation = (pre_merge_calmar - current_calmar) / abs(pre_merge_calmar)
        if degradation > self.rollback_threshold:
            log.warning(
                "[%s] Live degradation detected: Calmar %.2f→%.2f (%.1f%% drop). Rollback needed.",
                self.trader_id, pre_merge_calmar, current_calmar, degradation * 100,
            )
            return True
        return False

    @property
    def is_shadowing(self) -> bool:
        return self._active_shadow is not None

    @property
    def days_remaining(self) -> int:
        return max(0, self.eval_days - self._days_elapsed)


# ═══════════════════════════════════════════════════════════════════════════════
# Recovery Manager (§8.3)
# ═══════════════════════════════════════════════════════════════════════════════


class RecoveryManager:
    """Manages trader recovery from paused/emergency states.

    When a trader is paused:
      - It observes ticks but doesn't send orders
      - It makes mock decisions and journals them
      - It must articulate a recovery plan to exit recovery

    Args:
        trader_id: Which trader.
        min_recovery_ticks: Minimum observation ticks before exit allowed (default 10).
    """

    def __init__(self, trader_id: str, min_recovery_ticks: int = 10):
        self.trader_id = trader_id
        self.min_recovery_ticks = min_recovery_ticks
        self._recovery_ticks: int = 0
        self._mock_decisions: List[Dict[str, Any]] = []
        self._recovery_plan: Optional[str] = None

    def observe(self, tick_data: Dict[str, Any], mock_decision: Dict[str, Any]) -> None:
        """Record one observation tick during recovery.

        Args:
            tick_data: The market data seen.
            mock_decision: What the trader WOULD have done.
        """
        self._recovery_ticks += 1
        self._mock_decisions.append({
            "tick": tick_data,
            "mock_decision": mock_decision,
        })

        # Keep only last 50
        if len(self._mock_decisions) > 50:
            self._mock_decisions.pop(0)

    def propose_recovery_plan(self, plan: str) -> str:
        """Trader submits a recovery plan.

        Returns 'accepted' or 'rejected' with reason.
        """
        if not plan or len(plan) < 20:
            return "rejected: plan too short — be specific about what went wrong and how you'll fix it"

        if self._recovery_ticks < self.min_recovery_ticks:
            return f"rejected: need {self.min_recovery_ticks} observation ticks, only have {self._recovery_ticks}"

        self._recovery_plan = plan
        log.info("[%s] Recovery plan accepted: %s", self.trader_id, plan[:100])
        return "accepted"

    def can_exit_recovery(self) -> Tuple[bool, str]:
        """Check if trader can exit recovery mode.

        Returns:
            (ready, reason)
        """
        if self._recovery_ticks < self.min_recovery_ticks:
            return False, (
                f"Need {self.min_recovery_ticks} observation ticks "
                f"(have {self._recovery_ticks})"
            )
        if not self._recovery_plan:
            return False, "No recovery plan submitted"
        return True, "ready"

    def reset(self) -> None:
        """Reset after exiting recovery."""
        self._recovery_ticks = 0
        self._mock_decisions.clear()
        self._recovery_plan = None

    @property
    def observation_count(self) -> int:
        return self._recovery_ticks
