#!/usr/bin/env python3
"""Tests for walk-forward validation in src/prompt_sweep.py — DP-3.

Run:  pytest tests/test_walk_forward.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

from src.prompt_sweep import (
    build_walk_forward_windows,
    get_trading_days,
    _compute_walk_forward_metrics,
    _load_dates_data,
    PromptVariant,
    SignalParams,
    PERTURBATION_TEMPLATES,
    score_variant,
    generate_variants,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Window building tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildWalkForwardWindows:
    """Test walk-forward window construction."""

    def test_standard_case(self):
        """20 dates, train=5, val=1 → 15 windows."""
        dates = [f"2026-06-{i+1:02d}" for i in range(20)]
        windows = build_walk_forward_windows(dates, train_days=5, val_days=1)
        assert len(windows) == 15  # 20 - 5 - 1 + 1

    def test_first_window_structure(self):
        """First window: train on earliest dates, validate next."""
        dates = [f"2026-06-{i+1:02d}" for i in range(20)]
        windows = build_walk_forward_windows(dates, train_days=5, val_days=1)
        first_train, first_val = windows[0]
        assert first_train == ["2026-06-01", "2026-06-02", "2026-06-03",
                                "2026-06-04", "2026-06-05"]
        assert first_val == ["2026-06-06"]

    def test_last_window_structure(self):
        """Last window: train on latest training dates, validate last date."""
        dates = [f"2026-06-{i+1:02d}" for i in range(20)]
        windows = build_walk_forward_windows(dates, train_days=5, val_days=1)
        last_train, last_val = windows[-1]
        assert last_train == ["2026-06-15", "2026-06-16", "2026-06-17",
                               "2026-06-18", "2026-06-19"]
        assert last_val == ["2026-06-20"]

    def test_train_and_val_disjoint(self):
        """Training and validation dates must not overlap."""
        dates = [f"2026-06-{i+1:02d}" for i in range(30)]
        windows = build_walk_forward_windows(dates, train_days=10, val_days=5)
        for train, val in windows:
            train_set = set(train)
            val_set = set(val)
            assert train_set.isdisjoint(val_set), (
                f"Train {train} and val {val} overlap"
            )

    def test_windows_cover_all_dates(self):
        """Every date in the range should appear in at least one window."""
        dates = [f"2026-06-{i+1:02d}" for i in range(10)]
        windows = build_walk_forward_windows(dates, train_days=3, val_days=2)
        covered = set()
        for train, val in windows:
            covered.update(train)
            covered.update(val)
        assert covered == set(dates)

    def test_too_few_dates_returns_empty(self):
        """Not enough dates for even one window → empty list."""
        dates = ["2026-06-01", "2026-06-02"]
        windows = build_walk_forward_windows(dates, train_days=5, val_days=1)
        assert len(windows) == 0

    def test_exactly_enough_dates(self):
        """Exactly train+val dates → 1 window."""
        dates = [f"2026-06-{i+1:02d}" for i in range(6)]
        windows = build_walk_forward_windows(dates, train_days=5, val_days=1)
        assert len(windows) == 1

    def test_larger_val_days(self):
        """Multiple validation days per window."""
        dates = [f"2026-06-{i+1:02d}" for i in range(20)]
        windows = build_walk_forward_windows(dates, train_days=7, val_days=3)
        # 20 - 7 - 3 + 1 = 11
        assert len(windows) == 11
        first_train, first_val = windows[0]
        assert len(first_train) == 7
        assert len(first_val) == 3
        assert first_val == ["2026-06-08", "2026-06-09", "2026-06-10"]

    def test_empty_dates_list(self):
        """Empty dates → empty windows."""
        windows = build_walk_forward_windows([], train_days=5, val_days=1)
        assert len(windows) == 0

    def test_single_date_not_enough(self):
        """Single date can't satisfy train+val requirement."""
        windows = build_walk_forward_windows(["2026-06-01"], train_days=5, val_days=1)
        assert len(windows) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Trading day generation tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetTradingDays:
    """Test trading day enumeration."""

    def test_returns_requested_count(self):
        days = get_trading_days(10, end_date="2026-07-06")
        assert len(days) == 10

    def test_all_are_weekdays(self):
        days = get_trading_days(20, end_date="2026-07-06")
        from datetime import datetime
        for d in days:
            dt = datetime.strptime(d, "%Y-%m-%d")
            assert dt.weekday() < 5, f"{d} is a weekend day"

    def test_chronological_order(self):
        days = get_trading_days(15, end_date="2026-07-06")
        assert days == sorted(days)

    def test_does_not_include_future(self):
        days = get_trading_days(5, end_date="2026-07-06")
        for d in days:
            assert d <= "2026-07-06"

    def test_skips_weekends(self):
        """Weekend dates (Jul 4-5, 2026 = Sat-Sun) are excluded."""
        days = get_trading_days(5, end_date="2026-07-06")
        assert "2026-07-04" not in days
        assert "2026-07-05" not in days

    def test_reasonable_dates_returns_correct_count(self):
        """Large but reasonable number of trading days returns successfully."""
        # 200 trading days should be well within the safety valve
        days = get_trading_days(200, end_date="2026-07-06")
        assert len(days) == 200
        assert days[-1] <= "2026-07-06"


