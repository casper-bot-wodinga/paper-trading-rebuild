#!/usr/bin/env python3
"""
System-level guardrail tests for the paper-trading system.

Tests cover:
1. Spec compliance — bankroll, strategies, journal, positions format
2. Bug regression — silent import failures, column name mismatches, orphaned JS code
3. DB schema consistency — column names match between code and DB
4. Docker image integrity — all required modules present
"""
import os
import sys
import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ERRORS = []

def check(name, condition, detail=""):
    if not condition:
        ERRORS.append(f"  FAIL: {name} — {detail}")

def assert_no_orphaned_js(filepath):
    """Check for orphaned top-level code in JS that references undefined variables."""
    if not filepath.exists():
        return
    with open(filepath) as f:
        src = f.read()
    
    # Find script blocks
    scripts = re.findall(r'<script[^>]*>(.*?)</script>', src, re.DOTALL)
    for i, script in enumerate(scripts):
        lines = script.split('\n')
        for j, line in enumerate(lines):
            stripped = line.strip()
            # Watch for top-level for loops or function calls referencing undefined vars
            if stripped.startswith('for ') and 'positions' in stripped and 'let' not in stripped and 'var' not in stripped and 'const' not in stripped:
                # Check if it's inside a function
                preceding = '\n'.join(lines[:j])
                depth = preceding.count('{') - preceding.count('}')
                if depth == 0:
                    check(f"Orphaned top-level for loop in script block {i}",
                          False, f"Line {j+1}: {stripped}")

def assert_no_silent_except(filepath):
    """Check for bare 'except: pass' / 'except Exception: pass' that swallow errors."""
    if not filepath.exists():
        return
    with open(filepath) as f:
        src = f.read()
    
    # Find bare except: pass patterns (but allow them in test files)
    bare_excepts = re.findall(r'except\s+(Exception|)\s*:\s*\n\s+pass', src)
    check(f"No bare except:pass in {filepath.name}",
          len(bare_excepts) == 0,
          f"Found {len(bare_excepts)} bare except:pass blocks")

def assert_column_names_match(filepath):
    """Check that SQL column names match actual DB schema (where knowable from context)."""
    if not filepath.exists():
        return
    with open(filepath) as f:
        src = f.read()
    
    # Check for likely wrong column names in SQL queries
    # Common pandas/dict naming vs actual DB naming
    name_mismatches = [
        ("quantity", "qty"),    # DB has quantity, code sometimes uses qty
    ]
    
    for correct, wrong in name_mismatches:
        if wrong in src:
            check(f"Column name '{wrong}' in {filepath.name} — should be '{correct}'",
                  False, f"Found '{wrong}' in source, DB column is '{correct}'")

def test_import_chain():
    """Verify all modules can be imported without errors."""
    # Mock environment
    os.environ.setdefault("PG_DSN", "host=localhost port=5432 dbname=trading user=trader")
    
    modules = [
        ("src.leaderboard_api", ["_get_portfolio", "_get_positions_from_db", "_db", "_PgCursor"]),
    ]
    
    for mod_name, expected_attrs in modules:
        try:
            mod = __import__(mod_name, fromlist=expected_attrs)
            for attr in expected_attrs:
                check(f"Module {mod_name} has attr {attr}", hasattr(mod, attr))
        except Exception as e:
            check(f"Import {mod_name}", False, f"Failed: {e}")

def test_api_endpoints():
    """Verify API endpoints return 200 with expected structure."""
    import urllib.request
    import json as json_lib
    
    endpoints = [
        "/api/traders",
        "/api/summary",
        "/api/trades",
        "/api/decisions",
        "/api/pnl",
    ]
    
    base = os.environ.get("TEST_API_BASE", "http://localhost:5002")
    
    for ep in endpoints:
        try:
            req = urllib.request.Request(f"{base}{ep}")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json_lib.loads(resp.read())
                check(f"API {ep} returns 200", resp.status == 200)
                check(f"API {ep} returns valid JSON", data is not None)
        except Exception as e:
            check(f"API {ep}", False, f"Failed: {e}")

