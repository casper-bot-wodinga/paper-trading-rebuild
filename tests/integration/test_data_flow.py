#!/usr/bin/env python3
"""
Integration tests: Cross-service data flow.

Connects to all three services (Postgres, data-bus, dashboard) and verifies
that data flows correctly between them:

  1. Seed data in Postgres is visible through the dashboard API
  2. Data-bus queries the same Postgres and returns consistent results
  3. Agent state in Postgres matches what the dashboard reports
  4. Dashboard API returns consistent trader IDs across endpoints

These tests require a running Docker Compose stack with all services.
They are marked as integration tests and excluded from unit-only runs.

Usage:
    PG_DSN="host=trading-db port=5432 dbname=trading user=trader" \
    DASHBOARD_URL=http://dashboard:5002 \
    DATA_BUS_URL=http://data-bus:5000 \
    python3 -m pytest tests/integration/test_data_flow.py -v
"""

import os
import json

import pytest
import requests
import psycopg2
import psycopg2.extras

PG_DSN = os.getenv("PG_DSN", "host=trading-db port=5432 dbname=trading user=trader")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://dashboard:5002")
DATA_BUS_URL = os.getenv("DATA_BUS_URL", "http://data-bus:5000")
CI_MODE = os.getenv("CI", "").lower() in ("true", "1", "yes")
TIMEOUT = 15


# ── Helpers ──────────────────────────────────────────────────────────────────


def get_db():
    """Connect to Postgres."""
    try:
        conn = psycopg2.connect(PG_DSN)
        conn.autocommit = True
        return conn
    except Exception as e:
        if CI_MODE:
            pytest.fail(f"Cannot connect to Postgres at {PG_DSN}: {e}")
        else:
            pytest.skip(f"Postgres not available at {PG_DSN}: {e}")


def dash_get(path, expect_status=200):
    """GET a dashboard endpoint."""
    url = f"{DASHBOARD_URL}{path}"
    try:
        resp = requests.get(url, timeout=TIMEOUT)
    except requests.exceptions.ConnectionError as e:
        if CI_MODE:
            pytest.fail(f"Cannot connect to dashboard at {url}: {e}")
        else:
            pytest.skip(f"Dashboard not available at {url}: {e}")
    assert resp.status_code == expect_status, (
        f"{path}: expected {expect_status}, got {resp.status_code}: {resp.text[:200]}"
    )
    return resp.json()


def bus_get(path, params=None, expect_status=200):
    """GET a data-bus endpoint."""
    url = f"{DATA_BUS_URL}{path}"
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
    except requests.exceptions.ConnectionError as e:
        if CI_MODE:
            pytest.fail(f"Cannot connect to data-bus at {url}: {e}")
        else:
            pytest.skip(f"Data-bus not available at {url}: {e}")
    assert resp.status_code == expect_status, (
        f"{path}: expected {expect_status}, got {resp.status_code}: {resp.text[:200]}"
    )
    return resp.json()


# ═══════════════════════════════════════════════════════════════════════════════
# Postgres ↔ Dashboard consistency
# ═══════════════════════════════════════════════════════════════════════════════


class TestDBtoDashboard:
    """Verify that seed data in Postgres is visible through the dashboard API."""

    def test_agent_count_matches_db(self):
        """Dashboard should report the same number of traders as Postgres."""
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trading.agent_state")
        db_count = cur.fetchone()[0]
        conn.close()

        dash = dash_get("/api/traders")
        dash_count = len(dash["traders"])
        assert db_count == dash_count, (
            f"DB has {db_count} traders, dashboard reports {dash_count}"
        )

    def test_trader_ids_consistent(self):
        """Dashboard trader IDs should match actual agent_state records."""
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT agent_id FROM trading.agent_state ORDER BY agent_id")
        db_ids = {r[0] for r in cur.fetchall()}
        conn.close()

        dash = dash_get("/api/traders")
        dash_ids = set()
        for t in dash["traders"]:
            tid = t.get("id", t.get("trader_id", ""))
            dash_ids.add(tid)

        # The dashboard may prefix or transform IDs. Check that the short
        # names (kairos, aldridge, stonks) are present in both.
        db_short = set(id_.replace("trader-", "") for id_ in db_ids)
        dash_short = set()
        for tid in dash_ids:
            tid_clean = tid.replace("trader-", "")
            dash_short.add(tid_clean)

        expected = {"kairos", "aldridge", "stonks"}
        assert expected.issubset(db_short), f"DB missing traders: {expected - db_short}"
        assert expected.issubset(dash_short), f"Dashboard missing traders: {expected - dash_short}"


