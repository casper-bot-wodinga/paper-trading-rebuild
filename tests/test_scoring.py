"""Tests for src/scoring.py — composite agent scoring."""

import sys
import os
from unittest.mock import patch, MagicMock

import pytest

# Ensure src is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.scoring import (
    compute_score,
    compute_all_scores,
    LIVE_AGENTS,
    STARTING_VALUE,
    _get_max_drawdown,
    _get_violation_count,
    _get_margin_events,
    _get_win_rate,
    _get_latest_portfolio,
)


# ── compute_score ──────────────────────────────────────────────────────────

def test_compute_score_no_portfolio():
    """Returns zero/default dict when no portfolio data available."""
    with patch("src.scoring._get_latest_portfolio", return_value=None):
        result = compute_score("trader-kairos")
        assert result["score"] == 0.0
        assert result["ending_value"] == 0.0
        assert result["total_return"] == 0.0
        assert result["score_components"] == {"error": "no portfolio data"}


def test_compute_score_basic_positive_return():
    """Positive return with no drawdown or violations."""
    with patch("src.scoring._get_latest_portfolio") as mock_port, \
         patch("src.scoring._get_max_drawdown", return_value=0.0), \
         patch("src.scoring._get_violation_count", return_value=0), \
         patch("src.scoring._get_margin_events", return_value=0), \
         patch("src.scoring._get_win_rate", return_value=0.5):
        mock_port.return_value = {
            "portfolio_value": 10500.0,
            "cash": 2500.0,
            "daily_pnl": 500.0,
            "timestamp": "2026-07-16 12:00:00",
        }
        result = compute_score("trader-kairos")
        assert result["total_return"] == 0.05  # 5% return
        assert result["ending_value"] == 10500.00
        assert result["score"] == 0.05  # just base score
        assert result["max_drawdown"] == 0.0
        assert result["violation_count"] == 0


def test_compute_score_negative_return():
    """Negative return (portfolio value below starting)."""
    with patch("src.scoring._get_latest_portfolio") as mock_port, \
         patch("src.scoring._get_max_drawdown", return_value=0.0), \
         patch("src.scoring._get_violation_count", return_value=0), \
         patch("src.scoring._get_margin_events", return_value=0), \
         patch("src.scoring._get_win_rate", return_value=0.5):
        mock_port.return_value = {
            "portfolio_value": 9500.0,
            "cash": 5000.0,
            "daily_pnl": -500.0,
            "timestamp": "2026-07-16 12:00:00",
        }
        result = compute_score("trader-kairos")
        assert result["total_return"] == -0.05
        assert result["score"] == -0.05


def test_compute_score_with_drawdown():
    """Drawdown penalty reduces score proportionally."""
    with patch("src.scoring._get_latest_portfolio") as mock_port, \
         patch("src.scoring._get_max_drawdown", return_value=-0.20), \
         patch("src.scoring._get_violation_count", return_value=0), \
         patch("src.scoring._get_margin_events", return_value=0), \
         patch("src.scoring._get_win_rate", return_value=0.5):
        mock_port.return_value = {
            "portfolio_value": 11000.0,
            "cash": 1000.0,
            "daily_pnl": 1000.0,
            "timestamp": "2026-07-16 12:00:00",
        }
        result = compute_score("trader-kairos")
        # base: 0.10, drawdown penalty: -0.20 * 0.5 = -0.10
        assert result["total_return"] == 0.10
        assert result["max_drawdown"] == -0.20
        assert result["drawdown_penalty"] == -0.10
        assert result["score"] == 0.0  # 0.10 - 0.10 = 0


def test_compute_score_with_violations():
    """Risk gate vetoes reduce score by 2% each."""
    with patch("src.scoring._get_latest_portfolio") as mock_port, \
         patch("src.scoring._get_max_drawdown", return_value=0.0), \
         patch("src.scoring._get_violation_count", return_value=3), \
         patch("src.scoring._get_margin_events", return_value=0), \
         patch("src.scoring._get_win_rate", return_value=0.5):
        mock_port.return_value = {
            "portfolio_value": 11000.0,
            "cash": 1000.0,
            "daily_pnl": 500.0,
            "timestamp": "2026-07-16 12:00:00",
        }
        result = compute_score("trader-kairos")
        # base: 0.10, violations: -3 * 0.02 = -0.06
        assert result["violation_count"] == 3
        assert result["violation_penalties"] == -0.06
        assert result["score"] == 0.04  # 0.10 - 0.06 = 0.04


