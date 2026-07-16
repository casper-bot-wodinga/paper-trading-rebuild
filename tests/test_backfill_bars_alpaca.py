"""Tests for scripts/backfill_bars_alpaca.py — validation and repair logic."""

import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Import the Alpaca backfill module
import backfill_bars_alpaca as bba


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def good_bars_df():
    """Create realistic OHLCV bars with price variation (78 bars, one day)."""
    times = pd.date_range("2026-07-06 09:30", "2026-07-06 16:00", freq="5min",
                          tz="America/New_York")
    # Prices with realistic variation — random walk with ~40 distinct closes
    import random
    random.seed(42)
    base = 550.0
    rows = []
    for i, ts in enumerate(times):
        # Random walk with mean-reversion to produce 30+ distinct close values
        base += random.uniform(-0.8, 1.0)
        base = max(540, min(560, base))
        close = round(base, 2)
        rows.append({
            "timestamp": ts,
            "open": round(close - 0.2, 2),
            "high": round(close + 0.5, 2),
            "low": round(close - 0.5, 2),
            "close": close,
            "volume": 10000.0 + i * 100,
        })
    return pd.DataFrame(rows)


@pytest.fixture
def flat_bars_df():
    """Create bars with all identical close prices (the P0 bug pattern)."""
    times = pd.date_range("2026-07-15 09:30", "2026-07-15 16:00", freq="5min",
                          tz="America/New_York")
    rows = []
    for ts in times:
        rows.append({
            "timestamp": ts,
            "open": 751.83,
            "high": 751.83,
            "low": 751.83,
            "close": 751.83,
            "volume": 0.0,
        })
    return pd.DataFrame(rows)


@pytest.fixture
def sparse_bars_df():
    """Create bars with only 5 bars (too few)."""
    times = pd.date_range("2026-07-15 09:30", periods=5, freq="5min",
                          tz="America/New_York")
    rows = []
    for i, ts in enumerate(times):
        close = 100.0 + i
        rows.append({
            "timestamp": ts,
            "open": close - 0.1,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 10000.0,
        })
    return pd.DataFrame(rows)


