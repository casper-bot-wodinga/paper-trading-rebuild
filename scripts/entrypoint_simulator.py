#!/usr/bin/env python3
"""Entrypoint dispatcher for the Historical Trading Simulator container.

Wraps src/simulator.py with container-aware defaults, health-check server,
and environment variable configuration. Supports all simulator subcommands
plus a health-server mode for long-lived deployments.

Usage (via Docker CMD):
    sweep --all                          # nightly sweep for all traders
    sweep --trader kairos                # single trader sweep
    sweep --trader kairos --days 10      # with custom data window
    deep --trader kairos                 # deep validation (30 days)
    deep --trader kairos --days 45       # deep with custom window
    weekend                              # weekend 90-day sweep
    analyze --trader kairos              # generate hypotheses from last run
    test --trader kairos                 # quick synthetic-data smoke test
    serve                                # long-lived mode with health endpoint

Environment variables:
    SIM_DB_DSN           Postgres DSN for sweep results (optional, no-DB=offline)
    SIM_MODEL            OpenRouter model override (default: deepseek/deepseek-v4-flash)
    SIM_LOG_LEVEL        Python log level (default: INFO)
    SIM_STATE_DIR        State directory path (default: /app/state)
    SIM_DATA_DIR         Market data directory (default: /app/data/market)
    SIM_DEFAULT_CASH     Default starting cash (default: 100000)
    SIM_MAX_PARALLEL     Max concurrent LLM calls (default: 8)
    OPENROUTER_API_KEY   Required for LLM-based simulation
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import List, Optional

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

log = logging.getLogger("simulator_entrypoint")

# ── Config from environment ────────────────────────────────────────────────────

STATE_DIR = Path(os.getenv("SIM_STATE_DIR", "/app/state"))
DATA_DIR = Path(os.getenv("SIM_DATA_DIR", "/app/data/market"))
DB_DSN = os.getenv("SIM_DB_DSN", "")
DEFAULT_MODEL = os.getenv("SIM_MODEL", "deepseek/deepseek-v4-flash")
DEFAULT_CASH = float(os.getenv("SIM_DEFAULT_CASH", "100000"))
MAX_PARALLEL = int(os.getenv("SIM_MAX_PARALLEL", "8"))
DEFAULT_RELAX = int(os.getenv("SIM_RELAX_ITERATIONS", "3"))

# ── Logging ───────────────────────────────────────────────────────────────────

def setup_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def check_prerequisites():
    """Verify required env vars and directories before dispatch."""
    if not os.getenv("OPENROUTER_API_KEY"):
        log.warning("OPENROUTER_API_KEY is not set — LLM calls will fail")
        log.warning("Simulator running from this container requires OPENROUTER_API_KEY")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    log.info("State dir:   %s", STATE_DIR)
    log.info("Data dir:    %s", DATA_DIR)
    log.info("DB DSN:      %s", DB_DSN or "(offline — synthetic data only)")
    log.info("Model:       %s", DEFAULT_MODEL)
    log.info("Cash:        $%s", f"{DEFAULT_CASH:,.0f}")
    log.info("Max parallel: %s", MAX_PARALLEL)
    log.info("Relax iters: %s", DEFAULT_RELAX)


# ── Container health server ───────────────────────────────────────────────────

def start_health_server(port: int = 9100):
    """Minimal HTTP health endpoint for container orchestration.

    200 → {"status":"ok","mode":"serve"}
    """
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "ok",
                    "mode": "serve",
                    "simulator": "ready",
                }).encode())
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, fmt, *args):
            pass  # suppress default HTTP log noise

    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info("Health server listening on port %d", port)
    server.serve_forever()


# ── Dispatch helpers ──────────────────────────────────────────────────────────

def build_common_args() -> List[str]:
    """Build the common CLI arguments shared across subcommands."""
    args = []
    if DB_DSN:
        args.extend(["--db-dsn", DB_DSN])
    args.extend(["--model", DEFAULT_MODEL])
    args.extend(["--parallel", str(MAX_PARALLEL)])
    return args


def cmd_sweep(args_extra: List[str]):
    """Dispatch to simulator sweep subcommand."""
    from src.simulator import main as sim_main

    sys.argv = [
        "simulator",
        "sweep",
        *build_common_args(),
        *args_extra,
    ]
    log.info("Starting SWEEP: python3 -m src.simulator sweep %s", " ".join(args_extra))
    sim_main()


def cmd_deep(args_extra: List[str]):
    """Dispatch to simulator deep validation subcommand."""
    from src.simulator import main as sim_main

    sys.argv = [
        "simulator",
        "deep",
        *build_common_args(),
        *args_extra,
    ]
    log.info("Starting DEEP: python3 -m src.simulator deep %s", " ".join(args_extra))
    sim_main()


def cmd_weekend(args_extra: List[str]):
    """Dispatch to simulator weekend sweep subcommand."""
    from src.simulator import main as sim_main

    sys.argv = [
        "simulator",
        "weekend",
        *build_common_args(),
        *args_extra,
    ]
    log.info("Starting WEEKEND: python3 -m src.simulator weekend %s", " ".join(args_extra))
    sim_main()


def cmd_analyze(args_extra: List[str]):
    """Dispatch to simulator analysis subcommand."""
    from src.simulator import main as sim_main

    sys.argv = [
        "simulator",
        "analyze",
        *build_common_args(),
        *args_extra,
    ]
    log.info("Starting ANALYZE: python3 -m src.simulator analyze %s", " ".join(args_extra))
    sim_main()


def cmd_test(args_extra: List[str]):
    """Dispatch to simulator test subcommand (synthetic data smoke test)."""
    from src.simulator import main as sim_main

    sys.argv = [
        "simulator",
        "test",
        *build_common_args(),
        *args_extra,
    ]
    log.info("Starting TEST: python3 -m src.simulator test %s", " ".join(args_extra))
    sim_main()


def cmd_serve():
    """Run the health server only (for long-lived deployments)."""
    log.info("Starting in SERVE mode — health endpoint on :9100")
    start_health_server()


def cmd_runonce(args_extra: List[str]):
    """Run a one-shot simulation with explicit parameters.

    Combines the simulator module's run-once logic with container config.
    Accepts the same parameters as sweep but guarantees single execution.
    """
    # This uses the same sweep command path but with --once semantics
    from src.simulator import main as sim_main

    # Ensure --days is set (default to 5 if not specified)
    if not any(a.startswith("--days") for a in args_extra):
        args_extra = ["--days", "5"] + args_extra

    sys.argv = [
        "simulator",
        "sweep",
        *build_common_args(),
        *args_extra,
    ]
    log.info("Starting RUNONCE: python3 -m src.simulator sweep %s", " ".join(args_extra))
    sim_main()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Historical Trading Simulator — container entrypoint",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("SIM_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: %(default)s)",
    )

    sub = parser.add_subparsers(dest="command")

    # sweep
    sp = sub.add_parser("sweep", help="Run nightly sweep (simulator sweep)")
    sp.add_argument("--all", action="store_true", help="All traders")
    sp.add_argument("--trader", type=str, default=None, help="Single trader name")
    sp.add_argument("--days", type=int, default=5, help="Data window in days")
    sp.add_argument("--json", action="store_true", help="Output JSON")

    # deep
    dp = sub.add_parser("deep", help="Deep validation (30+ days)")
    dp.add_argument("--trader", type=str, default="kairos", help="Trader name")
    dp.add_argument("--days", type=int, default=30, help="Data window in days")
    dp.add_argument("--json", action="store_true", help="Output JSON")

    # weekend
    wp = sub.add_parser("weekend", help="Weekend 90-day sweep")
    wp.add_argument("--days", type=int, default=90, help="Data window in days")
    wp.add_argument("--json", action="store_true", help="Output JSON")

    # analyze
    ap = sub.add_parser("analyze", help="Analyze past results, generate hypotheses")
    ap.add_argument("--trader", type=str, default="kairos", help="Trader name")
    ap.add_argument("--json", action="store_true", help="Output JSON")

    # test
    tp = sub.add_parser("test", help="Quick synthetic-data smoke test")
    tp.add_argument("--trader", type=str, default="kairos", help="Trader name")
    tp.add_argument("--ticks", type=int, default=10, help="Number of synthetic ticks")

    # runonce
    rp = sub.add_parser("runonce", help="Single deterministic simulation run")
    rp.add_argument("--trader", type=str, default="kairos", help="Trader name")
    rp.add_argument("--days", type=int, default=5, help="Data window in days")
    rp.add_argument("--json", action="store_true", help="Output JSON")

    # serve
    sub.add_parser("serve", help="Long-lived mode with health endpoint")

    # Default: show help if no args
    if len(sys.argv) <= 1:
        parser.print_help()
        return

    args, extra = parser.parse_known_args()

    # Setup logging
    setup_logging(args.log_level)
    check_prerequisites()

    # Dispatch
    if args.command == "sweep":
        sweep_args = ["--days", str(args.days)]
        if args.all:
            sweep_args.append("--all")
        elif args.trader:
            sweep_args.extend(["--trader", args.trader])
        else:
            sweep_args.append("--all")  # default: all
        if args.json:
            sweep_args.append("--json")
        cmd_sweep(sweep_args)

    elif args.command == "deep":
        deep_args = ["--trader", args.trader, "--days", str(args.days)]
        if args.json:
            deep_args.append("--json")
        cmd_deep(deep_args)

    elif args.command == "weekend":
        weekend_args = ["--days", str(args.days)]
        if args.json:
            weekend_args.append("--json")
        cmd_weekend(weekend_args)

    elif args.command == "analyze":
        analyze_args = ["--trader", args.trader]
        if args.json:
            analyze_args.append("--json")
        cmd_analyze(analyze_args)

    elif args.command == "test":
        test_args = ["--trader", args.trader, "--ticks", str(args.ticks)]
        cmd_test(test_args)

    elif args.command == "runonce":
        runonce_args = ["--trader", args.trader, "--days", str(args.days)]
        if args.json:
            runonce_args.append("--json")
        cmd_runonce(runonce_args)

    elif args.command == "serve":
        cmd_serve()

    else:
        parser.print_help()


# ── Signal handling for graceful shutdown ────────────────────────────────────

def _handle_sigterm(signum, frame):
    log.info("Received SIGTERM — shutting down")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    main()