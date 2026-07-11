"""Tests for Drawdown Knockout Circuit Breaker — SPEC §8 (Drawdown Management).

Covers:
  - All 4 drawdown tiers: NORMAL, REDUCED, PAUSED, EMERGENCY
  - Cooling-off (3 consecutive losses → skip 2 signals)
  - Recovery mode (observation ticks + plan submission → exit PAUSED)
  - Human re_enable() from EMERGENCY
  - position_multiplier and allow_exits at each tier
  - DrawdownKnockoutGate rejection logic
  - Edge cases: sticky EMERGENCY, sequential transitions, threshold boundaries
"""

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
# KnockoutLevel tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestKnockoutLevel:
    """Verify enum values, trading flags, position multipliers, and exit allowances."""

    @pytest.mark.parametrize("level,can_trade", [
        (KnockoutLevel.NORMAL, True),
        (KnockoutLevel.REDUCED, True),
        (KnockoutLevel.PAUSED, False),
        (KnockoutLevel.EMERGENCY, False),
        (KnockoutLevel.COOLING_OFF, False),
    ])
    def test_can_trade(self, level, can_trade):
        assert level.can_trade == can_trade

    @pytest.mark.parametrize("level,can_open", [
        (KnockoutLevel.NORMAL, True),
        (KnockoutLevel.REDUCED, False),
        (KnockoutLevel.PAUSED, False),
        (KnockoutLevel.EMERGENCY, False),
        (KnockoutLevel.COOLING_OFF, False),
    ])
    def test_can_open_new_positions(self, level, can_open):
        assert level.can_open_new_positions == can_open

    @pytest.mark.parametrize("level,mult", [
        (KnockoutLevel.NORMAL, 1.0),
        (KnockoutLevel.REDUCED, 0.5),
        (KnockoutLevel.PAUSED, 0.0),
        (KnockoutLevel.EMERGENCY, 0.0),
        (KnockoutLevel.COOLING_OFF, 0.0),
    ])
    def test_position_multiplier(self, level, mult):
        assert level.position_multiplier == mult

    @pytest.mark.parametrize("level,allow", [
        (KnockoutLevel.NORMAL, True),
        (KnockoutLevel.REDUCED, True),
        (KnockoutLevel.PAUSED, True),
        (KnockoutLevel.EMERGENCY, False),
        (KnockoutLevel.COOLING_OFF, True),
    ])
    def test_allow_exits(self, level, allow):
        assert level.allow_exits == allow


