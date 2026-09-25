import os
import sqlite3
import asyncio
import math
import httpx
import numpy as np
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
API_WAIT_SECONDS = 13

if not API_KEY:
    raise RuntimeError("Missing Polygon key. Set POLYGON_API_KEY in .env")

# Polygon's free tier serves ~2 years of daily history.
HISTORY_REFETCH_DAYS = 730
SPLIT_REPAIR_COOLDOWN_DAYS = 30
# A split-contaminated series jumps by >= this factor and jumps straight back.
FLIP_FACTOR = 1.3
MIN_FLIPS = 2
# A jump of this factor across a hole of this many days marks a level break:
# either a stock rejoining the universe (refetch fills the hole) or a reused
# ticker (Polygon's own series is discontinuous; older rows are dropped).
BREAK_FACTOR = 1.5
BREAK_GAP_DAYS = 14


def find_split_contaminated(closes: pd.DataFrame) -> List[str]:
    """
    Tickers whose closes flip between two price scales: a large move immediately
    reversed by a comparable move (e.g. 100 -> 10 -> 100). Genuine gaps rarely
    round-trip overnight, so MIN_FLIPS of these marks a mixed series.
    """
    if closes.empty:
        return []
    df = closes.sort_values(["ticker", "date"])
    log_ret = np.log(df["close"] / df.groupby("ticker")["close"].shift())
    next_ret = log_ret.groupby(df["ticker"]).shift(-1)
    threshold = np.log(FLIP_FACTOR)
    flips = (
        (log_ret.abs() > threshold)
        & (next_ret.abs() > threshold)
        & (np.sign(log_ret) != np.sign(next_ret))
        & ((log_ret + next_ret).abs() < 0.5 * log_ret.abs())
    )
    counts = flips.groupby(df["ticker"]).sum()
    return sorted(counts[counts >= MIN_FLIPS].index)


def _level_breaks(closes: pd.DataFrame) -> pd.Series:
    """Boolean mask of rows that open a new price level after a long data hole."""
    df = closes.sort_values(["ticker", "date"])
    dates = pd.to_datetime(df["date"])
    gap_days = (dates - dates.groupby(df["ticker"]).shift()).dt.days
    factor = np.exp(np.log(df["close"] / df.groupby("ticker")["close"].shift()).abs())
    return (gap_days >= BREAK_GAP_DAYS) & (factor >= BREAK_FACTOR)


def find_level_breaks(closes: pd.DataFrame) -> List[str]:
    if closes.empty:
        return []
    df = closes.sort_values(["ticker", "date"])
    return sorted(set(df.loc[_level_breaks(df), "ticker"]))


def drop_before_last_break(closes: pd.DataFrame) -> pd.DataFrame:
    """For one ticker's series, keep only rows from its last level break onward."""
    df = closes.sort_values("date").reset_index(drop=True)
    breaks = df.index[_level_breaks(df)]
    return df.iloc[breaks[-1]:].reset_index(drop=True) if len(breaks) else df

