"""Tests for src/transaction_costs.py — §5 cost model for replay results.

Run:  pytest tests/test_transaction_costs.py -v
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List

import pytest

from src.transaction_costs import CostModel


# ── Minimal Trade stub matching the rebuild repo's Trade dataclass ──────────

@dataclass
class Trade:
    ticker: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    return_pct: float
    decision: str


@dataclass
class ReplayResult:
    trades: List[Trade]


# ── Helpers ─────────────────────────────────────────────────────────────────

def make_trade(
    entry_price: float = 100.0,
    exit_price: float = 105.0,
    shares: int = 100,
) -> Trade:
    """Create a minimal Trade for testing."""
    pnl = (exit_price - entry_price) * shares
    return_pct = (exit_price - entry_price) / entry_price * 100
    return Trade(
        ticker="AAPL",
        entry_time=datetime(2024, 1, 2, 10, 0, 0),
        exit_time=datetime(2024, 1, 2, 15, 30, 0),
        entry_price=entry_price,
        exit_price=exit_price,
        shares=shares,
        pnl=pnl,
        return_pct=return_pct,
        decision="SELL",
    )


# ── Tests: apply_to_trade ──────────────────────────────────────────────────

def test_cost_calculation_basic():
    """$10,000 notional entry, 10 bps slippage-only model ≈ $20 cost round-trip."""
    model = CostModel(slippage_bps=10.0, spread_bps=0.0, commission_per_share=0.0, min_trade_cost=0.0)
    # 100 shares × $100 entry, × $100 exit → round-trip notional = $20,000
    # 10 bps = 0.10% → 20000 * 0.001 = $20.00
    gross, net = model.apply_to_trade(100.0, 100.0, 100)
    assert gross == 0.0
    assert net == pytest.approx(-20.0, abs=0.01)


def test_cost_calculation_profitable_trade():
    """Profitable trade: gross positive, net reduced by costs."""
    model = CostModel(slippage_bps=10.0, spread_bps=5.0, commission_per_share=0.0, min_trade_cost=0.0)
    # Buy 100 @ 100, sell @ 101: notional = 20100
    # slippage 10 bps: 20100 * 0.001 = 20.10
    # spread 5 bps: 20100 * 0.0005 = 10.05
    # total cost: 30.15; gross pnl = 100
    gross, net = model.apply_to_trade(100.0, 101.0, 100)
    assert gross == 100.0
    assert net == pytest.approx(100.0 - 30.15, abs=0.02)


def test_minimum_cost_floor():
    """Tiny trade should be floored at min_trade_cost ($1.00)."""
    model = CostModel(slippage_bps=0.0, spread_bps=0.0, commission_per_share=0.0, min_trade_cost=1.0)
    # 1 share @ $50 → notional = $100, zero bps = $0 natural cost → floored at $1
    gross, net = model.apply_to_trade(50.0, 50.0, 1)
    assert gross == 0.0
    assert net == -1.0


def test_minimum_cost_floor_not_applied_when_cost_exceeds():
    """When natural cost > min, use natural cost, not floor."""
    model = CostModel(slippage_bps=100.0, spread_bps=0.0, commission_per_share=0.0, min_trade_cost=1.0)
    # notional = 100 * 2 = 200, slippage = 200 * 100/10000 = $2.00 > $1.00 floor
    gross, net = model.apply_to_trade(100.0, 100.0, 1)
    assert net == pytest.approx(-2.0, abs=0.01)


def test_commission_included():
    """Commission should add to costs on both sides."""
    model = CostModel(slippage_bps=0.0, spread_bps=0.0, commission_per_share=0.01, min_trade_cost=0.0)
    # 100 shares → commission = 0.01 * 100 * 2 = $2.00
    gross, net = model.apply_to_trade(100.0, 100.0, 100)
    assert net == pytest.approx(-2.0, abs=0.01)


def test_zero_quantity_trade():
    """Zero-share trade should have zero cost and zero pnl."""
    model = CostModel.default()
    gross, net = model.apply_to_trade(100.0, 101.0, 0)
    assert gross == 0.0
    assert net == 0.0


def test_negative_quantity_guarded():
    """Negative shares (invalid) returns zero."""
    model = CostModel.default()
    gross, net = model.apply_to_trade(100.0, 101.0, -10)
    assert gross == 0.0
    assert net == 0.0


def test_losing_trade_costs_make_it_worse():
    """Losing trade: costs deepen the net loss."""
    model = CostModel(slippage_bps=10.0, spread_bps=5.0, commission_per_share=0.0, min_trade_cost=0.0)
    # Buy 100 @ 100, sell @ 98: gross loss = -200
    # notional = 19800, cost ≈ 19800 * 15/10000 = 29.70
    gross, net = model.apply_to_trade(100.0, 98.0, 100)
    assert gross == -200.0
    assert net < gross  # net is worse (more negative)


# ── Tests: apply_to_result ──────────────────────────────────────────────────

def test_apply_to_result_single_trade():
    """apply_to_result adds pnl_net to each trade and returns total cost."""
    model = CostModel(slippage_bps=10.0, spread_bps=0.0, commission_per_share=0.0, min_trade_cost=0.0)
    t = make_trade(entry_price=100.0, exit_price=101.0, shares=100)
    result = ReplayResult(trades=[t])

    total_cost = model.apply_to_result(result)

    # notional = 100*100 + 101*100 = 20100, slippage = 20100*10/10000 = 20.10
    assert total_cost == pytest.approx(20.10, abs=0.02)
    assert hasattr(t, "pnl_net")
    assert t.pnl_net == pytest.approx(t.pnl - 20.10, abs=0.02)


def test_apply_to_result_multiple_trades():
    """Total cost is sum of individual per-trade costs."""
    model = CostModel(slippage_bps=10.0, spread_bps=0.0, commission_per_share=0.0, min_trade_cost=0.0)
    t1 = make_trade(entry_price=100.0, exit_price=101.0, shares=100)
    t2 = make_trade(entry_price=50.0, exit_price=55.0, shares=200)
    result = ReplayResult(trades=[t1, t2])

    total_cost = model.apply_to_result(result)

    # t1: notional=20100, cost=20100*10/10000=20.10
    # t2: notional=50*200+55*200=21000, cost=21000*10/10000=21.00
    expected = 20.10 + 21.00
    assert total_cost == pytest.approx(expected, abs=0.05)

    assert t1.pnl_net == pytest.approx(t1.pnl - 20.10, abs=0.05)
    assert t2.pnl_net == pytest.approx(t2.pnl - 21.00, abs=0.05)


# ── Tests: cross-model comparison ───────────────────────────────────────────

def test_alpaca_paper_cheaper_than_realistic():
    """Alpaca paper should have lower costs than realistic model."""
    paper = CostModel.alpaca_paper()
    realistic = CostModel.realistic()

    gross, net_paper = paper.apply_to_trade(100.0, 101.0, 100)
    _, net_realistic = realistic.apply_to_trade(100.0, 101.0, 100)

    assert net_paper > net_realistic, (
        f"Paper net {net_paper:.2f} should be higher than realistic {net_realistic:.2f}"
    )


def test_default_vs_realistic_are_equivalent():
    """default() and realistic() use same params in our design."""
    d = CostModel.default()
    r = CostModel.realistic()
    _, net_d = d.apply_to_trade(100.0, 101.0, 100)
    _, net_r = r.apply_to_trade(100.0, 101.0, 100)
    assert net_d == pytest.approx(net_r, abs=0.01)


# ── Tests: high-frequency penalty ───────────────────────────────────────────

def test_many_tiny_trades_penalized_more_than_few_big_trades():
    """100 tiny trades should incur higher total costs than 5 larger trades.

    Even with the same total notional exposure, the minimum per-trade cost
    and commission floors punish high-frequency variants.
    """
    model = CostModel(slippage_bps=10.0, spread_bps=5.0, commission_per_share=0.0, min_trade_cost=1.0)

    # 5 large trades: 1000 shares each, total 5000 shares
    large_trades = [
        make_trade(entry_price=100.0, exit_price=101.0, shares=1000)
        for _ in range(5)
    ]
    result_large = ReplayResult(trades=large_trades)
    cost_large = model.apply_to_result(result_large)

    # 100 tiny trades: 50 shares each, total 5000 shares (same total)
    tiny_trades = [
        make_trade(entry_price=100.0, exit_price=101.0, shares=50)
        for _ in range(100)
    ]
    result_tiny = ReplayResult(trades=tiny_trades)
    cost_tiny = model.apply_to_result(result_tiny)

    # Same total notional, but min_trade_cost floors tiny trades
    # 100 * $1 min = $100 minimum vs 5 large slip into natural-cost territory
    assert cost_tiny > cost_large, (
        f"100 tiny trades cost {cost_tiny:.2f} should exceed "
        f"5 large trades cost {cost_large:.2f}"
    )


# ── Tests: edge cases ──────────────────────────────────────────────────────

def test_empty_trades_list():
    """Cost model on result with no trades should return 0.0."""
    model = CostModel.default()
    result = ReplayResult(trades=[])
    total_cost = model.apply_to_result(result)
    assert total_cost == 0.0


def test_cost_never_exceeds_gross_plus_min():
    """Cost model shouldn't produce absurd values."""
    model = CostModel(slippage_bps=1000.0, spread_bps=0.0, commission_per_share=0.0, min_trade_cost=1.0)
    gross, net = model.apply_to_trade(100.0, 100.0, 1)
    # notional = 200, cost = 200*0.1 = 20, gross=0, net=-20
    assert net == pytest.approx(-20.0, abs=0.01)
    assert net >= -25.0  # sanity bound
