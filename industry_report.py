from __future__ import annotations

from datetime import date

import pandas as pd
from jinja2 import Template

from sic import UNCLASSIFIED_SIC2

INDUSTRY_TOP_N = 5
INDUSTRY_LARGEST_N = 5
STOCK_TOP_N = 5
STOCK_LARGEST_N = 10
INDUSTRY_MIN_MEMBERS = 3
INDUSTRY_MIN_ABS_CHANGE = 1


def notable_rank_changes(
    df: pd.DataFrame,
    *,
    top_n: int,
    largest_n: int,
    min_abs_change: int = 1,
    min_members: int | None = None,
) -> dict[str, pd.DataFrame]:
    ranked = df.dropna(subset=["current_rank", "last_month_rank"]).copy()
    if min_members is not None and "n_companies" in ranked.columns:
        ranked = ranked[ranked["n_companies"] >= min_members]
        ranked = ranked[ranked["sic2"].astype(str) != UNCLASSIFIED_SIC2]
    if ranked.empty:
        empty = ranked.iloc[0:0]
        return {"top": empty, "largest": empty}

    top_mask = ranked[["current_rank", "last_month_rank"]].min(axis=1) <= top_n
    top = ranked.loc[top_mask].sort_values(["current_rank", "name" if "name" in ranked.columns else ranked.columns[0]])

    movers = ranked[ranked["rank_change"].abs() >= min_abs_change]
    largest = movers.reindex(movers["rank_change"].abs().sort_values(ascending=False).index)
    largest = largest.head(largest_n)
    sort_cols = [c for c in ("current_rank", "name", "ticker") if c in largest.columns]
    if sort_cols:
        largest = largest.sort_values(sort_cols)
    return {"top": top, "largest": largest}


