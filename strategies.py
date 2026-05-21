import os
import time
import requests
import pandas as pd
from datetime import date, timedelta
from dotenv import load_dotenv

# --- Config ---
load_dotenv()
API_KEY = (os.getenv("POLYGON_API_KEY") or os.getenv("POLYGON_KEY") or "").strip()
RATE_LIMIT_SLEEP = 13  # 13s * 4 calls = ~52s. Keeps us safe under 5 calls/min.

class OptionPicker:
    def __init__(self):
        self.base_url = "https://api.polygon.io/v3/reference/options/contracts"
        self.session = requests.Session()

    def find_best_contract(self, ticker: str, stock_price: float, strategy_type: str):
        """
        Finds the specific contract symbol for a given strategy.
        Strategies: '100d_Call', '500d_LEAP', 'Short_Put'
        """
        # --- 1. Rate Limit Safety Valve ---
        print(f"         zzz Sleeping {RATE_LIMIT_SLEEP}s for API limit...")
        time.sleep(RATE_LIMIT_SLEEP)
        
        # --- 2. Strategy Parameters ---
        if strategy_type == "100d_Call":
            target_days = 100
            target_strike = stock_price * 1.05
            contract_type = "call"
            # Standard window for mid-term options
            date_window = 45  
            strike_window_pct = 0.25 
        
        elif strategy_type == "500d_LEAP":
            target_days = 500
            target_strike = stock_price * 1.10
            contract_type = "call"
            # HUGE window to bridge the gap between January LEAP cycles
            date_window = 200 
            strike_window_pct = 0.40
            
        elif strategy_type == "Short_Put":
            target_days = 30
            target_strike = stock_price * 1.00
            contract_type = "put"
            # Tight window for monthly/weekly expirations
            date_window = 20 
            strike_window_pct = 0.20
        else:
            return None

        target_date = date.today() + timedelta(days=target_days)
        
        # --- 3. Build Query ---
        # Widen the search range significantly based on strategy
        min_date = (target_date - timedelta(days=date_window)).isoformat()
        max_date = (target_date + timedelta(days=date_window)).isoformat()
        
        params = {
            "underlying_ticker": ticker,
            "contract_type": contract_type,
            "expiration_date.gte": min_date,
            "expiration_date.lte": max_date,
            "strike_price.gte": target_strike * (1 - strike_window_pct),
            "strike_price.lte": target_strike * (1 + strike_window_pct),
            "limit": 1000, # Max out results to avoid pagination missing best picks
            "apiKey": API_KEY
        }
        
        try:
            resp = self.session.get(self.base_url, params=params, timeout=10)
            
            if resp.status_code != 200:
                print(f"         ⚠️ API Error {resp.status_code}: {resp.text[:100]}")
                return None

            candidates = resp.json().get("results", [])
            
            if not candidates:
                print(f"         ⚠️ No candidates found for {ticker} {strategy_type}")
                print(f"            (Searched {min_date} to {max_date})")
                return None
            
            # --- 4. Selection Logic ---
            best_c = None
            min_score = float("inf")
            
            for c in candidates:
                exp = date.fromisoformat(c["expiration_date"])
                strike = c["strike_price"]
                
                # Score: Minimize distance to Target Date and Target Strike
                date_diff = abs((exp - target_date).days)
                strike_diff_pct = abs((strike - target_strike) / target_strike) * 100
                
                # We weight Strike heavily (5.0) because having the right Moneyness is 
                # usually more important than being off by a week or two.
                score = date_diff + (strike_diff_pct * 5.0)
                
                if score < min_score:
                    min_score = score
                    best_c = c
            
            if best_c:
                return {
                    "ticker": ticker,
                    "strategy": strategy_type,
                    "option_symbol": best_c["ticker"],
                    "expiration": best_c["expiration_date"],
                    "strike": best_c["strike_price"],
                    "contract_type": contract_type
                }

        except Exception as e:
            print(f"   ⚠️ Option pick error ({ticker}): {e}")
            
        return None