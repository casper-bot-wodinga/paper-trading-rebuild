#!/usr/bin/env python3
"""Virtual Trader Culling — weekly cleanup and regeneration.

Runs Sunday 23:00 ET.
  1. Rank all virtuals by 7-day rolling P&L (realized + unrealized)
  2. Cull bottom 3 per base trader (mark status='culled', set culled_at)
  3. Generate 3 new variants, sourcing at least one from prompt_sweep results:
     a. 1 sweep-sourced variant (from prompt_sweep results or DB probation pool)
     b. 1 param variant: random perturbation of the #1 virtual's params
     c. 1 wildcard: random new config within safe bounds
  4. New virtuals get status='probation' (2-day wait before promotion eligible)
  5. When sweep results are stale (>7 days) or unavailable, fall back to random
     generation (variant_type='random').
  6. Errors in the sweep-pulling path don't crash the cull cycle.

Usage:
    python3 src/virtual_cull.py                    # run once (for cron)
    python3 src/virtual_cull.py --dry-run           # print what would happen
    python3 src/virtual_cull.py --once              # run once (default, for testing)
    python3 src/virtual_cull.py --cull-count 2      # cull only 2 instead of 3
    python3 src/virtual_cull.py --base kairos       # only cull one base trader
    python3 src/virtual_cull.py --force-random      # skip sweep lookup, use random only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.signals import SignalParams

log = logging.getLogger("virtual_cull")

# ═══════════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════════

DB_DSN = os.getenv("VT_DB_DSN", "host=docker.klo port=5433 dbname=trading user=trader")
BASE_TRADERS = ["kairos", "aldridge", "stonks"]

DEFAULT_CULL_COUNT = 3
P7D_LOOKBACK_DAYS = 7
PROBATION_DAYS = 2

# Perturbation factor when applying sweep best params (±5% of range)
SWEEP_PERTURBATION_PCT = 0.05
# Sweep results config
SWEEP_RESULTS_DIR = str(
    Path(__file__).resolve().parent.parent / "results"
)
SWEEP_MAX_AGE_DAYS = 7
SHORT_NAMES = {
    "trader-kairos": "kairos",
    "trader-aldridge": "aldridge",
    "trader-stonks": "stonks",
}
# Reverse mapping: short name -> sweep results trader key
_TRAIDER_TO_SHORT = {
    "kairos": "kairos",
    "aldridge": "aldridge",
    "stonks": "stonks",
}

# Mapping from seed-style dot-notation config keys to flat SignalParams names
_CONFIG_KEY_MAP: Dict[str, str] = {
    "signal_params.momentum.threshold": "momentum_threshold",
    "signal_params.mean_reversion.rsi_oversold": "rsi_oversold",
    "signal_params.mean_reversion.rsi_overbought": "rsi_overbought",
    "signal_params.mean_reversion.bollinger_std": "bollinger_std",
    "signal_params.volume.threshold": "volume_threshold",
    "signal_params.volatility.regime_threshold": "vol_regime_threshold",
    "signal_params.volatility.reduction_multiplier": "vol_reduction_multiplier",
    "signal_params.position_sizing.base_size_pct": "base_size_pct",
    "signal_params.position_sizing.conviction_multiplier": "conviction_multiplier",
    "signal_params.position_sizing.max_positions": "max_positions",
    "signal_params.risk.stop_loss_pct": "stop_loss_pct",
    "signal_params.risk.take_profit_pct": "take_profit_pct",
    "signal_params.risk.trailing_stop_pct": "trailing_stop_pct",
    "signal_params.regime_weights.trending_up": "weight_trending_up",
    "signal_params.regime_weights.trending_down": "weight_trending_down",
    "signal_params.regime_weights.mean_reverting": "weight_mean_reverting",
    "signal_params.regime_weights.high_volatility": "weight_high_volatility",
}


# ═══════════════════════════════════════════════════════════════════════════════
# Database helpers
# ═══════════════════════════════════════════════════════════════════════════════

def get_db():
    """Return a psycopg2 connection with autocommit."""
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    return conn


# ═══════════════════════════════════════════════════════════════════════════════
# P&L computation (consistent with virtual_rotate.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _latest_close_prices(tickers: List[str], as_of: date) -> Dict[str, float]:
    """Get the most recent closing price for each ticker from market_data.bars."""
    if not tickers:
        return {}

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    prices: Dict[str, float] = {}
    for ticker in set(tickers):
        cur.execute(
            """SELECT close FROM market_data.bars
               WHERE ticker = %s AND timestamp <= %s::timestamp + interval '1 day'
               ORDER BY timestamp DESC LIMIT 1""",
            (ticker, as_of),
        )
        row = cur.fetchone()
        if row:
            prices[ticker] = float(row["close"])
    conn.close()
    return prices


def compute_7day_pnl(trader_ids: List[str]) -> Dict[str, float]:
    """Compute 7-day rolling net P&L: realized + mark-to-market unrealized."""
    since = date.today() - timedelta(days=P7D_LOOKBACK_DAYS)
    today = date.today()

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ── Realized P&L: closed within the window ──
    placeholders = ",".join(["%s"] * len(trader_ids))
    cur.execute(
        f"""SELECT trader_id, COALESCE(SUM(pnl), 0) as realized_pnl
            FROM trading.trades
            WHERE trader_id IN ({placeholders})
              AND exit_time IS NOT NULL
              AND exit_time::date >= %s
            GROUP BY trader_id""",
        (*trader_ids, since),
    )
    realized_map = {row["trader_id"]: float(row["realized_pnl"]) for row in cur.fetchall()}

    # ── Unrealized P&L: open positions marked to market ──
    cur.execute(
        f"""SELECT trader_id, ticker, SUM(shares) as total_shares,
                   AVG(entry_price) as avg_entry
            FROM trading.trades
            WHERE trader_id IN ({placeholders})
              AND exit_time IS NULL
            GROUP BY trader_id, ticker
            HAVING SUM(shares) != 0""",
        (*trader_ids,),
    )
    open_positions = cur.fetchall()

    open_tickers = list({p["ticker"] for p in open_positions})
    close_prices = _latest_close_prices(open_tickers, today)

    unrealized_map: Dict[str, float] = defaultdict(float)
    for pos in open_positions:
        trader = pos["trader_id"]
        ticker = pos["ticker"]
        shares = int(pos["total_shares"])
        entry = float(pos["avg_entry"])
        close = close_prices.get(ticker, entry)
        unrealized_map[trader] += (close - entry) * shares

    conn.close()

    # Combine
    pnl_map: Dict[str, float] = {}
    for tid in trader_ids:
        realized = realized_map.get(tid, 0.0)
        unrealized = unrealized_map.get(tid, 0.0)
        pnl_map[tid] = realized + unrealized

    return pnl_map


# ═══════════════════════════════════════════════════════════════════════════════
# Config normalization — handles both seed dot-notation and flat param names
# ═══════════════════════════════════════════════════════════════════════════════

def normalize_config(raw_config: Dict[str, Any]) -> Dict[str, float]:
    """Convert a stored config (seed dot-notation or flat names) to flat param dict.

    Supports both formats:
      - Seed format:  {"signal_params.momentum.threshold": 0.35, ...}
      - Flat format:  {"momentum_threshold": 0.35, ...}

    Only returns keys that match known SignalParams names.
    """
    result: Dict[str, float] = {}
    for key, value in raw_config.items():
        try:
            fval = float(value)
        except (TypeError, ValueError):
            continue

        # Try flat name first
        if key in SignalParams.param_names():
            result[key] = fval
        # Try dot-notation mapping
        elif key in _CONFIG_KEY_MAP:
            result[_CONFIG_KEY_MAP[key]] = fval

    return result


def get_param_value(config: Dict[str, Any], param_name: str) -> Optional[float]:
    """Extract a single SignalParams value from config dict, handling both formats."""
    # Flat name
    if param_name in config:
        try:
            return float(config[param_name])
        except (TypeError, ValueError):
            pass

    # Dot notation — find the reverse mapping
    for dot_key, flat_name in _CONFIG_KEY_MAP.items():
        if flat_name == param_name and dot_key in config:
            try:
                return float(config[dot_key])
            except (TypeError, ValueError):
                pass

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Sweep variant pulling — find recent prompt_sweep results to use as replacements
# ═══════════════════════════════════════════════════════════════════════════════

def _find_sweep_results_files(results_dir: str = SWEEP_RESULTS_DIR) -> List[Path]:
    """Find all sweep results JSON files (non-empty, valid JSON) in results_dir.

    Returns paths sorted by modification time (newest first).
    """
    dir_path = Path(results_dir)
    if not dir_path.exists():
        log.debug("  Sweep results dir %s does not exist", results_dir)
        return []

    json_files = sorted(
        dir_path.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return json_files


def _is_results_fresh(file_path: Path, max_age_days: int = SWEEP_MAX_AGE_DAYS) -> bool:
    """Check if a results file is recent enough to use.

    Uses the file's modification time. Returns True if modified within
    max_age_days of today.
    """
    mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
    age = datetime.now(timezone.utc) - mtime
    return age.days < max_age_days


def _read_sweep_results_json(file_path: Path) -> Optional[Dict[str, Any]]:
    """Parse a sweep results JSON file.

    Expected format (from prompt_sweep.py / promote_sweep_winner.py):
    {
        "sweep_date": "2026-07-10",
        "results": [
            {
                "trader": "kairos",
                "date": "2026-07-10",
                "baseline_score": 0.123,
                "variants": [
                    {
                        "variant_name": "wider_stops",
                        "score": 0.456,
                        "signal_params": {"momentum_threshold": 0.3, ...}
                    }
                ],
                "winner": {"variant_name": "wider_stops", ...}
            }
        ]
    }

    Returns the parsed dict, or None on failure.
    """
    try:
        with open(file_path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log.warning("  Sweep results file %s is not a JSON object", file_path)
            return None
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("  Could not read sweep results file %s: %s", file_path, e)
        return None


def _extract_sweep_variants_for_trader(
    results_data: Dict[str, Any],
    base_trader: str,
) -> List[Dict[str, Any]]:
    """Extract scored sweep variants for a given base trader from sweep results.

    Looks for the trader in the sweep results data and returns all variants
    (including the winner) that have signal_params. Results are sorted by
    score descending.

    Returns:
        List of dicts with keys:
            variant_name, score, signal_params, config (flat param dict)
    """
    sweep_results = results_data.get("results", [])
    sweep_date = results_data.get("sweep_date", results_data.get("date", "unknown"))

    trader_entry = None
    for sr in sweep_results:
        # The trader key can be a short name ("kairos") or long ("trader-kairos")
        trader_key = sr.get("trader", "")
        if trader_key == base_trader or trader_key == f"trader-{base_trader}":
            trader_entry = sr
            break

    if trader_entry is None:
        log.debug("  No sweep results for trader %s in %s", base_trader, sweep_date)
        return []

    variants = trader_entry.get("variants", [])
    if not variants:
        log.debug("  No variants in sweep results for trader %s", base_trader)
        return []

    extracted: List[Dict[str, Any]] = []
    for v in variants:
        signal_params = v.get("signal_params", {})
        if not signal_params or not isinstance(signal_params, dict):
            continue

        variant_name = v.get("variant_name", "unknown")
        score = v.get("score", 0.0)

        # Build flat config from signal_params
        config: Dict[str, float] = {}
        for name in SignalParams.param_names():
            if name in signal_params:
                try:
                    config[name] = float(signal_params[name])
                except (TypeError, ValueError):
                    pass

        if config:
            extracted.append({
                "variant_name": variant_name,
                "score": score,
                "signal_params": signal_params,
                "config": config,
            })

    # Sort by score descending
    extracted.sort(key=lambda v: v["score"], reverse=True)
    return extracted


def _find_probationary_sweep_virtuals(base_trader: str) -> List[Dict[str, Any]]:
    """Find probationary from_sweep virtuals in the DB for this base trader.

    Returns:
        List of dicts with keys: name, config, created_at, score (from config).
        Empty list if none found.
    """
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            """SELECT name, config, created_at
               FROM trading.virtual_traders
               WHERE base_trader = %s
                 AND variant_type = 'from_sweep'
                 AND status = 'probation'
                 AND created_at >= %s
               ORDER BY created_at DESC""",
            (base_trader, date.today() - timedelta(days=SWEEP_MAX_AGE_DAYS)),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        log.debug("  No probationary from_sweep virtuals for %s", base_trader)
        return []

    extracted: List[Dict[str, Any]] = []
    for row in rows:
        raw_config = row.get("config", {})
        if isinstance(raw_config, str):
            try:
                raw_config = json.loads(raw_config)
            except json.JSONDecodeError:
                continue
        if not isinstance(raw_config, dict):
            continue

        # Normalize config to flat params
        config = normalize_config(raw_config)

        extracted.append({
            "name": row["name"],
            "config": config,
            "created_at": row["created_at"],
        })

    return extracted


def _make_sweep_variant_name(
    base_trader: str,
    variant_name: str,
    sweep_date: str,
) -> str:
    """Generate a unique virtual trader name for a sweep-sourced variant.

    Format: {base_trader}-sweep-{variant_name}-{date}
    Example: kairos-sweep-wider-stops-20260710
    """
    safe_variant = variant_name.replace("_", "-").lower()
    safe_date = sweep_date.replace("-", "")
    return f"{base_trader}-sweep-{safe_variant}-{safe_date}"


def _pull_sweep_variants_for_replacement(
    base_trader: str,
) -> List[Dict[str, Any]]:
    """Pull prompt_sweep variants for use as culling replacements.

    Priority:
      1. Probationary virtuals in DB with variant_type='from_sweep' (recent)
      2. Sweep results JSON files in results/ (recent, not yet promoted)

    Returns:
        List of dicts with keys: name, config, variant_name (for logging),
        score.
        Each entry can be used as a replacement virtual trader. Empty list
        if no fresh sweep results are available.
    """
    variants: List[Dict[str, Any]] = []

    # Priority 1: Check DB for probationary from_sweep virtuals
    try:
        db_virtuals = _find_probationary_sweep_virtuals(base_trader)
        for v in db_virtuals:
            variants.append({
                "name": v["name"],
                "config": v["config"],
                "variant_name": v["name"],
                "score": 0.0,  # Score not stored in DB config
                "source": "db_probation",
            })
        if variants:
            log.info(
                "  Found %d probationary from_sweep virtuals for %s",
                len(variants), base_trader,
            )
    except Exception as e:
        log.warning("  Error probing DB for sweep virtuals: %s", e)

    # Priority 2: Check results/ for sweep JSON files
    try:
        sweep_files = _find_sweep_results_files()
        fresh_files = [f for f in sweep_files if _is_results_fresh(f)]

        if not fresh_files:
            log.debug("  No fresh sweep results files found (checked %d files)",
                       len(sweep_files))
        else:
            for file_path in fresh_files:
                results_data = _read_sweep_results_json(file_path)
                if results_data is None:
                    continue

                json_variants = _extract_sweep_variants_for_trader(
                    results_data, base_trader,
                )
                sweep_date = results_data.get(
                    "sweep_date",
                    results_data.get("date", "unknown"),
                )

                for jv in json_variants:
                    name = _make_sweep_variant_name(
                        base_trader, jv["variant_name"], sweep_date,
                    )
                    variants.append({
                        "name": name,
                        "config": jv["config"],
                        "variant_name": jv["variant_name"],
                        "score": jv["score"],
                        "source": f"results_json:{file_path.name}",
                    })

                if json_variants:
                    log.info(
                        "  Found %d variants from sweep file %s for %s",
                        len(json_variants), file_path.name, base_trader,
                    )
    except Exception as e:
        log.warning("  Error reading sweep results files: %s", e)

    # Deduplicate by name — keep the one with the highest score
    seen: Dict[str, Dict[str, Any]] = {}
    for v in variants:
        if v["name"] not in seen or v["score"] > seen[v["name"]]["score"]:
            seen[v["name"]] = v

    return list(seen.values())


# ═══════════════════════════════════════════════════════════════════════════════
# Variant generation
# ═══════════════════════════════════════════════════════════════════════════════

def _random_suffix() -> str:
    """Short random suffix for variant names."""
    import uuid
    return uuid.uuid4().hex[:6]


def _fetch_sweep_best_params(base_trader: str) -> Optional[Dict[str, float]]:
    """Fetch the best params from the latest sweep run for a base trader.

    Queries sweep_runs for the most recent completed run, then fetches
    the winning variant's params from sweep_results via validation_meta.

    Returns:
        Flat param dict (e.g. {'momentum_threshold': 0.55}), or None
        if no sweep results exist.
    """
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        # Get the most recent completed sweep run for this trader
        cur.execute(
            """SELECT id, best_variant_id, best_score
               FROM trading.sweep_runs
               WHERE trader_id = %s
                 AND finished_at IS NOT NULL
               ORDER BY finished_at DESC
               LIMIT 1""",
            (base_trader,),
        )
        run = cur.fetchone()
        if not run:
            log.info("  No sweep results for %s — falling back to random", base_trader)
            return None

        # Fetch the winning variant's validation_meta for params
        cur.execute(
            """SELECT validation_meta
               FROM trading.sweep_results
               WHERE run_id = %s AND variant_id = %s""",
            (run["id"], run["best_variant_id"]),
        )
        row = cur.fetchone()
        if not row or not row.get("validation_meta"):
            log.info("  No params found in sweep variant for %s", base_trader)
            return None

        meta = row["validation_meta"]
        if isinstance(meta, str):
            meta = json.loads(meta)

        # Extract params from validation_meta.signal_params_json
        params_json = meta.get("signal_params_json", "")
        if not params_json:
            log.info("  No signal_params_json in sweep result for %s", base_trader)
            return None

        if isinstance(params_json, str):
            params_data = json.loads(params_json)
        else:
            params_data = params_json

        log.info(
            "  Using sweep best params for %s (score=%s): %s",
            base_trader, run["best_score"], params_data,
        )
        return {k: float(v) for k, v in params_data.items()}

    except Exception as e:
        log.warning("  Error fetching sweep params for %s: %s — falling back", base_trader, e)
        return None
    finally:
        conn.close()


def generate_param_variant(
    base_trader: str, top_config: Dict[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """Generate a parameter perturbation using sweep results.

    Instead of random perturbation, uses the best params from the
    latest completed sweep run for this base trader. Applies a small
    ±5% perturbation to the sweep best params to explore nearby space.

    Falls back to the original random perturbation if no sweep results
    exist or the top config has no recognizable params.

    Returns:
        (name, flat_config_dict)
    """
    # Try sweep results first
    sweep_params = _fetch_sweep_best_params(base_trader)

    if sweep_params:
        params = SignalParams()
        new_config: Dict[str, Any] = {}

        # Start with all sweep best params, perturb one by ±5% of range
        tunable = [k for k in sweep_params if k in params.param_names()]

        if tunable:
            param_name = random.choice(tunable)
            current = sweep_params[param_name]
            bound = SignalParams.bound(param_name)
            delta = (bound.max_val - bound.min_val) * random.uniform(
                -SWEEP_PERTURBATION_PCT, SWEEP_PERTURBATION_PCT
            )
            new_val = bound.clip(current + delta)

            # Build config: perturbed param + other sweep params
            for name, val in sweep_params.items():
                if name in params.param_names():
                    new_config[name] = bound.clip(val) if name == param_name else float(val)
            new_config[param_name] = new_val

            log.info(
                "  Perturbed sweep best %s from %.4f to %.4f (Δ=%.4f)",
                param_name, current, new_val, delta,
            )
        else:
            # Sweep params exist but none match — use as-is
            new_config = dict(sweep_params)

        name = f"{base_trader}-param-{_random_suffix()}"
        return name, new_config

    # Fallback: random perturbation of the top config (original behavior)
    normalized = normalize_config(top_config)
    params = SignalParams()

    # Find parameters that are both in the top config AND known to SignalParams
    tunable = [k for k in normalized if k in params.param_names()]

    if not tunable:
        # Top config has no recognizable params — pick any SignalParams param
        tunable = params.param_names()

    param_name = random.choice(tunable)
    bound = SignalParams.bound(param_name)

    # Get current value (from top config, or default)
    if param_name in normalized:
        current = normalized[param_name]
    else:
        current = bound.default

    # Perturb within ±15% of the full range
    delta = (bound.max_val - bound.min_val) * random.uniform(-0.15, 0.15)
    new_val = bound.clip(current + delta)

    # Build the new config dict (flat names)
    new_config = {param_name: new_val}

    # Copy other params from top config for consistency
    for name, val in normalized.items():
        if name != param_name and name in params.param_names():
            new_config[name] = val

    name = f"{base_trader}-param-{_random_suffix()}"
    return name, new_config


def generate_prompt_variant(base_trader: str) -> Tuple[str, Dict[str, Any]]:
    """Generate a prompt variant — fallback when no sweep results available.

    Simulates a prompt change by adjusting signal weights and
    confidence thresholds. Superseded by sweep-sourced variants when
    prompt_sweep results are available.

    Returns:
        (name, flat_config_dict)
    """
    params = SignalParams()

    # Adjust signal emphasis to simulate different prompt priorities
    new_config: Dict[str, Any] = {
        "momentum_threshold": round(random.uniform(0.35, 0.75), 2),
        "volume_threshold": round(random.uniform(0.8, 2.0), 2),
        "weight_trending_up": round(random.uniform(0.5, 1.8), 2),
        "weight_trending_down": round(random.uniform(0.0, 1.0), 2),
        "weight_mean_reverting": round(random.uniform(0.3, 1.5), 2),
        "conviction_multiplier": round(random.uniform(1.0, 2.5), 2),
    }

    # Clip to bounds
    for pname, val in list(new_config.items()):
        bound = SignalParams.bound(pname)
        new_config[pname] = bound.clip(val)

    name = f"{base_trader}-prompt-{_random_suffix()}"
    return name, new_config


def generate_wildcard(base_trader: str) -> Tuple[str, Dict[str, Any]]:
    """Generate a completely random config within safe SignalParams bounds.

    Wildcard — picks a random subset (3–5) of parameters and randomizes them.
    This explores the parameter space broadly.

    Returns:
        (name, flat_config_dict)
    """
    params = SignalParams()
    all_names = params.param_names()

    # Pick 3–5 random params to randomize
    n_pick = random.randint(3, min(5, len(all_names)))
    picked = random.sample(all_names, n_pick)

    new_config: Dict[str, Any] = {}
    for pname in picked:
        bound = SignalParams.bound(pname)
        if bound.is_int:
            new_config[pname] = random.randint(int(bound.min_val), int(bound.max_val))
        else:
            new_config[pname] = round(random.uniform(bound.min_val, bound.max_val), 3)

    name = f"{base_trader}-wild-{_random_suffix()}"
    return name, new_config


# Ordered fallback generators (used when no sweep variants available)
_FALLBACK_GENERATORS: List[Tuple[str, Any]] = [
    ("params", generate_param_variant),
    ("prompt", generate_prompt_variant),
    ("wildcard", generate_wildcard),
]


# ═══════════════════════════════════════════════════════════════════════════════
# Database operations
# ═══════════════════════════════════════════════════════════════════════════════

def publish_sweep_results(
    trader_type: str,
    best_params: Dict[str, float],
    score: float,
    run_id: Optional[int] = None,
) -> int:
    """Write best sweep params to virtual_traders DB so culling picks them up.

    Updates active virtual traders for the given base strategy with the
    winning sweep params merged into their config. Also writes config_overrides
    to the config jsonb for immediate use.

    Args:
        trader_type: Base trader name (e.g., 'kairos', 'aldridge', 'stonks').
        best_params: Flat SignalParams dict from the winning variant.
        score: Best objective score from the sweep.
        run_id: Optional sweep run ID for logging.

    Returns:
        Number of virtual traders updated.
    """
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Get active virtual traders for this base strategy
    cur.execute(
        """SELECT id, name, config
           FROM trading.virtual_traders
           WHERE base_trader = %s
             AND status = 'active'
           ORDER BY name""",
        (trader_type,),
    )
    rows = cur.fetchall()

    if not rows:
        log.info("  No active %s virtuals to publish sweep results to", trader_type)
        conn.close()
        return 0

    # Convert flat params to dot-notation config format
    flat_to_dot = {v: k for k, v in _CONFIG_KEY_MAP.items()}
    dot_notation_overrides = {}
    for flat_name, value in best_params.items():
        if flat_name in flat_to_dot:
            dot_notation_overrides[flat_to_dot[flat_name]] = value
        else:
            # Already a flat name or can't map — store flat
            dot_notation_overrides[flat_name] = value

    updated = 0
    for row in rows:
        existing_config = row.get("config", {})
        if isinstance(existing_config, str):
            existing_config = json.loads(existing_config)

        # Merge sweep params into existing config (sweep wins on conflict)
        merged = {**existing_config, **dot_notation_overrides}

        cur.execute(
            """UPDATE trading.virtual_traders
               SET config = %s::jsonb
               WHERE id = %s""",
            (json.dumps(merged), row["id"]),
        )
        updated += 1
        log.info(
            "  Published sweep params to %s (score=%.4f, run_id=%s)",
            row["name"], score, run_id or "?",
        )

    conn.close()
    log.info(
        "  Updated %d virtual traders with sweep best params from run_id=%s",
        updated, run_id or "?",
    )
    return updated


def insert_virtual(
    name: str,
    base_trader: str,
    variant_type: str,
    config: Dict[str, Any],
    status: str = "probation",
):
    """Insert a new virtual trader into trading.virtual_traders.

    Skips duplicates (name already taken).
    """
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT id FROM trading.virtual_traders WHERE name = %s", (name,))
    if cur.fetchone():
        log.warning("  Virtual %s already exists — skipping", name)
        conn.close()
        return

    cur.execute(
        """INSERT INTO trading.virtual_traders
           (name, base_trader, variant_type, config, status, created_at, wins)
           VALUES (%s, %s, %s, %s::jsonb, %s, %s, 0)""",
        (name, base_trader, variant_type, json.dumps(config), status, date.today()),
    )
    conn.close()
    log.info("  ✅ Created %s (type=%s, status=%s)", name, variant_type, status)


def cull_virtual(name: str):
    """Mark a virtual trader as culled with timestamp."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """UPDATE trading.virtual_traders
           SET status = 'culled', culled_at = %s
           WHERE name = %s""",
        (date.today(), name),
    )
    conn.close()
    log.info("  🗑️  Culled %s", name)


def get_virtuals_for_base(base_trader: str) -> List[Dict[str, Any]]:
    """Get active virtual traders for a base type from the DB."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """SELECT name, config, status, variant_type
           FROM trading.virtual_traders
           WHERE base_trader = %s
             AND status IN ('active', 'probation')
           ORDER BY name""",
        (base_trader,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ═══════════════════════════════════════════════════════════════════════════════
# Main culling logic
# ═══════════════════════════════════════════════════════════════════════════════

def cull_base_trader(
    base_trader: str,
    cull_count: int = DEFAULT_CULL_COUNT,
    dry_run: bool = False,
    force_random: bool = False,
) -> Dict[str, Any]:
    """Run culling and regeneration for one base trader.

    Sources at least one replacement from prompt_sweep results when available.
    Falls back to random variant generation when sweep results are stale
    (>7 days) or unavailable.

    Args:
        base_trader: Short name of the base trader (e.g., 'kairos').
        cull_count: Number of virtuals to cull (and generate) per base.
        dry_run: If True, log intent but don't modify DB.
        force_random: If True, skip sweep lookup entirely.

    Returns:
        Summary dict with keys: base_trader, status, culled, generated, etc.
    """
    log.info("── %s ───────────────────────────────────────────────", base_trader.upper())

    # Get active virtuals
    virtuals = get_virtuals_for_base(base_trader)

    if len(virtuals) <= cull_count:
        log.warning(
            "  Only %d active virtuals — need more than %d to cull. Skipping.",
            len(virtuals), cull_count,
        )
        return {
            "base_trader": base_trader,
            "status": "too_few",
            "count": len(virtuals),
        }

    # ── Step 1: Compute 7-day P&L ──
    names = [v["name"] for v in virtuals]
    pnl_map = compute_7day_pnl(names)

    # ── Step 2: Rank and identify bottom performers ──
    # Sort ascending (worst first) for culling
    ranked = sorted(pnl_map.items(), key=lambda x: x[1])
    bottom = ranked[:cull_count]
    top_name, top_pnl = ranked[-1] if ranked else (None, 0.0)

    log.info("  7-day P&L ranking (%d virtuals):", len(ranked))
    for i, (name, pnl) in enumerate(ranked):
        marker = ""
        if (name, pnl) in bottom:
            marker = " ← CULL"
        elif name == top_name:
            marker = " ← TOP"
        log.info("    %2d. %-28s $%+8.2f%s", i + 1, name, pnl, marker)

    # ── Step 3: Get top trader's config for variant seeding ──
    top_config: Dict[str, Any] = {}
    for v in virtuals:
        if v["name"] == top_name:
            raw = v.get("config", {})
            top_config = raw if isinstance(raw, dict) else {}
            break

    # ── Step 4: Cull bottom performers ──
    for name, pnl in bottom:
        if not dry_run:
            cull_virtual(name)

    # ── Step 5: Pull sweep variants for replacements ──
    sweep_variants: List[Dict[str, Any]] = []
    if not force_random:
        try:
            sweep_variants = _pull_sweep_variants_for_replacement(base_trader)
        except Exception as e:
            log.warning(
                "  Error pulling sweep variants for %s (will use fallback): %s",
                base_trader, e,
            )
            sweep_variants = []

    # ── Step 6: Generate replacements ──
    log.info("  Generating %d new variants:", len(bottom))
    new_virtuals: List[Tuple[str, str, Dict[str, Any]]] = []
    sweep_index = 0

    for i in range(len(bottom)):
        # Try to use a sweep-sourced variant first
        if sweep_index < len(sweep_variants):
            sv = sweep_variants[sweep_index]
            sweep_index += 1

            new_name = sv["name"]
            new_config = sv["config"]
            variant_type = "from_sweep"
            source_label = sv.get("variant_name", "sweep")

            new_virtuals.append((new_name, variant_type, new_config))
            score_str = f" (score: {sv['score']:.4f})" if sv.get("score", 0.0) != 0.0 else ""
            log.info(
                "    %d. %-30s (type=%s, from sweep: %s)%s",
                i + 1, new_name, variant_type, source_label, score_str,
            )

            if not dry_run:
                insert_virtual(
                    new_name, base_trader, variant_type, new_config,
                    status="probation",
                )
            continue

        # Fallback: use random generators
        gen_idx = i - sweep_index
        if gen_idx < len(_FALLBACK_GENERATORS):
            gen_type, gen_fn = _FALLBACK_GENERATORS[gen_idx]
            if gen_type == "params":
                new_name, new_config = gen_fn(base_trader, top_config)
            else:
                new_name, new_config = gen_fn(base_trader)

            variant_type = "random"
            new_virtuals.append((new_name, variant_type, new_config))
            log.info("    %d. %-30s (type=%s, %s fallback)", i + 1, new_name, variant_type, gen_type)

            if not dry_run:
                insert_virtual(
                    new_name, base_trader, variant_type, new_config,
                    status="probation",
                )

    sweep_sourced = sum(1 for _, vt, _ in new_virtuals if vt == "from_sweep")
    random_sourced = sum(1 for _, vt, _ in new_virtuals if vt == "random")

    return {
        "base_trader": base_trader,
        "culled": [name for name, _ in bottom],
        "generated": [name for name, _, _ in new_virtuals],
        "sweep_sourced": sweep_sourced,
        "random_sourced": random_sourced,
        "top_trader": top_name,
        "top_pnl": top_pnl,
        "status": "ok",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def print_summary(results: List[Dict[str, Any]]):
    """Print human-readable summary to stdout."""
    total_culled = sum(len(r.get("culled", [])) for r in results)
    total_generated = sum(len(r.get("generated", [])) for r in results)
    total_sweep = sum(r.get("sweep_sourced", 0) for r in results)
    total_random = sum(r.get("random_sourced", 0) for r in results)

    print()
    print("═" * 72)
    print(f"  VIRTUAL TRADER CULLING — {date.today()}")
    print("═" * 72)

    for r in results:
        bt = r["base_trader"].upper()
        status = r.get("status", "")

        if status == "error":
            print(f"\n  ❌ {bt}: ERROR — {r.get('error', 'unknown')}")
            continue
        if status == "too_few":
            print(f"\n  ⚠️  {bt}: Only {r['count']} virtuals — skipped")
            continue

        culled = r.get("culled", [])
        generated = r.get("generated", [])
        top = r.get("top_trader", "?")
        top_pnl = r.get("top_pnl", 0)
        sweep_src = r.get("sweep_sourced", 0)
        random_src = r.get("random_sourced", 0)

        print(f"\n  📊 {bt}")
        print(f"     Top:       {top}  (7d P&L: ${top_pnl:.2f})")
        print(f"     Culled:    {', '.join(culled) if culled else 'none'}")
        print(f"     Generated: {', '.join(generated) if generated else 'none'}")
        if sweep_src or random_src:
            print(f"     Sources:   {sweep_src} sweep-sourced, {random_src} random")

    print(f"\n{'═' * 72}")
    print(f"  Summary: {total_culled} culled, {total_generated} generated "
          f"({total_sweep} from sweep, {total_random} random)")
    print(f"{'═' * 72}\n")


def main():
    global DB_DSN, BASE_TRADERS

    parser = argparse.ArgumentParser(
        description="Virtual Trader Culling — weekly cleanup with prompt_sweep integration"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without writing to DB")
    parser.add_argument("--once", action="store_true",
                        help="Run once and exit (default behavior; for script consistency)")
    parser.add_argument("--cull-count", type=int, default=DEFAULT_CULL_COUNT,
                        help=f"Cull this many virtuals per base (default: {DEFAULT_CULL_COUNT})")
    parser.add_argument("--base", type=str, default=None,
                        help="Only cull one base trader (default: all three)")
    parser.add_argument("--db-dsn", type=str, default=DB_DSN,
                        help="Postgres connection string")
    parser.add_argument("--force-random", action="store_true",
                        help="Skip sweep result lookup, generate all variants randomly")
    parser.add_argument("--sweep-results-dir", type=str, default=SWEEP_RESULTS_DIR,
                        help="Directory containing sweep results JSON files")
    args = parser.parse_args()

    DB_DSN = args.db_dsn
    global SWEEP_RESULTS_DIR
    SWEEP_RESULTS_DIR = args.sweep_results_dir

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("═" * 60)
    log.info("Virtual Trader Culling — %s", "DRY RUN" if args.dry_run else "LIVE")
    log.info("Date: %s | Cull count: %d", date.today(), args.cull_count)
    log.info("Sweep results dir: %s", SWEEP_RESULTS_DIR)
    if args.force_random:
        log.info("FORCE-RANDOM mode — skipping sweep result lookup")

    # Determine which base traders to cull
    if args.base:
        if args.base not in BASE_TRADERS:
            log.error("Unknown base trader: %s. Valid: %s", args.base, BASE_TRADERS)
            sys.exit(1)
        base_traders = [args.base]
    else:
        base_traders = list(BASE_TRADERS)

    # Run culling for each base trader
    results = []
    for bt in base_traders:
        try:
            result = cull_base_trader(
                bt,
                cull_count=args.cull_count,
                dry_run=args.dry_run,
                force_random=args.force_random,
            )
            results.append(result)
        except Exception as e:
            log.error("Culling failed for %s: %s", bt, e, exc_info=True)
            results.append({"base_trader": bt, "status": "error", "error": str(e)})

    # Summary
    print_summary(results)

    if args.dry_run:
        log.info("DRY RUN — no changes were made.")


if __name__ == "__main__":
    main()