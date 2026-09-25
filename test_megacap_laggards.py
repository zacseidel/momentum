import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from ranking import RankingService


DATE_MAP = {
    "latest_trading": "2026-09-23",
    "minus_3_months": "2026-06-23",
    "minus_6_months": "2026-03-23",
    "minus_1_year": "2025-09-23",
}


class MegacapLaggardsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "market_data.sqlite"
        sqlite3.connect(self.db_path).close()
        self.ranking = RankingService(self.db_path)
        # 12 candidates by weight; only the top 10 (T00-T09) should be ranked.
        self.candidates = pd.DataFrame({
            "symbol": [f"T{i:02d}" for i in range(12)],
            "name": [f"Company {i}" for i in range(12)],
            "weight": [float(12 - i) for i in range(12)],
        })

    def tearDown(self):
        self.temp_dir.cleanup()

    def _prices(self, returns):
        """returns: {ticker: (r3m, r6m, r12m)} relative to a latest close of 100."""
        rows = []
        for ticker, (r3, r6, r12) in returns.items():
            rows.append((ticker, DATE_MAP["latest_trading"], 100.0))
            rows.append((ticker, DATE_MAP["minus_3_months"], 100.0 / (1 + r3)))
            rows.append((ticker, DATE_MAP["minus_6_months"], 100.0 / (1 + r6)))
            rows.append((ticker, DATE_MAP["minus_1_year"], 100.0 / (1 + r12)))
        return pd.DataFrame(rows, columns=["ticker", "date", "close"])

    def test_picks_worst_average_rank_among_top_ten(self):
        # T00..T09 get steadily worse returns, so T09, T08, T07 are the laggards.
        returns = {f"T{i:02d}": (0.10 - i * 0.01, 0.20 - i * 0.02, 0.30 - i * 0.03) for i in range(10)}
        # T10/T11 are outside the top 10 and are terrible; they must be ignored.
        returns["T10"] = (-0.5, -0.5, -0.5)
        returns["T11"] = (-0.5, -0.5, -0.5)

        picks = self.ranking.rank_megacap_laggards(
            self.candidates, self._prices(returns), DATE_MAP
        )

        self.assertEqual(picks["ticker"].tolist(), ["T09", "T08", "T07"])
        self.assertEqual(picks["rank"].tolist(), [1, 2, 3])
        self.assertEqual(picks["avg_rank"].tolist(), [10.0, 9.0, 8.0])
        self.assertTrue((picks["universe_size"] == 10).all())
        self.assertAlmostEqual(picks.loc[0, "return_12m"], 0.03)

    def test_average_blends_periods(self):
        returns = {f"T{i:02d}": (0.10 - i * 0.01, 0.20 - i * 0.02, 0.30 - i * 0.03) for i in range(10)}
        # T00 is best on 6M/12M but worst on 3M: avg rank (10 + 1 + 1) / 3 = 4.
        returns["T00"] = (-0.9, 0.9, 0.9)

        picks = self.ranking.rank_megacap_laggards(
            self.candidates, self._prices(returns), DATE_MAP
        )
        self.assertNotIn("T00", picks["ticker"].tolist())

    def test_process_persists_and_formats(self):
        returns = {f"T{i:02d}": (0.10 - i * 0.01, 0.20 - i * 0.02, 0.30 - i * 0.03) for i in range(10)}
        picks = self.ranking.rank_megacap_laggards(
            self.candidates, self._prices(returns), DATE_MAP
        )
        display = self.ranking.process_megacap_laggards(picks, date(2026, 9, 24))

        self.assertEqual(display.loc[0, "return_12m"], "3.0%")
        self.assertEqual(display.loc[0, "avg_rank"], "10.0")
        with sqlite3.connect(self.db_path) as conn:
            saved = conn.execute("SELECT COUNT(*) FROM top10_megalaggards").fetchone()[0]
        self.assertEqual(saved, 3)


if __name__ == "__main__":
    unittest.main()
