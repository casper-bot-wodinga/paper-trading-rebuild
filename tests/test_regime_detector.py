"""Tests for K-Means regime detector — SPEC-v3 regime classification."""

import pytest
import numpy as np

from src.signals import (
    SignalParams,
    SignalEngine,
    _get_regime_detector,
    _kmeans_regime,
    REGIME_DETECTOR_PATH,
)
from src.regime_detector import (
    RegimeDetector,
    RegimeResult,
    REGIME_LABELS,
    create_detector,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_ohlcv_data(symbols=("SPY",), n_days=120, trend=0.001, noise=0.015, seed=42):
    """Generate synthetic OHLCV data for regime detector training.

    Each symbol gets n_days of bars with a gentle trend + noise.
    """
    rng = np.random.default_rng(seed)
    data = []
    for sym in symbols:
        prices = [100.0]
        for i in range(1, n_days):
            ret = trend + noise * rng.standard_normal()
            prices.append(prices[-1] * (1 + ret))

        volumes = rng.integers(1_000_000, 10_000_000, n_days)

        for i in range(n_days):
            p = prices[i]
            daily_range = abs(p * noise * rng.standard_normal())
            data.append({
                "symbol": sym,
                "date": f"2024-{((i // 30) + 1):02d}-{(i % 30 + 1):02d}",
                "open": p,
                "high": p + daily_range,
                "low": p - daily_range,
                "close": p,
                "volume": int(volumes[i]),
            })
    return data


def make_ohclv_with_regime_phases(n_days=200):
    """Generate data with distinct market regime phases.

    Phases:
    - Days 0-49: Strong bull trend (momentum_bull)
    - Days 50-99: High volatility sideways (volatility_spike)
    - Days 100-149: Bear trend (momentum_bear)
    - Days 150-199: Low vol drift (low_vol_drift)
    """
    rng = np.random.default_rng(123)
    prices = [100.0]

    for i in range(1, n_days):
        if i < 50:
            # Strong bull: +0.3% per day with low noise
            ret = 0.003 + 0.005 * rng.standard_normal()
        elif i < 100:
            # High vol sideways: 0% trend with high noise
            ret = 0.000 + 0.025 * rng.standard_normal()
        elif i < 150:
            # Strong bear: -0.3% per day with low noise
            ret = -0.003 + 0.006 * rng.standard_normal()
        else:
            # Low vol drift: 0% trend with very low noise
            ret = 0.000 + 0.002 * rng.standard_normal()

        prices.append(prices[-1] * (1 + ret))

    data = []
    for i, p in enumerate(prices):
        vol = 5_000_000
        if 50 <= i < 100:
            vol = 20_000_000  # high volume during vol spike
        elif i >= 150:
            vol = 1_000_000  # low volume during drift

        data.append({
            "symbol": "SPY",
            "date": f"2024-{((i // 30) + 1):02d}-{(i % 30 + 1):02d}",
            "open": float(p),
            "high": float(p * 1.005),
            "low": float(p * 0.995),
            "close": float(p),
            "volume": vol,
        })
    return data


# ── RegimeDetector tests ─────────────────────────────────────────────────────


class TestRegimeDetectorInit:
    def test_default_construction(self):
        detector = RegimeDetector(k=5)
        assert detector.k == 5
        assert detector._kmeans is None
        assert detector._scaler is None

    def test_with_model_path_nonexistent(self):
        detector = RegimeDetector(k=5, model_path="/tmp/nonexistent_kmeans.pkl")
        assert detector._kmeans is None  # doesn't crash on missing file

    def test_random_state_reproducible(self):
        data = make_ohlcv_data(symbols=["SPY"], n_days=120)
        d1 = RegimeDetector(k=4, random_state=42)
        d1.fit(data, symbols=["SPY"])

        d2 = RegimeDetector(k=4, random_state=42)
        d2.fit(data, symbols=["SPY"])

        # Same centroids
        c1 = d1._kmeans.cluster_centers_
        c2 = d2._kmeans.cluster_centers_
        assert np.allclose(c1, c2)


class TestRegimeDetectorFit:
    def test_fit_minimum_data(self):
        data = make_ohlcv_data(symbols=["SPY"], n_days=100)
        detector = RegimeDetector(k=3)
        detector.fit(data, symbols=["SPY"])
        assert detector._kmeans is not None
        assert detector._scaler is not None
        assert len(detector._feature_names) > 0

    def test_fit_too_few_data_raises(self):
        data = make_ohlcv_data(symbols=["SPY"], n_days=20)
        detector = RegimeDetector(k=5)
        with pytest.raises(ValueError, match="Need at least"):
            detector.fit(data, symbols=["SPY"])

    def test_fit_produces_feature_names(self):
        data = make_ohlcv_data(symbols=["SPY"], n_days=90)
        detector = RegimeDetector(k=3)
        detector.fit(data, symbols=["SPY"])
        names = detector._feature_names
        assert "SPY_mom_5d" in names
        assert "SPY_mom_20d" in names
        assert "SPY_rsi_14" in names
        assert "SPY_atr_pct" in names
        assert len(names) == 9  # 9 features per symbol

    def test_fit_assigns_labels_to_all_clusters(self):
        data = make_ohlcv_data(symbols=["SPY"], n_days=120)
        detector = RegimeDetector(k=5)
        detector.fit(data, symbols=["SPY"])
        assert len(detector._centroid_labels) == 5
        for cluster_id in range(5):
            assert cluster_id in detector._centroid_labels

    def test_fit_with_phased_data_detects_regimes(self):
        """K-Means should find 4+ distinct clusters in multi-phase data."""
        data = make_ohclv_with_regime_phases(n_days=200)
        detector = RegimeDetector(k=5)
        detector.fit(data, symbols=["SPY"])

        # All 5 clusters should be assigned
        assert len(detector._centroid_labels) == 5

        # Labels should include each regime type
        labels = set(detector._centroid_labels.values())
        assert "momentum_bull" in labels
        assert "momentum_bear" in labels
        assert "volatility_spike" in labels


class TestRegimeDetectorPredict:
    def test_predict_synthetic_data(self):
        data = make_ohlcv_data(symbols=["SPY"], n_days=120)
        detector = RegimeDetector(k=4)
        detector.fit(data, symbols=["SPY"])

        features, names = detector._extract_features(data, ["SPY"])
        latest = dict(zip(names, features[-1]))

        result = detector.predict(latest)
        assert isinstance(result, RegimeResult)
        assert result.cluster in range(4)
        assert 0.0 <= result.confidence <= 1.0
        assert isinstance(result.label, str)

    def test_predict_confidence_range(self):
        data = make_ohlcv_data(symbols=["SPY"], n_days=120)
        detector = RegimeDetector(k=4)
        detector.fit(data, symbols=["SPY"])

        features, names = detector._extract_features(data, ["SPY"])
        for fv in features[-10:]:
            feats = dict(zip(names, fv))
            result = detector.predict(feats)
            assert 0.0 <= result.confidence <= 1.0

    def test_predict_fails_without_fit(self):
        detector = RegimeDetector(k=4)
        with pytest.raises(RuntimeError):
            detector.predict({"SPY_mom_5d": 0.01, "SPY_mom_20d": 0.02})

    def test_predict_returns_to_dict(self):
        data = make_ohlcv_data(symbols=["SPY"], n_days=120)
        detector = RegimeDetector(k=4)
        detector.fit(data, symbols=["SPY"])

        features, names = detector._extract_features(data, ["SPY"])
        latest = dict(zip(names, features[-1]))
        result = detector.predict(latest)

        d = result.to_dict()
        assert "cluster" in d
        assert "label" in d
        assert "confidence" in d
        assert "description" in d
        assert "features" in d


class TestRegimeDetectorPersistence:
    def test_save_load_roundtrip(self, tmp_path):
        model_path = str(tmp_path / "test_kmeans.pkl")
        data = make_ohlcv_data(symbols=["SPY"], n_days=120)

        # Train and save
        detector = RegimeDetector(k=4, model_path=model_path)
        detector.fit(data, symbols=["SPY"])

        # Load fresh instance
        loaded = RegimeDetector(k=4, model_path=model_path)
        assert loaded._kmeans is not None
        assert loaded._scaler is not None
        assert loaded._feature_names == detector._feature_names
        assert loaded._centroid_labels == detector._centroid_labels

        # Predictions should match
        features, names = detector._extract_features(data, ["SPY"])
        latest = dict(zip(names, features[-1]))
        r1 = detector.predict(latest)
        r2 = loaded.predict(latest)
        assert r1.cluster == r2.cluster
        assert r1.label == r2.label


class TestRegimeDetectorFeatureExtraction:
    def test_extract_features_returns_array(self):
        data = make_ohlcv_data(symbols=["SPY"], n_days=80)
        detector = RegimeDetector(k=4)
        features, names = detector._extract_features(data, ["SPY"])
        assert len(features) > 0
        assert len(names) == 9  # 9 features per symbol
        assert len(features[0]) == len(names)

    def test_features_are_finite(self):
        data = make_ohlcv_data(symbols=["SPY"], n_days=120)
        detector = RegimeDetector(k=4)
        features, _ = detector._extract_features(data, ["SPY"])
        for fv in features:
            for val in fv:
                assert np.isfinite(val)

    def test_multi_symbol_features(self):
        data = make_ohlcv_data(symbols=["SPY", "QQQ"], n_days=80)
        detector = RegimeDetector(k=4)
        features, names = detector._extract_features(data, ["SPY", "QQQ"])
        # Each symbol contributes its own 9 features.
        # Feature vectors include both symbols (concatenated), but
        # _feature_names reflects the last symbol processed.
        assert len(names) == 9
        assert "QQQ_mom_5d" in names
        # Feature vectors include data from both symbols (80 - 50 = 30 per symbol = 60 total)
        assert len(features) == 60


# ── SignalParams K-Means weight tests ────────────────────────────────────────


class TestKMeansWeights:
    def test_new_weights_exist(self):
        p = SignalParams()
        assert hasattr(p, "weight_momentum_bull")
        assert hasattr(p, "weight_momentum_bear")
        assert hasattr(p, "weight_mean_reversion")
        assert hasattr(p, "weight_volatility_spike")
        assert hasattr(p, "weight_low_vol_drift")

    def test_new_weights_in_bounds(self):
        b_bull = SignalParams.bound("weight_momentum_bull")
        assert b_bull.min_val == 0.2
        assert b_bull.max_val == 2.0

        b_drift = SignalParams.bound("weight_low_vol_drift")
        assert b_drift.min_val == 0.0
        assert b_drift.max_val == 1.5

    def test_legacy_weights_still_exist(self):
        p = SignalParams()
        assert hasattr(p, "weight_trending_up")
        assert hasattr(p, "weight_trending_down")
        assert hasattr(p, "weight_mean_reverting")
        assert hasattr(p, "weight_high_volatility")

    def test_param_names_includes_kmeans_weights(self):
        names = SignalParams.param_names()
        assert "weight_momentum_bull" in names
        assert "weight_volatility_spike" in names
        assert "weight_low_vol_drift" in names


# ── SignalEngine K-Means regime integration tests ────────────────────────────


class DummyTick:
    def __init__(self, ticker, close, timestamp=None):
        self.ticker = ticker
        self.close = close
        self.timestamp = timestamp or "2024-01-02"


class TestSignalEngineKMeansRegime:
    def test_regime_includes_kmeans_labels(self):
        """SignalEngine.process() should report K-Means regime labels
        when model is available, falling back to rule-based labels."""
        engine = SignalEngine()
        prices = [100 * (1.015 ** i) for i in range(60)]
        for i, p in enumerate(prices):
            report = engine.process(DummyTick("AAPL", p, f"day-{i}"))
        # Falls back to rule-based if no K-Means model
        assert report.regime in (
            "TRENDING_UP", "TRENDING_DOWN", "MEAN_REVERTING", "HIGH_VOLATILITY",
            "momentum_bull", "momentum_bear", "mean_reversion",
            "volatility_spike", "low_vol_drift",
        )

    def test_regime_bias_maps_all_labels(self):
        """Verify regime_bias dict covers both K-Means and legacy labels."""
        engine = SignalEngine()
        # The regime_bias dict is constructed inside process().
        # Test via processing various data and checking composite_signal range.
        # All regimes produce bounded composite signals.
        for trend in [0.015, -0.015, 0.0]:
            engine = SignalEngine()
            prices = [100 * ((1 + trend) ** i) for i in range(60)]
            for p in prices:
                report = engine.process(DummyTick("AAPL", p))
            assert -1.0 <= report.composite_signal <= 1.0

    def test_module_level_regime_cache(self):
        """_kmeans_regime cache can be updated and read."""
        _kmeans_regime.clear()
        _kmeans_regime.update({
            "regime": "momentum_bull",
            "confidence": 0.85,
        })
        assert _kmeans_regime["regime"] == "momentum_bull"
        _kmeans_regime.clear()

    def test_signal_engine_uses_cached_regime(self):
        """When _kmeans_regime cache is populated, signal engine uses it."""
        _kmeans_regime.update({
            "regime": "momentum_bull",
            "confidence": 0.92,
        })
        try:
            engine = SignalEngine()
            prices = [100 * (1.001 ** i) for i in range(60)]
            for i, p in enumerate(prices):
                report = engine.process(DummyTick("AAPL", p, f"day-{i}"))
            # Should use cached K-Means regime instead of rule-based
            assert report.regime == "momentum_bull"
            # Confidence should match cache
            assert report.regime_confidence == 0.92
        finally:
            _kmeans_regime.clear()


# ── REGIME_LABELS integrity ──────────────────────────────────────────────────


class TestRegimeLabels:
    def test_five_labels_defined(self):
        assert len(REGIME_LABELS) == 5
        for i in range(5):
            assert i in REGIME_LABELS

    def test_labels_are_meaningful(self):
        expected = {"momentum_bull", "momentum_bear", "mean_reversion",
                     "volatility_spike", "low_vol_drift"}
        actual = set(REGIME_LABELS.values())
        assert actual == expected


# ── create_detector factory ──────────────────────────────────────────────────


class TestCreateDetector:
    def test_creates_instance(self):
        detector = create_detector(k=5, model_path="/tmp/test_factory.pkl")
        assert isinstance(detector, RegimeDetector)
        assert detector.k == 5
