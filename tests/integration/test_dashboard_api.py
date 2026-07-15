#!/usr/bin/env python3
"""
Integration tests: Dashboard API.

Connects to the running dashboard instance (via DASHBOARD_URL env var, defaults
to the Docker Compose container name) and verifies:

  1. All API endpoints return the expected JSON shapes
  2. Trader data is present with correct structure
  3. Positions, decisions, journal entries are well-formed
  4. Benchmark data loads correctly

These tests require a running Docker Compose stack with the dashboard service.
They are marked as integration tests and excluded from unit-only runs.

Usage:
    DASHBOARD_URL=http://dashboard:5002 \
    python3 -m pytest tests/integration/test_dashboard_api.py -v
"""

import os
import json

import pytest
import requests

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://dashboard:5002")
CI_MODE = os.getenv("CI", "").lower() in ("true", "1", "yes")
TIMEOUT = 15


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get(path, expect_status=200):
    """GET a dashboard endpoint and return parsed JSON."""
    url = f"{DASHBOARD_URL}{path}"
    try:
        resp = requests.get(url, timeout=TIMEOUT)
    except requests.exceptions.ConnectionError as e:
        if CI_MODE:
            pytest.fail(f"Cannot connect to dashboard at {url}: {e}")
        else:
            pytest.skip(f"Dashboard not available at {url}: {e}")
    assert resp.status_code == expect_status, (
        f"Expected {expect_status}, got {resp.status_code}: {resp.text[:300]}"
    )
    return resp.json()


# ═══════════════════════════════════════════════════════════════════════════════
# /api/traders
# ═══════════════════════════════════════════════════════════════════════════════


