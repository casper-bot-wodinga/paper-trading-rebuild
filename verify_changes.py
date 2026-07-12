#!/usr/bin/env python3
"""Verify the implementation changes are in place and the data bus is responding."""
import json
import urllib.request
import sys
import os
from pathlib import Path

checks = {
    "bars_endpoint_in_databus": False,
    "api_findings_in_leaderboard": False,
    "historical_sim_exists": False,
    "bar_loader_has_databus_method": False,
    "data_bus_responding": False,
    "bars_endpoint_responding": False,
}

# 1. Check data_bus.py for bars_endpoint
db_path = Path("/home/openclaw/projects/paper-trading-rebuild/src/data_bus.py")
if db_path.exists():
    content = db_path.read_text()
    checks["bars_endpoint_in_databus"] = "def bars_endpoint" in content

# 2. Check leaderboard_api.py for api_findings
lb_path = Path("/home/openclaw/projects/paper-trading-rebuild/src/leaderboard_api.py")
if lb_path.exists():
    content = lb_path.read_text()
    checks["api_findings_in_leaderboard"] = "def api_findings" in content

# 3. Check historical_sim.py exists
hs_path = Path("/home/openclaw/projects/paper-trading-rebuild/src/historical_sim.py")
checks["historical_sim_exists"] = hs_path.exists() and hs_path.stat().st_size > 1000

# 4. Check bar_loader.py has databus method
bl_path = Path("/home/openclaw/projects/paper-trading-rebuild/src/bar_loader.py")
if bl_path.exists():
    content = bl_path.read_text()
    checks["bar_loader_has_databus_method"] = "_load_from_databus" in content

# 5. Check data bus is responding
try:
    resp = urllib.request.urlopen("http://192.168.1.41:5000/health", timeout=5)
    checks["data_bus_responding"] = resp.status == 200
except Exception:
    pass

# 6. Check bars endpoint
try:
    resp = urllib.request.urlopen(
        "http://192.168.1.41:5000/bars?symbols=AAPL&interval=daily&start_date=2026-06-01&end_date=2026-07-02",
        timeout=10,
    )
    checks["bars_endpoint_responding"] = resp.status == 200
    if resp.status == 200:
        data = json.loads(resp.read())
        checks["AAPL_found"] = "AAPL" in data.get("symbols", {})
        checks["bar_count_AAPL"] = len(data.get("symbols", {}).get("AAPL", []))
except Exception as e:
    checks["bars_error"] = str(e)

# Print results
all_ok = all(v for k, v in checks.items() if not k.startswith("bar_count") and not k.startswith("AAPL") and k != "bars_error")
print("=" * 60)
print("  VERIFICATION REPORT")
print("=" * 60)
for k, v in checks.items():
    status = "✅" if v else "❌"
    print(f"  {status}  {k}: {v}")
print("=" * 60)
if all_ok:
    print("  ✅ ALL CHECKS PASSED")
else:
    print("  ❌ SOME CHECKS FAILED")
print("=" * 60)

# Write results to file
out = Path("/home/openclaw/projects/paper-trading-rebuild/state/verification.json")
out.write_text(json.dumps(checks, indent=2))
print(f"\n  Results written to {out}")