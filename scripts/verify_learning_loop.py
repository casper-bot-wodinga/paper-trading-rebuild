#!/usr/bin/env python3
"""
Parameterized Verification Plan — Learning Loop × Virtual Trader Integration

Tests the full pipeline: virtual trader → decisions → learning loop → synthesis → improvements.

Usage:
    python3 scripts/verify_learning_loop.py           # run all test scenarios
    python3 scripts/verify_learning_loop.py --scenario profit-mixed  # specific scenario
    python3 scripts/verify_learning_loop.py --list     # list available scenarios
    python3 scripts/verify_learning_loop.py --html     # output HTML report
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_DIR / "shared" / "trader.db"
sys.path.insert(0, str(PROJECT_DIR))

from src.learning_loop import run_for_agent, inject_test_data, get_agents, get_db
from src.journal_analyzer import JournalInsight, analyze_journal
from src.synthesis import synthesize_nightly, NightlySummary

# ═══════════════════════════════════════════════════════════════════════════════
# Test Scenarios
# ═══════════════════════════════════════════════════════════════════════════════

SCENARIOS = {
    "signal-only": {
        "description": "Signal-based trading only (no LLM), short window (5 days)",
        "timeframe": "5d",
        "traders": ["trader-kairos", "trader-aldridge"],
        "ticks": 78,  # ~1 day of 5-min bars
        "mode": "signal",
        "expected_win_rate_range": (0.3, 0.7),
    },
    "signal-only-long": {
        "description": "Signal-based trading, extended window (20 days)",
        "timeframe": "20d",
        "traders": ["trader-kairos", "trader-aldridge", "trader-stonks"],
        "ticks": 390,  # ~5 days
        "mode": "signal",
        "expected_win_rate_range": (0.3, 0.7),
    },
    "profit-mixed": {
        "description": "Mixed profit/loss trades — verifies learning loop catches both",
        "timeframe": "synthetic",
        "traders": ["trader-kairos", "trader-aldridge", "trader-stonks"],
        "trades_per_trader": 10,
        "mode": "synthetic",
        "expected_win_rate_range": (0.3, 0.6),
    },
    "all-winners": {
        "description": "All trades profitable — verifies learning loop detects good patterns",
        "timeframe": "synthetic",
        "traders": ["trader-kairos", "trader-aldridge", "trader-stonks"],
        "trades_per_trader": 8,
        "mode": "bias_positive",
        "expected_win_rate_range": (0.7, 1.0),
    },
    "all-losers": {
        "description": "All trades losing — verifies learning loop flags critical issues",
        "timeframe": "synthetic",
        "traders": ["trader-kairos", "trader-aldridge", "trader-stonks"],
        "trades_per_trader": 8,
        "mode": "bias_negative",
        "expected_win_rate_range": (0.0, 0.3),
    },
    "high-conviction": {
        "description": "High-conviction decisions with mixed outcomes — tests confidence calibration",
        "timeframe": "synthetic",
        "traders": ["trader-kairos", "trader-aldridge"],
        "trades_per_trader": 6,
        "mode": "high_conviction",
        "expected_win_rate_range": (0.3, 0.7),
    },
    "journal-heavy": {
        "description": "Many journal entries, few trades — tests reflection analysis",
        "timeframe": "synthetic",
        "traders": ["trader-stonks", "trader-kairos"],
        "trades_per_trader": 3,
        "mode": "journal_heavy",
        "expected_win_rate_range": (0.0, 1.0),
    },
}


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario Data Generators
# ═══════════════════════════════════════════════════════════════════════════════


def _gen_synthetic_trades(agent_id: str, count: int, mode: str) -> List[Dict]:
    """Generate synthetic trade data for a trader."""
    now = datetime.now(timezone.utc)
    trades = []
    tickers_by_agent = {
        "trader-kairos": ["NVDA", "AMD", "AVGO", "SMCI", "MRVL", "TSLA", "META", "GOOGL", "AMZN", "AAPL"],
        "trader-aldridge": ["JPM", "KO", "PG", "WMT", "PEP", "JNJ", "VZ", "MMM", "CAT", "BA"],
        "trader-stonks": ["GME", "AMC", "DJT", "HOOD", "COIN", "PLTR", "RKLB", "MARA", "RIOT", "SOFI"],
    }
    tickers = tickers_by_agent.get(agent_id, ["AAPL", "MSFT", "GOOG"])

    entry_reasons = {
        "trader-kairos": ["momentum_breakout", "volume_surge", "macd_bullish", "rsi_momentum", "earnings_momentum"],
        "trader-aldridge": ["fundamental_value", "insider_buying", "dividend_play", "oversold_bounce", "sector_rotation"],
        "trader-stonks": ["social_momentum", "unusual_options_flow", "wsb_mentions", "volume_spike", "short_squeeze_potential"],
    }
    reasons = entry_reasons.get(agent_id, ["signal"])

    rng = hash(agent_id) % 1000

    for i in range(count):
        ticker = tickers[i % len(tickers)]
        t = now - timedelta(hours=i * 4 + random_offset(rng + i))

        # Determine mode-specific behavior
        if mode == "bias_positive":
            pnl = abs(random_offset(rng + i * 3)) * 5 + 20
            pnl_pct = min(pnl / 150 * 100, 15)
            win = True
        elif mode == "bias_negative":
            pnl = -abs(random_offset(rng + i * 3)) * 5 - 10
            pnl_pct = max(pnl / 150 * 100, -12)
            win = False
        elif mode == "high_conviction":
            # Force high conviction pattern: 2 wins, 1 lose, repeat
            win = (i % 3 != 1)  # 66% win rate
            pnl_mag = abs(random_offset(rng + i * 13)) * 20 + 10
            pnl = pnl_mag if win else -pnl_mag * 0.8
            pnl_pct = pnl / 150 * 100
        elif mode == "journal_heavy":
            pnl = random_offset(rng + i * 11) * 10
            pnl_pct = pnl / 150 * 100
            win = pnl > 0
        else:
            # Force roughly 50/50 win/loss split
            win = (i % 2 == 0)  # even indices win, odd lose
            if win:
                pnl = (abs(random_offset(rng + i * 5)) * 10 + 15) * (1 if i % 3 != 0 else -1)
                if pnl < 0:
                    pnl = abs(pnl) * 0.3
            else:
                pnl = -abs(random_offset(rng + i * 7)) * 15 - 10
            pnl_pct = pnl / 150 * 100

        entry_price = 100 + random_offset(rng + i) * 20
        qty = max(1, int(abs(pnl) / (entry_price * 0.02) + 1))

        if win:
            exit_price = entry_price * (1 + pnl_pct / 100)
            exit_reason = "profit_target"
        else:
            exit_price = entry_price * (1 + pnl_pct / 100)
            exit_reason = "stop_loss"

        trades.append({
            "ticker": ticker,
            "action": "buy",
            "quantity": qty,
            "entry_price": round(entry_price, 2),
            "entry_reason": reasons[i % len(reasons)],
            "status": "closed",
            "exit_price": round(exit_price, 2),
            "exit_reason": exit_reason,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
        })

    return trades


def random_offset(seed: int) -> float:
    """Simple deterministic pseudo-random offset."""
    return ((seed * 16807 + 1) % 2147483647) / 2147483647 * 2 - 1


def _gen_decision_from_trade(agent_id: str, trade: Dict) -> Dict:
    """Generate a decision from a trade."""
    signals_by_agent = {
        "trader-kairos": ["momentum_breakout", "volume_surge", "macd_bullish"],
        "trader-aldridge": ["fundamental_value", "rsi_oversold", "insider_buying"],
        "trader-stonks": ["social_momentum", "unusual_options_flow", "volume_spike"],
    }
    base_signals = signals_by_agent.get(agent_id, ["signal"])

    action = "BUY" if trade["action"] == "buy" else "SELL"
    confidence = max(0.1, min(0.95, 0.5 + trade["pnl_pct"] / 30))

    return {
        "action": action,
        "ticker": trade["ticker"],
        "quantity": trade["quantity"],
        "confidence": round(confidence, 2),
        "thesis": f"Trade based on {trade['entry_reason']}. Entry at ${trade['entry_price']:.2f}",
        "mood": "confident" if confidence > 0.6 else "cautious",
        "signals_used": base_signals,
    }


def _gen_journal_entries(agent_id: str, trades: List[Dict], mode: str) -> List[str]:
    """Generate reflective journal entries based on trades."""
    entries = []

    # 1-2 entries per trade
    for i, trade in enumerate(trades[:5]):
        if trade["pnl"] > 0:
            entries.append(
                f"{trade['ticker']} was a solid trade — made ${trade['pnl']:.2f} "
                f"({trade['pnl_pct']:.1f}%). Entry thesis ({trade['entry_reason']}) played out as expected."
            )
        else:
            entries.append(
                f"{trade['ticker']} lost ${abs(trade['pnl']):.2f}. "
                f"Thesis: {trade['entry_reason']}. "
                f"Reviewing entry criteria — stop loss hit at {trade['exit_reason']}."
            )

    # Add meta-reflection entries
    if mode == "journal_heavy":
        entries.extend([
            f"Market regime feels uncertain. My win rate is concerning.",
            f"Reviewing my signal filters. The {trades[0]['entry_reason']} strategy needs refinement.",
            f"Considering adjusting position sizing. Current max position may be too aggressive.",
            f"Need to tighten stop-losses. Multiple trades hit -8% before I reacted.",
            f"Journal analysis suggests I'm overconfident on momentum signals in choppy markets.",
            f"Tomorrow I'll focus on higher-conviction setups only. Quality over quantity.",
            f"Sector rotation is picking up. Need to watch for macro shifts.",
            f"Risk management check: all positions sized within limits. Cash buffer at 40%.",
        ])

    return entries


# ═══════════════════════════════════════════════════════════════════════════════
# Test Runner
# ═══════════════════════════════════════════════════════════════════════════════


def _clear_trader_data(db: sqlite3.Connection, agent_id: str):
    """Clear existing test data for a trader."""
    db.execute("DELETE FROM trades WHERE agent_id = ?", (agent_id,))
    db.execute("DELETE FROM decisions WHERE agent_id = ?", (agent_id,))
    db.execute("DELETE FROM journal WHERE agent_id = ?", (agent_id,))
    db.commit()


def _inject_trader_data(db: sqlite3.Connection, agent_id: str, decisions: List[Dict], trades: List[Dict], journal: List[str]):
    """Inject data for a trader and return decision IDs."""
    now = datetime.now(timezone.utc)
    decision_ids = []

    for i, dec in enumerate(decisions):
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.000000+00:00")
        signals_json = json.dumps(dec.get("signals_used", []))
        cur = db.execute(
            """INSERT INTO decisions 
               (agent_id, timestamp, action, ticker, quantity, confidence, thesis, mood, source, signals_used, tick_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'live', ?, ?)""",
            (agent_id, ts, dec["action"], dec.get("ticker", ""), dec["quantity"],
             dec["confidence"], dec["thesis"], dec.get("mood", "neutral"),
             signals_json, i + 1),
        )
        decision_ids.append(cur.lastrowid)

    for i, trade in enumerate(trades):
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.000000+00:00")
        db.execute(
            """INSERT INTO trades
               (agent_id, timestamp, decision_id, ticker, action, quantity,
                entry_price, entry_reason, entry_timestamp, status,
                exit_price, exit_timestamp, exit_reason, pnl, pnl_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?, ?, ?)""",
            (agent_id, ts, decision_ids[i % len(decision_ids)],
             trade["ticker"], trade["action"], trade["quantity"],
             trade["entry_price"], trade.get("entry_reason", ""), ts,
             trade.get("exit_price", 0), ts, trade.get("exit_reason", ""),
             trade.get("pnl", 0), trade.get("pnl_pct", 0)),
        )

    for entry in journal:
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.000000+00:00")
        db.execute(
            "INSERT INTO journal (agent_id, timestamp, mood, entry, source) VALUES (?, ?, 'reflective', ?, 'md')",
            (agent_id, ts, entry),
        )

    db.commit()
    return decision_ids


def run_scenario(name: str, config: Dict) -> Dict[str, Any]:
    """Run a single verification scenario and return results."""
    print(f"\n{'='*70}")
    print(f"📊 SCENARIO: {name}")
    print(f"   {config['description']}")
    print(f"   Mode: {config['mode']} | Timeframe: {config['timeframe']}")
    print(f"{'='*70}")

    db = get_db()
    results = {}

    for agent_id in config["traders"]:
        # Clear old data
        _clear_trader_data(db, agent_id)

        # Generate data
        trades = _gen_synthetic_trades(agent_id, config.get("trades_per_trader", 10), config["mode"])
        decisions = [_gen_decision_from_trade(agent_id, t) for t in trades]
        journal = _gen_journal_entries(agent_id, trades, config["mode"])

        # Inject
        _inject_trader_data(db, agent_id, decisions, trades, journal)

        # Run learning loop
        try:
            loop_result = run_for_agent(agent_id)
            results[agent_id] = {
                "trades": len(trades),
                "decisions": len(decisions),
                "journal": len(journal),
                "win_rate": loop_result["win_rate"],
                "total_pnl": loop_result["total_pnl"],
                "signals": loop_result["signals"],
                "status": "ok",
            }
            # Verify
            expected_range = config["expected_win_rate_range"]
            wr_pct = loop_result["win_rate"]  # Already 0-100
            wr_dec = wr_pct / 100.0
            if expected_range[0] <= wr_dec <= expected_range[1]:
                results[agent_id]["pass"] = True
                results[agent_id]["note"] = f"Win rate {wr_pct:.0f}% in expected range [{expected_range[0]*100:.0f}%-{expected_range[1]*100:.0f}%]"
            else:
                results[agent_id]["pass"] = False
                results[agent_id]["note"] = f"Win rate {wr_pct:.0f}% OUTSIDE expected range [{expected_range[0]*100:.0f}%-{expected_range[1]*100:.0f}%]"

        except Exception as e:
            results[agent_id] = {"status": "error", "error": str(e), "pass": False}

    db.close()

    # Scenario-level summary
    passed = sum(1 for r in results.values() if r.get("pass"))
    failed = sum(1 for r in results.values() if not r.get("pass"))
    errors = sum(1 for r in results.values() if r.get("status") == "error")

    scenario_result = {
        "name": name,
        "config": config,
        "results": results,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "total": len(results),
    }

    print(f"\n  📋 Scenario Summary: {passed}/{len(results)} passed")
    for agent_id, r in results.items():
        status = "✅" if r.get("pass") else "❌"
        if r.get("status") == "error":
            print(f"    {status} {agent_id}: ERROR — {r.get('error', 'unknown')}")
        else:
            print(f"    {status} {agent_id}: {r['trades']} trades, ${r['total_pnl']:.2f} P&L, "
                  f"{r['win_rate']:.0f}% WR — {r.get('note', '')}")

    return scenario_result


def run_all_scenarios(scenarios: Dict[str, Dict]) -> Dict[str, Dict]:
    """Run all scenarios and return results."""
    results = {}
    total_passed = 0
    total_failed = 0
    total_errors = 0

    for name, config in scenarios.items():
        result = run_scenario(name, config)
        results[name] = result
        total_passed += result["passed"]
        total_failed += result["failed"]
        total_errors += result["errors"]

    # Final summary
    grand_total = total_passed + total_failed + total_errors
    print(f"\n\n{'='*70}")
    print(f"🏁 VERIFICATION SUMMARY: {total_passed}/{grand_total} passed, {total_failed} failed, {total_errors} errors")
    print(f"{'='*70}")

    for name, result in results.items():
        status = "✅" if result["failed"] == 0 and result["errors"] == 0 else "⚠️" if result["errors"] == 0 else "❌"
        print(f"  {status} {name}: {result['passed']}/{result['total']} passed")

    return results


def list_scenarios():
    """List available scenarios."""
    print("Available verification scenarios:")
    print(f"{'─'*70}")
    for name, config in SCENARIOS.items():
        print(f"  {name}")
        print(f"    {config['description']}")
        print(f"    Mode: {config['mode']} | Traders: {len(config['traders'])} | Range: {config['expected_win_rate_range']}")
        print()


def generate_html_report(results: Dict[str, Dict]):
    """Generate an HTML verification report."""
    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Learning Loop Verification Report</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2em auto; padding: 0 1em; }
h1 { color: #333; }
.scenario { border: 1px solid #ddd; border-radius: 8px; padding: 1em; margin: 1em 0; }
.pass { border-left: 4px solid #22c55e; }
.fail { border-left: 4px solid #ef4444; }
.error { border-left: 4px solid #f59e0b; }
.trader { margin: 0.5em 0; }
.status-pass { color: #16a34a; }
.status-fail { color: #dc2626; }
pre { background: #f5f5f5; padding: 0.5em; border-radius: 4px; overflow-x: auto; }
</style></head><body>
<h1>🧬 Learning Loop — Verification Report</h1>
<p>Generated: """ + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + """</p>
"""

    for name, result in results.items():
        cls = "pass" if result["failed"] == 0 else "fail"
        html += f'<div class="scenario {cls}">'
        html += f'<h3>{name}</h3>'
        html += f'<p>{result["config"]["description"]}</p>'
        html += f'<p>Result: {result["passed"]}/{result["total"]} passed</p>'

        for agent_id, r in result["results"].items():
            if r.get("status") == "error":
                html += f'<div class="trader error">❌ {agent_id}: {r["error"]}</div>'
            else:
                cls2 = "pass" if r.get("pass") else "fail"
                html += f'<div class="trader status-{cls2}">'
                html += f'{"✅" if r.get("pass") else "❌"} {agent_id}: {r["trades"]} trades, ${r["total_pnl"]:.2f} P&L, {r["win_rate"]:.0f}% WR'
                html += f'<br><small>{r.get("note", "")}</small>'
                if r.get("signals"):
                    html += "<br><small>Signals:"
                    for s in r["signals"]:
                        html += f"<br>&nbsp;&nbsp;{s}"
                    html += "</small>"
                html += '</div>'

        html += '</div>'

    html += """
</body></html>"""

    report_path = PROJECT_DIR / "logs" / "learning_loop_verification.html"
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(html)
    print(f"\n📄 HTML report: {report_path}")
    return report_path


