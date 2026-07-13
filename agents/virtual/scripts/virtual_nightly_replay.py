#!/usr/bin/env python3
"""
Nightly Virtual Replay — accelerated simulation with prompt variants.

Runs after market close (16:00-06:00 ET). For each virtual competitor:
  1. Load the variant's config (AGENTS.md + config.yaml + SKILL.md)
  2. Run historical replay on today's bars using the LLMEngine (direct API, no OpenClaw)
  3. Record decision quality, P&L, win rate
  4. Rank by objective metric
  5. Print results for promotion review

Architecture:
  This replaces the old Docker-based virtual runner on docker.klo (.179).
  Virtual agents run as OpenClaw agents during market hours (live dispatch).
  At night, this Python script runs accelerated simulation using the same
  agent config files that the live agents use — no need for separate "virtual"
  data pipeline.

  By running the SAME AGENTS.md + SKILL.md + config.yaml through both
  live OpenClaw (day) and accelerated replay (night), we get consistent
  evaluation across both modes. If a variant performs well in replay,
  it should perform well live — and vice versa.

Usage:
    python3 agents/virtual/scripts/virtual_nightly_replay.py                      # replay all variants on today
    python3 agents/virtual/scripts/virtual_nightly_replay.py --date 2026-07-13     # specific date
    python3 agents/virtual/scripts/virtual_nightly_replay.py --variant aggressive  # specific variant type
    python3 agents/virtual/scripts/virtual_nightly_replay.py --base kairos         # specific base trader
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("virtual_nightly_replay")

# ── Paths ───────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

VIRTUAL_AGENTS_DIR = PROJECT_ROOT / "agents" / "virtual"
SCRIPTS_DIR = VIRTUAL_AGENTS_DIR / "scripts"
DB_DSN = os.getenv("VT_DB_DSN", "host=192.168.1.179 port=5433 dbname=trading user=trader")
DATA_BUS_URL = os.getenv("VT_DATA_BUS_URL", "http://192.168.1.25:5000")

# Variant types and their SKILL.md patterns
BASE_TRADERS = ["kairos", "aldridge", "stonks"]
VARIANT_TYPES = ["aggressive", "conservative", "contrarian"]


# ── Variant definition ──────────────────────────────────────────────────────


@dataclass
class VariantConfig:
    """A virtual competitor variant."""
    base_trader: str
    variant_type: str
    name: str  # e.g. "virtual-kairos-aggressive"
    config_path: Path
    agents_md_path: Path
    soul_md_path: Path
    skill_md_path: Path


def discover_variants(
    base_filter: Optional[str] = None,
    variant_filter: Optional[str] = None,
) -> List[VariantConfig]:
    """Discover all virtual variant configs on disk.

    Args:
        base_filter: Only load variants for this base trader.
        variant_filter: Only load variants of this type.

    Returns:
        List of VariantConfig objects.
    """
    variants = []

    for base in BASE_TRADERS:
        if base_filter and base != base_filter:
            continue

        for vtype in VARIANT_TYPES:
            if variant_filter and vtype != variant_filter:
                continue

            variant_dir = VIRTUAL_AGENTS_DIR / base / vtype
            if not variant_dir.exists():
                log.warning("Variant dir not found: %s", variant_dir)
                continue

            name = f"virtual-{base}-{vtype}"

            config_path = variant_dir / "config.yaml"
            agents_md_path = variant_dir / "AGENTS.md"
            soul_md_path = variant_dir / "SOUL.md"
            skill_md_path = variant_dir / "skills" / "SKILL.md"

            variants.append(VariantConfig(
                base_trader=base,
                variant_type=vtype,
                name=name,
                config_path=config_path,
                agents_md_path=agents_md_path,
                soul_md_path=soul_md_path,
                skill_md_path=skill_md_path,
            ))

    return variants


# ── Replay Runner ───────────────────────────────────────────────────────────


def load_variant_config(variant: VariantConfig) -> Dict[str, Any]:
    """Load a variant's config files into a dict for the LLMEngine.

    Returns a dict with: identity, agents_md, soul, tools, memory, skills.
    """
    config = {
        "name": variant.name,
        "base_trader": variant.base_trader,
        "variant_type": variant.variant_type,
    }

    # Load AGENTS.md
    if variant.agents_md_path.exists():
        config["agents_md"] = variant.agents_md_path.read_text()
    else:
        config["agents_md"] = ""

    # Load SOUL.md
    if variant.soul_md_path.exists():
        config["soul"] = variant.soul_md_path.read_text()
    else:
        config["soul"] = ""

    # Load YAML config
    if variant.config_path.exists():
        try:
            import yaml
            with open(variant.config_path) as f:
                yaml_config = yaml.safe_load(f)
                config["yaml"] = yaml_config
        except Exception as e:
            log.warning("Could not load YAML for %s: %s", variant.name, e)
            config["yaml"] = {}

    # Load SKILL.md
    if variant.skill_md_path.exists():
        config["skill"] = variant.skill_md_path.read_text()
    else:
        config["skill"] = ""

    return config


def run_variant_replay(
    variant: VariantConfig,
    replay_date: date,
    mock: bool = False,
) -> Dict[str, Any]:
    """Run one variant through accelerated historical replay.

    1. Load the variant's config files
    2. Fetch historical bars from data bus (or mock data)
    3. Run signal engine over the bars, tick by tick
    4. Make trading decisions via LLMEngine
    5. Compute performance metrics

    Args:
        variant: The variant to replay.
        replay_date: Date to replay.
        mock: Use mock data if True.

    Returns:
        Dict with metrics: total_return, win_rate, sharpe, calmar, trades, etc.
    """
    from src.signals import SignalEngine, SignalParams
    from src.llm_engine import LLMEngine
    from src.replay import ReplayHarness, Tick
    from src.prompt_builder import AgentFiles

    config = load_variant_config(variant)

    # Build AgentFiles from variant config
    agent_files = AgentFiles(
        identity=f"I am {variant.name}. {config.get('soul', '')}",
        agents_md=config.get("agents_md", ""),
        soul=config.get("soul", ""),
        tools="",
        memory="",
        skills=[config.get("skill", "")] if config.get("skill") else [],
    )

    # Build SignalParams from YAML config
    signal_params = SignalParams()
    yaml_config = config.get("yaml", {})
    signal_overrides = yaml_config.get("signals", {})
    for key, value in signal_overrides.items():
        if hasattr(signal_params, key):
            try:
                signal_params.set(key, float(value))
            except (ValueError, TypeError):
                pass

    # Fetch or mock bars
    if mock:
        bars = _generate_mock_bars(replay_date, config.get("yaml", {}))
    else:
        bars = _fetch_bars_from_data_bus(replay_date)

    if not bars:
        log.warning("No bars available for %s on %s", variant.name, replay_date)
        return {
            "variant": variant.name,
            "base_trader": variant.base_trader,
            "variant_type": variant.variant_type,
            "date": str(replay_date),
            "status": "no_data",
            "trades": 0,
            "total_return": 0.0,
            "win_rate": 0.0,
            "sharpe": 0.0,
        }

    # Convert bars to Ticks
    ticks = []
    for bar in bars:
        tick = Tick(
            timestamp=bar.get("timestamp", datetime.now(timezone.utc)),
            ticker=bar.get("ticker", "SPY"),
            open=bar.get("open", 0),
            high=bar.get("high", 0),
            low=bar.get("low", 0),
            close=bar.get("close", 0),
            volume=bar.get("volume", 0),
            rsi=bar.get("rsi"),
            momentum=bar.get("momentum"),
            volatility=bar.get("volatility"),
            regime=bar.get("regime"),
        )
        ticks.append(tick)

    # Run replay
    engine = LLMEngine(
        model="openrouter/deepseek/deepseek-v4-flash",
        temperature=0.3,
        max_tokens=2000,
        max_retries=1,
    )

    harness = ReplayHarness(initial_balance=10000)

    def trader_fn(tick, portfolio, journal):
        signal = signal_params.compute(tick) if hasattr(signal_params, 'compute') else None
        return engine.decide(
            tick=tick,
            signal=signal,
            journal=journal or [],
            portfolio=portfolio,
            agent_files=agent_files,
        )

    try:
        result = harness.run(ticks, trader_fn)
    except Exception as e:
        log.error("Replay failed for %s: %s", variant.name, e)
        return {
            "variant": variant.name,
            "base_trader": variant.base_trader,
            "variant_type": variant.variant_type,
            "date": str(replay_date),
            "status": "error",
            "error": str(e),
        }

    # Compute metrics
    metrics = _compute_replay_metrics(result, ticks)

    log.info("  %-28s → %d trades, return=%+.2f%%, WR=%.0f%%, Sharpe=%.2f",
             variant.name, metrics["trades"],
             metrics["total_return_pct"] * 100,
             metrics["win_rate"] * 100,
             metrics["sharpe"])

    return metrics


def _fetch_bars_from_data_bus(replay_date: date) -> List[Dict[str, Any]]:
    """Fetch historical bars from the data bus for the given date."""
    import urllib.request

    symbols = ["SPY", "AAPL", "NVDA", "MSFT", "GOOGL", "META", "TSLA",
               "AMZN", "JPM", "BAC", "WMT", "DIS", "KO", "JNJ", "XOM", "QQQ"]
    all_bars = []

    for symbol in symbols:
        try:
            url = (
                f"{DATA_BUS_URL}/bars"
                f"?symbol={symbol}"
                f"&start_date={(replay_date - timedelta(days=60)).isoformat()}"
                f"&end_date={replay_date.isoformat()}"
                f"&interval=daily"
            )
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                bars = data.get("bars", data.get("symbols", {}).get(symbol, []))
                for bar in bars:
                    bar["ticker"] = symbol
                all_bars.extend(bars)
        except Exception as e:
            log.debug("Could not fetch bars for %s: %s", symbol, e)

    if not all_bars:
        log.warning("No bars returned from data bus — trying health endpoint")
        try:
            with urllib.request.urlopen(f"{DATA_BUS_URL}/health", timeout=5) as resp:
                log.info("Data bus health OK — may need to warm up bar cache")
        except Exception as e:
            log.warning("Data bus unreachable: %s", e)

    return all_bars


def _generate_mock_bars(replay_date: date, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Generate mock historical bars for testing."""
    import random
    import numpy as np

    symbols = ["SPY", "AAPL", "NVDA", "MSFT"]
    bars = []
    seed = int(replay_date.strftime("%Y%m%d"))
    rng = random.Random(seed)

    for symbol in symbols:
        price = {"SPY": 550, "AAPL": 220, "NVDA": 120, "MSFT": 450}.get(symbol, 100)
        for day_offset in range(-60, 1):
            ts = datetime.combine(replay_date + timedelta(days=day_offset),
                                  datetime.min.time()).replace(tzinfo=timezone.utc)
            change = rng.uniform(-0.03, 0.03)
            price *= (1 + change)

            bars.append({
                "timestamp": ts.isoformat(),
                "ticker": symbol,
                "open": price * (1 - rng.uniform(0, 0.005)),
                "high": price * (1 + rng.uniform(0, 0.01)),
                "low": price * (1 - rng.uniform(0, 0.01)),
                "close": price,
                "volume": int(rng.uniform(500000, 5000000)),
                "rsi": rng.uniform(30, 70),
                "momentum": rng.uniform(-0.05, 0.05),
                "volatility": rng.uniform(0.1, 0.3),
                "regime": rng.choice(["SUSTAINABLE", "CHOPPY", "EXHAUSTED"]),
            })

    return bars


