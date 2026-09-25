#!/usr/bin/env python
"""
One-off backfill: add the S&P 500 / S&P 400 Rank Momentum summaries to existing
momentum reports.

Walks report dates oldest -> newest, resolves target dates exactly as the live
pipeline does (fetching a grouped-daily snapshot for any 3/6-month date that was
never cached in full), rebuilds each index's membership on that date by
replaying data/universe/change_log.csv, saves picks to top10_rankmom500 /
top10_rankmom400 so streaks and drops chain correctly, and inserts summary-only
sections into each report's HTML.

Usage: python backfill_rankmom.py [START_DATE] [--dry-run]
"""
import asyncio
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from backfill_laggards import DOT_CSS, DOT_LEGEND
from chart_module import ma_signals_for_ticker, render_ma_dots
from prices import PriceService
from ranking import RANK_MOMENTUM_COHORTS, RankingService
from report import ReportService

DB_PATH = Path("data/market_data.sqlite")
REPORTS_DIR = Path("reports")
CHANGE_LOG = Path("data/universe/change_log.csv")
START_MARK = "<!-- rankmom:start -->"
END_MARK = "<!-- rankmom:end -->"
MEGACAP_ANCHOR = '<h2 id="summary-megacap">'
TITLES = {"rankmom500": "S&amp;P 500", "rankmom400": "S&amp;P 400"}


def members_on(log: pd.DataFrame, cohort: str, run_date: str) -> list[str]:
    """Replay the universe change log (sync runs first, so same-day changes apply)."""
    members = set()
    rows = log[(log["cohort"] == cohort) & (log["date"] <= run_date)]
    for action, symbol in zip(rows["action"], rows["symbol"]):
        if action == "add":
            members.add(symbol)
        else:
            members.discard(symbol)
    return sorted(members)


def load_prices(tickers: list[str], dates: dict) -> pd.DataFrame:
    needed = sorted(set(dates.values()))
    with sqlite3.connect(DB_PATH) as conn:
        prices = pd.read_sql(
            f"SELECT ticker, date, close FROM daily_prices "
            f"WHERE date IN ({','.join('?' * len(needed))})",
            conn, params=needed,
        )
    return prices[prices["ticker"].isin(tickers)]


def build_section(summaries: dict[str, str], add_legend: bool) -> str:
    legend = DOT_LEGEND if add_legend else ""
    blocks = []
    for cohort, summary_html in summaries.items():
        blocks.append(f"""
            <h2 id="summary-{cohort}" style="border-left-color: #16a085;">📶 {TITLES[cohort]} Rank Momentum</h2>
            <p style="font-size:0.9em; color:#666;">{TITLES[cohort]} stocks ranked by 3-, 6-, and 12-month return; these are the ten with the best average rank.{legend}</p>
            {summary_html}
""")
        legend = ""
    return f"{START_MARK}{''.join(blocks)}            {END_MARK}\n\n            "


def insert_section(html: str, section: str) -> str:
    if START_MARK in html:
        pattern = re.escape(START_MARK) + r".*?" + re.escape(END_MARK) + r"\s*"
        return re.sub(pattern, lambda _: section, html, count=1, flags=re.S)
    if MEGACAP_ANCHOR not in html:
        raise RuntimeError("Mega Cap summary heading not found")
    html = html.replace(MEGACAP_ANCHOR, section + MEGACAP_ANCHOR, 1)
    if ".ma-dot {" not in html:
        html = html.replace("</head>", DOT_CSS + "</head>", 1)
    return html


def render_summary(reporter, picks, cohort, dates, run_date) -> tuple[str, list]:
    with sqlite3.connect(DB_PATH) as conn:
        closes = dict(conn.execute(
            f"SELECT ticker, close FROM daily_prices WHERE date = ? "
            f"AND ticker IN ({','.join('?' * len(picks))})",
            [dates["latest_trading"]] + picks["ticker"].tolist(),
        ).fetchall())

    stocks = []
    for _, row in picks.iterrows():
        ticker = row["ticker"]
        streak_html = (
            f"🔥 <strong>since {row['streak_start']}</strong>" if row["streak"] > 1
            else "✨ <strong>New Entrant</strong>"
        )
        stocks.append({
            **row.to_dict(),
            "price": f"${closes.get(ticker, 0.0):.2f}",
            "streak_html": streak_html,
            "ma_dots": render_ma_dots(ma_signals_for_ticker(ticker, as_of=dates["latest_trading"])),
        })

    dropped = reporter._get_dropped_tickers(cohort, picks["ticker"].tolist(), run_date)
    dropped_stats = reporter._get_dropped_stats(dropped, dates)
    summary_html, _ = reporter._render_cohort(stocks, dropped_stats, cohort, link_cards=False)
    return summary_html, dropped


async def main():
    load_dotenv()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    start = args[0] if args else "0000-00-00"

    log = pd.read_csv(CHANGE_LOG)
    prices_service = PriceService()
    # Grouped-daily saves are filtered to known tickers; include former members too.
    prices_service.valid_tickers |= set(log["symbol"].dropna())
    ranking = RankingService(DB_PATH)
    reporter = ReportService(DB_PATH)

    report_files = sorted(
        f for f in REPORTS_DIR.glob("momentum_*.html")
        if f.stem.replace("momentum_", "") >= start
    )
    print(f"Backfilling {len(report_files)} reports from {report_files[0].stem[9:]}")

    for path in report_files:
        run_date = date.fromisoformat(path.stem.replace("momentum_", ""))
        dates = await prices_service.resolve_target_dates(run_date)

        summaries = {}
        for cohort, index_cohort in RANK_MOMENTUM_COHORTS.items():
            tickers = members_on(log, index_cohort, run_date.isoformat())
            ranks = ranking.rank_rank_momentum(
                pd.DataFrame({"symbol": tickers}), load_prices(tickers, dates), dates
            )
            if dry_run:
                print(f"{run_date} {cohort} n={ranks['universe_size'].iloc[0] if not ranks.empty else 0}"
                      f"/{len(tickers)} -> {ranks['ticker'].tolist()}")
                continue

            picks = ranking.process_rank_momentum(ranks, cohort, run_date)
            if picks.empty:
                raise RuntimeError(f"No {cohort} picks for {run_date}")
            summaries[cohort], dropped = render_summary(reporter, picks, cohort, dates, run_date)
            print(f"   ✏️  {path.name} {cohort} n={picks['universe_size'].iloc[0]}/{len(tickers)} "
                  f"{picks['ticker'].tolist()} dropped={dropped}")

        if dry_run:
            continue
        html = path.read_text(encoding="utf-8")
        has_legend = 'class="ma-legend"' in html or "10-day EMA, 21-day EMA" in html
        path.write_text(insert_section(html, build_section(summaries, not has_legend)), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
