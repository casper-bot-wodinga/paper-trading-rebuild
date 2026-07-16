"""Meta-cog loop: tracks whether learning loop changes improve P&L."""
import os, logging
from datetime import datetime, timedelta
import psycopg2

PG_DSN = os.getenv("PG_DSN", "host=192.168.1.179 port=5433 dbname=trading user=trader")

def get_db():
    return psycopg2.connect(PG_DSN)

def get_loop_win_rate(days=7):
    """% of param changes that improved subsequent P&L."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        WITH param_changes AS (
            SELECT created_at, param_name, old_value, new_value
            FROM trading.param_history
            WHERE created_at >= now() - interval '%s days'
        )
        SELECT
            pc.param_name,
            pc.created_at,
            COALESCE(
                (SELECT sum(t.pnl) FROM trading.trades t
                 WHERE t.created_at >= pc.created_at
                   AND t.created_at < pc.created_at + interval '2 hours'
                   AND t.agent_id LIKE '%%' || pc.param_name || '%%'),
                0
            ) AS subsequent_pnl
        FROM param_changes pc
        ORDER BY pc.created_at DESC
    """, [days])
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def health_check():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM trading.param_history")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return {"status": "ok", "param_history_count": count}
    except Exception as e:
        return {"status": "error", "error": str(e)}

if __name__ == "__main__":
    print(health_check())
