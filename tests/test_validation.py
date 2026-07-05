"""Tests for src/validation.py — walk-forward validation, overfitting, significance."""
import pytest
from src.validation import (
    walk_forward_split,
    is_overfit,
    is_significant,
    TimeWindow,
)


class TestWalkForwardSplit:
    def test_single_window(self):
        """Minimal: exactly one window fits."""
        windows = list(walk_forward_split(n_days=120, train_window=90, val_window=30))
        assert len(windows) == 1
        w = windows[0]
        assert w.train_start == 0
        assert w.train_end == 90
        assert w.val_start == 90
        assert w.val_end == 120

    def test_multiple_windows(self):
        """120 data points, 60+30 windows with step=1 → 31 windows."""
        windows = list(walk_forward_split(n_days=120, train_window=60, val_window=30, step=1))
        assert len(windows) == 31  # 120 - 90 + 1

    def test_not_enough_data(self):
        """Less data than one window → empty."""
        windows = list(walk_forward_split(n_days=50, train_window=60, val_window=30))
        assert len(windows) == 0

    def test_train_before_val(self):
        """Training window always ends where validation begins."""
        for w in walk_forward_split(n_days=200, train_window=90, val_window=30, step=5):
            assert w.train_end == w.val_start
            assert w.train_start < w.train_end < w.val_end

    def test_step_size(self):
        """Step controls how many windows per day."""
        windows_small_step = list(walk_forward_split(n_days=150, train_window=60, val_window=30, step=1))
        windows_big_step = list(walk_forward_split(n_days=150, train_window=60, val_window=30, step=10))
        assert len(windows_small_step) > len(windows_big_step)


class TestIsOverfit:
    def test_not_overfit(self):
        """Validation close to training → not overfit."""
        assert not is_overfit(train_score=1.5, val_score=1.4, threshold=0.30)

    def test_overfit(self):
        """Validation much worse → overfit."""
        assert is_overfit(train_score=1.5, val_score=0.9, threshold=0.30)
        # 0.9 < 1.5 * 0.7 = 1.05

    def test_boundary(self):
        """Exactly at threshold edge."""
        # 1.5 * 0.7 = 1.05 → 1.05 is NOT < 1.05
        assert not is_overfit(train_score=1.5, val_score=1.05, threshold=0.30)

    def test_val_better_than_train(self):
        """Validation BETTER than training → not overfit (generalization)."""
        assert not is_overfit(train_score=1.0, val_score=1.5, threshold=0.30)


class TestIsSignificant:
    def test_clear_improvement(self):
        """Big consistent improvement → significant."""
        baseline = [0.5, 0.6, 0.55, 0.5, 0.6, 0.55, 0.5, 0.6]
        candidate = [0.8, 0.9, 0.85, 0.8, 0.9, 0.85, 0.8, 0.9]
        is_sig, p_val = is_significant(baseline, candidate)
        assert is_sig
        assert p_val < 0.05

    def test_no_improvement(self):
        """Similar scores → not significant."""
        baseline = [0.5, 0.6, 0.55, 0.5, 0.6, 0.55, 0.5, 0.6]
        candidate = [0.51, 0.59, 0.54, 0.51, 0.6, 0.56, 0.49, 0.61]
        is_sig, p_val = is_significant(baseline, candidate)
        # Should NOT be significant (very small difference)
        assert p_val > 0.05 or not is_sig

    def test_insufficient_data(self):
        """Less than 5 points → cannot determine significance."""
        is_sig, p_val = is_significant([0.5, 0.6], [0.6, 0.7])
        assert not is_sig
        assert p_val == 1.0
