import os
import sqlite3
import asyncio
import httpx
import base64
import io
import pandas as pd
from datetime import date
from pathlib import Path
from jinja2 import Template
from dotenv import load_dotenv

# --- Local Imports ---
try:
    from chart_module import plot_stock_chart
    import matplotlib.pyplot as plt
    plt.set_loglevel("warning") 
except ImportError:
    print("⚠️ Warning: chart_module.py not found. Charts will be disabled.")
    plot_stock_chart = None

# --- Configuration ---
load_dotenv()
API_KEY = (os.getenv("POLYGON_API_KEY") or os.getenv("POLYGON_KEY") or "").strip()
DB_PATH = Path("data/market_data.sqlite")
UNIVERSE_LOG_PATH = Path("data/universe/change_log.csv")

# The Safety Valve: Seconds to wait between Polygon calls
RATE_LIMIT_SLEEP = 13 

class ReportService:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._ensure_metadata_tables()

    def _ensure_metadata_tables(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS company_metadata (
                    ticker TEXT PRIMARY KEY,
                    name TEXT,
                    description TEXT,
                    sector TEXT,
                    url TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS company_news (
                    id TEXT PRIMARY KEY,
                    ticker TEXT,
                    headline TEXT,
                    url TEXT,
                    published_utc TEXT,
                    summary TEXT
                )
            """)

    # ------------------------------------------------------------------
    # 1. Metadata & News Caching (With Progress Bar)
    # ------------------------------------------------------------------
    async def cache_metadata(self, tickers: list[str]):
        if not tickers: return
        
        # A. Check DB
        with sqlite3.connect(self.db_path) as conn:
            existing_meta = pd.read_sql(
                f"SELECT ticker FROM company_metadata WHERE ticker IN ({','.join(['?']*len(tickers))})", 
                conn, params=tickers
            )
            missing_meta = [t for t in tickers if t not in set(existing_meta['ticker'])]

            cutoff = (date.today() - pd.Timedelta(days=7)).isoformat()
            placeholders = ','.join(['?']*len(tickers))
            rows = conn.execute(f"""
                SELECT DISTINCT ticker FROM company_news 
                WHERE ticker IN ({placeholders}) AND published_utc > ?
            """, (*tickers, cutoff)).fetchall()
            tickers_with_news = {r[0] for r in rows}
            missing_news = [t for t in tickers if t not in tickers_with_news]

        # B. Fetch from API
        async with httpx.AsyncClient() as client:
            
            # Metadata Loop
            if missing_meta:
                print(f"📥 Need metadata for {len(missing_meta)} tickers (approx {len(missing_meta)*RATE_LIMIT_SLEEP}s)...")
                for i, t in enumerate(missing_meta):
                    print(f"    [{i+1}/{len(missing_meta)}] Fetching profile for {t}...")
                    data = await self._fetch_polygon_details(client, t)
                    self._save_metadata([data])
                    await asyncio.sleep(RATE_LIMIT_SLEEP)

            # News Loop
            if missing_news:
                print(f"📰 Need news for {len(missing_news)} tickers (approx {len(missing_news)*RATE_LIMIT_SLEEP}s)...")
                for i, t in enumerate(missing_news):
                    print(f"    [{i+1}/{len(missing_news)}] Fetching news for {t}...")
                    news_items = await self._fetch_polygon_news(client, t)
                    self._save_news(news_items)
                    await asyncio.sleep(RATE_LIMIT_SLEEP)

    # --- RESTORED HELPERS ---

    async def _fetch_polygon_details(self, client, ticker):
        url = f"https://api.polygon.io/v3/reference/tickers/{ticker}?apiKey={API_KEY}"
        try:
            resp = await client.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json().get("results", {})
                return {
                    "ticker": ticker,
                    "name": data.get("name"),
                    "description": data.get("description"),
                    "sector": data.get("sic_description") or data.get("market", ""),
                    "url": data.get("homepage_url")
                }
        except Exception: 
            pass
        return None

    async def _fetch_polygon_news(self, client, ticker):
        url = f"https://api.polygon.io/v2/reference/news?ticker={ticker}&limit=3&apiKey={API_KEY}"
        try:
            resp = await client.get(url, timeout=10)
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                return [{
                    "id": r.get("id"), "ticker": ticker, "headline": r.get("title"),
                    "url": r.get("article_url"), "published_utc": r.get("published_utc"),
                    "summary": r.get("description")
                } for r in results]
        except Exception: 
            pass
        return []

    def _save_metadata(self, items):
        items = [i for i in items if i]
        if items:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany("INSERT OR REPLACE INTO company_metadata VALUES (:ticker, :name, :description, :sector, :url)", items)

    def _save_news(self, items):
        if items:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany("INSERT OR REPLACE INTO company_news VALUES (:id, :ticker, :headline, :url, :published_utc, :summary)", items)

    # ------------------------------------------------------------------
    # 2. Data Gathering (Dropped Tickers, Universe Changes, Stats)
    # ------------------------------------------------------------------
    def _get_dropped_tickers(self, cohort: str, current_tickers: list, run_date: date) -> list:
        table_name = f"top10_{cohort}"
        current_set = set(current_tickers)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(f"SELECT MAX(date) FROM {table_name} WHERE date < ?", (run_date.isoformat(),))
                last_date = cursor.fetchone()[0]
                if not last_date: return []
                prev_df = pd.read_sql(f"SELECT ticker FROM {table_name} WHERE date = ?", conn, params=(last_date,))
                return sorted(list(set(prev_df["ticker"]) - current_set))
            except Exception:
                return []

    def _get_dropped_stats(self, dropped_tickers: list, target_dates: dict) -> list:
        if not dropped_tickers: return []
        
        needed_dates = [target_dates["latest_trading"], target_dates["minus_1_year"], target_dates["minus_1_week"]]
        placeholders = ",".join(["?"] * len(dropped_tickers))
        date_placeholders = ",".join(["?"] * len(needed_dates))
        
        with sqlite3.connect(self.db_path) as conn:
            query = f"""
                SELECT ticker, date, close 
                FROM daily_prices 
                WHERE ticker IN ({placeholders}) AND date IN ({date_placeholders})
            """
            df = pd.read_sql(query, conn, params=dropped_tickers + needed_dates)

        if df.empty: return [{"ticker": t, "ret_12m": "N/A", "ret_1w": "N/A"} for t in dropped_tickers]

        stats = []
        for t in dropped_tickers:
            t_df = df[df["ticker"] == t]
            try:
                curr = t_df[t_df["date"] == target_dates["latest_trading"]]["close"].values[0]
                prev_1y = t_df[t_df["date"] == target_dates["minus_1_year"]]["close"].values[0]
                prev_1w = t_df[t_df["date"] == target_dates["minus_1_week"]]["close"].values[0]
                
                r12 = f"{(curr - prev_1y) / prev_1y:.1%}"
                r1w = f"{(curr - prev_1w) / prev_1w:.1%}"
                
                if not r1w.startswith("-"): r1w = f"+{r1w}"
                stats.append({"ticker": t, "ret_12m": r12, "ret_1w": r1w})
            except IndexError:
                stats.append({"ticker": t, "ret_12m": "-", "ret_1w": "-"})
        return stats

    def _get_universe_changes(self, run_date: date) -> list[dict]:
        """Reads the CSV log and returns changes for the specific run date."""
        if not UNIVERSE_LOG_PATH.exists():
            return []
        
        try:
            df = pd.read_csv(UNIVERSE_LOG_PATH)
            target_str = run_date.strftime("%Y-%m-%d")
            changes = df[df["date"] == target_str].copy()
            if changes.empty: return []
            return changes.to_dict("records")
        except Exception as e:
            print(f"⚠️ Error reading universe log: {e}")
            return []

    # ------------------------------------------------------------------
    # 3. HTML Generation
    # ------------------------------------------------------------------
    def generate_html(self, top_picks: dict[str, pd.DataFrame], target_dates: dict, run_date: date) -> str:
        print("🎨 Rendering HTML Report...")
        
        voo_stats = self._get_voo_stats(target_dates)
        universe_changes = self._get_universe_changes(run_date)
        
        sections = {} 
        
        for cohort, df in top_picks.items():
            if df.empty:
                sections[cohort] = {"summary": "<p>No data.</p>", "cards": ""}
                continue
            
            enriched_df = self._enrich_data(df, cohort, target_dates)
            dropped_list = self._get_dropped_tickers(cohort, df["ticker"].tolist(), run_date)
            dropped_stats = self._get_dropped_stats(dropped_list, target_dates)
            
            summary_html, cards_html = self._render_cohort(enriched_df, dropped_stats, cohort)
            sections[cohort] = {"summary": summary_html, "cards": cards_html}

        return self._render_master_template(sections, run_date, voo_stats, universe_changes)

    def _enrich_data(self, df: pd.DataFrame, cohort: str, target_dates: dict) -> list[dict]:
        tickers = df["ticker"].tolist()
        
        with sqlite3.connect(self.db_path) as conn:
            meta = pd.read_sql(f"SELECT * FROM company_metadata WHERE ticker IN ({','.join(['?']*len(tickers))})", conn, params=tickers)
            news = pd.read_sql(f"SELECT * FROM company_news WHERE ticker IN ({','.join(['?']*len(tickers))}) ORDER BY published_utc DESC", conn, params=tickers)
            
            latest_date = target_dates["latest_trading"]
            prices = pd.read_sql(f"SELECT ticker, close FROM daily_prices WHERE date = ? AND ticker IN ({','.join(['?']*len(tickers))})", 
                               conn, params=[latest_date] + tickers)
        
        meta_dict = meta.set_index("ticker").to_dict("index")
        price_dict = prices.set_index("ticker")["close"].to_dict()
        
        enriched = []
        for _, row in df.iterrows():
            t = row["ticker"]
            info = meta_dict.get(t, {})
            headlines = news[news["ticker"] == t].head(3).to_dict("records")
            curr_price = price_dict.get(t, 0.0)
            
            desc_text = info.get("description") or "No description available."
            name_text = info.get("name") or t
            
            chart_uri = ""
            if plot_stock_chart:
                fig = None
                try:
                    print(f"   📈 Generating chart for {t}...")
                    fig, _ = plot_stock_chart(t, save_path=None)
                    buf = io.BytesIO()
                    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
                    buf.seek(0)
                    chart_b64 = base64.b64encode(buf.read()).decode()
                    chart_uri = f"data:image/png;base64,{chart_b64}"
                except Exception as e:
                    print(f"      ⚠️ Chart failed for {t}: {e}")
                finally:
                    if fig: plt.close(fig)
                    plt.close('all')

            streak = row.get("streak", 1)
            streak_start = row.get("streak_start", str(date.today()))
            if streak > 1:
                streak_html = f"🔥 <strong>since {streak_start}</strong>"
            else:
                streak_html = f"✨ <strong>New Entrant</strong>"

            enriched.append({
                **row.to_dict(), 
                "name": name_text,
                "description": desc_text,
                "price": f"${curr_price:.2f}",
                "headlines": headlines,
                "chart_uri": chart_uri,
                "streak_html": streak_html,
                "cohort": cohort
            })
            
        return enriched

    def _render_cohort(self, stocks: list[dict], dropped_stats: list[dict], cohort: str) -> tuple[str, str]:
        # 1. Active Summary
        summary_lines = []
        for s in stocks:
            anchor = f"{cohort}-{s['ticker']}"
            streak_color = "#006400" if "since" in s['streak_html'] else "#0000FF"
            
            w_ret = s['last_week_return']
            if w_ret:
                if not w_ret.startswith("-") and not w_ret.startswith("+"): w_ret = f"+{w_ret}"
                ret_color = "#c42020" if "-" in w_ret else "#006400"
                w_ret_span = f"<span style='color:{ret_color}'>{w_ret}</span>"
            else:
                w_ret_span = "N/A"

            line = f"""
                <div style="margin-bottom: 4px;">
                    <a href="#{anchor}" style="text-decoration:none; font-weight:bold; color:{streak_color};">
                        {s['ticker']}
                    </a> 
                    <span style="color:#555;">
                        ({s['price']} | {s['current_return']} 12M, {w_ret_span} 1W) - {s['streak_html']}
                    </span>
                </div>
            """
            summary_lines.append(line)
        
        # 2. Dropped Summary
        if dropped_stats:
            summary_lines.append(f"<div style='margin-top:10px; padding-top:10px; border-top:1px dashed #ccc; color:#888; font-size:0.9em;'>")
            dropped_items = []
            for d in dropped_stats:
                dw_ret = d['ret_1w']
                d_color = "#c42020" if "-" in dw_ret else "#006400"
                dropped_items.append(f"{d['ticker']} ({d['ret_12m']}, <span style='color:{d_color}'>{dw_ret}</span>)")
            
            summary_lines.append(f"<strong>Dropped:</strong> {', '.join(dropped_items)}")
            summary_lines.append("</div>")

        summary_html = "".join(summary_lines)

        # 3. Detailed Cards
        card_tpl = Template("""
        <div id="{{ cohort }}-{{ ticker }}" style="border-bottom: 2px solid #eee; padding: 30px 0;">
            <div style="display:flex; justify-content:space-between; align-items:baseline;">
                <h3 style="margin:0; font-size: 1.4em; color:#222;">
                    {{ ticker }} <span style="font-weight:normal; color:#555;">— {{ name }}</span> <span style="color:#333;">{{ price }}</span>
                </h3>
                <span style="font-size:0.9em; color:#666; background:#f5f5f5; padding: 4px 8px; border-radius:4px;">
                    Rank Change: <strong>{{ rank_change }}</strong> | {{ streak_html }}
                </span>
            </div>
            
            <div style="display:flex; margin-top:20px; gap:30px; flex-wrap:wrap;">
                <div style="flex: 1; min-width: 300px; max-width: 500px;">
                    <div style="background:#fafafa; padding:15px; border-radius:6px; margin-bottom:15px; border:1px solid #eee;">
                        <p style="margin:0; font-size: 1.1em;">
                            <strong>12-Mo Return:</strong> <span style="color:green; font-size:1.2em;">{{ current_return }}</span><br>
                            <span style="color:#666; font-size:0.9em;">Last Week: {{ last_week_return }}</span>
                        </p>
                    </div>

                    <p style="font-size:0.95em; line-height:1.6; color:#444;">{{ description }}...</p>
                    
                    <h5 style="margin-bottom:8px; margin-top:20px; border-bottom:1px solid #ddd; padding-bottom:5px;">Recent News</h5>
                    <ul style="font-size:0.9em; padding-left:20px; color:#0056b3; line-height:1.4;">
                        {% for n in headlines %}
                        <li style="margin-bottom:6px;"><a href="{{ n.url }}" style="color:inherit; text-decoration:none;" target="_blank">{{ n.headline }}</a></li>
                        {% endfor %}
                    </ul>
                </div>
                
                <div style="flex: 1; min-width: 400px;">
                    {% if chart_uri %}
                        <div class="chart-container" onclick="openLightbox('{{ chart_uri }}')">
                            <img src="{{ chart_uri }}" style="width:100%; display:block;" title="Click to Expand">
                        </div>
                        <div style="text-align:center; font-size:0.8em; color:#999; margin-top:5px;">(Click chart to expand)</div>
                    {% else %}
                        <div style="background:#f8f8f8; height:250px; display:flex; align-items:center; justify-content:center; color:#999; border:1px solid #eee; border-radius:4px;">
                            No Chart Available
                        </div>
                    {% endif %}
                </div>
            </div>
            
            <a href="#summary-{{ cohort }}" style="display:block; margin-top:20px; font-size:0.85em; text-decoration:none; color:#888;">
                ⬆ Back to {{ cohort }} Summary
            </a>
        </div>
        """)
        
        cards_html = "\n".join([card_tpl.render(**s) for s in stocks])
        return summary_html, cards_html

    def _render_master_template(self, sections, run_date, voo_stats, universe_changes):
        tpl = Template("""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Momentum Report - {{ date }}</title>
            <style>
                body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; max-width: 1000px; margin: 0 auto; padding: 20px; color: #333; background-color: #fdfdfd; }
                h1 { border-bottom: 3px solid #333; padding-bottom: 15px; margin-bottom: 30px; }
                h2 { margin-top: 50px; background-color: #f4f4f4; padding: 12px; border-left: 6px solid #333; border-radius: 0 4px 4px 0; }
                .benchmark { background: #e8f5e9; padding: 15px; border-radius: 8px; margin-bottom: 30px; text-align: center; border: 1px solid #c8e6c9; }
                .chart-container {
                    cursor: zoom-in;
                    transition: transform 0.1s;
                    border: 1px solid #ddd;
                    border-radius: 4px;
                    overflow: hidden;
                }
                .chart-container:hover {
                    border-color: #bbb;
                    box-shadow: 0 4px 8px rgba(0,0,0,0.1);
                }
                #lightbox {
                    display: none; position: fixed; z-index: 9999; left: 0; top: 0; 
                    width: 100%; height: 100%; overflow: auto; background-color: rgba(0,0,0,0.9); 
                    justify-content: center; align-items: center; animation: fadeIn 0.2s;
                }
                #lightbox-img {
                    margin: auto; display: block; max-width: 95%; max-height: 95%;
                    border-radius: 4px; box-shadow: 0 0 20px rgba(255,255,255,0.2);
                }
                
                /* Universe Change Table */
                .uni-table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 0.9em; }
                .uni-table th { text-align: left; background: #eee; padding: 8px; border-bottom: 2px solid #ddd; }
                .uni-table td { padding: 8px; border-bottom: 1px solid #eee; }
                
                @keyframes fadeIn { from {opacity: 0;} to {opacity: 1;} }
            </style>
        </head>
        <body>
            <div style="margin-bottom: 10px;">
                <a href="../index.html" style="text-decoration:none; color:#0066cc; font-size:0.9em;">&larr; Back to Dashboard</a>
            </div>

            <h1>🚀 Momentum Strategy Report <span style="float:right; font-weight:normal; font-size:0.6em; color:#777;">{{ date }}</span></h1>
            
            <div class="benchmark">
                <strong>Benchmark (VOO)</strong><br>
                12-Mo Return: <b>{{ voo.return_1y }}</b> | 1-Week: <b>{{ voo.return_1w }}</b>
            </div>

            {% if universe_changes %}
            <h2 style="border-left-color: #e67e22;">🔄 Universe Updates (Today)</h2>
            <table class="uni-table">
                <thead>
                    <tr><th>Cohort</th><th>Ticker</th><th>Action</th></tr>
                </thead>
                <tbody>
                {% for row in universe_changes %}
                    <tr>
                        <td>{{ row.cohort }}</td>
                        <td><strong>{{ row.ticker }}</strong></td>
                        <td style="font-weight:bold; color: {% if row.action == 'ADDED' %}green{% else %}red{% endif %};">{{ row.action }}</td>
                        <td>{{ row.name }}</td>
                    </tr>
                {% endfor %}
                </tbody>
            </table>
            {% endif %}

            <h2 id="summary-megacap">💎 Mega Cap Leaders</h2>
            {{ mega_summary | safe }}

            <h2 id="summary-sp500">🏢 S&P 500 Leaders</h2>
            {{ spy_summary | safe }}

            <h2 id="summary-sp400">🏭 S&P 400 (MidCap) Leaders</h2>
            {{ mdy_summary | safe }}
            
            <hr style="margin: 60px 0; border: 0; border-top: 1px solid #eee;">

            <h2>💎 Mega Cap Details</h2>
            {{ mega_cards | safe }}

            <h2>🏢 S&P 500 Details</h2>
            {{ spy_cards | safe }}

            <h2>🏭 S&P 400 Details</h2>
            {{ mdy_cards | safe }}
            
            <div style="text-align:center; margin-top:80px; color:#999; font-size:0.8em;">
                Generated by Python Momentum Engine • {{ date }}
            </div>

            <div id="lightbox" onclick="closeLightbox()">
                <img id="lightbox-img">
            </div>

            <script>
                function openLightbox(imgSrc) {
                    document.getElementById("lightbox").style.display = "flex";
                    document.getElementById("lightbox-img").src = imgSrc;
                }
                function closeLightbox() {
                    document.getElementById("lightbox").style.display = "none";
                }
                document.addEventListener('keydown', function(event) {
                    if (event.key === "Escape") closeLightbox();
                });
            </script>
        </body>
        </html>
        """)
        return tpl.render(
            date=run_date.strftime("%B %d, %Y"),
            voo=voo_stats,
            universe_changes=universe_changes,
            mega_summary=sections.get('megacap', {}).get('summary', ''), mega_cards=sections.get('megacap', {}).get('cards', ''),
            spy_summary=sections.get('sp500', {}).get('summary', ''), spy_cards=sections.get('sp500', {}).get('cards', ''),
            mdy_summary=sections.get('sp400', {}).get('summary', ''), mdy_cards=sections.get('sp400', {}).get('cards', ''),
        )

    def _get_voo_stats(self, target_dates):
        with sqlite3.connect(self.db_path) as conn:
            try:
                curr = pd.read_sql(f"SELECT close FROM daily_prices WHERE ticker='VOO' AND date='{target_dates['latest_trading']}'", conn).iloc[0,0]
                prev_yr = pd.read_sql(f"SELECT close FROM daily_prices WHERE ticker='VOO' AND date='{target_dates['minus_1_year']}'", conn).iloc[0,0]
                prev_wk = pd.read_sql(f"SELECT close FROM daily_prices WHERE ticker='VOO' AND date='{target_dates['minus_1_week']}'", conn).iloc[0,0]
                return {
                    "return_1y": f"{(curr/prev_yr - 1):.1%}",
                    "return_1w": f"{(curr/prev_wk - 1):.1%}"
                }
            except Exception:
                return {"return_1y": "N/A", "return_1w": "N/A"}