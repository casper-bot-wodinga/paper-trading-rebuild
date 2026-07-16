"""Fetch news headlines + sentiment for trader universe."""
import os, json, logging
from datetime import datetime, timezone
import psycopg2

PG_DSN = os.getenv("PG_DSN", "host=192.168.1.179 port=5433 dbname=trading user=trader")
UNIVERSE = ["AAPL","MSFT","NVDA","META","TSLA","AMD","PLTR","HOOD","COIN"]

def get_db():
    return psycopg2.connect(PG_DSN)

def health_check():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM market_data.news")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return {"status": "ok", "news_count": count}
    except Exception as e:
        return {"status": "error", "error": str(e)}

if __name__ == "__main__":
    print(json.dumps(health_check(), indent=2))