class PriceService:
    def __init__(self):
        self.db_path = DB_PATH
        self._ensure_db()
        # Semaphore to limit concurrency (Polygon free tier limits)
        self._semaphore = asyncio.Semaphore(1) 
        self._api_lock = asyncio.Lock()
        self._last_api_call_at = 0.0
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_repairs (
                    ticker      TEXT,
                    repaired_at TEXT,
                    reason      TEXT,
                    rows        INTEGER,
                    PRIMARY KEY (ticker, repaired_at)
                )
            """)

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
            "minus_3_months":  (ts - pd.DateOffset(months=3)).date(),
            "minus_6_months":  (ts - pd.DateOffset(months=6)).date(),
            "minus_1_year":   (ts - pd.DateOffset(years=1)).date(),
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

    async def repair_split_adjustments(self, as_of: date) -> List[str]:
        """
        Re-download full history for tickers whose cached prices mix split-adjusted
        and unadjusted closes.

        Polygon's adjusted=true is relative to the fetch date, so rows cached before
        a split are never restated. Candidates come from (a) Polygon's splits feed
        since the last check, (b) a local scan for prices that flip back and forth
        by a split-like factor, and (c) price-level jumps across long data holes
        (universe re-entry or a reused ticker). Each candidate's rows are replaced
        wholesale; a break that persists in Polygon's own series is truncated.
        """
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM sync_state WHERE key = 'splits_checked_through'"
            ).fetchone()
            cached = {r[0] for r in conn.execute("SELECT DISTINCT ticker FROM daily_prices")}
            recent_repairs = {
                r[0] for r in conn.execute(
                    "SELECT DISTINCT ticker FROM price_repairs WHERE repaired_at >= ?",
                    ((as_of - timedelta(days=SPLIT_REPAIR_COOLDOWN_DAYS)).isoformat(),),
                )
            }

        since = date.fromisoformat(row[0]) if row else as_of - timedelta(days=HISTORY_REFETCH_DAYS)
        print(f"🪓 Checking splits executed {since} → {as_of}...")
        splits = await self._fetch_splits(since, as_of)
        if splits is None:
            print("   ⚠️ Split feed unavailable; will retry next run.")
            split_tickers = set()
        else:
            split_tickers = {s["ticker"] for s in splits if s.get("ticker") in cached}

        # Flip-scan hits already repaired recently are skipped so a series Polygon
        # itself serves inconsistently can't trigger a refetch on every run.
        all_closes = self._load_all_closes()
        flip_tickers = set(find_split_contaminated(all_closes)) - recent_repairs
        break_tickers = set(find_level_breaks(all_closes)) - recent_repairs

        reasons = {t: "gap" for t in break_tickers}
        reasons.update({t: "flip" for t in flip_tickers})
        reasons.update({t: "split" for t in split_tickers})
        if not reasons:
            print("   ✅ No split-affected price histories.")
        else:
            print(f"   🔧 Refetching {len(reasons)} histories: {sorted(reasons)}")

        failed = []
        for ticker in sorted(reasons):
            if not await self._replace_ticker_history(ticker, as_of, reasons[ticker]):
                failed.append(ticker)

        # Only advance the checkpoint when the feed was read and every split repair
        # landed; otherwise the same window is re-checked next run.
        if splits is not None and not (set(failed) & split_tickers):
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO sync_state (key, value) VALUES ('splits_checked_through', ?)",
                    (as_of.isoformat(),),
                )
        if failed:
            print(f"   ⚠️ Could not repair {failed}; will retry next run.")
        return sorted(set(reasons) - set(failed))

    async def ensure_history_depth(
        self,
        tickers: List[str],
        days_needed: int = 300,
        as_of: date = None,
        min_rows: int = None,
    ):
        """
        Ensure specific tickers (e.g. Munger cohort) have continuous daily history 
        in the DB to support SMA calculation.
        """
        print(f"🕵️ Checking history depth for {len(tickers)} tickers...")
        
        end_date = as_of or date.today()
        start_check = end_date - timedelta(days=days_needed)
        start_iso = start_check.isoformat()
        
        fetches_made = 0

        for ticker in tickers:
            # 1. Check count of rows in DB since start date
            with sqlite3.connect(self.db_path) as conn:
                try:
                    count = conn.execute(
                        "SELECT count(*) FROM daily_prices WHERE ticker=? AND date >= ? AND date <= ?",
                        (ticker, start_iso, end_date.isoformat())
                    ).fetchone()[0]
                except Exception:
                    count = 0
            
            # If we have less than ~60% of the needed days, we assume gaps/missing data
            threshold = min_rows if min_rows is not None else int(days_needed * 0.6)

            if count < threshold:
                print(f"   📉 {ticker}: Found {count} rows (need >{threshold}). Backfilling...")
                success = await self._backfill_ticker(ticker, start_check, end_date)
                
                if success:
                    fetches_made += 1
            else:
                # print(f"   ✅ {ticker}: History ok ({count} rows).")
                pass
        
        if fetches_made > 0:
            print(f"   ✅ Backfill complete. Fetched {fetches_made} tickers.")

    async def ensure_cohort_history_depth(
        self,
        tickers: List[str],
        as_of: date,
        session_count: int = 200,
        minimum_observation_coverage: float = 0.90,
        minimum_coverage: float = 0.90,
    ):
        """Fill a fixed 200-session window, using grouped or per-ticker calls efficiently."""
        tickers = sorted(set(tickers))
        if not tickers:
            return

        recent_session_count = 10
        required_market_sessions = session_count + recent_session_count - 1
        minimum_rows = math.ceil(session_count * minimum_observation_coverage)
        start_date = as_of - timedelta(days=365)

        market_sessions = self._get_market_sessions(as_of, required_market_sessions)
        if len(market_sessions) < required_market_sessions:
            print("   📈 Backfilling VOO once to establish the market-session calendar.")
            await self._backfill_ticker("VOO", start_date, as_of)
            market_sessions = self._get_market_sessions(as_of, required_market_sessions)
        if len(market_sessions) < required_market_sessions:
            raise RuntimeError(
                f"Only {len(market_sessions)} recent market sessions are available; "
                f"{required_market_sessions} are required for the SMA screen."
            )

        counts = self._get_rolling_window_counts(
            tickers, market_sessions, session_count, recent_session_count
        )
        required_tickers = math.ceil(len(tickers) * minimum_coverage)
        covered = sum(counts.get(ticker, 0) >= minimum_rows for ticker in tickers)
        if covered >= required_tickers:
            print(
                f"   ✅ SP400 SMA coverage: {covered}/{len(tickers)} tickers have "
                f"at least {minimum_rows}/{session_count} observations."
            )
            return

        deficient = [ticker for ticker in tickers if counts.get(ticker, 0) < minimum_rows]
        missing_grouped_dates = [
            date.fromisoformat(target) for target in reversed(market_sessions)
            if not self._is_date_in_db(target)
        ]

        print(
            f"🧮 SP400 SMA backfill: {len(deficient)} ticker-range calls vs. "
            f"up to {len(missing_grouped_dates)} grouped-day calls."
        )
        if len(missing_grouped_dates) < len(deficient):
            print("   📚 Using grouped daily backfill; each call covers the full cohort.")
            for index, target in enumerate(missing_grouped_dates, start=1):
                await self._ensure_date_data(target)
                if index % 10 == 0 or index == len(missing_grouped_dates):
                    counts = self._get_rolling_window_counts(
                        tickers, market_sessions, session_count, recent_session_count
                    )
                    covered = sum(counts.get(ticker, 0) >= minimum_rows for ticker in tickers)
                    print(
                        f"      History coverage after {index} grouped targets: "
                        f"{covered}/{len(tickers)}"
                    )
                    if covered >= required_tickers:
                        break
        else:
            print("   📈 Using ticker-range backfill; fewer individual calls are needed.")
            await self.ensure_history_depth(
                deficient,
                days_needed=365,
                as_of=as_of,
                min_rows=minimum_rows,
            )

        final_counts = self._get_rolling_window_counts(
            tickers, market_sessions, session_count, recent_session_count
        )
        final_covered = sum(
            final_counts.get(ticker, 0) >= minimum_rows for ticker in tickers
        )
        if final_covered < required_tickers:
            print(
                f"   ⚠️ SP400 SMA history coverage is {final_covered}/{len(tickers)}; "
                f"stocks below {minimum_rows}/{session_count} observations will be skipped."
            )
        else:
            print(
                f"   ✅ SP400 SMA coverage: {final_covered}/{len(tickers)} tickers have "
                f"at least {minimum_rows}/{session_count} observations."
            )

    async def prepare_munger400_return_history(self, run_date: date) -> List[Dict[str, str]]:
        """Cache and return the twice-weekly observation pairs used by Munger400R."""
        start = pd.Timestamp(run_date) - pd.Timedelta(days=365)
        scheduled_runs = [
            timestamp.date()
            for timestamp in pd.date_range(start=start, end=run_date, freq="D")
            if timestamp.weekday() in (1, 4)  # Tuesday and Friday reports
        ]
        if run_date not in scheduled_runs:
            scheduled_runs.append(run_date)

        print(f"🗓️  Preparing {len(scheduled_runs)} Munger400R observation dates...")
        pairs = []
        seen = set()
        # Polygon's two-year boundary can exclude the first few nominal dates.
        # Keep a small safety margin and omit the whole 12-month pair instead of
        # spending several calls backtracking into even older, unavailable dates.
        provider_history_floor = date.today() - timedelta(days=720)
        for scheduled_run in sorted(scheduled_runs):
            observation = await self._ensure_date_data(scheduled_run - timedelta(days=1))
            baseline_target = (
                pd.Timestamp(observation) - pd.DateOffset(years=1)
            ).date()
            baseline = self._find_cached_market_date(baseline_target)
            if baseline is None and baseline_target < provider_history_floor:
                print(
                    f"   ⚠️ Skipping {observation}: baseline {baseline_target} is "
                    "outside Polygon's available history."
                )
                continue
            if baseline is None:
                try:
                    baseline = await self._ensure_date_data(baseline_target)
                except RuntimeError:
                    print(
                        f"   ⚠️ Skipping {observation}: no baseline data found near "
                        f"{baseline_target}."
                    )
                    continue
            key = (observation.isoformat(), baseline.isoformat())
            if key in seen:
                continue
            seen.add(key)
            pairs.append({
                "observation_date": key[0],
                "baseline_date": key[1],
            })

        return pairs

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
                resp = await self._rate_limited_get(client, url)
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
                resp = await self._rate_limited_get(client, url)
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

    async def _fetch_splits(self, since: date, until: date):
        """All market splits executed in [since, until]; None if the feed fails."""
        url = (
            "https://api.polygon.io/v3/reference/splits"
            f"?execution_date.gte={since}&execution_date.lte={until}"
            f"&order=asc&sort=execution_date&limit=1000&apiKey={API_KEY}"
        )
        splits = []
        async with httpx.AsyncClient(timeout=20.0) as client:
            while url:
                try:
                    resp = await self._rate_limited_get(client, url)
                except Exception as e:
                    print(f"      🔴 Split feed error: {e}")
                    return None
                if resp.status_code != 200:
                    print(f"      🔴 Split feed error: {resp.status_code}")
                    return None
                payload = resp.json()
                splits.extend(payload.get("results", []))
                next_url = payload.get("next_url")
                url = f"{next_url}&apiKey={API_KEY}" if next_url else None
        return splits

    async def _replace_ticker_history(self, ticker: str, as_of: date, reason: str) -> bool:
        """Swap a ticker's cached rows for one consistently adjusted download."""
        start = as_of - timedelta(days=HISTORY_REFETCH_DAYS)
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{as_of}"
            f"?adjusted=true&sort=asc&limit=50000&apiKey={API_KEY}"
        )
        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                resp = await self._rate_limited_get(client, url)
            except Exception as e:
                print(f"      🔴 {ticker}: refetch failed ({e})")
                return False
        if resp.status_code != 200:
            print(f"      🔴 {ticker}: refetch failed ({resp.status_code})")
            return False
        results = resp.json().get("results", [])
        if not results:
            print(f"      ⚠️ {ticker}: refetch returned no rows; keeping cached data")
            return False

        fresh = pd.DataFrame([
            {
                "ticker": ticker,
                "date": pd.to_datetime(r.get("t"), unit="ms").date().isoformat(),
                "open": r.get("o"), "high": r.get("h"), "low": r.get("l"),
                "close": r.get("c"), "volume": r.get("v"),
            }
            for r in results
        ])
        kept = drop_before_last_break(fresh)
        if len(kept) < len(fresh):
            print(
                f"      ✂️ {ticker}: Polygon series breaks at {kept['date'].iloc[0]} "
                f"(likely a reused ticker); dropping {len(fresh) - len(kept)} earlier rows"
            )
            reason = f"{reason}+truncated"
        rows = list(kept[["ticker", "date", "open", "high", "low", "close", "volume"]].itertuples(index=False, name=None))
        with sqlite3.connect(self.db_path) as conn:
            # One transaction: older unadjusted rows go too, so no stale jump survives.
            conn.execute("DELETE FROM daily_prices WHERE ticker = ?", (ticker,))
            conn.executemany("""
                INSERT OR REPLACE INTO daily_prices (ticker, date, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.execute(
                "INSERT OR REPLACE INTO price_repairs (ticker, repaired_at, reason, rows) VALUES (?, ?, ?, ?)",
                (ticker, as_of.isoformat(), reason, len(rows)),
            )
        print(f"      💾 {ticker}: replaced history with {len(rows)} adjusted rows ({reason})")
        return True

    def _load_all_closes(self) -> pd.DataFrame:
        with sqlite3.connect(self.db_path) as conn:
            return pd.read_sql_query(
                "SELECT ticker, date, close FROM daily_prices ORDER BY ticker, date", conn
            )

    async def _fetch_polygon_grouped(self, d: date) -> List[dict]:
        url = f"https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{d}?adjusted=true&apiKey={API_KEY}"
        # UPDATED: Timeout set to 15s
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await self._rate_limited_get(client, url)
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

    def _find_cached_market_date(self, target: date, max_backtrack: int = 5):
        current = target
        for _ in range(max_backtrack + 1):
            while current.weekday() >= 5:
                current -= timedelta(days=1)
            if self._is_date_in_db(current.isoformat()):
                return current
            current -= timedelta(days=1)
        return None

    def _get_history_counts(self, tickers: List[str], start_date: date, end_date: date) -> Dict[str, int]:
        placeholders = ",".join(["?"] * len(tickers))
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT ticker, COUNT(*)
                FROM daily_prices
                WHERE ticker IN ({placeholders}) AND date >= ? AND date <= ?
                GROUP BY ticker
                """,
                tickers + [start_date.isoformat(), end_date.isoformat()],
            ).fetchall()
        return {ticker: count for ticker, count in rows}

    def _get_market_sessions(self, as_of: date, count: int) -> List[str]:
        """Use VOO observations as the canonical US market-session calendar."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT date
                FROM daily_prices
                WHERE ticker = 'VOO' AND date <= ?
                ORDER BY date DESC
                LIMIT ?
                """,
                (as_of.isoformat(), count),
            ).fetchall()
        return [row[0] for row in reversed(rows)]

    def _get_rolling_window_counts(
        self,
        tickers: List[str],
        market_sessions: List[str],
        session_count: int,
        recent_session_count: int,
    ) -> Dict[str, int]:
        """Return each ticker's weakest observation count across the recent SMA windows."""
        ticker_placeholders = ",".join(["?"] * len(tickers))
        date_placeholders = ",".join(["?"] * len(market_sessions))
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT ticker, date
                FROM daily_prices
                WHERE ticker IN ({ticker_placeholders})
                  AND date IN ({date_placeholders})
                """,
                tickers + market_sessions,
            ).fetchall()

        dates_by_ticker: Dict[str, Set[str]] = {ticker: set() for ticker in tickers}
        for ticker, session_date in rows:
            dates_by_ticker[ticker].add(session_date)

        counts = {}
        for ticker, available_dates in dates_by_ticker.items():
            window_counts = [
                sum(
                    session_date in available_dates
                    for session_date in market_sessions[offset:offset + session_count]
                )
                for offset in range(recent_session_count)
            ]
            counts[ticker] = min(window_counts) if window_counts else 0
        return counts

    async def _rate_limited_get(self, client: httpx.AsyncClient, url: str):
        """Keep every Polygon request at least 13 seconds behind the previous one."""
        async with self._api_lock:
            loop = asyncio.get_running_loop()
            elapsed = loop.time() - self._last_api_call_at
            wait_seconds = API_WAIT_SECONDS - elapsed
            if self._last_api_call_at and wait_seconds > 0:
                print(f"      ⏳ Waiting {wait_seconds:.1f}s for Polygon rate limit...")
                await asyncio.sleep(wait_seconds)
            response = await client.get(url)
            self._last_api_call_at = loop.time()
            return response
