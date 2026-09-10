import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from chart_module import (
    ema,
    ma_position_signals,
    render_ma_dots,
    sma,
)
from report import ReportService


class MovingAverageMathTests(unittest.TestCase):
    def test_ema_recursive_span_3(self):
        # alpha = 2/(3+1) = 0.5; EMA_0 = first close
        close = pd.Series([10.0, 12.0, 11.0, 13.0])
        result = ema(close, 3)
        self.assertTrue(pd.isna(result.iloc[0]))
        self.assertTrue(pd.isna(result.iloc[1]))
        self.assertAlmostEqual(result.iloc[2], 11.0)
        self.assertAlmostEqual(result.iloc[3], 12.0)

    def test_sma_window_3(self):
        close = pd.Series([10.0, 12.0, 11.0, 13.0])
        result = sma(close, 3)
        self.assertTrue(pd.isna(result.iloc[0]))
        self.assertTrue(pd.isna(result.iloc[1]))
        self.assertAlmostEqual(result.iloc[2], 11.0)
        self.assertAlmostEqual(result.iloc[3], 12.0)


class PositionSignalTests(unittest.TestCase):
    def _series_with_last(self, last_close: float, n: int = 60) -> pd.Series:
        # Flat 100s so every MA is 100, then a different last close.
        values = [100.0] * (n - 1) + [last_close]
        return pd.Series(values)

    def test_close_above_all_averages(self):
        signals = ma_position_signals(self._series_with_last(101.0))
        self.assertTrue(signals["ema10_above"])
        self.assertTrue(signals["ema21_above"])
        self.assertTrue(signals["sma50_above"])

    def test_close_below_all_averages(self):
        signals = ma_position_signals(self._series_with_last(99.0))
        self.assertFalse(signals["ema10_above"])
        self.assertFalse(signals["ema21_above"])
        self.assertFalse(signals["sma50_above"])

    def test_close_equal_to_average_counts_as_above(self):
        signals = ma_position_signals(self._series_with_last(100.0))
        self.assertTrue(signals["ema10_above"])
        self.assertTrue(signals["ema21_above"])
        self.assertTrue(signals["sma50_above"])

    def test_short_history_is_unknown_not_a_crash(self):
        signals = ma_position_signals(pd.Series([10.0, 11.0, 12.0]))
        self.assertIsNone(signals["ema10_above"])
        self.assertIsNone(signals["ema21_above"])
        self.assertIsNone(signals["sma50_above"])

    def test_empty_series_is_unknown(self):
        signals = ma_position_signals(pd.Series(dtype=float))
        self.assertIsNone(signals["ema10_above"])
        self.assertIsNone(signals["ema21_above"])
        self.assertIsNone(signals["sma50_above"])


class DotHtmlTests(unittest.TestCase):
    def test_dot_order_and_classes(self):
        html = render_ma_dots({
            "ema10_above": True,
            "ema21_above": False,
            "sma50_above": None,
        })
        self.assertIn('class="ma-dots"', html)
        self.assertIn('title="10d EMA: last close above"', html)
        self.assertIn('title="21d EMA: last close below"', html)
        self.assertIn('title="50d SMA: insufficient history"', html)
        above_at = html.index('class="ma-dot above"')
        below_at = html.index('class="ma-dot below"')
        unknown_at = html.index('class="ma-dot unknown"')
        self.assertLess(above_at, below_at)
        self.assertLess(below_at, unknown_at)

    def test_missing_signals_render_unknown_dots(self):
        html = render_ma_dots(None)
        self.assertEqual(html.count("ma-dot unknown"), 3)


class ReportMarkupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.report = ReportService(Path(self.temp_dir.name) / "market_data.sqlite")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_master_template_includes_scoring_legend(self):
        html = self.report._render_master_template(
            {},
            date(2026, 1, 1),
            {"return_1y": "N/A", "return_1w": "N/A"},
            [],
        )
        self.assertIn('class="ma-legend"', html)
        self.assertIn("10-day EMA, 21-day EMA, and 50-day SMA", html)
        self.assertLess(html.index("ma-legend"), html.index("Mega Cap Leaders"))

    def test_summary_and_card_include_dots_before_ticker(self):
        dots = render_ma_dots({
            "ema10_above": True,
            "ema21_above": False,
            "sma50_above": True,
        })
        stock = {
            "ticker": "AAPL",
            "name": "Apple Inc.",
            "price": "$100.00",
            "streak_html": "✨ <strong>New Entrant</strong>",
            "ma_dots": dots,
            "current_return": "10%",
            "last_week_return": "+1%",
            "rank_change": 0,
            "headlines": [],
            "chart_uri": "",
            "cohort": "megacap",
            "description": "Consumer electronics.",
        }
        summary, cards = self.report._render_cohort([stock], [], "megacap")
        self.assertIn("ma-dots", summary)
        self.assertIn("ma-dot above", summary)
        self.assertIn(dots + "AAPL", summary)
        self.assertIn("ma-dots", cards)
        self.assertIn(dots + "AAPL", cards)


if __name__ == "__main__":
    unittest.main()
