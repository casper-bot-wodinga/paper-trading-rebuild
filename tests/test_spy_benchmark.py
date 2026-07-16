"""
Tests for scripts/spy_benchmark.py — SPY buy-and-hold benchmark overlay.

Tests metrics computation (returns, Sharpe, Calmar, drawdown, volatility)
and the benchmark function with mocked bar data.
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from scripts.spy_benchmark import (
    compute_spy_benchmark,
    format_benchmark,
    format_vs_spy,
    _compute_returns,
    _max_drawdown,
    _sharpe_ratio,
    _calmar_ratio,
    _win_rate,
    _volatility,
    _get_trading_dates,
    _fetch_spy_bars,
)


# ═══════════════════════════════════════════════════════════════════════════════
# _compute_returns
# ═══════════════════════════════════════════════════════════════════════════════


class TestComputeReturns:
    def test_normal(self):
        closes = [100.0, 101.0, 102.0]
        result = _compute_returns(closes)
        assert len(result) == 2
        assert abs(result[0] - 0.01) < 0.001  # 1% return
        assert abs(result[1] - (102 / 101 - 1)) < 0.001

    def test_single_price(self):
        assert _compute_returns([100.0]) == []

    def test_empty(self):
        assert _compute_returns([]) == []

    def test_declining(self):
        closes = [100.0, 99.0, 98.0]
        result = _compute_returns(closes)
        assert result[0] < 0
        assert result[1] < 0


# ═══════════════════════════════════════════════════════════════════════════════
# _max_drawdown
# ═══════════════════════════════════════════════════════════════════════════════


class TestMaxDrawdown:
    def test_no_drawdown(self):
        """All-increasing prices => 0 drawdown."""
        assert _max_drawdown([100.0, 101.0, 102.0, 103.0]) == 0.0

    def test_simple_drawdown(self):
        """Peak at 100, drop to 90, recover."""
        dd = _max_drawdown([100.0, 90.0, 100.0])
        assert abs(dd - 0.10) < 0.001

    def test_second_peak_deeper_drawdown(self):
        """First drawdown 10%, second drawdown 20%."""
        dd = _max_drawdown([100.0, 90.0, 95.0, 120.0, 96.0])
        assert abs(dd - 0.20) < 0.001  # (120-96)/120

    def test_empty(self):
        assert _max_drawdown([]) == 0.0

    def test_single_price(self):
        assert _max_drawdown([100.0]) == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# _sharpe_ratio
# ═══════════════════════════════════════════════════════════════════════════════


class TestSharpeRatio:
    def test_positive_returns(self):
        returns = [0.01, 0.01, 0.01]  # 1% daily
        sr = _sharpe_ratio(returns, risk_free_rate=0.0)
        # All same return => std=0 => Sharpe should be 0
        assert sr == 0.0

    def test_mixed_returns(self):
        returns = [0.01, -0.005, 0.02, -0.01, 0.015]
        sr = _sharpe_ratio(returns, risk_free_rate=0.0)
        assert sr != 0.0
        assert isinstance(sr, float)

    def test_empty(self):
        assert _sharpe_ratio([]) == 0.0

    def test_single_return(self):
        assert _sharpe_ratio([0.01]) == 0.0

    def test_all_zeros(self):
        assert _sharpe_ratio([0.0, 0.0, 0.0]) == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# _calmar_ratio
# ═══════════════════════════════════════════════════════════════════════════════


class TestCalmarRatio:
    def test_normal(self):
        # 20% return, 10% drawdown => Calmar = 2.0
        cr = _calmar_ratio(20.0, 10.0)
        assert abs(cr - 2.0) < 0.01

    def test_zero_drawdown(self):
        assert _calmar_ratio(10.0, 0.0) == 0.0

    def test_zero_return(self):
        assert _calmar_ratio(0.0, 5.0) == 0.0

    def test_negative_return(self):
        cr = _calmar_ratio(-5.0, 10.0)
        assert cr < 0


# ═══════════════════════════════════════════════════════════════════════════════
# _win_rate
# ═══════════════════════════════════════════════════════════════════════════════


class TestWinRate:
    def test_all_wins(self):
        assert _win_rate([0.01, 0.02, 0.005]) == 1.0

    def test_all_losses(self):
        assert _win_rate([-0.01, -0.02]) == 0.0

    def test_mixed(self):
        assert _win_rate([0.01, -0.01, 0.02, -0.02]) == 0.5

    def test_empty(self):
        assert _win_rate([]) == 0.0

    def test_zeros_not_positive(self):
        """Exact zero is not a win."""
        assert _win_rate([0.0, 0.01]) == 0.5


# ═══════════════════════════════════════════════════════════════════════════════
# _volatility
# ═══════════════════════════════════════════════════════════════════════════════


class TestVolatility:
    def test_constant_returns(self):
        """All same return => zero volatility."""
        vol = _volatility([0.01, 0.01, 0.01])
        assert vol == 0.0

    def test_mixed_returns(self):
        vol = _volatility([0.01, -0.02, 0.03, -0.01, 0.015])
        assert vol > 0.0

    def test_empty(self):
        assert _volatility([]) == 0.0

    def test_single_return(self):
        assert _volatility([0.01]) == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# _get_trading_dates
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetTradingDates:
    def test_basic(self):
        """Get trading dates without calendar (uses M-F fallback)."""
        dates = _get_trading_dates("2026-07-15", 5)
        assert len(dates) == 5
        assert dates[-1] == "2026-07-15"  # End date is included
        assert dates == sorted(dates)
        # All should be valid dates
        from datetime import date
        for d in dates:
            date.fromisoformat(d)

    def test_with_calendar(self):
        """Use pre-resolved calendar — only days in calendar are used."""
        calendar = ["2026-07-10", "2026-07-13", "2026-07-14",
                     "2026-07-15", "2026-07-16"]
        dates = _get_trading_dates("2026-07-15", 3, calendar=calendar)
        assert dates == ["2026-07-13", "2026-07-14", "2026-07-15"]

    def test_calendar_short(self):
        """Calendar has fewer dates than requested — return all available."""
        calendar = ["2026-07-14", "2026-07-15"]
        dates = _get_trading_dates("2026-07-15", 5, calendar=calendar)
        assert len(dates) == 2

    def test_end_date_not_in_calendar(self):
        """End date not a trading day — closes before end_date."""
        calendar = ["2026-07-10", "2026-07-13", "2026-07-14"]
        dates = _get_trading_dates("2026-07-15", 3, calendar=calendar)
        assert dates == ["2026-07-10", "2026-07-13", "2026-07-14"]


# ═══════════════════════════════════════════════════════════════════════════════
# _fetch_spy_bars
# ═══════════════════════════════════════════════════════════════════════════════


class TestFetchSpyBars:
    def test_data_bus_available(self):
        """When data bus responds, use its data."""
        mock_bars = [
            {"close": 500.0, "open": 499.0, "high": 501.0, "low": 498.0,
             "volume": 1000000, "timestamp": "2026-07-10"},
            {"close": 505.0, "open": 500.0, "high": 506.0, "low": 499.0,
             "volume": 1100000, "timestamp": "2026-07-11"},
        ]
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"bars": mock_bars}).encode()
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            bars = _fetch_spy_bars("2026-07-10", "2026-07-11")
            assert len(bars) == 2
            assert bars[0]["close"] == 500.0

    def test_data_bus_list_format(self):
        """Data bus returns a list instead of {bars: [...]}."""
        mock_bars = [{"close": 500.0}]
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(mock_bars).encode()
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            bars = _fetch_spy_bars("2026-07-10", "2026-07-11")
            assert len(bars) == 1

    def test_data_bus_unavailable_falls_back(self):
        """When data bus fails, fall back to yfinance."""
        with patch("urllib.request.urlopen", side_effect=OSError("no bus")):
            with patch("src.data_fetcher.fetch_bars_yfinance") as mock_yf:
                mock_yf.return_value = [{"close": 500.0}]
                bars = _fetch_spy_bars("2026-07-10", "2026-07-11")
                assert len(bars) == 1
                mock_yf.assert_called_once()

    def test_all_sources_fail(self):
        """When everything fails, return empty list."""
        with patch("urllib.request.urlopen", side_effect=OSError("no bus")):
            with patch("src.data_fetcher.fetch_bars_yfinance",
                       side_effect=ImportError("no yf")):
                bars = _fetch_spy_bars("2026-07-10", "2026-07-11")
                assert bars == []


# ═══════════════════════════════════════════════════════════════════════════════
# compute_spy_benchmark
# ═══════════════════════════════════════════════════════════════════════════════


class TestComputeSpyBenchmark:
    def test_successful_benchmark(self):
        """Happy path: SPY data available, compute all metrics."""
        mock_bars = [
            {"close": 500.0, "open": 499.0, "high": 501.0, "low": 498.0,
             "volume": 1000000, "timestamp": "2026-07-01"},
            {"close": 505.0, "open": 502.0, "high": 506.0, "low": 501.0,
             "volume": 1100000, "timestamp": "2026-07-02"},
            {"close": 510.0, "open": 506.0, "high": 511.0, "low": 505.0,
             "volume": 1200000, "timestamp": "2026-07-03"},
            {"close": 508.0, "open": 509.0, "high": 512.0, "low": 507.0,
             "volume": 1050000, "timestamp": "2026-07-06"},
            {"close": 515.0, "open": 510.0, "high": 516.0, "low": 509.0,
             "volume": 1150000, "timestamp": "2026-07-07"},
        ]
        with patch("scripts.spy_benchmark._fetch_spy_bars", return_value=mock_bars):
            result = compute_spy_benchmark(date_str="2026-07-07", n_dates=5)
            assert result["error"] is None
            assert result["total_return_pct"] == 3.0  # (515-500)/500
            assert result["start_price"] == 500.0
            assert result["end_price"] == 515.0
            assert result["n_bars"] == 5
            assert result["sharpe_ratio"] != 0.0
            assert result["max_drawdown_pct"] >= 0.0

    def test_no_bars(self):
        """No SPY data available."""
        with patch("scripts.spy_benchmark._fetch_spy_bars", return_value=[]):
            result = compute_spy_benchmark(date_str="2026-07-07", n_dates=5)
            assert result["error"] is not None

    def test_single_bar(self):
        """Single bar is not enough for metrics."""
        with patch("scripts.spy_benchmark._fetch_spy_bars",
                   return_value=[{"close": 500.0}]):
            result = compute_spy_benchmark(date_str="2026-07-07", n_dates=5)
            assert result["error"] is not None
            assert "need ≥ 2" in result["error"]

    def test_default_date(self):
        """Default date is yesterday."""
        mock_bars = [
            {"close": 500.0}, {"close": 505.0},
        ]
        with patch("scripts.spy_benchmark._fetch_spy_bars", return_value=mock_bars):
            result = compute_spy_benchmark(n_dates=5)
            assert result["error"] is None
            assert result["total_return_pct"] == 1.0

    def test_drawdown_detected(self):
        """Prices that drop midway should show drawdown."""
        mock_bars = [
            {"close": 500.0},
            {"close": 450.0},  # -10%
            {"close": 460.0},
        ]
        with patch("scripts.spy_benchmark._fetch_spy_bars", return_value=mock_bars):
            result = compute_spy_benchmark(date_str="2026-07-03", n_dates=3)
            assert result["max_drawdown_pct"] == 10.0


# ═══════════════════════════════════════════════════════════════════════════════
# format_benchmark
# ═══════════════════════════════════════════════════════════════════════════════


class TestFormatBenchmark:
    def test_successful(self):
        bench = {
            "start_date": "2026-07-01",
            "end_date": "2026-07-15",
            "trading_dates_available": 10,
            "n_bars": 10,
            "start_price": 500.0,
            "end_price": 515.0,
            "total_return_pct": 3.0,
            "max_drawdown_pct": 2.0,
            "sharpe_ratio": 1.5,
            "calmar_ratio": 1.5,
            "win_rate_pct": 60.0,
            "annualized_vol_pct": 15.0,
            "error": None,
        }
        result = format_benchmark(bench)
        assert "SPY Buy-and-Hold Benchmark" in result
        assert "$500" in result
        assert "$515" in result
        assert "+3.00%" in result
        assert "1.500" in result

    def test_error(self):
        bench = {"error": "No data available"}
        result = format_benchmark(bench)
        assert "⚠️" in result
        assert "No data available" in result


# ═══════════════════════════════════════════════════════════════════════════════
# format_vs_spy
# ═══════════════════════════════════════════════════════════════════════════════


class TestFormatVsSpy:
    def test_comparison(self):
        bench = {
            "total_return_pct": 3.0,
            "max_drawdown_pct": 2.0,
            "sharpe_ratio": 1.5,
            "calmar_ratio": 1.5,
            "win_rate_pct": 60.0,
            "annualized_vol_pct": 15.0,
        }
        variant = {
            "total_return_pct": 5.0,
            "max_drawdown_pct": 1.5,
            "sharpe_ratio": 2.0,
            "calmar_ratio": 3.33,
            "win_rate_pct": 70.0,
            "annualized_vol_pct": 12.0,
        }
        result = format_vs_spy(bench, variant)
        assert "vs. SPY" in result
        assert "Total Return" in result
        # Variant beats SPY on return (5 > 3)
        assert "✅" in result

    def test_variant_loses(self):
        bench = {
            "total_return_pct": 5.0,
            "max_drawdown_pct": 1.0,
            "sharpe_ratio": 2.0,
            "calmar_ratio": 5.0,
            "win_rate_pct": 70.0,
            "annualized_vol_pct": 10.0,
        }
        variant = {
            "total_return_pct": 1.0,
            "max_drawdown_pct": 5.0,
            "sharpe_ratio": 0.5,
            "calmar_ratio": 0.2,
            "win_rate_pct": 40.0,
            "annualized_vol_pct": 25.0,
        }
        result = format_vs_spy(bench, variant)
        assert "❌" in result

    def test_benchmark_error(self):
        bench = {"error": "No data"}
        variant = {"total_return_pct": 5.0}
        result = format_vs_spy(bench, variant)
        assert "unavailable" in result
