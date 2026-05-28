# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pip install -r requirements.txt

# Create a .env file with your Polygon.io API key
echo "POLYGON_API_KEY=your_key_here" > .env

# Initialize the SQLite database (run once)
python init_db.py

# Sync the stock universe from State Street (SSGA) — run before first report
python universe.py --sync
```

## Running the Pipeline

```bash
# Run the full weekly report for today
python run_report.py

# Run for a specific date (useful for backfilling or testing)
python run_report.py 2026-01-13
```

## Test Scripts

These are integration-style scripts, not unit tests — they hit the live DB and Polygon API:

```bash
python test_prices.py       # Tests date resolution and Polygon fetching
python test_ranking.py      # Tests full ranking pipeline for all 3 momentum cohorts
python test_options.py      # Tests option contract selection via Polygon
```

---

## Architecture

### Data Flow

```
universe.py → prices.py → ranking.py → tracker.py → report.py → build_site.py
                                              ↓
                                        strategies.py (option picks)
                                              ↓
                                        chart_module.py (offline, SQLite only)
```

`run_report.py` is the async orchestrator that drives this entire pipeline in sequence.

### Two Strategy Engines

**Momentum Engine** (cohorts: `megacap`, `sp500`, `sp400`):
- Ranks by 12-month return, filtered to stocks where rank is improving or steady vs. last month
- Picks Top 5 per cohort
- Exit rule: rank-based — sell immediately when a stock drops out of Top 5

**Munger Engine** (cohort: `munger`, Top 50 by market cap):
- Signal: price dipped below SMA-200 within the last 10 trading days AND has recovered above SMA-10
- Requires 300+ days of continuous history per ticker (fetched by `ensure_history_depth`)
- Exit rule: time-based — hold for minimum 365 days

### Persistence

- **`data/market_data.sqlite`** — primary store for all price data and ranking history
  - `daily_prices`: OHLCV per ticker/date (primary key: `ticker, date`)
  - `top10_sp500`, `top10_sp400`, `top10_megacap`: weekly ranking snapshots with streak tracking
  - `top10_munger`: munger picks with different schema (price, SMA values instead of returns)
- **`data/trade_log.csv`** — open/closed stock positions tracked by `TradeTracker`
- **`data/option_log.csv`** — shadow option positions (100d Call, 500d LEAP, Short Put) per stock trade
- **`data/universe/`** — CSV files per cohort (`sp500.csv`, `sp400.csv`, `megacap.csv`, `munger.csv`) updated weekly from SSGA

### Key Design Constraints

**Polygon free tier rate limiting**: The API allows 5 calls/minute. `PriceService` uses `asyncio.Semaphore(1)` and explicit `asyncio.sleep(15)` between backfill calls. `OptionPicker` (sync) sleeps 13s between calls. Do not remove these sleeps.

**Chart data must be pre-heated**: `chart_module.py` reads strictly from SQLite — it never calls the API. `run_report.py` calls `ensure_history_depth()` for all winners before generating reports to guarantee chart data is available.

**Universe sourcing**: `UniverseService` downloads SSGA ETF holdings (SPY for S&P 500, MDY for S&P 400) as Excel files and parses them dynamically since SSGA doesn't provide a stable API. GOOG/GOOGL are merged into a single combined entry before deriving MegaCap and Munger sub-cohorts.

**Date resolution**: `resolve_target_dates()` walks backward from the requested date to find the nearest actual trading day with data in the DB, handling weekends and market holidays transparently.

### Automation

A GitHub Actions workflow (`.github/workflows/`) runs `run_report.py` on a schedule (Tue/Fri at 9:00 UTC) and commits all output — reports, docs site, logs, and the SQLite database — back to `main`. The static site is served from the `/docs` folder via GitHub Pages.

The `trends/` directory contains manually authored markdown files that `build_site.py` renders into the site's Trends section.
