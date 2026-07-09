#!/usr/bin/env python3
"""
sync_trades.py — Stop-loss computation utilities used by the replay harness
and trade sync pipeline.

The full position-sync module lives at:
    /home/openclaw/projects/paper-trading-teams/src/sync_trades.py

This module provides the stop-loss constants and computation that
src/replay.py and tests/test_replay.py depend on.
"""

DEFAULT_STOP_LOSS_PCT = 0.05


def _compute_stop_loss(entry_price: float, stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT) -> float:
    """Compute the stop-loss price for a position entry.

    Args:
        entry_price: The average entry price of the position.
        stop_loss_pct: Stop-loss percentage as a decimal (default 0.05 = 5%).

    Returns:
        Stop-loss price rounded to 2 decimal places, clamped to >= 0.
        stop_loss = entry_price * (1 - stop_loss_pct)
    """
    if entry_price <= 0:
        return 0.0

    stop = round(entry_price * (1.0 - stop_loss_pct), 2)
    return max(0.0, stop)
