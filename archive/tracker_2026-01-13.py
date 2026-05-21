import pandas as pd
import numpy as np
import asyncio
import httpx
from pathlib import Path
from datetime import date, timedelta
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

    # ------------------------------------------------------------------
    # 1. Signal Processing
    # ------------------------------------------------------------------
    def process_signals(self, current_top10: pd.DataFrame, prices_df: pd.DataFrame, cohort: str, run_date: date):
        df_stock, df_opt = self.load_logs()
        
        top_5 = current_top10.head(5).copy()
        current_tickers = set(top_5["ticker"])
        
        # A. Detect NEW BUYS
        open_trades = df_stock[(df_stock["cohort"] == cohort) & (df_stock["status"] == "OPEN")]
        open_tickers = set(open_trades["ticker"])
        new_buys = current_tickers - open_tickers
        
        new_rows = []
        new_opts = []
        
        # Helper: Lookup price in the Master DataFrame (prices_df)
        def get_reference_price(ticker):
            try:
                row = prices_df[prices_df["ticker"] == ticker]
                if not row.empty:
                    return float(row.iloc[0]["close"])
            except Exception:
                pass
            return 0.0

        # 1. Handle Brand New Signals
        for t in new_buys:
            trade_id = f"{t}_{run_date}"
            print(f"   🔔 New Signal ({cohort}): Buy {t}")
            
            curr_price = get_reference_price(t)
            
            new_rows.append({
                "trade_id": trade_id, "cohort": cohort, "ticker": t,
                "signal_date": run_date, "status": "OPEN", "user_action": "WATCH"
            })
            
            if curr_price > 0:
                print(f"      🎲 Picking options for {t} @ ${curr_price:.2f}...")
                self._pick_options_for_trade(t, curr_price, trade_id, new_opts)
            else:
                print(f"      ⚠️ Skipping option pick for {t} (Price not in snapshot).")

        # 2. Backfill Missing Options for Existing Open Trades
        for _, row in open_trades.iterrows():
            tid = row["trade_id"]
            existing_opts = df_opt[df_opt["trade_id"] == tid]
            is_pending = any(o['trade_id'] == tid for o in new_opts)
            
            if existing_opts.empty and not is_pending:
                print(f"      🎲 Backfilling missing options for {row['ticker']}...")
                
                ref_price = get_reference_price(row["ticker"])
                if ref_price == 0 and not pd.isna(row.get("buy_price")):
                     ref_price = float(row["buy_price"])
                
                if ref_price > 0:
                    self._pick_options_for_trade(row["ticker"], ref_price, tid, new_opts)
                else:
                    print(f"      ⚠️ Could not determine reference price for {row['ticker']}.")

        if new_rows:
            df_stock = pd.concat([df_stock, pd.DataFrame(new_rows)], ignore_index=True)
        if new_opts:
            df_opt = pd.concat([df_opt, pd.DataFrame(new_opts)], ignore_index=True)

        # B. Detect DROPS
        drops_mask = (df_stock["cohort"] == cohort) & (df_stock["status"] == "OPEN") & (~df_stock["ticker"].isin(current_tickers))
        if drops_mask.any():
            dropping_ids = df_stock.loc[drops_mask, "trade_id"].tolist()
            print(f"   🔔 Sell Signal ({cohort}): Dropping {len(dropping_ids)} positions")
            
            df_stock.loc[drops_mask, "drop_date"] = run_date
            df_stock.loc[drops_mask, "status"] = "CLOSED"
            
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

    # ------------------------------------------------------------------
    # 2. Price Resolution
    # ------------------------------------------------------------------
    async def resolve_prices(self):
        df_stock, df_opt = self.load_logs()
        p_service = PriceService()
        
        # A. Stock Resolution
        needs_buy = df_stock[df_stock["buy_price"].isna() & df_stock["signal_date"].notna()]
        needs_sell = df_stock[df_stock["sell_price"].isna() & df_stock["drop_date"].notna()]
        
        if not needs_buy.empty or not needs_sell.empty:
            print(f"   📉 Tracker: Resolving {len(needs_buy)} stock buys, {len(needs_sell)} sells...")
            
            async def get_stock_price(ticker, d_str, col):
                base = date.fromisoformat(str(d_str))
                for i in range(1, 6):
                    t = base + timedelta(days=i)
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

            for i, r in needs_buy.iterrows():
                d, v, s = await get_stock_price(r["ticker"], r["signal_date"], "buy_price")
                if v: 
                    df_stock.at[i, "buy_date"] = d
                    df_stock.at[i, "buy_price"] = v
                    df_stock.at[i, "spy_buy_price"] = s

            for i, r in needs_sell.iterrows():
                d, v, s = await get_stock_price(r["ticker"], r["drop_date"], "sell_price")
                if v: 
                    df_stock.at[i, "sell_date"] = d
                    df_stock.at[i, "sell_price"] = v
                    df_stock.at[i, "spy_sell_price"] = s

            self.save_logs(df_stock, df_opt)

        # B. Option Resolution
        df_stock, df_opt = self.load_logs()
        merged = df_opt.merge(df_stock[["trade_id", "buy_date", "sell_date"]], on="trade_id", how="left")
        
        needs_entry = merged[merged["entry_price"].isna() & merged["buy_date"].notna()]
        if not needs_entry.empty:
            print(f"   🎲 Tracker: Resolving {len(needs_entry)} option entries...")
            async with httpx.AsyncClient() as client:
                for _, row in needs_entry.iterrows():
                    price = await self._fetch_option_price(client, row["option_symbol"], row["buy_date"])
                    if price:
                        mask = (df_opt["trade_id"] == row["trade_id"]) & (df_opt["strategy"] == row["strategy"])
                        df_opt.loc[mask, "entry_date"] = row["buy_date"]
                        df_opt.loc[mask, "entry_price"] = price

        needs_exit = merged[merged["exit_price"].isna() & merged["sell_date"].notna()]
        if not needs_exit.empty:
            print(f"   🎲 Tracker: Resolving {len(needs_exit)} option exits...")
            async with httpx.AsyncClient() as client:
                for _, row in needs_exit.iterrows():
                    price = await self._fetch_option_price(client, row["option_symbol"], row["sell_date"])
                    if price:
                        mask = (df_opt["trade_id"] == row["trade_id"]) & (df_opt["strategy"] == row["strategy"])
                        df_opt.loc[mask, "exit_date"] = row["sell_date"]
                        df_opt.loc[mask, "exit_price"] = price

        self.save_logs(df_stock, df_opt)

    async def _fetch_option_price(self, client, symbol, date_str):
        url = f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/{date_str}/{date_str}?adjusted=true&apiKey={POLYGON_KEY}"
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                res = resp.json().get("results", [])
                if res: return res[0]["c"]
        except: pass
        return None

    # ------------------------------------------------------------------
    # 3. HTML Report (UPDATED: Closed Positions Section)
    # ------------------------------------------------------------------
    def render_html_report(self) -> str:
        df_stock, df_opt = self.load_logs()
        
        # --- A. Completed Stock Trades (Stats) ---
        completed = df_stock.dropna(subset=["buy_price", "sell_price"]).copy()
        stock_stats_html = "<p>No completed trades.</p>"
        if not completed.empty:
            completed["buy_date"] = pd.to_datetime(completed["buy_date"])
            completed["sell_date"] = pd.to_datetime(completed["sell_date"])
            completed["days"] = (completed["sell_date"] - completed["buy_date"]).dt.days.clip(lower=1)
            
            # Calculate Returns
            completed["log_ret"] = np.log(completed["sell_price"] / completed["buy_price"])
            completed["spy_log_ret"] = np.log(completed["spy_sell_price"] / completed["spy_buy_price"])
            
            # Calculate Alphas
            completed["raw_alpha"] = completed["log_ret"] - completed["spy_log_ret"]
            completed["ann_alpha"] = completed["raw_alpha"] * (365 / completed["days"])
            
            # Aggregate
            summary = completed.groupby(["cohort", "user_action"]).agg({
                "trade_id": "count",        # Trades
                "days": "mean",             # Average Days
                "log_ret": "mean",          # Average Return
                "raw_alpha": "mean",        # Average Alpha
                "ann_alpha": "mean"         # Average Ann. Alpha
            }).reset_index() # Moves Cohort/User Action from index to columns (single header row)
            
            # Rename Columns
            summary.columns = [
                "Cohort", "User Action", "Trades", "Average Days", 
                "Average Return", "Average Actual Alpha", "Average Annualized Alpha"
            ]
            
            # Apply Formatting
            formatters = {
                "Average Days": "{:.1f}".format,
                "Average Return": "{:.2%}".format,
                "Average Actual Alpha": "{:.2%}".format,
                "Average Annualized Alpha": "{:.2%}".format
            }
            
            stock_stats_html = summary.to_html(classes="styled-table", index=False, formatters=formatters)


        # --- B. Completed Option Trades (Stats) ---
        comp_opts = df_opt.dropna(subset=["entry_price", "exit_price"]).copy()
        opt_agg_html = "<p>No completed option trades.</p>"
        if not comp_opts.empty:
            comp_opts = comp_opts.merge(df_stock[["trade_id", "cohort", "user_action"]], on="trade_id", how="left")
            comp_opts["entry_date"] = pd.to_datetime(comp_opts["entry_date"])
            comp_opts["exit_date"] = pd.to_datetime(comp_opts["exit_date"])
            comp_opts["days"] = (comp_opts["exit_date"] - comp_opts["entry_date"]).dt.days.clip(lower=1)
            comp_opts["log_ret"] = np.log(comp_opts["exit_price"] / comp_opts["entry_price"])
            comp_opts["ann_log_ret"] = comp_opts["log_ret"] * (365 / comp_opts["days"])
            
            opt_summary = comp_opts.groupby(["cohort", "user_action", "strategy"]).agg({
                "option_symbol": "count",
                "ann_log_ret": "mean"
            }).reset_index()
            
            opt_summary.columns = ["Cohort", "Stock Action", "Option Strategy", "Count", "Avg Ann. Log Return"]
            opt_summary["Avg Ann. Log Return"] = opt_summary["Avg Ann. Log Return"].apply(lambda x: f"{x:.2%}")
            opt_agg_html = opt_summary.to_html(classes="styled-table", index=False)

        # --- C. Active Positions (Details) ---
        # Stocks
        open_stocks = df_stock[df_stock["status"] == "OPEN"].copy()
        if not open_stocks.empty:
            open_stocks["buy_price"] = open_stocks["buy_price"].apply(lambda x: f"${x:.2f}" if pd.notnull(x) else "Pending")
            stk_cols = ["cohort", "ticker", "signal_date", "buy_price", "user_action"]
            open_stocks = open_stocks.sort_values(["cohort", "signal_date"])
            open_stocks_html = open_stocks[stk_cols].to_html(classes="styled-table", index=False)
            num_open_stocks = len(open_stocks)
        else:
            open_stocks_html = "<p>No active stock positions.</p>"
            num_open_stocks = 0

        # Options
        df_opt_disp = df_opt.merge(df_stock[["trade_id", "ticker", "cohort", "user_action"]], on="trade_id", how="left")
        open_opts = df_opt_disp[df_opt_disp["status"] == "OPEN"].copy()
        if not open_opts.empty:
            opt_details = open_opts[["cohort", "ticker", "strategy", "option_symbol", "entry_price"]].sort_values(["cohort", "ticker"])
            opt_details["entry_price"] = opt_details["entry_price"].apply(lambda x: f"${x:.2f}" if pd.notnull(x) else "Pending")
            opt_detail_html = opt_details.to_html(classes="styled-table", index=False)
            num_open_opts = len(open_opts)
        else:
            opt_detail_html = "<p>No active option positions.</p>"
            num_open_opts = 0

        # --- D. Closed Positions (Details - Recent) ---
        # Stocks: 20 most recent
        closed_stocks = df_stock[df_stock["status"] == "CLOSED"].copy()
        if not closed_stocks.empty:
            closed_stocks["drop_date"] = pd.to_datetime(closed_stocks["drop_date"])
            # Sort descending by drop date
            closed_stocks = closed_stocks.sort_values("drop_date", ascending=False).head(20)
            
            # Formatting
            closed_stocks["buy_price"] = closed_stocks["buy_price"].apply(lambda x: f"${x:.2f}" if pd.notnull(x) else "-")
            closed_stocks["sell_price"] = closed_stocks["sell_price"].apply(lambda x: f"${x:.2f}" if pd.notnull(x) else "-")
            
            # Columns to display
            c_stk_cols = ["cohort", "ticker", "buy_date", "buy_price", "drop_date", "sell_price", "user_action"]
            closed_stocks_html = closed_stocks[c_stk_cols].to_html(classes="styled-table", index=False)
            num_closed_stocks = len(closed_stocks)
        else:
            closed_stocks_html = "<p>No closed stock positions.</p>"
            num_closed_stocks = 0

        # Options: 60 most recent
        # We reuse df_opt_disp which has the ticker merged in
        closed_opts = df_opt_disp[df_opt_disp["status"] == "CLOSED"].copy()
        if not closed_opts.empty:
            closed_opts["exit_date"] = pd.to_datetime(closed_opts["exit_date"])
            # Sort descending by exit date
            closed_opts = closed_opts.sort_values("exit_date", ascending=False).head(60)
            
            # Formatting
            closed_opts["entry_price"] = closed_opts["entry_price"].apply(lambda x: f"${x:.2f}" if pd.notnull(x) else "-")
            closed_opts["exit_price"] = closed_opts["exit_price"].apply(lambda x: f"${x:.2f}" if pd.notnull(x) else "-")

            c_opt_cols = ["cohort", "ticker", "strategy", "option_symbol", "entry_price", "exit_date", "exit_price"]
            closed_opts_html = closed_opts[c_opt_cols].to_html(classes="styled-table", index=False)
            num_closed_opts = len(closed_opts)
        else:
            closed_opts_html = "<p>No closed option positions.</p>"
            num_closed_opts = 0

        # --- E. Render Template ---
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
                h3 { color: #555; margin-top: 20px; border-bottom: 1px solid #eee; }
                details { margin-bottom: 20px; border: 1px solid #eee; border-radius: 4px; padding: 10px; }
                summary { cursor: pointer; font-weight: bold; padding: 5px; background-color: #f9f9f9; }
                .styled-table { border-collapse: collapse; margin: 15px 0; font-size: 0.9em; width: 100%; }
                .styled-table th { background-color: #2c3e50; color: #ffffff; text-align: left; padding: 8px; }
                .styled-table td { border-bottom: 1px solid #ddd; padding: 8px; }
            </style>
        </head>
        <body>
            <div style="margin-bottom: 10px;">
                <a href="../index.html" style="text-decoration:none; color:#0066cc; font-size:0.9em;">&larr; Back to Dashboard</a>
            </div>
            <h1>📈 Performance Dashboard <span style="font-size:0.5em; float:right; color:#888;">{{ date }}</span></h1>
            
            <h2>📊 Strategy Performance (Aggregate)</h2>
            
            <h3>Stocks (Alpha vs SPY)</h3>
            {{ stock_stats_html | safe }}
            
            <h3>Options (Annualized Log Returns)</h3>
            {{ opt_agg_html | safe }}
            
            <h2>🟢 Active Positions</h2>
            
            <details open>
                <summary>Active Stock Trades ({{ num_stocks }})</summary>
                {{ open_stocks_html | safe }}
            </details>
            
            <details>
                <summary>Active Option Contracts ({{ num_opts }})</summary>
                {{ opt_detail_html | safe }}
            </details>

            <h2>🔴 Closed Positions (Recent)</h2>

            <details>
                <summary>Recently Closed Stocks ({{ num_closed_stocks }})</summary>
                {{ closed_stocks_html | safe }}
            </details>
            
            <details>
                <summary>Recently Closed Options ({{ num_closed_opts }})</summary>
                {{ closed_opts_html | safe }}
            </details>
            
            <div style="margin-top:50px; text-align:center; font-size:0.8em; color:#999;">
                Generated by Momentum Tracker
            </div>
        </body>
        </html>
        """)
        
        return tpl.render(
            date=date.today(),
            stock_stats_html=stock_stats_html,
            opt_agg_html=opt_agg_html,
            open_stocks_html=open_stocks_html,
            opt_detail_html=opt_detail_html,
            num_stocks=num_open_stocks,
            num_opts=num_open_opts,
            closed_stocks_html=closed_stocks_html,
            closed_opts_html=closed_opts_html,
            num_closed_stocks=num_closed_stocks,
            num_closed_opts=num_closed_opts
        )


