import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from ranking import RankingService
from report import ReportService


class Munger400Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "market_data.sqlite"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE daily_prices (
                    ticker TEXT,
                    date TEXT,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume INTEGER,
                    PRIMARY KEY (ticker, date)
                )
            """)
        self.ranking = RankingService(self.db_path)
        self.as_of = date(2026, 8, 17)
        self.candidates = pd.DataFrame({
            "symbol": [f"T{i:02d}" for i in range(20)] + ["CASH_USD"],
            "name": [f"Company {i}" for i in range(20)] + ["Cash"],
            "weight": [float(20 - i) for i in range(20)] + [100.0],
        })

    def tearDown(self):
        self.temp_dir.cleanup()

    def _insert_histories(self, signal_tickers):
        dates = pd.bdate_range(end=self.as_of, periods=220)
        rows = []
        rows.extend(
            ("VOO", timestamp.date().isoformat(), 100.0, 100.0, 100.0, 100.0, 1000)
            for timestamp in dates
        )
        for ticker in self.candidates[self.candidates.symbol != "CASH_USD"].symbol:
            closes = [100.0] * len(dates)
            if ticker in signal_tickers:
                closes[-10:] = [90.0] * 9 + [100.0]
            rows.extend(
                (ticker, timestamp.date().isoformat(), close, close, close, close, 1000)
                for timestamp, close in zip(dates, closes)
            )
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO daily_prices VALUES (?, ?, ?, ?, ?, ?, ?)", rows
            )
        return dates

    def test_munger400l_uses_top_weighted_fifteen_percent_and_excludes_cash(self):
        self._insert_histories({"T00", "T03"})

        results = self.ranking.rank_munger400l(self.candidates, self.as_of)

        self.assertEqual(results.ticker.tolist(), ["T00"])
        self.assertEqual(int(results.iloc[0].weight_rank), 1)
        self.assertEqual(results.iloc[0].dip_date, "2026-08-14")

    def test_munger400r_requires_historical_leadership_and_current_recovery(self):
        dates = self._insert_histories({"T00", "T03"})
        observation_date = dates[-20].date().isoformat()
        baseline_date = (dates[-20].date() - timedelta(days=365)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO daily_prices (ticker, date, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        f"T{index:02d}", baseline_date, baseline_close,
                        baseline_close, baseline_close, baseline_close, 1000,
                    )
                    for index in range(20)
                    for baseline_close in [50.0 if index < 3 else 100.0]
                ],
            )

        results = self.ranking.rank_munger400r(
            self.candidates,
            self.as_of,
            [{"observation_date": observation_date, "baseline_date": baseline_date}],
        )

        self.assertEqual(results.ticker.tolist(), ["T00"])
        self.assertEqual(int(results.iloc[0].best_return_rank), 1)
        self.assertEqual(int(results.iloc[0].return_universe_size), 20)
        self.assertEqual(results.iloc[0].best_return_date, observation_date)

    def test_munger400r_rejects_incomplete_cross_section(self):
        dates = self._insert_histories({"T00"})
        observation_date = dates[-20].date().isoformat()
        baseline_date = (dates[-20].date() - timedelta(days=365)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            for index in range(10):
                conn.execute(
                    """
                    INSERT INTO daily_prices (ticker, date, open, high, low, close, volume)
                    VALUES (?, ?, 100, 100, 100, 100, 1000)
                    """,
                    (f"T{index:02d}", baseline_date),
                )

        with self.assertRaisesRegex(RuntimeError, "coverage"):
            self.ranking.rank_munger400r(
                self.candidates,
                self.as_of,
                [{"observation_date": observation_date, "baseline_date": baseline_date}],
            )

    def test_sma_does_not_pull_older_rows_into_the_200_session_window(self):
        market_dates = pd.bdate_range(end=self.as_of, periods=209)
        older_dates = pd.bdate_range(end=market_dates[0] - pd.Timedelta(days=1), periods=200)
        dates = older_dates.append(market_dates[-100:])
        closes = [100.0] * len(dates)
        closes[-10:] = [90.0] * 9 + [100.0]
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO daily_prices VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("VOO", timestamp.date().isoformat(), 100, 100, 100, 100, 1000)
                    for timestamp in market_dates
                ] + [
                    ("SPARSE", timestamp.date().isoformat(), close, close, close, close, 1000)
                    for timestamp, close in zip(dates, closes)
                ],
            )

        results = self.ranking._mean_reversion_signals(["SPARSE"], self.as_of)

        self.assertTrue(results.empty)

    def test_sma_accepts_185_of_the_last_200_market_sessions(self):
        market_dates = pd.bdate_range(end=self.as_of, periods=209)
        omitted = set(market_dates[9:24])
        ticker_dates = [timestamp for timestamp in market_dates if timestamp not in omitted]
        closes = [100.0] * len(ticker_dates)
        closes[-10:] = [90.0] * 9 + [100.0]
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO daily_prices VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("VOO", timestamp.date().isoformat(), 100, 100, 100, 100, 1000)
                    for timestamp in market_dates
                ] + [
                    ("TOLERATED", timestamp.date().isoformat(), close, close, close, close, 1000)
                    for timestamp, close in zip(ticker_dates, closes)
                ],
            )

        results = self.ranking._mean_reversion_signals(["TOLERATED"], self.as_of)

        self.assertEqual(results.ticker.tolist(), ["TOLERATED"])

    def test_persistence_is_independent_and_idempotent(self):
        self._insert_histories({"T00"})
        large = self.ranking.rank_munger400l(self.candidates, self.as_of)

        self.ranking.process_munger400_picks(large, "munger400l", self.as_of)
        self.ranking.process_munger400_picks(large, "munger400l", self.as_of)

        with sqlite3.connect(self.db_path) as conn:
            large_count = conn.execute("SELECT COUNT(*) FROM top10_munger400l").fetchone()[0]
            primary_key = [
                row[1]
                for row in conn.execute("PRAGMA table_info(top10_munger400l)").fetchall()
                if row[5]
            ]
            return_table = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='top10_munger400r'"
            ).fetchone()[0]
        self.assertEqual(large_count, 1)
        self.assertEqual(primary_key, ["ticker", "date"])
        self.assertEqual(return_table, 0)

    def test_report_has_independent_top_level_sections(self):
        report = ReportService(self.db_path)
        empty = "<p>No active signals this week.</p>"
        html = report._render_master_template(
            {
                "munger400l": {"summary": empty, "cards": ""},
                "munger400r": {"summary": empty, "cards": ""},
            },
            self.as_of,
            {"return_1y": "N/A", "return_1w": "N/A"},
            [],
        )

        self.assertIn('id="summary-munger400l"', html)
        self.assertIn('id="summary-munger400r"', html)
        self.assertLess(html.index("Munger400L"), html.index("Munger400R"))
        self.assertLess(html.index("Munger400R"), html.index("Mega Cap Leaders"))


if __name__ == "__main__":
    unittest.main()
