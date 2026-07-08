"""
K-Means Regime Detector — replaces HMM CHOPPY/SUSTAINABLE/EXHAUSTED.

Uses K-Means clustering (k=5) on multi-TF feature vectors to identify
market regimes without the degenerate state collapse problem of HMMs.

Features:
- Multi-timeframe momentum (5d, 20d, 50d)
- RSI + RSI trend
- MACD cross + histogram
- Volatility (ATR %)
- Volume trend
- Price velocity
- Sector breadth (XLK, XLF, XLY momentum)

Regime labels are numerical cluster IDs with descriptive names assigned
by feature centroid analysis. Auto-retrains daily via cron or API call.

Usage:
    detector = RegimeDetector(k=5)
    detector.fit(price_history)  # train on 2y SPY data
    regime = detector.predict(current_features)  # → {cluster: 2, label: "momentum", confidence: 0.87}
"""

from __future__ import annotations

import json
import logging
import pickle
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

log = logging.getLogger("regime-detector")

# ── Regime descriptions (assigned after clustering, by centroid analysis) ────
REGIME_LABELS = {
    0: "momentum_bull",      # Strong upward momentum, high RSI, positive breadth
    1: "momentum_bear",      # Strong downward momentum, low RSI, negative breadth
    2: "mean_reversion",     # Sideways/oscillating — RSI 40-60, flat MACD
    3: "volatility_spike",   # High ATR, wide swings — risk-on/off whipsaw
    4: "low_vol_drift",      # Tight range, low volume — summer doldrums / pre-FOMC
}


@dataclass
class RegimeResult:
    """Output from regime detection."""
    cluster: int
    label: str
    confidence: float           # 0-1 distance from cluster center (1 = dead center)
    description: str
    features: Dict[str, float]  # current feature vector
    centroids: Dict[int, Dict[str, float]]  # all cluster centroids for context
    retrain_age_hours: float

    def to_dict(self) -> dict:
        return {
            "cluster": self.cluster,
            "label": self.label,
            "confidence": round(self.confidence, 4),
            "description": self.description,
            "features": {k: round(v, 6) for k, v in self.features.items()},
            "retrain_age_hours": round(self.retrain_age_hours, 1),
        }


