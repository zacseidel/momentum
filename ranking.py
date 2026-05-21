import sqlite3
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
