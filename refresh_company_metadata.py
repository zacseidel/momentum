#!/usr/bin/env python
"""One-time / manual Massive ticker-overview backfill for the combined universe."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from metadata_refresh import MAX_METADATA_REFRESH, select_refresh_candidates
from report import ReportService
from universe import UniverseService

DB_PATH = Path("data/market_data.sqlite")


def _existing_metadata() -> pd.DataFrame:
    if not DB_PATH.exists():
        return pd.DataFrame(columns=["ticker", "sic_code"])
    with sqlite3.connect(DB_PATH) as conn:
        try:
            return pd.read_sql("SELECT ticker, sic_code FROM company_metadata", conn)
        except Exception:
            return pd.DataFrame(columns=["ticker", "sic_code"])


async def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Fetch Massive company profiles (SIC + market cap).")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max tickers to fetch this run (default: all names missing sic_code).",
    )
    args = parser.parse_args()

    universe = UniverseService().combined_universe_tickers()
    metadata = _existing_metadata()
    cap = args.limit if args.limit is not None else 10**9
    candidates = select_refresh_candidates(
        metadata,
        universe,
        added_tickers=(),
        limit=cap,
    )

    print(f"Combined universe: {len(universe)} tickers")
    print(f"Missing SIC / profiles to fetch: {len(candidates)}")
    if not candidates:
        print("Nothing to fetch.")
        return

    service = ReportService()
    fetched = await service.upsert_profiles(candidates)
    print(f"✅ Saved {fetched} profiles.")


if __name__ == "__main__":
    asyncio.run(main())
