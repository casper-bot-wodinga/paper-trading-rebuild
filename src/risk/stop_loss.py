#!/usr/bin/env python3
"""
Stop-Loss Module — enforces position-level loss limits.

Three components:
  - HardStopLoss:   percentage-based hard stop from entry price
  - TrailingStopLoss: follows price up, never moves down
  - StopLossManager: orchestrates checks for all open positions

Config keys (from config/risk.yaml):
  risk.stop_loss.default_pct     (default: 0.05  = 5%)
  risk.stop_loss.trailing_pct    (default: 0.03  = 3%)
  risk.stop_loss.profit_target_pct (default: 0.15 = 15%, future use)

Integration point: trader.py process_tick — check BEFORE any new entries.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("trader.stop_loss")


# ═══════════════════════════════════════════════════════════════════════════════
# Hard Stop Loss — absolute percentage loss from entry
# ═══════════════════════════════════════════════════════════════════════════════


class HardStopLoss:
    """Percentage-based hard stop.

    Triggers when a position falls below entry_price * (1 - stop_pct).
    This is an absolute floor — once set, it never moves.
    """

    def __init__(self, stop_pct: float = 0.05):
        if stop_pct <= 0 or stop_pct >= 1.0:
            raise ValueError(f"stop_pct must be in (0, 1), got {stop_pct}")
        self.stop_pct = stop_pct

        # Per-ticker stop levels: ticker -> stop_price
        self._stops: Dict[str, float] = {}

    def set_stop(self, ticker: str, entry_price: float) -> None:
        """Record the hard-stop price for a ticker at entry."""
        ticker = ticker.upper()
        self._stops[ticker] = entry_price * (1.0 - self.stop_pct)

    def check(
        self, ticker: str, entry_price: float, current_price: float
    ) -> Tuple[bool, str]:
        """Check if the hard stop has been breached.

        Returns:
            (triggered: bool, reason: str)
        """
        ticker = ticker.upper()
        stop_price = self._stops.get(ticker, entry_price * (1.0 - self.stop_pct))

        if current_price <= stop_price:
            loss_pct = (current_price - entry_price) / entry_price
            return True, (
                f"{ticker}: {loss_pct:.1%} loss ≥ {self.stop_pct:.0%} hard stop "
                f"(${entry_price:.2f} → ${current_price:.2f}, stop at ${stop_price:.2f})"
            )

        return False, (
            f"{ticker}: {((current_price - entry_price) / entry_price):.1%} "
            f"(stop at ${stop_price:.2f})"
        )

    def reset(self, ticker: str) -> None:
        """Remove stop tracking for a closed position."""
        ticker = ticker.upper()
        self._stops.pop(ticker, None)


# ═══════════════════════════════════════════════════════════════════════════════
# Trailing Stop Loss — follows price up, never moves down
# ═══════════════════════════════════════════════════════════════════════════════


class TrailingStopLoss:
    """Trailing stop that ratchets up with price.

    Once set at trailing_pct below the highest price observed, the stop
    level never moves down. This locks in profits on winning positions
    while still allowing room for normal volatility.

    If a position never goes above entry, the trailing stop stays at
    entry_price * (1 - trailing_pct) — same as the hard stop.
    """

    def __init__(self, trailing_pct: float = 0.03):
        if trailing_pct <= 0 or trailing_pct >= 1.0:
            raise ValueError(f"trailing_pct must be in (0, 1), got {trailing_pct}")
        self.trailing_pct = trailing_pct

        # Per-ticker peak tracking: ticker -> (peak_price, stop_price)
        self._peaks: Dict[str, Tuple[float, float]] = {}

    def update(self, ticker: str, current_price: float) -> None:
        """Update the peak price for a ticker.

        Called on every tick. If current_price exceeds the recorded peak,
        the trailing stop level ratchets up.
        """
        ticker = ticker.upper()
        entry = self._peaks.get(ticker)

        if entry is None:
            # First observation — set peak at current price
            self._peaks[ticker] = (current_price, current_price * (1.0 - self.trailing_pct))
        else:
            peak, _stop = entry
            if current_price > peak:
                new_stop = current_price * (1.0 - self.trailing_pct)
                self._peaks[ticker] = (current_price, new_stop)

    def check(
        self, ticker: str, current_price: float
    ) -> Tuple[bool, str]:
        """Check if the trailing stop has been breached.

        Returns:
            (triggered: bool, reason: str)
        """
        ticker = ticker.upper()
        entry = self._peaks.get(ticker)

        if entry is None:
            # No peak recorded yet — can't evaluate
            return False, f"{ticker}: no trailing stop data yet"

        peak, stop_price = entry

        if current_price <= stop_price:
            drop_from_peak = (current_price - peak) / peak
            return True, (
                f"{ticker}: trailing stop breached — {drop_from_peak:.1%} "
                f"from peak ${peak:.2f} (stop=${stop_price:.2f}, current=${current_price:.2f})"
            )

        return False, (
            f"{ticker}: {((current_price - peak) / peak):.1%} from peak "
            f"${peak:.2f} (trailing stop at ${stop_price:.2f})"
        )

    def reset(self, ticker: str) -> None:
        """Remove trailing stop tracking for a closed position."""
        ticker = ticker.upper()
        self._peaks.pop(ticker, None)


# ═══════════════════════════════════════════════════════════════════════════════
# Stop Loss Manager — orchestrates both stop types for all open positions
# ═══════════════════════════════════════════════════════════════════════════════


class StopLossManager:
    """Orchestrates stop-loss enforcement across all open positions.

    Combines HardStopLoss (absolute floor) and TrailingStopLoss (profit lock-in).
    Called once per tick BEFORE any new trade decisions in the trader loop.

    Usage:
        manager = StopLossManager()
        breached = manager.check_all(
            positions={"AAPL": {"entry_price": 150.0, "shares": 10}},
            current_prices={"AAPL": 142.0},
        )
        for breach in breached:
            # Execute forced SELL, journal exit, then:
            manager.record_exit(breach["ticker"])
    """

    def __init__(self, config_overrides: Optional[Dict[str, Any]] = None):
        """Initialize with optional config overrides.

        Loads stop-loss parameters from config/risk.yaml via config_loader.
        Falls back to sensible defaults if config is unavailable.

        Args:
            config_overrides: Optional dict to override loaded config values.
                Keys: 'default_pct', 'trailing_pct'
        """
        # Load from config (with fallback)
        default_pct = 0.05
        trailing_pct = 0.03

        try:
            from src.config_loader import get_config
            config = get_config()
            default_pct = float(config.get("risk.stop_loss.default_pct", 0.05))
            trailing_pct = float(config.get("risk.stop_loss.trailing_pct", 0.03))
        except Exception:
            log.warning("StopLossManager: could not load config, using defaults")

        # Apply overrides
        if config_overrides:
            default_pct = float(config_overrides.get("default_pct", default_pct))
            trailing_pct = float(config_overrides.get("trailing_pct", trailing_pct))

        self._hard_stop = HardStopLoss(stop_pct=default_pct)
        self._trailing_stop = TrailingStopLoss(trailing_pct=trailing_pct)

        log.info(
            "StopLossManager initialized: hard_stop=%.0f%%, trailing=%.0f%%",
            default_pct * 100, trailing_pct * 100,
        )

    @property
    def hard_stop(self) -> HardStopLoss:
        return self._hard_stop

    @property
    def trailing_stop(self) -> TrailingStopLoss:
        return self._trailing_stop

    def check_all(
        self,
        positions: Dict[str, dict],
        current_prices: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        """Check all open positions for stop-loss breaches.

        For each position, checks both:
          1. Hard stop (absolute floor from entry)
          2. Trailing stop (profit lock-in)

        Args:
            positions: ticker -> {"entry_price": float, "shares": int, ...}
            current_prices: ticker -> current_price (float)

        Returns:
            List of breached positions, each with:
                ticker: str
                reason: str (human-readable)
                loss_pct: float (loss from entry, negative number)
                stop_type: "hard" | "trailing"
        """
        breached: List[Dict[str, Any]] = []

        for ticker, pos in positions.items():
            ticker = ticker.upper()

            # Skip positions without pricing data
            current_price = current_prices.get(ticker)
            if current_price is None or current_price <= 0:
                continue

            entry_price = float(pos.get("entry_price", 0) or 0)
            if entry_price <= 0:
                continue

            loss_pct = (current_price - entry_price) / entry_price

            # 1. Hard stop check — absolute floor
            hard_triggered, hard_reason = self._hard_stop.check(
                ticker, entry_price, current_price
            )

            if hard_triggered:
                breached.append({
                    "ticker": ticker,
                    "reason": hard_reason,
                    "loss_pct": loss_pct,
                    "stop_type": "hard",
                })
                continue  # Already breached, don't double-report

            # 2. Trailing stop check — profit lock-in
            self._trailing_stop.update(ticker, current_price)
            trail_triggered, trail_reason = self._trailing_stop.check(
                ticker, current_price
            )

            if trail_triggered:
                breached.append({
                    "ticker": ticker,
                    "reason": trail_reason,
                    "loss_pct": loss_pct,
                    "stop_type": "trailing",
                })

        return breached

    def record_exit(self, ticker: str) -> None:
        """Clean up tracking after a position is fully closed.

        Must be called after _simulate_fill closes a position (either
        through normal SELL or forced stop-loss exit).
        """
        ticker = ticker.upper()
        self._hard_stop.reset(ticker)
        self._trailing_stop.reset(ticker)

    def set_entry(self, ticker: str, entry_price: float) -> None:
        """Record a new position entry so stops can track it.

        Called from _simulate_fill when a new BUY is executed.
        Sets the hard stop level and initializes trailing stop tracking.
        """
        ticker = ticker.upper()
        self._hard_stop.set_stop(ticker, entry_price)
        self._trailing_stop.update(ticker, entry_price)
