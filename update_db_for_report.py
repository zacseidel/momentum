# update_db_for_report.py
import sqlite3
import pathlib

DB_PATH = pathlib.Path("data/market_data.sqlite")

def update():
    with sqlite3.connect(DB_PATH) as conn:
        print("🛠 Adding metadata tables...")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS company_metadata (
                ticker TEXT PRIMARY KEY,
                name TEXT,
                description TEXT,
                sector TEXT,
                url TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS company_news (
                id TEXT PRIMARY KEY,
                ticker TEXT,
                headline TEXT,
                url TEXT,
                published_utc TEXT,
                summary TEXT
            )
        """)
        print("✅ Done.")

if __name__ == "__main__":
    update()