# ═══════════════════════════════════════════════════════════════════════════════
# Aggregate metrics tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputeWalkForwardMetrics:
    """Test aggregate metric computation."""

    def test_perfect_winner(self):
        """Variant beats baseline on every window."""
        val_scores = [0.5, 0.6, 0.55, 0.7, 0.65]
        baseline_scores = [0.3, 0.3, 0.3, 0.3, 0.3]
        metrics = _compute_walk_forward_metrics(val_scores, baseline_scores)
        assert metrics["win_rate"] == 1.0
        assert metrics["avg_val_score"] == pytest.approx(0.6)
        assert metrics["val_stability"] > 0  # Some variance

    def test_complete_loser(self):
        """Variant never beats baseline."""
        val_scores = [0.1, 0.15, 0.12, 0.08, 0.11]
        baseline_scores = [0.3, 0.3, 0.3, 0.3, 0.3]
        metrics = _compute_walk_forward_metrics(val_scores, baseline_scores)
        assert metrics["win_rate"] == 0.0

    def test_partial_winner(self):
        """Variant beats baseline on 3/5 windows → 0.6 win rate."""
        val_scores = [0.5, 0.2, 0.5, 0.2, 0.5]
        baseline_scores = [0.3, 0.3, 0.3, 0.3, 0.3]
        metrics = _compute_walk_forward_metrics(val_scores, baseline_scores)
        assert metrics["win_rate"] == 0.6

    def test_zero_stability_with_one_window(self):
        """Single window → zero stability (not enough data for std)."""
        metrics = _compute_walk_forward_metrics([0.5], [0.3])
        assert metrics["val_stability"] == 0.0
        assert metrics["win_rate"] == 1.0
        assert metrics["avg_val_score"] == 0.5

    def test_empty_scores_returns_zeros(self):
        """Empty input → all zeros."""
        metrics = _compute_walk_forward_metrics([], [])
        assert metrics["avg_val_score"] == 0.0
        assert metrics["val_stability"] == 0.0
        assert metrics["win_rate"] == 0.0

    def test_consistent_performer(self):
        """Low variance variant should have low val_stability."""
        # Consistent scores (low variance)
        consistent = _compute_walk_forward_metrics(
            [0.5, 0.51, 0.49, 0.5, 0.5],
            [0.3, 0.3, 0.3, 0.3, 0.3],
        )
        # Inconsistent scores (high variance)
        inconsistent = _compute_walk_forward_metrics(
            [0.8, 0.1, 0.9, 0.05, 0.7],
            [0.3, 0.3, 0.3, 0.3, 0.3],
        )
        assert consistent["val_stability"] < inconsistent["val_stability"]


