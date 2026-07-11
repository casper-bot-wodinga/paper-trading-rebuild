"""
Metrics Registry — thread-safe in-memory counters, gauges, and histograms.

Provides:
  - MetricsRegistry: stores metrics with JSON export
  - metrics: global singleton instance

Usage:
    from src.observability.metrics import metrics

    metrics.increment("trades.executed", tags={"trader": "kairos"})
    metrics.gauge("portfolio.value", 10587.50)
    metrics.observe("latency.llm_ms", 1234)
    snapshot = metrics.snapshot()
    print(metrics.json())
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class MetricPoint:
    """A single metric data point with timestamp and optional tags."""

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


# Global singleton
metrics = MetricsRegistry()