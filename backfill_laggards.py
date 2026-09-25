#!/usr/bin/env python
"""
One-off backfill: add the Mega Cap Laggards summary to existing momentum reports.

Walks report dates oldest -> newest, computes picks from cached SQLite prices
(no API calls), saves them to top10_megalaggards so streaks/drops chain
correctly, and inserts a summary-only section into each report's HTML.

Top-10 membership per date:
  * On/after the first git snapshot of megacap.csv: exact SPY weights from git.
  * Before that: current SPY weights scaled by each stock's price change since
    then (approximate historical float-adjusted market cap).

Usage: python backfill_laggards.py [START_DATE] [--dry-run]
"""
import io
import re
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from chart_module import ma_signals_for_ticker, render_ma_dots
from ranking import RankingService
from report import ReportService
from universe import UniverseService

DB_PATH = Path("data/market_data.sqlite")
REPORTS_DIR = Path("reports")
START_MARK = "<!-- megalaggards:start -->"
END_MARK = "<!-- megalaggards:end -->"
SP500_ANCHOR = '<h2 id="summary-sp500">'

DOT_CSS = """<style>
    .ma-dots { display: inline-flex; gap: 3px; margin-right: 6px; vertical-align: middle; }
    .ma-dot { width: 0.7em; height: 0.7em; border-radius: 50%; display: inline-block; border: 1px solid rgba(0,0,0,.18); }
    .ma-dot.above { background: #1b8a3a; }
    .ma-dot.below { background: #c42020; }
    .ma-dot.unknown { background: #ccc; }
</style>
"""
DOT_LEGEND = (
    " Dots (left to right): 10-day EMA, 21-day EMA, 50-day SMA as of the report date;"
    " green = last close at or above, red = below."
)


def market_dates() -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM daily_prices WHERE ticker = 'VOO' ORDER BY date"
        )]


def resolve_offline(run_date: date, sessions: list[str]) -> dict[str, str]:
    """Mirror PriceService.resolve_target_dates using only cached market dates."""
    session_set = set(sessions)
    base = pd.Timestamp(run_date - timedelta(days=1))
    nominal = {
        "latest_trading": base,
        "minus_1_week": base - pd.Timedelta(weeks=1),
        "minus_1_month": base - pd.DateOffset(months=1),
        "minus_3_months": base - pd.DateOffset(months=3),
        "minus_6_months": base - pd.DateOffset(months=6),
        "minus_1_year": base - pd.DateOffset(years=1),
        "minus_13_months": base - pd.DateOffset(years=1, months=1),
    }
    resolved = {}
    for label, target in nominal.items():
        for back in range(10):
            candidate = (target - pd.Timedelta(days=back)).date().isoformat()
            if candidate in session_set:
                resolved[label] = candidate
                break
        else:
            raise RuntimeError(f"No cached market data near {target.date()} ({label})")
    return resolved


def git_megacap_snapshots() -> list[tuple[str, str]]:
    """[(commit_date, sha)] for megacap.csv, oldest first."""
    out = subprocess.run(
        ["git", "log", "--reverse", "--format=%ad %H", "--date=short", "--",
         "data/universe/megacap.csv"],
        capture_output=True, text=True, check=True,
    ).stdout.split("\n")
    return [tuple(line.split()) for line in out if line.strip()]


def megacap_from_git(sha: str) -> pd.DataFrame:
    blob = subprocess.run(
        ["git", "show", f"{sha}:data/universe/megacap.csv"],
        capture_output=True, text=True, check=True,
    ).stdout
    return pd.read_csv(io.StringIO(blob))


def estimated_megacap(as_of: str, latest: str) -> pd.DataFrame:
    """Scale current SPY weights by price(as_of) / price(latest)."""
    universe = UniverseService()
    current = universe._derive_top_weighted(universe.get_cohort("sp500"), n=60)
    tickers = current["symbol"].tolist()
    placeholders = ",".join("?" * len(tickers))
    with sqlite3.connect(DB_PATH) as conn:
        prices = pd.read_sql(
            f"SELECT ticker, date, close FROM daily_prices "
            f"WHERE ticker IN ({placeholders}) AND date IN (?, ?)",
            conn, params=tickers + [as_of, latest],
        ).pivot(index="ticker", columns="date", values="close")
        history = pd.read_sql(
            f"SELECT ticker, date, close FROM daily_prices "
            f"WHERE ticker IN ({placeholders}) AND date BETWEEN ? AND ? ORDER BY ticker, date",
            conn, params=tickers + [as_of, latest],
        )
    # Some cached series mix split-adjusted and unadjusted closes; their price
    # ratios are meaningless, so leave those names out of the size estimate.
    jumps = history.groupby("ticker")["close"].pct_change().abs() > 0.4
    broken = sorted(set(history.loc[jumps, "ticker"]))
    if broken:
        print(f"   ⚠️ {as_of}: skipping split-contaminated series {broken}")
    ratio = (prices[as_of] / prices[latest]).reindex(current["symbol"]).values
    estimated = current.assign(weight=current["weight"].values * ratio).dropna(subset=["weight"])
    estimated = estimated[~estimated["symbol"].isin(broken)]
    return estimated.sort_values("weight", ascending=False).head(25).reset_index(drop=True)


