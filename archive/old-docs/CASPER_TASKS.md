# Casper Tasks — July 7, 2026
# Hermes needs you to unblock these. Log progress below each item.

## P0 — TRADERS NOT EXECUTING (0 trades today)

Kairos is running but making all HOLD decisions:
- "Volume filter blocking all new entries" at 9:58 AM
- Hit a DB error trying to query Postgres for open positions
- Fear & Greed at 27 — traders need to trade through fear, not freeze

Actions needed:
1. Check Kairos/Stonks/Aldridge OpenClaw cron configs — are they firing every 5 min?
2. Lower volume filter threshold or add a bootstrap bypass when zero positions open
3. Add Postgres connection to trader .env files (host=192.168.1.179, port=5433, db=trading, user=trader)
4. Restart risk_gate with BOOTSTRAP_MODE=True — it may have reverted on restart
5. Verify all three traders have working Alpaca API keys

## P1 — DASHBOARD NEEDS POSTGRES

Dashboard at :5002 reads old paper-trading-teams SQLite which has 5 total trades.
Postgres on docker.klo:5433 has 1,064 trades but dashboard doesn't read it.

Actions:
1. Either update leaderboard_api.py to use psycopg2 → 192.168.1.179:5433
2. Or we build a new dashboard serving Postgres data
3. Schema mapping: old `executed_trades` → new `trading.executed_trades`, old `trader_decisions` → new `trading.decisions`

## P2 — SWEEP PRODUCES 0 TRADES

prompt_sweep.py runs but signal engine defaults produce 0 trades in replay.
Need bootstrap params: momentum_threshold=0.10, volume_threshold=0.3, conviction_multiplier=0.3

Actions:
1. Add --bootstrap flag to prompt_sweep.py or create bootstrap SignalParams preset
2. Run: python3 src/prompt_sweep.py --trader kairos --date 2026-07-02 --variants 8
3. Verify trades appear in replay output

## P3 — POSTGRES MIGRATION GAPS

Live traders still write to old paper-trading-teams/shared/trader.db (SQLite).
Postgres has historical data only.

Actions:
1. Point traders' sync scripts to Postgres instead of SQLite
2. Ensure all new trades/decisions/journal entries go to Postgres
3. Verify write-on-transaction (invariant #1)

## Progress Log