# Known Issues — Paper Trading Rebuild

Track active issues in the rebuild. Port only issues relevant to this codebase — legacy issues stay in the legacy repo. Update status as issues are resolved.

**Last updated**: 2026-07-06

---

## 🔴 Critical

### Kairos -7% P&L — 0% Win Rate on Momentum Strategy
- **Date:** 2026-07-06 (ongoing since June 30 in legacy)
- **Severity:** Critical — trader is losing money with no winning trades
- **Description:** Kairos's momentum strategy has produced a 0% win rate and -7% drawdown. The strategy parameters (momentum threshold, RSI bounds, stop-loss distance) are likely miscalibrated for current market conditions. The nightly optimization pipeline (DP-1 through DP-5 in `specs/nightly-optimization-pipeline.md`) is designed to address this by tuning signal parameters via walk-forward validation and prompt sweeping.
- **Status:** Being addressed by nightly optimization pipeline. Awaiting first successful night run.
- **Impact:** Kairos continues to lose paper money. If not fixed, risks elimination from the competition.

### No Live Postgres Connection — Traders Still Writing to Legacy SQLite
- **Date:** 2026-07-06
- **Severity:** Critical — rebuild's core infrastructure is not yet live
- **Description:** The rebuild's Postgres schema (`src/db/schema.sql`) and connection layer (`src/db/connection.py`) are built, but the Postgres container is not running in production. Live traders (Kairos, Aldridge, Stonks) continue writing decisions to the legacy SQLite database. The `scripts/migrate_sqlite_to_pg.py` migration script exists but has not been executed against live data.
- **Status:** Pending. Requires: (1) deploy Postgres container via Docker Compose, (2) run migration script against current SQLite, (3) cut over trader decision writes to Postgres.
- **Impact:** All rebuild infrastructure is running against test/example data. Live trading is still legacy-only.

---

## 🟡 High

### Data Bus Reads from Legacy DB — Needs Postgres Cutover
- **Date:** 2026-07-06
- **Severity:** High — data pipeline is split across two systems
- **Description:** The data bus (`src/data_fetcher.py`) fetches market data correctly but portfolio state and trade history still come from the legacy SQLite database. During the transition, the rebuild's signal engine can't access live portfolio positions without a sync bridge.
- **Status:** Blocked on Postgres connectivity (see above). Once Postgres is live and migration complete, cut over data bus reads.
- **Impact:** Signal engine and walk-forward validation run on stale/reconstructed data, not live portfolio state.

### Stonks Pipeline Stall — Verify Resolved in Rebuild
- **Date:** 2026-06-30 (legacy issue)
- **Severity:** High — trader silently stopped producing decisions
- **Description:** In the legacy system, Stonks went 3+ days producing 0 trading decisions despite the cron firing. The pipeline stall was never fully root-caused. The rebuild introduces health checks and decision freshness monitoring — but these haven't been tested against the same stall pattern.
- **Status:** Verify resolved. Run the rebuild's monitoring against a full trading day simulation to confirm pipeline health checks would catch a stall. If the root cause was data bus-related, verify the rebuild's data bus doesn't have the same failure mode.
- **Impact:** If unresolved, Stonks could silently stop trading in the rebuild too.

---

## 🟢 Medium

### Sync Bridge Not Built
- **Date:** 2026-07-06
- **Severity:** Medium — blocks dashboard Phase 2
- **Description:** The dashboard phased migration plan (DECISIONS #5) requires a sync bridge that writes rebuild Postgres data back to legacy SQLite tables. This bridge has not been built. Until it exists, the legacy dashboard won't reflect rebuild data.
- **Status:** Planned, not started. Depends on Postgres connectivity.
- **Impact:** During transition, Raf sees legacy data only — losing visibility into rebuild performance.

### No Automated Health Checks
- **Date:** 2026-07-06
- **Severity:** Medium — silent failures possible
- **Description:** The rebuild has no automated health monitoring for: cron presence, decision freshness, data bus uptime, Postgres connectivity, or pipeline stall detection. The legacy system had the same gap, which caused the Stonks stall to go unnoticed for days.
- **Status:** Not started. Candidate for `scripts/health_check.py` or a `monitors/` directory.
- **Impact:** Any component can silently fail without detection until a human notices.

---

## Notes

- Issues from the legacy repo that are **resolved in the rebuild by design** (e.g., SQLite single-writer bottleneck, two-feature regime classification, cron vanishing due to stale agent IDs) are NOT listed here — they're addressed by the rebuild's architecture.
- This file is rebuilt-specific. Legacy issues live in `casper-bot-wodinga/paper-trading-teams/issues`.