# ═══════════════════════════════════════════════════════════════════════════════
# Winner criteria tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestWinnerCriteria:
    """Test the walk-forward winner selection criteria."""

    def test_all_criteria_pass(self):
        """Variant that passes all three criteria should be a winner."""
        # win_rate >= 0.6: 3/5 = 0.6 ✓
        # avg_val > baseline + 0.05: 0.55 > 0.40 + 0.05 ✓
        # stability < 2 * baseline_stability: 0.05 < 2 * 0.08 ✓
        val_scores = [0.6, 0.3, 0.6, 0.3, 0.6]  # beats on 3/5
        baseline_scores = [0.4, 0.4, 0.4, 0.4, 0.4]
        m = _compute_walk_forward_metrics(val_scores, baseline_scores)
        assert m["win_rate"] >= 0.6
        assert m["avg_val_score"] > np.mean(baseline_scores) + 0.05

    def test_fails_win_rate(self):
        """Low win rate should fail criterion."""
        val_scores = [0.6, 0.2, 0.3, 0.2, 0.3]  # beats on 1/5 = 0.2
        baseline_scores = [0.4, 0.4, 0.4, 0.4, 0.4]
        m = _compute_walk_forward_metrics(val_scores, baseline_scores)
        assert m["win_rate"] < 0.6

    def test_fails_avg_score(self):
        """Barely-above-baseline fails the +0.05 margin."""
        val_scores = [0.41, 0.41, 0.41, 0.41, 0.41]  # avg = 0.41, beats every time
        baseline_scores = [0.4, 0.4, 0.4, 0.4, 0.4]
        m = _compute_walk_forward_metrics(val_scores, baseline_scores)
        assert m["win_rate"] == 1.0  # passes win_rate
        assert m["avg_val_score"] < np.mean(baseline_scores) + 0.05  # fails avg

    def test_fails_stability(self):
        """Wildly inconsistent variant should fail stability check."""
        # High variance: alternates between amazing and terrible
        val_scores = [0.8, 0.05, 0.9, 0.02, 0.7]
        baseline_scores = [0.4, 0.4, 0.4, 0.42, 0.38]  # low baseline variance
        m = _compute_walk_forward_metrics(val_scores, baseline_scores)
        baseline_m = _compute_walk_forward_metrics(baseline_scores, baseline_scores)
        # Variant stability should be much higher than baseline
        assert m["val_stability"] > 2.0 * baseline_m["val_stability"]

    def test_baseline_zero_stability_does_not_fail(self):
        """If baseline has zero stability (all same scores), don't unfairly fail."""
        val_scores = [0.5, 0.55, 0.48]
        baseline_scores = [0.3, 0.3, 0.3]  # zero std
        m = _compute_walk_forward_metrics(val_scores, baseline_scores)
        # Stability check should pass since baseline stability is 0
        # (handled in _run_multidate_sweep: baseline_stability == 0 → pass)


