# Spec: K-Means Regime Detection — Hidden Market States

**Parent**: [SPEC.md](../SPEC.md)
**Created**: 2026-07-06
**Status**: Active

---

## Problem

The current regime classifier in `src/signals.py` uses threshold-based bucketing:

```python
if momentum > 0.3 and vol < threshold: → "TRENDING_UP"
elif momentum < -0.3:                 → "TRENDING_DOWN"
elif vol > threshold:                 → "HIGH_VOLATILITY"
else:                                 → "MEAN_REVERTING"
```

This has three structural problems:

1. **Arbitrary categories.** Four regimes are hardcoded. Who decided there are exactly four? Market data doesn't care about our taxonomy.

2. **Two-feature blindness.** Only momentum and volatility are used. Real market states involve multi-dimensional relationships: volume profile, breadth, sector correlation, VIX term structure, overnight vs. intraday variance — none of which the current classifier can see.

3. **Hard boundaries create cliffs.** A stock at momentum 0.31 is "TRENDING_UP" while 0.29 is "MEAN_REVERTING." These are the same stock, microseconds apart, assigned to opposite regimes. The model has no notion of uncertainty or distance from boundaries.

## Proposed Solution: K-Means Clustering

K-means finds natural groupings in the feature space without imposing category labels or transition rules. It answers: "how many distinct market states does the data actually contain?" rather than "which of our 4 predefined buckets does this fit?"

### Feature Vector Design

Each trading day produces one feature vector. The features capture what matters for regime detection:

| Feature | Source | Rationale |
|---------|--------|-----------|
| Daily return | OHLCV close | Raw direction |
| Intraday range (high-low)/close | OHLCV | Volatility structure |
| Volume / 20-day avg volume | OHLCV | Participation signal |
| RSI(14) | Computed | Momentum/timing |
| % stocks above 50-day MA (breadth) | Alpaca or Yahoo | Market-wide participation |
| Sector correlation (avg pairwise) | Computed | Diversification vs. concentration |
| VIX change % | Yahoo/FRED | Fear gauge velocity |
| Overnight return % | Pre-market data | Gap behavior |
| 5-day realized vol | Computed | Recent stability |
| SPY/QQQ ratio change | Yahoo | Risk-on/off rotation |

**Why these 10**: They span price (return, range), momentum (RSI), participation (volume, breadth), correlation structure (sector corr, SPY/QQQ), and volatility regime (VIX, realized vol, overnight). No single dimension dominates.

### Algorithm

```
Nightly (runs at 4:05 PM ET after market close):

1. Pull last 60 trading days of feature vectors from cache
2. Standardize features (z-score: (x - μ) / σ)
3. Run k-means for k=3 through k=8
4. Select k using silhouette score (best cluster separation)
5. Label each cluster with LLM (or manual rules):
   - Feed cluster centroids to LLM → "describe this market state in 2-3 words"
   - Examples: "low vol grind up," "sector rotation," "vol crush," "risk-off cascade"
6. Store cluster assignments + centroids in `market_regimes` table
7. Today's feature vector → predict cluster → return regime label
8. Cache regime label for next trading day

During trading (data bus GET /market_regime):
  Return the cached regime label for today + cluster confidence score
```

### Why k-means not something fancier

| Algorithm | Pro | Con | Verdict |
|-----------|-----|-----|---------|
| **K-means** | Fast, interpretable, k chosen by data | Assumes spherical clusters | ✅ Best first step |
| HMM | Models state transitions | Assumes Markov dynamics, needs pre-specified k | ❌ Wrong assumption |
| DBSCAN | Finds arbitrary shapes, no k needed | Hard to label clusters, density-sensitive | ❌ Overkill for daily data |
| Gaussian Mixture | Soft assignment, covariance | More complex, same assumptions | 🟡 Upgrade path |

K-means is the right starting point. If the data warrants it, Gaussian Mixture Models are the natural upgrade — same feature vectors, same pipeline, just swap the algorithm.

### Integration Point

The `data_bus__get_market_regime` endpoint currently returns whatever `ml_worker_service.py` computes. The k-means pipeline replaces the regime computation in the ML worker, keeping the same API:

```
GET /market_regime → {
    "regime": "low_vol_grind_up",
    "regime_label": "Low Vol Grind Up",
    "confidence": 0.73,
    "cluster_id": 3,
    "k_clusters": 5,
    "feature_vector": [...],
    "nearest_centroids": {
        "low_vol_grind_up": 0.12,
        "risk_off_cascade": 1.45,
        "sector_rotation": 0.98,
        "...": ...
    }
}
```

Traders see the same `regime` field they already use. The `regime_weight` in SignalParams maps cluster labels to position sizing adjustments — LLM-assigned per cluster.

### Files Affected

| File | Change |
|------|--------|
| `src/signals.py` | Remove `_classify_regime()`, replace with `_predict_regime()` that queries k-means cache |
| `src/regime_detector.py` | NEW — k-means pipeline: feature extraction, clustering, labeling, persistence |
| `src/ml_worker_service.py` | Replace regime computation with `RegimeDetector` |
| `src/db/` | NEW — add `market_regimes` table |
| `specs/kmeans-regime.md` | This file |
| `tests/test_regime_detector.py` | NEW — unit + integration tests |

---

## Verification Criteria

### 1. Reproducibility
- [ ] Given identical feature vectors, k-means produces identical cluster assignments (fixed seed)
- [ ] Silhouette score is deterministic for a given k

### 2. Cluster quality
- [ ] Silhouette score > 0.3 for selected k (0.3 = meaningful cluster structure)
- [ ] No cluster contains < 5% of data points (trivial/outlier cluster)
- [ ] Cluster centroids are meaningfully different (any two centroids differ by > 0.5σ on at least one feature)

### 3. Stability
- [ ] Adding 5 days of new data doesn't change > 20% of historical cluster assignments
- [ ] k selected by silhouette score is stable (±1) across 3 consecutive nights

### 4. Regime discrimination
- [ ] Different regimes produce measurably different trader behavior:
  - In "trending" regimes, momentum trader (Kairos) win rate > 40%
  - In "high vol" regimes, Stonks trades > 2x more than Aldridge
  - In "low vol" regimes, Aldridge win rate > Kairos win rate

### 5. Backward compatibility
- [ ] `data_bus__get_market_regime` returns same JSON structure (regime, confidence fields present)
- [ ] Existing trader prompts parse regime labels without errors
- [ ] SignalReport.regime field still populated

### 6. Nightly integration
- [ ] Pipeline runs within 30 seconds on 60 days × 10 features
- [ ] Fails gracefully if less than 20 days of data available (falls back to rule-based)
- [ ] Writes results to `market_regimes` table with timestamp

### 7. Simulation validation (the real test)
- [ ] Run 60-day backtest with k-means regimes vs. current rule-based regimes
- [ ] K-means regime classification produces > 10% improvement in composite signal accuracy
- [ ] Walk-forward: train on days 1-30, predict day 31, slide forward, compare accuracy
- [ ] No single regime dominates > 50% of trading days (must have real variety)

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Too few data points → unstable clusters | Minimum 30 days; fall back to rule-based below that |
| All data points cluster into 1-2 groups | Increase feature count or reduce k range |
| Regime labels change daily | Cache labels for N days; only update if centroid shift > threshold |
| LLM labels clusters poorly | Use human-labeled examples as few-shot context in the labeling prompt |
| K-means assumes equal cluster sizes | Not a problem for daily regimes — markets don't produce equal-sized states, and k-means handles unequal clusters fine |

---

## Open Questions

1. **Feature selection**: Are 10 features the right set? Start with 10, measure feature importance via centroid analysis after 2 weeks, drop features that never contribute to cluster separation.
2. **Labeling approach**: LLM (cheap flash model) or heuristic rules? Start with LLM labels, validate against human expectations, add heuristic fallback if LLM is inconsistent.
3. **Confidence metric**: Distance to nearest centroid as confidence proxy, or something else? Start with normalized distance (closer = more confident), measure if it correlates with regime stability over time.
4. **What happens when k changes?** If silhouette score picks k=4 some nights and k=6 others, trader strategies need to handle variable regime counts. Solution: traders reference regime by label (string), not index. Labels are persistent across retrains.