def test_compute_score_with_margin_events():
    """Margin events reduce score by 5% each."""
    with patch("src.scoring._get_latest_portfolio") as mock_port, \
         patch("src.scoring._get_max_drawdown", return_value=0.0), \
         patch("src.scoring._get_violation_count", return_value=0), \
         patch("src.scoring._get_margin_events", return_value=2), \
         patch("src.scoring._get_win_rate", return_value=0.5):
        mock_port.return_value = {
            "portfolio_value": 11000.0,
            "cash": 1000.0,
            "daily_pnl": 500.0,
            "timestamp": "2026-07-16 12:00:00",
        }
        result = compute_score("trader-kairos")
        # base: 0.10, margin: -2 * 0.05 = -0.10
        assert result["margin_events"] == 2
        assert result["violation_penalties"] == -0.10
        assert result["score"] == 0.0


def test_compute_score_win_rate_bonus():
    """Win rate > 50% gives a small bonus."""
    with patch("src.scoring._get_latest_portfolio") as mock_port, \
         patch("src.scoring._get_max_drawdown", return_value=0.0), \
         patch("src.scoring._get_violation_count", return_value=0), \
         patch("src.scoring._get_margin_events", return_value=0), \
         patch("src.scoring._get_win_rate", return_value=0.75):
        mock_port.return_value = {
            "portfolio_value": 10000.0,
            "cash": 5000.0,
            "daily_pnl": 0.0,
            "timestamp": "2026-07-16 12:00:00",
        }
        result = compute_score("trader-kairos")
        # bonus = (0.75 - 0.5) * 0.04 = 0.01
        assert result["win_rate"] == 0.75
        assert result["score"] == 0.01
        assert result["score_components"]["win_rate_bonus"] == 0.01


def test_compute_score_win_rate_at_50():
    """Win rate at exactly 50% gives no bonus."""
    with patch("src.scoring._get_latest_portfolio") as mock_port, \
         patch("src.scoring._get_max_drawdown", return_value=0.0), \
         patch("src.scoring._get_violation_count", return_value=0), \
         patch("src.scoring._get_margin_events", return_value=0), \
         patch("src.scoring._get_win_rate", return_value=0.50):
        mock_port.return_value = {
            "portfolio_value": 10000.0,
            "cash": 5000.0,
            "daily_pnl": 0.0,
            "timestamp": "2026-07-16 12:00:00",
        }
        result = compute_score("trader-kairos")
        assert result["score"] == 0.0
        assert result["score_components"]["win_rate_bonus"] == 0.0


def test_compute_score_win_rate_below_50():
    """Win rate < 50% gives no bonus and no penalty from win_rate."""
    with patch("src.scoring._get_latest_portfolio") as mock_port, \
         patch("src.scoring._get_max_drawdown", return_value=0.0), \
         patch("src.scoring._get_violation_count", return_value=0), \
         patch("src.scoring._get_margin_events", return_value=0), \
         patch("src.scoring._get_win_rate", return_value=0.30):
        mock_port.return_value = {
            "portfolio_value": 10000.0,
            "cash": 5000.0,
            "daily_pnl": 0.0,
            "timestamp": "2026-07-16 12:00:00",
        }
        result = compute_score("trader-kairos")
        assert result["score"] == 0.0
        assert result["score_components"]["win_rate_bonus"] == 0.0


def test_compute_score_win_rate_100():
    """100% win rate gives maximum bonus (0.02)."""
    with patch("src.scoring._get_latest_portfolio") as mock_port, \
         patch("src.scoring._get_max_drawdown", return_value=0.0), \
         patch("src.scoring._get_violation_count", return_value=0), \
         patch("src.scoring._get_margin_events", return_value=0), \
         patch("src.scoring._get_win_rate", return_value=1.0):
        mock_port.return_value = {
            "portfolio_value": 10000.0,
            "cash": 5000.0,
            "daily_pnl": 0.0,
            "timestamp": "2026-07-16 12:00:00",
        }
        result = compute_score("trader-kairos")
        # bonus = (1.0 - 0.5) * 0.04 = 0.02
        assert result["score"] == 0.02
        assert result["score_components"]["win_rate_bonus"] == 0.02


def test_compute_score_combined_penalties():
    """Score combines drawdown, violations, margin events, and win rate."""
    with patch("src.scoring._get_latest_portfolio") as mock_port, \
         patch("src.scoring._get_max_drawdown", return_value=-0.10), \
         patch("src.scoring._get_violation_count", return_value=1), \
         patch("src.scoring._get_margin_events", return_value=1), \
         patch("src.scoring._get_win_rate", return_value=0.80):
        mock_port.return_value = {
            "portfolio_value": 12000.0,
            "cash": 2000.0,
            "daily_pnl": 500.0,
            "timestamp": "2026-07-16 12:00:00",
        }
        result = compute_score("trader-kairos")
        # base: 0.20
        # drawdown_penalty: -0.10 * 0.5 = -0.05
        # violation_penalties: -(1 * 0.02 + 1 * 0.05) = -0.07
        # win_rate_bonus: (0.80 - 0.50) * 0.04 = 0.012
        # score = 0.20 + (-0.05) + (-0.07) + 0.012 = 0.092
        assert result["total_return"] == 0.20
        assert result["drawdown_penalty"] == -0.05
        assert result["violation_penalties"] == -0.07
        assert result["score_components"]["win_rate_bonus"] == 0.012
        assert result["score"] == 0.092


