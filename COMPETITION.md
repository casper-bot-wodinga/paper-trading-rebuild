# COMPETITION: Paper Trading Tournament Rules & Virtual Trader Pipeline

> **Companion spec to** `SPEC.md`
> **Repos:** `Tesselation-Studios/paper-trading-rebuild`, `Tesselation-Studios/paper-trading-agents`, `Tesselation-Studios/paper-trading-agents`
> **Status:** Active — all three traders live, competition started Jul 9, 2026
> **Goal:** Maximize portfolio value by December 31, 2026. Winner takes all (bragging rights).
> **Last updated:** 2026-07-09

---

## §C1 — The Tournament

### C1.1 Competition Structure

Three AI traders compete to maximize portfolio value. Each starts with $10,000 paper capital.
End date: **December 31, 2026, 4:00 PM ET.**

| Trader | Starting Capital | Current | Strategy | Persona |
|--------|-----------------|---------|----------|---------|
| **Kairos** | $10,000 | ~$9,360 | Momentum + ML | Zara Chen, data-driven quant |
| **Aldridge** | $10,000 | ~$10,176 | Value + fundamentals | Edmund Whitfield, old-school analyst |
| **Stonks** | $10,000 | ~$10,639 | Social/sentiment signals | Stan "the Man" Hoolihan, WSB energy |
| **SPY (benchmark)** | $10,000 | — | Buy-and-hold | Baseline to beat |

### C1.2 Prize Structure

- **Winner:** Most portfolio value at 12/31/26 close
- **Loser:** Strategy analyzed, lessons documented, shared with the group
- **Benchmark:** Must beat SPY buy-and-hold to be considered "successful"
- **Drawdown floor:** > 15% drawdown = paused. > 20% = eliminated from competition.

### C1.3 Calendar & Phases

```
Jul 9  ─── Competition starts. All traders at $10K.
            Bootstrap: learn fast, lose small.
            First 30 trades — loose conf (0.15), 1-2% size, $10-40 stocks.

Jul-Aug ── Bootstrap phase. Volume > perfection.
            Risk gate: WARN only (not veto). Max learning.
            Positive expectancy unlocks normal mode.

Sep-Oct ── Mid-game. Strategies converge. Winners emerge.
            Risk gate: VETO mode active.
            Tightened parameters, higher conviction.

Nov-Dec ── Endgame. Max aggression within drawdown limits.
            All tools unlocked (shorting, options — if earned).
            Final push for portfolio max.

Dec 31 ─── 4:00 PM ET — Final mark. Winner declared.
```

### C1.4 Expanding Universe

Traders earn access to broader markets by proving consistent profitability:

| Phase | Markets | Unlock Condition |
|-------|---------|------------------|
| **Phase 1** (now) | Regular stocks ($10-40) | Default |
| **Phase 2** | Shorts | Prove consistent profitability on longs (30+ trades, positive expectancy) |
| **Phase 3** | Crypto | Same as Phase 2 + 60+ profitable trades |
| **Phase 4** | Options, leveraged ETFs, forex | Same as Phase 3 + 90+ profitable trades |

**"By any means necessary"** means within the rules. No cheating, no exploiting bugs,
no manipulating other traders. But shorting, leverage, crypto, options — all fair game
once earned.

### C1.5 Leaderboard

The dashboard at `trading.wodinga.studio` is the scoreboard. Every trader checks it.
Every trader knows where they stand.

