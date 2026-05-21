from __future__ import annotations

import asyncio
import io
from datetime import date
from pathlib import Path
from typing import Literal
import pandas as pd
import httpx

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

# Official direct download links for State Street Global Advisors (SSGA) holdings
HOLDINGS_URLS = {
    "SPY": "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx",
    "MDY": "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-mdy.xlsx",
}

# --------------------------------------------------------------------------------------
# Main Service
# --------------------------------------------------------------------------------------

class UniverseService:
    """Maintain latest universe CSVs & an append-only change log."""

    def __init__(self, data_dir: Path | str = "data/universe") -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ------------- public -------------

    async def sync(self, *, as_of: date | None = None) -> None:
        """Download, diff, and update files. Intended to run weekly."""
        as_of = as_of or date.today()
        print(f"🟢 Starting universe sync for {as_of}...")

        # 1. Download raw data
        sp500_df, sp400_df = await self._download_all()
        print(f"🟢 Downloaded SP500 rows: {len(sp500_df)} | SP400 rows: {len(sp400_df)}")

        # 2. Derive Sub-Cohorts (MegaCap = Top 25, Munger = Top 50)
        # Note: We use a helper to properly merge GOOG/GOOGL before ranking
        megacap_df = self._derive_top_weighted(sp500_df, n=25)
        munger_df = self._derive_top_weighted(sp500_df, n=50)
        
        print(f"🟢 Derived MegaCap (Top 25) and Munger (Top 50) lists")

        # 3. Write to disk and log changes
        for cohort, new_df in {
            "sp500": sp500_df,
            "sp400": sp400_df,
            "megacap": megacap_df,
            "munger": munger_df,
        }.items():
            adds, drops = self._write_and_log(cohort, new_df, as_of)
            print(f"   {cohort.upper():7} -> wrote {len(new_df):4} rows | +{len(adds):<2} / -{len(drops):<2}")

        print("🟢 Universe sync complete! \u2713")

    def get_cohort(self, cohort: Literal["megacap", "sp500", "sp400", "munger"] = "sp500") -> pd.DataFrame:
        """Read a cleaned cohort file from disk."""
        return pd.read_csv(self.data_dir / f"{cohort}.csv")

    def get_change_log(self) -> pd.DataFrame:
        """Read the history of adds/removes."""
        return pd.read_csv(self.data_dir / "change_log.csv")

    # ------------- internal helpers -------------

    async def _download_all(self):
        """Fetch both files concurrently."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=headers) as client:
            # We use gather to run both requests at the same time
            results = await asyncio.gather(
                self._download_holdings(client, "SPY"),
                self._download_holdings(client, "MDY"),
                return_exceptions=True
            )

        # Basic error checking for the async results
        final_dfs = []
        for ticker, res in zip(["SPY", "MDY"], results):
            if isinstance(res, Exception):
                print(f"🔴 Error fetching {ticker}: {res}")
                # Return empty DF on failure to prevent crash
                final_dfs.append(pd.DataFrame(columns=["symbol", "name", "weight"]))
            else:
                final_dfs.append(res)
        
        return final_dfs[0], final_dfs[1]

    async def _download_holdings(self, client: httpx.AsyncClient, ticker: str) -> pd.DataFrame:
        url = HOLDINGS_URLS[ticker]
        resp = await client.get(url)
        resp.raise_for_status()

        # Load into memory buffer
        buf = io.BytesIO(resp.content)
        
        # Read the first ~20 rows to find the header. 
        # SSGA files often start with 4-6 lines of disclaimer/date info.
        raw_head = pd.read_excel(buf, engine="openpyxl", nrows=20, header=None)
        
        header_row_idx = None
        for i, row in raw_head.iterrows():
            # Convert row to string to search case-insensitively
            row_str = row.astype(str).str.lower().tolist()
            if any("ticker" in x for x in row_str) and any("weight" in x for x in row_str):
                header_row_idx = i
                break
        
        if header_row_idx is None:
            raise ValueError(f"Could not find header row in {ticker} file.")

        # Re-read the file using the correct header row
        buf.seek(0)
        df = pd.read_excel(buf, engine="openpyxl", skiprows=header_row_idx)
        
        # Clean column names (strip whitespace)
        df.columns = df.columns.str.strip()

        # Normalize columns map
        col_map = {}
        for c in df.columns:
            cl = c.lower()
            if "ticker" in cl or "symbol" in cl:
                col_map[c] = "symbol"
            elif "name" in cl and "fund" not in cl: # Avoid "Fund Name" header
                col_map[c] = "name"
            elif "weight" in cl:
                col_map[c] = "weight"

        # Check we have what we need
        required = {"symbol", "name", "weight"}
        if not required.issubset(set(col_map.values())):
            raise KeyError(f"Missing columns for {ticker}. Found mappings: {col_map}")

        df = df.rename(columns=col_map)[["symbol", "name", "weight"]]

        # Clean Data
        df = df.dropna(subset=["symbol"])
        df = df[df["symbol"].astype(str).str.strip() != ""] # Remove empty strings
        
        # Clean Weights (remove % sign if present and convert to float)
        def clean_weight(val):
            if isinstance(val, (int, float)):
                return val
            if isinstance(val, str):
                return float(val.replace("%", ""))
            return 0.0

        df["weight"] = df["weight"].apply(clean_weight)
        
        return df.reset_index(drop=True)

    def _derive_top_weighted(self, sp500: pd.DataFrame, n: int) -> pd.DataFrame:
        """Combine GOOG/GOOGL and return top N by weight."""
        df = sp500.copy()
        
        # Handle Google Dual Class
        goog_mask = df.symbol.isin(["GOOGL", "GOOG"])
        if goog_mask.any():
            goog_weight = df.loc[goog_mask, "weight"].sum()
            df = df.loc[~goog_mask]
            
            # Create a combined entry
            goog_entry = pd.DataFrame([{
                "symbol": "GOOGL", 
                "name": "Alphabet Inc. (Combined)", 
                "weight": goog_weight
            }])
            df = pd.concat([df, goog_entry], ignore_index=True)

        # Sort and take top N
        return df.sort_values("weight", ascending=False).head(n).reset_index(drop=True)

    def _write_and_log(self, cohort: str, new_df: pd.DataFrame, as_of: date):
        file_path = self.data_dir / f"{cohort}.csv"
        
        # Calculate diffs
        if file_path.exists():
            old_df = pd.read_csv(file_path)
            old_syms = set(old_df.symbol.astype(str))
            new_syms = set(new_df.symbol.astype(str))
            adds = new_syms - old_syms
            drops = old_syms - new_syms
        else:
            adds = set(new_df.symbol.astype(str))
            drops = set()

        # Save new file
        new_df.to_csv(file_path, index=False)

        # Append to Change Log
        if adds or drops:
            log_path = self.data_dir / "change_log.csv"
            log_entries = []
            
            for sym in adds:
                log_entries.append({"date": as_of, "cohort": cohort, "action": "add", "symbol": sym})
            for sym in drops:
                log_entries.append({"date": as_of, "cohort": cohort, "action": "drop", "symbol": sym})
            
            log_df = pd.DataFrame(log_entries)
            
            if log_path.exists():
                log_df.to_csv(log_path, mode='a', header=False, index=False)
            else:
                log_df.to_csv(log_path, mode='w', header=True, index=False)

        return adds, drops

if __name__ == "__main__":
    # Simple CLI entrypoint
    import sys
    if "--sync" in sys.argv:
        asyncio.run(UniverseService().sync())
    else:
        print("Usage: python universe.py --sync")
