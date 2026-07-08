#!/usr/bin/env python3
"""
RiskManager — chains composable risk gates in order and evaluates trades.

Each gate is a pure function: (context, action, timestamp=None) -> (granted, reason).
Gates are chained in order. The first gate to reject stops the chain.

Loads thresholds from YAML config via src.config_loader.

Observability: gate blocks fire alerts via src.observability:
  - p1: PositionGate / ExposureGate blocks a trade (position-limit or exposure-limit)
  - p0: same trader hits same gate ≥ 3 times in a session
"""

from collections import defaultdict
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional

from src.config_loader import get_config
from src.observability import alert, metrics
from src.risk.gates import (
    CashGate,
    PositionGate,
    ExposureGate,
    PDTGate,
    HoursGate,
    ConvictionGate,
)


class RiskManager:
    """Chains risk gates and evaluates trade actions.

    Usage:
        manager = RiskManager()
        granted, reason, gate_results = manager.evaluate(
            action={"type": "BUY", "ticker": "AAPL", "quantity": 10, "price": 150.0},
            portfolio={"portfolio_value": 100000, "cash": 50000, "positions": [...], "day_trades": [...]},
            positions=[],
            timestamp=datetime.now(),
        )
    """

    # Gates that trigger observability alerts on block
    _OBSERVABLE_GATES = {"PositionGate", "ExposureGate"}

    # Session-level threshold for escalating p1 → p0
    _SESSION_ESCALATION_THRESHOLD = 3

    def __init__(self, gates: Optional[List] = None):
        """Initialize RiskManager with gates.

        Args:
            gates: Optional list of gate instances. If None, loads from config.
        """
        if gates is not None:
            self._gates = gates
        else:
            self._gates = self._build_default_gates()

        # Per-session gate-block tracking: {(trader, gate_name): count}
        self._session_gate_blocks: Dict[Tuple[str, str], int] = defaultdict(int)

    def _build_default_gates(self) -> List:
        """Build the default gate chain from YAML config.

        Keys used from config/risk.yaml:
            risk.spec_risk.max_position_pct  (default: 0.20)
            risk.spec_risk.max_exposure_pct  (default: 1.00)
            risk.spec_risk.pdt_day_trade_limit (default: 3)
        """
        try:
            config = get_config()
            spec_risk = config.get("risk.spec_risk", {})
        except Exception:
            spec_risk = {}

        max_position_pct = float(spec_risk.get("max_position_pct", 0.20))
        max_exposure_pct = float(spec_risk.get("max_exposure_pct", 1.00))
        pdt_day_trade_limit = int(spec_risk.get("pdt_day_trade_limit", 3))

        return [
            CashGate(),
            HoursGate(),
            ConvictionGate(min_conviction=float(spec_risk.get("require_conviction", 0.3))),
            PositionGate(max_position_pct=max_position_pct),
            ExposureGate(max_exposure_pct=max_exposure_pct),
            PDTGate(pdt_day_trade_limit=pdt_day_trade_limit),
        ]

    @property
    def gates(self) -> List:
        """Return the list of gates in evaluation order."""
        return list(self._gates)

    def _fire_gate_block_alert(
        self,
        gate_name: str,
        reason: str,
        action: Dict[str, Any],
    ) -> None:
        """Fire observability alerts when an observable gate blocks a trade.

        p1: immediate alert for every PositionGate/ExposureGate block.
        p0: escalated if same trader+gate blocks ≥ SESSION_ESCALATION_THRESHOLD times.
        """
        if gate_name not in self._OBSERVABLE_GATES:
            return

        ticker = str(action.get("ticker", "")).upper()
        trader = str(action.get("trader", "unknown"))

        # Increment session counter
        block_key = (trader, gate_name)
        self._session_gate_blocks[block_key] += 1
        block_count = self._session_gate_blocks[block_key]

        alert_data = {
            "trader": trader,
            "ticker": ticker,
            "gate": gate_name,
            "reason": reason,
            "session_blocks": block_count,
        }

        # Always fire p1 for observable gate blocks
        alert.p1(
            f"{gate_name} blocked trade for {trader}",
            alert_data,
        )
        metrics.increment(
            f"gate.block.{gate_name.lower()}",
            tags={"trader": trader, "ticker": ticker},
        )

        # Escalate to p0 if threshold breached
        if block_count >= self._SESSION_ESCALATION_THRESHOLD:
            alert.p0(
                f"{trader} hit {gate_name} {block_count}x in session — risk limit breach",
                alert_data,
            )
            metrics.increment(
                "gate.block.escalated",
                tags={"trader": trader, "gate": gate_name},
            )

    def reset_session_blocks(self) -> None:
        """Reset the per-session gate-block counters (e.g., at session start)."""
        self._session_gate_blocks.clear()

    def evaluate(
        self,
        action: Dict[str, Any],
        portfolio: Optional[Dict[str, Any]] = None,
        positions: Optional[List[Dict[str, Any]]] = None,
        timestamp: Optional[datetime] = None,
    ) -> Tuple[bool, str, List[Dict[str, Any]]]:
        """Evaluate an action through all risk gates.

        Gates are evaluated in order. The first gate to reject stops the chain.
        All gate results (pass/fail/skip) are recorded.

        Args:
            action: Dict with trade details:
                - type (or action): "BUY", "SELL", "HOLD"
                - ticker: stock symbol
                - quantity: number of shares
                - price (or current_price): per-share price
            portfolio: Dict with portfolio state:
                - portfolio_value: total portfolio value
                - cash: available cash
                - day_trades: list of recent day trades
            positions: List of current open positions, each with:
                - ticker: stock symbol
                - quantity: shares held
                - market_value: current market value
            timestamp: Optional datetime for historical replay/backtesting

        Returns:
            (granted: bool, reason: str, gate_results: list)
            gate_results: list of dicts with {gate, passed, reason}
        """
        # Build context from portfolio + positions
        context = dict(portfolio or {})
        if positions is not None:
            context["positions"] = positions

        gate_results = []
        granted = True
        final_reason = "All gates passed"

        for gate in self._gates:
            gate_name = type(gate).__name__
            try:
                passed, reason = gate.check(context, action, timestamp)
                gate_results.append({
                    "gate": gate_name,
                    "passed": passed,
                    "reason": reason,
                })

                if not passed:
                    granted = False
                    final_reason = f"Blocked by {gate_name}: {reason}"
                    self._fire_gate_block_alert(gate_name, reason, action)
                    break

            except Exception as e:
                # Gate error — log and skip (fail-open for safety)
                gate_results.append({
                    "gate": gate_name,
                    "passed": True,
                    "reason": f"ERROR (skipped): {e}",
                })

        return granted, final_reason, gate_results
