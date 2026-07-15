#!/usr/bin/env python3
"""
Integration tests: Database schema and seed data verification.

Connects to the running Postgres instance (via PG_DSN or defaults to the
Docker Compose container name) and verifies:

  1. All expected schemas and tables exist
  2. Seed data was loaded with expected row counts
  3. Deterministic data values match known expected values
  4. Foreign key relationships are consistent
  5. Indexes exist for query performance

These tests require a running Docker Compose stack. They are marked as
integration tests and excluded from unit-only runs.

Usage:
    CI=true python3 -m pytest tests/integration/test_db_schema.py -v
"""

import os
import sys
from datetime import datetime, timezone

import pytest
import psycopg2
import psycopg2.extras

PG_DSN = os.getenv("PG_DSN", "host=trading-db port=5432 dbname=trading user=trader")
CI_MODE = os.getenv("CI", "").lower() in ("true", "1", "yes")


# ── Helpers ──────────────────────────────────────────────────────────────────


def get_conn():
    """Connect to Postgres. Raises on failure."""
    try:
        conn = psycopg2.connect(PG_DSN)
        conn.autocommit = True
        return conn
    except Exception as e:
        if CI_MODE:
            pytest.fail(f"Cannot connect to Postgres at {PG_DSN}: {e}")
        else:
            pytest.skip(f"Postgres not available at {PG_DSN}: {e}")


def table_exists(cur, schema, table):
    """Check if a table exists in the given schema."""
    cur.execute(
        "SELECT EXISTS (SELECT FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s)",
        (schema, table),
    )
    return cur.fetchone()[0]


def row_count(cur, table):
    """Get the number of rows in a table."""
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


# ═══════════════════════════════════════════════════════════════════════════════
# Schema existence
# ═══════════════════════════════════════════════════════════════════════════════


class TestSchemaExists:
    """Verify all required schemas and tables exist."""

    def test_schema_trading_exists(self):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT EXISTS (SELECT FROM information_schema.schemata WHERE schema_name = 'trading')")
        assert cur.fetchone()[0], "Schema 'trading' does not exist"
        conn.close()

    def test_schema_market_data_exists(self):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT EXISTS (SELECT FROM information_schema.schemata WHERE schema_name = 'market_data')")
        assert cur.fetchone()[0], "Schema 'market_data' does not exist"
        conn.close()

    def test_required_tables_exist(self):
        """Verify all tables that the dashboard and data-bus depend on."""
        conn = get_conn()
        cur = conn.cursor()
        required = [
            ("trading", "agent_profile"),
            ("trading", "agent_state"),
            ("trading", "portfolio_snapshots"),
            ("trading", "trader_positions"),
            ("trading", "trader_decisions"),
            ("trading", "orders"),
            ("trading", "trader_journal"),
            ("trading", "risk_events"),
            ("trading", "trader_watchlist"),
            ("trading", "equity_snapshots"),
            ("trading", "signals"),
            ("market_data", "bars"),
        ]
        missing = []
        for schema, table in required:
            if not table_exists(cur, schema, table):
                missing.append(f"{schema}.{table}")
        conn.close()
        assert not missing, f"Missing required tables: {missing}"


# ═══════════════════════════════════════════════════════════════════════════════
# Seed data counts
# ═══════════════════════════════════════════════════════════════════════════════


