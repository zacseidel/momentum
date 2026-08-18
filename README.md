# Quantitative Market Report Engine

**Project Status:** Active
**Primary Goal:** Automated twice-weekly stock-market analysis and static report generation.

## 📖 Project Overview
This project implements a quantitative reporting system comprising three strategy families:
1.  **Momentum Engine (Growth):** Focuses on **S&P 500**, **S&P 400 (MidCap)**, and **MegaCap** (Top 25).
2.  **Munger Engine (Value/Reversion):** Focuses on high-quality **Top 50 Market Cap** stocks trading at a discount.
3.  **Munger400L / Munger400R:** Applies mean reversion to the largest S&P 400 stocks by MDY weight and to former S&P 400 return leaders.

It runs a weekly pipeline that:
1.  **Syncs** the universe of stocks from State Street (SSGA).
2.  **Downloads** price history using Polygon.io (optimized for free-tier rate limits).
3.  **Ranks & Filters** stocks using cohort-specific logic (Momentum vs. Mean Reversion).
4.  **Generates** a static website (`/docs`) with interactive reports and charts.

---

## 🧠 Strategy Logic

### 1. Momentum Engine
* **Cohorts:** MegaCap, S&P 500, S&P 400.
* **Signal:** High 12-month volatility-adjusted returns with momentum persistence.
* **Selection:** Top 5 per cohort.

### 2. Munger Engine
* **Cohort:** Top 50 Stocks by Market Cap.
* **Signal:** "Quality at a Discount."
    * *Dip:* Price dipped below the **200-day Moving Average** within the last 10 days.
    * *Recovery:* Price has recovered above the **10-day Moving Average**.
* **Selection:** Opportunistic (All valid signals).

### 3. Munger400L and Munger400R
* **Shared Signal:** At least one close below the 200-day SMA in the last 10 trading sessions, followed by the latest close above the 10-day SMA. Each SMA uses the fixed preceding 200 market sessions and requires at least 90% observation coverage; older rows are never pulled in to fill gaps.
* **Munger400L Universe:** Largest 15% of current S&P 400 constituents by MDY portfolio weight.
* **Munger400R Universe:** Current S&P 400 constituents that ranked in the top 15% by 12-month return on any twice-weekly report date during the trailing year.

---

## 🏗 System Architecture (AI Context)

### Data Flow
`Universe` → `Prices (Cached History)` → `Ranking` → `Signal Generation` → `Report` → `Site Builder`

### Core Modules

#### 1. Orchestration
* **`run_report.py`**: The entry point.
    * **Role:** Async orchestrator.
    * **Logic:** Syncs universe → Resolves dates → backfills required history → calculates ranks → renders the report → builds the website.
    * **Key Feature:** Ensures all chart data is in SQLite before report generation to prevent API throttling.

#### 2. Data Ingestion
* **`universe.py`**:
    * **Logic:** Derives `munger` cohort (Top 50) and `megacap` (Top 25) from SSGA raw files.
* **`prices.py`**:
    * **Source:** Polygon.io (Grouped Daily + Aggregates).
    * **Smart Backfill:** Uses grouped-daily calls for broad cohort gaps and ticker-range calls when only a few symbols are deficient. All Polygon calls share a 13-second throttle.

#### 3. Analytics & Strategy
* **`ranking.py`**:
    * **Momentum Logic:** `(Current - 1Y) / 1Y` with persistence checks.
    * **Munger Logic:** Vectorized Pandas check for `(Low < SMA200) & (Close > SMA10)`.
#### 4. Visualization
* **`chart_module.py`**:
    * **Mode:** **Offline**. Reads strictly from `market_data.sqlite`.
    * **Output:** Generates Matplotlib candle charts with VOO (S&P 500) overlays for the report lightboxes.

## 🚀 Usage Guide

### 1. Setup
```bash
# Install Dependencies
pip install -r requirements.txt

# Set Environment Variables (.env)
POLYGON_API_KEY=your_key_here

# Initialize Database
python init_db.py
