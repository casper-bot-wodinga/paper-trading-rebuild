"""Tests for src/holdout.py — 15% test slice for quarterly evaluation.

Run:  pytest tests/test_holdout.py -v
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

import pytest

from src.holdout import HoldoutManager, HoldoutResult, QuarterlyEvalSummary
from src.transaction_costs import CostModel


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def holdout_manager(tmp_path: Path) -> HoldoutManager:
    """HoldoutManager using a temp file."""
    return HoldoutManager(holdout_file=tmp_path / "holdout.json")


@pytest.fixture
def trading_dates() -> List[str]:
    """100 consecutive trading days (skipping weekends)."""
    dates: List[str] = []
    d = datetime(2026, 1, 2)
    while len(dates) < 100:
        if d.weekday() < 5:  # Mon-Fri
            dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return dates


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: create_holdout
# ═══════════════════════════════════════════════════════════════════════════════


def test_create_holdout_15_percent(holdout_manager, trading_dates):
    """15% of 100 trading days = 15 holdout dates."""
    holdout = holdout_manager.create_holdout(trading_dates, fraction=0.15)
    assert len(holdout) == 15
    assert holdout == trading_dates[-15:]  # most recent 15%


def test_create_holdout_10_percent(holdout_manager, trading_dates):
    """10% of 100 = 10 holdout dates."""
    holdout = holdout_manager.create_holdout(trading_dates, fraction=0.10)
    assert len(holdout) == 10
    assert holdout == trading_dates[-10:]  # most recent 10%


def test_create_holdout_minimum_1_date(holdout_manager):
    """Even with very small fraction, at least 1 date is reserved."""
    dates = [f"2026-01-{d:02d}" for d in range(1, 22)]  # 21 trading days
    holdout = holdout_manager.create_holdout(dates, fraction=0.01)
    assert len(holdout) == 1
    assert holdout == [dates[-1]]  # most recent


def test_create_holdout_requires_20_dates(holdout_manager):
    """Less than 20 trading days raises ValueError."""
    dates = [f"2026-01-{d:02d}" for d in range(1, 10)]  # 9 dates
    with pytest.raises(ValueError, match="at least 20"):
        holdout_manager.create_holdout(dates)


def test_create_holdout_rejects_existing(holdout_manager, trading_dates):
    """Creating holdout again without force=True raises ValueError."""
    holdout_manager.create_holdout(trading_dates)
    with pytest.raises(ValueError, match="already exists"):
        holdout_manager.create_holdout(trading_dates)


def test_create_holdout_force_overwrites(holdout_manager, trading_dates):
    """force=True overwrites existing holdout set."""
    holdout_manager.create_holdout(trading_dates, fraction=0.15)
    new_dates = trading_dates[:-20]  # fewer dates
    holdout = holdout_manager.create_holdout(new_dates, fraction=0.15, force=True)
    assert len(holdout) == len(new_dates[-int(len(new_dates) * 0.15):])


def test_create_holdout_bounds_check(holdout_manager, trading_dates):
    """Fraction must be in (0.0, 0.5)."""
    with pytest.raises(ValueError, match="0.0"):
        holdout_manager.create_holdout(trading_dates, fraction=0.0)
    with pytest.raises(ValueError, match="0.5"):
        holdout_manager.create_holdout(trading_dates, fraction=0.5)
    with pytest.raises(ValueError, match="0.5"):
        holdout_manager.create_holdout(trading_dates, fraction=0.6)


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: get_holdout_dates
# ═══════════════════════════════════════════════════════════════════════════════


def test_get_holdout_dates_empty_by_default(holdout_manager):
    """No holdout set → empty list."""
    assert holdout_manager.get_holdout_dates() == []


def test_get_holdout_dates_after_create(holdout_manager, trading_dates):
    """After creation, returns the holdout dates."""
    holdout_manager.create_holdout(trading_dates)
    got = holdout_manager.get_holdout_dates()
    assert len(got) == 15
    assert got == trading_dates[-15:]


def test_get_holdout_dates_cached(holdout_manager, trading_dates):
    """get_holdout_dates should return cached value."""
    holdout_manager.create_holdout(trading_dates)
    holdout_manager._cache = ["2026-06-01"]  # corrupt cache
    got = holdout_manager.get_holdout_dates()
    assert got == ["2026-06-01"]  # returns cache


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: filter_holdout
# ═══════════════════════════════════════════════════════════════════════════════


def test_filter_holdout_removes_holdout_dates(holdout_manager, trading_dates):
    """filter_holdout should remove holdout dates from a list."""
    holdout_manager.create_holdout(trading_dates, fraction=0.15)
    # Original 100 dates → filtered should be 85 dates
    filtered = holdout_manager.filter_holdout(trading_dates)
    assert len(filtered) == 85
    # None of the filtered dates should be holdout dates
    for d in filtered:
        assert d not in holdout_manager.get_holdout_dates()


def test_filter_holdout_no_holdout_set(holdout_manager, trading_dates):
    """No holdout set → returns original list unchanged."""
    filtered = holdout_manager.filter_holdout(trading_dates)
    assert filtered == trading_dates


def test_filter_holdout_preserves_order(holdout_manager, trading_dates):
    """filter_holdout preserves chronological ordering of remaining dates."""
    holdout_manager.create_holdout(trading_dates, fraction=0.15)
    filtered = holdout_manager.filter_holdout(trading_dates)
    assert filtered == sorted(filtered)


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: is_holdout
# ═══════════════════════════════════════════════════════════════════════════════


def test_is_holdout_true(holdout_manager, trading_dates):
    """Most recent dates should be holdout."""
    holdout_manager.create_holdout(trading_dates, fraction=0.15)
    assert holdout_manager.is_holdout(trading_dates[-1])  # last date = holdout
    assert holdout_manager.is_holdout(trading_dates[-15])  # 15th from end = holdout


def test_is_holdout_false(holdout_manager, trading_dates):
    """Earlier dates should not be holdout."""
    holdout_manager.create_holdout(trading_dates, fraction=0.15)
    assert not holdout_manager.is_holdout(trading_dates[0])  # first date = training
    assert not holdout_manager.is_holdout(trading_dates[-16])  # just outside holdout


def test_is_holdout_no_set(holdout_manager):
    """No holdout set → always False."""
    assert not holdout_manager.is_holdout("2026-01-02")


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: persistence
# ═══════════════════════════════════════════════════════════════════════════════


def test_persistence_across_instances(tmp_path, trading_dates):
    """Holdout data persists when creating a new HoldoutManager on same file."""
    mgr1 = HoldoutManager(holdout_file=tmp_path / "holdout.json")
    mgr1.create_holdout(trading_dates)

    mgr2 = HoldoutManager(holdout_file=tmp_path / "holdout.json")
    got = mgr2.get_holdout_dates()
    assert len(got) == 15
    assert got == trading_dates[-15:]


def test_persistence_empty_file(tmp_path):
    """File exists but is empty/invalid → returns empty list."""
    f = tmp_path / "holdout.json"
    f.write_text("")
    mgr = HoldoutManager(holdout_file=f)
    assert mgr.get_holdout_dates() == []


def test_evaluation_history(tmp_path, trading_dates):
    """record_eval and get_evaluation_history work correctly."""
    mgr = HoldoutManager(holdout_file=tmp_path / "holdout.json")
    mgr.create_holdout(trading_dates)

    summary = QuarterlyEvalSummary(
        trader="kairos",
        holdout_dates=trading_dates[-15:],
        n_dates=15,
        n_dates_with_data=14,
        mean_objective_score=1.25,
        mean_calmar=0.85,
        mean_profit_factor=1.5,
        mean_win_rate=0.55,
        total_pnl=450.00,
        total_cost_adjusted_pnl=420.00,
        n_trades=30,
    )

    mgr.record_eval("kairos", summary)

    history = mgr.get_evaluation_history()
    assert len(history) == 1
    assert history[0]["trader"] == "kairos"
    assert history[0]["mean_objective_score"] == 1.25
    assert history[0]["total_pnl"] == 450.00


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: evaluation
# ═══════════════════════════════════════════════════════════════════════════════


def test_evaluate_no_holdout_set(holdout_manager):
    """evaluate with no holdout set returns empty summary."""
    summary = holdout_manager.evaluate(
        trader="kairos",
        variant_name="baseline",
        ticks_by_date={},
    )
    assert summary.n_dates == 0
    assert summary.mean_objective_score == 0.0


def test_evaluate_no_data(holdout_manager, trading_dates):
    """evaluate with holdout dates but no tick data returns zero summary."""
    holdout_manager.create_holdout(trading_dates, fraction=0.15)
    summary = holdout_manager.evaluate(
        trader="kairos",
        variant_name="baseline",
        ticks_by_date={},
    )
    assert summary.n_dates == 15
    assert summary.n_dates_with_data == 0
    assert summary.mean_objective_score == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: HoldoutResult and QuarterlyEvalSummary
# ═══════════════════════════════════════════════════════════════════════════════


def test_holdout_result_default_timestamp():
    """HoldoutResult gets a timestamp automatically."""
    hr = HoldoutResult(
        trader="kairos",
        variant_name="baseline",
        eval_date="2026-03-15",
        holdout_dates=["2026-03-15"],
        n_holdout_days=1,
        objective_score=1.0,
        calmar=0.5,
        profit_factor=1.2,
        win_rate=0.6,
        n_trades=5,
        total_pnl=100.0,
        cost_adjusted_pnl=95.0,
    )
    assert hr.timestamp  # non-empty
    assert "T" in hr.timestamp or " " in hr.timestamp  # ISO-like


def test_quarterly_eval_summary_default_timestamp():
    """QuarterlyEvalSummary gets a timestamp automatically."""
    summary = QuarterlyEvalSummary(
        trader="kairos",
        holdout_dates=["2026-03-15"],
        n_dates=1,
        n_dates_with_data=1,
        mean_objective_score=1.0,
        mean_calmar=0.5,
        mean_profit_factor=1.2,
        mean_win_rate=0.6,
        total_pnl=100.0,
        total_cost_adjusted_pnl=95.0,
        n_trades=5,
    )
    assert summary.timestamp  # non-empty


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: update_source_range
# ═══════════════════════════════════════════════════════════════════════════════


def test_update_source_range_no_holdout(holdout_manager, trading_dates):
    """update_source_range should be a no-op if no holdout set exists."""
    # Should not raise
    holdout_manager.update_source_range(trading_dates)


def test_update_source_range(holdout_manager, trading_dates):
    """update_source_range should update the source_range metadata."""
    holdout_manager.create_holdout(trading_dates)

    # Extend the date pool
    extended = trading_dates + ["2026-12-01", "2026-12-02"]
    holdout_manager.update_source_range(extended)

    # Holdout dates should still be the same
    assert holdout_manager.get_holdout_dates() == trading_dates[-15:]


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: edge cases
# ═══════════════════════════════════════════════════════════════════════════════


def test_create_holdout_fraction_rounding(holdout_manager):
    """Fraction such that n_holdout rounds to 1."""
    dates = [f"2026-01-{d:02d}" for d in range(1, 22)]  # 21 dates
    holdout = holdout_manager.create_holdout(dates, fraction=0.01)
    # 21 * 0.01 = 0.21 → max(1, 0) = 1
    assert len(holdout) == 1


def test_handle_empty_date_list(holdout_manager):
    """Empty date list for create should raise ValueError."""
    with pytest.raises(ValueError, match="at least 20"):
        holdout_manager.create_holdout([])


def test_filter_holdout_with_empty(holdout_manager):
    """Filter empty list → empty list is fine."""
    assert holdout_manager.filter_holdout([]) == []


def test_filter_holdout_with_no_overlap(holdout_manager, trading_dates):
    """Filter dates that aren't in holdout → all returned."""
    holdout_manager.create_holdout(trading_dates, fraction=0.15)
    random_dates = ["2099-01-01", "2099-01-02"]
    filtered = holdout_manager.filter_holdout(random_dates)
    assert len(filtered) == 2