def test_compute_score_components_structure():
    """score_components dict has all expected keys."""
    with patch("src.scoring._get_latest_portfolio") as mock_port, \
         patch("src.scoring._get_max_drawdown", return_value=0.0), \
         patch("src.scoring._get_violation_count", return_value=0), \
         patch("src.scoring._get_margin_events", return_value=0), \
         patch("src.scoring._get_win_rate", return_value=0.5):
        mock_port.return_value = {
            "portfolio_value": 10000.0,
            "cash": 5000.0,
            "daily_pnl": 0.0,
            "timestamp": "2026-07-16 12:00:00",
        }
        result = compute_score("trader-kairos")
        expected_keys = {"base_score", "drawdown_penalty", "violation_penalties",
                         "win_rate_bonus", "formula"}
        assert expected_keys.issubset(set(result["score_components"].keys()))


def test_compute_score_result_keys():
    """Result dict has all expected top-level keys."""
    with patch("src.scoring._get_latest_portfolio") as mock_port, \
         patch("src.scoring._get_max_drawdown", return_value=0.0), \
         patch("src.scoring._get_violation_count", return_value=0), \
         patch("src.scoring._get_margin_events", return_value=0), \
         patch("src.scoring._get_win_rate", return_value=0.5):
        mock_port.return_value = {
            "portfolio_value": 10000.0,
            "cash": 5000.0,
            "daily_pnl": 0.0,
            "timestamp": "2026-07-16 12:00:00",
        }
        result = compute_score("trader-kairos")
        expected_keys = {"score", "ending_value", "total_return", "max_drawdown",
                         "drawdown_penalty", "violation_count", "margin_events",
                         "violation_penalties", "win_rate", "score_components"}
        assert expected_keys.issubset(set(result.keys()))


def test_compute_score_daily_pnl_none():
    """Handles None daily_pnl gracefully."""
    with patch("src.scoring._get_latest_portfolio") as mock_port, \
         patch("src.scoring._get_max_drawdown", return_value=0.0), \
         patch("src.scoring._get_violation_count", return_value=0), \
         patch("src.scoring._get_margin_events", return_value=0), \
         patch("src.scoring._get_win_rate", return_value=0.5):
        mock_port.return_value = {
            "portfolio_value": 10000.0,
            "cash": 5000.0,
            "daily_pnl": None,
            "timestamp": "2026-07-16 12:00:00",
        }
        result = compute_score("trader-kairos")
        assert result["score"] == 0.0


# ── compute_all_scores ─────────────────────────────────────────────────────

def test_compute_all_scores_returns_list():
    """compute_all_scores returns a list."""
    with patch("src.scoring._get_latest_portfolio", return_value={
        "portfolio_value": 10000.0, "cash": 5000.0,
        "daily_pnl": 0.0, "timestamp": "2026-07-16 12:00:00",
    }), \
         patch("src.scoring._get_max_drawdown", return_value=0.0), \
         patch("src.scoring._get_violation_count", return_value=0), \
         patch("src.scoring._get_margin_events", return_value=0), \
         patch("src.scoring._get_win_rate", return_value=0.5):
        results = compute_all_scores()
        assert isinstance(results, list)
        assert len(results) == len(LIVE_AGENTS)


def test_compute_all_scores_includes_agent_ids():
    """Each result includes agent_id."""
    with patch("src.scoring._get_latest_portfolio", return_value={
        "portfolio_value": 10000.0, "cash": 5000.0,
        "daily_pnl": 0.0, "timestamp": "2026-07-16 12:00:00",
    }), \
         patch("src.scoring._get_max_drawdown", return_value=0.0), \
         patch("src.scoring._get_violation_count", return_value=0), \
         patch("src.scoring._get_margin_events", return_value=0), \
         patch("src.scoring._get_win_rate", return_value=0.5):
        results = compute_all_scores()
        agent_ids = {r["agent_id"] for r in results}
        assert agent_ids == set(LIVE_AGENTS)


