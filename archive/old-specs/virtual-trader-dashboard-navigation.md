# Virtual Trader Dashboard — Navigation & User Flow Design

> **Spec for:** Issue #85
> **Status:** Design — implementation deferred
> **Updated:** 2026-07-12
> **Parent:** [SPEC.md](../SPEC.md)
> **Depends on:** Virtual trader runner deployed (`virtual_runner.py`, `virtual_rotate.py`, `virtual_cull.py`)

---

## 1. Overview

The virtual trader dashboard shows the live state of **all virtual traders** across three base strategies (Kairos, Aldridge, Stonks). It lives on the `trading` Canvas board and supplements the existing command center (`:5002`).

**Primary audience:** Raf (operator), Hermes (orchestrator) — glance-and-understand visibility into virtual trader health, rotation, and learning loop outcomes.

### Existing dashboard landscape

| View | Location | Purpose |
|------|----------|---------|
| **Leaderboard UI** | `:5002` (Flask) | Live portfolio values, recent decisions, journal, positions, options chain |
| **System Health** | Canvas `trading` board | Circuit breakers, alerts, metrics, test results |
| **Trader Diagnostics** | `:5002/trader-debug` | DB stats, API keys, ML worker health, MCP servers |
| **This spec →** | Canvas `trading` board + optional `:5005` (new) | Virtual trader cards, rotation history, parameter comparison |

---

## 2. Navigation Structure

### 2.1 Primary Views

```
┌────────────────────────────────────────────────────────────────────┐
│                    VIRTUAL TRADER DASHBOARD                        │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  [1. Summary Bar]  ← Pinned top. Always visible on every view.    │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  2. TRADER GRID VIEW  (default/landing)                      │  │
│  │                                                              │  │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐           │  │
│  │  │ Kairos  │ │ Kairos  │ │ Kairos  │ │Kairos   │           │  │
│  │  │ (live)  │ │(looser) │ │(tighter)│ │(aggro)  │           │  │
│  │  └─────────┘ └─────────┘ └─────────┘ └─────────┘           │  │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐           │  │
│  │  │Aldridge │ │Aldridge │ │Aldridge │ │Aldridge │           │  │
│  │  │ (live)  │ │(deep v) │ │(wide n) │ │(small)  │           │  │
│  │  └─────────┘ └─────────┘ └─────────┘ └─────────┘           │  │
│  │  ...more cards                                              │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  [3. Rotation Log]  ← Latest daily rotation events                │
│  [4. Parameter & Learning Loop]  ← What changed, win rate          │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

### 2.2 View Hierarchy

| View | Route / Access | Content | Update Cadence |
|------|---------------|---------|----------------|
| **Summary Bar** | Top of `trading` board, always present | Active/total virtuals, avg P&L, last rotation, last refresh time | Every 5 min |
| **Trader Grid** | Default landing view | All virtual trader cards (9-24 cards depending on culling state) | Every 5 min |
| **Trader Detail** | Click card → card expands | Full stats: params, performance, decisions, journal | On click (data fresh) |
| **Rotation Log** | Scroll below grid | Daily rotation history, promotion/cull events | Once per day (static) |
| **Comparison View** | Tab or toggle on grid | Side-by-side param vs performance, leaderboard ranking | Every 5 min |
| **Parameter History** | Modal or accordion | Timeseries: param drift, win rate over time | Every 5 min |
| **Learning Loop** | Dedicated section | Win rate of changes, last synthesis output | Every 5 min |

### 2.3 Canvas Card Layout on `trading` Board

The `trading` board already has a system health card. We add:

| Card Title | Type | Refreshes | Position |
|-----------|------|-----------|----------|
| `🩺 System Health` (existing) | markdown | Every 5 min | Top-left |
| `📊 Virtual Trader Summary` (new) | markdown | Every 5 min | Top, next to health |
| `🎯 Kairos Stable` (new) | markdown | Every 5 min | Row 2 |
| `🎯 Aldridge Stable` (new) | markdown | Every 5 min | Row 2 |
| `🎯 Stonks Stable` (new) | markdown | Every 5 min | Row 2 |
| `🔄 Daily Rotation` (new) | markdown | Once daily | Below cards |
| `📈 Learning Loop` (new) | markdown | Every 5 min | Below rotation |

Each **virtual trader card** contains:
- Name, base trader, variant type
- Status badge: 🟢 trading / ⏸ paused / 🟡 probation / 🔴 error
- Current P&L and win rate
- Last decision (action, ticker, timestamp)
- Current param overrides (top 3-5)
- Mini sparklines for P&L over last 5 ticks

---

## 3. User Flows

### 3.1 Flow A: View All → Drill Into One → View History → Adjust Params

```
Landing (Trader Grid)
  │
  ├── Scan: Summary bar shows "24 virtuals active, avg P&L -$12.30"
  │
  ├── Scan: All cards show green/red P&L at a glance
  │         → Notice "kairos-looser" has +$87 P&L (best in stable)
  │         → Notice "stonks-hype" has 🔴 error badge
  │
  ├── Click "kairos-looser" card
  │     │
  │     ├── Card expands to Detail view (inline expand, not modal)
  │     │     ├── Full config: {rsi_oversold: 20, base_size: 0.15}
  │     │     ├── Performance breakdown:
  │     │     │     ├── Daily P&L: +$42.50
  │     │     │     ├── Weekly P&L: +$87.10
  │     │     │     ├── Win rate: 63% (12W / 7L)
  │     │     │     ├── Trade count: 19
  │     │     │     └── Avg hold time: 2.3 days
  │     │     ├── Last 5 decisions (scrollable list)
  │     │     ├── Param change history (timeline)
  │     │     └── Learning loop score: 0.73 (above baseline)
  │     │
  │     ├── Click "Compare with base" → Comparison panel slides in
  │     │     ├── Side-by-side: kairos-looser vs kairos-live
  │     │     ├── Metric radar chart (win rate, P&L, Sharpe, drawdown)
  │     │     └── "This virtual is outperforming the live trader by +2.1%"
  │     │
  │     └── Click "Adjust params" → Parameter editor panel
  │           ├── Current: rsi_oversold=20, base_size=0.15
  │           ├── Sliders & presets for safe ranges
  │           ├── "Apply and create new variant" button
  │           └── Confirmation: "New variant kairos-looser-v2 created"
  │
  └── Click "View history" → Rotation Log view
        ├── Timeline of promotions/culls for last 14 days
        ├── Each event: date, trader, reason, P&L delta
        └── Highlighted: "kairos-looser promoted on 2026-07-10"
