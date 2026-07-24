"""
ml_signal.py — gRPC-backed HMM regime signal for trader agents.

Ported 2026-07-24 from github.com/casper-bot-wodinga/gpu-compute
(orchestrator/ml_signal.py) — real, working infrastructure that was never
wired into the v4 rebuild. Wraps GpuClient with feature extraction (same
7-feature set as MomentumRegimeDetector), scaler management (local), and
3-state output post-processing.

Usage:
    from src.ml_signal import retrain_hmm, get_regime

    # Train (once, or on fresh market data)
    result = await retrain_hmm("SPY", ohlcv_df)

    # Infer (each tick)
    signal = await get_regime("SPY", ohlcv_df)
    # {"regime": "SUSTAINABLE", "confidence": 0.71, "details": {...}, "source": "grpc"}
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import tempfile
from pathlib import Path

logger = logging.getLogger("ml_signal")

# Scalers live locally so inference doesn't need a round-trip model download
_SCALER_DIR = Path.home() / ".openclaw" / "gpu-models" / "scalers"


# ------------------------------------------------------------------
# Feature extraction  (mirrors MomentumRegimeDetector.calculate_features)
# ------------------------------------------------------------------

def _extract_features(df) -> "tuple[list[list[float]], dict]":
    """
    Extract 7 technical features from an OHLCV DataFrame.
    Expects columns: open, high, low, close, volume.
    Returns (feature_rows, last_bar_details).
    """
    df = df.copy()

    # RSI-14
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss))
    df["rsi_trend"] = df["rsi"].diff()

    # MACD 12/26/9
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    df["macd_diff"] = macd - macd.ewm(span=9, adjust=False).mean()

    # Volume trend
    vol_ma = df["volume"].rolling(20).mean()
    df["volume_trend"] = (df["volume"] - vol_ma) / vol_ma

    # Price velocity & returns
    df["returns"] = df["close"].pct_change() * 100
    df["price_velocity"] = df["close"].pct_change(5) * 100

    # Volatility
    df["volatility"] = df["returns"].rolling(20).std()

    df = df.dropna()

    cols = ["rsi", "rsi_trend", "macd_diff", "volume_trend", "price_velocity", "returns", "volatility"]
    if df.empty:
        return [], {}

    X = df[cols].values.tolist()
    last = df.iloc[-1]
    details = {c: float(last[c]) for c in cols}

    return X, details


def _scale(X: list, scaler) -> list:
    import numpy as np
    return scaler.transform(np.array(X)).tolist()


def _sub_classify(details: dict) -> str:
    """CHOPPY vs EXHAUSTED heuristic (same as MomentumRegimeDetector)."""
    if details.get("rsi", 50) > 60 and details.get("rsi_trend", 0) < -0.3 and details.get("returns", 0) < 0:
        return "EXHAUSTED"
    return "CHOPPY"


def _scaler_path(symbol: str) -> Path:
    _SCALER_DIR.mkdir(parents=True, exist_ok=True)
    return _SCALER_DIR / f"hmm_{symbol}_scaler.pkl"


def _meta_path(symbol: str) -> Path:
    _SCALER_DIR.mkdir(parents=True, exist_ok=True)
    return _SCALER_DIR / f"hmm_{symbol}_meta.json"


def _load_sustainable_state(symbol: str) -> int:
    """Which HMM state index means SUSTAINABLE for this symbol's model.

    GaussianHMM.fit() assigns state indices arbitrarily based on the
    training data, not by semantic meaning — there's no guarantee state 0
    is the "good" state. retrain_regime.py determines this via a local
    shadow-fit after training and writes it here; default to 0 (old
    behavior) if the meta file is missing so this degrades safely rather
    than erroring.
    """
    mp = _meta_path(symbol)
    if not mp.exists():
        return 0
    try:
        return int(json.loads(mp.read_text()).get("sustainable_state", 0))
    except Exception:
        return 0


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

async def retrain_hmm(
    symbol: str,
    ohlcv_df,
    n_components: int = 2,
    n_iter: int = 1500,
    client=None,
) -> dict:
    """
    Retrain the HMM for a given symbol on fresh OHLCV data.

    Steps:
      1. Extract 7 features from ohlcv_df
      2. Fit a new StandardScaler, save locally
      3. Scale features, upload JSON to the GPU worker via gRPC
      4. Submit train job, wait for completion
      5. Return result dict

    Args:
        symbol:       Ticker symbol (e.g. "SPY")
        ohlcv_df:     pandas DataFrame with columns: open, high, low, close, volume
        n_components: HMM hidden states (default 2)
        n_iter:       HMM training iterations (default 1500)
        client:       Optional pre-connected GpuClient

    Returns:
        {"symbol": ..., "converged": bool, "n_components": int, "artifact_path": str}
    """
    from sklearn.preprocessing import StandardScaler
    import numpy as np

    _pool = None
    _close = client is None
    if _close:
        from src.gpu_client import WorkerPool
        _pool = WorkerPool.from_env()
        client = await _pool.pick()  # pin to one worker for the whole train sequence
        if client is None:
            return {"error": "no healthy workers available"}

    try:
        X_raw, _ = _extract_features(ohlcv_df)

        # Fit scaler and save locally
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(np.array(X_raw)).tolist()
        sp = _scaler_path(symbol)
        with open(sp, "wb") as f:
            pickle.dump(scaler, f)
        logger.info("Scaler saved to %s", sp)

        # Write scaled training data to temp file
        data = {"features": X_scaled, "lengths": [len(X_scaled)]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            json.dump(data, tmp)
            tmp_path = tmp.name

        # Upload to the worker (use the pinned client, not the pool)
        up_resp = await client.upload_file(tmp_path, staging_subdir="train")
        os.unlink(tmp_path)
        if up_resp is None or not up_resp.ok:
            return {"error": f"upload failed: {getattr(up_resp, 'error', 'no response')}"}
        remote_path = up_resp.stored_path

        # Submit training job
        job_id = await client.submit_train(
            model_type="hmm",
            symbol=symbol,
            data_path=remote_path,
            n_components=n_components,
            n_iter=n_iter,
            write_reload_flag=True,
        )
        if job_id is None:
            return {"error": "submit_train returned None"}

        logger.info("Train job %s submitted (symbol=%s)", job_id, symbol)
        result = await client.wait_for_job(job_id, timeout=600.0)

        if result is None:
            return {"error": "train job timed out"}
        if result.phase == 3:  # FAILED
            return {"error": result.error}

        out = json.loads(result.result_json) if result.result_json else {}
        out["scaler_path"] = str(sp)
        logger.info("Training complete: %s", out)
        return out

    finally:
        if _pool is not None:
            await _pool.close()


async def get_regime(
    symbol: str,
    ohlcv_df,
    client=None,
) -> dict:
    """
    Get the current momentum regime for a symbol via gRPC inference.

    Requires retrain_hmm() to have been called at least once for this symbol
    (scaler must exist locally, HMM model must exist on the worker).

    Returns:
        {
            "regime": "SUSTAINABLE" | "EXHAUSTED" | "CHOPPY",
            "confidence": 0.0-1.0,
            "details": {rsi, rsi_trend, macd_diff, ...},
            "source": "grpc",
        }
    """
    sp = _scaler_path(symbol)
    if not sp.exists():
        return {
            "regime": "CHOPPY",
            "confidence": 0.0,
            "details": {},
            "source": "error",
            "error": f"No scaler for {symbol} — run retrain_hmm first",
        }

    with open(sp, "rb") as f:
        scaler = pickle.load(f)

    X_raw, details = _extract_features(ohlcv_df)
    X_scaled = _scale(X_raw, scaler)
    sustainable_state = _load_sustainable_state(symbol)

    _pool = None
    _close = client is None
    if _close:
        from src.gpu_client import WorkerPool
        _pool = WorkerPool.from_env()
        client = await _pool.pick()
        if client is None:
            return {"regime": "CHOPPY", "confidence": 0.0, "details": details, "source": "error", "error": "no healthy workers"}

    try:
        job_id = await client.submit_infer(
            model_name=f"hmm_{symbol}",
            features=X_scaled,
        )
        if job_id is None:
            return {"regime": "CHOPPY", "confidence": 0.0, "details": details, "source": "error", "error": "submit_infer returned None"}

        result = await client.wait_for_job(job_id, timeout=30.0)
        if result is None or result.phase == 3:
            err = getattr(result, "error", "timeout") if result else "timeout"
            return {"regime": "CHOPPY", "confidence": 0.0, "details": details, "source": "error", "error": err}

        out = json.loads(result.result_json)
        last_state = out.get("state", -1)
        log_score = out.get("log_score", 0.0)

        hmm_regime = "SUSTAINABLE" if last_state == sustainable_state else "NOT_SUSTAINABLE"

        # Moderate confidence using log score magnitude
        raw_conf = min(0.92, abs(log_score) / (abs(log_score) + 1))
        confidence = round(raw_conf, 3)

        if hmm_regime == "SUSTAINABLE":
            final_regime = "SUSTAINABLE"
        else:
            final_regime = _sub_classify(details)
            confidence = round(confidence * 0.7, 3)  # heuristic sub-classification haircut

        return {
            "regime": final_regime,
            "confidence": confidence,
            "details": details,
            "source": "grpc",
            "hmm_state": last_state,
            "log_score": log_score,
        }

    finally:
        if _pool is not None:
            await _pool.close()
