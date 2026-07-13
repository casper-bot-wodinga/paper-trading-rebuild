"""
Holdout set management — SPEC-v3 §7, §23.

Manages a 15% test slice of trading dates reserved for quarterly evaluation
against truly unseen data. These dates are excluded from training and
validation windows used in the nightly pipeline and prompt sweeps.

The holdout set is stored persistently in ``data/holdout.json`` and is
created once for a given lookback window. New holdout dates can be appended
by running ``holdout_eval.py --update``.

Usage:
    from src.holdout import HoldoutManager

    mgr = HoldoutManager()
    holdout_dates = mgr.get_holdout_dates()
    training_dates = mgr.filter_holdout(all_dates)

    # Quarterly eval
    results = mgr.evaluate(trader, variant, ticks)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.metrics import objective_score, compute_calmar, compute_profit_factor
from src.transaction_costs import CostModel

log = logging.getLogger("holdout")

# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent.parent
HOLDOUT_FILE = PROJECT_DIR / "data" / "holdout.json"

# ── Default fraction ─────────────────────────────────────────────────────────
DEFAULT_HOLDOUT_FRACTION = 0.15  # 15%


@dataclass
class HoldoutResult:
    """Result from a holdout evaluation run."""

    trader: str
    variant_name: str
    eval_date: str
    holdout_dates: List[str]
    n_holdout_days: int
    objective_score: float
    calmar: float
    profit_factor: float
    win_rate: float
    n_trades: int
    total_pnl: float
    cost_adjusted_pnl: float
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class QuarterlyEvalSummary:
    """Aggregated summary from a quarterly holdout evaluation."""

    trader: str
    holdout_dates: List[str]
    n_dates: int
    n_dates_with_data: int
    mean_objective_score: float
    mean_calmar: float
    mean_profit_factor: float
    mean_win_rate: float
    total_pnl: float
    total_cost_adjusted_pnl: float
    n_trades: int
    timestamp: str = ""
    per_date_results: List[HoldoutResult] = field(default_factory=list)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════════════════════════
# Holdout Manager
# ═══════════════════════════════════════════════════════════════════════════════


class HoldoutManager:
    """Manage the 15% holdout set of trading dates.

    The holdout set is a fixed slice of **the most recent** 15% of dates
    in a given date list. These dates are never used for training or
    validation in the nightly pipeline — they are reserved strictly for
    quarterly out-of-sample evaluation.

    Storage is in ``data/holdout.json``:
    {
        "created_at": "2026-07-11T...",
        "holdout_dates": ["2026-03-15", "2026-03-16", ...],
        "fraction": 0.15,
        "source_range": ["2025-01-01", "2026-07-10"],
        "last_eval": null
    }
    """

    def __init__(self, holdout_file: Path = HOLDOUT_FILE):
        self._holdout_file = holdout_file
        self._cache: Optional[List[str]] = None

    # ── Public API ────────────────────────────────────────────────────────

    def get_holdout_dates(self) -> List[str]:
        """Return the current holdout date list.

        Returns empty list if no holdout set has been created yet.
        """
        if self._cache is not None:
            return self._cache
        data = self._load()
        if data is None:
            return []
        self._cache = data.get("holdout_dates", [])
        return self._cache

    def create_holdout(
        self,
        all_dates: List[str],
        fraction: float = DEFAULT_HOLDOUT_FRACTION,
        force: bool = False,
    ) -> List[str]:
        """Create a holdout set from the most recent 15% of dates.

        Args:
            all_dates: Sorted list of all available trading dates (ascending).
            fraction: Fraction of dates to reserve as holdout (default 0.15).
            force: If True, overwrite existing holdout set.

        Returns:
            The holdout date list.

        Raises:
            ValueError: If holdout already exists and force=False.
            ValueError: If fraction is not in (0.0, 0.5).
        """
        if len(all_dates) < 20:
            raise ValueError(
                f"Need at least 20 trading dates to create a holdout set, "
                f"got {len(all_dates)}."
            )

        if not 0.0 < fraction < 0.5:
            raise ValueError(
                f"Holdout fraction must be in (0.0, 0.5), got {fraction}."
            )

        existing = self._load()
        if existing is not None and not force:
            existing_dates = existing.get("holdout_dates", [])
            if existing_dates:
                raise ValueError(
                    f"Holdout set already exists ({len(existing_dates)} dates). "
                    f"Use force=True to overwrite."
                )

        # Sort dates chronologically
        sorted_dates = sorted(all_dates)

        # Take the most recent 15% as holdout (out-of-sample)
        n_holdout = max(1, int(len(sorted_dates) * fraction))
        holdout_dates = sorted_dates[-n_holdout:]

        data = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "holdout_dates": holdout_dates,
            "fraction": fraction,
            "source_range": [sorted_dates[0], sorted_dates[-1]],
            "last_eval": None,
        }

        self._save(data)
        self._cache = holdout_dates

        log.info(
            "Created holdout set: %d dates (%.1f%% of %d total) — %s → %s",
            len(holdout_dates),
            fraction * 100,
            len(all_dates),
            holdout_dates[0],
            holdout_dates[-1],
        )

        return holdout_dates

    def filter_holdout(self, dates: List[str]) -> List[str]:
        """Remove holdout dates from a date list.

        This is used to ensure holdout dates never enter training or
        validation windows.

        Args:
            dates: List of dates to filter.

        Returns:
            Dates with holdout days removed, preserving order.
        """
        holdout = set(self.get_holdout_dates())
        return [d for d in dates if d not in holdout]

    def is_holdout(self, date_str: str) -> bool:
        """Check if a single date is in the holdout set."""
        return date_str in self.get_holdout_dates()

    def update_source_range(self, all_dates: List[str]) -> None:
        """Update the source_range metadata without changing holdout dates.

        Call this when the overall date pool expands (e.g., a new quarter).
        """
        data = self._load()
        if data is None:
            return
        sorted_dates = sorted(all_dates)
        data["source_range"] = [sorted_dates[0], sorted_dates[-1]]
        self._save(data)

    def record_eval(self, trader: str, results: QuarterlyEvalSummary) -> None:
        """Record that a quarterly evaluation was performed.

        Stores eval metadata in the holdout file.
        """
        data = self._load()
        if data is None:
            data = {"created_at": datetime.now(timezone.utc).isoformat(), "holdout_dates": [], "fraction": 0.15}

        evals = data.get("evaluations", [])
        evals.append({
            "timestamp": results.timestamp,
            "trader": trader,
            "n_dates": results.n_dates_with_data,
            "mean_objective_score": results.mean_objective_score,
            "mean_calmar": results.mean_calmar,
            "mean_profit_factor": results.mean_profit_factor,
            "mean_win_rate": results.mean_win_rate,
            "total_pnl": results.total_pnl,
        })
        data["evaluations"] = evals
        data["last_eval"] = results.timestamp
        self._save(data)

    def get_evaluation_history(self) -> List[Dict[str, Any]]:
        """Return the full evaluation history."""
        data = self._load()
        if data is None:
            return []
        return data.get("evaluations", [])

    # ── Evaluation ────────────────────────────────────────────────────────

    def evaluate(
        self,
        trader: str,
        variant_name: str,
        ticks_by_date: Dict[str, List[Any]],
        cost_model: Optional[CostModel] = None,
    ) -> QuarterlyEvalSummary:
        """Run evaluation on the holdout set for a given trader variant.

        Args:
            trader: Trader short name (e.g., 'kairos').
            variant_name: Variant being evaluated (e.g., 'baseline', 'wider_stops').
            ticks_by_date: Dict mapping date → list of Tick objects.
            cost_model: Optional CostModel for transaction cost adjustment.

        Returns:
            QuarterlyEvalSummary with aggregated metrics across all holdout dates.
        """
        from src.replay import ReplayHarness
        from src.signals import SignalEngine, SignalParams

        holdout_dates = self.get_holdout_dates()
        if not holdout_dates:
            log.warning("No holdout dates configured — cannot evaluate.")
            return QuarterlyEvalSummary(
                trader=trader,
                holdout_dates=[],
                n_dates=0,
                n_dates_with_data=0,
                mean_objective_score=0.0,
                mean_calmar=0.0,
                mean_profit_factor=0.0,
                mean_win_rate=0.0,
                total_pnl=0.0,
                total_cost_adjusted_pnl=0.0,
                n_trades=0,
            )

        if cost_model is None:
            cost_model = CostModel.default()

        per_date: List[HoldoutResult] = []

        for date_str in holdout_dates:
            ticks = ticks_by_date.get(date_str, [])
            if not ticks:
                log.debug("No data for holdout date %s — skipping", date_str)
                continue

            harness = ReplayHarness(
                initial_balance=100_000.0,
                cost_model=cost_model,
            )

            # Use a simple buy-and-hold signal trader for evaluation
            params = SignalParams()
            engine = SignalEngine(params=params)

            def trader_fn(tick, portfolio):
                return engine.process(tick)

            result = harness.run(ticks, trader_fn)

            trade_pnls = [getattr(t, "pnl_net", t.pnl) for t in result.trades]
            score = objective_score(result.returns, result.equity_curve, trade_pnls)
            calmar = float(compute_calmar(result.returns, result.equity_curve))
            pf = float(compute_profit_factor(trade_pnls))
            wr = result.net_win_rate if hasattr(result, "net_win_rate") else result.win_rate
            total_pnl = sum(result.trade_pnls)
            cost_pnl = sum(getattr(t, "pnl_net", t.pnl) for t in result.trades)

            hr = HoldoutResult(
                trader=trader,
                variant_name=variant_name,
                eval_date=date_str,
                holdout_dates=holdout_dates,
                n_holdout_days=1,
                objective_score=score,
                calmar=calmar,
                profit_factor=pf,
                win_rate=wr,
                n_trades=len(result.trades),
                total_pnl=float(total_pnl),
                cost_adjusted_pnl=float(cost_pnl),
            )
            per_date.append(hr)

        n_with_data = len(per_date)
        if n_with_data == 0:
            return QuarterlyEvalSummary(
                trader=trader,
                holdout_dates=holdout_dates,
                n_dates=len(holdout_dates),
                n_dates_with_data=0,
                mean_objective_score=0.0,
                mean_calmar=0.0,
                mean_profit_factor=0.0,
                mean_win_rate=0.0,
                total_pnl=0.0,
                total_cost_adjusted_pnl=0.0,
                n_trades=0,
            )

        summary = QuarterlyEvalSummary(
            trader=trader,
            holdout_dates=holdout_dates,
            n_dates=len(holdout_dates),
            n_dates_with_data=n_with_data,
            mean_objective_score=sum(r.objective_score for r in per_date) / n_with_data,
            mean_calmar=sum(r.calmar for r in per_date) / n_with_data,
            mean_profit_factor=sum(r.profit_factor for r in per_date) / n_with_data,
            mean_win_rate=sum(r.win_rate for r in per_date) / n_with_data,
            total_pnl=sum(r.total_pnl for r in per_date),
            total_cost_adjusted_pnl=sum(r.cost_adjusted_pnl for r in per_date),
            n_trades=sum(r.n_trades for r in per_date),
            per_date_results=per_date,
        )

        return summary

    # ── Internal helpers ──────────────────────────────────────────────────

    def _load(self) -> Optional[Dict[str, Any]]:
        """Load holdout data from JSON file."""
        if not self._holdout_file.exists():
            return None
        try:
            with open(self._holdout_file, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to load holdout file %s: %s", self._holdout_file, e)
            return None

    def _save(self, data: Dict[str, Any]) -> None:
        """Save holdout data to JSON file."""
        self._holdout_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._holdout_file, "w") as f:
            json.dump(data, f, indent=2)
        log.debug("Saved holdout set to %s", self._holdout_file)