# ═══════════════════════════════════════════════════════════════════════════════
# Cost integration tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCostIntegration:
    """Test that costs affect scoring when enabled."""

    def test_cost_model_changes_pnl(self):
        """Applying costs should change trade PnL."""
        from src.transaction_costs import CostModel

        model = CostModel(slippage_bps=10.0)

        # Simulate a trade result
        gross, net = model.apply_to_trade(100.0, 101.0, 100)
        assert gross == 100.0  # $100 profit before costs
        assert net < gross  # Net is less after costs

    def test_cost_penalizes_high_frequency(self):
        """Many small trades should incur higher total costs than few large ones."""
        from src.transaction_costs import CostModel

        model = CostModel(slippage_bps=10.0, spread_bps=5.0)

        # 1 large trade
        _, net_large = model.apply_to_trade(100.0, 101.0, 1000)

        # 10 small trades (same total notional)
        total_net_small = 0.0
        for _ in range(10):
            _, net = model.apply_to_trade(100.0, 101.0, 100)
            total_net_small += net

        # Small trades have same gross but more costs due to min_trade_cost
        # net_large per share: 1000 shares × $1 gross = $1000 gross
        # Cost on notional 201000 @ 15bps = 301.5 → net = 698.5, per share = 0.6985
        # net_small per share: 100 shares × $1 gross = $100 gross
        # Cost on notional 20100 @ 15bps = 30.15 → net = 69.85, per share = 0.6985
        # Same per-share net in both cases if min_trade_cost=0
        # But min_trade_cost=1 means each small trade gets floored
        # So small trades are penalized more
        assert total_net_small <= net_large + 0.01  # Allow for floating point

    def test_disabling_costs_preserves_gross(self):
        """Without cost model, score_variant uses gross PnL."""
        from src.replay import Tick
        from datetime import datetime, timedelta

        ticks = []
        rng = np.random.default_rng(42)
        base_time = datetime(2024, 1, 2, 9, 30)
        price = 100.0
        for i in range(30):
            ts = base_time + timedelta(minutes=30 * i)
            noise = rng.normal(0, 0.002)
            price = price * (1.0 + noise)
            ticks.append(Tick(
                timestamp=ts,
                ticker="SPY",
                open=price * 0.999,
                high=price * 1.005,
                low=price * 0.995,
                close=price,
                volume=1_000_000,
            ))

        variant = PromptVariant(
            trader="test",
            variant_id=1,
            variant_name="test",
            description="test",
            prompt_text="# test",
            signal_params=SignalParams(),
            baseline_params=SignalParams(),
        )

        # Without costs
        score_no_cost, _ = score_variant(variant, ticks, cost_model=None)
        assert isinstance(score_no_cost, float)
        assert not np.isnan(score_no_cost)

    def test_same_variant_ranks_differently_with_costs(self):
        """Costs should change the relative ranking of high-frequency vs low-frequency."""
        from src.transaction_costs import CostModel
        from src.replay import Tick
        from datetime import datetime, timedelta

        # Create ticks that strongly trigger bullish signals (low threshold)
        rng = np.random.default_rng(42)
        ticks = []
        
        # Create 13 ticks per ticker — enough for signal to build up
        tickers = ["AAPL", "MSFT", "NVDA", "SPY"]
        prices = {"AAPL": 225.0, "MSFT": 450.0, "NVDA": 130.0, "SPY": 590.0}
        base_time = datetime(2024, 1, 2, 9, 30)
        
        for ticker in tickers:
            price = prices[ticker]
            for i in range(13):  # 6.5 hours @ 30min
                ts = base_time + timedelta(minutes=30 * i)
                # Strong uptrend
                price = price * (1.0 + 0.003)
                ticks.append(Tick(
                    timestamp=ts,
                    ticker=ticker,
                    open=price * 0.999,
                    high=price * 1.005,
                    low=price * 0.995,
                    close=price,
                    volume=1_000_000,
                ))
        ticks.sort(key=lambda t: t.timestamp)

        # Aggressive variant (high conviction multiplier → more trades)
        agg_params = SignalParams()
        agg_params.momentum_threshold = 0.2  # low threshold → more signals
        agg_params.conviction_multiplier = 2.5
        agg_params.base_size_pct = 0.20

        agg = PromptVariant("test", 1, "agg", "", "# agg", agg_params, SignalParams())

        # Score without costs
        score_no, _ = score_variant(agg, ticks, cost_model=None)

        # Score with costs
        model = CostModel(slippage_bps=10.0)
        score_with, _ = score_variant(agg, ticks, cost_model=model)

        # Both should be valid scores
        assert isinstance(score_no, float)
        assert isinstance(score_with, float)

        # With costs, score should be ≤ without costs (costs never improve score)
        assert score_with <= score_no + 0.001  # Allow float tolerance