def format_pct(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{float(value):+.1%}"


def format_cap(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    number = float(value)
    abs_number = abs(number)
    if abs_number >= 1e12:
        return f"${number / 1e12:.1f}T"
    if abs_number >= 1e9:
        return f"${number / 1e9:.1f}B"
    if abs_number >= 1e6:
        return f"${number / 1e6:.1f}M"
    return f"${number:,.0f}"


def format_rank(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"#{int(value)}"


def format_change(value) -> tuple[str, str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "— 0", ""
    delta = int(value)
    if delta > 0:
        return f"↑ {delta}", "up"
    if delta < 0:
        return f"↓ {abs(delta)}", "down"
    return "— 0", ""


def _entity_label(row: pd.Series, stocks: bool) -> str:
    if stocks:
        name = row.get("name") or row.get("ticker")
        ticker = row.get("ticker")
        return f"{name} ({ticker})"
    name = row.get("name") or row.get("sic2")
    sic2 = row.get("sic2")
    return f'<a href="#industry-{sic2}">{name}</a>'


def _change_rows(df: pd.DataFrame, stocks: bool) -> list[dict]:
    rows = []
    if df is None or df.empty:
        return rows
    ordered = df.sort_values(["current_rank", "name"] if "name" in df.columns else ["current_rank"])
    for _, row in ordered.iterrows():
        change_text, change_class = format_change(row.get("rank_change"))
        rows.append({
            "entity": _entity_label(row, stocks),
            "previous": format_rank(row.get("last_month_rank")),
            "current": format_rank(row.get("current_rank")),
            "change": change_text,
            "change_class": change_class,
            "ret": format_pct(row.get("current_return")),
        })
    return rows


def generate_industry_html(
    stock_ranks: pd.DataFrame,
    industry_ranks: pd.DataFrame,
    metadata: pd.DataFrame,
    run_date: date,
    voo_stats: dict | None = None,
) -> str:
    stocks = stock_ranks.copy()
    if "ticker" not in stocks.columns:
        stocks = stocks.reset_index().rename(columns={"index": "ticker"})
    meta = metadata.copy() if metadata is not None else pd.DataFrame()
    if not meta.empty:
        keep = [c for c in ("ticker", "name", "sic_code", "market_cap") if c in meta.columns]
        overlap = [c for c in keep if c != "ticker" and c in stocks.columns]
        if overlap:
            stocks = stocks.drop(columns=overlap)
        stocks = stocks.merge(meta[keep], on="ticker", how="left")
    if "name" not in stocks.columns:
        stocks["name"] = stocks["ticker"]
    else:
        stocks["name"] = stocks["name"].fillna(stocks["ticker"])

    industries = industry_ranks.copy()
    if "name" not in industries.columns:
        industries["name"] = industries["sic2"]

    industry_notable = notable_rank_changes(
        industries,
        top_n=INDUSTRY_TOP_N,
        largest_n=INDUSTRY_LARGEST_N,
        min_abs_change=INDUSTRY_MIN_ABS_CHANGE,
        min_members=INDUSTRY_MIN_MEMBERS,
    )
    stock_notable = notable_rank_changes(
        stocks,
        top_n=STOCK_TOP_N,
        largest_n=STOCK_LARGEST_N,
        min_abs_change=1,
    )

    top_stocks = stocks.dropna(subset=["current_rank"]).sort_values(["current_rank", "ticker"]).head(10)

    industry_table = industries.sort_values(["current_rank", "sic2"], na_position="last")
    industry_rows = []
    for _, row in industry_table.iterrows():
        change_text, change_class = format_change(row.get("rank_change"))
        industry_rows.append({
            "sic2": row.get("sic2"),
            "name": row.get("name"),
            "n": int(row.get("n_companies") or 0),
            "cap": format_cap(row.get("market_cap")),
            "ret": format_pct(row.get("current_return")),
            "prev_ret": format_pct(row.get("last_month_return")),
            "rank": format_rank(row.get("current_rank")),
            "change": change_text,
            "change_class": change_class,
        })

    grouped = []
    if not stocks.empty:
        if "sic2" not in stocks.columns:
            from sic import sic2_from_code
            if "sic_code" in stocks.columns:
                stocks["sic2"] = stocks["sic_code"].apply(sic2_from_code)
            else:
                stocks["sic2"] = UNCLASSIFIED_SIC2
        industry_by_code = {
            str(row["sic2"]): row for _, row in industries.iterrows()
        }
        for sic2, members in stocks.groupby("sic2", dropna=False):
            key = str(sic2)
            info = industry_by_code.get(key)
            member_rows = []
            ordered_members = members.sort_values(["current_rank", "ticker"], na_position="last")
            for _, row in ordered_members.iterrows():
                change_text, change_class = format_change(row.get("rank_change"))
                member_rows.append({
                    "name": row.get("name") or row.get("ticker"),
                    "ticker": row.get("ticker"),
                    "cap": format_cap(row.get("market_cap")),
                    "ret": format_pct(row.get("current_return")),
                    "rank": format_rank(row.get("current_rank")),
                    "change": change_text,
                    "change_class": change_class,
                })
            grouped.append({
                "sic2": key,
                "name": (info.get("name") if info is not None else None) or key,
                "ret": format_pct(info.get("current_return") if info is not None else None),
                "members": member_rows,
            })
        grouped.sort(
            key=lambda item: (
                float(industry_by_code[item["sic2"]]["current_rank"])
                if item["sic2"] in industry_by_code
                and pd.notna(industry_by_code[item["sic2"]].get("current_rank"))
                else 9999,
                item["sic2"],
            )
        )

    voo = voo_stats or {"return_1y": "—", "return_1w": "—"}
    template = Template(_PAGE_TEMPLATE)
    template.globals["change_table"] = _change_table
    top_stock_rows = []
    for rec in top_stocks.to_dict("records"):
        top_stock_rows.append({
            "rank": format_rank(rec.get("current_rank")),
            "name": rec.get("name") or rec.get("ticker"),
            "ticker": rec.get("ticker"),
            "cap": format_cap(rec.get("market_cap")),
            "ret": format_pct(rec.get("current_return")),
        })
    return template.render(
        date=run_date.strftime("%B %d, %Y"),
        voo=voo,
        industry_top=_change_rows(industry_notable["top"], stocks=False),
        industry_largest=_change_rows(industry_notable["largest"], stocks=False),
        stock_top=_change_rows(stock_notable["top"], stocks=True),
        stock_largest=_change_rows(stock_notable["largest"], stocks=True),
        industry_rows=industry_rows,
        top_stocks=top_stock_rows,
        industries=grouped,
    )


_PAGE_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Industry Momentum - {{ date }}</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; max-width: 1000px; margin: 0 auto; padding: 20px; color: #333; background-color: #fdfdfd; }
        h1 { border-bottom: 3px solid #333; padding-bottom: 15px; margin-bottom: 30px; }
        h2 { margin-top: 50px; background-color: #f4f4f4; padding: 12px; border-left: 6px solid #333; border-radius: 0 4px 4px 0; }
        h3 { margin-top: 32px; }
        .benchmark { background: #e8f5e9; padding: 15px; border-radius: 8px; margin-bottom: 30px; text-align: center; border: 1px solid #c8e6c9; }
        .lede { color: #555; font-size: 0.95em; line-height: 1.5; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 0.92em; }
        th { text-align: left; background: #eee; padding: 8px; border-bottom: 2px solid #ddd; }
        td { padding: 8px; border-bottom: 1px solid #eee; }
        td.num, th.num { text-align: right; }
        .up { color: #006400; font-weight: 600; }
        .down { color: #c42020; font-weight: 600; }
        .badge { display: inline-block; margin-left: 10px; font-size: 0.8em; background: #f0f0f0; padding: 2px 8px; border-radius: 10px; color: #444; }
        a { color: #0066cc; text-decoration: none; }
        .empty { color: #777; font-style: italic; }
    </style>
</head>
<body>
    <div style="margin-bottom: 10px;">
        <a href="../index.html">&larr; Back to Dashboard</a>
    </div>

    <h1>Industry &amp; Rank Changes <span style="float:right; font-weight:normal; font-size:0.6em; color:#777;">{{ date }}</span></h1>
    <p class="lede">S&amp;P 500 and S&amp;P 400 combined. Stocks and SIC 2-digit industries are ranked by 12-month return. Rank change compares that ranking with the same 12-month window from one month earlier. Industry returns are market-cap weighted.</p>

    <div class="benchmark">
        <strong>Benchmark (VOO)</strong><br>
        12-Mo Return: <b>{{ voo.return_1y }}</b> | 1-Week: <b>{{ voo.return_1w }}</b>
    </div>

    <h2 id="notable-changes">Notable Changes</h2>
    <h3>Industries</h3>
    <h4>Top-five comparison</h4>
    {{ change_table(industry_top) }}
    <h4>Largest rank changes</h4>
    {{ change_table(industry_largest) }}

    <h3>Stocks</h3>
    <h4>Top-five comparison</h4>
    {{ change_table(stock_top) }}
    <h4>Largest rank changes</h4>
    {{ change_table(stock_largest) }}

    <h2 id="industry-performance">Industry Performance</h2>
    <table>
        <thead>
            <tr>
                <th>Industry</th>
                <th class="num">Companies</th>
                <th class="num">Market cap</th>
                <th class="num">12m Return</th>
                <th class="num">Last month 12m</th>
                <th class="num">Rank</th>
                <th class="num">Change</th>
            </tr>
        </thead>
        <tbody>
        {% for row in industry_rows %}
            <tr>
                <td><a href="#industry-{{ row.sic2 }}">{{ row.name }}</a></td>
                <td class="num">{{ row.n }}</td>
                <td class="num">{{ row.cap }}</td>
                <td class="num">{{ row.ret }}</td>
                <td class="num">{{ row.prev_ret }}</td>
                <td class="num">{{ row.rank }}</td>
                <td class="num {{ row.change_class }}">{{ row.change }}</td>
            </tr>
        {% endfor %}
        </tbody>
    </table>

    <h2 id="current-top-stocks">Current Top Stocks</h2>
    <table>
        <thead>
            <tr>
                <th>Rank</th>
                <th>Company</th>
                <th>Ticker</th>
                <th class="num">Market cap</th>
                <th class="num">12m Return</th>
            </tr>
        </thead>
        <tbody>
        {% for row in top_stocks %}
            <tr>
                <td>{{ row.rank }}</td>
                <td>{{ row.name }}</td>
                <td>{{ row.ticker }}</td>
                <td class="num">{{ row.cap }}</td>
                <td class="num">{{ row.ret }}</td>
            </tr>
        {% endfor %}
        </tbody>
    </table>

    <h2 id="companies-by-industry">Companies by Industry</h2>
    {% for industry in industries %}
    <h3 id="industry-{{ industry.sic2 }}">
        <a href="#industry-performance">{{ industry.name }}</a>
        <span class="badge">12m {{ industry.ret }}</span>
    </h3>
    <table>
        <thead>
            <tr>
                <th>Company</th>
                <th>Ticker</th>
                <th class="num">Market cap</th>
                <th class="num">12m Return</th>
                <th class="num">Rank</th>
                <th class="num">Change</th>
            </tr>
        </thead>
        <tbody>
        {% for row in industry.members %}
            <tr>
                <td>{{ row.name }}</td>
                <td>{{ row.ticker }}</td>
                <td class="num">{{ row.cap }}</td>
                <td class="num">{{ row.ret }}</td>
                <td class="num">{{ row.rank }}</td>
                <td class="num {{ row.change_class }}">{{ row.change }}</td>
            </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endfor %}

    <div style="text-align:center; margin-top:80px; color:#999; font-size:0.8em;">
        Generated by Python Momentum Engine • {{ date }}
    </div>
</body>
</html>
"""


def _change_table(rows: list[dict]) -> str:
    if not rows:
        return "<p class='empty'>No qualifying names this week.</p>"
    lines = [
        "<table><thead><tr><th>Entity</th><th class='num'>Previous</th><th class='num'>Current</th><th class='num'>Change</th><th class='num'>Current return</th></tr></thead><tbody>"
    ]
    for row in rows:
        lines.append(
            "<tr>"
            f"<td>{row['entity']}</td>"
            f"<td class='num'>{row['previous']}</td>"
            f"<td class='num'>{row['current']}</td>"
            f"<td class='num {row['change_class']}'>{row['change']}</td>"
            f"<td class='num'>{row['ret']}</td>"
            "</tr>"
        )
    lines.append("</tbody></table>")
    return "".join(lines)



