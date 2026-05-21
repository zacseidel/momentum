import os
import requests
import pandas as pd
from datetime import date, timedelta
from dotenv import load_dotenv

# Load API Key
load_dotenv()
API_KEY = (os.getenv("POLYGON_API_KEY") or os.getenv("POLYGON_KEY") or "").strip()

if not API_KEY:
    raise RuntimeError("Missing Polygon key. Set POLYGON_API_KEY in .env")

def get_option_chain(ticker, expiration_date):
    """
    Fetches all option contracts for a ticker on a specific expiration date,
    then fetches their most recent daily bar (close price).
    """
    print(f"🔗 Fetching option chain for {ticker} expiring {expiration_date}...")
    
    # 1. Get All Contract Tickers for this Expiration
    # Endpoint: v3/reference/options/contracts
    contracts_url = "https://api.polygon.io/v3/reference/options/contracts"
    params = {
        "underlying_ticker": ticker,
        "expiration_date": expiration_date,
        "limit": 1000,
        "apiKey": API_KEY
    }
    
    resp = requests.get(contracts_url, params=params)
    if resp.status_code != 200:
        print(f"❌ Error fetching contracts: {resp.status_code} - {resp.text}")
        return
        
    results = resp.json().get("results", [])
    if not results:
        print("⚠️ No contracts found for this date.")
        return

    print(f"   Found {len(results)} contracts.")

    # 2. Fetch Prices (Grouped Daily for the specific expiration date is not standard, 
    #    so we loop or use a snapshot. For a test script, we'll loop top 10 near-the-money
    #    to show it works, or fetch yesterday's close for specific contracts).
    
    # Let's parse the contract data nicely
    chain_data = []
    
    for c in results:
        chain_data.append({
            "ticker": c["ticker"],
            "strike": c["strike_price"],
            "type": c["contract_type"], # 'call' or 'put'
            "expiration": c["expiration_date"]
        })
    
    df = pd.DataFrame(chain_data)
    df = df.sort_values("strike")
    
    # 3. Fetch Prices for the Chain (Using v2/aggs/ticker/{option_ticker}...)
    #    We will fetch the Close price for the previous trading day.
    target_date = (date.today() - timedelta(days=1 if date.today().weekday() > 0 else 3)).isoformat()
    print(f"   Fetching closing prices for {target_date}...")

    # Rate Limit Protection (Simple check)
    # Since we can't loop 100 times quickly on free tier, let's just pick 
    # 3 Calls and 3 Puts around the middle strike to demonstrate.
    
    mid_idx = len(df) // 2
    subset = pd.concat([df.iloc[mid_idx-3:mid_idx+3]])  # Middle 6 contracts

    final_rows = []
    
    for _, row in subset.iterrows():
        opt_ticker = row["ticker"]
        # Fetch yesterday's candle
        url = f"https://api.polygon.io/v2/aggs/ticker/{opt_ticker}/range/1/day/{target_date}/{target_date}"
        r = requests.get(url, params={"adjusted": "true", "apiKey": API_KEY})
        
        price = "N/A"
        vol = 0
        if r.status_code == 200 and r.json().get("results"):
            # Get 'c' (Close) and 'v' (Volume)
            bar = r.json()["results"][0]
            price = bar.get("c")
            vol = bar.get("v")
        
        final_rows.append({
            "Type": row["type"].upper(),
            "Strike": row["strike"],
            "Ticker": opt_ticker,
            "Close": price,
            "Volume": vol
        })
        
    # Display
    res_df = pd.DataFrame(final_rows)
    print("\n--- Option Chain Sample ---")
    print(res_df.to_string(index=False))

if __name__ == "__main__":
    # Pick a likely Friday expiration about 2-4 weeks out
    # You might need to adjust this date to a valid Friday!
    # Simple logic: Find next 3rd Friday or just use a specific known date.
    
    # EXAMPLE: Adjust this to a known upcoming Friday
    test_expiry = "2026-02-20" 
    
    get_option_chain("GOOG", test_expiry)