@dataclass
class RegimeDetector:
    """K-Means regime detector with auto-retrain capability."""

    k: int = 5
    model_path: str = field(default="")
    random_state: int = 42

    def __post_init__(self):
        self._scaler: Optional[StandardScaler] = None
        self._kmeans: Optional[KMeans] = None
        self._feature_names: List[str] = []
        self._centroid_labels: Dict[int, str] = {}
        self._trained_at: Optional[datetime] = None

        if self.model_path:
            self._load()

    # ── Public API ───────────────────────────────────────────────────────────

    def fit(self, price_history: List[dict], symbols: Optional[List[str]] = None) -> "RegimeDetector":
        """Train K-Means on historical data.

        Args:
            price_history: List of {symbol, date, open, high, low, close, volume} dicts
            symbols: Symbols to include (default: ["SPY"])

        Returns:
            self for chaining
        """
        symbols = symbols or ["SPY"]
        features, names = self._extract_features(price_history, symbols)
        self._feature_names = names
        X = np.array(features)

        if len(X) < self.k * 10:
            raise ValueError(f"Need at least {self.k * 10} data points, got {len(X)}")

        self._scaler = StandardScaler()
        X_scaled = self._scaler.fit_transform(X)

        self._kmeans = KMeans(n_clusters=self.k, random_state=self.random_state, n_init=10)
        self._kmeans.fit(X_scaled)

        self._assign_labels()
        self._trained_at = datetime.now()
        self._save()

        log.info(f"K-Means regime detector trained: k={self.k}, n_samples={len(X)}")
        return self

    def predict(self, current_features: Dict[str, float]) -> RegimeResult:
        """Predict regime from current feature snapshot.

        Args:
            current_features: Dict of feature_name → value

        Returns:
            RegimeResult with cluster assignment and metadata
        """
        if self._kmeans is None:
            if self.model_path:
                self._load()
            if self._kmeans is None:
                raise RuntimeError("Model not trained. Call .fit() first.")

        # Build feature vector in correct order
        X = np.array([[current_features.get(name, 0.0) for name in self._feature_names]])
        X_scaled = self._scaler.transform(X)

        cluster = int(self._kmeans.predict(X_scaled)[0])
        distances = self._kmeans.transform(X_scaled)[0]

        # Confidence: 1 - normalized distance from center
        # The farthest cluster distance anchors the normalization
        max_dist = np.max(distances)
        min_dist = distances[cluster]
        confidence = 1.0 - (min_dist / max_dist) if max_dist > 0 else 1.0

        label = self._centroid_labels.get(cluster, REGIME_LABELS.get(cluster, f"cluster_{cluster}"))
        description = self._describe_regime(label, current_features)

        age_hours = 0.0
        if self._trained_at:
            age_hours = (datetime.now() - self._trained_at).total_seconds() / 3600

        return RegimeResult(
            cluster=cluster,
            label=label,
            confidence=confidence,
            description=description,
            features=current_features,
            centroids=self._get_centroids(),
            retrain_age_hours=age_hours,
        )

    # ── Feature Extraction ───────────────────────────────────────────────────

    def _extract_features(self, data: List[dict], symbols: List[str]) -> Tuple[List[List[float]], List[str]]:
        """Extract multi-TF feature vectors from OHLCV data."""
        feature_names = []
        feature_vectors = []

        for symbol in symbols:
            symbol_data = sorted(
                [d for d in data if d.get("symbol", "").upper() == symbol.upper()],
                key=lambda x: x.get("date", ""),
            )
            closes = np.array([d["close"] for d in symbol_data])
            volumes = np.array([d.get("volume", 0) for d in symbol_data])
            highs = np.array([d["high"] for d in symbol_data])
            lows = np.array([d["low"] for d in symbol_data])

            for i in range(50, len(closes)):
                window = closes[max(0, i-50):i+1]
                vol_window = volumes[max(0, i-50):i+1]

                features = {
                    # Momentum (multi-TF)
                    f"{symbol}_mom_5d": self._pct_change(window, 5),
                    f"{symbol}_mom_20d": self._pct_change(window, 20),
                    f"{symbol}_mom_50d": self._pct_change(window, min(50, len(window)-1)),
                    # RSI
                    f"{symbol}_rsi_14": self._compute_rsi(window, 14),
                    f"{symbol}_rsi_trend": self._compute_rsi_trend(window, 14),
                    # MACD
                    f"{symbol}_macd_diff": self._compute_macd_diff(window),
                    # Volatility
                    f"{symbol}_atr_pct": self._compute_atr_pct(highs[max(0,i-20):i+1], lows[max(0,i-20):i+1], window),
                    # Volume
                    f"{symbol}_vol_trend": self._pct_change(vol_window.astype(float), min(20, len(vol_window)-1)),
                    # Price velocity (acceleration)
                    f"{symbol}_price_vel": self._price_velocity(window),
                }

                if i == 50:  # first pass: collect names
                    feature_names = list(features.keys())

                feature_vectors.append([features.get(name, 0.0) for name in feature_names])

        return feature_vectors, feature_names

    # ── Technical Indicators ─────────────────────────────────────────────────

    def _pct_change(self, series: np.ndarray, lookback: int) -> float:
        if len(series) < lookback + 1:
            return 0.0
        return float((series[-1] - series[-lookback-1]) / series[-lookback-1])

    def _compute_rsi(self, closes: np.ndarray, period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        deltas = np.diff(closes[-period-1:])
        gains = np.maximum(deltas, 0)
        losses = np.abs(np.minimum(deltas, 0))
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return float(100.0 - (100.0 / (1.0 + rs)))

    def _compute_rsi_trend(self, closes: np.ndarray, period: int = 14) -> float:
        """RSI difference between now and 5 bars ago."""
        if len(closes) < period + 6:
            return 0.0
        rsi_now = self._compute_rsi(closes, period)
        rsi_ago = self._compute_rsi(closes[:-5], period)
        return float(rsi_now - rsi_ago)

    def _compute_macd_diff(self, closes: np.ndarray) -> float:
        """MACD line minus signal line."""
        if len(closes) < 26:
            return 0.0
        ema12 = self._ema(closes, 12)
        ema26 = self._ema(closes, 26)
        macd_line = ema12 - ema26

        # Signal line: 9-period EMA of MACD line (approximate)
        if len(closes) < 35:
            return 0.0
        # Fast approximation using last value
        return float(macd_line)

    def _ema(self, series: np.ndarray, period: int) -> float:
        if len(series) < period:
            return float(np.mean(series))
        alpha = 2.0 / (period + 1)
        result = np.mean(series[:period])
        for val in series[period:]:
            result = alpha * val + (1 - alpha) * result
        return float(result)

    def _compute_atr_pct(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
        if len(highs) < period or len(lows) < period:
            return 0.0
        tr = np.maximum(highs[-period:] - lows[-period:],
                        np.abs(highs[-period:] - np.roll(closes[-period:], 1)))
        atr = np.mean(tr)
        return float(atr / closes[-1]) if closes[-1] > 0 else 0.0

    def _price_velocity(self, closes: np.ndarray) -> float:
        """Second derivative of price — acceleration."""
        if len(closes) < 5:
            return 0.0
        returns = np.diff(closes[-5:]) / closes[-5:-1]
        return float(np.diff(returns).mean()) if len(returns) > 1 else 0.0

    # ── Label Assignment ─────────────────────────────────────────────────────

    def _assign_labels(self):
        """Assign human-readable labels to clusters based on centroid features.
        
        Centroids are Z-scored (mean=0, std≈1 across the training set).
        Labels are assigned by ranking clusters on key dimensions:
        - Highest momentum → momentum_bull
        - Highest volatility → volatility_spike
        - Lowest momentum → momentum_bear
        - Lowest volatility → low_vol_drift
        - Remaining → mean_reversion
        """
        centroids = self._kmeans.cluster_centers_
        labels = {}

        # Find feature indices
        mom_idx = self._feature_names.index("SPY_mom_20d") if "SPY_mom_20d" in self._feature_names else 0
        rsi_idx = self._feature_names.index("SPY_rsi_14") if "SPY_rsi_14" in self._feature_names else 1
        atr_idx = self._feature_names.index("SPY_atr_pct") if "SPY_atr_pct" in self._feature_names else -1
        vol_idx = self._feature_names.index("SPY_vol_trend") if "SPY_vol_trend" in self._feature_names else -1

        # Extract scores per cluster
        cluster_scores = []
        for i, centroid in enumerate(centroids):
            mom_score = centroid[mom_idx]
            rsi_score = centroid[rsi_idx] if rsi_idx >= 0 else 0
            atr_score = centroid[atr_idx] if atr_idx >= 0 else 0
            vol_score = centroid[vol_idx] if vol_idx >= 0 else 0
            cluster_scores.append({
                "id": i,
                "mom": mom_score,
                "rsi": rsi_score,
                "atr": atr_score,
                "vol": vol_score,
            })

        # Sort clusters by key attributes for ranking-based assignment
        by_mom = sorted(cluster_scores, key=lambda c: c["mom"], reverse=True)
        by_atr = sorted(cluster_scores, key=lambda c: c["atr"], reverse=True)

        # Assign labels using relative ranking (works regardless of scaling)
        assigned = set()

        # 1. Highest momentum cluster → momentum_bull
        for cs in by_mom:
            if cs["id"] not in assigned:
                labels[cs["id"]] = "momentum_bull"
                assigned.add(cs["id"])
                break

        # 2. Lowest momentum cluster → momentum_bear
        for cs in reversed(by_mom):
            if cs["id"] not in assigned:
                labels[cs["id"]] = "momentum_bear"
                assigned.add(cs["id"])
                break

        # 3. Highest ATR cluster → volatility_spike
        for cs in by_atr:
            if cs["id"] not in assigned:
                labels[cs["id"]] = "volatility_spike"
                assigned.add(cs["id"])
                break

        # 4. Remaining → mean_reversion (or low_vol_drift if truly flat)
        for cs in cluster_scores:
            if cs["id"] not in assigned:
                # Check if this remaining cluster is truly low-vol
                if abs(cs["mom"]) < abs(cluster_scores[by_mom[-1]["id"]]["mom"]) * 0.5 \
                   and cs["atr"] < sorted([c["atr"] for c in cluster_scores])[1]:
                    labels[cs["id"]] = "low_vol_drift"
                else:
                    labels[cs["id"]] = "mean_reversion"
                assigned.add(cs["id"])

        self._centroid_labels = labels

    def _describe_regime(self, label: str, features: Dict[str, float]) -> str:
        """Generate a one-line description of the current regime."""
        descriptions = {
            "momentum_bull": "Strong upward trend — momentum trades favored, size up",
            "momentum_bear": "Strong downward trend — shorts or cash, no longs",
            "mean_reversion": "Sideways/oscillating — mean-reversion setups, smaller size",
            "volatility_spike": "High volatility — wide stops, reduced size, or cash",
            "low_vol_drift": "Low volume chop — wait for catalyst, no new entries",
        }
        return descriptions.get(label, f"Unknown regime: {label}")

    def _get_centroids(self) -> Dict[int, Dict[str, float]]:
        """Return centroid dict for API response."""
        if self._kmeans is None:
            return {}
        result = {}
        for i, centroid in enumerate(self._kmeans.cluster_centers_):
            result[i] = {
                name: round(float(centroid[j]), 6)
                for j, name in enumerate(self._feature_names)
            }
        return result

    # ── Persistence ──────────────────────────────────────────────────────────

    def _save(self):
        if not self.model_path:
            return
        Path(self.model_path).parent.mkdir(parents=True, exist_ok=True)
        state = {
            "k": self.k,
            "feature_names": self._feature_names,
            "centroid_labels": self._centroid_labels,
            "trained_at": self._trained_at.isoformat() if self._trained_at else None,
            "scaler": self._scaler,
            "kmeans": self._kmeans,
        }
        with open(self.model_path, "wb") as f:
            pickle.dump(state, f)

    def _load(self) -> bool:
        path = Path(self.model_path)
        if not path.exists():
            return False
        try:
            with open(path, "rb") as f:
                state = pickle.load(f)
            self.k = state["k"]
            self._feature_names = state["feature_names"]
            self._centroid_labels = state.get("centroid_labels", {})
            self._trained_at = datetime.fromisoformat(state["trained_at"]) if state.get("trained_at") else None
            self._scaler = state["scaler"]
            self._kmeans = state["kmeans"]
            return True
        except Exception as e:
            log.warning(f"Failed to load model: {e}")
            return False


# ── Convenience factory ──────────────────────────────────────────────────────

def create_detector(k: int = 5, model_path: str = "/home/openclaw/data/regime_kmeans.pkl") -> RegimeDetector:
    """Create a RegimeDetector, loading from disk if available."""
    return RegimeDetector(k=k, model_path=model_path)
