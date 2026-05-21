import asyncio
import pandas as pd
from datetime import date
from prices import PriceService
from ranking import RankingService
from universe import UniverseService

# The three categories we want to report on
COHORTS = ["megacap", "sp500", "sp400"]

async def main():
    print("🧪 Starting Multi-Category Ranking Test...")

    # 1. Setup Services
    u_service = UniverseService()
    p_service = PriceService()
    r_service = RankingService()

    # 2. Pick a test date
    test_date = date.today() 
    print(f"📅 Test Date: {test_date}")

    # 3. Resolve Dates (We do this once for all categories)
    #    This ensures we have the historical data needed.
    print("⏳ Resolving target dates & checking data availability...")
    target_dates = await p_service.resolve_target_dates(test_date)
    print(f"✅ Dates Resolved: {target_dates}")

    # 4. Loop through each Category
    for cohort in COHORTS:
        print(f"\n" + "="*40)
        print(f"🧐 Processing Category: {cohort.upper()}")
        print(f"="*40)

        # A. Get the Tickers
        try:
            cohort_df = u_service.get_cohort(cohort)
            tickers = cohort_df["symbol"].tolist()
            print(f"   📋 Universe: Loaded {len(tickers)} tickers.")
        except FileNotFoundError:
            print(f"   ❌ Error: {cohort}.csv not found. Run 'python universe.py --sync' first.")
            continue

        # B. Get Price Snapshots
        #    Only fetches prices for this specific list of tickers
        prices_df = await p_service.get_snapshots(tickers, target_dates)
        print(f"   📉 Prices: Retrieved {len(prices_df)} rows from DB.")

        # C. Calculate Ranks
        ranked_df = r_service.calculate_ranks(prices_df, target_dates)
        
        if ranked_df.empty:
            print(f"   ⚠️  No valid data to rank for {cohort} (Check history).")
            continue

        # D. Extract Top 10 & Update Streaks
        print(f"   🏆 Calculating Top 10 & Streaks...")
        top_10 = r_service.extract_top_picks(ranked_df, cohort, test_date)
        
        # E. Print the Results
        if not top_10.empty:
            print(f"\n   Top 5 for {cohort.upper()}:")
            # Show the Tweetable data: Ticker, Return, Streak, Start Date
            print(top_10[["ticker", "current_return", "streak", "streak_start"]].head(5).to_string(index=False))
        else:
            print("   ⚠️  Top 10 list is empty.")

    print("\n✅ Test Complete.")

if __name__ == "__main__":
    asyncio.run(main())