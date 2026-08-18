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
python -m unittest test_munger400_unit.py  # Offline Munger400 unit coverage
```

---

## Architecture

### Data Flow

```
universe.py → prices.py → ranking.py → report.py → build_site.py
                                      ↓
                                chart_module.py (offline, SQLite only)
```

`run_report.py` is the async orchestrator that drives this entire pipeline in sequence.

### Two Strategy Engines

**Momentum Engine** (cohorts: `megacap`, `sp500`, `sp400`):
- Ranks by 12-month return, filtered to stocks where rank is improving or steady vs. last month
- Picks Top 5 per cohort

**Munger Engine** (cohort: `munger`, Top 50 by market cap):
- Signal: price dipped below SMA-200 within the last 10 trading days AND has recovered above SMA-10
- Requires 300+ days of continuous history per ticker (fetched by `ensure_history_depth`)

**Munger400 Report Strategies** (current `sp400` constituents):
- `munger400l`: largest 15% by MDY portfolio weight, then the close-based Munger SMA signal
- `munger400r`: top 15% by 12-month return on any Tue/Fri report observation in the trailing year, then the same SMA signal
- Each SMA-200 calculation uses its fixed preceding 200 market sessions, requires at least 90% observation coverage, and never pulls in older rows to fill gaps
- Both persist independent report streaks and render as separate report sections
- Observation pairs outside Polygon's retention window are skipped unless already present in SQLite

### Persistence

- **`data/market_data.sqlite`** — primary store for all price data and ranking history
  - `daily_prices`: OHLCV per ticker/date (primary key: `ticker, date`)
  - `top10_sp500`, `top10_sp400`, `top10_megacap`: weekly ranking snapshots with streak tracking
  - `top10_munger`: munger picks with different schema (price, SMA values instead of returns)
  - `top10_munger400l`, `top10_munger400r`: independent report-only SP400 mean-reversion snapshots
- **`data/universe/`** — CSV files per cohort (`sp500.csv`, `sp400.csv`, `megacap.csv`, `munger.csv`) updated weekly from SSGA

### Key Design Constraints

**Polygon free tier rate limiting**: All Polygon requests in `PriceService` pass through one shared 13-second throttle. Broad SP400 history gaps use grouped-daily calls; isolated gaps use ticker-range calls.

**Chart data must be pre-heated**: `chart_module.py` reads strictly from SQLite — it never calls the API. `run_report.py` calls `ensure_history_depth()` for all winners before generating reports to guarantee chart data is available.

**Universe sourcing**: `UniverseService` downloads SSGA ETF holdings (SPY for S&P 500, MDY for S&P 400) as Excel files and parses them dynamically since SSGA doesn't provide a stable API. GOOG/GOOGL are merged into a single combined entry before deriving MegaCap and Munger sub-cohorts.

**Date resolution**: `resolve_target_dates()` walks backward from the requested date to find the nearest actual trading day with data in the DB, handling weekends and market holidays transparently.

### Automation

A GitHub Actions workflow (`.github/workflows/`) runs `run_report.py` on a Tuesday/Friday schedule and commits the reports, docs site, universe files, and SQLite cache back to `main`. The static site is served from the `/docs` folder via GitHub Pages.

The `trends/` directory contains manually authored markdown files that `build_site.py` renders into the site's Trends section.