def _compute_replay_metrics(
    result: Any,
    ticks: List[Tick],
) -> Dict[str, Any]:
    """Compute performance metrics from replay results.

    Args:
        result: ReplayHarness.run() result (has equity_curve, trades, metrics).
        ticks: The ticks that were replayed (for date range).

    Returns:
        Dict of metrics.
    """
    # Extract trades
    trades = getattr(result, 'trades', getattr(result, 'decisions', []))
    n_trades = len(trades)

    # Win rate
    winning_trades = sum(1 for t in trades if getattr(t, 'pnl', 0) > 0)
    win_rate = winning_trades / max(n_trades, 1)

    # Total return
    final_equity = getattr(result, 'final_equity',
                           getattr(result, 'equity_curve', [10000])[-1]
                           if hasattr(result, 'equity_curve') and result.equity_curve
                           else 10000)
    total_return = (final_equity - 10000) / 10000

    # Sharpe ratio (approximate)
    returns = []
    if hasattr(result, 'equity_curve') and result.equity_curve:
        eq = result.equity_curve
        for i in range(1, len(eq)):
            returns.append((eq[i] - eq[i-1]) / eq[i-1])
    sharpe = 0.0
    if returns:
        mean_ret = sum(returns) / len(returns)
        std_ret = (sum((r - mean_ret) ** 2 for r in returns) / len(returns)) ** 0.5
        if std_ret > 0:
            sharpe = mean_ret / std_ret * (252 ** 0.5)  # annualized

    # Max drawdown
    max_dd = 0.0
    if hasattr(result, 'equity_curve') and result.equity_curve:
        peak = result.equity_curve[0]
        for eq in result.equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd

    return {
        "trades": n_trades,
        "winning_trades": winning_trades,
        "total_return": total_return,
        "total_return_pct": round(total_return * 100, 2),
        "win_rate": round(win_rate, 4),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(max_dd, 4),
        "final_equity": round(final_equity, 2),
    }


