"""
Structured logging — JSON-lines output for all agents and services.

Provides:
  - setup_logging(): one-call setup for console + JSON-lines handlers
  - get_logger(name): returns a StructuredLogAdapter that accepts extra= dict
  - JsonFormatter: formats log records as one JSON line per entry
  - StructuredLogAdapter: LoggerAdapter that passes extra= as structured data

Usage:
    from src.observability.logger import setup_logging, get_logger

    setup_logging(level="INFO", json_log="logs/trading.jsonl")
    log = get_logger("my_agent")
    log.info("Trade executed", extra={"ticker": "AAPL", "pnl": 0.05})
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


class JsonFormatter(logging.Formatter):
    """JSON log formatter for machine-parseable log files.

    Outputs one JSON object per line with ts, level, logger, msg, and
    optional extra_data and exception fields.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exc"] = str(record.exc_info[1])
        extra_data = getattr(record, "extra_data", None)
        if extra_data is not None:
            entry["data"] = extra_data
        return json.dumps(entry, default=str)


class StructuredLogAdapter(logging.LoggerAdapter):
    """Logger adapter that accepts extra structured data.

    Example:
        log.info("processing", extra={"ticker": "AAPL", "regime": "TRENDING"})
    """

    def process(self, msg: str, kwargs: Any) -> tuple:  # type: ignore[override]
        extra_data = kwargs.pop("extra", None)
        if extra_data is not None:
            kwargs["extra"] = {"extra_data": extra_data}
        return msg, kwargs


# Module-level logger cache
_loggers: Dict[str, StructuredLogAdapter] = {}
_initialized = False


def get_logger(name: str) -> StructuredLogAdapter:
    """Get a structured logger for a module.

    Preferred over ``logging.getLogger()`` — returns a StructuredLogAdapter
    that supports ``log.info("msg", extra={"key": "value"})``.
    """
    if name not in _loggers:
        raw = logging.getLogger(name)
        _loggers[name] = StructuredLogAdapter(raw, {})
    return _loggers[name]


def setup_logging(
    level: str = "INFO",
    json_log: Optional[str] = None,
    console: bool = True,
) -> None:
    """Configure centralized logging for all modules.

    Call once near application entry point. Subsequent calls are no-ops.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR).
        json_log: Path to write JSON-structured logs (one per line).
        console: Whether to also log to stderr in human-readable format.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Clear any pre-existing handlers
    root.handlers.clear()

    if console:
        handler = logging.StreamHandler(sys.stderr)
        handler.setLevel(getattr(logging, level.upper(), logging.INFO))
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)-5s] %(name)-18s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(handler)

    if json_log:
        json_path = Path(json_log)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(str(json_path))
        handler.setLevel(logging.DEBUG)  # JSON always at DEBUG for completeness
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)

    # Suppress noisy third-party loggers
    for noisy in ("urllib3", "httpx", "httpcore", "asyncio", "psycopg2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)