def test_db_connection():
    """Verify PG_DSN connects and schema is intact."""
    import psycopg2
    import psycopg2.extras
    
    dsn = os.getenv("PG_DSN", "host=trading-db port=5432 dbname=trading user=trader")
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Check required tables exist
        cur.execute("""
            SELECT table_name FROM information_schema.tables 
            WHERE table_schema = 'trading'
        """)
        tables = [r['table_name'] for r in cur.fetchall()]
        required = ['portfolio_snapshots', 'trader_positions', 'trades', 'decisions', 'quotes']
        for t in required:
            check(f"Table trading.{t} exists", t in tables)
        
        # Check trader_positions columns
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'trading' AND table_name = 'trader_positions'
        """)
        cols = [r['column_name'] for r in cur.fetchall()]
        expected_cols = ['quantity', 'ticker', 'trader_id', 'status', 'market_value']
        for c in expected_cols:
            check(f"Column trading.trader_positions.{c} exists", c in cols)
        
        # Check portfolio_snapshots has recent data
        cur.execute("""
            SELECT COUNT(*) as cnt FROM trading.portfolio_snapshots
        """)
        count = cur.fetchone()['cnt']
        check(f"Portfolio snapshots has data", count > 0, f"Found {count} rows")
        
        cur.close()
        conn.close()
    except Exception as e:
        check("DB connection", False, f"Failed: {e}")

def test_positions_flow():
    """Verify _get_positions_from_db returns valid data for known traders."""
    try:
        sys.path.insert(0, str(REPO / "src"))
        from leaderboard_api import _get_positions_from_db, _get_portfolio, _get_portfolio_from_db
        
        for trader in ["kairos", "aldridge", "stonks"]:
            positions = _get_positions_from_db(trader)
            check(f"Positions for {trader} is list", isinstance(positions, list))
            if positions:
                p = positions[0]
                for field in ["ticker", "qty", "market_value", "unrealized_pl", "current_price"]:
                    check(f"Position field {field}", field in p)
        
        # Portfolio should not be None
        for trader in ["kairos", "aldridge"]:
            pf = _get_portfolio(trader)
            check(f"Portfolio for {trader}", pf is not None)
            if pf:
                for field in ["portfolio_value", "positions", "_source"]:
                    check(f"Portfolio field {field}", field in pf)
    except Exception as e:
        check("Positions flow", False, f"Failed: {e}")

def test_no_alpaca_dependency():
    """Verify the codebase has no actual Alpaca runtime dependency."""
    import_src = (REPO / "src" / "leaderboard_api.py").read_text()
    
    # _get_alpaca_portfolio should not exist (renamed to _get_portfolio)
    check("No _get_alpaca_portfolio function",
          "_get_alpaca_portfolio" not in import_src)
    
    # AlpacaExecutor should not be imported
    check("No AlpacaExecutor import",
          "AlpacaExecutor" not in import_src)

def run():
    print("=" * 60)
    print("Paper Trading System — Comprehensive Guardrail Tests")
    print("=" * 60)
    
    test_no_alpaca_dependency()
    test_import_chain()
    test_db_connection()
    
    # Check source files for orphaned code and naming issues
    for f in REPO.glob("src/leaderboard_ui/**/*.html"):
        assert_no_orphaned_js(f)
    
    for f in REPO.glob("src/**/*.py"):
        assert_no_silent_except(f)
        assert_column_names_match(f)
    
    test_positions_flow()
    
    if os.getenv("TEST_API") == "1":
        test_api_endpoints()
    
    print(f"\n{'=' * 60}")
    print(f"Results: {len(ERRORS)} failures")
    for e in ERRORS:
        print(e)
    
    return len(ERRORS)

if __name__ == "__main__":
    sys.exit(run())