def print_results_table(results: List[Dict[str, Any]]):
    """Print replay results as a formatted table."""
    print()
    print("═" * 100)
    print(f"  VIRTUAL COMPETITOR REPLAY RESULTS — {date.today()}")
    print("═" * 100)

    if not results:
        print("  No results to display.")
        print()
        return

    # Filter only successful runs
    successful = [r for r in results if r.get("status") in (None, "ok")]
    errored = [r for r in results if r.get("status") == "error"]
    no_data = [r for r in results if r.get("status") == "no_data"]

    if successful:
        print(f"\n  {'Variant':<30} {'Trades':<7} {'Return':<9} {'WinRate':<8} "
              f"{'Sharpe':<8} {'MaxDD':<8} {'Final':<10}")
        print("  " + "-" * 80)

        # Sort by total return (descending)
        sorted_results = sorted(successful, key=lambda r: r.get("total_return", 0), reverse=True)
        for r in sorted_results:
            ret = r.get("total_return_pct", 0)
            print(f"  {r['variant']:<30} {r.get('trades', 0):<7} "
                  f"{ret:+.2f}%{' ':<5} "
                  f"{r.get('win_rate', 0)*100:<7.0f}% "
                  f"{r.get('sharpe', 0):<8.2f} "
                  f"{r.get('max_drawdown', 0)*100:<7.1f}% "
                  f"${r.get('final_equity', 0):<8.2f}")

        # Show ranking
        print(f"\n  ── RANKING ──")
        for i, r in enumerate(sorted_results, 1):
            ret = r.get("total_return_pct", 0)
            print(f"  #{i:<2} {r['variant']:<28} Return: {ret:+.2f}%")

    if errored:
        print(f"\n  ❌ {len(errored)} variant(s) had errors:")
        for r in errored:
            print(f"     {r['variant']}: {r.get('error', 'unknown error')}")

    if no_data:
        print(f"\n  ⚠️  {len(no_data)} variant(s) had no data:")
        for r in no_data:
            print(f"     {r['variant']}: no bars available for replay date")

    print()


