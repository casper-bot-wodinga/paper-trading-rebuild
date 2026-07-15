#!/usr/bin/env python3
"""Simple regime query — returns current SPY regime from K-Means model"""
import sys, json, os
sys.path.insert(0, '/home/openclaw/projects/paper-trading-rebuild')

MODEL_PATH = '/home/openclaw/data/regime_kmeans.pkl'
DB_URL = os.getenv("REGIME_CRON_DB_URL", "postgresql://trader:@trading-db:5432/trading")

def get_regime():
    """Query or train regime detector, return JSON"""
    from src.regime_detector import RegimeDetector
    import psycopg2, psycopg2.extras
    import numpy as np
    
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT symbol, date, open, high, low, close, volume FROM market_data.bars_1d WHERE symbol='SPY' ORDER BY date ASC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    
    if len(rows) < 50:
        return {"regime": "unknown", "label": "unknown", "error": f"Not enough data ({len(rows)} rows)"}
    
    # Build price list
    prices = [float(r["close"]) for r in rows]
    volumes = [int(r["volume"]) for r in rows]
    highs = [float(r["high"]) for r in rows]
    lows = [float(r["low"]) for r in rows]
    
    # If model exists, load and predict on last 21 days
    if os.path.exists(MODEL_PATH):
        detector = RegimeDetector(k=5, model_path=MODEL_PATH)
        detector._load()
        if detector._kmeans is not None:
            # Build feature vector same way as fit()
            from src.regime_detector import REGIME_LABELS
            prices_np = np.array(prices[-120:])  # last 120 days for features
            
            # Compute features (simplified)
            n = len(prices_np)
            if n < 21:
                return {"regime": "unknown", "label": "unknown", "error": "Too few prices"}
            
            # Approximate features using last values
            recent = prices_np[-21:]
            features = {}
            if n >= 5:
                features["mom_5d"] = (recent[-1] / recent[-5] - 1) * 100
            if n >= 20:
                features["mom_20d"] = (recent[-1] / recent[-20] - 1) * 100
            if n >= 50:
                features["mom_50d"] = (prices_np[-1] / prices_np[-50] - 1) * 100
            
            # RSI-14
            gains, losses = 0, 0
            for i in range(-14, -1):
                change = prices_np[i] - prices_np[i-1]
                if change > 0: gains += change
                else: losses -= change
            rsi_val = 100 - (100 / (1 + (gains/14) / (max(losses/14, 0.001))))
            features["rsi_14"] = rsi_val
            
            # MACD
            ema12 = sum(recent[-12:]) / 12
            ema26 = sum(recent[-26:]) / 26 if len(recent) >= 26 else ema12
            features["macd_diff"] = ema12 - ema26
            
            # ATR%
            tr_sum = 0
            for i in range(-14, -1):
                tr = max(highs[i] - lows[i], abs(highs[i] - prices[i-1]), abs(lows[i] - prices[i-1]))
                tr_sum += tr
            features["atr_pct"] = (tr_sum / 14) / recent[-1] * 100
            
            # Volume trend
            vol_recent = sum(volumes[-5:]) / 5
            vol_prev = sum(volumes[-10:-5]) / 5 if len(volumes) >= 10 else vol_recent
            features["vol_trend"] = (vol_recent / vol_prev - 1) * 100 if vol_prev > 0 else 0
            
            # Price velocity
            features["price_vel"] = (recent[-1] / recent[-5] - 1) * 100
            
            # Build feature vector
            X = np.array([[features.get(name, 0.0) for name in detector._feature_names]])
            X_scaled = detector._scaler.transform(X)
            cluster = int(detector._kmeans.predict(X_scaled)[0])
            distances = detector._kmeans.transform(X_scaled)[0]
            max_dist = np.max(distances)
            min_dist = distances[cluster]
            confidence = 1.0 - (min_dist / max_dist) if max_dist > 0 else 1.0
            label = detector._centroid_labels.get(cluster, REGIME_LABELS.get(cluster, f"cluster_{cluster}"))
            
            return {
                "regime": cluster,
                "label": label,
                "confidence": round(confidence, 3),
                "features": {k: round(v, 4) for k, v in features.items()},
            }
    
    return {"regime": "unknown", "label": "unknown", "error": "Model not loaded"}

if __name__ == '__main__':
    result = get_regime()
    print(json.dumps(result, indent=2))