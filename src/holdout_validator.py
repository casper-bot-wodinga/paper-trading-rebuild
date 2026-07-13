#!/usr/bin/env python3
"""
Holdout Validator — temporal train/val/holdout splitting.

SPEC-v3 §6: No random shuffling. Time series data must respect temporal order.
Training always precedes validation. Holdout is the final, untouched fold.

Usage:
    from src.holdout_validator import HoldoutSplitter, HoldoutConfig

    splitter = HoldoutSplitter()
    train, val, holdout = splitter.split(dates)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class HoldoutConfig:
    """Configuration for temporal holdout splitting.

    Attributes:
        train_pct: Fraction of data used for training (default: 0.6).
        val_pct: Fraction used for validation (default: 0.2).
        holdout_pct: Final fraction kept completely unseen (default: 0.2).
        min_train_days: Minimum training days required.
        min_val_days: Minimum validation days required.
        gap_days: Days to skip between train→val and val→holdout (avoids leakage).
    """

    train_pct: float = 0.6
    val_pct: float = 0.2
    holdout_pct: float = 0.2
    min_train_days: int = 10
    min_val_days: int = 3
    gap_days: int = 0


class HoldoutSplitter:
    """Time-series-aware train/val/holdout splitter.

    Always preserves temporal order. No shuffling.
    """

    def __init__(self, config: Optional[HoldoutConfig] = None):
        self.config = config or HoldoutConfig()

    def split(
        self,
        dates: List[str],
    ) -> Tuple[List[str], List[str], List[str]]:
        """Split sorted dates into train, val, holdout.

        Args:
            dates: Chronologically sorted date strings.

        Returns:
            (train_dates, val_dates, holdout_dates) — each chronologically sorted.
        """
        if not dates:
            return [], [], []

        n = len(dates)
        train_end = max(self.min_train_days, int(n * self.config.train_pct))
        val_len = max(self.min_val_days, int(n * self.config.val_pct))
        gap = self.config.gap_days

        # Ensure we don't exceed total length
        val_end = min(train_end + gap + val_len, n)
        holdout_start = val_end
        holdout_end = n

        train = dates[:train_end]
        val = dates[train_end + gap:val_end] if train_end + gap < val_end else []
        holdout = dates[holdout_start:holdout_end]

        return train, val, holdout

    def walk_forward_windows(
        self,
        dates: List[str],
        train_window: int = 90,
        val_window: int = 30,
        step: int = 1,
        holdout_fold: bool = True,
    ) -> List[Tuple[List[str], List[str], Optional[List[str]]]]:
        """Generate walk-forward windows, optionally reserving a holdout fold.

        Each window: (train_dates, val_dates, holdout_dates | None)

        Args:
            dates: Chronologically sorted dates.
            train_window: Days per training window.
            val_window: Days per validation window.
            step: Days to advance each window.
            holdout_fold: If True, reserve the last fold as holdout.

        Returns:
            List of (train_dates, val_dates, holdout_dates_or_None) tuples.
        """
        from src.validation import walk_forward_split, TimeWindow  # type: ignore[import]

        windows: List[Tuple[List[str], List[str], Optional[List[str]]]] = []

        # Reserve the last val_window days as a final holdout if requested
        holdout_start = len(dates) - val_window if holdout_fold and len(dates) > val_window * 2 else len(dates)
        holdout_dates = dates[holdout_start:] if holdout_start < len(dates) else []
        effective_dates = dates[:holdout_start] if holdout_dates else dates

        for tw in walk_forward_split(len(effective_dates), train_window, val_window, step):
            train = effective_dates[tw.train_start:tw.train_end]
            val = effective_dates[tw.val_start:tw.val_end]
            windows.append((train, val, holdout_dates if holdout_fold and tw.val_end >= holdout_start - 1 else None))

        return windows
