import os
import markdown
import shutil
from pathlib import Path
from datetime import datetime, date
from jinja2 import Template

# --- Config ---
BASE_DIR = Path(".")
REPORTS_DIR = BASE_DIR / "reports"
TRENDS_DIR = BASE_DIR / "trends"

SITE_DIR = BASE_DIR / "docs"
SITE_REPORTS = SITE_DIR / "reports"
SITE_TRENDS = SITE_DIR / "trends"

def build_website():
    print("🏗️  Building Static Website into /docs...")
    
    # 1. Clean Rebuild
    if SITE_DIR.exists() and SITE_DIR.name == "docs":
        shutil.rmtree(SITE_DIR) 
    
    SITE_DIR.mkdir()
    SITE_REPORTS.mkdir()
    SITE_TRENDS.mkdir()

    # 2. Reports
    report_links = []
    if REPORTS_DIR.exists():
        for f in sorted(REPORTS_DIR.glob("momentum_*.html"), reverse=True):
            shutil.copy(f, SITE_REPORTS / f.name)
            date_str = f.stem.replace("momentum_", "")
            try:
                display_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%B %d, %Y")
            except ValueError: display_date = date_str

            report_links.append({
                "date": date_str, "url": f"reports/{f.name}", "display": display_date
            })

    # 3. Performance
    perf_files = list(REPORTS_DIR.glob("performance_*.html"))
    has_perf = False
    if perf_files:
        latest_perf = sorted(perf_files)[-1]
        shutil.copy(latest_perf, SITE_DIR / "performance.html")
        has_perf = True

    # 4. Trends
    trend_links = []
    if TRENDS_DIR.exists():
        for md_file in sorted(TRENDS_DIR.glob("*.md"), reverse=True):
            text = md_file.read_text(encoding="utf-8")
            html_content = markdown.markdown(text)
            out_name = md_file.stem + ".html"
            final_html = render_page_tpl(md_file.stem.replace("-", " ").title(), html_content)
            (SITE_TRENDS / out_name).write_text(final_html, encoding="utf-8")
            trend_links.append({"title": md_file.stem.replace("-", " ").title(), "url": f"trends/{out_name}", "date": md_file.stem[:10]})

    # 5. Generate Pages
    # Index
    (SITE_DIR / "index.html").write_text(
        render_index(report_links, trend_links, has_perf), encoding="utf-8"
    )
    
    # About (UPDATED FOR MUNGER STRATEGY)
    about_content = """
    <h2>The Philosophy</h2>
    <p>This project applies quantitative strategies to the US Stock Market to remove emotion from investing. It currently runs two distinct strategy engines:</p>
    
    <h3>1. Momentum Engine</h3>
    <p>Focuses on <strong>MegaCap</strong>, <strong>S&P 500</strong>, and <strong>MidCap 400</strong> cohorts.</p>
    <ul>
        <li><strong>Ranking:</strong> Stocks are ranked by 12-month volatility-adjusted returns.</li>
        <li><strong>Selection:</strong> Top 5 tickers in each cohort.</li>
        <li><strong>Exit:</strong> Rank-based. Assets are sold immediately when they drop out of the Top 5.</li>
    </ul>

    <h3>2. Munger Engine (Mean Reversion)</h3>
    <p>Focuses on high-quality <strong>Top 50 Market Cap</strong> stocks trading at a discount.</p>
    <ul>
        <li><strong>Signal:</strong> Price dips below the 200-day Moving Average (last 10 days) but recovers above the 10-day MA.</li>
        <li><strong>Selection:</strong> Opportunistic (all valid signals).</li>
        <li><strong>Exit:</strong> Time-based. Assets are held for a minimum of 1 year to allow for mean reversion.</li>
    </ul>

    <h3>The Technology</h3>
    <p>Built with Python, using Polygon.io for data, SQLite for caching, and GitHub Actions for automation.</p>
    """
    (SITE_DIR / "about.html").write_text(
        render_page_tpl("About This Project", about_content), encoding="utf-8"
    )

    (SITE_DIR / ".nojekyll").touch()
    print(f"✅ Website built successfully.")