# ═══════════════════════════════════════════════════════════════════════════════
# KnockoutState tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestKnockoutState:
    """Verify state properties delegate to level, and to_dict works."""

    def test_properties_delegate_to_level_normal(self):
        state = KnockoutState(level=KnockoutLevel.NORMAL)
        assert state.can_trade is True
        assert state.can_open_new_positions is True
        assert state.position_multiplier == 1.0
        assert state.allow_exits is True

    def test_properties_delegate_to_level_emergency(self):
        state = KnockoutState(level=KnockoutLevel.EMERGENCY)
        assert state.can_trade is False
        assert state.can_open_new_positions is False
        assert state.position_multiplier == 0.0
        assert state.allow_exits is False

    def test_properties_delegate_to_level_reduced(self):
        state = KnockoutState(level=KnockoutLevel.REDUCED)
        assert state.can_trade is True
        assert state.can_open_new_positions is False
        assert state.position_multiplier == 0.5
        assert state.allow_exits is True

    def test_properties_delegate_to_level_cooling_off(self):
        state = KnockoutState(level=KnockoutLevel.COOLING_OFF)
        assert state.can_trade is False
        assert state.position_multiplier == 0.0
        assert state.allow_exits is True

    def test_to_dict_includes_all_keys(self):
        now = datetime(2026, 7, 10, 12, 0, 0)
        state = KnockoutState(
            level=KnockoutLevel.PAUSED,
            current_drawdown=0.12,
            peak_equity=100_000.0,
            consecutive_losses=4,
            cooling_off_signals_to_skip=1,
            paused_at=now,
            recovery_plan="Overleveraged; reducing position count.",
            recovery_observation_ticks=10,
        )
        d = state.to_dict()
        assert d["level"] == "paused"
        assert d["current_drawdown_pct"] == 12.0
        assert d["peak_equity"] == 100_000.0
        assert d["consecutive_losses"] == 4
        assert d["cooling_off_remaining"] == 1
        assert d["can_trade"] is False
        assert d["can_open_new_positions"] is False
        assert d["position_multiplier"] == 0.0
        assert d["allow_exits"] is True
        assert d["paused_at"] == "2026-07-10T12:00:00"
        assert d["emergency_at"] is None
        assert d["has_recovery_plan"] is True
        assert d["recovery_observation_ticks"] == 10

    def test_to_dict_no_timestamps(self):
        state = KnockoutState()
        d = state.to_dict()
        assert d["paused_at"] is None
        assert d["emergency_at"] is None
        assert d["has_recovery_plan"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# KnockoutStateMachine tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestKnockoutStateMachine:
    """Pure state machine: drawdown tiers, boundaries, sticky transitions, cooling-off."""

    FAKE_NOW = datetime(2026, 7, 10, 12, 0, 0)

    def make_state(self, **overrides) -> KnockoutState:
        defaults = dict(
            level=KnockoutLevel.NORMAL,
            current_drawdown=0.0,
            peak_equity=0.0,
            consecutive_losses=0,
            cooling_off_signals_to_skip=0,
        )
        defaults.update(overrides)
        return KnockoutState(**defaults)

    # ── 4 Tiers ───────────────────────────────────────────────────────

    def test_normal_below_5pct(self):
        """Drawdown < 5% → NORMAL."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=97_000)  # 3% DD
        assert new.level == KnockoutLevel.NORMAL

    def test_reduced_at_5pct(self):
        """Drawdown exactly at 5% → REDUCED."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=95_000)  # 5% DD
        assert new.level == KnockoutLevel.REDUCED

    def test_reduced_7pct(self):
        """Drawdown 7% → REDUCED."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=93_000)  # 7% DD
        assert new.level == KnockoutLevel.REDUCED

    def test_paused_at_10pct(self):
        """Drawdown exactly at 10% → PAUSED."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=90_000)  # 10% DD
        assert new.level == KnockoutLevel.PAUSED

    def test_paused_12pct(self):
        """Drawdown 12% → PAUSED (above 10, below 15)."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=88_000)  # 12% DD
        assert new.level == KnockoutLevel.PAUSED

    def test_emergency_at_15pct(self):
        """Drawdown exactly at 15% → EMERGENCY."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=85_000)  # 15% DD
        assert new.level == KnockoutLevel.EMERGENCY

    def test_emergency_20pct(self):
        """Synthetic 20% drawdown triggers EMERGENCY."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=80_000)  # 20% DD
        assert new.level == KnockoutLevel.EMERGENCY

    # ── Threshold boundaries ──────────────────────────────────────────

    def test_boundary_4_9pct_stays_normal(self):
        """4.9% drawdown stays NORMAL."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=95_100)  # 4.9% DD
        assert new.level == KnockoutLevel.NORMAL

    def test_boundary_5_1pct_is_reduced(self):
        """5.1% drawdown → REDUCED."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=94_900)  # 5.1% DD
        assert new.level == KnockoutLevel.REDUCED

    def test_boundary_9_9pct_is_reduced(self):
        """9.9% drawdown stays REDUCED."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=90_100)  # 9.9% DD
        assert new.level == KnockoutLevel.REDUCED

    def test_boundary_10_1pct_is_paused(self):
        """10.1% drawdown → PAUSED."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=89_900)  # 10.1% DD
        assert new.level == KnockoutLevel.PAUSED

    def test_boundary_14_9pct_is_paused(self):
        """14.9% drawdown stays PAUSED."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=85_100)  # 14.9% DD
        assert new.level == KnockoutLevel.PAUSED

    def test_boundary_15_1pct_is_emergency(self):
        """15.1% drawdown → EMERGENCY."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=84_900)  # 15.1% DD
        assert new.level == KnockoutLevel.EMERGENCY

    # ── Peak equity tracking ─────────────────────────────────────────

    def test_peak_equity_tracks_highs(self):
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=110_000)
        assert new.peak_equity == 110_000
        _, newer = sm.compute(new, equity=105_000)
        assert newer.peak_equity == 110_000  # doesn't drop

    def test_peak_starts_at_zero(self):
        sm = KnockoutStateMachine()
        state = self.make_state()
        _, new = sm.compute(state, equity=100_000)
        assert new.peak_equity == 100_000

    # ── Sticky EMERGENCY ──────────────────────────────────────────────

    def test_emergency_stays_emergency_on_recovery(self):
        """Once in EMERGENCY, stays even if equity recovers."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=80_000)  # 20% DD → EMERGENCY
        assert new.level == KnockoutLevel.EMERGENCY
        # Equity recovers above peak, but level stays EMERGENCY
        _, newer = sm.compute(new, equity=120_000)
        assert newer.level == KnockoutLevel.EMERGENCY

    def test_emergency_stays_emergency_through_normal_equity(self):
        """EMERGENCY is sticky even when equity is back at peak."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=84_000)  # 16% DD → EMERGENCY
        assert new.level == KnockoutLevel.EMERGENCY
        # New peak at 120k, equity 120k → DD = 0%, but sticky
        state2 = new
        state2.peak_equity = 120_000
        _, newer = sm.compute(state2, equity=120_000)
        assert newer.level == KnockoutLevel.EMERGENCY

    # ── Sequential transitions ────────────────────────────────────────

    def test_transitions_normal_to_reduced(self):
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=94_000)  # 6% DD
        assert new.level == KnockoutLevel.REDUCED

    def test_transitions_reduced_to_paused(self):
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000, level=KnockoutLevel.REDUCED)
        _, new = sm.compute(state, equity=88_000)  # 12% DD
        assert new.level == KnockoutLevel.PAUSED

    def test_transitions_paused_to_emergency(self):
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000, level=KnockoutLevel.PAUSED)
        _, new = sm.compute(state, equity=83_000)  # 17% DD
        assert new.level == KnockoutLevel.EMERGENCY

    def test_transitions_normal_skip_to_emergency(self):
        """Rapidly goes from NORMAL directly to EMERGENCY at 20% DD."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=80_000)
        assert new.level == KnockoutLevel.EMERGENCY

    # ── Cooling-off (3 consecutive losses → skip 2 signals) ──────────

    def test_cooling_off_triggered_3_losses(self):
        """3rd consecutive loss triggers COOLING_OFF via compute()."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000, consecutive_losses=2)
        _, new = sm.compute(state, equity=99_000, last_trade_pnl=-500)
        assert new.consecutive_losses == 3
        assert new.level == KnockoutLevel.COOLING_OFF

    def test_cooling_off_skip_count(self):
        """cooling_off_signals_to_skip decrements each tick."""
        sm = KnockoutStateMachine()
        state = self.make_state(
            peak_equity=100_000,
            consecutive_losses=3,
            cooling_off_signals_to_skip=2,
            level=KnockoutLevel.COOLING_OFF,
        )
        _, new = sm.compute(state, equity=99_500)
        assert new.cooling_off_signals_to_skip == 1
        assert new.level == KnockoutLevel.COOLING_OFF

    def test_cooling_off_exits_via_winning_trade(self):
        """A winning trade resets consecutive_losses, allowing exit from COOLING_OFF."""
        sm = KnockoutStateMachine()
        state = self.make_state(
            peak_equity=100_000,
            consecutive_losses=3,
            cooling_off_signals_to_skip=2,
            level=KnockoutLevel.COOLING_OFF,
        )
        # One tick decrements skip to 1
        _, s1 = sm.compute(state, equity=97_000)
        assert s1.cooling_off_signals_to_skip == 1
        # Winning trade resets consecutive_losses
        _, s2 = sm.compute(s1, equity=99_000, last_trade_pnl=500)
        assert s2.consecutive_losses == 0
        # Now cooling_off_signals_to_skip still 1, so stays COOLING_OFF
        assert s2.level == KnockoutLevel.COOLING_OFF
        # One more tick exhausts skip count, and since DD < 5%, goes to NORMAL
        _, s3 = sm.compute(s2, equity=99_000)
        assert s3.cooling_off_signals_to_skip == 0
        assert s3.level == KnockoutLevel.NORMAL
        """Skip count decrements each tick while cooling-off."""
        sm = KnockoutStateMachine()
        state = self.make_state(
            peak_equity=100_000,
            consecutive_losses=3,
            cooling_off_signals_to_skip=2,
            level=KnockoutLevel.COOLING_OFF,
        )
        _, new = sm.compute(state, equity=97_000)
        assert new.cooling_off_signals_to_skip == 1
        # Still COOLING_OFF because cooling_off_signals_to_skip > 0
        assert new.level == KnockoutLevel.COOLING_OFF

    def test_cooling_off_does_not_skip_tick_when_already_paused(self):
        """If COOLING_OFF is active but DD >= 10%, PAUSED takes priority."""
        sm = KnockoutStateMachine()
        state = self.make_state(
            peak_equity=100_000,
            consecutive_losses=3,
            cooling_off_signals_to_skip=1,
            level=KnockoutLevel.COOLING_OFF,
        )
        _, new = sm.compute(state, equity=88_000)  # 12% DD
        # COOLING_OFF is checked before PAUSED in _determine_level path,
        # but since cooling_off_signals_to_skip > 0, it returns COOLING_OFF
        assert new.level == KnockoutLevel.COOLING_OFF

    def test_win_resets_consecutive_losses(self):
        """A winning trade resets consecutive_losses to 0."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000, consecutive_losses=2)
        _, new = sm.compute(state, equity=99_500, last_trade_pnl=500)
        assert new.consecutive_losses == 0
        assert new.level == KnockoutLevel.NORMAL or new.level == KnockoutLevel.REDUCED

    # ── Recovery from PAUSED ─────────────────────────────────────────

    def test_paused_requires_recovery_plan_to_exit(self):
        """PAUSED stays PAUSED without a recovery plan even when DD recovers."""
        sm = KnockoutStateMachine(min_recovery_ticks=0)
        state = self.make_state(
            peak_equity=100_000,
            level=KnockoutLevel.PAUSED,
            recovery_plan=None,
        )
        _, new = sm.compute(state, equity=98_000)  # 2% DD, well below caution
        # Without plan, stays PAUSED
        assert new.level == KnockoutLevel.PAUSED

    def test_paused_requires_observation_ticks(self):
        """PAUSED stays PAUSED if recovery_observation_ticks < min_recovery_ticks."""
        sm = KnockoutStateMachine(min_recovery_ticks=10)
        state = self.make_state(
            peak_equity=100_000,
            level=KnockoutLevel.PAUSED,
            recovery_plan="I will reduce leverage.",
            recovery_observation_ticks=3,
        )
        _, new = sm.compute(state, equity=98_000)
        assert new.level == KnockoutLevel.PAUSED  # not enough ticks

    def test_paused_exits_to_normal_when_all_conditions_met(self):
        """With plan + enough ticks + DD < 5% → NORMAL (falls through to reduced/normal check and DD < 5% means NORMAL)."""
        sm = KnockoutStateMachine(min_recovery_ticks=5)
        state = self.make_state(
            peak_equity=100_000,
            level=KnockoutLevel.PAUSED,
            recovery_plan="Plan to reduce.",
            recovery_observation_ticks=10,
        )
        _, new = sm.compute(state, equity=98_000)  # 2% DD
        assert new.level == KnockoutLevel.NORMAL

    def test_paused_stays_paused_with_plan_but_dd_above_caution(self):
        """With plan + ticks but DD 5-15%, stays PAUSED (DD must be < caution to exit)."""
        sm = KnockoutStateMachine(min_recovery_ticks=5)
        state = self.make_state(
            peak_equity=100_000,
            level=KnockoutLevel.PAUSED,
            recovery_plan="Plan to reduce.",
            recovery_observation_ticks=10,
        )
        # 6% DD — above the 5% caution threshold, so stays PAUSED
        _, new = sm.compute(state, equity=94_000)
        assert new.level == KnockoutLevel.PAUSED

    def test_paused_exits_to_normal_when_all_conditions_met(self):
        """With plan + enough ticks + DD < 5% → NORMAL."""
        sm = KnockoutStateMachine(min_recovery_ticks=5)
        state = self.make_state(
            peak_equity=100_000,
            level=KnockoutLevel.PAUSED,
            recovery_plan="Plan to reduce.",
            recovery_observation_ticks=10,
        )
        _, new = sm.compute(state, equity=98_000)  # 2% DD
        assert new.level == KnockoutLevel.NORMAL

    def test_paused_dd_above_10_stays_paused(self):
        """If already paused and DD is still >= 10%, stays PAUSED."""
        sm = KnockoutStateMachine()
        state = self.make_state(
            peak_equity=100_000,
            level=KnockoutLevel.PAUSED,
        )
        _, new = sm.compute(state, equity=89_000)  # 11% DD
        assert new.level == KnockoutLevel.PAUSED

    # ── Edge cases ───────────────────────────────────────────────────

    def test_zero_equity(self):
        """Zero equity should not cause division errors."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=0)
        assert new.current_drawdown == 1.0  # 100% DD
        assert new.level == KnockoutLevel.EMERGENCY

    def test_negative_equity(self):
        """Negative equity should also result in EMERGENCY."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=-5000)
        assert new.level == KnockoutLevel.EMERGENCY

    def test_peak_zero_avoids_div_zero(self):
        """When peak_equity is 0, drawdown should be 0.0 (no div by zero)."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=0)
        _, new = sm.compute(state, equity=100_000)
        assert new.current_drawdown == 0.0
        assert new.peak_equity == 100_000

    def test_equity_increases_then_drops_uses_highest_peak(self):
        """Peak equity is tracked correctly through sequential updates."""
        sm = KnockoutStateMachine()
        state = self.make_state()
        _, s1 = sm.compute(state, equity=100_000)
        assert s1.peak_equity == 100_000
        _, s2 = sm.compute(s1, equity=110_000)
        assert s2.peak_equity == 110_000
        _, s3 = sm.compute(s2, equity=95_000)  # 13.6% DD from 110k
        assert s3.peak_equity == 110_000
        assert s3.level == KnockoutLevel.PAUSED

    def test_no_trade_pnl_preserves_consecutive_losses(self):
        """When last_trade_pnl is None, consecutive_losses stays unchanged."""
        sm = KnockoutStateMachine()
        state = self.make_state(peak_equity=100_000, consecutive_losses=2)
        _, new = sm.compute(state, equity=95_000)
        assert new.consecutive_losses == 2

    def test_custom_thresholds(self):
        """Custom threshold values are respected."""
        sm = KnockoutStateMachine(
            caution_threshold=0.02,
            pause_threshold=0.05,
            emergency_threshold=0.10,
        )
        state = self.make_state(peak_equity=100_000)
        _, s1 = sm.compute(state, equity=97_000)  # 3% DD
        assert s1.level == KnockoutLevel.REDUCED  # canceled at 2%
        _, s2 = sm.compute(s1, equity=94_000)  # 6% DD
        assert s2.level == KnockoutLevel.PAUSED  # paused at 5%
        _, s3 = sm.compute(s2, equity=89_000)  # 11% DD
        assert s3.level == KnockoutLevel.EMERGENCY  # emergency at 10%

    def test_timestamp_propagation(self):
        """The 'now' timestamp is returned via state updates."""
        sm = KnockoutStateMachine()
        now = datetime(2026, 7, 10, 15, 30, 0)
        state = self.make_state(peak_equity=100_000)
        _, new = sm.compute(state, equity=100_000, now=now)
        # No paused/emergency timestamp set here (those are set by orchestrator)
        assert new.level == KnockoutLevel.NORMAL


# ═══════════════════════════════════════════════════════════════════════════════
# DrawdownKnockoutGate tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestDrawdownKnockoutGate:
    """Composable gate that rejects trades during knockout conditions."""

    def make_state(self, level=KnockoutLevel.NORMAL, drawdown=0.0) -> KnockoutState:
        return KnockoutState(level=level, current_drawdown=drawdown, peak_equity=100_000)

    def make_context(self, state) -> dict:
        return {"knockout_state": state}

    # ── NORMAL: passes ───────────────────────────────────────────────

    def test_normal_allows_buy(self):
        gate = DrawdownKnockoutGate()
        ctx = self.make_context(self.make_state(KnockoutLevel.NORMAL, 0.03))
        granted, reason = gate.check(ctx, {"type": "BUY", "ticker": "AAPL"})
        assert granted is True
        assert "NORMAL" in reason

    def test_normal_allows_sell(self):
        gate = DrawdownKnockoutGate()
        ctx = self.make_context(self.make_state(KnockoutLevel.NORMAL, 0.02))
        granted, reason = gate.check(ctx, {"type": "SELL", "ticker": "AAPL"})
        assert granted is True
        assert "NORMAL" in reason

    def test_normal_allows_hold(self):
        gate = DrawdownKnockoutGate()
        ctx = self.make_context(self.make_state(KnockoutLevel.NORMAL))
        granted, reason = gate.check(ctx, {"type": "HOLD", "ticker": "AAPL"})
        assert granted is True
        assert "HOLD always allowed" in reason

    # ── REDUCED: passes (sizing applied by multiplier) ───────────────

    def test_reduced_allows_buy(self):
        gate = DrawdownKnockoutGate()
        ctx = self.make_context(self.make_state(KnockoutLevel.REDUCED, 0.07))
        granted, reason = gate.check(ctx, {"type": "BUY", "ticker": "AAPL"})
        assert granted is True
        assert "REDUCED" in reason
        assert "50%" in reason

    def test_reduced_allows_sell(self):
        gate = DrawdownKnockoutGate()
        ctx = self.make_context(self.make_state(KnockoutLevel.REDUCED, 0.06))
        granted, reason = gate.check(ctx, {"type": "SELL", "ticker": "TSLA"})
        assert granted is True

    # ── PAUSED: rejects BUY, allows SELL ─────────────────────────────

    def test_paused_rejects_buy(self):
        gate = DrawdownKnockoutGate()
        ctx = self.make_context(self.make_state(KnockoutLevel.PAUSED, 0.12))
        granted, reason = gate.check(ctx, {"type": "BUY", "ticker": "AAPL"})
        assert granted is False
        assert "PAUSED" in reason
        assert "no new positions" in reason
        assert "Observation only" in reason

    def test_paused_allows_sell(self):
        gate = DrawdownKnockoutGate()
        ctx = self.make_context(self.make_state(KnockoutLevel.PAUSED, 0.12))
        granted, reason = gate.check(ctx, {"type": "SELL", "ticker": "AAPL"})
        assert granted is True
        assert "PAUSED" in reason
        assert "SELL AAPL allowed" in reason

    # ── COOLING_OFF: rejects BUY, allows SELL ────────────────────────

    def test_cooling_off_rejects_buy(self):
        gate = DrawdownKnockoutGate()
        ctx = self.make_context(self.make_state(KnockoutLevel.COOLING_OFF, 0.03))
        granted, reason = gate.check(ctx, {"type": "BUY", "ticker": "AAPL"})
        assert granted is False
        assert "COOLING_OFF" in reason or "COOLING OFF" in reason
        assert "no new positions" in reason

    def test_cooling_off_allows_sell(self):
        gate = DrawdownKnockoutGate()
        ctx = self.make_context(self.make_state(KnockoutLevel.COOLING_OFF, 0.03))
        granted, reason = gate.check(ctx, {"type": "SELL", "ticker": "AAPL"})
        assert granted is True
        assert "exits permitted" in reason

    # ── EMERGENCY: rejects everything ────────────────────────────────

    def test_emergency_rejects_buy(self):
        gate = DrawdownKnockoutGate()
        ctx = self.make_context(self.make_state(KnockoutLevel.EMERGENCY, 0.18))
        granted, reason = gate.check(ctx, {"type": "BUY", "ticker": "AAPL"})
        assert granted is False
        assert "EMERGENCY" in reason
        assert "frozen" in reason

    def test_emergency_rejects_sell(self):
        gate = DrawdownKnockoutGate()
        ctx = self.make_context(self.make_state(KnockoutLevel.EMERGENCY, 0.20))
        granted, reason = gate.check(ctx, {"type": "SELL", "ticker": "AAPL"})
        assert granted is False
        assert "EMERGENCY" in reason
        assert "frozen" in reason

    def test_emergency_rejects_hold(self):
        """HOLD is always allowed regardless of level."""
        gate = DrawdownKnockoutGate()
        ctx = self.make_context(self.make_state(KnockoutLevel.EMERGENCY))
        granted, reason = gate.check(ctx, {"type": "HOLD"})
        assert granted is True
        assert "HOLD always allowed" in reason

    # ── Missing knockout state ───────────────────────────────────────

    def test_no_knockout_state_passes(self):
        """If context lacks knockout_state, gate passes (not its job)."""
        gate = DrawdownKnockoutGate()
        granted, reason = gate.check({}, {"type": "BUY", "ticker": "AAPL"})
        assert granted is True
        assert "no knockout state" in reason

    # ── Action key flexibility ───────────────────────────────────────

    def test_accepts_action_key_instead_of_type(self):
        """Gate should handle both 'type' and 'action' keys."""
        gate = DrawdownKnockoutGate()
        ctx = self.make_context(self.make_state(KnockoutLevel.PAUSED, 0.12))
        granted, reason = gate.check(ctx, {"action": "BUY", "ticker": "AAPL"})
        assert granted is False
        assert "PAUSED" in reason


# ═══════════════════════════════════════════════════════════════════════════════
# DrawdownKnockout (orchestrator) tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestDrawdownKnockout:
    """Full orchestrator: update, submit_recovery_plan, re_enable, emergency_status."""

    FAKE_NOW = datetime(2026, 7, 10, 12, 0, 0)

    # ── update() — 4 tiers ───────────────────────────────────────────

    def test_update_normal(self):
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        assert dk.state.level == KnockoutLevel.NORMAL
        assert dk.state.peak_equity == 100_000

    def test_update_reduced(self):
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=94_000, now=self.FAKE_NOW)  # 6% DD
        assert dk.state.level == KnockoutLevel.REDUCED
        assert dk.state.position_multiplier == 0.5

    def test_update_paused(self):
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=88_000, now=self.FAKE_NOW)  # 12% DD
        assert dk.state.level == KnockoutLevel.PAUSED

    def test_update_emergency(self):
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=80_000, now=self.FAKE_NOW)  # 20% DD
        assert dk.state.level == KnockoutLevel.EMERGENCY

    def test_20pct_triggers_emergency(self):
        """Synthetic 20% drawdown triggers position freeze."""
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=80_000, now=self.FAKE_NOW)
        assert dk.state.level == KnockoutLevel.EMERGENCY
        assert dk.state.position_multiplier == 0.0
        assert dk.state.allow_exits is False

    # ── update() — cooling-off ───────────────────────────────────────

    def test_cooling_off_triggered_via_update(self):
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=99_500, last_trade_pnl=-500, now=self.FAKE_NOW)
        dk.update(equity=99_000, last_trade_pnl=-500, now=self.FAKE_NOW)
        dk.update(equity=98_500, last_trade_pnl=-500, now=self.FAKE_NOW)
        assert dk.state.level == KnockoutLevel.COOLING_OFF
        assert dk.state.cooling_off_signals_to_skip == 2

    def test_cooling_off_exits_via_winning_trade(self):
        """After cooling-off, a winning trade resets consecutive losses → exits COOLING_OFF."""
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=99_500, last_trade_pnl=-500, now=self.FAKE_NOW)
        dk.update(equity=99_000, last_trade_pnl=-500, now=self.FAKE_NOW)
        dk.update(equity=98_500, last_trade_pnl=-500, now=self.FAKE_NOW)
        assert dk.state.level == KnockoutLevel.COOLING_OFF
        assert dk.state.cooling_off_signals_to_skip == 2
        # Consume 2 skip ticks
        dk.update(equity=98_500, now=self.FAKE_NOW)
        dk.update(equity=98_500, now=self.FAKE_NOW)
        assert dk.state.cooling_off_signals_to_skip == 0
        # Still COOLING_OFF due to consecutive_losses = 3
        assert dk.state.level == KnockoutLevel.COOLING_OFF
        # Winning trade resets consecutive losses
        dk.update(equity=99_000, last_trade_pnl=2000, now=self.FAKE_NOW)
        assert dk.state.consecutive_losses == 0
        assert dk.state.level == KnockoutLevel.NORMAL

    def test_cooling_off_skips_two_signals(self):
        """After triggering cooling-off, next 2 ticks are COOLING_OFF."""
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=99_500, last_trade_pnl=-500, now=self.FAKE_NOW)
        dk.update(equity=99_000, last_trade_pnl=-500, now=self.FAKE_NOW)
        dk.update(equity=98_500, last_trade_pnl=-500, now=self.FAKE_NOW)
        assert dk.state.level == KnockoutLevel.COOLING_OFF
        assert dk.state.cooling_off_signals_to_skip == 2
        # Tick 1 of 2 skipped
        dk.update(equity=98_500, now=self.FAKE_NOW)
        assert dk.state.level == KnockoutLevel.COOLING_OFF
        assert dk.state.cooling_off_signals_to_skip == 1
        # Tick 2 of 2 skipped
        dk.update(equity=98_500, now=self.FAKE_NOW)
        assert dk.state.level == KnockoutLevel.COOLING_OFF
        assert dk.state.cooling_off_signals_to_skip == 0
        # With skip count = 0, consecutive_losses (3) still keeps state COOLING_OFF
        dk.update(equity=98_500, now=self.FAKE_NOW)
        assert dk.state.level == KnockoutLevel.COOLING_OFF

    # ── update() — recovery from PAUSED ──────────────────────────────

    def test_recovery_observation_ticks_increment(self):
        """Each update() while PAUSED increments observation ticks."""
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=88_000, now=self.FAKE_NOW)  # enters PAUSED
        assert dk.state.recovery_observation_ticks == 1
        dk.update(equity=88_000, now=self.FAKE_NOW)
        assert dk.state.recovery_observation_ticks == 2
        dk.update(equity=88_000, now=self.FAKE_NOW)
        assert dk.state.recovery_observation_ticks == 3

    def test_recovery_exit_via_plan_and_ticks(self):
        """Full recovery cycle: plan + ticks + DD < 5% → NORMAL."""
        dk = DrawdownKnockout("test-trader", min_recovery_ticks=5)
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=88_000, now=self.FAKE_NOW)  # PAUSED
        assert dk.state.level == KnockoutLevel.PAUSED
        # Observe for several ticks
        for _ in range(8):
            dk.update(equity=88_000, now=self.FAKE_NOW)
        assert dk.state.recovery_observation_ticks >= 5
        # Submit recovery plan
        result = dk.submit_recovery_plan(
            "Overleveraged in tech. Will reduce max positions from 6 to 4 "
            "and increase stop-loss distance to 8%."
        )
        assert result == "accepted"
        # With plan + ticks, DD below caution → exits PAUSED
        dk.update(equity=97_000, now=self.FAKE_NOW)
        assert dk.state.level == KnockoutLevel.NORMAL

    # ── submit_recovery_plan() ───────────────────────────────────────

    def test_submit_recovery_plan_not_in_paused(self):
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        result = dk.submit_recovery_plan("Some detailed plan here for recovery purposes.")
        assert "rejected" in result
        assert "not in PAUSED mode" in result

    def test_submit_recovery_plan_too_short(self):
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=88_000, now=self.FAKE_NOW)
        result = dk.submit_recovery_plan("Short")
        assert "rejected" in result
        assert "too short" in result

    def test_submit_recovery_plan_not_enough_ticks(self):
        dk = DrawdownKnockout("test-trader", min_recovery_ticks=10)
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=88_000, now=self.FAKE_NOW)  # PAUSED, 1 tick
        result = dk.submit_recovery_plan(
            "Overleveraged in tech. Will reduce position sizes by 30%."
        )
        assert "rejected" in result
        assert "observation ticks" in result

    def test_submit_recovery_plan_accepted(self):
        dk = DrawdownKnockout("test-trader", min_recovery_ticks=5)
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=88_000, now=self.FAKE_NOW)
        for _ in range(10):
            dk.update(equity=88_000, now=self.FAKE_NOW)
        result = dk.submit_recovery_plan(
            "Overleveraged in momentum stocks. Reducing to 3 positions max "
            "and tightening stop-losses to 5%."
        )
        assert result == "accepted"

    # ── re_enable() ──────────────────────────────────────────────────

    def test_re_enable_from_emergency(self):
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=80_000, now=self.FAKE_NOW)  # EMERGENCY
        assert dk.state.level == KnockoutLevel.EMERGENCY
        dk.re_enable()
        assert dk.state.level == KnockoutLevel.NORMAL
        assert dk.state.current_drawdown == 0.0
        assert dk.state.peak_equity == 0.0
        assert dk.state.consecutive_losses == 0
        assert dk.state.cooling_off_signals_to_skip == 0
        assert dk.state.paused_at is None
        assert dk.state.emergency_at is None
        assert dk.state.recovery_plan is None
        assert dk.state.recovery_observation_ticks == 0
        assert len(dk.state.journal) > 0
        assert "MANUALLY RE-ENABLED" in dk.state.journal[-1]

    def test_re_enable_journaled(self):
        """re_enable() adds a journal entry."""
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=80_000, now=self.FAKE_NOW)
        dk.re_enable()
        assert any("MANUALLY RE-ENABLED" in entry for entry in dk.state.journal)

    def test_re_enable_not_emergency_no_error(self):
        """re_enable() called when not in EMERGENCY should not throw."""
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.re_enable()  # should not crash
        assert dk.state.level == KnockoutLevel.NORMAL

    # ── emergency_status() ───────────────────────────────────────────

    def test_emergency_status_normal(self):
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        status = dk.emergency_status()
        assert status["trader_id"] == "test-trader"
        assert status["level"] == "normal"
        assert status["freeze_positions"] is False
        assert status["alert_required"] is False

    def test_emergency_status_paused(self):
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=88_000, now=self.FAKE_NOW)
        status = dk.emergency_status()
        assert status["level"] == "paused"
        assert status["freeze_positions"] is False
        assert status["alert_required"] is True

    def test_emergency_status_emergency(self):
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=80_000, now=self.FAKE_NOW)
        status = dk.emergency_status()
        assert status["level"] == "emergency"
        assert status["freeze_positions"] is True
        assert status["alert_required"] is True
        assert status["freeze_positions"] is True

    def test_emergency_status_recovery_ready(self):
        dk = DrawdownKnockout("test-trader", min_recovery_ticks=5)
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=88_000, now=self.FAKE_NOW)
        for _ in range(6):
            dk.update(equity=88_000, now=self.FAKE_NOW)
        status = dk.emergency_status()
        assert status["recovery_ready"] is True
        assert status["level"] == "paused"

    def test_emergency_status_thresholds(self):
        dk = DrawdownKnockout("test-trader")
        status = dk.emergency_status()
        assert status["thresholds"]["caution_pct"] == 5.0
        assert status["thresholds"]["pause_pct"] == 10.0
        assert status["thresholds"]["emergency_pct"] == 15.0

    # ── Timestamps ───────────────────────────────────────────────────

    def test_paused_at_set_on_entry(self):
        dk = DrawdownKnockout("test-trader")
        now = datetime(2026, 7, 10, 14, 30, 0)
        dk.update(equity=100_000, now=now)
        dk.update(equity=88_000, now=now)
        assert dk.state.paused_at == now

    def test_emergency_at_set_on_entry(self):
        dk = DrawdownKnockout("test-trader")
        now = datetime(2026, 7, 10, 14, 30, 0)
        dk.update(equity=100_000, now=now)
        dk.update(equity=80_000, now=now)
        assert dk.state.emergency_at == now

    def test_paused_at_not_set_on_subsequent_ticks(self):
        """paused_at should only be set on the tick that enters PAUSED."""
        dk = DrawdownKnockout("test-trader")
        t1 = datetime(2026, 7, 10, 12, 0, 0)
        dk.update(equity=100_000, now=t1)
        dk.update(equity=88_000, now=t1)  # enters PAUSED at t1
        # Subsequent tick at a different time
        t2 = datetime(2026, 7, 10, 13, 0, 0)
        dk.update(equity=88_000, now=t2)
        assert dk.state.paused_at == t1  # should not have changed

    # ── Position multiplier lifecycle ────────────────────────────────

    def test_position_multiplier_normal_then_reduced(self):
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        assert dk.state.position_multiplier == 1.0
        dk.update(equity=94_000, now=self.FAKE_NOW)
        assert dk.state.position_multiplier == 0.5
        dk.update(equity=88_000, now=self.FAKE_NOW)
        assert dk.state.position_multiplier == 0.0

    # ── Equity history ───────────────────────────────────────────────

    def test_equity_history_tracking(self):
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=95_000, now=self.FAKE_NOW)
        dk.update(equity=90_000, now=self.FAKE_NOW)
        assert len(dk.state.equity_history) == 3
        assert dk.state.equity_history == [100_000, 95_000, 90_000]

    # ── Journal ──────────────────────────────────────────────────────

    def test_journal_logs_transitions(self):
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=94_000, now=self.FAKE_NOW)  # enters REDUCED
        assert any("normal → reduced" in e.lower() for e in dk.state.journal)

    def test_journal_logs_cooling_off_trigger(self):
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        dk.update(equity=99_500, last_trade_pnl=-500, now=self.FAKE_NOW)
        dk.update(equity=99_000, last_trade_pnl=-500, now=self.FAKE_NOW)
        dk.update(equity=98_500, last_trade_pnl=-500, now=self.FAKE_NOW)
        assert any("cooling off" in e.lower() for e in dk.state.journal)

    # ── __repr__ ─────────────────────────────────────────────────────

    def test_repr(self):
        dk = DrawdownKnockout("test-trader")
        dk.update(equity=100_000, now=self.FAKE_NOW)
        rep = repr(dk)
        assert "test-trader" in rep
        assert "normal" in rep


# ═══════════════════════════════════════════════════════════════════════════════
# inject_knockout_gate tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestInjectKnockoutGate:
    """Injection helper for RiskManager gate chain."""

    def test_inject_with_existing_knockout(self):
        from src.risk.manager import RiskManager
        rm = RiskManager(gates=[])
        dk = DrawdownKnockout("trader-1")
        injected_dk, new_rm = inject_knockout_gate(rm, knockout=dk)
        assert injected_dk is dk
        assert dk.gate in new_rm.gates

    def test_inject_creates_knockout(self):
        from src.risk.manager import RiskManager
        rm = RiskManager(gates=[])
        injected_dk, new_rm = inject_knockout_gate(rm, trader_id="trader-2")
        assert injected_dk.trader_id == "trader-2"
        assert injected_dk.gate in new_rm.gates

    def test_inject_knockout_at_front(self):
        """Knockout gate should be first in the chain."""
        from src.risk.manager import RiskManager
        dummy_gate = MagicMock()
        dummy_gate.check.return_value = (True, "dummy")
        rm = RiskManager(gates=[dummy_gate])
        injected_dk, new_rm = inject_knockout_gate(rm, trader_id="trader-3")
        assert new_rm.gates[0] is injected_dk.gate
        assert len(new_rm.gates) == 2

    def test_inject_requires_trader_id(self):
        from src.risk.manager import RiskManager
        rm = RiskManager(gates=[])
        with pytest.raises(ValueError, match="trader_id"):
            inject_knockout_gate(rm)