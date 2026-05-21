import os
import sqlite3
import asyncio
import httpx
import pandas as pd
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv
from typing import Dict, List, Set

# Import the Universe Service to get the allowed tickers
from universe import UniverseService

# --- Configuration ---
load_dotenv()
API_KEY = (os.getenv("POLYGON_API_KEY") or os.getenv("POLYGON_KEY") or "").strip()
DB_PATH = Path("data/market_data.sqlite")

if not API_KEY:
    raise RuntimeError("Missing Polygon key. Set POLYGON_API_KEY in .env")

class PriceService:
    def __init__(self):
        self.db_path = DB_PATH
        self._ensure_db()
        # Semaphore to limit concurrency (Polygon free tier limits)
        self._semaphore = asyncio.Semaphore(1) 
        self.valid_tickers = self._load_universe_tickers()

    def _ensure_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_prices (
                    ticker TEXT,
                    date   TEXT,
                    open   REAL, high REAL, low REAL, close REAL, volume INTEGER,
                    PRIMARY KEY (ticker, date)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON daily_prices(date)")

    def _load_universe_tickers(self) -> Set[str]:
        """Load Universe + Explicitly Add VOO to whitelist."""
        u = UniverseService()
        try:
            sp500 = u.get_cohort("sp500")
            sp400 = u.get_cohort("sp400")
            try:
                munger = u.get_cohort("munger")
                munger_set = set(munger.symbol)
            except:
                munger_set = set()
            
            return set(sp500.symbol) | set(sp400.symbol) | munger_set | {"VOO"}
        except Exception:
            return {"VOO"}

    # --- Public API ---

    async def resolve_target_dates(self, run_date: date) -> Dict[str, str]:
        base_date = run_date - timedelta(days=1)
        ts = pd.Timestamp(base_date)
        
        nominal_map = {
            "latest_trading":  base_date,
            "minus_1_week":    (ts - pd.Timedelta(weeks=1)).date(),
            "minus_1_month":   (ts - pd.DateOffset(months=1)).date(),
            "minus_1_year":    (ts - pd.DateOffset(years=1)).date(),
            "minus_13_months": (ts - pd.DateOffset(years=1, months=1)).date(),
        }

        resolved_map = {}
        print(f"🗓️  Resolving {len(nominal_map)} target dates...")

        for label, target in nominal_map.items():
            actual_date = await self._ensure_date_data(target)
            resolved_map[label] = actual_date.isoformat()
            
            if actual_date != target:
                print(f"   Shape-shift ({label}): Requested {target} -> Found {actual_date}")

        return resolved_map

    async def ensure_history_depth(self, tickers: List[str], days_needed: int = 300):
        """
        Ensure specific tickers (e.g. Munger cohort) have continuous daily history 
        in the DB to support SMA calculation.
        """
        print(f"🕵️ Checking history depth for {len(tickers)} tickers...")
        
        start_check = date.today() - timedelta(days=days_needed)
        start_iso = start_check.isoformat()
        
        fetches_made = 0

        for ticker in tickers:
            # 1. Check count of rows in DB since start date
            with sqlite3.connect(self.db_path) as conn:
                try:
                    count = conn.execute(
                        "SELECT count(*) FROM daily_prices WHERE ticker=? AND date >= ?", 
                        (ticker, start_iso)
                    ).fetchone()[0]
                except Exception:
                    count = 0
            
            # If we have less than ~60% of the needed days, we assume gaps/missing data
            threshold = int(days_needed * 0.6) 

            if count < threshold:
                print(f"   📉 {ticker}: Found {count} rows (need >{threshold}). Backfilling...")
                success = await self._backfill_ticker(ticker, start_check, date.today())
                
                if success:
                    fetches_made += 1
                    # POLYGON FREE TIER LIMIT: 5 calls / minute.
                    # We sleep 15s after every call to be safe (4 calls/min).
                    print("      ⏳ Sleeping 15s for rate limit...")
                    await asyncio.sleep(15)
            else:
                # print(f"   ✅ {ticker}: History ok ({count} rows).")
                pass
        
        if fetches_made > 0:
            print(f"   ✅ Backfill complete. Fetched {fetches_made} tickers.")

    async def get_snapshots(self, tickers: List[str], date_map: Dict[str, str]) -> pd.DataFrame:
        needed_dates = list(set(date_map.values()))
        if not needed_dates:
            return pd.DataFrame()

        placeholders = ",".join(["?"] * len(needed_dates))
        query = f"""
            SELECT ticker, date, close, high, low, volume 
            FROM daily_prices 
            WHERE date IN ({placeholders})
        """
        
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql_query(query, conn, params=needed_dates)
        
        target_set = set(tickers) | {"VOO"}
        return df[df['ticker'].isin(target_set)].copy()
    
    def get_ticker_history(self, ticker: str, lookback_days: int = 365) -> pd.DataFrame:
        """Synchronous fetch from SQLite for a single ticker's time series."""
        start_iso = (date.today() - timedelta(days=lookback_days)).isoformat()
        query = """
            SELECT date, close, high, low, open 
            FROM daily_prices 
            WHERE ticker = ? AND date >= ? 
            ORDER BY date ASC
        """
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql_query(query, conn, params=(ticker, start_iso))
        return df

    # --- Internal Helpers ---

    async def _ensure_date_data(self, target: date, max_backtrack=5) -> date:
        curr_date = target
        
        for _ in range(max_backtrack + 1):
            while curr_date.weekday() >= 5:
                curr_date -= timedelta(days=1)

            curr_iso = curr_date.isoformat()

            if self._is_date_in_db(curr_iso):
                await self._fetch_and_save_benchmark("VOO", curr_date)
                return curr_date

            async with self._semaphore:
                data = await self._fetch_polygon_grouped(curr_date)
            
            if data:
                self._save_to_db(data, curr_iso)
                await self._fetch_and_save_benchmark("VOO", curr_date)
                return curr_date
            
            curr_date -= timedelta(days=1)
            await asyncio.sleep(0.5)

        raise RuntimeError(f"No market data found near {target}")

    async def _fetch_and_save_benchmark(self, ticker: str, d: date):
        with sqlite3.connect(self.db_path) as conn:
            exists = conn.execute("SELECT 1 FROM daily_prices WHERE ticker=? AND date=?", 
                                (ticker, d.isoformat())).fetchone()
        if exists:
            return

        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{d}/{d}?adjusted=true&apiKey={API_KEY}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    res = resp.json().get("results", [])
                    if res:
                        r = res[0]
                        self._save_single_row(ticker, d.isoformat(), r)
                        print(f"      Use separate fetch for {ticker} on {d}")
            except Exception:
                pass

    async def _backfill_ticker(self, ticker: str, start_date: date, end_date: date) -> bool:
        """
        Fetch continuous range of data for a single ticker.
        Returns True if successful, False if failed.
        """
        url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start_date}/{end_date}?adjusted=true&apiKey={API_KEY}"
        
        # UPDATED: Timeout set to 15s to catch heavy stocks like INTC without hanging forever
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    results = resp.json().get("results", [])
                    if results:
                        rows = []
                        for r in results:
                            # Polygon returns timestamps in millis for Aggs
                            ts_date = pd.to_datetime(r.get("t"), unit="ms").date().isoformat()
                            rows.append((
                                ticker, ts_date, 
                                r.get("o"), r.get("h"), r.get("l"), r.get("c"), r.get("v")
                            ))
                        
                        with sqlite3.connect(self.db_path) as conn:
                            conn.executemany("""
                                INSERT OR REPLACE INTO daily_prices (ticker, date, open, high, low, close, volume)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                            """, rows)
                        print(f"      💾 Backfilled {len(rows)} rows for {ticker}")
                        return True
                    else:
                        print(f"      ⚠️ No history found for {ticker}")
                        return False
                elif resp.status_code == 429:
                    print(f"      🔴 Rate Limited on {ticker}")
                    return False
                else:
                    print(f"      🔴 Error fetching {ticker}: {resp.status_code}")
                    return False
            except httpx.TimeoutException:
                print(f"      🔴 Timeout fetching {ticker} (skipped)")
                return False
            except Exception as e:
                print(f"      🔴 Exception fetching {ticker}: {e}")
                return False

    async def _fetch_polygon_grouped(self, d: date) -> List[dict]:
        url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{d}?adjusted=true&apiKey={API_KEY}"
        # UPDATED: Timeout set to 15s
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.get(url)
                if resp.status_code == 429:
                    print("   ⚠️ Rate limited. Pausing 65s...")
                    await asyncio.sleep(65) 
                    return await self._fetch_polygon_grouped(d)
                if resp.status_code != 200:
                    return [] 
                return resp.json().get("results", [])
            except Exception:
                return []

    def _save_to_db(self, results: List[dict], date_str: str):
        if not results: return
        filtered_rows = []
        if not self.valid_tickers:
            self.valid_tickers = self._load_universe_tickers()

        for r in results:
            ticker = r.get("T")
            if ticker in self.valid_tickers:
                filtered_rows.append((
                    ticker, date_str, 
                    r.get("o"), r.get("h"), r.get("l"), r.get("c"), r.get("v")
                ))
        
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany("""
                INSERT OR REPLACE INTO daily_prices (ticker, date, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, filtered_rows)
        
        print(f"   💾 Saved {len(filtered_rows)} rows for {date_str}")
        
    def _save_single_row(self, ticker, date_str, r):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO daily_prices (ticker, date, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (ticker, date_str, r.get("o"), r.get("h"), r.get("l"), r.get("c"), r.get("v")))

    def _is_date_in_db(self, date_str: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT count(*) FROM daily_prices WHERE date=?", (date_str,)).fetchone()
        return row[0] > 800