```

### 3.2 Flow B: Monitor Rotation (Daily Check)

```
Landing → Scroll down to Rotation Log section
  │
  ├── Today's rotation status:
  │     ├── 🏆 Current LIVE: kairos-tighter (promoted yesterday)
  │     ├── 📊 Yesterday's ranking: 1. kairos-tighter (+$34), 2. kairos-looser (+$21)...
  │     ├── 🗑 Culled last week: kairos-small, stonks-contrarian, aldridge-big
  │     └── 🆕 New this week: kairos-v2, stonks-hybrid
  │
  └── Click any culled trader → Detail of why it failed
```

### 3.3 Flow C: Learning Loop Inspection

```
Landing → Scroll to Learning Loop section
  │
  ├── Current learning state:
  │     ├── Win rate of param changes: +2.3% (improving)
  │     ├── Last parameter sweep result at 2026-07-11 20:00
  │     │     ├── Variants tested: 8
  │     │     ├── Top scorer: kairos-v2 (win rate 58%)
  │     │     └── ⬆ Promoted: kairos-v2 → replaces kairos-tighter
  │     └── Nightly synthesis: "Kairos benefits from lower RSI thresholds
  │         in trending markets. Current regime favors momentum."
  │
  └── Click "Full sweep report" → links to nightly report in `reports/`
```

### 3.4 Flow D: Error Investigation

```
Landing → See 🔴 error badge on "stonks-hype"
  │
  ├── Click card → Detail view
  │     ├── Error: "Signal engine returned NaN for volume_pct"
  │     ├── Last successful tick: 2026-07-12 09:45:23
  │     ├── Elapsed since error: 2.3 hours
  │     └── Suggested fix: "Increase volume threshold or disable volume filter"
  │
  └── Click "Exclude from rotation" → Virtual marked as `degraded`
        → Rotation system will not consider it for promotion
        → Dashboard shows 🟡 degraded badge
