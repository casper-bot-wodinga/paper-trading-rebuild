"""Test suite for src/metrics.py — objective function metrics.

SPEC-v3 §2: Calmar, Sortino, profit factor, expectancy, VaR, composite score.
"""
import pytest
import numpy as np
from src.metrics import (
    compute_max_drawdown,
    compute_calmar,
    compute_sortino,
    compute_sharpe,
    compute_profit_factor,
    compute_win_rate,
    compute_expectancy,
    compute_var_95,
    objective_score,
)


class TestMaxDrawdown:
    def test_no_drawdown(self):
        equity = np.array([100, 105, 110, 115, 120])
        assert compute_max_drawdown(equity) == pytest.approx(0.0)

    def test_simple_drawdown(self):
        equity = np.array([100, 90, 85, 95, 80])
        dd = compute_max_drawdown(equity)
        assert dd == pytest.approx(0.20)  # 20% from 100 to 80

    def test_peak_then_drop(self):
        equity = np.array([100, 120, 110, 90, 105])
        dd = compute_max_drawdown(equity)
        assert dd == pytest.approx(0.25)  # 25% from 120 to 90


class TestCalmar:
    def test_positive_calmar(self):
        returns = np.array([0.01, 0.02, -0.005, 0.015, 0.01])
        equity = np.cumprod(1 + returns)
        calmar = compute_calmar(returns, equity)
        assert calmar > 0

    def test_negative_calmar(self):
        returns = np.array([-0.01, -0.02, -0.03, -0.01])
        equity = np.cumprod(1 + returns)
        calmar = compute_calmar(returns, equity)
        assert calmar < 0

    def test_zero_drawdown(self):
        returns = np.array([0.01, 0.01, 0.01])
        equity = np.array([100, 101, 102.01, 103.0301])
        calmar = compute_calmar(returns, equity)
        assert calmar > 0  # large number since DD ≈ 0


class TestSortino:
    def test_all_positive(self):
        returns = np.array([0.01, 0.02, 0.03])
        sortino = compute_sortino(returns, risk_free_rate=0.0)
        # All returns positive → downside deviation = 0 → Sortino = inf
        assert np.isinf(sortino) or sortino > 100

    def test_mixed_returns(self):
        returns = np.array([0.02, -0.01, 0.03, -0.02, 0.01])
        sortino = compute_sortino(returns, risk_free_rate=0.0)
        # Only [-0.01, -0.02] contribute to downside
        assert sortino > 0

    def test_all_negative(self):
        returns = np.array([-0.01, -0.02, -0.03])
        sortino = compute_sortino(returns, risk_free_rate=0.0)
        assert sortino < 0


class TestSharpe:
    def test_positive_sharpe(self):
        returns = np.array([0.01, 0.02, 0.015, 0.01])
        sharpe = compute_sharpe(returns, risk_free_rate=0.0)
        assert sharpe > 0

    def test_sharpe_with_risk_free(self):
        returns = np.array([0.012, 0.008, 0.015, 0.011, 0.009])
        sharpe = compute_sharpe(returns, risk_free_rate=0.04 / 252)  # daily
        assert sharpe > 0


class TestProfitFactor:
    def test_profitable(self):
        trades = [100, -50, 200, -30, 150]  # wins: 450, losses: 80
        assert compute_profit_factor(trades) == pytest.approx(450 / 80)

    def test_breakeven(self):
        trades = [100, -100]
        assert compute_profit_factor(trades) == pytest.approx(1.0)

    def test_losing(self):
        trades = [50, -200]
        assert compute_profit_factor(trades) == pytest.approx(0.25)


class TestWinRate:
    def test_mixed(self):
        trades = [100, -50, 200, -30]
        assert compute_win_rate(trades) == pytest.approx(0.5)

    def test_all_wins(self):
        trades = [10, 20, 30]
        assert compute_win_rate(trades) == pytest.approx(1.0)


class TestExpectancy:
    def test_positive(self):
        trades = [100, -50, 200, -30]
        assert compute_expectancy(trades) == pytest.approx(220 / 4)

    def test_negative(self):
        trades = [-100, -50]
        assert compute_expectancy(trades) == pytest.approx(-75.0)


class TestVaR:
    def test_var_95(self):
        returns = np.array([0.001, -0.002, 0.003, -0.001, 0.002] * 50)
        var = compute_var_95(returns)
        # Should be a negative number (worst case loss)
        assert var < 0

    def test_var_symmetric(self):
        np.random.seed(42)
        returns = np.random.normal(0.001, 0.02, 252)
        var = compute_var_95(returns)
        # With μ=0.001, σ=0.02: VaR ≈ 0.001 - 1.645*0.02 ≈ -0.032
        assert var < -0.02
        assert var > -0.05


class TestObjectiveScore:
    def test_good_trader(self):
        returns = np.array([0.02, 0.01, -0.005, 0.03, 0.01] * 20)
        equity = np.cumprod(1 + returns)
        trades = [100, -30, 200, -20, 150]
        score = objective_score(returns, equity, trades)
        assert score > 0

    def test_knockout_drawdown(self):
        # Create >15% drawdown
        equity = np.array([100, 85, 84, 83, 84])  # 16% DD
        returns = np.diff(equity) / equity[:-1]
        trades = [10, 5]
        score = objective_score(returns, equity, trades)
        assert score == 0.0  # Knockout

    def test_losing_trader(self):
        returns = np.array([-0.02, -0.01, -0.03, -0.01] * 10)
        equity = np.cumprod(1 + returns)
        trades = [-100, -50, -30]
        score = objective_score(returns, equity, trades)
        assert score <= 0
