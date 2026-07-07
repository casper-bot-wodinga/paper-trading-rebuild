"""Kairos ML Backtesting Toolkit — SPEC-v3 §15.

Kairos is the ML engineer. This toolkit gives Kairos a clean API to:
  - Run backtests over historical data with configurable SignalParams
  - Grid-search parameter space to find optimal configurations
  - Compute ML features from tick data (momentum, RSI, volatility, etc.)
  - Perform regime-aware performance analysis
  - Export results in a format consumable by Kairos's OpenClaw agent

Usage:
    from src.kairos_backtest import KairosBacktester

    kbt = KairosBacktester()
    result = kbt.run_backtest(ticker="AAPL", n_ticks=50)
    print(result.summary())

    # Grid search
    grid_result = kbt.grid_search(
        ticker="AAPL", n_ticks=50,
        param_grid={"momentum_threshold": [0.3, 0.5, 0.7]}
    )
    print(grid_result.best_params)
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from src.metrics import compute_calmar, compute_sortino, compute_max_drawdown
from src.replay import (
    Portfolio,
    ReplayHarness,
    ReplayResult,
    Tick,
    Trade,
    TraderDecision,
    TraderFn,
    make_uptrend_ticks,
)
from src.signals import SignalEngine, SignalParams, SignalReport


# ── ML Feature Engineering ────────────────────────────────────────────────────


@dataclass
class MLFeatures:
    """ML features computed from tick data for a given window."""

    timestamp: datetime
    ticker: str

    # Price features
    close: float
    log_return: float
    rolling_return_5: float
    rolling_return_20: float

    # Momentum features
    momentum_score: float
    rsi: float

    # Volatility features
    volatility_5: float
    volatility_20: float
    bollinger_position: float

    # Volume features
    volume_ratio: float
    volume_trend: float

    # Composite signal
    composite_signal: float = 0.0
    conviction: float = 0.0
    regime: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "ticker": self.ticker,
            "close": self.close,
            "log_return": self.log_return,
            "rolling_return_5": self.rolling_return_5,
            "rolling_return_20": self.rolling_return_20,
            "momentum_score": self.momentum_score,
            "rsi": self.rsi,
            "volatility_5": self.volatility_5,
            "volatility_20": self.volatility_20,
            "bollinger_position": self.bollinger_position,
            "volume_ratio": self.volume_ratio,
            "volume_trend": self.volume_trend,
            "composite_signal": self.composite_signal,
            "conviction": self.conviction,
            "regime": self.regime,
        }

    def to_array(self) -> np.ndarray:
        """Return features as a numpy array for ML training."""
        return np.array([
            self.close,
            self.log_return,
            self.rolling_return_5,
            self.rolling_return_20,
            self.momentum_score,
            self.rsi,
            self.volatility_5,
            self.volatility_20,
            self.bollinger_position,
            self.volume_ratio,
            self.volume_trend,
            self.composite_signal,
            self.conviction,
        ])


class FeatureEngineer:
    """Compute ML features from tick data.

    Produces a time series of MLFeatures that can be used
    for training, prediction, or signal generation.
    """

    def __init__(self, signal_params: Optional[SignalParams] = None):
        self.signal_engine = SignalEngine(signal_params or SignalParams())
        self._price_history: Dict[str, List[float]] = {}
        self._volume_history: Dict[str, List[float]] = {}

    def compute_features(self, ticks: Sequence[Tick]) -> List[MLFeatures]:
        """Compute ML features for a sequence of ticks."""
        features = []

        for tick in ticks:
            ticker = tick.ticker

            if ticker not in self._price_history:
                self._price_history[ticker] = []
                self._volume_history[ticker] = []
            self._price_history[ticker].append(tick.close)
            self._volume_history[ticker].append(tick.volume)

            prices = self._price_history[ticker]
            volumes = self._volume_history[ticker]

            log_return = 0.0
            if len(prices) >= 2:
                prev = prices[-2]
                if prev > 0:
                    log_return = float(np.log(prices[-1] / prev))

            rr5 = _rolling_return(prices, 5)
            rr20 = _rolling_return(prices, 20)
            vol5 = _rolling_volatility(prices, 5)
            vol20 = _rolling_volatility(prices, 20)

            bb_pos = 0.0
            if len(prices) >= 20:
                ma20 = float(np.mean(prices[-20:]))
                std20 = float(np.std(prices[-20:]))
                if std20 > 0:
                    bb_pos = (tick.close - ma20) / (2.0 * std20)

            vol_ratio = _volume_ratio(volumes, 20)
            vol_trend = _volume_trend(volumes, 5)

            signal_report: SignalReport = self.signal_engine.process(tick)

            mf = MLFeatures(
                timestamp=tick.timestamp,
                ticker=tick.ticker,
                close=tick.close,
                log_return=log_return,
                rolling_return_5=rr5,
                rolling_return_20=rr20,
                momentum_score=signal_report.momentum_score,
                rsi=signal_report.rsi,
                volatility_5=vol5,
                volatility_20=vol20,
                bollinger_position=bb_pos,
                volume_ratio=vol_ratio or 1.0,
                volume_trend=vol_trend,
                composite_signal=signal_report.composite_signal,
                conviction=signal_report.conviction,
                regime=signal_report.regime,
            )
            features.append(mf)

        return features

    def reset(self):
        """Clear internal state."""
        self._price_history.clear()
        self._volume_history.clear()
        self.signal_engine = SignalEngine(self.signal_engine.params)


def _rolling_return(prices: List[float], window: int) -> float:
    if len(prices) < window + 1:
        return 0.0
    prev = prices[-window - 1]
    if prev == 0:
        return 0.0
    return float(prices[-1] / prev - 1.0)


def _rolling_volatility(prices: List[float], window: int) -> float:
    if len(prices) < window + 1:
        return 0.0
    returns = [float(np.log(prices[i] / prices[i - 1])) for i in range(-window, 0)]
    return float(np.std(returns))


def _volume_ratio(volumes: List[float], window: int) -> float:
    if len(volumes) < window:
        return 1.0
    recent = volumes[-window:]
    avg = float(np.mean(recent[:-1])) if len(recent) > 1 else float(np.mean(recent))
    if avg == 0:
        return 1.0
    return float(recent[-1] / avg)


def _volume_trend(volumes: List[float], window: int) -> float:
    if len(volumes) < window:
        return 0.0
    recent = np.array(volumes[-window:], dtype=np.float64)
    mean_vol = float(np.mean(recent))
    if mean_vol == 0:
        return 0.0
    x = np.arange(window, dtype=np.float64)
    slope = float(np.polyfit(x, recent, 1)[0])
    return slope / mean_vol


# ── Backtest Result ────────────────────────────────────────────────────────────


@dataclass
class BacktestResult:
    """Result of a single backtest run."""

    ticker: str
    trader: str
    n_ticks: int
    params: Dict[str, Any] = field(default_factory=dict)

    total_pnl: float = 0.0
    total_trades: int = 0
    win_rate: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    calmar_ratio: float = 0.0
    sortino_ratio: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    objective: float = 0.0

    regime_breakdown: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    features: List[MLFeatures] = field(default_factory=list)

    @classmethod
    def from_replay(
        cls,
        replay: ReplayResult,
        ticker: str = "AAPL",
        n_ticks: int = 50,
        params: Optional[Dict[str, Any]] = None,
        features: Optional[List[MLFeatures]] = None,
    ) -> "BacktestResult":
        """Build a BacktestResult from a ReplayResult."""
        trades = replay.trades

        win_count = sum(1 for t in trades if t.pnl > 0)
        wr = win_count / len(trades) if trades else 0.0

        mmd = compute_max_drawdown(replay.equity_curve)

        calmar = 0.0
        sortino = 0.0
        if len(replay.returns) > 1:
            calmar = compute_calmar(replay.returns, replay.equity_curve)
            sortino = compute_sortino(replay.returns)

        # Sharpe: mean(returns) / std(returns)
        sharpe = 0.0
        if len(replay.returns) > 1:
            r_std = float(np.std(replay.returns))
            if r_std > 0:
                sharpe = float(np.mean(replay.returns)) / r_std * np.sqrt(252)

        gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))
        pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

        expectancy_val = replay.total_pnl / len(trades) if trades else 0.0

        # Composite objective: Calmar * 0.4 + Sortino * 0.15 + PF_clamped * 0.3 + Expectancy_scaled * 0.15
        pf_clamped = min(pf, 10.0) if pf != float("inf") else 10.0
        obj = (calmar * 0.4 + sortino * 0.15 + pf_clamped * 0.3 +
               (expectancy_val / max(abs(expectancy_val), 1.0)) * 0.15 * 10.0)

        return cls(
            ticker=ticker,
            trader="kairos",
            n_ticks=n_ticks,
            params=params or {},
            total_pnl=replay.total_pnl,
            total_trades=len(trades),
            win_rate=wr,
            max_drawdown=mmd,
            sharpe_ratio=sharpe,
            calmar_ratio=calmar,
            sortino_ratio=sortino,
            profit_factor=pf,
            expectancy=expectancy_val,
            objective=obj,
            features=features or [],
        )

    def summary(self) -> str:
        lines = [
            f"=== Backtest: {self.ticker} ({self.n_ticks} ticks, {self.trader}) ===",
            f"Trades: {self.total_trades} | Win Rate: {self.win_rate:.1%}",
            f"P&L: ${self.total_pnl:,.2f} | Max DD: {self.max_drawdown:.1%}",
            f"Calmar: {self.calmar_ratio:.2f} | Sortino: {self.sortino_ratio:.2f}",
            f"Profit Factor: {self.profit_factor:.2f} | Expectancy: ${self.expectancy:,.2f}",
            f"Objective: {self.objective:.3f}",
        ]
        if self.regime_breakdown:
            lines.append("\nRegime Performance:")
            for regime, stats in sorted(self.regime_breakdown.items()):
                lines.append(
                    f"  {regime}: {stats.get('trades', 0)} trades, "
                    f"WR {stats.get('win_rate', 0):.1%}, "
                    f"P&L ${stats.get('pnl', 0):,.2f}"
                )
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "trader": self.trader,
            "n_ticks": self.n_ticks,
            "params": self.params,
            "total_pnl": self.total_pnl,
            "total_trades": self.total_trades,
            "win_rate": self.win_rate,
            "max_drawdown": self.max_drawdown,
            "sharpe_ratio": self.sharpe_ratio,
            "calmar_ratio": self.calmar_ratio,
            "sortino_ratio": self.sortino_ratio,
            "profit_factor": self.profit_factor,
            "expectancy": self.expectancy,
            "objective": self.objective,
            "regime_breakdown": self.regime_breakdown,
        }


@dataclass
class GridSearchResult:
    """Result of a parameter grid search."""

    ticker: str
    trader: str
    n_ticks: int
    n_combinations: int
    results: List[BacktestResult] = field(default_factory=list)

    @property
    def best_result(self) -> Optional[BacktestResult]:
        if not self.results:
            return None
        return max(self.results, key=lambda r: r.objective)

    @property
    def best_params(self) -> Dict[str, Any]:
        br = self.best_result
        return br.params if br else {}

    def summary(self) -> str:
        lines = [
            f"=== Grid Search: {self.ticker}, {self.n_combinations} combos ===",
        ]
        br = self.best_result
        if br:
            lines.append(f"BEST: {br.params}")
            lines.append(
                f"  Objective: {br.objective:.3f}, P&L: ${br.total_pnl:,.2f}, "
                f"Trades: {br.total_trades}, WR: {br.win_rate:.1%}"
            )
        top3 = sorted(self.results, key=lambda r: r.objective, reverse=True)[:3]
        lines.append("\nTop 3:")
        for i, r in enumerate(top3):
            lines.append(
                f"  {i + 1}. Obj={r.objective:.3f} P&L=${r.total_pnl:,.2f} "
                f"Trades={r.total_trades} WR={r.win_rate:.1%} "
                f"Params={r.params}"
            )
        return "\n".join(lines)


# ── Kairos Backtester ──────────────────────────────────────────────────────────


class KairosBacktester:
    """Kairos's ML-focused backtesting toolkit.

    Wraps the replay harness, signal engine, and metrics into a
    clean API for running backtests and grid searches.

    Usage:
        kbt = KairosBacktester()
        result = kbt.run_backtest("AAPL", n_ticks=50)
        grid = kbt.grid_search("AAPL", n_ticks=50,
            param_grid={"momentum_threshold": [0.3, 0.5, 0.7]})
    """

    def __init__(
        self,
        initial_balance: float = 10_000,
        signal_params: Optional[SignalParams] = None,
    ):
        self.initial_balance = initial_balance
        self.signal_params = signal_params or SignalParams(
            momentum_threshold=0.25,  # relaxed for backtesting
            rsi_oversold=25.0,
            rsi_overbought=75.0,
            base_size_pct=0.15,
        )
        self.feature_engineer = FeatureEngineer(self.signal_params)

    def run_backtest(
        self,
        ticker: str = "AAPL",
        n_ticks: int = 50,
        params: Optional[SignalParams] = None,
        trader_fn: Optional[TraderFn] = None,
    ) -> BacktestResult:
        """Run a single backtest.

        Args:
            ticker: Stock ticker to simulate.
            n_ticks: Number of ticks to simulate.
            params: SignalParams override.
            trader_fn: Custom trader function. Uses built-in default if None.

        Returns:
            BacktestResult with full metrics and ML features.
        """
        sp = params or self.signal_params

        # Generate synthetic ticks
        ticks = make_uptrend_ticks(
            ticker=ticker, n=n_ticks
        )

        # Compute signal features on each tick
        engine = SignalEngine(sp)
        for tick in ticks:
            engine.process(tick)

        # Run replay
        harness = ReplayHarness(initial_balance=self.initial_balance)

        if trader_fn is None:
            trader_fn = _build_default_trader(engine)

        replay_result = harness.run(ticks, trader_fn)

        # Compute ML features
        self.feature_engineer.reset()
        features = self.feature_engineer.compute_features(ticks)

        # Build result with computed metrics
        result = BacktestResult.from_replay(
            replay_result,
            ticker=ticker,
            n_ticks=n_ticks,
            params=_params_to_dict(sp),
            features=features,
        )

        return result

    def grid_search(
        self,
        ticker: str = "AAPL",
        n_ticks: int = 50,
        param_grid: Optional[Dict[str, List[float]]] = None,
    ) -> GridSearchResult:
        """Run a grid search over parameter combinations.

        Args:
            ticker: Stock ticker.
            n_ticks: Ticks per backtest.
            param_grid: Dict of param_name → [values].
                Direct field names from SignalParams: "momentum_threshold",
                "rsi_oversold", "rsi_overbought", "vol_regime_threshold",
                "base_size_pct", "stop_loss_pct", etc.

        Returns:
            GridSearchResult with all combinations ranked by objective.
        """
        if param_grid is None:
            param_grid = {
                "momentum_threshold": [0.25, 0.40, 0.55],
                "rsi_oversold": [25.0, 30.0, 35.0],
            }

        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combinations = list(itertools.product(*values))
        results = []

        for combo in combinations:
            param_dict = dict(zip(keys, combo))
            sp = _apply_param_overrides(self.signal_params, param_dict)

            result = self.run_backtest(ticker=ticker, n_ticks=n_ticks, params=sp)
            result.params = param_dict
            results.append(result)

        return GridSearchResult(
            ticker=ticker,
            trader="kairos",
            n_ticks=n_ticks,
            n_combinations=len(combinations),
            results=results,
        )

    def multi_ticker_scan(
        self,
        tickers: List[str],
        n_ticks: int = 50,
        params: Optional[SignalParams] = None,
    ) -> List[BacktestResult]:
        """Scan multiple tickers with the same parameters.

        Args:
            tickers: List of ticker symbols.
            n_ticks: Ticks per backtest.
            params: SignalParams override.

        Returns:
            List of BacktestResult, sorted by objective descending.
        """
        results = []
        for ticker in tickers:
            result = self.run_backtest(ticker=ticker, n_ticks=n_ticks, params=params)
            results.append(result)
        results.sort(key=lambda r: r.objective, reverse=True)
        return results


# ── Helpers ────────────────────────────────────────────────────────────────────


def _params_to_dict(sp: SignalParams) -> Dict[str, Any]:
    """Convert SignalParams to a plain dict."""
    return {
        "momentum_threshold": sp.momentum_threshold,
        "momentum_lookback": sp.momentum_lookback,
        "rsi_oversold": sp.rsi_oversold,
        "rsi_overbought": sp.rsi_overbought,
        "volume_threshold": sp.volume_threshold,
        "vol_regime_threshold": sp.vol_regime_threshold,
        "base_size_pct": sp.base_size_pct,
        "stop_loss_pct": sp.stop_loss_pct,
        "take_profit_pct": sp.take_profit_pct,
    }


def _apply_param_overrides(
    base: SignalParams, overrides: Dict[str, float]
) -> SignalParams:
    """Apply parameter overrides to a SignalParams, returning a new copy."""
    # Build a dict of all current values and override
    kwargs = {
        "momentum_threshold": base.momentum_threshold,
        "momentum_lookback": base.momentum_lookback,
        "momentum_decay": base.momentum_decay,
        "rsi_oversold": base.rsi_oversold,
        "rsi_overbought": base.rsi_overbought,
        "bollinger_std": base.bollinger_std,
        "volume_threshold": base.volume_threshold,
        "vol_regime_threshold": base.vol_regime_threshold,
        "vol_reduction_multiplier": base.vol_reduction_multiplier,
        "base_size_pct": base.base_size_pct,
        "conviction_multiplier": base.conviction_multiplier,
        "max_positions": base.max_positions,
        "stop_loss_pct": base.stop_loss_pct,
        "take_profit_pct": base.take_profit_pct,
        "trailing_stop_pct": base.trailing_stop_pct,
        "weight_trending_up": base.weight_trending_up,
        "weight_trending_down": base.weight_trending_down,
        "weight_mean_reverting": base.weight_mean_reverting,
        "weight_high_volatility": base.weight_high_volatility,
    }
    kwargs.update({k: v for k, v in overrides.items() if k in kwargs})
    return SignalParams(**kwargs)


def _build_default_trader(engine: SignalEngine) -> TraderFn:
    """Build a default trader function using the signal engine."""

    def trader(tick: Tick, portfolio: Portfolio) -> TraderDecision:
        report: SignalReport = engine.process(tick)

        # Simple strategy: buy on any bullish momentum when not overbought
        if report.momentum_score > 0.02 and report.rsi < 70:
            has_position = tick.ticker in portfolio.positions
            if not has_position and portfolio.position_count < 5:
                return TraderDecision(
                    ticker=tick.ticker,
                    decision="BUY",
                    conviction=min(report.momentum_score, 0.9),
                    rationale=f"Mom={report.momentum_score:.2f} RSI={report.rsi:.0f} Regime={report.regime}",
                    shares=10,
                )

        # Sell on overbought
        if report.rsi > 75 and tick.ticker in portfolio.positions:
            return TraderDecision(
                ticker=tick.ticker,
                decision="SELL",
                conviction=0.7,
                rationale=f"Overbought: RSI={report.rsi:.0f}",
                shares=portfolio.positions[tick.ticker].shares,
            )

        return TraderDecision(
            ticker=tick.ticker,
            decision="HOLD",
            conviction=0.5,
            rationale=f"No signal: Mom={report.momentum_score:.2f} RSI={report.rsi:.0f}",
            shares=0,
        )

    return trader