"""Tests for src/observability — logging, metrics, alerts, and telegram integration."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

import pytest

from src.observability import setup_logging, get_logger, metrics, alert
from src.observability.logger import JsonFormatter, StructuredLogAdapter
from src.observability.metrics import MetricsRegistry
from src.observability.alert import AlertManager


# ═══════════════════════════════════════════════════════════════════════════════
# Setup / Teardown
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def reset_state():
    """Reset logging initialized flag so setup_logging can be called per test."""
    import src.observability.logger as _log_mod
    _log_mod._initialized = False
    # Also clear root handlers to avoid handler buildup
    import logging
    root = logging.getLogger()
    root.handlers.clear()
    yield


# ═══════════════════════════════════════════════════════════════════════════════
# Logger Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetLogger:
    def test_returns_structured_adapter(self):
        log = get_logger("test_module")
        assert isinstance(log, StructuredLogAdapter)

    def test_same_name_returns_same_logger(self):
        log1 = get_logger("test_same")
        log2 = get_logger("test_same")
        assert log1 is log2

    def test_different_names_different_loggers(self):
        log1 = get_logger("test_a")
        log2 = get_logger("test_b")
        assert log1 is not log2


class TestSetupLogging:
    def test_json_log_creates_file(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            json_path = f.name

        try:
            setup_logging(level="INFO", json_log=json_path, console=False)
            log = get_logger("test_json")
            log.info("test message", extra={"key": "value"})

            # Flush handlers
            import logging
            logging.shutdown()

            with open(json_path) as f2:
                lines = f2.readlines()
                assert len(lines) >= 1
                entry = json.loads(lines[0])
                assert entry["msg"] == "test message"
                assert entry["data"]["key"] == "value"
                assert entry["logger"] == "test_json"
                assert "ts" in entry
                assert entry["level"] == "INFO"
        finally:
            try:
                os.unlink(json_path)
            except OSError:
                pass

    def test_json_log_no_extra(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            json_path = f.name

        try:
            # Reset to clear handlers, then re-setup
            import logging
            root = logging.getLogger()
            root.handlers.clear()

            setup_logging(level="DEBUG", json_log=json_path, console=False)
            log = get_logger("test_no_extra")
            log.debug("plain message")

            import logging
            logging.shutdown()

            with open(json_path) as f2:
                lines = f2.readlines()
                assert len(lines) >= 1
                entry = json.loads(lines[0])
                assert entry["msg"] == "plain message"
                assert "data" not in entry  # no extra data field
        finally:
            try:
                os.unlink(json_path)
            except OSError:
                pass


class TestJsonFormatter:
    def test_format_basic(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname=__file__, lineno=1, msg="hello",
            args=(), exc_info=None,
        )
        output = formatter.format(record)
        entry = json.loads(output)
        assert entry["level"] == "INFO"
        assert entry["msg"] == "hello"
        assert entry["logger"] == "test"

    def test_format_with_exception(self):
        formatter = JsonFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            record = logging.LogRecord(
                name="test", level=logging.ERROR,
                pathname=__file__, lineno=1, msg="error occurred",
                args=(), exc_info=sys.exc_info(),
            )
        output = formatter.format(record)
        entry = json.loads(output)
        assert "exc" in entry
        assert "boom" in entry["exc"]


# ═══════════════════════════════════════════════════════════════════════════════
# Metrics Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMetricsRegistry:
    def test_increment(self):
        m = MetricsRegistry()
        m.increment("requests")
        m.increment("requests")
        snap = m.snapshot()
        assert snap["counters"]["requests"] == 2.0

    def test_increment_with_tags(self):
        m = MetricsRegistry()
        m.increment("trades", tags={"trader": "kairos"})
        m.increment("trades", tags={"trader": "aldridge"})
        snap = m.snapshot()
        assert snap["counters"]["trades"] == 2.0
        assert len(snap["recent_events"]) == 2

    def test_gauge(self):
        m = MetricsRegistry()
        m.gauge("portfolio.value", 10587.50)
        snap = m.snapshot()
        assert snap["gauges"]["portfolio.value"] == 10587.50

    def test_gauge_overwrite(self):
        m = MetricsRegistry()
        m.gauge("portfolio.value", 100.0)
        m.gauge("portfolio.value", 200.0)
        snap = m.snapshot()
        assert snap["gauges"]["portfolio.value"] == 200.0

    def test_observe(self):
        m = MetricsRegistry()
        m.observe("latency", 100)
        m.observe("latency", 200)
        m.observe("latency", 300)
        snap = m.snapshot()
        h = snap["histograms"]["latency"]
        assert h["count"] == 3
        assert h["min"] == 100
        assert h["max"] == 300
        assert h["avg"] == 200

    def test_event(self):
        m = MetricsRegistry()
        m.event("backtest.completed", tags={"variant": "momentum"})
        snap = m.snapshot()
        assert len(snap["recent_events"]) == 1
        assert snap["recent_events"][0]["name"] == "backtest.completed"

    def test_snapshot_structure(self):
        m = MetricsRegistry()
        m.increment("test")
        snap = m.snapshot()
        assert "ts" in snap
        assert "counters" in snap
        assert "gauges" in snap
        assert "histograms" in snap
        assert "recent_events" in snap

    def test_json_serialization(self):
        m = MetricsRegistry()
        m.increment("test")
        json_str = m.json()
        parsed = json.loads(json_str)
        assert parsed["counters"]["test"] == 1.0

    def test_reset_events(self):
        m = MetricsRegistry()
        m.increment("test")
        assert len(m.snapshot()["recent_events"]) == 1
        m.reset_events()
        assert len(m.snapshot()["recent_events"]) == 0
        # Counter should persist
        assert m.snapshot()["counters"]["test"] == 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# Alert Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestAlertManager:
    def test_info_always_fires(self):
        a = AlertManager()
        a.info("test info", {"key": "val"})
        summary = a.summary()
        assert summary["p0_count"] == 0
        assert summary["p1_count"] == 0

    def test_p0_fires(self):
        a = AlertManager()
        a.p0("critical error", {"trader": "kairos"})
        summary = a.summary()
        assert summary["p0_count"] == 1
        assert "p0:critical error" in summary["last_alerts"]

    def test_p1_fires(self):
        a = AlertManager()
        a.p1("high latency", {"ms": 5000})
        summary = a.summary()
        assert summary["p1_count"] == 1

    def test_rate_limit_p0(self):
        a = AlertManager()
        a.p0("test alert")
        a.p0("test alert")  # Should be rate-limited
        summary = a.summary()
        assert summary["p0_count"] == 1  # Only first fired

    def test_different_alerts_not_rate_limited(self):
        a = AlertManager()
        a.p0("alert A")
        a.p0("alert B")
        summary = a.summary()
        assert summary["p0_count"] == 2

    def test_summary_structure(self):
        a = AlertManager()
        a.p0("critical", {"detail": "x"})
        a.p1("warning", {"detail": "y"})
        summary = a.summary()
        assert "p0_count" in summary
        assert "p1_count" in summary
        assert "last_alerts" in summary


# ═══════════════════════════════════════════════════════════════════════════════
# Telegram Tests (no network)
# ═══════════════════════════════════════════════════════════════════════════════


class TestTelegram:
    def test_alert_skipped_when_not_configured(self):
        """Telegram alerts should be no-ops when env vars are not set."""
        from src.observability.telegram import telegram_alert, _ENABLED
        assert not _ENABLED  # Not configured in test env
        result = telegram_alert("test", {"key": "val"})
        assert result is False  # Skipped cleanly

    def test_configure_at_runtime(self):
        """configure_telegram should enable sending."""
        import src.observability.telegram as tg
        assert not tg._ENABLED  # Not configured
        tg.configure_telegram("test:token", "test:chat")
        assert tg._ENABLED  # Now enabled
        tg.configure_telegram("", "")
        assert not tg._ENABLED  # Now disabled