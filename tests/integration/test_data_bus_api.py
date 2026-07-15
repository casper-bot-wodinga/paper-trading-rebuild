#!/usr/bin/env python3
"""
Integration tests: Data Bus API.

Connects to the running data-bus instance (via DATA_BUS_URL env var, defaults
to the Docker Compose container name) and verifies:

  1. Health endpoint returns expected structure
  2. Key endpoints respond and return valid JSON
  3. Response schemas match expected shapes
  4. Data bus is healthy with schedulers running

These tests require a running Docker Compose stack with the data-bus service.
They are marked as integration tests and excluded from unit-only runs.

Usage:
    DATA_BUS_URL=http://data-bus:5000 \
    python3 -m pytest tests/integration/test_data_bus_api.py -v
"""

import os
import json

import pytest
import requests

DATA_BUS_URL = os.getenv("DATA_BUS_URL", "http://data-bus:5000")
CI_MODE = os.getenv("CI", "").lower() in ("true", "1", "yes")
TIMEOUT = 15


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get(path, params=None, expect_status=200):
    """GET a data-bus endpoint and return parsed JSON."""
    url = f"{DATA_BUS_URL}{path}"
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
    except requests.exceptions.ConnectionError as e:
        if CI_MODE:
            pytest.fail(f"Cannot connect to data-bus at {url}: {e}")
        else:
            pytest.skip(f"Data-bus not available at {url}: {e}")
    assert resp.status_code == expect_status, (
        f"Expected {expect_status}, got {resp.status_code}: {resp.text[:300]}"
    )
    return resp.json()


# ═══════════════════════════════════════════════════════════════════════════════
# /health
# ═══════════════════════════════════════════════════════════════════════════════


class TestHealth:
    """GET /health — liveness and scheduler state."""

    def test_status_ok(self):
        data = _get("/health")
        assert data["status"] == "ok"
        assert data["service"] == "data-bus"

    def test_has_uptime(self):
        data = _get("/health")
        assert data["uptime_seconds"] > 0

    def test_has_cache_stats(self):
        data = _get("/health")
        assert "cache_stats" in data
        assert "keys" in data["cache_stats"]
        assert isinstance(data["cache_stats"]["keys"], int)

    def test_has_schedulers(self):
        data = _get("/health")
        assert "schedulers" in data
        assert isinstance(data["schedulers"], list)
        # Should have at least some schedulers registered, even if in "off" mode
        assert len(data["schedulers"]) > 0

    def test_scheduler_schema(self):
        data = _get("/health")
        for s in data["schedulers"]:
            assert "name" in s
            assert "interval" in s
            assert "mode" in s
            assert s["mode"] in ("off", "market", "always")

    def test_tracked_symbols(self):
        data = _get("/health")
        assert isinstance(data["tracked_symbols"], int)


# ═══════════════════════════════════════════════════════════════════════════════
# /quotes
# ═══════════════════════════════════════════════════════════════════════════════


class TestQuotes:
    """GET /quotes — quote data."""

    def test_returns_valid_json(self):
        data = _get("/quotes", {"symbols": "AAPL"})
        assert "quotes" in data

    def test_quote_has_fields(self):
        data = _get("/quotes", {"symbols": "AAPL"})
        quotes = data["quotes"]
        assert "AAPL" in quotes
        q = quotes["AAPL"]
        for field in ("close", "volume", "high", "low", "open", "source"):
            assert field in q, f"Missing field: {field}"

    def test_meta_fields(self):
        data = _get("/quotes", {"symbols": "AAPL"})
        assert isinstance(data["cached"], int)
        assert isinstance(data["fetched_live"], int)


# ═══════════════════════════════════════════════════════════════════════════════
# /discover
# ═══════════════════════════════════════════════════════════════════════════════


class TestDiscover:
    """GET /discover — endpoint enumeration."""

    def test_returns_endpoints(self):
        data = _get("/discover")
        assert isinstance(data, dict)
        assert len(data) > 0, "Expected at least one endpoint in discover"

    def test_health_in_discover(self):
        data = _get("/discover")
        endpoints = [k.lower() for k in data.keys()]
        assert any("health" in e for e in endpoints), "/health not found in discover"