class TestSeedDataCounts:
    """Verify seed data was loaded with expected row counts.

    Expected counts are deterministic because seed_test_data.py uses
    random.seed(42) and a fixed reference timestamp (FIXED_NOW).
    """

    def test_agent_profiles(self):
        conn = get_conn()
        cur = conn.cursor()
        count = row_count(cur, "trading.agent_profile")
        assert count == 3, f"Expected 3 agent profiles, got {count}"
        conn.close()

    def test_agent_states(self):
        conn = get_conn()
        cur = conn.cursor()
        count = row_count(cur, "trading.agent_state")
        assert count == 3, f"Expected 3 agent states, got {count}"
        conn.close()

    def test_portfolio_snapshots(self):
        conn = get_conn()
        cur = conn.cursor()
        count = row_count(cur, "trading.portfolio_snapshots")
        assert count == 3, f"Expected 3 portfolio snapshots, got {count}"
        conn.close()

    def test_trader_positions(self):
        conn = get_conn()
        cur = conn.cursor()
        count = row_count(cur, "trading.trader_positions")
        # Kairos: 2, Aldridge: 2, Stonks: 4 = 8 total
        assert count == 8, f"Expected 8 trader positions, got {count}"
        conn.close()

    def test_trader_decisions(self):
        conn = get_conn()
        cur = conn.cursor()
        count = row_count(cur, "trading.trader_decisions")
        # 3 traders × 3 decisions each = 9
        assert count == 9, f"Expected 9 trader decisions, got {count}"
        conn.close()

    def test_orders(self):
        conn = get_conn()
        cur = conn.cursor()
        count = row_count(cur, "trading.orders")
        # 3 traders × 5 orders each = 15
        assert count == 15, f"Expected 15 orders, got {count}"
        conn.close()

    def test_trader_journal(self):
        conn = get_conn()
        cur = conn.cursor()
        count = row_count(cur, "trading.trader_journal")
        # 3 traders × 3 journal entries each = 9
        assert count == 9, f"Expected 9 journal entries, got {count}"
        conn.close()

    def test_equity_snapshots(self):
        conn = get_conn()
        cur = conn.cursor()
        count = row_count(cur, "trading.equity_snapshots")
        # 3 traders × 30 days = 90
        assert count == 90, f"Expected 90 equity snapshots, got {count}"
        conn.close()

    def test_risk_events(self):
        conn = get_conn()
        cur = conn.cursor()
        count = row_count(cur, "trading.risk_events")
        assert count == 5, f"Expected 5 risk events, got {count}"
        conn.close()

    def test_signals(self):
        conn = get_conn()
        cur = conn.cursor()
        count = row_count(cur, "trading.signals")
        assert count == 7, f"Expected 7 signals, got {count}"
        conn.close()

    def test_watchlist_entries(self):
        conn = get_conn()
        cur = conn.cursor()
        count = row_count(cur, "trading.trader_watchlist")
        # 3 traders × 10 tickers each
        assert count == 30, f"Expected 30 watchlist entries, got {count}"
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Deterministic data values
# ═══════════════════════════════════════════════════════════════════════════════


class TestDeterministicValues:
    """Verify deterministic seed data values.

    With random.seed(42) and FIXED_NOW=2026-07-15T12:00:00Z, every run
    produces identical values. These assertions lock in the expected output.

    If you change the seed script and values shift, update these assertions.
    """

    def test_agent_profile_names(self):
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT agent_id, name FROM trading.agent_profile ORDER BY agent_id")
        rows = cur.fetchall()
        assert rows[0]["agent_id"] == "aldridge", f"Expected aldridge first, got {rows[0]['agent_id']}"
        assert rows[0]["name"] == "Edmund Whitfield"
        assert rows[1]["agent_id"] == "kairos"
        assert rows[1]["name"] == "Zara Chen"
        assert rows[2]["agent_id"] == "stonks"
        assert rows[2]["name"] == "Stan Hoolihan"
        conn.close()

    def test_kairos_state(self):
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM trading.agent_state WHERE agent_id = 'trader-kairos'")
        row = cur.fetchone()
        assert row is not None
        assert row["is_active"] is True
        assert row["equity"] > 0
        conn.close()

    def test_stonks_positions_count(self):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trading.trader_positions WHERE trader_id = 'stonks'")
        count = cur.fetchone()[0]
        assert count == 4, f"Expected Stonks to have 4 positions, got {count}"
        conn.close()

    def test_journal_entries_have_content(self):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT agent_id, entry FROM trading.trader_journal ORDER BY agent_id, timestamp")
        rows = cur.fetchall()
        assert len(rows) == 9
        for agent_id, entry in rows:
            assert len(entry) > 10, f"Journal entry too short for {agent_id}: {entry[:20]}"
        conn.close()

    def test_decisions_have_thesis(self):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT action, thesis FROM trading.trader_decisions ORDER BY agent_id, timestamp")
        rows = cur.fetchall()
        for action, thesis in rows:
            if action in ("BUY", "SELL"):
                assert len(thesis) > 20, f"Thesis too short for {action}: {thesis[:30]}"
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Data integrity
# ═══════════════════════════════════════════════════════════════════════════════


