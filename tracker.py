import pandas as pd
import numpy as np
import asyncio
import httpx
import time
from pathlib import Path
from datetime import date, timedelta, datetime
from jinja2 import Template
from prices import PriceService
from strategies import OptionPicker
import os
from dotenv import load_dotenv

# --- Config ---
load_dotenv()
POLYGON_KEY = os.getenv("POLYGON_API_KEY")
LOG_PATH = Path("data/trade_log.csv")
OPT_LOG_PATH = Path("data/option_log.csv")
# 13s ensures we stay under 5 calls/min (60 / 13 = 4.6 calls)
API_WAIT_SECONDS = 13 

class TradeTracker:
    def __init__(self):
        self.log_path = LOG_PATH
        self.opt_log_path = OPT_LOG_PATH
        self._ensure_logs_exist()
        self.option_picker = OptionPicker()

    def _ensure_logs_exist(self):
        if not self.log_path.exists():
            df = pd.DataFrame(columns=[
                "trade_id", "cohort", "ticker", 
                "signal_date", "buy_date", "buy_price", "spy_buy_price",
                "drop_date", "sell_date", "sell_price", "spy_sell_price",
                "status", "user_action"
            ])
            df.to_csv(self.log_path, index=False, encoding="utf-8")
            
        if not self.opt_log_path.exists():
            df_opt = pd.DataFrame(columns=[
                "trade_id", "strategy", "option_symbol", 
                "expiration", "strike", "contract_type",
                "entry_date", "entry_price", "exit_date", "exit_price",
                "status"
            ])
            df_opt.to_csv(self.opt_log_path, index=False, encoding="utf-8")

    def load_logs(self):
        return (
            pd.read_csv(self.log_path, encoding="utf-8"),
            pd.read_csv(self.opt_log_path, encoding="utf-8")
        )

    def save_logs(self, df_stock, df_opt):
        df_stock.to_csv(self.log_path, index=False, encoding="utf-8")
        df_opt.to_csv(self.opt_log_path, index=False, encoding="utf-8")

    def process_signals(self, current_top10: pd.DataFrame, prices_df: pd.DataFrame, cohort: str, run_date: date):
        df_stock, df_opt = self.load_logs()
        
        # --- 1. Selection Logic ---
        if cohort == "munger":
            current_picks = current_top10.copy()
        else:
            current_picks = current_top10.head(5).copy()
            
        current_tickers = set(current_picks["ticker"])
        open_trades = df_stock[(df_stock["cohort"] == cohort) & (df_stock["status"] == "OPEN")]
        open_tickers = set(open_trades["ticker"])
        
        # --- 2. Buy Logic ---
        new_buys = current_tickers - open_tickers
        new_rows = []
        new_opts = []
        
        def get_reference_price(ticker):
            try:
                row = prices_df[prices_df["ticker"] == ticker]
                if not row.empty: return float(row.iloc[0]["close"])
            except Exception: pass
            return 0.0

        for t in new_buys:
            trade_id = f"{t}_{run_date}"
            print(f"   🔔 New Signal ({cohort}): Buy {t}")
            curr_price = get_reference_price(t)
            new_rows.append({
                "trade_id": trade_id, "cohort": cohort, "ticker": t,
                "signal_date": run_date, "status": "OPEN", "user_action": "WATCH"
            })
            if curr_price > 0:
                self._pick_options_for_trade(t, curr_price, trade_id, new_opts)

        if new_rows:
            df_stock = pd.concat([df_stock, pd.DataFrame(new_rows)], ignore_index=True)
        if new_opts:
            df_opt = pd.concat([df_opt, pd.DataFrame(new_opts)], ignore_index=True)

        # --- 3. Exit Logic ---
        dropping_ids = []

        if cohort == "munger":
            # Time-Based Exit (1 Year)
            for idx, row in open_trades.iterrows():
                entry_val = row.get("buy_date")
                if pd.isna(entry_val): entry_val = row["signal_date"]
                try:
                    entry_dt = pd.to_datetime(entry_val).date()
                    days_held = (run_date - entry_dt).days
                    if days_held >= 365:
                        print(f"   ⏳ Munger Exit ({row['ticker']}): Held for {days_held} days.")
                        dropping_ids.append(row["trade_id"])
                except Exception: continue

        else:
            # Rank-Based Exit
            drops_mask = (df_stock["cohort"] == cohort) & \
                         (df_stock["status"] == "OPEN") & \
                         (~df_stock["ticker"].isin(current_tickers))
            if drops_mask.any():
                dropping_ids = df_stock.loc[drops_mask, "trade_id"].tolist()

        # --- 4. Apply Closures ---
        if dropping_ids:
            stk_mask = df_stock["trade_id"].isin(dropping_ids)
            df_stock.loc[stk_mask, "drop_date"] = run_date
            df_stock.loc[stk_mask, "status"] = "CLOSED"
            opt_mask = (df_opt["trade_id"].isin(dropping_ids)) & (df_opt["status"] == "OPEN")
            df_opt.loc[opt_mask, "status"] = "CLOSED"

        self.save_logs(df_stock, df_opt)

    def _pick_options_for_trade(self, ticker, price, trade_id, result_list):
        strategies = ["100d_Call", "500d_LEAP", "Short_Put"]
        for strat in strategies:
            contract = self.option_picker.find_best_contract(ticker, price, strat)
            if contract:
                result_list.append({
                    "trade_id": trade_id, "strategy": strat,
                    "option_symbol": contract["option_symbol"],
                    "expiration": contract["expiration"],
                    "strike": contract["strike"],
                    "contract_type": contract["contract_type"],
                    "status": "OPEN"
                })

    async def resolve_prices(self):
        """
        OPTIMIZED: Only attempts to resolve prices for recent trades (< 14 days old).
        Prevents infinite retry loops on broken/old data.
        """
        return None
        df_stock, df_opt = self.load_logs()
        p_service = PriceService()
        
        # SAFETY VALVE: Don't look back further than 14 days
        cutoff_date = date.today() - timedelta(days=14)
        cutoff_str = cutoff_date.isoformat()

        # --- 1. Stock Prices ---
        # Filter for missing prices WHERE date is recent
        needs_buy = df_stock[
            (df_stock["buy_price"].isna()) & 
            (df_stock["signal_date"] >= cutoff_str)
        ]
        needs_sell = df_stock[
            (df_stock["sell_price"].isna()) & 
            (df_stock["drop_date"] >= cutoff_str)
        ]
        
        # Log skipped items (optional, for your sanity)
        skipped_buy = len(df_stock[df_stock["buy_price"].isna()]) - len(needs_buy)
        if skipped_buy > 0:
            print(f"   ⚠️ Skipping {skipped_buy} old missing stock BUY prices (older than {cutoff_str})")

        async def get_stock_price(ticker, d_str, col, min_date=None, max_date=None):
            base = date.fromisoformat(str(d_str))
            for i in range(1, 6):
                t = base + timedelta(days=i)
                if max_date and t >= max_date: return None, None, None
                if min_date and t <= min_date: continue
                if t > date.today(): return None, None, None
                try:
                    await p_service._ensure_date_data(t)
                    snap = await p_service.get_snapshots([ticker, "VOO"], {"tgt":t.isoformat()})
                    if not snap.empty:
                        row = snap[snap["ticker"]==ticker]
                        spy = snap[snap["ticker"]=="VOO"]
                        if not row.empty and not spy.empty:
                            v = row.iloc[0]["high"] if col=="buy_price" else row.iloc[0]["low"]
                            sv = spy.iloc[0]["high"] if col=="buy_price" else spy.iloc[0]["low"]
                            return t.isoformat(), v, sv
                except: pass
            return None, None, None

        if not needs_buy.empty:
            print(f"   🔎 Resolving {len(needs_buy)} recent stock entries...")
            for i, r in needs_buy.iterrows():
                drop_dt = date.fromisoformat(str(r["drop_date"])) if pd.notnull(r.get("drop_date")) else None
                d, v, s = await get_stock_price(r["ticker"], r["signal_date"], "buy_price", max_date=drop_dt)
                if v: 
                    df_stock.at[i, "buy_date"] = d
                    df_stock.at[i, "buy_price"] = v
                    df_stock.at[i, "spy_buy_price"] = s

        if not needs_sell.empty:
            print(f"   🔎 Resolving {len(needs_sell)} recent stock exits...")
            for i, r in needs_sell.iterrows():
                current_row = df_stock.loc[i]
                buy_dt = date.fromisoformat(str(current_row["buy_date"])) if pd.notnull(current_row["buy_date"]) else None
                d, v, s = await get_stock_price(r["ticker"], r["drop_date"], "sell_price", min_date=buy_dt)
                if v: 
                    df_stock.at[i, "sell_date"] = d
                    df_stock.at[i, "sell_price"] = v
                    df_stock.at[i, "spy_sell_price"] = s

        self.save_logs(df_stock, df_opt)

        # --- 2. Option Prices ---
        df_stock, df_opt = self.load_logs()
        merged = df_opt.merge(df_stock[["trade_id", "buy_date", "sell_date", "signal_date", "drop_date"]], on="trade_id", how="left")
        
        # Use buy_date if avail, else signal_date for cutoff check
        merged["eff_entry_date"] = merged["buy_date"].fillna(merged["signal_date"])
        merged["eff_exit_date"] = merged["sell_date"].fillna(merged["drop_date"])

        # Filter Option Entries
        needs_entry = merged[
            (merged["entry_price"].isna()) & 
            (merged["eff_entry_date"] >= cutoff_str)
        ]
        
        # Filter Option Exits
        needs_exit = merged[
            (merged["exit_price"].isna()) & 
            (merged["eff_exit_date"].notna()) & 
            (merged["eff_exit_date"] >= cutoff_str)
        ]

        skipped_opt = len(merged[merged["entry_price"].isna()]) - len(needs_entry)
        if skipped_opt > 0:
            print(f"   ⚠️ Skipping {skipped_opt} old missing option prices (older than {cutoff_str})")

        if not needs_entry.empty:
            print(f"   🎲 Resolving {len(needs_entry)} recent option entries...")
            async with httpx.AsyncClient() as client:
                for _, row in needs_entry.iterrows():
                    ref_date = row["buy_date"] if pd.notnull(row["buy_date"]) else row["trade_id"].split('_')[1]
                    price = await self._fetch_option_price(client, row["option_symbol"], ref_date)
                    if price:
                        mask = (df_opt["trade_id"] == row["trade_id"]) & (df_opt["strategy"] == row["strategy"])
                        df_opt.loc[mask, "entry_date"] = ref_date
                        df_opt.loc[mask, "entry_price"] = price

        if not needs_exit.empty:
            print(f"   🎲 Resolving {len(needs_exit)} recent option exits...")
            async with httpx.AsyncClient() as client:
                for _, row in needs_exit.iterrows():
                    price = await self._fetch_option_price(client, row["option_symbol"], row["sell_date"])
                    if price:
                        mask = (df_opt["trade_id"] == row["trade_id"]) & (df_opt["strategy"] == row["strategy"])
                        df_opt.loc[mask, "exit_date"] = row["sell_date"]
                        df_opt.loc[mask, "exit_price"] = price

        self.save_logs(df_stock, df_opt)

    async def _fetch_option_price(self, client, symbol, date_str):
        return None
        base = date.fromisoformat(str(date_str))
        
        for i in range(1, 6):
            t = base + timedelta(days=i)
            if t.weekday() >= 5: continue 
            if t > date.today(): break
                
            t_str = t.isoformat()
            
            # Enforce rate limit
            print(f"      zzz Waiting {API_WAIT_SECONDS}s for Polygon API...")
            await asyncio.sleep(API_WAIT_SECONDS)
            
            url = f"https://api.polygon.io/v1/open-close/option/{symbol}/{t_str}?adjusted=true&apiKey={POLYGON_KEY}"
            
            try:
                resp = await client.get(url, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    price = data.get("close") or data.get("open")
                    if price: return price
                elif resp.status_code == 404:
                    print(f"      ℹ️ No quote data for {t_str}, trying next day...")
            except Exception as e:
                print(f"      ⚠️ Request error: {e}")
                
        return None
        
    def render_html_report(self) -> str:
        # (Keep existing render_html_report logic exactly as is - no changes needed there)
        # I am omitting it here for brevity, but include the full method from previous turn in your file.
        # ... [Paste render_html_report code here] ...
        df_stock, df_opt = self.load_logs()
        
        completed = df_stock.dropna(subset=["buy_price", "sell_price", "buy_date", "sell_date"]).copy()
        stock_stats_html = "<p>No completed trades.</p>"
        if not completed.empty:
            completed["buy_date"] = pd.to_datetime(completed["buy_date"])
            completed["sell_date"] = pd.to_datetime(completed["sell_date"])
            completed = completed[completed["buy_date"] < completed["sell_date"]]
            if not completed.empty:
                completed["days"] = (completed["sell_date"] - completed["buy_date"]).dt.days.clip(lower=1)
                completed["log_ret"] = np.log(completed["sell_price"] / completed["buy_price"])
                completed["spy_log_ret"] = np.log(completed["spy_sell_price"] / completed["spy_buy_price"])
                completed["raw_alpha"] = completed["log_ret"] - completed["spy_log_ret"]
                completed["win"] = (completed["log_ret"] > 0).astype(int)
                
                summary = completed.groupby(["cohort", "user_action"]).agg({
                    "trade_id": "count", "win": "mean", "days": "mean", "log_ret": "mean", "raw_alpha": "mean"
                }).reset_index()
                summary.columns = ["Cohort", "Action", "Trades", "Win Rate", "Avg Days", "Avg Log Ret", "Avg Alpha"]
                formatters = {"Win Rate": "{:.1%}".format, "Avg Days": "{:.1f}".format, "Avg Log Ret": "{:.2%}".format, "Avg Alpha": "{:.2%}".format}
                stock_stats_html = summary.to_html(classes="styled-table", index=False, formatters=formatters)

        comp_opts = df_opt.dropna(subset=["entry_price", "exit_price"]).copy()
        opt_agg_html = "<p>No completed option trades.</p>"
        if not comp_opts.empty:
            comp_opts = comp_opts.merge(df_stock[["trade_id", "cohort", "user_action"]], on="trade_id", how="left")
            comp_opts["entry_date"] = pd.to_datetime(comp_opts["entry_date"])
            comp_opts["exit_date"] = pd.to_datetime(comp_opts["exit_date"])
            comp_opts = comp_opts[comp_opts["entry_date"] < comp_opts["exit_date"]]
            if not comp_opts.empty:
                comp_opts["log_ret"] = np.log(comp_opts["exit_price"] / comp_opts["entry_price"])
                comp_opts["win"] = (comp_opts["log_ret"] > 0).astype(int)
                opt_summary = comp_opts.groupby(["cohort", "user_action", "strategy"]).agg({
                    "option_symbol": "count", "win": "mean", "log_ret": "mean"
                }).reset_index()
                opt_summary.columns = ["Cohort", "Stock Action", "Strategy", "Count", "Win Rate", "Avg Log Ret"]
                opt_summary["Win Rate"] = opt_summary["Win Rate"].apply(lambda x: f"{x:.1%}")
                opt_summary["Avg Log Ret"] = opt_summary["Avg Log Ret"].apply(lambda x: f"{x:.2%}")
                opt_agg_html = opt_summary.to_html(classes="styled-table", index=False)

        df_opt_disp = df_opt.merge(df_stock[["trade_id", "ticker", "cohort", "user_action"]], on="trade_id", how="left")
        open_stocks = df_stock[df_stock["status"] == "OPEN"].copy()
        open_opts = df_opt_disp[df_opt_disp["status"] == "OPEN"].copy()
        
        open_stocks_html = open_stocks[["cohort", "ticker", "signal_date", "buy_price"]].to_html(classes="styled-table", index=False) if not open_stocks.empty else "<p>None</p>"
        opt_detail_html = open_opts[["cohort", "ticker", "strategy", "option_symbol", "entry_price"]].to_html(classes="styled-table", index=False) if not open_opts.empty else "<p>None</p>"

        closed_stocks = df_stock[df_stock["status"] == "CLOSED"].copy().sort_values("drop_date", ascending=False).head(20)
        closed_opts = df_opt_disp[df_opt_disp["status"] == "CLOSED"].copy().sort_values("exit_date", ascending=False).head(60)
        
        for df in [closed_stocks, closed_opts]:
            if not df.empty:
                for col in ["buy_price", "sell_price", "entry_price", "exit_price"]:
                    if col in df.columns:
                        df[col] = df[col].apply(lambda x: f"${x:.2f}" if pd.notnull(x) else "-")

        closed_stk_html = closed_stocks[["cohort", "ticker", "buy_date", "buy_price", "drop_date", "sell_price"]].to_html(classes="styled-table", index=False) if not closed_stocks.empty else "<p>None</p>"
        closed_opt_html = closed_opts[["cohort", "ticker", "strategy", "option_symbol", "entry_price", "exit_date", "exit_price"]].to_html(classes="styled-table", index=False) if not closed_opts.empty else "<p>None</p>"

        tpl = Template("""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>Performance Tracker</title>
            <style>
                body { font-family: -apple-system, sans-serif; max-width: 1100px; margin: 0 auto; padding: 20px; color: #333; background: #fdfdfd; }
                h1 { border-bottom: 2px solid #333; padding-bottom: 10px; }
                h2 { margin-top: 40px; background: #f4f4f4; padding: 10px; border-left: 5px solid #2980b9; }
                details { margin-top: 10px; border: 1px solid #eee; padding: 10px; border-radius: 4px; }
                summary { font-weight: bold; cursor: pointer; }
                .styled-table { border-collapse: collapse; margin: 15px 0; font-size: 0.9em; width: 100%; }
                .styled-table th { background-color: #2c3e50; color: #ffffff; text-align: left; padding: 8px; }
                .styled-table td { border-bottom: 1px solid #ddd; padding: 8px; }
            </style>
        </head>
        <body>
            <h1>📈 Performance Dashboard</h1>
            <h2>📊 Aggregate Performance</h2>
            <h3>Stocks (Alpha vs SPY)</h3> {{ stock_stats_html | safe }}
            <h3>Options (Log Returns)</h3> {{ opt_agg_html | safe }}
            
            <h2>🟢 Active Positions</h2>
            <details open><summary>Active Stocks</summary>{{ open_stocks_html | safe }}</details>
            <details><summary>Active Options</summary>{{ opt_detail_html | safe }}</details>

            <h2>🔴 Closed Positions (Recent)</h2>
            <details><summary>Recently Closed Stocks (Last 20)</summary>{{ closed_stk_html | safe }}</details>
            <details><summary>Recently Closed Options (Last 60)</summary>{{ closed_opt_html | safe }}</details>
        </body>
        </html>
        """)
        return tpl.render(stock_stats_html=stock_stats_html, opt_agg_html=opt_agg_html, open_stocks_html=open_stocks_html, opt_detail_html=opt_detail_html, closed_stk_html=closed_stk_html, closed_opt_html=closed_opt_html)
