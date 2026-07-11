"""
Observability — centralized logging, metrics, and alerting for the paper trading system.

Provides:
  - setup_logging(): consistent structured+console logging for all modules
  - MetricsRegistry: in-memory metrics with JSON export for Canvas/dashboards
  - AlertManager: circuit-breaker alerts → Canvas/Telegram

Usage:
    from src.observability import setup_logging, metrics, alert

    setup_logging(level="INFO", json_log="logs/trading.jsonl")
    metrics.increment("trades.executed", tags={"trader": "kairos"})
    alert.p0("Circuit breaker tripped", {"trader": "kairos", "reason": "drawdown"})

Ref: SPEC-v3 observability requirements, fusion-review weakness #8
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ═══════════════════════════════════════════════════════════════════════════════
# Structured Logging
# ═══════════════════════════════════════════════════════════════════════════════


class JsonFormatter(logging.Formatter):
    """JSON log formatter for machine-parseable log files."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
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
    """Logger adapter that accepts extra structured data."""

    def process(self, msg: str, kwargs: Any) -> tuple:  # type: ignore[override]
        extra_data = kwargs.pop("extra", None)
        if extra_data is not None:
            kwargs["extra"] = {"extra_data": extra_data}
        return msg, kwargs


# Module-level loggers with structured adapter support
_loggers: Dict[str, StructuredLogAdapter] = {}


def get_logger(name: str) -> StructuredLogAdapter:
    """Get a structured logger for a module.

    Preferred over `logging.getLogger()` — returns a StructuredLogAdapter
    that supports `log.info("msg", extra={"key": "value"})`.
    """
    if name not in _loggers:
        raw = logging.getLogger(name)
        _loggers[name] = StructuredLogAdapter(raw, {})
    return _loggers[name]


_initialized = False


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


# ═══════════════════════════════════════════════════════════════════════════════
# Metrics Registry
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class MetricPoint:
    """A single metric data point."""

    name: str
    value: float
    tags: Dict[str, str] = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class MetricsRegistry:
    """Thread-safe in-memory metrics store with JSON export.

    Supports counters, gauges, and histograms. Designed for periodic
    export to Canvas dashboards and log files.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, float] = defaultdict(float)
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, List[float]] = defaultdict(list)
        self._events: List[MetricPoint] = []

    def increment(self, name: str, value: float = 1.0, tags: Optional[Dict[str, str]] = None) -> None:
        """Increment a counter metric."""
        with self._lock:
            self._counters[name] += value
            self._events.append(MetricPoint(name=name, value=value, tags=tags or {}))

    def gauge(self, name: str, value: float) -> None:
        """Set a gauge metric."""
        with self._lock:
            self._gauges[name] = value

    def observe(self, name: str, value: float) -> None:
        """Record a histogram observation."""
        with self._lock:
            self._histograms[name].append(value)

    def event(self, name: str, tags: Optional[Dict[str, str]] = None) -> None:
        """Record a discrete event (no value)."""
        with self._lock:
            self._events.append(MetricPoint(name=name, value=1.0, tags=tags or {}))

    def snapshot(self) -> Dict[str, Any]:
        """Export current metrics as a JSON-serializable dict."""
        with self._lock:
            return {
                "ts": datetime.now(timezone.utc).isoformat(),
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": {
                    k: {
                        "count": len(v),
                        "min": min(v) if v else 0,
                        "max": max(v) if v else 0,
                        "avg": sum(v) / len(v) if v else 0,
                    }
                    for k, v in self._histograms.items()
                },
                "recent_events": [
                    {"name": e.name, "value": e.value, "tags": e.tags, "ts": e.ts}
                    for e in self._events[-100:]  # Last 100 events
                ],
            }

    def reset_events(self) -> None:
        """Clear the event buffer (keep counters/gauges/histograms)."""
        with self._lock:
            self._events.clear()

    def json(self) -> str:
        """Export snapshot as JSON string."""
        return json.dumps(self.snapshot(), default=str, indent=2)


# Global metrics registry
metrics = MetricsRegistry()


# ═══════════════════════════════════════════════════════════════════════════════
# Alert Manager
# ═══════════════════════════════════════════════════════════════════════════════


class AlertManager:
    """Alert routing for circuit breaker trips and system health events.

    Routes alerts to:
      - Structured logs (always)
      - Canvas dashboard (via metrics)
      - Telegram (future: via messaging gateway)
    """

    def __init__(self) -> None:
        self._alert_log = get_logger("alerts")
        self._alert_count: Dict[str, int] = defaultdict(int)
        self._last_alert: Dict[str, float] = {}
        self._cooldown_s = 300  # 5 min between repeated alerts

    def _should_fire(self, alert_key: str) -> bool:
        """Rate-limit: only fire if cooldown elapsed."""
        now = time.time()
        last = self._last_alert.get(alert_key, 0)
        if now - last < self._cooldown_s:
            return False
        self._last_alert[alert_key] = now
        return True

    def p0(self, title: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Fire a P0 (critical) alert — circuit breaker, data loss, etc."""
        alert_key = f"p0:{title}"
        if not self._should_fire(alert_key):
            return
        self._alert_count["p0"] += 1
        payload = {"severity": "P0", "title": title, "data": data or {}}
        self._alert_log.error("P0 ALERT: %s", title, extra=payload)
        metrics.increment("alerts.p0", tags={"title": title})
        metrics.gauge("alerts.p0.total", float(self._alert_count["p0"]))

    def p1(self, title: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Fire a P1 (high) alert."""
        alert_key = f"p1:{title}"
        if not self._should_fire(alert_key):
            return
        self._alert_count["p1"] += 1
        payload = {"severity": "P1", "title": title, "data": data or {}}
        self._alert_log.warning("P1 ALERT: %s", title, extra=payload)
        metrics.increment("alerts.p1", tags={"title": title})

    def info(self, title: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Log an informational system event."""
        payload = {"severity": "INFO", "title": title, "data": data or {}}
        self._alert_log.info("SYSTEM: %s", title, extra=payload)

    def summary(self) -> Dict[str, Any]:
        """Get alert summary for dashboards."""
        return {
            "p0_count": self._alert_count["p0"],
            "p1_count": self._alert_count["p1"],
            "last_alerts": dict(self._last_alert),
        }


# Global alert manager
alert = AlertManager()


# ═══════════════════════════════════════════════════════════════════════════════
# Convenience: wire existing loggers
# ═══════════════════════════════════════════════════════════════════════════════


def wire_metrics_to_log() -> None:
    """Periodically flush metrics snapshot to the JSON log.

    Call once to start a background thread that writes metrics every 60s.
    """
    def _flush_loop() -> None:
        while True:
            time.sleep(60)
            try:
                mlog = get_logger("metrics")
                mlog.info("metrics_snapshot", extra=metrics.snapshot())
                metrics.reset_events()
            except Exception:
                pass

    t = threading.Thread(target=_flush_loop, daemon=True, name="metrics-flush")
    t.start()