def test_compute_all_scores_all_have_scores():
    """Every result has a numeric score."""
    with patch("src.scoring._get_latest_portfolio", return_value={
        "portfolio_value": 10000.0, "cash": 5000.0,
        "daily_pnl": 0.0, "timestamp": "2026-07-16 12:00:00",
    }), \
         patch("src.scoring._get_max_drawdown", return_value=0.0), \
         patch("src.scoring._get_violation_count", return_value=0), \
         patch("src.scoring._get_margin_events", return_value=0), \
         patch("src.scoring._get_win_rate", return_value=0.5):
        results = compute_all_scores()
        for r in results:
            assert isinstance(r["score"], (int, float))


# ── helpers (with mocked DB) ───────────────────────────────────────────────

def test_get_latest_portfolio_returns_none_on_db_error():
    """_get_latest_portfolio returns None when DB is unreachable."""
    with patch("src.scoring._get_db", side_effect=Exception("connection refused")):
        result = _get_latest_portfolio("trader-kairos")
        assert result is None


def test_get_max_drawdown_returns_zero_on_db_error():
    """_get_max_drawdown returns 0.0 when DB is unreachable."""
    with patch("src.scoring._get_db", side_effect=Exception("connection refused")):
        result = _get_max_drawdown("trader-kairos")
        assert result == 0.0


def test_get_violation_count_returns_zero_on_db_error():
    """_get_violation_count returns 0 when DB is unreachable."""
    with patch("src.scoring._get_db", side_effect=Exception("connection refused")):
        result = _get_violation_count("trader-kairos")
        assert result == 0


def test_get_margin_events_returns_zero_on_db_error():
    """_get_margin_events returns 0 when DB is unreachable."""
    with patch("src.scoring._get_db", side_effect=Exception("connection refused")):
        result = _get_margin_events("trader-kairos")
        assert result == 0


def test_get_win_rate_returns_zero_on_db_error():
    """_get_win_rate returns 0.0 when DB is unreachable."""
    with patch("src.scoring._get_db", side_effect=Exception("connection refused")):
        result = _get_win_rate("trader-kairos")
        assert result == 0.0


# ── constants ──────────────────────────────────────────────────────────────

def test_starting_value_is_10000():
    """STARTING_VALUE constant is $10,000."""
    assert STARTING_VALUE == 10_000.0


def test_live_agents_contains_expected():
    """LIVE_AGENTS includes all three traders."""
    assert "trader-kairos" in LIVE_AGENTS
    assert "trader-aldridge" in LIVE_AGENTS
    assert "trader-stonks" in LIVE_AGENTS


# ── drawdown calculation logic ─────────────────────────────────────────────

def test_max_drawdown_from_peak():
    """Max drawdown correctly computes drawdown from rolling peak."""
    with patch("src.scoring._get_db") as mock_db:
        mock_conn = MagicMock()
        mock_db.return_value = mock_conn
        # conn.cursor() returns a context manager; __enter__ returns the mock cur
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.__enter__.return_value = mock_cur

        # Simulate: peak 11000 → drops to 9000 → recovers to 9500
        mock_cur.fetchall.return_value = [
            {"portfolio_value": 10000, "timestamp": "2026-07-01"},
            {"portfolio_value": 11000, "timestamp": "2026-07-05"},  # peak
            {"portfolio_value": 9000, "timestamp": "2026-07-10"},   # trough (-18.2%)
            {"portfolio_value": 9500, "timestamp": "2026-07-16"},   # recovery
        ]

        result = _get_max_drawdown("trader-kairos")
        # Max drawdown: (9000 - 11000) / 11000 = -0.1818...
        assert result == pytest.approx(-0.1818, abs=0.001)


def test_max_drawdown_increasing():
    """If portfolio only increases, max drawdown is 0."""
    with patch("src.scoring._get_db") as mock_db:
        mock_conn = MagicMock()
        mock_db.return_value = mock_conn
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.__enter__.return_value = mock_cur

        mock_cur.fetchall.return_value = [
            {"portfolio_value": 10000, "timestamp": "2026-07-01"},
            {"portfolio_value": 10500, "timestamp": "2026-07-08"},
            {"portfolio_value": 11000, "timestamp": "2026-07-16"},
        ]

        result = _get_max_drawdown("trader-kairos")
        assert result == 0.0


def test_max_drawdown_single_point():
    """Single portfolio snapshot gives 0 drawdown."""
    with patch("src.scoring._get_db") as mock_db:
        mock_conn = MagicMock()
        mock_db.return_value = mock_conn
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.__enter__.return_value = mock_cur

        mock_cur.fetchall.return_value = [
            {"portfolio_value": 10000, "timestamp": "2026-07-16"},
        ]

        result = _get_max_drawdown("trader-kairos")
        assert result == 0.0
