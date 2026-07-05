"""Walk-forward validation — SPEC-v3 §6.

No random shuffling. Time series data must respect temporal order.
Training always precedes validation. Out-of-sample is sacred.
"""
from dataclasses import dataclass
from datetime import date
from typing import Iterator


@dataclass
class TimeWindow:
    """A single walk-forward window."""
    train_start: int
    train_end: int
    val_start: int
    val_end: int


def walk_forward_split(
    n_days: int,
    train_window: int = 90,
    val_window: int = 30,
    step: int = 1,
) -> Iterator[TimeWindow]:
    """Generate walk-forward train/validation windows.

    Window layout:
        [T-120 ... T-30] → train
        [T-30  ... T   ] → val

    Next step:
        [T-119 ... T-29] → train
        [T-29  ... T+1 ] → val

    Args:
        n_days: Total number of days of data.
        train_window: Days in training window (default 90).
        val_window: Days in validation window (default 30).
        step: Days to advance per window (default 1).

    Yields:
        TimeWindow objects with integer indices.
    """
    total_window = train_window + val_window
    if n_days < total_window:
        return  # Not enough data

    for start in range(0, n_days - total_window + 1, step):
        yield TimeWindow(
            train_start=start,
            train_end=start + train_window,
            val_start=start + train_window,
            val_end=start + total_window,
        )


def is_overfit(
    train_score: float,
    val_score: float,
    threshold: float = 0.30,
) -> bool:
    """Detect overfitting: validation score significantly worse than training.

    If val_score < train_score × (1 - threshold), the model is overfit.

    Example: train Sharpe 1.5, val Sharpe 0.9, threshold 0.30:
        0.9 < 1.5 × 0.7 = 1.05 → OVERFIT (rejected)

    Args:
        train_score: Metric on training window.
        val_score: Metric on validation (out-of-sample) window.
        threshold: Maximum acceptable degradation (default 0.30 = 30%).

    Returns:
        True if overfit (reject the change).
    """
    return val_score < train_score * (1.0 - threshold)


def is_significant(
    baseline_scores: list[float],
    candidate_scores: list[float],
    p_threshold: float = 0.05,
) -> tuple[bool, float]:
    """Test if candidate improvement is statistically significant.

    Uses paired t-test. Requires at least 5 data points.

    Args:
        baseline_scores: Metric values under current config (per window).
        candidate_scores: Metric values under proposed config (per window).
        p_threshold: Significance threshold (default 0.05).

    Returns:
        (is_significant: bool, p_value: float)
    """
    import numpy as np
    from scipy import stats as scipy_stats

    if len(baseline_scores) < 5:
        return False, 1.0

    baseline_arr = np.array(baseline_scores)
    candidate_arr = np.array(candidate_scores)

    try:
        t_stat, p_value = scipy_stats.ttest_rel(candidate_arr, baseline_arr)
    except (ImportError, Exception):
        # Fallback: simple z-test of differences
        diffs = candidate_arr - baseline_arr
        mean_diff = np.mean(diffs)
        std_diff = np.std(diffs, ddof=1)
        if std_diff < 1e-10:
            return False, 1.0
        z = mean_diff / (std_diff / np.sqrt(len(diffs)))
        # Two-tailed test: p ≈ 2 * (1 - Φ(|z|))
        p_value = 2.0 * (1.0 - 0.5 * (1.0 + np.tanh(np.sqrt(2.0 / np.pi) * (abs(z) + 0.044715 * abs(z) ** 3))))  # approx
        is_sig = abs(z) > 1.96  # 95% confidence

    return bool(p_value < p_threshold), float(p_value)


def evaluate_config(
    replay_fn,
    config: dict,
    data_window: TimeWindow,
) -> float:
    """Evaluate a config on a specific data window.

    Args:
        replay_fn: Function that takes (config, data_slice) → metrics dict.
        config: Parameter configuration to test.
        data_window: Which data to use.

    Returns:
        Calmar ratio (or whichever metric replay_fn returns).
    """
    result = replay_fn(config, data_window)
    # replay_fn returns dict with 'calmar' or 'objective_score'
    return result.get("objective_score", result.get("calmar", result.get("sharpe", 0.0)))