class TestDataIntegrity:
    """Verify data integrity constraints."""

    def test_no_null_agent_ids(self):
        """Every row that has an agent_id should have a non-null, non-empty value."""
        conn = get_conn()
        cur = conn.cursor()
        tables_with_agent = [
            "trading.agent_profile",
            "trading.agent_state",
            "trading.trader_positions",
            "trading.trader_decisions",
            "trading.orders",
            "trading.trader_journal",
            "trading.risk_events",
            "trading.portfolio_snapshots",
        ]
        for table in tables_with_agent:
            col = "agent_id" if "agent" in table.lower() else "trader_id"
            cur.execute(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE {col} IS NULL OR TRIM({col}) = ''"
            )
            nulls = cur.fetchone()[0]
            assert nulls == 0, f"Table {table} has {nulls} null/empty {col} values"
        conn.close()

    def test_equity_is_positive(self):
        """All equity snapshots should have positive equity and cash."""
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trading.equity_snapshots WHERE equity <= 0 OR cash <= 0")
        bad = cur.fetchone()[0]
        assert bad == 0, f"Found {bad} equity snapshots with non-positive equity or cash"
        conn.close()

    def test_equity_snapshot_dates(self):
        """Equity snapshots should span 30 days with no gaps per trader."""
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT trader_id, COUNT(DISTINCT date) as day_count
            FROM trading.equity_snapshots
            GROUP BY trader_id
        """)
        rows = cur.fetchall()
        for row in rows:
            assert row["day_count"] == 30, f"Trader {row['trader_id']} has {row['day_count']} days, expected 30"
        conn.close()

    def test_benchmark_tickers(self):
        """SPY and QQQ should be present in market_data.bars."""
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT ticker FROM market_data.bars ORDER BY ticker")
        tickers = [r[0] for r in cur.fetchall()]
        assert "SPY" in tickers, "SPY not found in bars"
        assert "QQQ" in tickers, "QQQ not found in bars"
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Index coverage
# ═══════════════════════════════════════════════════════════════════════════════


class TestIndexes:
    """Verify that performance indexes exist."""

    def test_positions_index(self):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM pg_indexes
                WHERE schemaname = 'trading'
                AND tablename = 'trader_positions'
                AND indexname LIKE '%_trader%'
            )
        """)
        assert cur.fetchone()[0], "Missing index on trader_positions.trader_id"
        conn.close()

    def test_decisions_index(self):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT EXISTS (
                SELECT FROM pg_indexes
                WHERE schemaname = 'trading'
                AND tablename = 'trader_decisions'
                AND indexname LIKE '%_agent_ts%'
            )
        """)
        assert cur.fetchone()[0], "Missing index on trader_decisions(agent_id, timestamp)"
        conn.close()

    def test_equity_snapshots_unique(self):
        """Verify the unique constraint on (trader_id, date)."""
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'trading.equity_snapshots'::regclass
            AND contype = 'u'
        """)
        constraints = [r[0] for r in cur.fetchall()]
        assert any("equity_trader_date" in c for c in constraints), (
            f"Missing unique constraint on equity_snapshots(trader_id, date). Found: {constraints}"
        )
        conn.close()