import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from ranking import RankingService


DATE_MAP = {
    "latest_trading": "2026-09-23",
    "minus_1_week": "2026-09-16",
    "minus_3_months": "2026-06-23",
    "minus_6_months": "2026-03-23",
    "minus_1_year": "2025-09-23",
}


class RankMomentumTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "market_data.sqlite"
        sqlite3.connect(self.db_path).close()
        self.ranking = RankingService(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _candidates(tickers):
        return pd.DataFrame({"symbol": tickers, "weight": [1.0] * len(tickers)})

    @staticmethod
    def _prices(returns, r1w=0.01):
        """returns: {ticker: (r3m, r6m, r12m)} relative to a latest close of 100."""
        rows = []
        for ticker, (r3, r6, r12) in returns.items():
            rows.append((ticker, DATE_MAP["latest_trading"], 100.0))
            rows.append((ticker, DATE_MAP["minus_1_week"], 100.0 / (1 + r1w)))
            rows.append((ticker, DATE_MAP["minus_3_months"], 100.0 / (1 + r3)))
            rows.append((ticker, DATE_MAP["minus_6_months"], 100.0 / (1 + r6)))
            rows.append((ticker, DATE_MAP["minus_1_year"], 100.0 / (1 + r12)))
        return pd.DataFrame(rows, columns=["ticker", "date", "close"])

    @staticmethod
    def _steady_returns(count):
        # T00 is best on every period, T(count-1) is worst.
        return {
            f"T{i:02d}": (0.30 - i * 0.01, 0.50 - i * 0.02, 0.90 - i * 0.03)
            for i in range(count)
        }

    def test_picks_ten_best_average_ranks(self):
        returns = self._steady_returns(15)
        picks = self.ranking.rank_rank_momentum(
            self._candidates(list(returns)), self._prices(returns), DATE_MAP
        )

        self.assertEqual(picks["ticker"].tolist(), [f"T{i:02d}" for i in range(10)])
        self.assertEqual(picks["rank"].tolist(), list(range(1, 11)))
        self.assertEqual(picks["avg_rank"].tolist(), [float(i) for i in range(1, 11)])
        self.assertTrue((picks["universe_size"] == 15).all())
        self.assertAlmostEqual(picks.loc[0, "last_week_return"], 0.01)

    def test_average_blends_periods(self):
        returns = self._steady_returns(20)
        # Best 12M return, but worst on 3M and 6M: avg rank (20 + 20 + 1) / 3.
        returns["T00"] = (-0.9, -0.9, 5.0)
        picks = self.ranking.rank_rank_momentum(
            self._candidates(list(returns)), self._prices(returns), DATE_MAP
        )
        self.assertNotIn("T00", picks["ticker"].tolist())

    def test_tie_goes_to_higher_12m_return(self):
        # Each name ranks 1, 2 and 3 once, so all three average 2.0.
        prices = self._prices({
            "X": (0.30, 0.10, 0.20),
            "Y": (0.10, 0.20, 0.30),
            "Z": (0.20, 0.30, 0.10),
        })
        picks = self.ranking.rank_rank_momentum(
            self._candidates(["X", "Y", "Z"]), prices, DATE_MAP
        )
        self.assertTrue((picks["avg_rank"] == 2.0).all())
        self.assertEqual(picks["ticker"].tolist(), ["Y", "X", "Z"])

    def test_skips_missing_history_and_cash(self):
        returns = self._steady_returns(12)
        prices = self._prices(returns)
        # T00 has no 12-month-ago close (e.g. a recent IPO).
        prices = prices[~((prices["ticker"] == "T00") & (prices["date"] == DATE_MAP["minus_1_year"]))]
        picks = self.ranking.rank_rank_momentum(
            self._candidates(list(returns) + ["CASH_USD"]), prices, DATE_MAP
        )
        self.assertNotIn("T00", picks["ticker"].tolist())
        self.assertNotIn("CASH_USD", picks["ticker"].tolist())
        self.assertTrue((picks["universe_size"] == 11).all())

    def test_process_persists_formats_and_tracks_streaks(self):
        returns = self._steady_returns(12)
        candidates = self._candidates(list(returns))
        picks = self.ranking.rank_rank_momentum(candidates, self._prices(returns), DATE_MAP)
        first = self.ranking.process_rank_momentum(picks, "rankmom500", date(2026, 9, 22))
        self.assertEqual(first.loc[0, "return_12m"], "90.0%")
        self.assertEqual(first.loc[0, "avg_rank"], "1.0")
        self.assertEqual(first.loc[0, "rank_3m"], 1)
        self.assertTrue((first["streak"] == 1).all())

        second = self.ranking.process_rank_momentum(picks, "rankmom500", date(2026, 9, 25))
        self.assertTrue((second["streak"] == 2).all())
        self.assertTrue((second["streak_start"] == "2026-09-22").all())

        with sqlite3.connect(self.db_path) as conn:
            saved = conn.execute("SELECT COUNT(*) FROM top10_rankmom500").fetchone()[0]
            other = conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'top10_rankmom400'"
            ).fetchone()
        self.assertEqual(saved, 20)
        self.assertIsNone(other)

    def test_rejects_unknown_cohort(self):
        with self.assertRaises(ValueError):
            self.ranking.process_rank_momentum(pd.DataFrame(), "sp500", date(2026, 9, 25))


if __name__ == "__main__":
    unittest.main()
