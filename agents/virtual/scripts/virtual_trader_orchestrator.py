#!/usr/bin/env python3
"""
Virtual Competitor Dispatcher — MARKET TICK orchestration for virtual OpenClaw agents.

Architecture:
  Instead of running Python CLI scripts that call LLM directly (the old Docker approach),
  virtual traders are now full OpenClaw agents on this VM (.41). The orchestrator
  dispatches MARKET TICK events via sessions_send to each virtual agent's inbox.

  Daytime (market hours, 5-min cadence):
    1. Fetch market data snapshot from data bus
    2. Build tick context packet for each virtual variant
    3. sessions_send to each virtual agent with the tick context
    4. Each virtual agent makes a decision independently
    
  Nighttime (post-market, accelerated):
    See virtual_nightly_replay.py

Usage:
    python3 agents/virtual/scripts/virtual_trader_orchestrator.py           # run continuously
    python3 agents/virtual/scripts/virtual_trader_orchestrator.py --once    # one tick cycle
    python3 agents/virtual/scripts/virtual_trader_orchestrator.py --virtual kairos-aggressive  # specific variant
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("virtual_orchestrator")

# ── Config ───────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DATA_BUS_URL = os.getenv("VT_DATA_BUS_URL", "http://192.168.1.25:5000")
DB_DSN = os.getenv("VT_DB_DSN", "host=192.168.1.179 port=5433 dbname=trading user=trader")
HERMES_BRIDGE_URL = "http://localhost:8644/send"

# Mapping of virtual agent IDs -> workspace paths
VIRTUAL_AGENTS = {
    "virtual-kairos-aggressive":   "kairos-aggressive",
    "virtual-kairos-conservative": "kairos-conservative",
    "virtual-kairos-contrarian":   "kairos-contrarian",
    "virtual-aldridge-aggressive":   "aldridge-aggressive",
    "virtual-aldridge-conservative": "aldridge-conservative",
    "virtual-aldridge-contrarian":   "aldridge-contrarian",
    "virtual-stonks-aggressive":   "stonks-aggressive",
    "virtual-stonks-conservative": "stonks-conservative",
    "virtual-stonks-contrarian":   "stonks-contrarian",
}


def load_chat_token() -> str:
    """Load Hermes chat bridge token."""
    token_path = Path.home() / "projects" / "hermes-openclaw-bridge" / ".casper_chat_token"
    try:
        return token_path.read_text().strip()
    except Exception:
        return ""


def send_market_tick(
    agent_id: str,
    tick_context: Dict[str, Any],
) -> bool:
    """Send a MARKET TICK message to a virtual agent via the Hermes bridge.

    The message is posted to the agent's inbox. On the next heartbeat,
    the agent reads it and makes a trading decision.

    Returns True if the bridge accepted the message.
    """
    token = load_chat_token()
    if not token:
        log.warning("No chat token available — sending via direct write instead")

        # Fallback: write to agent's inbox file
        inbox_path = Path(f"/home/openclaw/.openclaw/inbox-{agent_id}.json")
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if inbox_path.exists():
            try:
                existing = json.loads(inbox_path.read_text())
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []
        existing.append(tick_context)
        # Keep only last 20 messages
        existing = existing[-20:]
        inbox_path.write_text(json.dumps(existing, indent=2, default=str))
        return True

    try:
        message = json.dumps({
            "type": "MARKET_TICK",
            "agent": agent_id,
            "context": tick_context,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        data = json.dumps({
            "agentId": agent_id,
            "message": message,
        }).encode()

        req = urllib.request.Request(
            HERMES_BRIDGE_URL,
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = resp.read().decode()
            log.debug("Sent tick to %s: %s", agent_id, result[:100])
            return True
    except Exception as e:
        log.warning("Failed to send tick to %s via bridge: %s — falling back to inbox", agent_id, e)
        # Fallback to inbox write
        inbox_path = Path(f"/home/openclaw/.openclaw/inbox-{agent_id}.json")
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if inbox_path.exists():
            try:
                existing = json.loads(inbox_path.read_text())
            except Exception:
                existing = []
        existing.append(tick_context)
        existing = existing[-20:]
        inbox_path.write_text(json.dumps(existing, indent=2, default=str))
        return True


def fetch_market_snapshot() -> Dict[str, Any]:
    """Fetch the current market snapshot from the data bus.

    Returns a dict with quotes, regime, signals, F&G that all virtual agents need.
    """
    snapshot: Dict[str, Any] = {
        "type": "MARKET_TICK",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "regime": "unknown",
        "fear_greed": 50,
        "vix": 15.0,
        "quotes": {},
        "momentum_signals": {},
        "symbols": [],
    }

    # Fetch market regime
    try:
        with urllib.request.urlopen(f"{DATA_BUS_URL}/market-regime", timeout=5) as resp:
            data = json.loads(resp.read().decode())
            snapshot["regime"] = data.get("regime", "unknown")
            snapshot["fear_greed"] = data.get("fear_greed", 50)
            snapshot["vix"] = data.get("vix", 15.0)
    except Exception as e:
        log.debug("Could not fetch market regime: %s", e)

    # Fetch health endpoint for tracked symbols
    symbols = ["SPY", "AAPL", "NVDA", "MSFT", "GOOGL", "META", "TSLA"]
    try:
        with urllib.request.urlopen(f"{DATA_BUS_URL}/health", timeout=5) as resp:
            data = json.loads(resp.read().decode())
            entries = data.get("cache_stats", {}).get("entries", [])
            cached_symbols = [e.split(":", 1)[1] for e in entries if e.startswith("quote:")]
            if cached_symbols:
                symbols = cached_symbols
    except Exception:
        pass

    snapshot["symbols"] = symbols

    # Fetch quotes
    try:
        url = f"{DATA_BUS_URL}/quotes?symbols={','.join(symbols[:20])}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            snapshot["quotes"] = data.get("quotes", {})
    except Exception as e:
        log.debug("Could not fetch quotes: %s", e)

    # Fetch momentum signals
    try:
        url = f"{DATA_BUS_URL}/signals/momentum?symbols={','.join(symbols[:20])}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            snapshot["momentum_signals"] = data.get("signals", {})
    except Exception as e:
        log.debug("Could not fetch momentum signals: %s", e)

    return snapshot


def is_market_hours() -> bool:
    """Check if we're within 09:30-16:00 ET on a weekday."""
    try:
        import pytz
        eastern = pytz.timezone("US/Eastern")
        now = datetime.now(eastern)
        if now.weekday() >= 5:
            return False
        open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
        close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return open_t <= now <= close_t
    except Exception:
        return True


