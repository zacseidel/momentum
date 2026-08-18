import sqlite3
import math
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date
from typing import Dict, List

# --- Configuration ---
DB_PATH = Path("data/market_data.sqlite")

class RankingService:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path

    def calculate_ranks(self, prices_df: pd.DataFrame, date_map: Dict[str, str]) -> pd.DataFrame:
        """
        Takes raw price data, calculates momentum returns, and ranks them.
        Returns a DataFrame sorted by best performance.
        """
        if prices_df.empty:
            return pd.DataFrame()

        # 1. Pivot Long Data to Wide (Index=Ticker, Columns=Date)
        pivoted = prices_df.pivot(index="ticker", columns="date", values="close")
        
        # 2. Map friendly names
        try:
            c_now      = pivoted[date_map["latest_trading"]]
            c_1week    = pivoted[date_map["minus_1_week"]]
            c_1month   = pivoted[date_map["minus_1_month"]]
            c_1year    = pivoted[date_map["minus_1_year"]]
            c_13months = pivoted[date_map["minus_13_months"]]
        except KeyError as e:
            print(f"❌ Ranking Error: Missing price column for {e}")
            return pd.DataFrame()

        # 3. Calculate Returns
        current_return = (c_now - c_1year) / c_1year
        previous_return = (c_1month - c_13months) / c_13months
        last_week_return = (c_now - c_1week) / c_1week

        # 4. Ranking (Lower rank is better)
        current_rank  = current_return.rank(ascending=False, method="min")
        previous_rank = previous_return.rank(ascending=False, method="min")
        rank_change = previous_rank - current_rank

        # 5. Assemble Results
        df = pd.DataFrame({
            "current_return":    current_return,
            "last_week_return":  last_week_return,
            "last_month_return": previous_return,
            "current_rank":      current_rank,
            "last_month_rank":   previous_rank,
            "rank_change":       rank_change
        })

        # 6. Filter: "Improving or Steady"
        df = df.dropna()
        df = df[df["current_rank"] <= df["last_month_rank"]]
        
        # Sort by raw return (Highest first)
        return df.sort_values("current_return", ascending=False)

    def rank_munger_cohort(self, candidates_df: pd.DataFrame) -> pd.DataFrame:
        """
        Identifies 'Munger' candidates:
        1. Market Cap: Top 50 (passed in via candidates_df)
        2. Dip: Close Price was < 200-day SMA within the last 10 trading days.
        3. Recovery: Current Price is > 10-day SMA.
        """
        tickers = candidates_df['symbol'].tolist()
        if not tickers:
            return pd.DataFrame()

        print(f"📊 Analyzing {len(tickers)} candidates for Munger Strategy...")

        # 1. Bulk Fetch History (Fetch 400 days to be safe for 200SMA)
        #    We assume run_report.py has already called 'ensure_history_depth'
        start_date = (pd.Timestamp.now() - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        placeholders = ",".join(["?"] * len(tickers))
        
        with sqlite3.connect(self.db_path) as conn:
            query = f"""
                SELECT ticker, date, close 
                FROM daily_prices 
                WHERE ticker IN ({placeholders}) AND date >= ?
                ORDER BY ticker, date ASC
            """
            prices_df = pd.read_sql_query(query, conn, params=tickers + [start_date])

        if prices_df.empty:
            print("   ⚠️ No price data found for Munger candidates.")
            return pd.DataFrame()

        qualified_tickers = []
        
        # 2. Process Each Ticker
        for ticker, group in prices_df.groupby("ticker"):
            df = group.sort_values("date").set_index("date")
            
            if len(df) < 200:
                continue

            # Calculate Indicators
            df['sma_200'] = df['close'].rolling(window=200).mean()
            df['sma_10'] = df['close'].rolling(window=10).mean()
            
            # Logic: Dip < 200MA in last 10 days
            df['below_200'] = df['close'] < df['sma_200']
            last_10_days = df.iloc[-10:]
            has_dip = last_10_days['below_200'].any()
            
            # Logic: Recovery > 10MA now
            current_recovery = df.iloc[-1]['close'] > df.iloc[-1]['sma_10']

            if has_dip and current_recovery:
                latest_price = df.iloc[-1]['close']
                latest_sma200 = df.iloc[-1]['sma_200']
                
                qualified_tickers.append({
                    "ticker": ticker,  # Using 'ticker' to match system convention
                    "price": latest_price,
                    "sma_200": latest_sma200,
                    "sma_10": df.iloc[-1]['sma_10'],
                    "pct_below_200": (latest_price - latest_sma200) / latest_sma200
                })

        # 3. Format Output
        results_df = pd.DataFrame(qualified_tickers)
        
        if results_df.empty:
            return pd.DataFrame()

        # Merge with Weights (Market Cap proxy) and Sort
        # We need to rename 'symbol' to 'ticker' in candidates to merge easily
        candidates_renamed = candidates_df.rename(columns={"symbol": "ticker"})
        results_df = results_df.merge(candidates_renamed[['ticker', 'weight']], on='ticker', how='left')
        
        # Sort by Weight (Highest Market Cap first)
        results_df = results_df.sort_values("weight", ascending=False).reset_index(drop=True)
        
        # Add Rank column
        results_df.index += 1
        results_df.reset_index(inplace=True)
        results_df.rename(columns={"index": "rank"}, inplace=True)

        return results_df

    def rank_munger400l(self, candidates_df: pd.DataFrame, as_of_date: date) -> pd.DataFrame:
        """Find mean-reversion signals among the largest 15% of the SP400 by MDY weight."""
        candidates = self._clean_sp400_candidates(candidates_df)
        if candidates.empty:
            return pd.DataFrame()

        candidates = candidates.sort_values("weight", ascending=False).reset_index(drop=True)
        candidates["weight_rank"] = candidates.index + 1
        size_count = math.ceil(len(candidates) * 0.15)
        eligible = candidates.head(size_count).copy()

        signals = self._mean_reversion_signals(eligible["symbol"].tolist(), as_of_date)
        if signals.empty:
            return pd.DataFrame()

        results = signals.merge(
            eligible[["symbol", "weight", "weight_rank"]].rename(columns={"symbol": "ticker"}),
            on="ticker",
            how="inner",
        )
        results = results.sort_values(["weight_rank", "ticker"]).reset_index(drop=True)
        results.insert(0, "rank", results.index + 1)
        return results

    def rank_munger400r(
        self,
        candidates_df: pd.DataFrame,
        as_of_date: date,
        observation_pairs: List[Dict[str, str]],
        minimum_coverage: float = 0.90,
    ) -> pd.DataFrame:
        """Find mean-reversion signals among recent top-15% SP400 return leaders."""
        candidates = self._clean_sp400_candidates(candidates_df)
        if candidates.empty or not observation_pairs:
            return pd.DataFrame()

        tickers = candidates["symbol"].tolist()
        dates = sorted({
            pair[key]
            for pair in observation_pairs
            for key in ("observation_date", "baseline_date")
        })
        placeholders = ",".join(["?"] * len(tickers))
        date_placeholders = ",".join(["?"] * len(dates))
        with sqlite3.connect(self.db_path) as conn:
            prices = pd.read_sql_query(
                f"""
                SELECT ticker, date, close
                FROM daily_prices
                WHERE ticker IN ({placeholders}) AND date IN ({date_placeholders})
                """,
                conn,
                params=tickers + dates,
            )

        if prices.empty:
            raise RuntimeError("No SP400 price snapshots are available for Munger400R.")

        pivoted = prices.pivot(index="ticker", columns="date", values="close")
        minimum_count = math.ceil(len(tickers) * minimum_coverage)
        leaders: Dict[str, Dict] = {}

        for pair in sorted(observation_pairs, key=lambda item: item["observation_date"]):
            observation_date = pair["observation_date"]
            baseline_date = pair["baseline_date"]
            if observation_date not in pivoted.columns or baseline_date not in pivoted.columns:
                raise RuntimeError(
                    f"Missing Munger400R snapshot pair: {observation_date} / {baseline_date}."
                )

            returns = (pivoted[observation_date] / pivoted[baseline_date] - 1).dropna()
            if len(returns) < minimum_count:
                raise RuntimeError(
                    f"Munger400R coverage for {observation_date} is {len(returns)}/{len(tickers)}; "
                    f"at least {minimum_count} valid stocks are required."
                )

            ranks = returns.rank(ascending=False, method="min").astype(int)
            cutoff = math.ceil(len(returns) * 0.15)
            qualified = ranks[ranks <= cutoff]

            for ticker, return_rank in qualified.items():
                percentile = return_rank / len(returns)
                existing = leaders.get(ticker)
                if existing is None or percentile < existing["best_return_percentile"]:
                    leaders[ticker] = {
                        "ticker": ticker,
                        "best_12m_return": float(returns[ticker]),
                        "best_return_rank": int(return_rank),
                        "return_universe_size": int(len(returns)),
                        "best_return_percentile": float(percentile),
                        "best_return_date": observation_date,
                        "most_recent_qualified_date": observation_date,
                    }
                else:
                    existing["most_recent_qualified_date"] = observation_date

        if not leaders:
            return pd.DataFrame()

        leadership = pd.DataFrame(leaders.values())
        signals = self._mean_reversion_signals(leadership["ticker"].tolist(), as_of_date)
        if signals.empty:
            return pd.DataFrame()

        results = signals.merge(leadership, on="ticker", how="inner")
        results = results.sort_values(
            ["best_return_percentile", "best_return_rank", "ticker"]
        ).reset_index(drop=True)
        results.insert(0, "rank", results.index + 1)
        return results

    def process_munger400_picks(
        self, picks_df: pd.DataFrame, cohort: str, run_date: date
    ) -> pd.DataFrame:
        """Persist and format an independent Munger400 report cohort."""
        if cohort not in {"munger400l", "munger400r"}:
            raise ValueError(f"Unsupported Munger400 cohort: {cohort}")

        self._ensure_munger400_table(cohort)
        if picks_df.empty:
            self._delete_run_date(cohort, run_date)
            print(f"⚠️  No {cohort.upper()} candidates found.")
            return pd.DataFrame()

        persisted = self._calculate_streaks(picks_df.copy(), cohort, run_date)
        persisted["date"] = run_date.isoformat()
        self._save_to_db(persisted, cohort, run_date)

        display_df = persisted.copy()
        for column in ["price", "sma_200", "sma_10"]:
            display_df[column] = display_df[column].apply(lambda value: f"${value:,.2f}")
        display_df["pct_below_200"] = display_df["pct_below_200"].apply(
            lambda value: f"{value:.1%}"
        )
        if cohort == "munger400l":
            display_df["weight"] = display_df["weight"].apply(lambda value: f"{value:.3f}%")
        else:
            display_df["best_12m_return"] = display_df["best_12m_return"].apply(
                lambda value: f"{value:.1%}"
            )
            display_df["best_return_percentile"] = display_df[
                "best_return_percentile"
            ].apply(lambda value: f"{value:.1%}")

        print(f"   💾 Saved {len(display_df)} {cohort.upper()} picks.")
        return display_df

    @staticmethod
    def _clean_sp400_candidates(candidates_df: pd.DataFrame) -> pd.DataFrame:
        required = {"symbol", "weight"}
        if candidates_df.empty or not required.issubset(candidates_df.columns):
            return pd.DataFrame(columns=["symbol", "weight"])
        candidates = candidates_df.dropna(subset=["symbol", "weight"]).copy()
        candidates["symbol"] = candidates["symbol"].astype(str)
        return candidates[candidates["symbol"] != "CASH_USD"].drop_duplicates("symbol")

    def _mean_reversion_signals(
        self,
        tickers: List[str],
        as_of_date: date,
        minimum_coverage: float = 0.90,
    ) -> pd.DataFrame:
        """Apply SMA tests over fixed market-session windows, tolerating modest gaps."""
        if not tickers:
            return pd.DataFrame()

        as_of = pd.Timestamp(as_of_date).date().isoformat()
        sma_sessions = 200
        recent_sessions = 10
        required_sessions = sma_sessions + recent_sessions - 1
        minimum_sma_observations = math.ceil(sma_sessions * minimum_coverage)
        minimum_short_observations = math.ceil(recent_sessions * minimum_coverage)
        placeholders = ",".join(["?"] * len(tickers))
        with sqlite3.connect(self.db_path) as conn:
            market_dates = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT date
                    FROM daily_prices
                    WHERE ticker = 'VOO' AND date <= ?
                    ORDER BY date DESC
                    LIMIT ?
                    """,
                    (as_of, required_sessions),
                ).fetchall()
            ][::-1]
            if len(market_dates) < required_sessions:
                return pd.DataFrame()

            date_placeholders = ",".join(["?"] * len(market_dates))
            prices = pd.read_sql_query(
                f"""
                SELECT ticker, date, close
                FROM daily_prices
                WHERE ticker IN ({placeholders}) AND date IN ({date_placeholders})
                ORDER BY ticker, date ASC
                """,
                conn,
                params=tickers + market_dates,
            )

        signals = []
        for ticker, group in prices.groupby("ticker"):
            history = (
                group.drop_duplicates("date")
                .set_index("date")
                .reindex(market_dates)
            )
            history["sma_200"] = history["close"].rolling(
                sma_sessions, min_periods=minimum_sma_observations
            ).mean()
            history["sma_10"] = history["close"].rolling(
                recent_sessions, min_periods=minimum_short_observations
            ).mean()
            recent = history.tail(recent_sessions)
            latest = history.iloc[-1]
            if pd.isna(latest["close"]) or pd.isna(latest["sma_200"]):
                continue

            dipped = recent[recent["close"] < recent["sma_200"]]
            if (
                dipped.empty
                or pd.isna(latest["sma_10"])
                or latest["close"] <= latest["sma_10"]
            ):
                continue

            signals.append({
                "ticker": ticker,
                "price": float(latest["close"]),
                "sma_200": float(latest["sma_200"]),
                "sma_10": float(latest["sma_10"]),
                "pct_below_200": float((latest["close"] - latest["sma_200"]) / latest["sma_200"]),
                "dip_date": str(dipped.index[-1]),
            })

        return pd.DataFrame(signals)

    def extract_top_picks(self, ranked_df: pd.DataFrame, cohort: str, run_date: date) -> pd.DataFrame:
        """
        Standard Momentum Saver: Slices Top 10, calculates Streak, formats %.
        """
        if ranked_df.empty:
            print(f"⚠️  No ranked results for {cohort}.")
            return pd.DataFrame()

        # 1. Select Top 10
        top_10 = ranked_df.head(10).copy()
        if "ticker" not in top_10.columns:
             top_10.index.name = "ticker"
             top_10 = top_10.reset_index()

        # 2. Calculate Streaks
        top_10 = self._calculate_streaks(top_10, cohort, run_date)

        # 3. Format
        display_df = top_10.copy()
        pct_cols = ["current_return", "last_week_return", "last_month_return"]
        for c in pct_cols:
            if c in display_df.columns:
                display_df[c] = display_df[c].apply(lambda x: f"{x:.1%}")

        display_df["date"] = run_date.isoformat()

        # 4. Save
        self._save_to_db(display_df, cohort, run_date)
        return display_df

    def process_munger_picks(self, munger_df: pd.DataFrame, run_date: date) -> pd.DataFrame:
        """
        Specialized Saver for Munger Cohort.
        Handles different columns (Price, SMA) but keeps Streak logic.
        """
        cohort = "munger"
        if munger_df.empty:
            print(f"⚠️  No Munger candidates found.")
            # We still might want to clear the DB for this date?
            # For now, just return.
            return pd.DataFrame()

        # 1. Calculate Streaks (Works because munger_df has 'ticker')
        df = self._calculate_streaks(munger_df, cohort, run_date)

        # 2. Format Money/Percent
        display_df = df.copy()
        
        # Format Price and SMAs as currency
        for c in ["price", "sma_200", "sma_10"]:
            display_df[c] = display_df[c].apply(lambda x: f"${x:,.2f}")
            
        # Format the 'Discount' percent if it exists
        if "pct_below_200" in display_df.columns:
             display_df["pct_below_200"] = display_df["pct_below_200"].apply(lambda x: f"{x:.1%}")

        display_df["date"] = run_date.isoformat()

        # 3. Save
        self._save_to_db(display_df, cohort, run_date)
        print(f"   💾 Saved {len(display_df)} Munger picks.")
        
        return display_df

    def _calculate_streaks(self, current_df: pd.DataFrame, cohort: str, run_date: date) -> pd.DataFrame:
        """
        Calculates consecutive streaks AND preserves the original start date of the streak.
        """
        table_name = f"top10_{cohort}"
        
        with sqlite3.connect(self.db_path) as conn:
            # A. Find the most recent previous entry
            cursor = conn.cursor()
            try:
                cursor.execute(f"SELECT MAX(date) FROM {table_name} WHERE date < ?", (run_date.isoformat(),))
                last_date = cursor.fetchone()[0]
            except sqlite3.OperationalError:
                last_date = None

            # B. If no history, everyone starts today
            if not last_date:
                current_df["streak"] = 1
                current_df["streak_start"] = run_date.isoformat()
                return current_df

            # C. Get history (streak count AND start date)
            try:
                prev_df = pd.read_sql(
                    f"SELECT ticker, streak, streak_start FROM {table_name} WHERE date = ?", 
                    conn, 
                    params=(last_date,)
                )
            except Exception:
                # If table exists but schema changed or some other error, treat as new
                prev_df = pd.DataFrame()
        
        if prev_df.empty:
            current_df["streak"] = 1
            current_df["streak_start"] = run_date.isoformat()
            return current_df

        # D. Merge History
        #    suffixes: '_new' (current run), '_old' (last run)
        merged = current_df.merge(prev_df, on="ticker", how="left", suffixes=("", "_old"))
        
        # E. Logic
        #    Streak: if old exists, old + 1. Else 1.
        merged["streak"] = merged["streak"].fillna(0).astype(int) + 1
        
        #    Start Date: if old exists, keep old start. Else use today.
        today_str = run_date.isoformat()
        merged["streak_start"] = merged["streak_start"].fillna(today_str)
        
        # Cleanup
        cols_to_drop = [c for c in merged.columns if "_old" in c]
        return merged.drop(columns=cols_to_drop)

    def _save_to_db(self, df: pd.DataFrame, cohort: str, run_date: date):
        table_name = f"top10_{cohort}"
        run_iso = run_date.isoformat()
        
        with sqlite3.connect(self.db_path) as conn:
            # 1. Clean slate for this specific date
            try:
                conn.execute(f"DELETE FROM {table_name} WHERE date = ?", (run_iso,))
            except sqlite3.OperationalError:
                # Table doesn't exist yet, that's fine
                pass
            
            # 2. Insert
            #    Note: This will create the table schema based on the DF columns
            #    if the table doesn't exist. This is exactly what we want 
            #    for 'top10_munger' to have different columns than 'top10_sp500'.
            df.to_sql(table_name, conn, if_exists="append", index=False)

    def _delete_run_date(self, cohort: str, run_date: date):
        table_name = f"top10_{cohort}"
        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute(f"DELETE FROM {table_name} WHERE date = ?", (run_date.isoformat(),))
            except sqlite3.OperationalError:
                pass

    def _ensure_munger400_table(self, cohort: str):
        if cohort == "munger400l":
            extra_columns = """
                weight REAL,
                weight_rank INTEGER,
            """
        elif cohort == "munger400r":
            extra_columns = """
                best_12m_return REAL,
                best_return_rank INTEGER,
                return_universe_size INTEGER,
                best_return_percentile REAL,
                best_return_date DATE,
                most_recent_qualified_date DATE,
            """
        else:
            raise ValueError(f"Unsupported Munger400 cohort: {cohort}")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS top10_{cohort} (
                    rank INTEGER,
                    ticker TEXT,
                    price REAL,
                    sma_200 REAL,
                    sma_10 REAL,
                    pct_below_200 REAL,
                    dip_date DATE,
                    {extra_columns}
                    streak INTEGER DEFAULT 1,
                    streak_start DATE,
                    date DATE,
                    PRIMARY KEY (ticker, date)
                )
            """)