```

---

## 4. Filtering System

### 4.1 Filter Bar (persistent across grid view)

```
┌─────────────────────────────────────────────────────────────────────┐
│ [All Traders] │ Status: [All 🟢 Active 🔴 Error 🟡 Probation]     │
│ Performance: [Best   Worst   Newest   Oldest]                       │
│ Base Trader: [All   Kairos   Aldridge   Stonks]                     │
│ Variant Type: [All   Params   Prompt   Portfolio   Regime]          │
│ Search: [_________________]  Reset                                  │
│ Showing 12 of 24 virtual traders                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 4.2 Filter Dimensions

| Filter | Options | Behavior |
|--------|---------|----------|
| **Status** | All, Active, Error, Probation, Paused, Degraded, Culled | Single-select OR any combination of checkboxes |
| **Performance** | Best (highest P&L), Worst, Newest, Oldest, Most volatile | Sort mode, combined with other filters |
| **Base Trader** | All, Kairos, Aldridge, Stonks | Multi-select (show both Kairos and Aldridge virtuals) |
| **Variant Type** | All, Params, Prompt, Portfolio, Regime | Multi-select |
| **Search** | Free text | Matches name, ticker, param values (e.g. "rsi_oversold") |
| **Win Rate Range** | Slider 0-100% | Combined with other filters |

### 4.3 Default Views (presets)

| Preset | Filter | Use Case |
|--------|--------|----------|
| `🔄 All Active` | Status=Active, sort by P&L descending | Default landing |
| `🏆 Top Performers` | P&L > 0, sort by P&L | Best strategies |
| `⚠️ Needs Attention` | Status=Error OR Status=Degraded | Fires to fix |
| `🆕 New This Week` | Created within 7 days | Monitor probation virtuals |
| `📈 Learning Loop` | Params changed in last 24h | See what's being explored |

### 4.4 Filter State Persistence

- Filters survive page refresh (localStorage or Canvas query params)
- Preset selection stored in Canvas card content (other cards can link to filtered views)
- URL-encoded filter state in Canvas card links

---

## 5. Comparison & Leaderboard Views

### 5.1 Leaderboard Card (Summary)

Single Canvas card showing:

```
┌─────────────────────────────────────────────────────┐
│ 🏆 VIRTUAL TRADER LEADERBOARD                        │
│ Updated: 2026-07-12 12:30 ET                         │
├─────────────────────────────────────────────────────┤
│ Rank │ Trader          │ P&L      │ Win Rate │ WR ↑ │
│ ─────┼─────────────────┼──────────┼──────────┼────── │
│ 🥇   │ kairos-looser   │ +$87.10  │ 63%      │ +12%  │
│ 🥈   │ kairos-tighter  │ +$34.50  │ 55%      │ +4%   │
│ 🥉   │ aldridge-deep-v | +$21.30  │ 52%      │ -1%   │
│ 4    │ kairos-live     │ +$12.80  │ 50%      │ —     │
│ ...  │ ...             │ ...      │ ...      │ ...   │
│ 24   │ stonks-hype     │ -$89.20  │ 18%      │ -32%  │
├─────────────────────────────────────────────────────┤
│ Base trader win rates by stable:                     │
│   Kairos:  4/8 virtuals positive  avg +$23.40       │
│   Aldridge: 3/8 virtuals positive avg -$5.10        │
│   Stonks:   2/8 virtuals positive  avg -$42.80      │
└─────────────────────────────────────────────────────┘
```

### 5.2 Side-by-Side Comparison

When two cards are selected (checkboxes in grid), a comparison panel appears:

```
┌── kairos-looser ──┬── kairos-live (base) ──┐
│ P&L: +$87.10      │ P&L: +$12.80            │
│ Win Rate: 63%     │ Win Rate: 50%            │
│ Trades: 19        │ Trades: 47               │
│ Max DD: 5.2%      │ Max DD: 14.7%            │
│ Score: 0.73       │ Score: 0.41              │
│ Avg Hold: 2.3d    │ Avg Hold: 1.1d           │
│ RSI Oversold: 20  │ RSI Oversold: 30         │
│ Base Size: 0.15   │ Base Size: 0.10          │
├──────────────────┼─────────────────────────┤
│ 🟢 Outperforming   │                        │
│ in all metrics     │                        │
└──────────────────┴─────────────────────────┘
```