def run_one_cycle(
    virtual_names: Optional[List[str]] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run one MARKET TICK dispatch cycle to all virtual agents.

    Args:
        virtual_names: Specific virtuals to dispatch to. None = all.
        dry_run: If True, print what would be sent without dispatching.

    Returns:
        Summary dict.
    """
    agents_to_dispatch = list(VIRTUAL_AGENTS.keys())
    if virtual_names:
        agents_to_dispatch = [a for a in agents_to_dispatch if a in virtual_names]
        missing = set(virtual_names) - set(VIRTUAL_AGENTS.keys())
        if missing:
            log.warning("Unknown virtual agents: %s", sorted(missing))

    log.info("Dispatching MARKET TICK to %d virtual agents", len(agents_to_dispatch))
    log.info("  Agents: %s", ", ".join(agents_to_dispatch))

    if dry_run:
        log.info("DRY RUN — would dispatch to: %s", agents_to_dispatch)
        # Still fetch snapshot (no side effects)
        snapshot = fetch_market_snapshot()
        log.info("  Snapshot: %d quotes, %d signals, regime=%s",
                 len(snapshot["quotes"]), len(snapshot["momentum_signals"]),
                 snapshot["regime"])
        return {
            "status": "dry_run",
            "agents": len(agents_to_dispatch),
            "quotes": len(snapshot.get("quotes", {})),
        }

    # Fetch market snapshot
    snapshot = fetch_market_snapshot()
    if not snapshot.get("quotes"):
        log.warning("No market data available — will send empty snapshot")

    log.info("  Market: regime=%s F&G=%s VIX=%s symbols=%d",
             snapshot["regime"], snapshot["fear_greed"],
             snapshot["vix"], len(snapshot["symbols"]))

    # Dispatch to all virtual agents in parallel
    success = 0
    fail = 0

    with ThreadPoolExecutor(max_workers=min(9, len(agents_to_dispatch))) as pool:
        future_to_agent = {
            pool.submit(send_market_tick, agent_id, snapshot): agent_id
            for agent_id in agents_to_dispatch
        }

        for future in as_completed(future_to_agent):
            agent_id = future_to_agent[future]
            try:
                if future.result():
                    success += 1
                    log.debug("  ✅ %s received tick", agent_id)
                else:
                    fail += 1
                    log.warning("  ❌ %s failed to receive tick", agent_id)
            except Exception as e:
                fail += 1
                log.error("  💥 %s exception: %s", agent_id, e)

    log.info("Cycle complete: %d/%d dispatched successfully", success, success + fail)

    return {
        "status": "ok",
        "agents_dispatched": len(agents_to_dispatch),
        "success": success,
        "fail": fail,
        "quotes": len(snapshot.get("quotes", {})),
        "regime": snapshot.get("regime", "unknown"),
        "fear_greed": snapshot.get("fear_greed", 50),
    }


def main():
    parser = argparse.ArgumentParser(description="Virtual Competitor MARKET TICK Dispatcher")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be sent")
    parser.add_argument("--virtual", type=str, default=None,
                        help="Comma-separated virtual agent IDs (default: all)")
    parser.add_argument("--interval", type=int, default=300,
                        help="Tick interval in seconds (default: 300 = 5 min)")
    parser.add_argument("--data-bus", type=str, default=DATA_BUS_URL, help="Data bus URL")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    global DATA_BUS_URL
    DATA_BUS_URL = args.data_bus

    virtual_names: Optional[List[str]] = None
    if args.virtual:
        virtual_names = [f"virtual-{n.strip()}" if not n.strip().startswith("virtual-")
                         else n.strip() for n in args.virtual.split(",") if n.strip()]
        log.info("Filtering to virtuals: %s", virtual_names)

    log.info("═" * 60)
    log.info("Virtual Competitor MARKET TICK Dispatcher")
    log.info("  Data bus: %s", DATA_BUS_URL)
    log.info("  Interval: %ds", args.interval)
    log.info("  Virtual agents: %s", virtual_names or "ALL")

    if args.dry_run:
        run_one_cycle(virtual_names=virtual_names, dry_run=True)
        return

    if args.once:
        result = run_one_cycle(virtual_names=virtual_names)
        log.info("One-shot complete: %d dispatched, %d succeeded",
                 result.get("agents_dispatched", 0), result.get("success", 0))
        return

    # Continuous loop during market hours
    cycle = 0
    log.info("Starting continuous dispatch loop")
    while True:
        if is_market_hours():
            cycle += 1
            log.info("── Cycle #%d ──────────────────────────────────────", cycle)
            try:
                run_one_cycle(virtual_names=virtual_names)
            except Exception as e:
                log.error("Cycle #%d failed: %s", cycle, e, exc_info=True)
        else:
            log.debug("Outside market hours — sleeping")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
