"""Transaction cost model for replay results — SPEC §5.

Applies realistic trading costs (slippage, spread, commission) to completed
trades from the replay harness. Without this, high-frequency variants always
win because gross returns ignore friction.

Integration: called AFTER ReplayHarness.run() and BEFORE objective_score().

Usage:
    from src.transaction_costs import CostModel
    from src.replay import ReplayResult

    costs = CostModel.default()
    result: ReplayResult = harness.run(data, trader)
    total_cost = costs.apply_to_result(result)
    # Each trade now has .pnl_net = .pnl - cost
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.replay import ReplayResult


@dataclass
class CostModel:
    """Post-hoc transaction cost model for replay trade results."""

    slippage_bps: float = 10.0  # 0.1% per round-trip trade
    commission_per_share: float = 0.0  # $0 for Alpaca free tier
    spread_bps: float = 5.0  # 0.05% average bid-ask spread
    min_trade_cost: float = 1.0  # minimum $1 cost per round-trip

    # ── Factory methods ──────────────────────────────────────────────────

    @staticmethod
    def default() -> "CostModel":
        """Default retail trading costs: 10 bps slippage + 5 bps spread."""
        return CostModel()

    @staticmethod
    def alpaca_paper() -> "CostModel":
        """Alpaca paper trading — no commissions, reduced slippage/spread."""
        return CostModel(commission_per_share=0.0, slippage_bps=5.0, spread_bps=3.0)

    @staticmethod
    def realistic() -> "CostModel":
        """Realistic retail costs for evaluation / live comparison."""
        return CostModel(slippage_bps=10.0, spread_bps=5.0)

    # ── Core API ─────────────────────────────────────────────────────────

    def apply_to_trade(
        self, entry_price: float, exit_price: float, shares: int
    ) -> tuple[float, float]:
        """Apply costs to a single round-trip trade.

        Args:
            entry_price: Fill price at entry.
            exit_price: Fill price at exit.
            shares: Number of shares traded.

        Returns:
            (gross_pnl, net_pnl) — both in dollars.
        """
        if shares <= 0:
            return 0.0, 0.0

        gross_pnl = (exit_price - entry_price) * shares

        # Round-trip notional: both sides of the trade
        notional = (entry_price + exit_price) * shares

        slippage = notional * self.slippage_bps / 10000.0
        spread = notional * self.spread_bps / 10000.0
        commission = self.commission_per_share * shares * 2  # buy + sell

        total_cost = max(slippage + spread + commission, self.min_trade_cost)
        net_pnl = gross_pnl - total_cost

        return gross_pnl, net_pnl

    def apply_to_result(self, result: "ReplayResult") -> float:
        """Apply costs to every trade in a ReplayResult.

        Sets ``trade.pnl_net = trade.pnl - cost`` on each Trade object
        and returns the total cost deducted across all trades.

        Args:
            result: A ReplayResult from ReplayHarness.run().

        Returns:
            Total cost deducted (sum of all per-trade costs).
        """
        total_cost = 0.0
        for trade in result.trades:
            _, net_pnl = self.apply_to_trade(
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                shares=trade.shares,
            )
            cost = trade.pnl - net_pnl
            trade.pnl_net = net_pnl  # type: ignore[attr-defined]
            total_cost += cost
        return total_cost