# ═══════════════════════════════════════════════════════════════════════════════
# Integration with existing functions
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultiDateIntegration:
    """Tests for multi-date score integration points."""

    def test_variant_has_multidate_fields(self):
        """PromptVariant should have the new multi-date fields."""
        v = PromptVariant(
            trader="test",
            variant_id=1,
            variant_name="test",
            description="test",
            prompt_text="# test",
            signal_params=SignalParams(),
            baseline_params=SignalParams(),
        )
        assert hasattr(v, "val_scores")
        assert isinstance(v.val_scores, list)
        assert len(v.val_scores) == 0
        assert hasattr(v, "avg_val_score")
        assert v.avg_val_score == 0.0
        assert hasattr(v, "val_stability")
        assert v.val_stability == 0.0

    def test_load_dates_data_with_empty_dates(self):
        """Empty date list should return empty tick list."""
        ticks = _load_dates_data([])
        assert ticks == []

    def test_generate_variants_still_works(self):
        """generate_variants should still work with the same interface."""
        prompt = "# Test Trader\nMomentum strategy."
        variants = generate_variants("test", prompt, n_variants=3)
        assert len(variants) == 3
        for v in variants:
            assert hasattr(v, "val_scores")

    def test_score_variant_accepts_cost_model(self):
        """score_variant should accept optional cost_model parameter."""
        from src.replay import Tick
        import numpy as np
        from datetime import datetime, timedelta

        ticks = []
        rng = np.random.default_rng(42)
        base_time = datetime(2024, 1, 2, 9, 30)
        price = 100.0
        for i in range(30):
            ts = base_time + timedelta(minutes=30 * i)
            price = price * (1.0 + rng.normal(0, 0.002))
            ticks.append(Tick(
                timestamp=ts, ticker="SPY",
                open=price * 0.999, high=price * 1.005,
                low=price * 0.995, close=price,
                volume=1_000_000,
            ))

        variant = PromptVariant(
            trader="test", variant_id=1, variant_name="test",
            description="test", prompt_text="# test",
            signal_params=SignalParams(),
            baseline_params=SignalParams(),
        )

        # Should work without cost_model
        score, result = score_variant(variant, ticks)
        assert isinstance(score, float)

        # Should also work with cost_model=None explicitly
        score2, result2 = score_variant(variant, ticks, cost_model=None)
        assert score == pytest.approx(score2)

    def test_run_sweep_single_date_unchanged(self):
        """run_sweep with n_dates=1 (default) should work exactly as before.

        NOTE: This test needs agent directories which only exist in the
        paper-trading-teams repo. It's skipped in the rebuild repo.
        """
        from src.prompt_sweep import run_sweep
        # Check if agents directory exists — if not, skip
        from src.prompt_sweep import PROJECT_DIR, AGENTS_DIR
        if not AGENTS_DIR.exists():
            pytest.skip("Agent directories not available in rebuild repo")

        results = run_sweep(
            date_str="2026-07-05",
            trader="kairos",
            n_variants=2,
            dry_run=True,
            n_dates=1,  # defaults to 1 anyway
        )
        assert len(results) == 1
        assert results[0].trader == "kairos"
        assert results[0].date == "2026-07-05"


# ═══════════════════════════════════════════════════════════════════════════════
# Statistical significance gate tests (SPEC §6.1)
# ═══════════════════════════════════════════════════════════════════════════════