# ═══════════════════════════════════════════════════════════════════════════════
# /signals
# ═══════════════════════════════════════════════════════════════════════════════


class TestSignals:
    """GET /signals — trading signals."""

    def test_returns_json(self):
        data = _get("/signals")
        assert "signals" in data
        assert isinstance(data["signals"], list)
        assert "count" in data
        assert data["count"] == len(data["signals"])


# ═══════════════════════════════════════════════════════════════════════════════
# /fear_greed
# ═══════════════════════════════════════════════════════════════════════════════


class TestFearGreed:
    """GET /fear_greed — fear & greed index."""

    def test_returns_json(self):
        data = _get("/fear_greed")
        assert "fear_greed" in data
        fg = data["fear_greed"]
        assert "value" in fg
        assert isinstance(fg["value"], (int, float))
        assert "classification" in fg
        assert isinstance(fg["classification"], str)


# ═══════════════════════════════════════════════════════════════════════════════
# /macro
# ═══════════════════════════════════════════════════════════════════════════════


class TestMacro:
    """GET /macro — FRED macro indicators."""

    def test_returns_json(self):
        # With NO_API_KEYS, this may return empty indicators — that's OK
        data = _get("/macro")
        assert "macro" in data
        assert "indicators" in data["macro"]
        assert isinstance(data["macro"]["indicators"], dict)

    def test_yields_present(self):
        data = _get("/macro")
        macro = data["macro"]
        # Yields may be empty dict without API keys — check structure only
        assert "yields" in macro or "spread_10y2y" in macro


# ═══════════════════════════════════════════════════════════════════════════════
# /source-quality
# ═══════════════════════════════════════════════════════════════════════════════


class TestSourceQuality:
    """GET /source-quality — data source health."""

    def test_returns_json(self):
        data = _get("/source-quality")
        assert "sources" in data
        assert isinstance(data["sources"], list)


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-endpoint consistency
# ═══════════════════════════════════════════════════════════════════════════════


class TestContentType:
    """Every endpoint must return JSON Content-Type."""

    ENDPOINTS = [
        "/health",
        "/discover",
        "/quotes?symbols=AAPL",
        "/signals",
        "/fear_greed",
        "/macro",
        "/source-quality",
    ]

    def test_all_endpoints_return_json(self):
        for path in self.ENDPOINTS:
            url = f"{DATA_BUS_URL}{path}"
            try:
                resp = requests.get(url, timeout=TIMEOUT)
            except requests.exceptions.ConnectionError:
                if CI_MODE:
                    pytest.fail(f"Cannot connect to data-bus at {url}")
                else:
                    pytest.skip(f"Data-bus not available at {url}")
            ct = resp.headers.get("Content-Type", "")
            assert "application/json" in ct, (
                f"{path}: expected JSON Content-Type, got '{ct}'"
            )
            # Must parse as valid JSON
            resp.json()


class TestResponseTime:
    """All endpoints should respond within TIMEOUT."""

    ENDPOINTS = [
        "/health",
        "/discover",
        "/quotes?symbols=AAPL",
        "/signals",
        "/fear_greed",
        "/macro",
        "/source-quality",
    ]

    def test_all_respond_under_timeout(self):
        from datetime import datetime

        for path in self.ENDPOINTS:
            t0 = datetime.now()
            try:
                resp = requests.get(f"{DATA_BUS_URL}{path}", timeout=TIMEOUT)
            except requests.exceptions.ConnectionError:
                continue
            elapsed = (datetime.now() - t0).total_seconds()
            assert resp.status_code in (200, 404, 503), (
                f"{path}: unexpected status {resp.status_code}"
            )
            assert elapsed < TIMEOUT, (
                f"{path}: took {elapsed:.1f}s (limit {TIMEOUT}s)"
            )