### 5.3 Metric Radar Charts

For the comparison view, render a simple ASCII radar or use HTML Canvas:

```
Metrics compared (normalized 0-1):
                  Sharpe
                    ↑
                  / \
         Win Rate ←   → P&L
                 /     \
          Drawdown ←   → Trade Count
              (inverted) \
                    Score
```

### 5.4 Cross-Trader Insights

| Insight Card | Data | Refreshes |
|-------------|------|-----------|
| **Best variant by base trader** | Top virtual for Kairos / Aldridge / Stonks | Daily |
| **Common winning params** | Aggregate: "Winning Kairos virtuals use RSI 20-25, base_size 0.12-0.18" | Daily |
| **Regime correlation** | "In TRENDING_UP regimes, momentum variants outperform value variants 2:1" | Weekly |
| **Rotation velocity** | "4 promotions this week, avg 2.3 days between rotations" | Weekly |

---

## 6. Mobile vs Desktop Considerations

### 6.1 Current Constraints

- Canvas renders markdown cards — no custom responsive layout
- The `:5002` flask dashboard has its own HTML/CSS (responsive grid)
- Mobile users: Raf via phone, Hermes via webhook (text-only)

### 6.2 Three-Tier Rendering Strategy

| Tier | Channel | Format | Detail Level |
|------|---------|--------|-------------|
| **Desktop** | Canvas `trading` board | Markdown cards + HTML detail panels | Full (grid + detail + charts) |
| **Mobile** | Canvas mobile browser | Collapsed markdown cards | Summary only (name, status, P&L) |
| **Text-only** | Hermes bridge / chat | Compact text | "24 virtuals. Best: kairos-looser +$87" |

### 6.3 Responsive Layout Rules

**Desktop (>1024px):**
- Summary bar in full-width card
- Trader grid: 4-6 columns (24 virtuals → 4-6 rows)
- Detail panel slides in as overlay
- Filters: horizontal bar with dropdowns

**Tablet (768-1024px):**
- Trader grid: 2-3 columns
- Detail panel: inline expand (below card, not overlay)
- Filters: collapsed hamburger menu

**Mobile (<768px):**
- Trader grid: 1 column, stacked cards
- Cards show only: name, status dot, P&L, last action
- Detail: full-screen overlay with swipe-to-close
- Filters: bottom sheet drawer
- Comparison: not available (link to desktop view)

### 6.4 Canvas Markdown Card Optimization for Mobile

Each card should have a mobile-optimized version:
- **Full card** (desktop): Tables, sparklines, detailed stats
- **Compact card** (mobile): Single line, status + P&L only
- Cards auto-detect viewport via Canvas API or serve both and let Canvas choose

Example mobile-friendly compact card:

```
🟢 kairos-looser  |  +$87.10  |  63% WR  |  19 trades
🔴 stonks-hype    |  -$89.20  |  18% WR  |  ERROR
```

---

## 7. Integration with Existing Canvas Card System

### 7.1 Canvas API Surface

The existing `canvas_dashboard.py` (`_push_to_canvas`) supports:

| Feature | Supported? | Notes |
|----------|-----------|-------|
| Create cards | ✅ | `_push_to_canvas()` |
| Update in-place | ✅ | Pass `card_id` to overwrite |
| Expire cards | ✅ | `expires_days` parameter |
| Board scoping | ✅ | `board` parameter |
| Agent attribution | ✅ | `agent`, `agent_emoji` |

### 7.2 New: `virtual_dashboard.py` Script

New script at `scripts/push_virtual_dashboard.py` — runs every 5 min via cron:

```python
"""
Push virtual trader dashboard to Canvas.

Cron (every 5 min during market hours):
    */5 * * * 1-5 cd ~/projects/paper-trading-rebuild && \
        python3 scripts/push_virtual_dashboard.py \
            >> logs/push_virtual_dashboard.log 2>&1

Creates/updates cards on the `trading` board:
    1. Summary card — total virtuals, avg P&L, last rotation
    2. One card per virtual trader — name, status, params, P&L, last decision
    3. Rotation log card — daily rotation events
    4. Learning loop card — param changes, win rate, synthesis
"""
```

