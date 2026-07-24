"""Tests for src/ml_signal.py's pure feature-extraction/labeling logic.

No real gRPC connection or GPU worker here — that's exercised manually
against the live worker (see scripts/retrain_regime.py), not in CI. Just
the deterministic feature math and state-labeling helpers, same style as
tests/test_gpu_client.py."""
import asyncio
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ml_signal  # noqa: E402


def _synthetic_ohlcv(n=60, seed=0):
    """A plausible-looking OHLCV series — enough rows to survive the
    rolling(20)/pct_change(5) warmup in _extract_features."""
    rng = np.random.default_rng(seed)
    closes = 100 + np.cumsum(rng.normal(0, 0.5, n))
    opens = closes + rng.normal(0, 0.1, n)
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0, 0.2, n))
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0, 0.2, n))
    volumes = rng.integers(1000, 5000, n).astype(float)
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes})


class TestExtractFeatures:
    def test_returns_expected_columns_and_shapes(self):
        df = _synthetic_ohlcv()
        X, details = ml_signal._extract_features(df)
        assert len(X) > 0
        assert all(len(row) == 7 for row in X)
        expected_cols = {"rsi", "rsi_trend", "macd_diff", "volume_trend", "price_velocity", "returns", "volatility"}
        assert set(details.keys()) == expected_cols

    def test_drops_warmup_rows_with_nans(self):
        df = _synthetic_ohlcv(n=60)
        X, _ = ml_signal._extract_features(df)
        # rolling(20) + diff warmup means we lose at least ~20 rows
        assert len(X) < len(df)
        assert all(all(np.isfinite(v) for v in row) for row in X)

    def test_too_few_rows_yields_no_features(self):
        df = _synthetic_ohlcv(n=5)
        X, _ = ml_signal._extract_features(df)
        assert X == []


class TestScale:
    def test_scale_matches_sklearn_transform(self):
        from sklearn.preprocessing import StandardScaler
        X = [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]
        scaler = StandardScaler().fit(X)
        scaled = ml_signal._scale(X, scaler)
        assert np.allclose(scaled, scaler.transform(np.array(X)))


class TestSubClassify:
    def test_exhausted_when_rsi_high_and_falling_and_returns_negative(self):
        details = {"rsi": 75, "rsi_trend": -1.0, "returns": -0.5}
        assert ml_signal._sub_classify(details) == "EXHAUSTED"

    def test_choppy_when_rsi_not_overbought(self):
        details = {"rsi": 45, "rsi_trend": -1.0, "returns": -0.5}
        assert ml_signal._sub_classify(details) == "CHOPPY"

    def test_choppy_when_rsi_trend_not_falling(self):
        details = {"rsi": 75, "rsi_trend": 0.2, "returns": -0.5}
        assert ml_signal._sub_classify(details) == "CHOPPY"

    def test_choppy_on_missing_details(self):
        assert ml_signal._sub_classify({}) == "CHOPPY"


class TestSustainableStateMeta:
    def test_defaults_to_zero_when_meta_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_SCALER_DIR", tmp_path)
        assert ml_signal._load_sustainable_state("NOPE") == 0

    def test_reads_persisted_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_SCALER_DIR", tmp_path)
        meta_path = ml_signal._meta_path("SPY")
        meta_path.write_text(json.dumps({"sustainable_state": 1}))
        assert ml_signal._load_sustainable_state("SPY") == 1

    def test_falls_back_to_zero_on_corrupt_meta(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_SCALER_DIR", tmp_path)
        meta_path = ml_signal._meta_path("SPY")
        meta_path.write_text("not json")
        assert ml_signal._load_sustainable_state("SPY") == 0


class TestGetRegimeNoScaler:
    def test_returns_error_when_scaler_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_signal, "_SCALER_DIR", tmp_path)
        df = _synthetic_ohlcv()
        result = asyncio.run(ml_signal.get_regime("NOPE", df))
        assert result["source"] == "error"
        assert "No scaler" in result["error"]
