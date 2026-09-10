import sqlite3
from pathlib import Path
from typing import Optional, Tuple

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd

# --- Config ---
DB_PATH = Path("data/market_data.sqlite")
EMA10_COLOR = "#1b9e77"
EMA21_COLOR = "#7570b3"
EMA10_SPAN = 10
EMA21_SPAN = 21
SMA50_WINDOW = 50

_DOT_SPECS = (
    ("10d EMA", "ema10_above"),
    ("21d EMA", "ema21_above"),
    ("50d SMA", "sma50_above"),
)

def _fetch_history_from_db(ticker: str, days_back=365) -> pd.DataFrame:
    """
    Fetches daily OHLCV from the local SQLite DB.
    Assumes data has already been synced/backfilled by the PriceService.
    """
    # Calculate cutoff date
    cutoff_date = (pd.Timestamp.now() - pd.Timedelta(days=days_back)).strftime("%Y-%m-%d")

    # Connect and Query
    # Note: We assume the DB exists because run_report.py creates it
    if not DB_PATH.exists():
        return pd.DataFrame()

    with sqlite3.connect(DB_PATH) as conn:
        query = """
            SELECT date, open, high, low, close, volume 
            FROM daily_prices 
            WHERE ticker = ? AND date >= ? 
            ORDER BY date ASC
        """
        df = pd.read_sql_query(query, conn, params=(ticker, cutoff_date))

    if df.empty:
        return pd.DataFrame()

    # Format for mplfinance
    # 1. Set Index to Datetime
    df["Date"] = pd.to_datetime(df["date"])
    df = df.set_index("Date").sort_index()
    
    # 2. Rename columns to TitleCase (Required by mplfinance)
    df = df.rename(columns={
        "open": "Open", 
        "high": "High", 
        "low": "Low", 
        "close": "Close", 
        "volume": "Volume"
    })
    
    return df[["Open", "High", "Low", "Close", "Volume"]]


def ema(close: pd.Series, span: int) -> pd.Series:
    """Standard recursive EMA: alpha = 2/(span+1), seeded from the first observation."""
    return close.astype(float).ewm(span=span, adjust=False, min_periods=span).mean()


def sma(close: pd.Series, window: int) -> pd.Series:
    return close.astype(float).rolling(window=window, min_periods=window).mean()


def moving_average_series(close: pd.Series) -> dict:
    close = close.astype(float)
    return {
        "ema10": ema(close, EMA10_SPAN),
        "ema21": ema(close, EMA21_SPAN),
        "sma50": sma(close, SMA50_WINDOW),
    }


def _last_above(close: pd.Series, average: pd.Series) -> Optional[bool]:
    if close.empty or average.empty:
        return None
    last_close = close.iloc[-1]
    last_avg = average.iloc[-1]
    if pd.isna(last_close) or pd.isna(last_avg):
        return None
    return float(last_close) >= float(last_avg)


def ma_position_signals(close: pd.Series) -> dict:
    """Last-close vs 10d EMA / 21d EMA / 50d SMA. True=above, False=below, None=unknown."""
    empty = pd.Series(dtype=float)
    if close is None or len(close) == 0:
        return {
            "ema10": empty,
            "ema21": empty,
            "sma50": empty,
            "ema10_above": None,
            "ema21_above": None,
            "sma50_above": None,
        }
    series = moving_average_series(close)
    return {
        **series,
        "ema10_above": _last_above(close, series["ema10"]),
        "ema21_above": _last_above(close, series["ema21"]),
        "sma50_above": _last_above(close, series["sma50"]),
    }


def ma_signals_for_ticker(ticker: str) -> dict:
    df = _fetch_history_from_db(ticker)
    if df.empty:
        return ma_position_signals(pd.Series(dtype=float))
    return ma_position_signals(df["Close"])