### 7.3 Card Lifecycle

| Event | Action | Card ID |
|-------|--------|---------|
| Dashboard init | Create cards | `vs-{name}` for each virtual, `vs-summary`, `vs-rotation`, `vs-learning` |
| Every 5 min | Update all cards in-place | Same card IDs, updated content |
| Virtual culled | Stop updating card, add note: "🗑 Culled on 2026-07-12" | Card expired after 7 days |
| Virtual promoted | Update card status: "🏆 PROMOTED TO LIVE" | Same card, updated content + expires in 30 days |
| Virtual created | Create new card | New card ID, 24h expiry |
| Market closed | Pause refreshes, keep last snapshot | Cards stale but visible |

### 7.4 Card Content Templates

**Summary Card:**
```markdown
## 📊 Virtual Trader Summary
**Updated:** 2026-07-12 12:30:00 ET

| Metric | Value |
|--------|-------|
| Total virtuals | 24 (8 Kairos · 8 Aldridge · 8 Stonks) |
| Active | 21 |
| Error/Degraded | 2 (stonks-hype, stonks-contrarian) |
| Probation | 3 (new this week) |
| Best virtual | kairos-looser (+$87.10) |
| Worst virtual | stonks-hype (-$89.20) |
| Avg P&L across all | -$4.30 |
| Positive P&L | 9/24 (37.5%) |
| Last rotation | 2026-07-11 (kairos-tighter promoted) |
```

**Virtual Trader Card:**
```markdown
## 🎯 kairos-looser
**Base:** Kairos · **Type:** Params · **Status:** 🟢 Trading

| Metric | Value |
|--------|-------|
| P&L | **+$87.10** (+0.87%) |
| Win Rate | **63%** (12W/7L) |
| Trades | 19 |
| Last Decision | BUY AAPL @ $234.50 (12:28 ET) |
| Created | 2026-07-05 |

**Params:**
- `rsi_oversold`: 20 (base: 30 ↓)
- `base_size`: 0.15 (base: 0.10 ↑)

**Learning Loop Score:** 0.73 (above baseline)
```

**Rotation Log Card:**
```markdown
## 🔄 Rotation Log — Last 7 Days
| Date | Champion | Promoted? | Culled |
|------|----------|-----------|--------|
| Jul 12 | kairos-tighter | — | — |
| Jul 11 | kairos-looser | → kairos-tighter | stonks-contrarian |
| Jul 10 | kairos-patient | → kairos-looser | kairos-aggro |
| Jul 9 | kairos-prompt-v2 | → kairos-patient | aldridge-big |
| Jul 8 | kairos-live | — | — |
| Jul 7 | kairos-tighter | → kairos-live | stonks-small |
| Jul 6 | kairos-live | — | — |
```

**Learning Loop Card:**
```markdown
## 📈 Learning Loop
**Last synthesis:** 2026-07-11 20:00 ET

| Metric | Value |
|--------|-------|
| Param change win rate | +2.3% over 7 days |
| Sweep variants tested | 8 |
| Top scorer | kairos-v2 (58% WR) |
| ⬆ Promoted | kairos-v2 → LIVE |

**Synthesis summary:**
> Kairos benefits from lower RSI thresholds in trending markets.
> Current regime favors momentum. Volume filter tuning shows
> highest impact — widening from 0.30 to 0.50 improved trade
> frequency by +40% without reducing win rate.

**Active experiments:**
| Variant | Changed | Expected Impact |
|---------|---------|-----------------|
| kairos-wide-stops | Jul 11 | Reduce max DD |
| aldridge-deep-value | Jul 10 | Test value premium |
| stonks-hybrid | Jul 9 | Regime-aware sizing |
```

### 7.5 Integration with Existing Canva Push Scripts

The new `push_virtual_dashboard.py` runs separately from `push_observability_board.py`:

