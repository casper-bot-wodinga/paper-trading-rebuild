# Paper Trading Rebuild — BACKLOG

Prioritized work remaining for the rebuild. P0 = blocking live operation. P1 = must-have for full transition. P2 = quality/velocity. P3 = future.

**Last updated**: 2026-07-06

---

## 🔴 P0 — Get Rebuild Live (blocking)

- [ ] **Postgres connectivity for live trading** — Deploy Postgres container via Docker Compose (`infra/docker/docker-compose.trading.yml`). Run `scripts/migrate_sqlite_to_pg.py` against current SQLite data. Verify `src/db/connection.py` connects successfully. Cut over trader decision writes to Postgres.
- [ ] **Trader prompt integration** — Traders must read from rebuild signals (not legacy). Wire `src/signals.py` output into the context blob constructed by `src/trader.py`. Verify traders see rebuild-generated momentum/RSI/volatility/regime fields.
- [ ] **Data bus Postgres cutover** — Switch `src/data_fetcher.py` portfolio state reads from legacy SQLite to rebuild Postgres. Depends on Postgres connectivity + migration.
- [ ] **Verify Stonks pipeline stall is resolved** — Run full trading day simulation through rebuild pipeline. Confirm health checks would catch a 0-decision stall. Root cause the legacy stall if possible.

---

## 🟡 P1 — Transition Complete (soon)

- [ ] **Dashboard Phase 2: sync bridge** — Build `scripts/sync_bridge.py` to write rebuild Postgres data back to legacy SQLite tables. Keeps old dashboard current while rebuild matures. See DECISIONS #5.
- [ ] **Dashboard Phase 3: rebuild-native dashboard** — New frontend reading directly from Postgres. Decommission legacy dashboard. Depends on Phase 2 sync bridge validation.
- [ ] **K-means regime detector — shadow mode** — Run K-means regime classifier (`specs/kmeans-regime.md`) in shadow mode alongside current rule-based classifier. Compare regime assignments for 2+ weeks before cutting over. Verify regime labels are stable and interpretable.
- [ ] **K-means regime detector — live** — After shadow mode validation, cut over signal engine to K-means regime labels. Update `src/signals.py` to read cluster assignments instead of threshold-based bucketing.
- [ ] **Learning loop wiring** — Wire structured journal, params history, and `--apply` flag. Traders read their own P&L journal on each tick. Parameters auto-tune via gradient descent. See SPEC.md §9-11.
- [ ] **Nightly optimization pipeline production run** — Run `specs/nightly-optimization-pipeline.md` end-to-end against live data. Verify walk-forward validation, prompt sweeps, and parameter gradient descent all complete without error.

---

## 🟢 P2 — Quality + Velocity (later)

- [ ] **MCP tools for Alpaca execution** — Currently exec-based. Move to proper MCP tool calls for order placement, position queries, and account state. Improves reliability and auditability.
- [ ] **WebSocket live dashboard** — Real-time P&L updates, position changes, and trader decisions via WebSocket instead of page refresh. See SPEC.md §17.
- [ ] **Automated health checks** — `scripts/health_check.py`: cron presence, decision freshness, data bus uptime, Postgres connectivity, pipeline stall detection. Alerts via canvas or chat bridge.
- [ ] **Alembic migration baseline** — Initialize Alembic on rebuild schema. All future schema changes go through Alembic migrations, not manual SQL.
- [ ] **Trader strategy branches** — Git-versioned strategy snapshots per trader. Each prompt change is a branch with `strategy-notes.md` explaining what changed and why. Enables rollback via `git revert`.
- [ ] **fusion-review.md action items** — Address the researcher's three structural concerns: (1) reduce Calmar weight to 0.25 in objective function, (2) add gradient-noise guardrails to signal engine, (3) add temporal purging to walk-forward validation windows.

---

## ⚪ P3 — Future / Nice-to-Have

- [ ] **Gaussian Mixture (GMM) upgrade for regime detection** — If K-means fixed-K limitation proves problematic, upgrade to GMM for soft cluster assignments and variable cluster covariance. See DECISIONS #2.
- [ ] **HMM regime detection (v5)** — If temporal regime transitions prove critical, resurrect HMM approach with the benefit of K-means/GMM ground-truth labels for initialization. Runs on Mac GPU worker.
- [ ] **Cross-trader learning** — Kairos learns from Stonks's mistakes. Requires shared knowledge base across trader persistent sessions. See legacy DECISIONS #9 (Option A was rejected; this revisits with better infrastructure).
- [ ] **Combinatorial purged cross-validation** — If walk-forward validation sample sizes grow, upgrade from simple rolling windows to CPCV for more robust out-of-sample estimates.
- [ ] **Multi-asset expansion** — Beyond equities: ETFs, crypto (via Alpaca crypto), options. Requires new data pipelines, risk gates, and trader prompt engineering.

---

## Open Questions

- **When to cut over live traders from legacy to rebuild?** The rebuild's signal engine must match or beat legacy performance before any trader switch. What's the acceptance criterion? (Proposal: rebuild Calmar ≥ legacy Calmar for 10 consecutive trading days in shadow mode.)
- **Should the nightly optimization pipeline run on the Mac GPU worker?** Currently designed for local execution. The GPU worker could parallelize Phase 1 parameter sweeps. Worth measuring before migrating — the overhead of gRPC file transfer may negate compute gains.
- **Keep legacy repo alive as archive or archive-and-delete?** Once rebuild is fully live, the legacy repo becomes historical reference. Archive it (make read-only, update README with pointer to rebuild).
