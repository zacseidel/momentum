# Quantitative Strategy Engine

**Project Status:** Active / v5 Dual-Engine Architecture
**Last Update:** February 2026
**Primary Goal:** Automated weekly stock market reporting, multi-strategy execution, and performance tracking.

## 📖 Project Overview
This project implements a quantitative trading system comprising two distinct engines:
1.  **Momentum Engine (Growth):** Focuses on **S&P 500**, **S&P 400 (MidCap)**, and **MegaCap** (Top 25).
2.  **Munger Engine (Value/Reversion):** Focuses on high-quality **Top 50 Market Cap** stocks trading at a discount.

It runs a weekly pipeline that:
1.  **Syncs** the universe of stocks from State Street (SSGA).
2.  **Downloads** price history using Polygon.io (optimized for free-tier rate limits).
3.  **Ranks & Filters** stocks using cohort-specific logic (Momentum vs. Mean Reversion).
4.  **Generates** a static website (`/docs`) with interactive reports and dashboards.
5.  **Executes** a multi-strategy backtest:
    * **Stocks:** Tracks active Buy/Sell signals with hybrid exit logic (Rank-based vs. Time-based).
    * **Options:** "Shadow tracks" three specific option strategies (100d Call, LEAP, Short Put) for every stock pick.

---

## 🧠 Strategy Logic

### 1. Momentum Engine
* **Cohorts:** MegaCap, S&P 500, S&P 400.
* **Signal:** High 12-month volatility-adjusted returns with momentum persistence.
* **Selection:** Top 5 per cohort.
* **Exit Rule:** **Rank-Based.** Sell immediately when a stock drops out of the Top 5.

### 2. Munger Engine
* **Cohort:** Top 50 Stocks by Market Cap.
* **Signal:** "Quality at a Discount."
    * *Dip:* Price dipped below the **200-day Moving Average** within the last 10 days.
    * *Recovery:* Price has recovered above the **10-day Moving Average**.
* **Selection:** Opportunistic (All valid signals).
* **Exit Rule:** **Time-Based.** Hold for a minimum of **365 days** to allow for mean reversion, ignoring weekly rank fluctuations.

---

## 🏗 System Architecture (AI Context)

### Data Flow
`Universe` → `Prices (Deep History)` → `Ranking` → `Signal Generation` → `Tracker (Hybrid Exits)` → `Option Picker` → `Site Builder`

### Core Modules

#### 1. Orchestration
* **`run_report.py`**: The entry point.
    * **Role:** Async orchestrator.
    * **Logic:** Syncs universe → Resolves dates → **Pre-heats Data** (fetches full history for winners) → Calculates Ranks → Updates Tracker → Builds Website.
    * **Key Feature:** Ensures all chart data is in SQLite before report generation to prevent API throttling.

#### 2. Data Ingestion
* **`universe.py`**:
    * **Logic:** Derives `munger` cohort (Top 50) and `megacap` (Top 25) from SSGA raw files.
* **`prices.py`**:
    * **Source:** Polygon.io (Grouped Daily + Aggregates).
    * **Smart Backfill:** Includes `ensure_history_depth()` to fetch 300+ days of history for Munger candidates (needed for SMA200) vs. sparse snapshots for Momentum.

#### 3. Analytics & Strategy
* **`ranking.py`**:
    * **Momentum Logic:** `(Current - 1Y) / 1Y` with persistence checks.
    * **Munger Logic:** Vectorized Pandas check for `(Low < SMA200) & (Close > SMA10)`.
* **`strategies.py` (The Option Picker)**:
    * **Role:** Auto-selects specific option contracts (e.g., "NVDA 260515 C 140") for every new stock signal.
    * **Strategies:** 100d Call (5% OTM), LEAP (500d), Short Put (ATM).

#### 4. Visualization
* **`chart_module.py`**:
    * **Mode:** **Offline**. Reads strictly from `market_data.sqlite`.
    * **Output:** Generates Matplotlib candle charts with VOO (S&P 500) overlays for the report lightboxes.

#### 5. Portfolio Tracking (`tracker.py`)
* **Role:** The State Machine for the portfolio.
* **Hybrid Exit Logic:**
    * `if cohort == 'munger'`: Checks if `(Today - Entry_Date) > 365 days`.
    * `else`: Checks if `ticker not in current_top_5`.
* **Logs:** Maintains `data/trade_log.csv` (Stocks) and `data/option_log.csv` (Options).

---

## 🚀 Usage Guide

### 1. Setup
```bash
# Install Dependencies
pip install pandas numpy matplotlib mplfinance httpx requests jinja2 python-dotenv openpyxl markdown

# Set Environment Variables (.env)
POLYGON_API_KEY=your_key_here

# Initialize Database
python init_db.py