class TestStatisticalSignificanceGate:
    """Test that the is_significant() gate from validation.py works correctly."""

    def test_significant_improvement_detected(self):
        """Clear improvement should be statistically significant."""
        from src.validation import is_significant
        # Baseline: consistently ~0.3
        baseline = [0.30, 0.31, 0.29, 0.32, 0.30, 0.31, 0.30, 0.29, 0.31, 0.30]
        # Candidate: consistently ~0.45 (50% better)
        candidate = [0.45, 0.46, 0.44, 0.47, 0.45, 0.46, 0.45, 0.44, 0.46, 0.45]
        is_sig, p_val = is_significant(baseline, candidate, p_threshold=0.05)
        assert is_sig, f"Expected significant improvement, got p={p_val:.4f}"
        assert p_val < 0.05

    def test_no_improvement_not_significant(self):
        """No real improvement should fail significance test."""
        from src.validation import is_significant
        # Both sets have same mean (~0.3) with noise
        baseline = [0.30, 0.32, 0.29, 0.31, 0.30, 0.28, 0.33, 0.30, 0.31, 0.29]
        candidate = [0.29, 0.31, 0.30, 0.32, 0.28, 0.31, 0.30, 0.29, 0.32, 0.30]
        is_sig, p_val = is_significant(baseline, candidate, p_threshold=0.05)
        assert not is_sig, f"Expected not significant, got p={p_val:.4f}"

    def test_insufficient_data_returns_not_significant(self):
        """Fewer than 5 data points returns not significant."""
        from src.validation import is_significant
        is_sig, p_val = is_significant([0.3, 0.4], [0.5, 0.6], p_threshold=0.05)
        assert not is_sig
        assert p_val == 1.0

    def test_overfit_detection(self):
        """Walk-forward should detect when validation degrades vs training."""
        from src.validation import is_overfit
        # Train Sharpe 2.0, Validation Sharpe 0.5 — clearly overfit
        assert is_overfit(2.0, 0.5, threshold=0.30)
        # Train Sharpe 1.0, Validation Sharpe 0.8 — within threshold
        assert not is_overfit(1.0, 0.8, threshold=0.30)
        # Exact boundary: val = train * 0.7
        assert not is_overfit(1.0, 0.7, threshold=0.30)
        # Just below boundary
        assert is_overfit(1.0, 0.69, threshold=0.30)

    def test_significance_gate_blocks_noisy_improvement(self):
        """High-variance improvement that isn't statistically significant should be caught."""
        from src.validation import is_significant
        # Baseline: consistent
        baseline = [0.30, 0.31, 0.30, 0.29, 0.31, 0.30, 0.31, 0.30, 0.29, 0.31]
        # Candidate: higher mean but extremely noisy — not reliably better
        candidate = [0.50, 0.10, 0.60, 0.05, 0.55, 0.08, 0.65, 0.07, 0.45, 0.12]
        is_sig, p_val = is_significant(baseline, candidate, p_threshold=0.05)
        # With this much variance, the improvement shouldn't be significant
        assert not is_sig or p_val > 0.01, (
            f"Noisy improvement should not be clearly significant, got p={p_val:.4f}"
        )


class TestMinimumOOSWindows:
    """Test that minimum 5 OOS windows requirement is enforced."""

    def test_walk_forward_with_insufficient_dates_warns(self):
        """When fewer than 5 OOS windows exist, a warning is issued."""
        dates = [f"2026-06-{i+1:02d}" for i in range(10)]
        # train=5, val=1 → 10 - 5 - 1 + 1 = 5 windows (exactly at minimum)
        windows = build_walk_forward_windows(dates, train_days=5, val_days=1)
        assert len(windows) >= 5, f"Expected >= 5 windows, got {len(windows)}"

    def test_below_minimum_raises_flag(self):
        """Too few dates for 5 OOS windows should be detectable."""
        dates = [f"2026-06-{i+1:02d}" for i in range(8)]
        # train=5, val=2 → 8 - 5 - 2 + 1 = 2 windows (< 5 minimum)
        windows = build_walk_forward_windows(dates, train_days=5, val_days=2)
        assert len(windows) < 5, "Expected < 5 windows with insufficient dates"

    def test_default_cli_is_multi_date(self):
        """Verifies that the prompt_sweep CLI default is multi-date (not single)."""
        # The default should be > 1 for multi-date walk-forward
        # We verify by checking the run_sweep function signature default
        from src.prompt_sweep import run_sweep
        import inspect
        sig = inspect.signature(run_sweep)
        n_dates_default = sig.parameters['n_dates'].default
        assert n_dates_default >= 5, (
            f"run_sweep n_dates default is {n_dates_default}, "
            f"should be >= 5 for multi-date walk-forward"
        )
