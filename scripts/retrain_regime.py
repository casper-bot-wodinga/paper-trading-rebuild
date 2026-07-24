#!/usr/bin/env python3
"""Retrain the SPY market-regime HMM used by get_market_regime.

Steps:
  1. Refresh SPY bars via backfill_bars_alpaca.py --tickers core --days 30
  2. Load shared/cache/bars/SPY.parquet
  3. Train via src.ml_signal.retrain_hmm — uploads training data to the GPU
     worker via gRPC (upload_file), submits a real TrainJob, and the actual
     GaussianHMM.fit() runs on the Mac (legend-of-macs.local:5002), not here.
     This VM has no GPU; that's the whole reason the worker exists.
  4. Do a local shadow-fit — same features, same random_state=42 — purely to
     inspect which state index has the higher mean 'returns' (GaussianHMM.fit()
     assigns state indices arbitrarily, not by semantic meaning, and the real
     fitted model stays on the Mac / never comes back over the wire, so there's
     no other way to read its means_ from here). This is NOT the served model —
     it's a small diagnostic fit only used to write sustainable_state to
     ~/.openclaw/gpu-models/scalers/hmm_SPY_meta.json, read back by
     src.ml_signal.get_regime().

Requires the GPU worker (~/projects/gpu-compute on the Mac) to be running the
casper/grpc-phase-1 branch or later — earlier deploys are missing the
UploadFile RPC (confirmed 2026-07-24) and step 3 will fail with
StatusCode.UNIMPLEMENTED.

Meant to run weekly (stonks-regime-retrain cron) — regime dynamics don't
shift fast enough to need daily retraining, and this is a real ~600s
GPU-worker job, not a cheap call.

Usage:
    python3 scripts/retrain_regime.py
    python3 scripts/retrain_regime.py --symbol SPY --days 30
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
BACKFILL_SCRIPT = PROJECT_DIR / "scripts" / "backfill_bars_alpaca.py"
BARS_DIR = PROJECT_DIR / "shared" / "cache" / "bars"

sys.path.insert(0, str(PROJECT_DIR))
load_dotenv(PROJECT_DIR / ".env")

from src import ml_signal  # noqa: E402


def refresh_bars(days: int, verbose: bool) -> int:
    """Shell out to the existing Alpaca backfill script (same subprocess
    pattern as data_bus.py's _run_hmm_retrain and Stan's sync_historical_bars.py)."""
    cmd = [sys.executable, str(BACKFILL_SCRIPT), "--tickers", "core", "--days", str(days)]
    if verbose:
        cmd.append("--verbose")

    # backfill_bars_alpaca.py wants generic APCA_API_KEY_ID/APCA_API_SECRET_KEY;
    # this repo's .env only has the per-account ALPACA_STONKS_KEY/_SECRET. Market
    # data isn't account-scoped, so reuse Stan's paper creds.
    env = os.environ.copy()
    if not env.get("APCA_API_KEY_ID") and env.get("ALPACA_STONKS_KEY"):
        env["APCA_API_KEY_ID"] = env["ALPACA_STONKS_KEY"]
    if not env.get("APCA_API_SECRET_KEY") and env.get("ALPACA_STONKS_SECRET"):
        env["APCA_API_SECRET_KEY"] = env["ALPACA_STONKS_SECRET"]

    result = subprocess.run(cmd, cwd=str(PROJECT_DIR), env=env)
    return result.returncode


def determine_sustainable_state(ohlcv_df, n_components: int, n_iter: int) -> int:
    """Local shadow-fit (same features/random_state as the worker's training
    job) purely to inspect which state has the higher mean 'returns' — that's
    the SUSTAINABLE state. Not the served model; the real one is trained on
    the Mac in retrain_hmm() and never comes back over the wire."""
    import numpy as np
    from hmmlearn.hmm import GaussianHMM
    from sklearn.preprocessing import StandardScaler

    X_raw, _ = ml_signal._extract_features(ohlcv_df)
    cols = ["rsi", "rsi_trend", "macd_diff", "volume_trend", "price_velocity", "returns", "volatility"]
    returns_idx = cols.index("returns")

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(np.array(X_raw))

    model = GaussianHMM(n_components=n_components, covariance_type="diag", n_iter=n_iter, random_state=42)
    model.fit(X_scaled, [len(X_scaled)])

    means_by_state = model.means_[:, returns_idx]
    return int(np.argmax(means_by_state))


async def main_async(symbol: str, days: int, n_components: int, n_iter: int, verbose: bool) -> int:
    print(f"[1/3] Refreshing {symbol} bars ({days}d)...")
    rc = refresh_bars(days, verbose)
    if rc != 0:
        print(f"ERROR: bar refresh failed (exit {rc})", file=sys.stderr)
        return 1

    parquet_path = BARS_DIR / f"{symbol}.parquet"
    if not parquet_path.exists():
        print(f"ERROR: {parquet_path} not found after refresh", file=sys.stderr)
        return 1

    import pandas as pd
    df = pd.read_parquet(parquet_path)
    print(f"[2/3] Loaded {len(df)} bars, training HMM on the GPU worker (n_components={n_components}, n_iter={n_iter})...")

    result = await ml_signal.retrain_hmm(symbol, df, n_components=n_components, n_iter=n_iter)
    if "error" in result:
        print(f"ERROR: retrain_hmm failed: {result['error']}", file=sys.stderr)
        return 1
    print(f"  Trained on Mac: {result}")

    print("[3/3] Determining sustainable state via local shadow-fit...")
    sustainable_state = determine_sustainable_state(df, n_components, n_iter)
    meta_path = ml_signal._meta_path(symbol)
    meta_path.write_text(json.dumps({"sustainable_state": sustainable_state}))
    print(f"  sustainable_state={sustainable_state} -> {meta_path}")

    print("Done.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--n-components", type=int, default=2)
    parser.add_argument("--n-iter", type=int, default=1500)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    return asyncio.run(main_async(args.symbol, args.days, args.n_components, args.n_iter, args.verbose))


if __name__ == "__main__":
    sys.exit(main())