| Script | Board | Cadence | Cards |
|--------|-------|---------|-------|
| `push_observability_board.py` | `trading` | Every 5 min | System health (circuit breakers, alerts, metrics, tests) |
| `push_virtual_dashboard.py` (new) | `trading` | Every 5 min | Virtual summary, trader cards, rotation, learning loop |

Both scripts can coexist without collision since they use different card IDs.

### 7.6 Postgres Data Sources

| Data | Table | Query Pattern |
|------|-------|--------------|
| Virtual trader registry | `trading.virtual_traders` | `SELECT * WHERE status IN ('active','probation','live')` |
| Virtual trades | `trading.executed_trades` | `SELECT * WHERE trade_source='virtual' AND agent_id LIKE 'virt-%'` |
| Virtual decisions | `trading.decisions` | `SELECT * WHERE source='virtual'` (if separated) or join via agent_id |
| Parameter history | `trading.param_history` | `SELECT * WHERE agent_id = virt-name` |
| Rotation log | `trading.rotation_log` | `SELECT * ORDER BY date DESC LIMIT 14` |
| Portfolio snapshots | `trading.portfolio_snapshots` | `SELECT * WHERE trader_id = virt-name` |
| Agent profile | `trading.agent_profile` | `SELECT performance WHERE agent_id = virt-name` |

---

## 8. Performance & Scalability

### 8.1 Card Count Limits

- Canvas supports 20-30 cards per board before scrolling becomes unwieldy
- With 24 virtual traders (8 × 3) + 3-4 summary/rotation/Learning cards = **27-28 cards total**
- **Stays within practical limits** — no need for virtual scrolling in v1

### 8.2 Data Freshness

| Card Type | Acceptable Staleness | Refresh Mechanism |
|-----------|---------------------|-------------------|
| Summary | 5 min | Cron poll |
| Trader cards | 5 min | Cron poll |
| Rotation log | 24h | Static update at 20:00 ET |
| Learning loop | 5 min | Cron poll (nightly synthesis when fresh) |

### 8.3 Query Optimization

- Batch Postgres queries per cron cycle (single connection, multi-query)
- Cache rotation log data (only changes once/day)
- Use `portfolio_snapshots` table instead of computing P&L from trade history each time
- Param history query limited to last 30 days per virtual

---

## 9. Implementation Phases

| Phase | Scope | Effort | Dependencies |
|-------|-------|--------|-------------|
| **P0** | `scripts/push_virtual_dashboard.py` — summary card + trader cards. One-shot push. | ~200 lines Python | Virtual trader tables exist in Postgres |
| **P1** | Rotation log card + learning loop card. Detail expand on card click (inline). | ~150 lines | P0 done |
| **P2** | Filter bar implementation. Preset views. Search. | ~200 lines JS + API | `:5005` web view built |
| **P3** | Comparison view. Side-by-side. Radar charts (ASCII or Canvas HTML). | ~250 lines | P2 done |
| **P4** | Mobile optimization. Text-only bridge messages. Responsive breakpoints. | ~100 lines | P0-P3 done |

**Phase P0 is the minimum viable dashboard** — summary + individual cards that auto-refresh. Everything else is iterative polish.

---

## 10. Appendix: Current State Assessment

### Already exists in the codebase

| Component | Lines | Reuse Strategy |
|-----------|-------|----------------|
| `canvas_dashboard.py` | ~250 | Reuse `_push_to_canvas()`, `_load_canvas_credentials()` |
| `leaderboard_api.py` | ~900 | Reuse Postgres query patterns, portfolio fetching |
| `pg_dashboard.py` | ~100 | Reference for Postgres query structure |
| `virtual_runner.py` | 0 (not yet built) | Will define virtual trader naming and config |
| `virtual_traders.md` spec | ~80 | Virtual trader schema and lifecycle |

### Gaps to fill

1. Virtual trader cards have no Canvas rendering code yet (P0)
2. No daily rotation log summary format (P1)
3. No comparison/leaderboard logic (P3)
4. No filter/preset architecture (P2)
5. No mobile-compact card format (P4)
6. No bridge/chat text summary for Hermes (P4)

---

> **Next step:** Implement `scripts/push_virtual_dashboard.py` (P0) — see `.tasks/ready/virtual-trader-dashboard.md` for implementation task.