def build_section(summary_html: str, add_legend: bool) -> str:
    legend = DOT_LEGEND if add_legend else ""
    return f"""{START_MARK}
            <h2 id="summary-megalaggards" style="border-left-color: #c0392b;">🐢 Mega Cap Laggards</h2>
            <p style="font-size:0.9em; color:#666;">The 10 largest S&amp;P 500 stocks ranked by 3-, 6-, and 12-month return; these are the three with the worst average rank.{legend}</p>
            {summary_html}
            {END_MARK}

            """


def insert_section(html: str, section: str) -> str:
    if START_MARK in html:
        pattern = re.escape(START_MARK) + r".*?" + re.escape(END_MARK) + r"\s*"
        return re.sub(pattern, lambda _: section, html, count=1, flags=re.S)
    if SP500_ANCHOR not in html:
        raise RuntimeError("S&P 500 summary heading not found")
    html = html.replace(SP500_ANCHOR, section + SP500_ANCHOR, 1)
    if ".ma-dot {" not in html:
        html = html.replace("</head>", DOT_CSS + "</head>", 1)
    return html


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    start = args[0] if args else "2026-01-20"

    sessions = market_dates()
    snapshots = git_megacap_snapshots()
    first_snapshot = snapshots[0][0] if snapshots else "9999-12-31"
    ranking = RankingService(DB_PATH)
    reporter = ReportService(DB_PATH)

    report_files = sorted(
        f for f in REPORTS_DIR.glob("momentum_*.html")
        if f.stem.replace("momentum_", "") >= start
    )
    print(f"Backfilling {len(report_files)} reports from {start} (git weights from {first_snapshot})")

    for path in report_files:
        run_date = date.fromisoformat(path.stem.replace("momentum_", ""))
        dates = resolve_offline(run_date, sessions)

        if run_date.isoformat() >= first_snapshot:
            sha = [s for d, s in snapshots if d <= run_date.isoformat()][-1]
            megacap = megacap_from_git(sha)
            source = "git"
        else:
            megacap = estimated_megacap(dates["latest_trading"], sessions[-1])
            source = "est"

        tickers = megacap["symbol"].tolist()
        placeholders = ",".join("?" * len(tickers))
        needed = [dates[k] for k in ("latest_trading", "minus_3_months", "minus_6_months", "minus_1_year")]
        with sqlite3.connect(DB_PATH) as conn:
            prices = pd.read_sql(
                f"SELECT ticker, date, close FROM daily_prices "
                f"WHERE ticker IN ({placeholders}) AND date IN ({','.join('?' * 4)})",
                conn, params=tickers + needed,
            )

        ranks = ranking.rank_megacap_laggards(megacap, prices, dates)
        top10 = megacap.sort_values("weight", ascending=False).head(10)["symbol"].tolist()
        if dry_run:
            print(f"{run_date} [{source}] top10={','.join(top10)} -> {ranks['ticker'].tolist()}")
            continue

        picks = ranking.process_megacap_laggards(ranks, run_date)
        if picks.empty:
            raise RuntimeError(f"No laggard picks for {run_date}")
        with sqlite3.connect(DB_PATH) as conn:
            closes = dict(conn.execute(
                f"SELECT ticker, close FROM daily_prices WHERE date = ? AND ticker IN ({','.join('?' * len(picks))})",
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

        dropped = reporter._get_dropped_tickers("megalaggards", picks["ticker"].tolist(), run_date)
        dropped_stats = reporter._get_dropped_stats(dropped, dates)
        summary_html, _ = reporter._render_cohort(stocks, dropped_stats, "megalaggards", link_cards=False)

        html = path.read_text(encoding="utf-8")
        has_legend = 'class="ma-legend"' in html
        path.write_text(insert_section(html, build_section(summary_html, not has_legend)), encoding="utf-8")
        print(f"   ✏️  {path.name} [{source}] {picks['ticker'].tolist()} dropped={dropped}")


if __name__ == "__main__":
    main()