def main():
    parser = argparse.ArgumentParser(description="Nightly Virtual Competitor Replay")
    parser.add_argument("--date", type=str, default=None,
                        help="Date to replay (YYYY-MM-DD). Default: today")
    parser.add_argument("--base", type=str, default=None,
                        help="Base trader to replay (kairos/aldridge/stonks). Default: all")
    parser.add_argument("--variant", type=str, default=None,
                        help="Variant type (aggressive/conservative/contrarian). Default: all")
    parser.add_argument("--mock", action="store_true",
                        help="Use mock data instead of data bus")
    parser.add_argument("--once", action="store_true",
                        help="Run once and exit (default behavior)")
    parser.add_argument("--data-bus", type=str, default=DATA_BUS_URL,
                        help="Data bus base URL")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    global DATA_BUS_URL
    DATA_BUS_URL = args.data_bus

    replay_date = date.today()
    if args.date:
        replay_date = date.fromisoformat(args.date)

    log.info("═" * 80)
    log.info("Nightly Virtual Competitor Replay")
    log.info("  Date: %s | Mock: %s", replay_date, args.mock)
    log.info("  Base filter: %s | Variant filter: %s",
             args.base or "ALL", args.variant or "ALL")
    log.info("  Data bus: %s", DATA_BUS_URL)

    # Discover variants
    variants = discover_variants(
        base_filter=args.base,
        variant_filter=args.variant,
    )

    if not variants:
        log.error("No variants found!")
        sys.exit(1)

    log.info("Found %d variants to replay:", len(variants))
    for v in variants:
        log.info("  %s (base=%s, type=%s)", v.name, v.base_trader, v.variant_type)

    # Run replay for each variant
    results = []
    for variant in variants:
        log.info("── Replaying %s ──────────────────────────────", variant.name)
        try:
            result = run_variant_replay(variant, replay_date, mock=args.mock)
            results.append(result)
        except Exception as e:
            log.error("Replay crashed for %s: %s", variant.name, e, exc_info=True)
            results.append({
                "variant": variant.name,
                "base_trader": variant.base_trader,
                "variant_type": variant.variant_type,
                "status": "error",
                "error": str(e),
            })

    # Print results
    print_results_table(results)

    # Save results to state file for promotion pipeline
    output_dir = SCRIPTS_DIR.parent / ".replay_results"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"replay_{replay_date.isoformat()}.json"

    # Ensure JSON-serializable
    def serialize(val):
        if isinstance(val, (datetime, date)):
            return val.isoformat()
        if hasattr(val, '__float__'):
            return float(val)
        return str(val)

    serializable_results = json.loads(
        json.dumps(results, default=serialize)
    )

    with open(output_file, "w") as f:
        json.dump({
            "date": replay_date.isoformat(),
            "count": len(results),
            "results": serializable_results,
        }, f, indent=2)

    log.info("Results saved to %s", output_file)
    log.info("Done.")


if __name__ == "__main__":
    # Backport dataclass for the import guard
    from dataclasses import dataclass
    main()