| Position | Effect |
|----------|--------|
| 1st | Pride + strategy influence (winner's approach studied by others) |
| 2nd | Pressure to catch up |
| 3rd | Existential threat — must pivot or be eliminated |
| Below SPY | Failing the benchmark — strategy needs fundamental review |

**HOLD is not a strategy — it's a forfeit.** A trader that goes a full trading day
without at least considering a trade must journal why.

---

## §C2 — Virtual Trader Pipeline

### C2.1 Architecture

Each real trader spawns N virtual variants — same persona, tweaked parameters/prompts:

```
For each real trader (Kairos, Aldridge, Stonks):
  └── n virtual traders (prompt/param variants)
        │
        ▼
    Python script (Docker container on docker.klo → .179)
    - Opens structured API calls to OpenRouter
    - Simulates trader decision loop without OpenClaw overhead
    - Uses Python tools from paper-trading-rebuild
    - Edits its own prompt files and commits to git branches
```

### C2.2 Startup Variant Matrix

Initial 24 virtual variants per trader (config/virtual_traders.json):

```
For each of 3 traders:
  × 2 confidence levels     (tight 0.25 / loose 0.15)
  × 2 position sizes        (small 1% / big 3%)
  × 2 aggression levels     (patient / aggressive)
  × 2 valuation styles      (value / momentum) — shifts signal weighting
  
  = 24 virtual traders per real trader
  = 72 total virtual traders
```

### C2.3 Daily Cycle

| Time | Activity |
|------|----------|
| **09:30-16:00 ET** — Market hours | Virtual traders run LIVE alongside real trader. Both record trades to DB. Virtual trader edits prompts + commits to branches. |
| **16:00-16:30 ET** — EOD snapshot | Snapshot all variants' progress. Flag variants that have completed their evaluation window for promotion review. |
| **16:30-20:00 ET** — Cooldown + review | Review day's results and completed evaluations. |
| **20:00-06:00 ET** — Night backtest | Virtual traders switch to backtesting on historical data. Multi-day evaluation windows simulated at accelerated speed. |
| **06:00-09:30 ET** — Pre-market | Winning virtual strategies (based on completed evaluation windows) promoted. New branches created for today's experiments. |

### C2.4 Championship Belt — Rotation & Culling

The "championship belt" model (implemented in `scripts/virtual_rotate.py` and `scripts/virtual_cull.py`):

```
Every N days (N = eval_window_days for each variant):

  For each evaluation window group:
    Rank all variants by cumulative P&L over their evaluation window
    Top 3: "contenders" — stay active, may get promoted
    Middle: "challengers" — stay active, one more cycle
    Bottom: "culled" — removed from active set
    Last: "pariah" — recorded as negative example, never reused

  Culled variants are tagged in DB: status = 'culled', reason = 'rank_bottom_N'
  Contenders are compared against the real trader's performance
```

### C2.5 Promotion Pipeline

A virtual variant becomes a real trader update when:

1. **Variant completes its evaluation window** (1d, 5d, 20d, or 90d)
2. **Objective score computed over the FULL window**
3. **Compared against:**
   - Real trader's performance over the same window
   - Other virtual variants with the same evaluation window
   - Baseline (previous best params)
4. **If improvement > threshold** (e.g., 5% better than baseline):
   - Create PR to `paper-trading-agents` repo
   - Merge to main → deployed to real OpenClaw agent
5. **If no improvement:** log "no improvement found" with window context
6. **If performance degraded:** log "regression detected" — do not promote

**Key insight:** A strategy that looks bad on day 1 might be excellent on day 5.
Never kill a variant early unless it hits a hard drawdown limit (>15%).

### C2.6 Git Branching Strategy

Each virtual trader operates on its own git branch in `paper-trading-agents`:

```
main                    — Current production prompts for real traders
├── kairos/             — Kairos's current AGENTS.md etc.
├── aldridge/           — Aldridge's current files
└── stonks/             — Stonks's current files

branches/
├── kairos/experiment-conf-0.25/    — Variant: lower confidence threshold
├── kairos/experiment-size-2pct/    — Variant: smaller position size
├── kairos/sweep-2026-07-09/        — Nightly sweep results
├── aldridge/fundamental-screener/  — Variant: different screening approach
└── stonks/social-signal-boost/     — Variant: weighted social signals
```

**Correlation data:** Over time, branches + their objective metrics give us data to correlate:
- Which prompt phrasing → best P&L?
- Which param values → best win rate?
- Which strategy → best Calmar ratio?

**Pruning rules (runs nightly):**

| Branch pattern | Delete after | Condition |
|---|---|---|
| `branches/<trader>/experiment-*` | 14 days | If no PR opened and no commits in 7 days |
| `branches/<trader>/sweep-*` | 7 days | Sweep branches are disposable |
| Tags | Never | Immutable release history |

---

## §C3 — Multi-Timeframe Evaluation

Not all strategies can be judged in a single day. Some need a week or more to show edge.

### C3.1 Evaluation Windows

| Window | What It Tests | Example |
|--------|--------------|---------|
| **1-day sprint** | Short-term timing, momentum, scalping | Kairos day-trade variants |
| **5-day week** | Swing trading, thesis development | Aldridge value plays |
| **20-day month** | Trend following, regime adaptation | Long-term holds |
| **90-day quarter** | Full strategy lifecycle | Major strategy pivots |

### C3.2 Metadata Per Variant

Each virtual variant declares its evaluation window at creation:

```json
{
  "variant_id": "kairos-momentum-v0.25-5d",
  "eval_window_days": 5,
  "start_date": "2026-07-09",
  "end_date": "2026-07-15",
  "objective_score": 0.0
}
```

The system tracks performance across ALL windows simultaneously. A strategy
negative on day 1 but positive by day 5 is not a failure — it's a swing trade.

### C3.3 Promotion by Window

| Window | Min Days Before Promotion | Auto-Promote Threshold |
|--------|--------------------------|----------------------|
| 1-day | 1 day | > 10% Calmar improvement |
| 5-day | 5 days | > 8% Calmar improvement |
| 20-day | 20 days | > 5% Calmar improvement |
| 90-day | 90 days | > 3% Calmar improvement |

Shorter windows require stronger evidence. Longer windows can promote on smaller
improvements because the evaluation is more statistically robust.

---

## §C4 — News Discovery

### C4.1 Capability

Traders can discover and read news sources dynamically to:
- Discover new stocks to watch
- Identify patterns in world news and markets
- Intuit what moves will be most profitable
- Over time: get better at reading news for trading edge

### C4.2 Implementation

The data bus provides news feeds. Traders can use browser tools for deep research
and insert findings as structured JSON into Postgres:

```json
{
  "source": "browser_research",
  "ticker": "AAPL",
  "headline": "Apple announces new AI features at WWDC",
  "sentiment": "positive",
  "relevance_score": 0.85,
  "trader_note": "This could drive upgrade cycle. Watching for follow-through."
}
```

### C4.3 Data Bus News API

| Endpoint | Description |
|----------|-------------|
| `GET /news/{ticker}` | Recent news for a ticker |
| `GET /news/trending` | Market-wide trending topics |
| `POST /news/insight` | Trader inserts a research finding |

### C4.4 Training Signal

News findings inserted by traders become training data. After the fact:
- "Did this news correlate with price movement?"
- "Did the trader correctly interpret the news?"
- "Which news sources produce the best trading signals?"

Over time, this trains traders to get better at news interpretation.

---

## §C5 — Data Flow Architecture

### C5.1 Data Bus as Sole Front

```
External APIs (Alpaca, news, financial data)
        │
        ▼
┌─────────────────────────────────┐
│         DATA BUS                │  ← Docker container on docker.klo
│  - Rate-limit aware             │     (referenced in SPEC.md §1.2)
│  - Continuously polls signals   │
│  - Caches + refreshes           │
│  - Persists EVERYTHING to disk  │
└──────────┬──────────────────────┘
           │
    ┌──────┴──────┐
    ▼              ▼
┌─────────┐  ┌──────────┐
│ Postgres│  │ Browser  │  ← Exception: manual web reading
│ (docker │  │ (manual) │    Trader inserts structured JSON
│ .klo:   │  │          │    into Postgres via data bus
│ 5433)   │  └──────────┘
└────┬────┘
     │
     ├── Feeds real traders (OpenClaw .41) via tick_prompt.py
     ├── Feeds virtual traders (Docker .179) with same data
     └── Feeds historical simulator (Docker .179) for backtests
```

### C5.2 Rules

- **Primary path:** All traders fetch data through data bus
- **Exception:** Browser/curl for manual web research → insert findings as JSON into Postgres via data bus
- **Persistence:** ALL fetched data persists to disk → feeds historical simulator → trains future traders
- **Data bus IS the front for Postgres** — traders never connect to Postgres directly

---

## §C6 — Three-Step Reflection Loop

This is the heartbeat/journal process. Every trader executes this during each
heartbeat session (separate from trading ticks — see SPEC.md §4.1).

### C6.1 Step 1 — Raw Logging (during trading)

Logged at the end of each trading tick or whenever the trader has a relevant thought:

- **Market sentiment:** Numeric 1-10 (how they feel about the market)
- **Position confidence:** Numeric 1-10 per holding (how they feel about each position)
- **Needs/blockers:** Any tool gaps, data issues, confusion
- **Other thoughts:** Free-form observations

```json
{
  "step": "raw_log",
  "timestamp": "2026-07-09T14:30:00Z",
  "market_sentiment": 7,
  "positions": {
    "AAPL": {"confidence": 8, "note": "Breakout confirmed, volume increasing"},
    "MSFT": {"confidence": 3, "note": "No catalyst yet, considering exit"}
  },
  "needs": ["sector_rotation_data"],
  "thoughts": "Market feels bullish but narrow — only tech is leading"
}
```

### C6.2 Step 2 — Journal Reflection (at HEARTBEAT)

Distill recent entries into themes:

- **Sentiment trends:** Are you becoming more bullish/bearish over the day?
- **Dominant emotions:** Excitement, fear, boredom, confusion?
- **Core values check:** Did your decisions align with your stated strategy?
- **Strategy in your own words:** Describe your current approach. Does it match your AGENTS.md?

### C6.3 Step 3 — Actionable Synthesis (at HEARTBEAT)

Structured output that feeds into the learning loop:

- **What's going well → continue**
- **What's not going well → change**
- **What's blocking trading** (specific errors, data gaps)
- **Opportunities for improvement**
- **Lessons learned backed by objective metrics:**
  - P&L (daily, weekly, total)
  - Win rate
  - Drawdown
  - Trade count
  - Confidence accuracy (did high-conviction trades outperform?)

### C6.4 Implementation Per Trader

| Trader | Reflection Status | Files |
|--------|------------------|-------|
| **Aldridge** | ✅ Deployed Jul 9 | `skills/reflection/SKILL.md` + HEARTBEAT.md updated |
| **Kairos** | 🔲 Pending | Needs HEARTBEAT.md + reflection skill |
| **Stonks** | 🔲 Pending | Needs HEARTBEAT.md + reflection skill |

---

## §C7 — Immediate Next Steps (Checklist)

Tracked in GitHub Issues #80-#88 on `Tesselation-Studios/paper-trading-rebuild`.

### P0 (Critical)

- [ ] **#80 — Dockerize data bus on docker.klo**
  Dockerfile + compose exist but need verification that the data bus actually works
  as a container. Current Dockerfile wraps `pg_dashboard.py`, not `data_bus.py`.

- [ ] **#81 — Migrate trader files to `paper-trading-agents` repo**
  Sync the ACTUAL workspaces from OpenClaw (.41) into the prompts repo.
  Current files in the agents repo are stale (Jul 6-7). The fresh versions
  (Aldridge reflection, Kairos refocus) need to be committed.

- [ ] **#82 — Three-step reflection for Kairos + Stonks**
  Copy the Aldridge pattern: `skills/reflection/SKILL.md` + updated HEARTBEAT.md.
  Aldridge already deployed — follow same format.

- [ ] **#83 — Deploy virtual runner on docker.klo**
  `docker-compose.yml` has the service. Need to deploy and verify it's running
  against Postgres and data bus containers.

- [ ] **#84 — Build historical simulator Docker container**
  Not started. Need a container that can replay historical days at accelerated speed.

- [ ] **#87 — Implement git branching strategy**
  Branch-per-variant structure defined above. Need CI/CD to enforce pruning rules.

### P1 (High)

- [ ] **#85 — Virtual trader dashboard on Canvas**
  Design spec posted. Need to wire up data fetcher and push live cards.

- [ ] **#86 — News discovery capability**
  Not started. Data bus needs `/news` endpoints. Traders need browser tool access.

- [ ] **#88 — Aldridge reflection done. Kairos + Stonks pending.**
  ✅ Aldridge done. 🔲 Others.

- [ ] **#77 — Hermes review weaknesses (tx costs, holdout, sweep validation)**
  Needs assessment. Transaction cost model was recently committed (commit 77bbce3).

### Also Tracking

- [ ] **Watchdog.py syntax error** — Broken since Jun 30, spamming canvas with idle alerts
- [ ] **tick_prompt cron missing** — Health monitor flags it every 30 min
- [ ] **Kairos bleeding** -7.1% — Fix card in review

---

## §C8 — Competition Maintenance

### C8.1 Daily EOD Check

Each trading day at ~16:00 ET, a system check confirms:
- All three traders took at least one tick
- Portfolio values computed and logged
- Daily P&L recorded
- No trader hit drawdown limits
- Benchmarks available (or logged as unavailable)

### C8.2 Weekly Summary (Every Monday)

- Rolling 30-day Calmar for each trader
- Win rate trends
- Virtual variant rotation results (winners promoted, culled logged)
- Leaderboard updated on Canvas

### C8.3 Post-Competition Analysis (Jan 1, 2027)

Winner declared. Full retrospective:
- Which strategies worked? Which didn't?
- Who beat SPY? Who didn't?
- What learned patterns carry forward?
- Competition lessons documented as `postmortems/competition-2026.md`