def render_page_tpl(title, content):
    tpl = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{{ title }}</title>
        <style>
            body { font-family: -apple-system, system-ui, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; line-height: 1.6; color: #333; }
            h1 { border-bottom: 2px solid #eee; padding-bottom: 10px; }
            a { color: #0066cc; text-decoration: none; } 
            .nav { margin-bottom: 40px; font-size: 0.9em; }
        </style>
    </head>
    <body>
        <div class="nav">
            <a href="../index.html">← Back to Dashboard</a>
        </div>
        <h1>{{ title }}</h1>
        {{ content | safe }}
    </body>
    </html>
    """
    return Template(tpl).render(title=title, content=content)

def render_index(reports, trends, has_perf):
    tpl = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Momentum Strategy Dashboard</title>
        <style>
            body { font-family: -apple-system, system-ui, sans-serif; max-width: 900px; margin: 0 auto; padding: 40px 20px; color: #222; background: #fcfcfc; }
            h1 { font-weight: 800; letter-spacing: -1px; margin-bottom: 10px; }
            h2 { margin-top: 40px; border-bottom: 2px solid #eee; padding-bottom: 10px; }
            .hero { background: #222; color: #fff; padding: 30px; border-radius: 8px; margin-bottom: 40px; display:flex; justify-content:space-between; align-items:center; }
            .hero h1 { margin: 0; color: #fff; }
            .btn { background: #2ecc71; color: #fff; text-decoration: none; padding: 10px 20px; border-radius: 4px; font-weight: bold; }
            .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 40px; }
            .card-list { list-style: none; padding: 0; }
            .card-list li { margin-bottom: 8px; padding-bottom: 8px; border-bottom: 1px solid #f0f0f0; }
            .card-list a { text-decoration: none; color: #0066cc; font-weight: 500; font-size: 1.1em; }
            .meta { font-size: 0.85em; color: #888; }
        </style>
    </head>
    <body>
        <div class="hero">
            <div>
                <h1>Quantitative Engines</h1>
                <p style="margin:5px 0 0 0; opacity:0.8;">Weekly Momentum & Mean Reversion Analysis.</p>
            </div>
            {% if has_perf %}
            <a href="zacseidel.github.io/eval" class="btn">View Performance 📈</a>
            {% endif %}
        </div>

        <div class="grid">
            <div>
                <h2>📑 Weekly Reports</h2>
                <ul class="card-list">
                    {% for r in reports %}
                    <li>
                        <a href="{{ r.url }}">{{ r.display }}</a>
                    </li>
                    {% endfor %}
                    {% if not reports %}<li>No reports found.</li>{% endif %}
                </ul>
            </div>
            
            <div>
                <h2>🧠 Insights & Trends</h2>
                <div style="background: #f9f9f9; padding: 20px; border-radius: 8px;">
                    {% for t in trends %}
                    <div style="margin-bottom: 15px;">
                        <a href="{{ t.url }}" style="font-weight:bold; color:#333; text-decoration:none;">{{ t.title }}</a>
                        <br><span class="meta">{{ t.date }}</span>
                    </div>
                    {% endfor %}
                    {% if not trends %}
                    <p style="color:#999; font-style:italic;">No insights written yet.</p>
                    {% endif %}
                </div>
            </div>
        </div>

        <div style="text-align:center; margin-top:60px; color:#ccc; font-size:0.8em;">
            Generated {{ date }} • <a href="about.html" style="color:#ccc;">About this Project</a>
        </div>
    </body>
    </html>
    """
    return Template(tpl).render(
        reports=reports, 
        trends=trends, 
        has_perf=has_perf,
        date=date.today().strftime("%Y-%m-%d")
    )

if __name__ == "__main__":
    build_website()
