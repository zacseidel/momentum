#!/usr/bin/env python
"""Rebuild the combined-universe industry HTML from SQLite (no Massive calls)."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from build_site import build_website
from ranking import RankingService
from report import ReportService
from sic import load_sic_major_groups
from universe import UniverseService

REPORT_DIR = Path("reports")
DB_PATH = Path("data/market_data.sqlite")


def _busy_dates() -> set[str]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT date FROM daily_prices GROUP BY date HAVING COUNT(DISTINCT ticker) > 100"
        )
        return {row[0] for row in rows}


def nearest_trading_day(target: date, busy: set[str]) -> str:
    current = target
    for _ in range(10):
        iso = current.isoformat()
        if iso in busy:
            return iso
        current -= timedelta(days=1)
    raise RuntimeError(f"No cached trading day near {target}")


def resolve_dates(run_date: date) -> dict[str, str]:
    busy = _busy_dates()
    base = run_date - timedelta(days=1)
    ts = pd.Timestamp(base)
    return {
        "latest_trading": nearest_trading_day(base, busy),
        "minus_1_week": nearest_trading_day((ts - pd.Timedelta(weeks=1)).date(), busy),
        "minus_1_month": nearest_trading_day((ts - pd.DateOffset(months=1)).date(), busy),
        "minus_1_year": nearest_trading_day((ts - pd.DateOffset(years=1)).date(), busy),
        "minus_13_months": nearest_trading_day((ts - pd.DateOffset(years=1, months=1)).date(), busy),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_date", nargs="?", default=date.today().isoformat())
    parser.add_argument("--skip-site", action="store_true")
    args = parser.parse_args()
    run_date = date.fromisoformat(args.run_date)

    tickers = UniverseService().combined_universe_tickers()
    date_map = resolve_dates(run_date)
    placeholders = ",".join(["?"] * len(date_map))
    with sqlite3.connect(DB_PATH) as conn:
        prices = pd.read_sql(
            f"SELECT ticker, date, close FROM daily_prices WHERE date IN ({placeholders})",
            conn,
            params=list(date_map.values()),
        )
    prices = prices[prices["ticker"].isin(tickers)]

    ranking = RankingService()
    ranks = ranking.calculate_ranks(prices, date_map, require_improving=False)
    ranking.save_momentum_ranks(ranks, run_date)

    report = ReportService()
    metadata = report.load_company_metadata(tickers)
    industries = ranking.rank_industries(ranks, metadata, load_sic_major_groups())
    ranking.save_industry_ranks(industries, run_date)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    html = report.generate_industry_html(ranks, industries, run_date, date_map)
    out = REPORT_DIR / f"industry_{run_date.isoformat()}.html"
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out} ({len(ranks)} stocks, {len(industries)} industries)")
    if not args.skip_site:
        build_website()


if __name__ == "__main__":
    main()
