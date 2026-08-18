# init_db.py
import sqlite3
import pathlib

DB_PATH = pathlib.Path("data/market_data.sqlite")

def initialize_database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()

        # 1. Price Data
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_prices (
                ticker TEXT,
                date   DATE,
                open   REAL, high REAL, low REAL, close REAL, volume INTEGER,
                PRIMARY KEY (ticker, date)
            )
        """)
        # The "Speed" Index (makes queries fast)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_date ON daily_prices(date)")

        # 2. Ranking History (Now with STREAK column)
        #    streak = How many consecutive weeks has this stock been in the top 10?
        schema_top10 = """
            ticker            TEXT,
            date              DATE,
            current_return    TEXT,
            last_month_return TEXT,
            last_week_return  TEXT,
            current_rank      REAL,
            last_month_rank   REAL,
            rank_change       REAL,
            streak            INTEGER DEFAULT 1,
            streak_start      DATE, 
            PRIMARY KEY (ticker, date)
        """
        
        cohorts = ["sp500", "sp400", "megacap"] 
        for c in cohorts:
            table_name = f"top10_{c}"
            cur.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({schema_top10})")
            
            # Migrations: If table exists from old runs but lacks 'streak', add it
            # (This prevents errors if you run this script on an existing DB)
            try:
                cur.execute(f"ALTER TABLE {table_name} ADD COLUMN streak INTEGER DEFAULT 1")
            except sqlite3.OperationalError:
                pass # Column already exists, ignore

        # 3. Report-only SP400 mean-reversion cohorts
        cur.execute("""
            CREATE TABLE IF NOT EXISTS top10_munger400l (
                rank INTEGER,
                ticker TEXT,
                price REAL,
                sma_200 REAL,
                sma_10 REAL,
                pct_below_200 REAL,
                dip_date DATE,
                weight REAL,
                weight_rank INTEGER,
                streak INTEGER DEFAULT 1,
                streak_start DATE,
                date DATE,
                PRIMARY KEY (ticker, date)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS top10_munger400r (
                rank INTEGER,
                ticker TEXT,
                price REAL,
                sma_200 REAL,
                sma_10 REAL,
                pct_below_200 REAL,
                dip_date DATE,
                best_12m_return REAL,
                best_return_rank INTEGER,
                return_universe_size INTEGER,
                best_return_percentile REAL,
                best_return_date DATE,
                most_recent_qualified_date DATE,
                streak INTEGER DEFAULT 1,
                streak_start DATE,
                date DATE,
                PRIMARY KEY (ticker, date)
            )
        """)

        conn.commit()
        print(f"✅ Database initialized with Streak support at {DB_PATH}")

if __name__ == "__main__":
    initialize_database()
