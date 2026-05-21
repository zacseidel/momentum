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
from tracker import TradeTracker
from build_site import build_website

# Config
REPORT_DIR = Path("reports")
REPORT_DIR.mkdir(parents=True, exist_ok=True)
MOMENTUM_COHORTS = ["megacap", "sp500", "sp400"]

async def build_report(run_date: date):
    print(f"🚀 Starting Momentum Report for {run_date}")
    
    # 0. Initialize Tracker
    tracker = TradeTracker()

    # 1. Tracker Maintenance (Auto-Fill missing prices)
    print("📋 Checking for missing trade prices...")
    await tracker.resolve_prices()

    # 2. Universe Sync
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

        # Tracker: Process Signals
        tracker.process_signals(top_10, prices_df, cohort, run_date)

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

    # --- C. Chart Data Preparation ---
    # Crucial: Ensure we have full history for ALL winners so charts render
    print(f"📉 Pre-heating chart data for {len(all_winners)} winners...")
    await p_service.ensure_history_depth(list(set(all_winners)), days_needed=365)

    # 5. Momentum Report (Main HTML)
    print("📝 Generating Momentum HTML...")
    rep_service = ReportService()
    
    # Prefetch news/metadata
    await rep_service.cache_metadata(list(set(all_winners)))
    
    # Generate Main Report
    momentum_html = rep_service.generate_html(top_picks, target_dates, run_date)
    mom_file = REPORT_DIR / f"momentum_{run_date.isoformat()}.html"
    mom_file.write_text(momentum_html, encoding="utf-8")
    
    # 6. Performance Report (Tracker HTML)
    print("📝 Generating Performance HTML...")
    perf_html = tracker.render_html_report()
    perf_file = REPORT_DIR / f"performance_{run_date.isoformat()}.html"
    perf_file.write_text(perf_html, encoding="utf-8")
    
    return mom_file, perf_file

def main():
    load_dotenv()
    
    if len(sys.argv) > 1:
        run_date = date.fromisoformat(sys.argv[1])
    else:
        run_date = date.today()

    try:
        # Run Pipeline
        mom_file, perf_file = asyncio.run(build_report(run_date))
        
        print(f"\n✅ SUCCESS!")
        print(f"   1. Momentum Report:  {mom_file.absolute()}")
        print(f"   2. Perf Dashboard:   {perf_file.absolute()}")

        # --- Build the Website ---
        build_website()
        # -------------------------
        
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
