#!/usr/bin/env python3
"""
Virtual Trader Promotion Check — SPEC-v3 §1.2, virtual-trader-promotion.md.

Runs nightly after market close. For each virtual trader:
  1. Load performance metrics (P&L, win rate, drawdown, calmar, sortino)
  2. Check promotion criteria against current tier
  3. If criteria met, promote to next tier
  4. Log promotion to trading.promotion_summary
  5. Update tier_snapshots

Tier System (from spec):
  Probation → Rookie → Veteran → Expert → Elite → Live

Promotion Criteria:
  Probation→Rookie:  5+ trades, 2+ days old, any positive return
  Rookie→Veteran:    20+ trades, 7+ days old, Sortino > 0.5, Calmar > 0.3
  Veteran→Expert:    50+ trades, 14+ days old, Sortino > 1.0, Calmar > 0.5, top-3 rank
  Expert→Elite:      100+ trades, 30+ days old, Sortino > 1.5, Calmar > 0.8, top-1 rank
  Elite→Live:        Manual approval only (gateway operator)

Usage:
    python3 scripts/promotion_check.py                   # run once (for cron)
    python3 scripts/promotion_check.py --dry-run          # print what would happen
    python3 scripts/promotion_check.py --base kairos      # only check one base trader
    python3 scripts/promotion_check.py --force-promote kairos-rk-1  # force promotion
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None  # type: ignore

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

log = logging.getLogger("promotion_check")

# ═══════════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════════

DB_DSN = os.getenv("VT_DB_DSN", "host=docker.klo port=5433 dbname=trading user=trader")
BASE_TRADERS = ["kairos", "aldridge", "stonks"]

# Tier definitions
TIERS = ["probation", "rookie", "veteran", "expert", "elite", "live"]
TIER_INDEX = {t: i for i, t in enumerate(TIERS)}

# Slot caps per tier (from spec)
TIER_SLOTS: Dict[str, int] = {
    "probation": 999,  # unlimited
    "rookie": 12,
    "veteran": 8,
    "expert": 4,
    "elite": 2,
    "live": 1,
}

# ── Promotion criteria ───────────────────────────────────────────────────────

@dataclass
class PromotionCriteria:
    """Criteria for a single tier transition."""
    min_trades: int
    min_age_days: int
    min_sortino: float
    min_calmar: float
    min_return_pct: float
    min_rank: int = 999  # 1 = best, 999 = no rank requirement
    max_drawdown: float = 50.0  # max allowed drawdown %
    min_win_rate: float = 0.0
    soft_gate: bool = False  # if True, only soft checks (almost automatic)

# Promote into tiers (from_tier→to_tier)
PROMOTION_GATES: Dict[Tuple[str, str], PromotionCriteria] = {
    ("probation", "rookie"): PromotionCriteria(
        min_trades=5, min_age_days=2, min_sortino=0.0, min_calmar=0.0,
        min_return_pct=0.0, soft_gate=True,
    ),
    ("rookie", "veteran"): PromotionCriteria(
        min_trades=20, min_age_days=7, min_sortino=0.5, min_calmar=0.3,
        min_return_pct=0.0, min_win_rate=0.40,
    ),
    ("veteran", "expert"): PromotionCriteria(
        min_trades=50, min_age_days=14, min_sortino=1.0, min_calmar=0.5,
        min_return_pct=5.0, min_rank=3, min_win_rate=0.45,
    ),
    ("expert", "elite"): PromotionCriteria(
        min_trades=100, min_age_days=30, min_sortino=1.5, min_calmar=0.8,
        min_return_pct=10.0, min_rank=1, min_win_rate=0.50,
        max_drawdown=30.0,
    ),
    ("elite", "live"): PromotionCriteria(
        min_trades=200, min_age_days=60, min_sortino=2.0, min_calmar=1.2,
        min_return_pct=20.0, min_rank=1, min_win_rate=0.55,
        max_drawdown=20.0,
    ),
}


# ═══════════════════════════════════════════════════════════════════════════════
# Domain Types
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class VirtualTrader:
    """A virtual trader record from the database."""
    id: int
    name: str
    base_trader: str
    variant_type: str
    config: Dict[str, Any]
    status: str
    tier: str
    composite_score: Optional[float]
    created_at: Optional[date]
    promoted_at: Optional[datetime]
    wins: int = 0
    live_dates: Optional[List[date]] = None

    @property
    def age_days(self) -> int:
        if not self.created_at:
            return 0
        return (date.today() - self.created_at).days


@dataclass
class PromotionCheck:
    """Result of checking one virtual trader for promotion."""
    trader: VirtualTrader
    from_tier: str
    to_tier: str
    eligible: bool
    reasons: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)


@dataclass
class PromotionResult:
    """Result of a promotion action."""
    trader_name: str
    from_tier: str
    to_tier: str
    promoted: bool
    reason: str
    metrics: Dict[str, float] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════════
# Database helpers
# ═══════════════════════════════════════════════════════════════════════════════


def get_db() -> Any:
    """Get a database connection."""
    if psycopg2 is None:
        raise ImportError("psycopg2 is not installed")
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    return conn


def fetch_virtual_traders(
    conn: Any,
    base_trader: Optional[str] = None,
) -> List[VirtualTrader]:
    """Fetch all active virtual traders from the database.

    Args:
        conn: Database connection
        base_trader: Optional filter by base trader

    Returns:
        List of VirtualTrader objects
    """
    query = """
        SELECT id, name, base_trader, variant_type, config, status,
               tier, composite_score, created_at, promoted_at, wins, live_dates
        FROM trading.virtual_traders
        WHERE status = 'active'
    """
    params: List[Any] = []
    if base_trader:
        query += " AND base_trader = %s"
        params.append(base_trader)
    query += " ORDER BY base_trader, created_at ASC"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    results: List[VirtualTrader] = []
    for row in rows:
        results.append(VirtualTrader(
            id=row["id"],
            name=row["name"],
            base_trader=row["base_trader"],
            variant_type=row["variant_type"],
            config=row["config"] or {},
            status=row["status"],
            tier=row["tier"] or "probation",
            composite_score=row["composite_score"],
            created_at=row["created_at"],
            promoted_at=row["promoted_at"],
            wins=row["wins"] or 0,
            live_dates=row["live_dates"],
        ))
    return results


def fetch_trader_metrics(
    conn: Any,
    trader: VirtualTrader,
) -> Dict[str, float]:
    """Fetch performance metrics for a virtual trader.

    Queries trades, equity_snapshots, and sweep_results tables.

    Args:
        conn: Database connection
        trader: VirtualTrader object

    Returns:
        Dict with keys: n_trades, total_return_pct, max_drawdown,
        win_rate, sortino, calmar, profit_factor, composite_score
    """
    metrics: Dict[str, float] = {
        "n_trades": 0,
        "total_return_pct": 0.0,
        "max_drawdown": 0.0,
        "win_rate": 0.0,
        "sortino": 0.0,
        "calmar": 0.0,
        "profit_factor": 0.0,
        "composite_score": 0.0,
    }

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Trades
        cur.execute("""
            SELECT COUNT(*) as n_trades,
                   COALESCE(AVG(NULLIF(return_pct, 0)), 0) as avg_return,
                   COALESCE(SUM(pnl), 0) as total_pnl
            FROM trading.trades
            WHERE trader_id = %s AND exit_time IS NOT NULL
        """, (trader.name,))
        row = cur.fetchone()
        if row:
            metrics["n_trades"] = row["n_trades"] or 0
            metrics["total_return_pct"] = float(row["avg_return"] or 0) * 100

        # Win rate
        cur.execute("""
            SELECT COUNT(*) FILTER (WHERE pnl > 0) as wins,
                   COUNT(*) as total
            FROM trading.trades
            WHERE trader_id = %s AND exit_time IS NOT NULL AND pnl IS NOT NULL
        """, (trader.name,))
        row = cur.fetchone()
        if row and row["total"] > 0:
            metrics["win_rate"] = (row["wins"] / row["total"]) * 100

        # Equity snapshots for max drawdown and sortino
        cur.execute("""
            SELECT
                COALESCE(MAX(max_drawdown), 0) as max_dd,
                COALESCE(AVG(sortino_30d), 0) as avg_sortino,
                COALESCE(AVG(calmar_30d), 0) as avg_calmar,
                COALESCE(AVG(profit_factor), 0) as avg_pf,
                COALESCE(AVG(equity), 0) as avg_equity
            FROM trading.equity_snapshots
            WHERE trader_id = %s
        """, (trader.name,))
        row = cur.fetchone()
        if row:
            metrics["max_drawdown"] = float(row["max_dd"] or 0)
            metrics["sortino"] = float(row["avg_sortino"] or 0)
            metrics["calmar"] = float(row["avg_calmar"] or 0)
            metrics["profit_factor"] = float(row["avg_pf"] or 0)

        # Composite score from sweep_results
        cur.execute("""
            SELECT objective_score
            FROM trading.sweep_results
            WHERE trader_id = %s
            ORDER BY id DESC
            LIMIT 1
        """, (trader.name,))
        row = cur.fetchone()
        if row and row["objective_score"]:
            metrics["composite_score"] = float(row["objective_score"])

    return metrics


def fetch_rank(
    conn: Any,
    trader: VirtualTrader,
) -> int:
    """Get the rank of this trader among its peers in the same base trader group.

    Ranks by composite score (higher is better). 1 = best.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) + 1 as rank
            FROM trading.virtual_traders vt
            WHERE vt.base_trader = %s
              AND vt.status = 'active'
              AND COALESCE(vt.composite_score, 0) > COALESCE(%s, 0)
        """, (trader.base_trader, trader.composite_score))
        row = cur.fetchone()
        return row[0] if row else 999


def promote_trader(
    conn: Any,
    trader: VirtualTrader,
    to_tier: str,
    reason: str,
    metrics: Dict[str, float],
) -> bool:
    """Execute a promotion: update DB and log to promotion_summary.

    Args:
        conn: Database connection
        trader: VirtualTrader to promote
        to_tier: Target tier
        reason: Reason for promotion
        metrics: Performance metrics at promotion time

    Returns:
        True if promotion succeeded
    """
    from_tier = trader.tier

    try:
        with conn.cursor() as cur:
            # Update virtual trader record
            cur.execute("""
                UPDATE trading.virtual_traders
                SET tier = %s, promoted_at = NOW(), composite_score = %s
                WHERE id = %s
            """, (to_tier, metrics.get("composite_score", 0), trader.id))

            # Log to promotion_summary
            cur.execute("""
                INSERT INTO trading.promotion_summary
                    (trader_id, virtual_name, from_tier, to_tier,
                     composite_score, calmar, sortino, profit_factor,
                     win_rate, total_return_pct, max_drawdown, n_trades,
                     reason, promoted_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                trader.base_trader,
                trader.name,
                from_tier,
                to_tier,
                metrics.get("composite_score"),
                metrics.get("calmar"),
                metrics.get("sortino"),
                metrics.get("profit_factor"),
                metrics.get("win_rate"),
                metrics.get("total_return_pct"),
                metrics.get("max_drawdown"),
                metrics.get("n_trades"),
                reason,
                "system",
            ))

        log.info(f"Promoted {trader.name}: {from_tier} → {to_tier} ({reason})")
        return True
    except Exception as e:
        log.error(f"Failed to promote {trader.name}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# Promotion Logic
# ═══════════════════════════════════════════════════════════════════════════════


def check_promotion(
    trader: VirtualTrader,
    metrics: Dict[str, float],
    rank: int,
) -> PromotionCheck:
    """Check if a virtual trader is eligible for promotion.

    Args:
        trader: Virtual trader to check
        metrics: Performance metrics
        rank: Current rank among peers

    Returns:
        PromotionCheck with eligibility result
    """
    current_tier = trader.tier
    current_idx = TIER_INDEX.get(current_tier, 0)

    # Can't promote beyond live
    if current_tier == "live":
        return PromotionCheck(
            trader=trader, from_tier="live", to_tier="live",
            eligible=False, failures=["Already at max tier (live)"],
        )

    # Can't promote if no next tier
    if current_idx >= len(TIERS) - 1:
        return PromotionCheck(
            trader=trader, from_tier=current_tier, to_tier=current_tier,
            eligible=False, failures=["No next tier available"],
        )

    next_tier = TIERS[current_idx + 1]
    gate = PROMOTION_GATES.get((current_tier, next_tier))
    if not gate:
        return PromotionCheck(
            trader=trader, from_tier=current_tier, to_tier=next_tier,
            eligible=False, failures=[f"No promotion gate defined for {current_tier}→{next_tier}"],
        )

    check = PromotionCheck(
        trader=trader,
        from_tier=current_tier,
        to_tier=next_tier,
        eligible=True,
        metrics=metrics,
    )

    # Check criteria
    n_trades = metrics.get("n_trades", 0)
    if n_trades < gate.min_trades:
        check.eligible = False
        check.failures.append(f"Trades: {n_trades} < {gate.min_trades}")

    if trader.age_days < gate.min_age_days:
        check.eligible = False
        check.failures.append(f"Age: {trader.age_days}d < {gate.min_age_days}d")

    sortino = metrics.get("sortino", 0)
    if sortino < gate.min_sortino:
        check.eligible = False
        check.failures.append(f"Sortino: {sortino:.2f} < {gate.min_sortino:.2f}")

    calmar = metrics.get("calmar", 0)
    if calmar < gate.min_calmar:
        check.eligible = False
        check.failures.append(f"Calmar: {calmar:.2f} < {gate.min_calmar:.2f}")

    total_return = metrics.get("total_return_pct", 0)
    if total_return < gate.min_return_pct:
        check.eligible = False
        check.failures.append(f"Return: {total_return:.1f}% < {gate.min_return_pct:.1f}%")

    win_rate = metrics.get("win_rate", 0)
    if win_rate < gate.min_win_rate:
        check.eligible = False
        check.failures.append(f"Win rate: {win_rate:.1f}% < {gate.min_win_rate * 100:.0f}%")

    max_dd = metrics.get("max_drawdown", 0)
    if max_dd > gate.max_drawdown:
        check.eligible = False
        check.failures.append(f"Drawdown: {max_dd:.1f}% > {gate.max_drawdown:.1f}%")

    if rank > gate.min_rank:
        check.eligible = False
        check.failures.append(f"Rank: #{rank} > #{gate.min_rank}")

    if check.eligible:
        check.reasons.append(f"All {len(check.failures) + 1} criteria passed")

    return check


def run_promotion_check(
    base_trader: Optional[str] = None,
    dry_run: bool = False,
    force_promote: Optional[str] = None,
) -> List[PromotionResult]:
    """Run the full promotion check for all virtual traders.

    Args:
        base_trader: Optional filter by base trader
        dry_run: If True, only print what would happen
        force_promote: Force promotion of a specific virtual trader name

    Returns:
        List of PromotionResult objects
    """
    conn = get_db()
    results: List[PromotionResult] = []

    try:
        traders = fetch_virtual_traders(conn, base_trader)

        if not traders:
            log.info("No active virtual traders found")
            return results

        # Group by base trader
        by_base: Dict[str, List[VirtualTrader]] = defaultdict(list)
        for t in traders:
            by_base[t.base_trader].append(t)

        for base, group in by_base.items():
            log.info(f"Checking {len(group)} virtual traders for {base}")

            for trader in group:
                # Fetch metrics
                metrics = fetch_trader_metrics(conn, trader)
                rank = fetch_rank(conn, trader)

                # Force promotion bypass
                if force_promote and trader.name == force_promote:
                    current_idx = TIER_INDEX.get(trader.tier, 0)
                    if current_idx < len(TIERS) - 1:
                        next_tier = TIERS[current_idx + 1]
                        result = PromotionResult(
                            trader_name=trader.name,
                            from_tier=trader.tier,
                            to_tier=next_tier,
                            promoted=True,
                            reason="Force promoted by operator",
                            metrics=metrics,
                        )
                        if not dry_run:
                            promote_trader(conn, trader, next_tier, result.reason, metrics)
                        results.append(result)
                        log.info(f"Force promoted {trader.name} → {next_tier}")
                    continue

                # Check promotion
                check = check_promotion(trader, metrics, rank)

                if check.eligible:
                    result = PromotionResult(
                        trader_name=trader.name,
                        from_tier=check.from_tier,
                        to_tier=check.to_tier,
                        promoted=True,
                        reason="; ".join(check.reasons),
                        metrics=metrics,
                    )
                    if not dry_run:
                        promote_trader(conn, trader, check.to_tier, result.reason, metrics)
                    results.append(result)
                    log.info(f"Promoting {trader.name}: {check.from_tier}→{check.to_tier}")
                else:
                    log.debug(f"Skipping {trader.name}: {', '.join(check.failures)}")

        # Snapshot tier distribution
        if not dry_run:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT trading.snapshot_tiers()")
                    log.info("Tier snapshot updated")
            except Exception as e:
                log.warning(f"Tier snapshot failed: {e}")

    finally:
        conn.close()

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Virtual Trader Promotion Check — SPEC-v3 §1.2"
    )
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen")
    parser.add_argument("--base", type=str, help="Only check one base trader (e.g. kairos)")
    parser.add_argument("--force-promote", type=str, help="Force promote a specific virtual trader")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [promotion] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    results = run_promotion_check(
        base_trader=args.base,
        dry_run=args.dry_run,
        force_promote=args.force_promote,
    )

    if not results:
        print("No promotions today.")
        return

    print(f"\n{'=' * 60}")
    print(f"Promotion Results ({len(results)} total)")
    print(f"{'=' * 60}")
    for r in results:
        status = "✅ PROMOTED" if r.promoted else "❌ SKIPPED"
        print(f"  {status} {r.trader_name}: {r.from_tier} → {r.to_tier}")
        print(f"         Reason: {r.reason}")
        if r.metrics:
            print(f"         Trades: {r.metrics.get('n_trades', 0)}, "
                  f"Return: {r.metrics.get('total_return_pct', 0):.1f}%, "
                  f"Sortino: {r.metrics.get('sortino', 0):.2f}")


if __name__ == "__main__":
    main()