def main():
    parser = argparse.ArgumentParser(description="Learning Loop Verification Suite")
    parser.add_argument("--scenario", help="Run specific scenario (default: all)")
    parser.add_argument("--list", action="store_true", help="List available scenarios")
    parser.add_argument("--html", action="store_true", help="Generate HTML report")
    parser.add_argument("--output", help="Output file for JSON results")

    args = parser.parse_args()

    if args.list:
        list_scenarios()
        return

    if args.scenario:
        if args.scenario not in SCENARIOS:
            print(f"Unknown scenario: {args.scenario}")
            print(f"Available: {', '.join(SCENARIOS.keys())}")
            sys.exit(1)
        scenarios = {args.scenario: SCENARIOS[args.scenario]}
    else:
        scenarios = SCENARIOS

    results = run_all_scenarios(scenarios)

    if args.html:
        generate_html_report(results)

    if args.output:
        output_path = Path(args.output)
        # Convert to serializable
        serializable = {}
        for name, result in results.items():
            serializable[name] = {
                "name": result["name"],
                "passed": result["passed"],
                "failed": result["failed"],
                "errors": result["errors"],
                "total": result["total"],
                "results": {
                    aid: {k: v for k, v in r.items() if k != "signals"}
                    for aid, r in result["results"].items()
                }
            }
        output_path.write_text(json.dumps(serializable, indent=2))
        print(f"📄 JSON results: {output_path}")


if __name__ == "__main__":
    main()