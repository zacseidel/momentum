import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from industry_report import notable_rank_changes
from metadata_refresh import select_refresh_candidates
from ranking import RankingService
from sic import UNCLASSIFIED_SIC2, sic2_from_code
from universe import is_valid_ticker


class SicTests(unittest.TestCase):
    def test_sic2_from_four_digit_code(self):
        self.assertEqual(sic2_from_code("3571"), "35")

    def test_sic2_from_missing_is_unclassified(self):
        self.assertEqual(sic2_from_code(None), UNCLASSIFIED_SIC2)
        self.assertEqual(sic2_from_code(""), UNCLASSIFIED_SIC2)
        self.assertEqual(sic2_from_code("nan"), UNCLASSIFIED_SIC2)


class TickerFilterTests(unittest.TestCase):
    def test_rejects_placeholders(self):
        self.assertFalse(is_valid_ticker("-"))
        self.assertFalse(is_valid_ticker("CASH_USD"))
        self.assertFalse(is_valid_ticker("2602335D"))
        self.assertTrue(is_valid_ticker("AAPL"))
        self.assertTrue(is_valid_ticker("BRK.B"))


class CombinedRankTests(unittest.TestCase):
    def setUp(self):
        self.service = RankingService(Path("unused.sqlite"))
        self.dates = {
            "latest_trading": "2026-09-04",
            "minus_1_week": "2026-08-28",
            "minus_1_month": "2026-08-04",
            "minus_1_year": "2025-09-04",
            "minus_13_months": "2025-08-04",
        }

    def _prices(self):
        # A: 12m +100% now, +10% last month -> rank improves
        # B: 12m +50% now, +80% last month -> rank worsens
        # C: 12m +10% now, +10% last month -> steady
        rows = []
        closes = {
            "A": {"2026-09-04": 200, "2026-08-28": 190, "2026-08-04": 110, "2025-09-04": 100, "2025-08-04": 100},
            "B": {"2026-09-04": 150, "2026-08-28": 148, "2026-08-04": 180, "2025-09-04": 100, "2025-08-04": 100},
            "C": {"2026-09-04": 110, "2026-08-28": 109, "2026-08-04": 110, "2025-09-04": 100, "2025-08-04": 100},
        }
        for ticker, points in closes.items():
            for day, close in points.items():
                rows.append({"ticker": ticker, "date": day, "close": close})
        return pd.DataFrame(rows)

    def test_unfiltered_ranks_include_decliners(self):
        ranked = self.service.calculate_ranks(self._prices(), self.dates, require_improving=False)
        self.assertEqual(list(ranked.index), ["A", "B", "C"])
        self.assertEqual(int(ranked.loc["A", "current_rank"]), 1)
        self.assertEqual(int(ranked.loc["B", "current_rank"]), 2)
        self.assertGreater(ranked.loc["A", "rank_change"], 0)
        self.assertLess(ranked.loc["B", "rank_change"], 0)

    def test_improving_filter_drops_decliners(self):
        ranked = self.service.calculate_ranks(self._prices(), self.dates, require_improving=True)
        self.assertNotIn("B", ranked.index)
        self.assertIn("A", ranked.index)


class IndustryAggregationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "t.sqlite"
        self.service = RankingService(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_cap_weighted_industry_returns_and_ranks(self):
        ranks = pd.DataFrame({
            "ticker": ["A1", "A2", "B1"],
            "current_return": [0.10, 0.20, 0.50],
            "last_month_return": [0.40, 0.40, 0.10],
            "last_week_return": [0.0, 0.0, 0.0],
            "current_rank": [3, 2, 1],
            "last_month_rank": [1, 2, 3],
            "rank_change": [-2, 0, 2],
        })
        metadata = pd.DataFrame({
            "ticker": ["A1", "A2", "B1"],
            "sic_code": ["3571", "3572", "7372"],
            "market_cap": [100.0, 300.0, 50.0],
            "name": ["One", "Two", "Three"],
        })
        industries = self.service.rank_industries(ranks, metadata, {"35": "Machinery", "73": "Business Services"})
        by_code = industries.set_index("sic2")
        # Group 35: (0.10*100 + 0.20*300) / 400 = 0.175
        self.assertAlmostEqual(float(by_code.loc["35", "current_return"]), 0.175)
        self.assertEqual(int(by_code.loc["73", "current_rank"]), 1)
        self.assertEqual(int(by_code.loc["35", "current_rank"]), 2)
        self.assertLess(float(by_code.loc["35", "rank_change"]), 0)

    def test_missing_sic_goes_to_unclassified(self):
        ranks = pd.DataFrame({
            "ticker": ["Z"],
            "current_return": [0.1],
            "last_month_return": [0.1],
            "last_week_return": [0.0],
            "current_rank": [1],
            "last_month_rank": [1],
            "rank_change": [0],
        })
        metadata = pd.DataFrame({"ticker": ["Z"], "sic_code": [None], "market_cap": [10.0]})
        industries = self.service.rank_industries(ranks, metadata)
        self.assertEqual(industries.iloc[0]["sic2"], UNCLASSIFIED_SIC2)

    def test_small_and_unclassified_groups_excluded_from_notable(self):
        industries = pd.DataFrame({
            "sic2": ["35", "73", "00", "28"],
            "name": ["Machinery", "Services", "Unclassified", "Chemicals"],
            "n_companies": [5, 4, 20, 2],
            "current_return": [0.4, 0.3, 0.9, 0.2],
            "last_month_return": [0.1, 0.2, 0.1, 0.5],
            "current_rank": [1, 2, 3, 4],
            "last_month_rank": [3, 2, 4, 1],
            "rank_change": [2, 0, 1, -3],
        })
        notable = notable_rank_changes(
            industries,
            top_n=5,
            largest_n=5,
            min_abs_change=1,
            min_members=3,
        )
        largest_codes = set(notable["largest"]["sic2"])
        self.assertIn("35", largest_codes)
        self.assertNotIn("00", largest_codes)
        self.assertNotIn("28", largest_codes)


class MetadataCandidateTests(unittest.TestCase):
    def test_skips_complete_profiles_and_prefers_adds(self):
        metadata = pd.DataFrame({
            "ticker": ["AAPL", "MSFT", "OLD"],
            "sic_code": ["3571", "7372", "1311"],
        })
        universe = ["AAPL", "MSFT", "NEW", "OLD"]
        selected = select_refresh_candidates(
            metadata,
            universe,
            added_tickers=["OLD", "NEW"],
            limit=15,
        )
        self.assertEqual(selected, ["OLD", "NEW"])

    def test_honors_limit(self):
        metadata = pd.DataFrame(columns=["ticker", "sic_code"])
        universe = [f"T{i}" for i in range(20)]
        selected = select_refresh_candidates(metadata, universe, limit=15)
        self.assertEqual(len(selected), 15)
        self.assertEqual(selected, universe[:15])

    def test_missing_sic_is_selected(self):
        metadata = pd.DataFrame({
            "ticker": ["AAPL", "MSFT"],
            "sic_code": ["3571", None],
        })
        selected = select_refresh_candidates(metadata, ["AAPL", "MSFT"], limit=15)
        self.assertEqual(selected, ["MSFT"])


class HtmlReportTests(unittest.TestCase):
    def test_industry_page_contains_sections_and_drilldown(self):
        from industry_report import generate_industry_html

        stocks = pd.DataFrame({
            "ticker": ["AAA", "BBB", "CCC", "DDD"],
            "current_return": [0.4, 0.3, 0.2, 0.1],
            "last_month_return": [0.1, 0.2, 0.25, 0.3],
            "last_week_return": [0.0, 0.0, 0.0, 0.0],
            "current_rank": [1, 2, 3, 4],
            "last_month_rank": [4, 3, 2, 1],
            "rank_change": [3, 1, -1, -3],
            "name": ["Alpha", "Beta", "Gamma", "Delta"],
            "sic_code": ["3571", "3571", "3571", "7372"],
            "market_cap": [100.0, 80.0, 70.0, 50.0],
            "sic2": ["35", "35", "35", "73"],
        })
        industries = pd.DataFrame({
            "sic2": ["35", "73"],
            "name": ["Machinery", "Services"],
            "n_companies": [3, 1],
            "market_cap": [250.0, 50.0],
            "coverage": [1.0, 1.0],
            "current_return": [0.31, 0.1],
            "last_month_return": [0.18, 0.3],
            "current_rank": [1, 2],
            "last_month_rank": [2, 1],
            "rank_change": [1, -1],
        })
        html = generate_industry_html(stocks, industries, stocks, date(2026, 9, 8))
        self.assertIn("Notable Changes", html)
        self.assertIn("Industry Performance", html)
        self.assertIn("Current Top Stocks", html)
        self.assertIn('id="industry-35"', html)
        self.assertIn("Alpha", html)
        self.assertIn("Machinery", html)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "t.sqlite"
        self.service = RankingService(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_saves_unfiltered_momentum_ranks(self):
        ranked = pd.DataFrame({
            "current_return": [1.0, 0.5],
            "last_month_return": [0.2, 0.8],
            "last_week_return": [0.01, -0.01],
            "current_rank": [1, 2],
            "last_month_rank": [2, 1],
            "rank_change": [1, -1],
        }, index=pd.Index(["AAA", "BBB"], name="ticker"))
        self.service.save_momentum_ranks(ranked, date(2026, 9, 8))
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT ticker, current_rank, rank_change FROM momentum_ranks ORDER BY ticker"
            ).fetchall()
        self.assertEqual(rows, [("AAA", 1, 1), ("BBB", 2, -1)])


if __name__ == "__main__":
    unittest.main()