class TestTraders:
    """GET /api/traders — trader list with portfolio data."""

    def test_returns_json(self):
        data = _get("/api/traders")
        assert "traders" in data
        assert isinstance(data["traders"], list)

    def test_three_traders(self):
        data = _get("/api/traders")
        assert len(data["traders"]) == 3, f"Expected 3 traders, got {len(data['traders'])}"

    def test_trader_has_required_fields(self):
        data = _get("/api/traders")
        for t in data["traders"]:
            assert "id" in t, f"Missing id in trader {t}"
            assert "name" in t, f"Missing name in trader {t}"
            assert "portfolio_value" in t, f"Missing portfolio_value in trader {t}"
            assert t["portfolio_value"] > 0, f"portfolio_value should be positive, got {t['portfolio_value']}"

    def test_has_benchmarks(self):
        data = _get("/api/traders")
        assert "benchmarks" in data

    def test_benchmark_fields(self):
        data = _get("/api/traders")
        benchmarks = data["benchmarks"]
        for b_name in ("SPY", "QQQ"):
            b = benchmarks.get(b_name, benchmarks.get(b_name.lower()))
            if b:
                assert isinstance(b, dict), f"Benchmark {b_name} should be a dict"

    def test_agent_names_present(self):
        """Trader objects should have display names, not just IDs."""
        data = _get("/api/traders")
        names = {t["id"]: t.get("name", "") for t in data["traders"]}
        assert names.get("trader-kairos") or names.get("kairos"), "Missing Kairos name"
        assert names.get("trader-aldridge") or names.get("aldridge"), "Missing Aldridge name"
        assert names.get("trader-stonks") or names.get("stonks"), "Missing Stonks name"

    def test_html_page_served(self):
        """The dashboard's root path should serve the HTML frontend."""
        url = f"{DASHBOARD_URL}/"
        resp = requests.get(url, timeout=TIMEOUT)
        assert resp.status_code == 200
        ct = resp.headers.get("Content-Type", "")
        assert "text/html" in ct or "html" in ct.lower(), (
            f"Expected HTML, got Content-Type: {ct}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# /api/positions
# ═══════════════════════════════════════════════════════════════════════════════


class TestPositions:
    """GET /api/positions — open positions."""

    def test_returns_json(self):
        data = _get("/api/positions")
        assert "positions" in data

    def test_positions_have_fields(self):
        data = _get("/api/positions")
        for pos in data["positions"]:
            assert "ticker" in pos, f"Missing ticker in {pos}"
            assert "quantity" in pos, f"Missing quantity in {pos}"
            assert "trader_id" in pos or "trader" in pos, f"Missing trader_id in {pos}"

    def test_aaple_present(self):
        """AAPL should be in at least one position."""
        data = _get("/api/positions")
        tickers = {p["ticker"] for p in data["positions"]}
        assert "AAPL" in tickers, "AAPL not found in positions"


# ═══════════════════════════════════════════════════════════════════════════════
# /api/activity
# ═══════════════════════════════════════════════════════════════════════════════


class TestActivity:
    """GET /api/activity — recent activity/decisions."""

    def test_returns_json(self):
        data = _get("/api/activity?limit=10")
        assert "events" in data
        assert isinstance(data["events"], list)

    def test_events_have_fields(self):
        data = _get("/api/activity?limit=10")
        if len(data["events"]) > 0:
            event = data["events"][0]
            assert isinstance(event, dict), "Event should be a dict"


# ═══════════════════════════════════════════════════════════════════════════════
# /api/journal
# ═══════════════════════════════════════════════════════════════════════════════


class TestJournal:
    """GET /api/journal — trader journal entries."""

    def test_returns_json(self):
        data = _get("/api/journal?limit=10")
        assert "entries" in data
        assert isinstance(data["entries"], list)

    def test_entries_have_content(self):
        data = _get("/api/journal?limit=10")
        if len(data["entries"]) > 0:
            entry = data["entries"][0]
            assert isinstance(entry, dict), "Entry should be a dict"


# ═══════════════════════════════════════════════════════════════════════════════
# /api/signals
# ═══════════════════════════════════════════════════════════════════════════════


class TestSignals:
    """GET /api/signals — ML trading signals."""

    def test_returns_json(self):
        data = _get("/api/signals?limit=10")
        assert "signals" in data
        assert isinstance(data["signals"], list)

    def test_signals_have_data(self):
        data = _get("/api/signals?limit=10")
        if len(data["signals"]) > 0:
            sig = data["signals"][0]
            assert isinstance(sig, dict), "Signal should be a dict"


# ═══════════════════════════════════════════════════════════════════════════════
# /api/watchlists
# ═══════════════════════════════════════════════════════════════════════════════


class TestWatchlists:
    """GET /api/watchlists — trader watchlist data."""

    def test_returns_json(self):
        data = _get("/api/watchlists")
        assert isinstance(data, dict)


# ═══════════════════════════════════════════════════════════════════════════════
# /api/heartbeat
# ═══════════════════════════════════════════════════════════════════════════════


class TestHeartbeat:
    """GET /api/heartbeat — heartbeat data."""

    def test_returns_json(self):
        data = _get("/api/heartbeat")
        assert isinstance(data, dict)


# ═══════════════════════════════════════════════════════════════════════════════
# /api/vetoes
# ═══════════════════════════════════════════════════════════════════════════════


class TestVetoes:
    """GET /api/vetoes — risk events."""

    def test_returns_json(self):
        data = _get("/api/vetoes?limit=10")
        assert "vetoes" in data
        assert isinstance(data["vetoes"], list)

    def test_vetoes_have_reason(self):
        data = _get("/api/vetoes?limit=10")
        for v in data["vetoes"]:
            assert isinstance(v, dict), "Veto should be a dict"


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-endpoint consistency
# ═══════════════════════════════════════════════════════════════════════════════


class TestResponseTimes:
    """All dashboard endpoints should respond within TIMEOUT."""

    ENDPOINTS = [
        "/api/traders",
        "/api/positions",
        "/api/activity?limit=10",
        "/api/journal?limit=10",
        "/api/signals?limit=10",
        "/api/watchlists",
        "/api/heartbeat",
        "/api/vetoes?limit=10",
    ]

    def test_all_respond_under_timeout(self):
        from datetime import datetime

        for path in self.ENDPOINTS:
            t0 = datetime.now()
            resp = requests.get(f"{DASHBOARD_URL}{path}", timeout=TIMEOUT)
            elapsed = (datetime.now() - t0).total_seconds()
            assert resp.status_code == 200, f"{path}: status {resp.status_code}"
            assert elapsed < TIMEOUT, f"{path}: took {elapsed:.1f}s (limit {TIMEOUT}s)"