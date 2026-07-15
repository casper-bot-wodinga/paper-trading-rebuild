#!/usr/bin/env python3
"""Check and fix Stonks' missing executed_trades table"""
import psycopg2, sys

PG_DSN = os.getenv("FIX_STONKS_DB_URL", "postgresql://trader:***@trading-db:5432/trading")
conn = psycopg2.connect(PG_DSN)
cur = conn.cursor()

# Check tables
cur.execute("SELECT table_name, table_type FROM information_schema.tables WHERE table_schema='trading' ORDER BY table_name")
print("📋 Trading tables:")
for r in cur.fetchall():
    print(f"  {r[0]:30s} ({r[1]})")

# Check executed_trades specifically
cur.execute("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_schema='trading' AND table_name='executed_trades')")
exists = cur.fetchone()[0]
print(f"\n📊 executed_trades table exists: {exists}")

if not exists:
    print("\n🔧 Creating executed_trades table...")
    cur.execute("""
        CREATE TABLE trading.executed_trades (
            id SERIAL PRIMARY KEY,
            agent_id VARCHAR(50) NOT NULL,
            ticker VARCHAR(10) NOT NULL,
            action VARCHAR(10) NOT NULL,
            shares INTEGER NOT NULL,
            price DECIMAL(10,2) NOT NULL,
            stop_loss DECIMAL(10,2),
            pnl DECIMAL(10,2),
            entry_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            exit_time TIMESTAMPTZ,
            status VARCHAR(20) DEFAULT 'open',
            rationale TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    conn.commit()
    print("✅ Created!")
    
    # Also create decisions table if missing
    cur.execute("SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_schema='trading' AND table_name='decisions')")
    if not cur.fetchone()[0]:
        cur.execute("""
            CREATE TABLE trading.decisions (
                id SERIAL PRIMARY KEY,
                agent_id VARCHAR(50) NOT NULL,
                ticker VARCHAR(10),
                action VARCHAR(10) NOT NULL,
                conviction DECIMAL(5,2),
                rationale TEXT,
                timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.commit()
        print("✅ Created decisions table too!")
else:
    # Check columns
    cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema='trading' AND table_name='executed_trades' ORDER BY ordinal_position")
    print(f"\n📋 executed_trades columns:")
    for r in cur.fetchall():
        print(f"  {r[0]:20s} ({r[1]})")
    
    # Check Stonks data
    cur.execute("SELECT COUNT(*) FROM trading.executed_trades WHERE agent_id='trader-stonks'")
    count = cur.fetchone()[0]
    print(f"\n📊 Stonks trades: {count}")

cur.close()
conn.close()
print("\n✅ Done!")