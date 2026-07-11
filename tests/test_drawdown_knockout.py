#!/usr/bin/env python3
"""Tests for drawdown_knockout — drawdown knockout circuit breaker (SPEC §8)."""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from src.drawdown_knockout import (
    KnockoutLevel,
    KnockoutState,
    KnockoutStateMachine,
    DrawdownKnockoutGate,
    DrawdownKnockout,
    inject_knockout_gate,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _make_state(
    level: KnockoutLevel = KnockoutLevel.NORMAL,
    dd: float = 0.0,
    peak: float = 100000.0,
    consecutive_losses: int = 0,
    cool_off_remaining: int = 0,
    paused_at=None,
    emergency_at=None,
    recovery_plan: str | None = None,
    recovery_ticks: int = 0,
) -> KnockoutState:
    return KnockoutState(
        level=level,
        current_drawdown=dd,
        peak_equity=peak,
        consecutive_losses=consecutive_losses,
        cooling_off_signals_to_skip=cool_off_remaining,
        paused_at=paused_at,
        emergency_at=emergency_at,
        recovery_plan=recovery_plan,
        recovery_observation_ticks=recovery_ticks,
    )


_SHARED_NOW = datetime(2026, 7, 11, 10, 0, 0)

machine_default = KnockoutStateMachine()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. KnockoutLevel enum
# ═══════════════════════════════════════════════════════════════════════════════


class TestKnockoutLevel:
    """Cover all 4 tiers + cooling_off — can_trade, position_multiplier, allow_exits."""

    # ── can_trade ──────────────────────────────────────────────────────

    def test_normal_can_trade(self):
        assert KnockoutLevel.NORMAL.can_trade is True

    def test_reduced_can_trade(self):
        assert KnockoutLevel.REDUCED.can_trade is True

    def test_paused_cannot_trade(self):
        assert KnockoutLevel.PAUSED.can_trade is False

    def test_emergency_cannot_trade(self):
        assert KnockoutLevel.EMERGENCY.can_trade is False

    def test_cooling_off_cannot_trade(self):
        assert KnockoutLevel.COOLING_OFF.can_trade is False

    # ── can_open_new_positions ─────────────────────────────────────────

    def test_normal_can_open_new(self):
        assert KnockoutLevel.NORMAL.can_open_new_positions is True

    def test_reduced_cannot_open_new(self):
        assert KnockoutLevel.REDUCED.can_open_new_positions is False

    def test_paused_cannot_open_new(self):
        assert KnockoutLevel.PAUSED.can_open_new_positions is False

    def test_emergency_cannot_open_new(self):
        assert KnockoutLevel.EMERGENCY.can_open_new_positions is False

    def test_cooling_off_cannot_open_new(self):
        assert KnockoutLevel.COOLING_OFF.can_open_new_positions is False

    # ── position_multiplier ────────────────────────────────────────────

    def test_normal_multiplier(self):
        assert KnockoutLevel.NORMAL.position_multiplier == 1.0

    def test_reduced_multiplier(self):
        assert KnockoutLevel.REDUCED.position_multiplier == 0.5

    def test_paused_multiplier(self):
        assert KnockoutLevel.PAUSED.position_multiplier == 0.0

    def test_emergency_multiplier(self):
        assert KnockoutLevel.EMERGENCY.position_multiplier == 0.0

    def test_cooling_off_multiplier(self):
        assert KnockoutLevel.COOLING_OFF.position_multiplier == 0.0

    # ── allow_exits ────────────────────────────────────────────────────

    def test_normal_allow_exits(self):
        assert KnockoutLevel.NORMAL.allow_exits is True

    def test_reduced_allow_exits(self):
        assert KnockoutLevel.REDUCED.allow_exits is True

    def test_paused_allow_exits(self):
        assert KnockoutLevel.PAUSED.allow_exits is True

    def test_emergency_no_exits(self):
        assert KnockoutLevel.EMERGENCY.allow_exits is False

    def test_cooling_off_allow_exits(self):
        assert KnockoutLevel.COOLING_OFF.allow_exits is True


# ═══════════════════════════════════════════════════════════════════════════════
# 2. KnockoutState
# ═══════════════════════════════════════════════════════════════════════════════


class TestKnockoutState:
    """Properties delegate to level; to_dict works."""

    def test_properties_delegate_to_level(self):
        state = _make_state(level=KnockoutLevel.EMERGENCY, dd=0.20, peak=1000.0)
        assert state.can_trade is False
        assert state.can_open_new_positions is False
        assert state.position_multiplier == 0.0
        assert state.allow_exits is False

    def test_to_dict_returns_all_keys(self):
        state = _make_state(
            level=KnockoutLevel.PAUSED, dd=0.12, peak=50000.0,
            consecutive_losses=2, cool_off_remaining=0,
            paused_at=_SHARED_NOW,
        )
        d = state.to_dict()
        assert d["level"] == "paused"
        assert d["current_drawdown_pct"] == 12.0
        assert d["peak_equity"] == 50000.0
        assert d["consecutive_losses"] == 2
        assert d["cooling_off_remaining"] == 0
        assert d["can_trade"] is False
        assert d["can_open_new_positions"] is False
        assert d["position_multiplier"] == 0.0
        assert d["allow_exits"] is True
        assert d["paused_at"] is not None
        assert d["emergency_at"] is None
        assert d["has_recovery_plan"] is False
        assert d["recovery_observation_ticks"] == 0

    def test_to_dict_no_dates(self):
        state = _make_state()
        d = state.to_dict()
        assert d["paused_at"] is None
        assert d["emergency_at"] is None

    def test_to_dict_recovery_plan_present(self):
        state = _make_state(recovery_plan="reduce leverage to 1x")
        d = state.to_dict()
        assert d["has_recovery_plan"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# 3. KnockoutStateMachine — pure state machine
# ═══════════════════════════════════════════════════════════════════════════════


class TestStateMachine:
    """Test all 4 tiers + cooling-off + recovery from paused + sticky emergency."""

    # ── NORMAL ─────────────────────────────────────────────────────────

    def test_compute_normal_below_caution(self):
        state = _make_state(peak=100000.0)
        level, new = machine_default.compute(state, equity=98000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.NORMAL
        dd = (100000 - 98000) / 100000
        assert new.current_drawdown == pytest.approx(dd)

    def test_compute_normal_at_zero_drawdown(self):
        state = _make_state(peak=50000.0)
        level, new = machine_default.compute(state, equity=50000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.NORMAL
        assert new.current_drawdown == 0.0

    def test_compute_normal_peak_rises(self):
        state = _make_state(peak=100000.0)
        level, new = machine_default.compute(state, equity=110000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.NORMAL
        assert new.peak_equity == 110000.0
        assert new.current_drawdown == 0.0

    # ── REDUCED ────────────────────────────────────────────────────────

    def test_compute_reduced_at_caution_threshold(self):
        """5% drawdown → REDUCED."""
        state = _make_state(peak=100000.0)
        level, new = machine_default.compute(state, equity=95000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.REDUCED
        assert new.current_drawdown == pytest.approx(0.05)

    def test_compute_reduced_below_pause(self):
        """8% drawdown → REDUCED (not paused)."""
        state = _make_state(peak=100000.0)
        level, new = machine_default.compute(state, equity=92000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.REDUCED
        assert 0.05 < new.current_drawdown < 0.10

    def test_compute_reduced_precise_boundary(self):
        """5.0% drawdown is exactly reduced."""
        state = _make_state(peak=100000.0)
        level, new = machine_default.compute(state, equity=95000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.REDUCED

    # ── PAUSED ─────────────────────────────────────────────────────────

    def test_compute_paused_at_pause_threshold(self):
        """10% drawdown → PAUSED."""
        state = _make_state(peak=100000.0)
        level, new = machine_default.compute(state, equity=90000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.PAUSED

    def test_compute_paused_below_emergency(self):
        """12% drawdown → PAUSED."""
        state = _make_state(peak=100000.0)
        level, new = machine_default.compute(state, equity=88000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.PAUSED

    # ── EMERGENCY ──────────────────────────────────────────────────────

    def test_compute_emergency_at_threshold(self):
        """15% drawdown → EMERGENCY."""
        state = _make_state(peak=100000.0)
        level, new = machine_default.compute(state, equity=85000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.EMERGENCY

    def test_compute_emergency_above_threshold(self):
        """20% drawdown → EMERGENCY."""
        state = _make_state(peak=100000.0)
        level, new = machine_default.compute(state, equity=80000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.EMERGENCY

    def test_compute_emergency_sticky_does_not_exit(self):
        """Once EMERGENCY, stays EMERGENCY even if equity recovers."""
        state = _make_state(
            level=KnockoutLevel.EMERGENCY, peak=100000.0, dd=0.20,
            emergency_at=_SHARED_NOW,
        )
        # Equity recovers to 99K (only 1% drawdown), but still EMERGENCY
        level, new = machine_default.compute(state, equity=99000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.EMERGENCY, (
            "EMERGENCY is sticky even when drawdown recovers"
        )

    # ── Sequential transitions ─────────────────────────────────────────

    def test_sequential_transitions_normal_to_reduced_to_paused(self):
        """As drawdown worsens, level progresses through tiers."""
        state = _make_state(peak=100000.0)

        level, state = machine_default.compute(state, equity=93000.0, now=_SHARED_NOW)  # 7% DD
        assert level == KnockoutLevel.REDUCED

        level, state = machine_default.compute(state, equity=87000.0, now=_SHARED_NOW)  # 13% DD
        assert level == KnockoutLevel.PAUSED

    def test_sequential_transitions_through_all_tiers(self):
        """Normal → Reduced → Paused → Emergency."""
        state = _make_state(peak=100000.0)

        # 3% DD → Normal
        level, state = machine_default.compute(state, equity=97000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.NORMAL

        # 8% DD → Reduced
        level, state = machine_default.compute(state, equity=92000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.REDUCED

        # 12% DD → Paused
        level, state = machine_default.compute(state, equity=88000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.PAUSED

        # 20% DD → Emergency
        level, state = machine_default.compute(state, equity=80000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.EMERGENCY

    def test_equity_recovery_brings_back_to_normal(self):
        """Non-sticky tiers (reduced/paused) recover when equity returns."""
        state = _make_state(peak=100000.0)

        # Drop to 85K (15% Emergency — sticky)
        level, state = machine_default.compute(state, equity=85000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.EMERGENCY

        # But if we start from Reduced (not Emergency), recovery works
        state2 = _make_state(peak=100000.0)
        level2, state2 = machine_default.compute(state2, equity=92000.0, now=_SHARED_NOW)
        assert level2 == KnockoutLevel.REDUCED

        # Recover back above caution
        level3, state3 = machine_default.compute(state2, equity=97000.0, now=_SHARED_NOW)
        assert level3 == KnockoutLevel.NORMAL

    # ── COOLING OFF ────────────────────────────────────────────────────

    def test_cooling_off_triggers_on_three_consecutive_losses(self):
        """3 consecutive losses → COOLING_OFF level."""
        state = _make_state(peak=100000.0, consecutive_losses=2)
        # 3rd consecutive loss
        level, new = machine_default.compute(
            state, equity=98000.0, last_trade_pnl=-500.0, now=_SHARED_NOW,
        )
        assert level == KnockoutLevel.COOLING_OFF
        assert new.consecutive_losses == 3

    def test_cooling_off_not_triggered_with_win(self):
        """A winning trade resets consecutive losses counter."""
        state = _make_state(peak=100000.0, consecutive_losses=2)
        level, new = machine_default.compute(
            state, equity=98000.0, last_trade_pnl=200.0, now=_SHARED_NOW,
        )
        assert level == KnockoutLevel.NORMAL
        assert new.consecutive_losses == 0

    def test_cooling_off_skips_signals(self):
        """When cooling_off_signals_to_skip > 0, remains COOLING_OFF."""
        state = _make_state(peak=100000.0, cool_off_remaining=2)
        level, new = machine_default.compute(state, equity=98000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.COOLING_OFF
        assert new.cooling_off_signals_to_skip == 1  # decremented

    def test_cooling_off_decays_over_ticks(self):
        """Skip counter decrements each tick; returns to normal after exhausted."""
        state = _make_state(peak=100000.0, cool_off_remaining=1)

        # Tick 1: still cooling off, counter hits 0
        level, state = machine_default.compute(state, equity=98000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.COOLING_OFF
        assert state.cooling_off_signals_to_skip == 0

        # Tick 2: cooling off exhausted, back to normal
        level, state = machine_default.compute(state, equity=98000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.NORMAL

    def test_cooling_off_and_emergency_emergency_wins(self):
        """Emergency threshold overrides cooling-off."""
        state = _make_state(peak=100000.0, consecutive_losses=3)
        # Even with max losses, huge drawdown → EMERGENCY
        level, new = machine_default.compute(state, equity=80000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.EMERGENCY

    # ── Recovery from PAUSED ───────────────────────────────────────────

    def test_recovery_plan_required_to_exit_paused(self):
        """No recovery plan → stays paused even if DD < 10% and < 5%."""
        state = _make_state(
            level=KnockoutLevel.PAUSED,
            peak=100000.0, dd=0.04,  # DD is 4%, well below all thresholds
            recovery_plan=None,
            recovery_ticks=20,
            paused_at=_SHARED_NOW,
        )
        level, new = machine_default.compute(state, equity=96000.0, now=_SHARED_NOW)
        # No plan → stays PAUSED
        assert level == KnockoutLevel.PAUSED, (
            "No recovery plan → stay paused even below threshold"
        )

    def test_recovery_needs_min_observation_ticks(self):
        """Recovery plan submitted but not enough observation ticks → stays paused."""
        state = _make_state(
            level=KnockoutLevel.PAUSED,
            peak=100000.0, dd=0.04,
            recovery_plan="Reduce leverage and tighten stops",
            recovery_ticks=3,  # below default 10
            paused_at=_SHARED_NOW,
        )
        level, new = machine_default.compute(state, equity=96000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.PAUSED

    def test_recovery_paused_exits_to_normal_when_dd_below_caution(self):
        """All conditions met and DD < 5% → exits to NORMAL."""
        state = _make_state(
            level=KnockoutLevel.PAUSED,
            peak=100000.0, dd=0.04,  # 4% drawdown
            recovery_plan="Reduce leverage and tighten stops",
            recovery_ticks=15,
            paused_at=_SHARED_NOW,
        )
        level, new = machine_default.compute(state, equity=96000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.NORMAL, (
            f"Expected NORMAL, got {level} (DD must be < 5% to exit paused)"
        )

    def test_recovery_paused_stays_paused_if_dd_above_caution(self):
        """All conditions met but DD still > 5% → stays PAUSED (must get below caution)."""
        state = _make_state(
            level=KnockoutLevel.PAUSED,
            peak=100000.0, dd=0.08,  # 8% drawdown > 5% caution
            recovery_plan="Reduce leverage and tighten stops",
            recovery_ticks=15,
            paused_at=_SHARED_NOW,
        )
        level, new = machine_default.compute(state, equity=92000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.PAUSED, (
            f"DD=8% (>5% caution) should keep PAUSED; got {level}"
        )

    # ── Edge cases ─────────────────────────────────────────────────────

    def test_drawdown_when_peak_is_zero(self):
        """Edge case: peak_equity starts at 0 (not yet initialized)."""
        state = _make_state(peak=0.0)
        level, new = machine_default.compute(state, equity=100000.0, now=_SHARED_NOW)
        assert level == KnockoutLevel.NORMAL
        assert new.current_drawdown == 0.0

    def test_no_last_trade_pnl_leaves_consecutive_losses_unchanged(self):
        """When last_trade_pnl is None, consecutive losses are not modified."""
        state = _make_state(consecutive_losses=2)
        level, new = machine_default.compute(state, equity=98000.0, now=_SHARED_NOW)
        assert new.consecutive_losses == 2  # unchanged

    def test_cooling_off_mixed_with_reduced_levels(self):
        """Drawdown level returned is COOLING_OFF even when DD is 8%."""
        state = _make_state(peak=100000.0, consecutive_losses=3)
        level, new = machine_default.compute(
            state, equity=92000.0, last_trade_pnl=-200.0, now=_SHARED_NOW,
        )
        # Cooling-off takes priority over REDUCED
        assert level == KnockoutLevel.COOLING_OFF


# ═══════════════════════════════════════════════════════════════════════════════
# 4. DrawdownKnockoutGate — rejection/acceptance logic
# ═══════════════════════════════════════════════════════════════════════════════


class TestDrawdownKnockoutGate:
    """Gate protocol: check(context, action) → (granted, reason)."""

    def make_gate(self) -> DrawdownKnockoutGate:
        return DrawdownKnockoutGate()

    def make_context(self, level: KnockoutLevel, dd: float = 0.0, peak: float = 100000.0) -> dict:
        return {"knockout_state": _make_state(level=level, dd=dd, peak=peak)}

    # ── No state → pass-through ────────────────────────────────────────

    def test_no_knockout_state_passthrough(self):
        gate = self.make_gate()
        granted, reason = gate.check({}, {"type": "BUY", "ticker": "AAPL"})
        assert granted is True
        assert "no knockout state" in reason

    # ── HOLD always allowed ────────────────────────────────────────────

    def test_hold_always_allowed(self):
        gate = self.make_gate()
        context = self.make_context(KnockoutLevel.EMERGENCY)
        granted, reason = gate.check(context, {"type": "HOLD"})
        assert granted is True
        assert "HOLD always allowed" in reason

    # ── NORMAL ─────────────────────────────────────────────────────────

    def test_normal_allows_buy(self):
        gate = self.make_gate()
        context = self.make_context(KnockoutLevel.NORMAL)
        granted, reason = gate.check(context, {"type": "BUY", "ticker": "AAPL"})
        assert granted is True
        assert "NORMAL" in reason

    def test_normal_allows_sell(self):
        gate = self.make_gate()
        context = self.make_context(KnockoutLevel.NORMAL)
        granted, reason = gate.check(context, {"type": "SELL", "ticker": "AAPL"})
        assert granted is True

    # ── REDUCED ────────────────────────────────────────────────────────

    def test_reduced_allows_buy_with_reason(self):
        gate = self.make_gate()
        context = self.make_context(KnockoutLevel.REDUCED, dd=0.08)
        granted, reason = gate.check(context, {"type": "BUY", "ticker": "TSLA"})
        assert granted is True
        assert "REDUCED" in reason
        assert "50%" in reason  # position sizing at 50%

    def test_reduced_allows_sell(self):
        gate = self.make_gate()
        context = self.make_context(KnockoutLevel.REDUCED)
        granted, reason = gate.check(context, {"type": "SELL", "ticker": "TSLA"})
        assert granted is True

    # ── PAUSED ─────────────────────────────────────────────────────────

    def test_paused_rejects_buy(self):
        gate = self.make_gate()
        context = self.make_context(KnockoutLevel.PAUSED, dd=0.12)
        granted, reason = gate.check(context, {"type": "BUY", "ticker": "AAPL"})
        assert granted is False
        assert "PAUSED" in reason.upper()
        assert "no new positions" in reason

    def test_paused_allows_sell(self):
        gate = self.make_gate()
        context = self.make_context(KnockoutLevel.PAUSED, dd=0.12)
        granted, reason = gate.check(context, {"type": "SELL", "ticker": "AAPL"})
        assert granted is True
        assert "SELL" in reason
        assert "exits permitted" in reason

    # ── COOLING_OFF ────────────────────────────────────────────────────

    def test_cooling_off_rejects_buy(self):
        gate = self.make_gate()
        context = self.make_context(KnockoutLevel.COOLING_OFF, dd=0.06)
        granted, reason = gate.check(context, {"type": "BUY", "ticker": "AAPL"})
        assert granted is False
        assert "COOLING" in reason.upper()
        assert "no new positions" in reason

    def test_cooling_off_allows_sell(self):
        gate = self.make_gate()
        context = self.make_context(KnockoutLevel.COOLING_OFF, dd=0.06)
        granted, reason = gate.check(context, {"type": "SELL", "ticker": "AAPL"})
        assert granted is True
        assert "exits permitted" in reason

    # ── EMERGENCY ──────────────────────────────────────────────────────

    def test_emergency_rejects_buy(self):
        gate = self.make_gate()
        context = self.make_context(KnockoutLevel.EMERGENCY, dd=0.20)
        granted, reason = gate.check(context, {"type": "BUY", "ticker": "AAPL"})
        assert granted is False
        assert "EMERGENCY" in reason
        assert "frozen" in reason
        assert "Human must re-enable" in reason

    def test_emergency_rejects_sell(self):
        """During EMERGENCY, even SELLs are rejected — positions frozen."""
        gate = self.make_gate()
        context = self.make_context(KnockoutLevel.EMERGENCY, dd=0.20)
        granted, reason = gate.check(context, {"type": "SELL", "ticker": "AAPL"})
        assert granted is False
        assert "frozen" in reason

    # ── Action dict key flexibility ────────────────────────────────────

    def test_action_can_use_action_key(self):
        """Gate handles both 'type' and 'action' keys."""
        gate = self.make_gate()
        context = self.make_context(KnockoutLevel.PAUSED)
        granted, reason = gate.check(context, {"action": "BUY"})
        assert granted is False

    def test_action_lowercase_accepted(self):
        gate = self.make_gate()
        context = self.make_context(KnockoutLevel.NORMAL)
        granted, reason = gate.check(context, {"type": "buy", "ticker": "AAPL"})
        assert granted is True  # .upper() normalizes


# ═══════════════════════════════════════════════════════════════════════════════
# 5. DrawdownKnockout — orchestrator (update, transitions, recovery, re_enable)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def dk():
    """Default DrawdownKnockout instance."""
    return DrawdownKnockout(trader_id="test-trader")


@pytest.fixture
def dk_with_alerts():
    """DrawdownKnockout with patched alert/metrics for transition tests."""
    with patch("src.drawdown_knockout.alert") as mock_alert, \
         patch("src.drawdown_knockout.metrics") as mock_metrics:
        dk_inst = DrawdownKnockout(trader_id="test-trader")
        dk_inst._alert = mock_alert
        dk_inst._metrics = mock_metrics
        yield dk_inst


class TestDrawdownKnockout:
    """Integration tests for the full orchestrator."""

    # ── Initial state ──────────────────────────────────────────────────

    def test_initial_state(self, dk):
        assert dk.state.level == KnockoutLevel.NORMAL
        assert dk.state.current_drawdown == 0.0
        assert dk.state.peak_equity == 0.0
        assert dk.state.consecutive_losses == 0
        assert dk.state.cooling_off_signals_to_skip == 0
        assert dk.gate is not None
        assert dk._total_transitions == 0

    def test_repr(self, dk):
        r = repr(dk)
        assert "test-trader" in r
        assert "normal" in r

    # ── NORMAL trading ─────────────────────────────────────────────────

    def test_update_normal(self, dk):
        dk.update(equity=100000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.NORMAL
        assert dk.state.peak_equity == 100000.0

    def test_update_peak_rises(self, dk):
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=110000.0, now=_SHARED_NOW)
        assert dk.state.peak_equity == 110000.0
        assert dk.state.current_drawdown == 0.0

    def test_update_small_drawdown_stays_normal(self, dk):
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=97000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.NORMAL
        assert dk.state.current_drawdown == pytest.approx(0.03)

    # ── REDUCED ────────────────────────────────────────────────────────

    def test_update_reduced(self, dk):
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=92000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.REDUCED
        assert dk.state.position_multiplier == 0.5
        assert dk.state.allow_exits is True

    def test_update_reduced_transition_count(self, dk):
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=92000.0, now=_SHARED_NOW)
        assert dk._total_transitions == 1

    # ── PAUSED ─────────────────────────────────────────────────────────

    def test_update_paused(self, dk):
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=88000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.PAUSED
        assert dk.state.position_multiplier == 0.0
        assert dk.state.allow_exits is True
        assert dk.state.paused_at is not None

    def test_update_paused_increments_recovery_ticks(self, dk):
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=88000.0, now=_SHARED_NOW)
        ticks_before = dk.state.recovery_observation_ticks
        dk.update(equity=87000.0, now=_SHARED_NOW)
        assert dk.state.recovery_observation_ticks == ticks_before + 1

    # ── EMERGENCY ──────────────────────────────────────────────────────

    def test_update_emergency(self, dk):
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=80000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.EMERGENCY
        assert dk.state.allow_exits is False
        assert dk.state.position_multiplier == 0.0
        assert dk.state.emergency_at is not None

    def test_emergency_is_sticky(self, dk):
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=80000.0, now=_SHARED_NOW)
        # Recovery to 99K → still EMERGENCY
        dk.update(equity=99000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.EMERGENCY

    def test_emergency_status_dict(self, dk):
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=80000.0, now=_SHARED_NOW)
        status = dk.emergency_status()
        assert status["freeze_positions"] is True
        assert status["alert_required"] is True
        assert status["total_transitions"] == 1
        assert status["trader_id"] == "test-trader"
        assert "thresholds" in status

    # ── Sequential transitions ─────────────────────────────────────────

    def test_full_transition_chain(self, dk):
        """NORMAL → REDUCED → PAUSED → EMERGENCY."""
        dk.update(equity=100000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.NORMAL

        dk.update(equity=92000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.REDUCED

        dk.update(equity=88000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.PAUSED

        dk.update(equity=80000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.EMERGENCY
        assert dk._total_transitions == 3

    def test_transition_reduced_back_to_normal(self, dk):
        """REDUCED recovers to NORMAL when equity comes back."""
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=92000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.REDUCED
        assert dk._total_transitions == 1

        # Recover above caution threshold
        dk.update(equity=98000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.NORMAL
        assert dk._total_transitions == 2

    # ── Synthetic 20% drawdown triggers position freeze ────────────────

    def test_synthetic_20_percent_drawdown_freeze(self, dk):
        """20% drawdown → EMERGENCY → positions frozen."""
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=80000.0, now=_SHARED_NOW)  # 20% drawdown
        assert dk.state.level == KnockoutLevel.EMERGENCY
        assert dk.state.allow_exits is False
        assert dk.state.position_multiplier == 0.0

        # Gate rejects all trades
        context = {"knockout_state": dk.state}
        granted_buy, _ = dk.gate.check(context, {"type": "BUY", "ticker": "AAPL"})
        granted_sell, _ = dk.gate.check(context, {"type": "SELL", "ticker": "AAPL"})
        assert granted_buy is False
        assert granted_sell is False

    # ── Cooling-off via orchestrator ───────────────────────────────────

    def test_cooling_off_triggered_via_update(self, dk):
        """3 losing trades → cooling off → next 2 signals skipped."""
        dk.update(equity=100000.0, now=_SHARED_NOW)  # peak = 100K

        # 3 consecutive losses
        dk.update(equity=98000.0, last_trade_pnl=-200.0, now=_SHARED_NOW)  # loss 1
        assert dk.state.consecutive_losses == 1

        dk.update(equity=96000.0, last_trade_pnl=-200.0, now=_SHARED_NOW)  # loss 2
        assert dk.state.consecutive_losses == 2

        dk.update(equity=94000.0, last_trade_pnl=-200.0, now=_SHARED_NOW)  # loss 3 → cooling off
        assert dk.state.consecutive_losses == 3
        assert dk.state.level == KnockoutLevel.COOLING_OFF
        assert dk.state.cooling_off_signals_to_skip == 2

    def test_cooling_off_decays_each_tick(self, dk):
        """Cooling off skip counter decrements each tick; exits on a winning trade."""
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=98000.0, last_trade_pnl=-200.0, now=_SHARED_NOW)
        dk.update(equity=96000.0, last_trade_pnl=-200.0, now=_SHARED_NOW)
        dk.update(equity=94000.0, last_trade_pnl=-200.0, now=_SHARED_NOW)  # loss 3 → cooling off
        assert dk.state.cooling_off_signals_to_skip == 2

        # Tick 1: skip → remaining 1
        dk.update(equity=93000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.COOLING_OFF
        assert dk.state.cooling_off_signals_to_skip == 1

        # Tick 2: skip → remaining 0; consecutive_losses still 3 so re-enters COOLING_OFF
        dk.update(equity=92000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.COOLING_OFF
        assert dk.state.cooling_off_signals_to_skip == 0

        # A winning trade resets consecutive losses, exiting cooling-off
        dk.update(equity=93000.0, last_trade_pnl=500.0, now=_SHARED_NOW)
        assert dk.state.consecutive_losses == 0
        assert dk.state.level != KnockoutLevel.COOLING_OFF

    # ── Recovery from paused ──────────────────────────────────────────

    def test_full_recovery_from_paused(self, dk):
        """Paused + observe ticks + submit plan + DD < 5% → exit paused."""
        dk.update(equity=100000.0, now=_SHARED_NOW)

        # Hit 12% drawdown → PAUSED
        dk.update(equity=88000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.PAUSED

        # Observe for enough ticks (need 10)
        for i in range(12):
            dk.update(equity=86000.0, now=_SHARED_NOW)
        assert dk.state.recovery_observation_ticks >= 10

        # Submit recovery plan
        result = dk.submit_recovery_plan(
            "Reduce position sizing by 50% and tighten stop losses"
        )
        assert result == "accepted"
        assert dk.state.recovery_plan is not None

        # Recovery with DD back to 4% → should exit PAUSED to NORMAL
        dk.update(equity=96000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.NORMAL

    def test_recovery_from_paused_requires_min_ticks(self, dk):
        """Submit plan too early → rejected; need enough observation ticks."""
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=88000.0, now=_SHARED_NOW)

        # Only 1 tick in paused
        result = dk.submit_recovery_plan("Reduce leverage and tighten stops")
        assert "rejected" in result
        assert "observation ticks" in result
        # Should still be PAUSED
        assert dk.state.level == KnockoutLevel.PAUSED

    def test_recovery_plan_not_in_paused_rejected(self, dk):
        """Submit recovery plan while not PAUSED → rejected."""
        dk.update(equity=100000.0, now=_SHARED_NOW)
        result = dk.submit_recovery_plan("Reduce leverage and tighten stops")
        assert "rejected" in result
        assert "not in PAUSED mode" in result

    def test_recovery_plan_too_short_rejected(self, dk):
        """Short recovery plan → rejected."""
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=88000.0, now=_SHARED_NOW)

        # Wait enough ticks
        for i in range(15):
            dk.update(equity=86000.0, now=_SHARED_NOW)

        result = dk.submit_recovery_plan("Fix it")
        assert "rejected" in result
        assert "too short" in result

    # ── Human re_enable from emergency ────────────────────────────────

    def test_re_enable_from_emergency(self, dk):
        """re_enable() resets state from EMERGENCY to NORMAL."""
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=80000.0, now=_SHARED_NOW)
        assert dk.state.level == KnockoutLevel.EMERGENCY

        dk.re_enable()
        assert dk.state.level == KnockoutLevel.NORMAL
        assert dk.state.current_drawdown == 0.0
        assert dk.state.peak_equity == 0.0  # resets — next update sets peak
        assert dk.state.consecutive_losses == 0
        assert dk.state.cooling_off_signals_to_skip == 0
        assert dk.state.emergency_at is None
        assert dk.state.paused_at is None

    def test_re_enable_resets_peak_on_next_update(self, dk):
        """After re_enable, next update resets peak equity."""
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=80000.0, now=_SHARED_NOW)
        dk.re_enable()

        dk.update(equity=50000.0, now=_SHARED_NOW)
        assert dk.state.peak_equity == 50000.0  # new baseline
        assert dk.state.current_drawdown == 0.0  # no drawdown from new peak

    def test_re_enable_not_in_emergency_warns(self, dk):
        """re_enable() called during NORMAL logs a warning but still resets."""
        with patch("src.drawdown_knockout.log") as mock_log:
            dk.re_enable()
            # log.warning called twice: once for "not in EMERGENCY", once for "RE-ENABLED"
            assert mock_log.warning.call_count == 2
            args_0 = str(mock_log.warning.call_args_list[0])
            assert "not in EMERGENCY" in args_0

        # State still reset
        assert dk.state.level == KnockoutLevel.NORMAL

    def test_re_enable_clears_recovery_state(self, dk):
        """re_enable() erases recovery-related state."""
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=88000.0, now=_SHARED_NOW)
        for i in range(12):
            dk.update(equity=86000.0, now=_SHARED_NOW)
        dk.submit_recovery_plan("Reduce leverage and tighten stops")
        # Now hit emergency
        dk.update(equity=80000.0, now=_SHARED_NOW)

        dk.re_enable()
        assert dk.state.recovery_plan is None
        assert dk.state.recovery_observation_ticks == 0

    # ── Journal ────────────────────────────────────────────────────────

    def test_journal_updated_on_transition(self, dk):
        """Each transition adds an entry to the journal."""
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=92000.0, now=_SHARED_NOW)
        assert len(dk.state.journal) == 1
        assert "normal → reduced" in dk.state.journal[0]

    def test_journal_updated_on_cooling_off(self, dk):
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=98000.0, last_trade_pnl=-200.0, now=_SHARED_NOW)
        dk.update(equity=96000.0, last_trade_pnl=-200.0, now=_SHARED_NOW)
        dk.update(equity=94000.0, last_trade_pnl=-200.0, now=_SHARED_NOW)
        journal = "\n".join(dk.state.journal)
        assert "COOLING OFF" in journal

    def test_journal_updated_on_recovery_plan(self, dk):
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=88000.0, now=_SHARED_NOW)
        for i in range(12):
            dk.update(equity=86000.0, now=_SHARED_NOW)
        dk.submit_recovery_plan("Reduce leverage and tighten stops")
        assert any("Recovery plan" in e for e in dk.state.journal)

    def test_journal_updated_on_re_enable(self, dk):
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=80000.0, now=_SHARED_NOW)
        dk.re_enable()
        assert any("RE-ENABLED" in e for e in dk.state.journal)

    # ── Equity history ─────────────────────────────────────────────────

    def test_equity_history(self, dk):
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=95000.0, now=_SHARED_NOW)
        assert len(dk.state.equity_history) == 2
        assert dk.state.equity_history == [100000.0, 95000.0]

    def test_equity_history_capped(self, dk):
        for i in range(1050):
            dk.update(equity=float(100000 - i), now=_SHARED_NOW)
        # Should be truncated to last ~500 (1000 entries kept, then trim to 500)
        assert len(dk.state.equity_history) <= 600

    # ── Emergency status ───────────────────────────────────────────────

    def test_emergency_status_recovery_ready(self, dk):
        dk.update(equity=100000.0, now=_SHARED_NOW)
        dk.update(equity=88000.0, now=_SHARED_NOW)
        for i in range(12):
            dk.update(equity=86000.0, now=_SHARED_NOW)
        status = dk.emergency_status()
        assert status["recovery_ready"] is True
        assert status["alert_required"] is True

    def test_emergency_status_normal(self, dk):
        dk.update(equity=100000.0, now=_SHARED_NOW)
        status = dk.emergency_status()
        assert status["freeze_positions"] is False
        assert status["alert_required"] is False
        assert status["recovery_ready"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# 6. inject_knockout_gate
# ═══════════════════════════════════════════════════════════════════════════════


class TestInjectKnockoutGate:
    """Test injection helper for wiring into RiskManager."""

    def test_inject_with_existing_knockout(self):
        """Can inject an existing DrawdownKnockout into RiskManager."""
        from src.risk.manager import RiskManager

        rm = RiskManager(gates=[])
        dk = DrawdownKnockout(trader_id="test-trader")
        result_dk, result_rm = inject_knockout_gate(rm, knockout=dk)

        assert result_dk is dk
        assert dk.gate is result_rm.gates[0]

    def test_inject_creates_new_knockout_with_trader_id(self):
        """Creates new DrawdownKnockout when none provided."""
        from src.risk.manager import RiskManager

        rm = RiskManager(gates=[])
        result_dk, result_rm = inject_knockout_gate(rm, trader_id="new-trader")

        assert result_dk.trader_id == "new-trader"
        assert result_dk.gate is result_rm.gates[0]

    def test_inject_raises_without_trader_id(self):
        """Must provide trader_id when creating a new knockout."""
        from src.risk.manager import RiskManager

        rm = RiskManager(gates=[])
        with pytest.raises(ValueError, match="trader_id"):
            inject_knockout_gate(rm)

    def test_inject_preserves_existing_gates(self):
        """Existing gates in the risk manager are preserved after knockout gate."""
        from src.risk.manager import RiskManager

        mock_gate = MagicMock()
        mock_gate.check.return_value = (True, "mock")
        rm = RiskManager(gates=[mock_gate])

        result_dk, result_rm = inject_knockout_gate(rm, trader_id="test")
        # Knockout gate should be first, then mock
        assert len(result_rm.gates) == 2
        assert result_rm.gates[0] is result_dk.gate
        assert result_rm.gates[1] is mock_gate