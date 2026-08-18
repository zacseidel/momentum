#!/usr/bin/env python
# run_report.py - The Orchestrator

import matplotlib
matplotlib.use("Agg") # Force non-interactive backend

import asyncio
import sys
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

# Local Modules
from universe import UniverseService
from prices import PriceService
from ranking import RankingService
from report import ReportService
from build_site import build_website

# Config
REPORT_DIR = Path("reports")
REPORT_DIR.mkdir(parents=True, exist_ok=True)
MOMENTUM_COHORTS = ["megacap", "sp500", "sp400"]

async def build_report(run_date: date):
    print(f"🚀 Starting Momentum Report for {run_date}")
    
    # 1. Universe Sync
    u_service = UniverseService()
    await u_service.sync(as_of=run_date)

    # 3. Prices & Dates
    p_service = PriceService()
    target_dates = await p_service.resolve_target_dates(run_date)

    # 4. Ranking & Signal Generation
    r_service = RankingService()
    top_picks = {} 
    all_winners = [] # We collect all tickers that need charts/metadata

    # --- A. Standard Momentum Strategy ---
    for cohort in MOMENTUM_COHORTS:
        print(f"📊 Processing {cohort.upper()}...")
        
        # Get Tickers & Prices
        cohort_df = u_service.get_cohort(cohort)
        tickers = cohort_df['symbol'].tolist()
        prices_df = await p_service.get_snapshots(tickers, target_dates)
        
        # Rank
        ranked_df = r_service.calculate_ranks(prices_df, target_dates)
        top_10 = r_service.extract_top_picks(ranked_df, cohort, run_date)
        top_picks[cohort] = top_10
        
        # Collect winners for later processing
        if not top_10.empty:
            all_winners.extend(top_10['ticker'].tolist())

    # --- B. Munger Strategy (Top 50 Market Cap Reversion) ---
    print(f"📊 Processing MUNGER STRATEGY...")
    munger_candidates = u_service.get_cohort("munger")
    munger_tickers = munger_candidates['symbol'].tolist()
    
    # 1. Ensure Deep History (Needed for 200SMA)
    await p_service.ensure_history_depth(munger_tickers, days_needed=300)
    
    # 2. Rank & Process
    munger_ranks = r_service.rank_munger_cohort(munger_candidates)
    munger_picks = r_service.process_munger_picks(munger_ranks, run_date)
    
    top_picks["munger"] = munger_picks
    if not munger_picks.empty:
        all_winners.extend(munger_picks['ticker'].tolist())

    # --- C. Munger400 Strategies (Report-Only SP400 Mean Reversion) ---
    print("📊 Processing MUNGER400L and MUNGER400R STRATEGIES...")
    sp400_candidates = u_service.get_cohort("sp400")
    sp400_tickers = sp400_candidates.loc[
        sp400_candidates["symbol"] != "CASH_USD", "symbol"
    ].tolist()
    latest_trading_date = date.fromisoformat(target_dates["latest_trading"])

    # Use the fixed last-200-session window, allowing up to 10% missing observations.
    await p_service.ensure_cohort_history_depth(
        sp400_tickers,
        as_of=latest_trading_date,
        session_count=200,
        minimum_observation_coverage=0.90,
    )

    munger400l_ranks = r_service.rank_munger400l(
        sp400_candidates, latest_trading_date
    )
    munger400l_picks = r_service.process_munger400_picks(
        munger400l_ranks, "munger400l", run_date
    )
    top_picks["munger400l"] = munger400l_picks

    return_observations = await p_service.prepare_munger400_return_history(run_date)
    munger400r_ranks = r_service.rank_munger400r(
        sp400_candidates,
        latest_trading_date,
        return_observations,
    )
    munger400r_picks = r_service.process_munger400_picks(
        munger400r_ranks, "munger400r", run_date
    )
    top_picks["munger400r"] = munger400r_picks

    for picks in (munger400l_picks, munger400r_picks):
        if not picks.empty:
            all_winners.extend(picks["ticker"].tolist())

    # --- D. Chart Data Preparation ---
    # Crucial: Ensure we have full history for ALL winners so charts render
    print(f"📉 Pre-heating chart data for {len(all_winners)} winners...")
    await p_service.ensure_history_depth(
        list(set(all_winners)), days_needed=365, min_rows=180
    )

    # 5. Momentum Report (Main HTML)
    print("📝 Generating Momentum HTML...")
    rep_service = ReportService()
    
    # Prefetch news/metadata
    await rep_service.cache_metadata(list(set(all_winners)))
    
    # Generate Main Report
    momentum_html = rep_service.generate_html(top_picks, target_dates, run_date)
    mom_file = REPORT_DIR / f"momentum_{run_date.isoformat()}.html"
    mom_file.write_text(momentum_html, encoding="utf-8")
    
    return mom_file

def main():
    load_dotenv()
    
    if len(sys.argv) > 1:
        run_date = date.fromisoformat(sys.argv[1])
    else:
        run_date = date.today()

    try:
        # Run Pipeline
        mom_file = asyncio.run(build_report(run_date))
        
        print(f"\n✅ SUCCESS!")
        print(f"   Momentum Report: {mom_file.absolute()}")

        # --- Build the Website ---
        build_website()
        # -------------------------
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