def render_ma_dots(signals: Optional[dict] = None) -> str:
    """Three traffic-light dots: 10d EMA, 21d EMA, 50d SMA."""
    signals = signals or {}
    parts = []
    for label, key in _DOT_SPECS:
        above = signals.get(key)
        if above is True:
            cls, title = "above", f"{label}: last close above"
        elif above is False:
            cls, title = "below", f"{label}: last close below"
        else:
            cls, title = "unknown", f"{label}: insufficient history"
        parts.append(f'<span class="ma-dot {cls}" title="{title}"></span>')
    return f'<span class="ma-dots">{"".join(parts)}</span>'


def _ema_addplots(close: pd.Series) -> list:
    """Skip an EMA overlay when the series has no plottable values yet."""
    series = moving_average_series(close)
    addplots = []
    for key, color, label in (
        ("ema10", EMA10_COLOR, "10d EMA"),
        ("ema21", EMA21_COLOR, "21d EMA"),
    ):
        values = series[key]
        if values.notna().any():
            addplots.append(
                mpf.make_addplot(values.values, color=color, width=1.1, label=label)
            )
    return addplots


def plot_stock_chart(ticker: str, save_path: str = None, benchmark_ticker="VOO") -> Tuple:
    """
    Generates a candle chart with VOO overlay using LOCAL DATA.
    Returns: (fig, axes) tuple.
    """
    # 1. Get Data from DB
    df = _fetch_history_from_db(ticker)
    
    # Safety: If data is sparse (e.g. only 2 momentum snapshots), we can't chart it.
    # The orchestrator (run_report.py) must ensure 'ensure_history_depth' was called first.
    if df.empty or len(df) < 20:
        print(f"⚠️ Skipping chart for {ticker}: Not enough history in DB ({len(df)} rows).")
        return None, None
    
    bench = _fetch_history_from_db(benchmark_ticker)
    
    # 2. Align Benchmark (Normalize VOO to start at Ticker's price)
    addplots = []
    
    if not bench.empty:
        common_idx = df.index.intersection(bench.index)
        
        if not common_idx.empty:
            # Start comparison from the 10th common point to stabilize
            anchor_pos = min(10, len(common_idx) - 1) 
            anchor_date = common_idx[anchor_pos]
            
            # Calculate scaling factor
            t_price = df.loc[anchor_date, "Close"]
            b_price = bench.loc[anchor_date, "Close"]
            
            if b_price > 0:
                scale = t_price / b_price
                
                # Create normalized series
                bench_norm = bench.loc[common_idx, "Close"] * scale
                
                # Reindex to match the main dataframe exactly (handle gaps)
                bench_aligned = bench_norm.reindex(df.index) 
                
                addplots.append(
                    mpf.make_addplot(
                        bench_aligned.values, 
                        color="orange", 
                        linestyle="dashed", 
                        width=1.5,
                        label=f"{benchmark_ticker} (Comp)"
                    )
                )

    addplots.extend(_ema_addplots(df["Close"]))

    # 3. Plot Style
    mc = mpf.make_marketcolors(up="#00b300", down="#ff3333", edge="inherit", wick="inherit", volume="in")
    s = mpf.make_mpf_style(base_mpf_style="yahoo", marketcolors=mc, gridstyle=":", rc={"font.size": 10})

    # 4. Generate Plot
    try:
        fig, axes = mpf.plot(
            df,
            type="candle",
            volume=True,
            mav=(20, 50),
            addplot=addplots,
            style=s,
            title=f"\n{ticker} vs {benchmark_ticker} (1Y)",
            returnfig=True,
            figsize=(10, 5),
            tight_layout=True,
            datetime_format="%b %Y"
        )
        
        # Legend
        if axes and len(axes) > 0:
            axes[0].legend(loc="upper left")
        
        if save_path:
            fig.savefig(save_path, bbox_inches="tight")
            # Close memory to prevent leaks in loops
            plt.close(fig)
            
        return fig, axes
    except Exception as e:
        print(f"❌ Error plotting {ticker}: {e}")
        return None, None

if __name__ == "__main__":
    # Test assumes you have data in the sqlite DB
    print("Testing Chart Module (Offline Mode)...")
    plot_stock_chart("NVDA", "test_chart.png")
