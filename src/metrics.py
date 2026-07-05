"""Objective function metrics — SPEC-v3 §2, §23.

Core metrics: Calmar, Sortino, Sharpe, profit factor, win rate, expectancy, VaR.
Composite objective_score drives the learning loop.

All functions accept numpy arrays. All returns are floats.
"""
import numpy as np
from numpy.typing import NDArray


def compute_max_drawdown(equity: NDArray[np.float64]) -> float:
    """Maximum peak-to-trough decline as a positive fraction.

    Args:
        equity: Array of equity values over time.

    Returns:
        Max drawdown as a positive fraction (0.20 = 20%).
        0.0 if equity never declined.
    """
    if len(equity) < 2:
        return 0.0
    peak = np.maximum.accumulate(equity)
    drawdowns = (peak - equity) / peak
    return float(np.max(drawdowns))


def compute_calmar(
    returns: NDArray[np.float64],
    equity: NDArray[np.float64],
    periods_per_year: int = 252,
) -> float:
    """Calmar ratio: annualized return divided by max drawdown.

    Calmar = (annualized_return) / abs(max_drawdown)

    A Calmar of 2.0 means you make 2x your worst drawdown per year.
    Higher is better. Negative means losing money.

    Args:
        returns: Array of period returns (e.g., daily).
        equity: Array of equity values over time.
        periods_per_year: 252 for daily, 12 for monthly.

    Returns:
        Calmar ratio. Returns large number if drawdown ≈ 0.
    """
    ann_return = float(np.mean(returns)) * periods_per_year
    max_dd = compute_max_drawdown(equity)
    if max_dd < 1e-10:
        return 100.0 if ann_return > 0 else float("inf") if ann_return > 0 else ann_return * 100
    return ann_return / max_dd


def compute_sortino(
    returns: NDArray[np.float64],
    risk_free_rate: float = 0.04,
    periods_per_year: int = 252,
) -> float:
    """Sortino ratio: excess return divided by downside deviation.

    Only penalizes downside volatility. Upside volatility is profit,
    not risk.

    Sortino = (R_p - R_f) / σ_d

    Args:
        returns: Array of period returns.
        risk_free_rate: Annual risk-free rate (default 4%).
        periods_per_year: 252 for daily.

    Returns:
        Sortino ratio. Inf if no downside deviation. Higher is better.
    """
    excess = returns - (risk_free_rate / periods_per_year)
    downside = excess[excess < 0]
    if len(downside) < 2:
        return float("inf") if np.mean(returns) > (risk_free_rate / periods_per_year) else float("-inf")
    downside_std = np.std(downside, ddof=1)
    if downside_std < 1e-10:
        return float("inf") if np.mean(excess) > 0 else float("-inf")
    return float((np.mean(excess) * np.sqrt(periods_per_year)) / downside_std)


def compute_sharpe(
    returns: NDArray[np.float64],
    risk_free_rate: float = 0.04,
    periods_per_year: int = 252,
) -> float:
    """Sharpe ratio: excess return divided by total volatility.

    Sharpe = (R_p - R_f) / σ

    Args:
        returns: Array of period returns.
        risk_free_rate: Annual risk-free rate.
        periods_per_year: 252 for daily.

    Returns:
        Sharpe ratio.
    """
    excess = returns - (risk_free_rate / periods_per_year)
    if len(excess) < 2:
        return 0.0
    std = np.std(excess, ddof=1)
    if std < 1e-10:
        return 0.0
    return float((np.mean(excess) * np.sqrt(periods_per_year)) / std)


def compute_profit_factor(trades: list[float]) -> float:
    """Profit factor: gross profit divided by gross loss.

    PF = sum(wins) / abs(sum(losses))

    > 1.0 = profitable. > 2.0 = strong edge.

    Args:
        trades: List of trade P&L values (positive = win, negative = loss).

    Returns:
        Profit factor. 1.0 if no losses. 0.0 if no trades.
    """
    if not trades:
        return 0.0
    wins = sum(t for t in trades if t > 0)
    losses = abs(sum(t for t in trades if t < 0))
    if losses < 1e-10:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


def compute_win_rate(trades: list[float]) -> float:
    """Win rate: fraction of trades that are profitable.

    Args:
        trades: List of trade P&L values.

    Returns:
        Win rate as a fraction (0.0 to 1.0).
    """
    if not trades:
        return 0.0
    return sum(1 for t in trades if t > 0) / len(trades)


def compute_expectancy(trades: list[float]) -> float:
    """Expectancy: average P&L per trade in dollars.

    Positive = you have an edge. Negative = losing money per trade.

    Args:
        trades: List of trade P&L values.

    Returns:
        Average P&L per trade.
    """
    if not trades:
        return 0.0
    return sum(trades) / len(trades)


def compute_var_95(
    returns: NDArray[np.float64],
    confidence: float = 0.95,
) -> float:
    """Value at Risk: worst-case loss at given confidence.

    VaR = μ - z × σ

    With 95% confidence, you won't lose more than VaR.

    Args:
        returns: Array of period returns.
        confidence: 0.95 for 95% VaR, 0.99 for 99%.

    Returns:
        VaR as a fraction (negative = loss).
    """
    from scipy.stats import norm  # optional; fall back to lookup

    mu = float(np.mean(returns))
    sigma = float(np.std(returns, ddof=1))
    try:
        z = norm.ppf(confidence)
    except ImportError:
        # Hardcoded z-scores for common confidence levels
        z_map = {0.90: 1.282, 0.95: 1.645, 0.99: 2.326}
        z = z_map.get(confidence, 1.645)
    return mu - z * sigma


def objective_score(
    returns: NDArray[np.float64],
    equity: NDArray[np.float64],
    trades: list[float],
    risk_free_rate: float = 0.04,
) -> float:
    """Composite objective score — what the learning loop maximizes.

    Weights (SPEC-v3 §2.2):
        Calmar:     0.40
        Sortino:    0.15
        Profit factor: 0.30
        Expectancy: 0.15

    Knockout: if max_drawdown > 15%, return 0.0.

    Args:
        returns: Array of period returns.
        equity: Array of equity values.
        trades: List of trade P&L values.
        risk_free_rate: Annual risk-free rate.

    Returns:
        Composite score. Higher = better. 0.0 = knockout (drawdown too high).
    """
    max_dd = compute_max_drawdown(equity)
    if max_dd > 0.15:
        return 0.0

    calmar = compute_calmar(returns, equity)
    sortino = compute_sortino(returns, risk_free_rate)
    pf = compute_profit_factor(trades)
    exp_val = compute_expectancy(trades)

    # Z-score expectancy relative to its own magnitude (normalize)
    exp_abs = abs(exp_val)
    exp_z = exp_val / max(exp_abs, 1.0)

    # Clip extreme values to prevent one metric from dominating
    calmar_clipped = max(min(calmar, 10.0), -10.0)
    sortino_clipped = max(min(sortino, 10.0), -10.0)

    score = (
        0.40 * calmar_clipped
        + 0.15 * sortino_clipped
        + 0.30 * pf
        + 0.15 * exp_z
    )
    return float(score)