@pytest.fixture
def temp_bars_dir():
    """Temporary bars directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        orig = bba.BARS_DIR
        bba.BARS_DIR = Path(tmpdir)
        yield Path(tmpdir)
        bba.BARS_DIR = orig


# ══════════════════════════════════════════════════════════════════════════════
# validate_bars
# ══════════════════════════════════════════════════════════════════════════════

def test_validate_good_bars_passes(good_bars_df):
    """Good bars with price variation pass validation."""
    is_valid, issues = bba.validate_bars(good_bars_df, "SPY")
    assert is_valid, f"Good bars should pass validation, got: {issues}"
    assert len(issues) == 0


def test_validate_flat_bars_fails(flat_bars_df):
    """All identical close prices → fails validation."""
    is_valid, issues = bba.validate_bars(flat_bars_df, "SPY")
    assert not is_valid, "Flat bars should fail validation"
    # Should have at least an issue about distinct closes or zero range
    flat_issues = [i for i in issues if "distinct" in i.lower() or "zero" in i.lower()]
    assert len(flat_issues) >= 1, f"Expected flat-price issue, got: {issues}"


def test_validate_sparse_bars_fails(sparse_bars_df):
    """Too few bars → fails validation."""
    is_valid, issues = bba.validate_bars(sparse_bars_df, "SPY")
    assert not is_valid
    assert any("only 5 bars" in i or "30" in i for i in issues), \
        f"Expected sparse-bar issue, got: {issues}"


def test_validate_empty_df():
    """Empty DataFrame → fails."""
    is_valid, issues = bba.validate_bars(pd.DataFrame(), "SPY")
    assert not is_valid
    assert any("empty" in i.lower() for i in issues)


def test_validate_none():
    """None → fails."""
    is_valid, issues = bba.validate_bars(None, "SPY")
    assert not is_valid


def test_validate_multi_date_mixed():
    """One good date + one bad date → fails."""
    times_good = pd.date_range("2026-07-06 09:30", periods=78, freq="5min",
                               tz="America/New_York")
    times_bad = pd.date_range("2026-07-15 09:30", periods=78, freq="5min",
                               tz="America/New_York")

    import random
    random.seed(99)
    base = 500.0
    good_rows = []
    for i, ts in enumerate(times_good):
        base += random.uniform(-0.7, 0.9)
        base = max(490, min(510, base))
        close = round(base, 2)
        good_rows.append({"timestamp": ts, "open": round(close-0.15,2),
                          "high": round(close+0.45,2), "low": round(close-0.45,2),
                          "close": close, "volume": 10000.0})
    bad_rows = [{"timestamp": ts, "open": 751.83, "high": 751.83,
                 "low": 751.83, "close": 751.83, "volume": 0.0}
                for ts in times_bad]

    df = pd.DataFrame(good_rows + bad_rows)
    is_valid, issues = bba.validate_bars(df, "SPY")
    assert not is_valid
    # Should flag 2026-07-15 as bad but not 2026-07-06
    assert any("2026-07-15" in i for i in issues), f"Expected 07-15 flagged, got: {issues}"
    assert not any("2026-07-06" in i for i in issues), \
        f"2026-07-06 should pass, got: {issues}"


# ══════════════════════════════════════════════════════════════════════════════
# bad_dates_in_cache
# ══════════════════════════════════════════════════════════════════════════════

def test_bad_dates_finds_flat_data(temp_bars_dir, flat_bars_df):
    """Bad data in cache → returns bad date."""
    path = temp_bars_dir / "SPY.parquet"
    flat_bars_df.to_parquet(path, index=False)

    bad_dates = bba.bad_dates_in_cache("SPY")
    assert "2026-07-15" in bad_dates, \
        f"Expected 2026-07-15 as bad date, got: {bad_dates}"


def test_bad_dates_returns_empty_for_good_data(temp_bars_dir, good_bars_df):
    """Good data → returns empty list."""
    path = temp_bars_dir / "SPY.parquet"
    good_bars_df.to_parquet(path, index=False)

    bad_dates = bba.bad_dates_in_cache("SPY")
    assert bad_dates == [], f"Expected no bad dates, got: {bad_dates}"


def test_bad_dates_no_file(temp_bars_dir):
    """No file → returns empty list."""
    bad_dates = bba.bad_dates_in_cache("NONEXISTENT")
    assert bad_dates == []


# ══════════════════════════════════════════════════════════════════════════════
# missing_date_range — exclude today
# ══════════════════════════════════════════════════════════════════════════════

def test_missing_date_range_excludes_today():
    """End date should be yesterday, not today."""
    today = date.today()
    yesterday = today - timedelta(days=1)

    start, end = bba.missing_date_range("TEST", days=5, existing=set())
    assert start is not None
    assert end is not None
    end_d = date.fromisoformat(end)
    assert end_d <= yesterday, \
        f"End date {end} should be <= yesterday {yesterday}"


def test_missing_date_range_with_repair_dates():
    """Repair dates are always treated as missing even if in 'existing'."""
    today = date.today()
    # Mark ALL expected dates as existing
    existing = set()
    for i in range(8):  # days=5 + 1 (buffer) + today + yesterday
        d = today - timedelta(days=i)
        existing.add(d.strftime("%Y-%m-%d"))

    repair_date = (today - timedelta(days=3)).strftime("%Y-%m-%d")
    repair_set = {repair_date}

    start, end = bba.missing_date_range("TEST", days=5, existing=existing,
                                         repair_dates=repair_set)
    # Since we exclude today but repair_date is in range, it should be included
    # If repair_date is in the expected range, start/end should not be None
    assert start is not None, "Should have start date for repair"
    assert end is not None, "Should have end date for repair"


def test_missing_date_range_fully_covered_except_today():
    """All dates including today covered → skip (today excluded automatically)."""
    today = date.today()
    existing = set()
    for i in range(9):  # cover more than days range
        d = today - timedelta(days=i)
        existing.add(d.strftime("%Y-%m-%d"))

    # With repair_dates=None, should detect nothing missing
    start, end = bba.missing_date_range("TEST", days=5, existing=existing)
    # Since end_date is yesterday and all those dates exist → None, None
    assert start is None
    assert end is None


# ══════════════════════════════════════════════════════════════════════════════
# resolve_tickers (delegate to backfill_bars_alpaca's version)
# ══════════════════════════════════════════════════════════════════════════════

def test_resolve_core():
    tickers = bba.resolve_tickers("core")
    assert len(tickers) == 8
    assert "SPY" in tickers


def test_resolve_all():
    tickers = bba.resolve_tickers("all")
    assert len(tickers) >= 30


def test_resolve_comma_list():
    tickers = bba.resolve_tickers("AAPL,MSFT,NVDA")
    assert tickers == ["AAPL", "MSFT", "NVDA"]


# ══════════════════════════════════════════════════════════════════════════════
# merge_and_dedup
# ══════════════════════════════════════════════════════════════════════════════

def test_merge_no_existing(temp_bars_dir, good_bars_df):
    path = temp_bars_dir / "NEW.parquet"
    result = bba.merge_and_dedup(path, good_bars_df)
    assert len(result) == len(good_bars_df)
    assert result["timestamp"].is_monotonic_increasing


def test_merge_dedup_keeps_newer(temp_bars_dir):
    ts = pd.Timestamp("2026-07-01 14:30:00", tz="America/New_York")
    existing = pd.DataFrame({
        "timestamp": [ts],
        "open": [100.0], "high": [101.0], "low": [99.0],
        "close": [100.5], "volume": [10000.0],
    })
    path = temp_bars_dir / "DEDUP.parquet"
    existing.to_parquet(path, index=False)

    new = pd.DataFrame({
        "timestamp": [ts],
        "open": [200.0], "high": [201.0], "low": [199.0],
        "close": [200.5], "volume": [20000.0],
    })
    result = bba.merge_and_dedup(path, new)
    assert len(result) == 1
    assert result["open"].iloc[0] == 200.0


# ══════════════════════════════════════════════════════════════════════════════
# atomic_write
# ══════════════════════════════════════════════════════════════════════════════

def test_atomic_write_creates_file(temp_bars_dir, good_bars_df):
    path = temp_bars_dir / "ATOMIC.parquet"
    bba.atomic_write(good_bars_df, path)
    assert path.exists()
    df = pd.read_parquet(path)
    assert len(df) == len(good_bars_df)
