"""
Alert Manager — circuit breaker alerts and system health events.

Provides:
  - AlertManager: routes P0/P1/INFO alerts to logs, metrics, and Telegram
  - alert: global singleton instance

Usage:
    from src.observability.alert import alert

    alert.p0("Circuit breaker tripped", {"trader": "kairos", "reason": "drawdown"})
    alert.p1("High latency detected", {"latency_ms": 5000})
    summary = alert.summary()
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Dict, Optional

from src.observability.logger import get_logger
from src.observability.metrics import metrics
from src.observability.telegram import telegram_alert


class AlertManager:
    """Alert routing for circuit breaker trips and system health events.

    Routes alerts to:
      - Structured logs (always)
      - Metrics registry (always)
      - Telegram webhook (P0 only, rate-limited)
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
        """Fire a P0 (critical) alert — circuit breaker, data loss, etc.

        P0 alerts are:
        1. Logged as ERROR in structured logs
        2. Recorded as a metrics counter
        3. Sent to Telegram webhook (if configured)
        """
        alert_key = f"p0:{title}"
        if not self._should_fire(alert_key):
            return
        self._alert_count["p0"] += 1
        payload = {"severity": "P0", "title": title, "data": data or {}}
        self._alert_log.error("P0 ALERT: %s", title, extra=payload)
        metrics.increment("alerts.p0", tags={"title": title})
        metrics.gauge("alerts.p0.total", float(self._alert_count["p0"]))

        # Fire Telegram alert asynchronously (best-effort)
        try:
            telegram_alert(f"🚨 P0: {title}", data)
        except Exception:
            self._alert_log.warning("Telegram alert failed", extra={"title": title})

    def p1(self, title: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Fire a P1 (high) alert.

        P1 alerts are:
        1. Logged as WARNING in structured logs
        2. Recorded as a metrics counter
        """
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


# Global singleton
alert = AlertManager()