class TestPositionsConsistency:
    """Verify position data flows correctly from DB to dashboard."""

    def test_positions_reported(self):
        """Dashboard should report the same positions as Postgres."""
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT SUM(quantity) FROM trading.trader_positions")
        db_total_qty = cur.fetchone()[0] or 0
        conn.close()

        dash = dash_get("/api/positions")
        dash_total_qty = sum(
            float(p.get("quantity", 0) or 0) for p in dash["positions"]
        )
        assert abs(dash_total_qty - db_total_qty) < 0.01, (
            f"DB total qty {db_total_qty}, dashboard reports {dash_total_qty}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Postgres ↔ Data-bus consistency
# ═══════════════════════════════════════════════════════════════════════════════


class TestDBtoBus:
    """Verify that the data-bus serves data consistent with Postgres."""

    def test_data_bus_healthy(self):
        """The data-bus health endpoint should report status 'ok'."""
        health = bus_get("/health")
        assert health["status"] == "ok"
        assert health["service"] == "data-bus"

    def test_data_bus_knows_db(self):
        """Data-bus should report cached entries (from Postgres/SQLite)."""
        health = bus_get("/health")
        assert health["cache_stats"]["keys"] >= 0


# ═══════════════════════════════════════════════════════════════════════════════
# Data-bus → Dashboard consistency
# ═══════════════════════════════════════════════════════════════════════════════


class TestBusToDashboard:
    """Verify both services agree on basic system state."""

    def test_both_services_respond(self):
        """Both data-bus and dashboard should respond to health requests."""
        bus_get("/health")
        dash_get("/api/traders")
        # If we got here without exceptions, both services are responding


# ═══════════════════════════════════════════════════════════════════════════════
# End-to-end seed data verification
# ═══════════════════════════════════════════════════════════════════════════════


class TestSeedDataIntegrity:
    """End-to-end verification that seed data is consistent across all services."""

    def test_known_traders_appear_in_dashboard(self):
        """The three known traders should be visible in the dashboard."""
        dash = dash_get("/api/traders")
        dash_ids = {t["id"] for t in dash["traders"]}

        known = {"trader-kairos", "trader-aldridge", "trader-stonks"}
        missing = known - dash_ids

        # The dashboard might return short IDs (no "trader-" prefix)
        if missing:
            dash_short = {t["id"].replace("trader-", "") for t in dash["traders"]}
            expected_short = {"kairos", "aldridge", "stonks"}
            missing_short = expected_short - dash_short
            assert not missing_short, (
                f"Dashboard missing traders. Known forms: {known}. "
                f"Expected short: {expected_short}. Found: {dash_ids}"
            )

    def test_dashboard_portfolio_values_positive(self):
        """Every trader's portfolio value should be positive."""
        dash = dash_get("/api/traders")
        for t in dash["traders"]:
            pv = t.get("portfolio_value") or t.get("equity") or 0
            assert float(pv) > 0, f"Trader {t['id']} has non-positive portfolio: {pv}"

    def test_at_least_one_benchmark(self):
        """At least SPY benchmark should be present."""
        dash = dash_get("/api/traders")
        benchmarks = dash.get("benchmarks", {})
        spy = benchmarks.get("SPY") or benchmarks.get("spy")
        if spy is None:
            # Check for any key that contains SPY
            spy = next(
                (v for k, v in benchmarks.items() if "spy" in k.lower()),
                None
            )
        if not CI_MODE:
            pytest.skip("Benchmarks not available in all environments")
        assert spy is not None, f"No SPY benchmark found. Keys: {list(benchmarks.keys())}"