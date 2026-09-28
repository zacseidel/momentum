import asyncio
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd

os.environ.setdefault("POLYGON_API_KEY", "test-key")

import prices
from prices import (
    PriceService,
    drop_before_last_break,
    find_level_breaks,
    find_split_contaminated,
)


def _series(ticker, closes):
    dates = pd.bdate_range("2026-01-01", periods=len(closes))
    return pd.DataFrame({
        "ticker": ticker,
        "date": [d.date().isoformat() for d in dates],
        "close": closes,
    })


class FlipDetectionTests(unittest.TestCase):
    def test_flags_series_mixing_two_price_scales(self):
        mixed = _series("KLAC", [100, 101, 10.2, 102, 10.1, 10.3, 103, 104])
        self.assertEqual(find_split_contaminated(mixed), ["KLAC"])

    def test_ignores_clean_split_and_real_gaps(self):
        clean_split = _series("SPLT", [100, 101, 50.5, 51, 52, 51.5])
        earnings_gap = _series("GAP", [100, 140, 141, 139, 100, 101])
        frame = pd.concat([clean_split, earnings_gap])
        self.assertEqual(find_split_contaminated(frame), [])


class LevelBreakTests(unittest.TestCase):
    def test_reused_ticker_is_flagged_and_truncated(self):
        old = _series("BNY", [10.0, 10.1, 10.2])
        new = pd.DataFrame({
            "ticker": "BNY",
            "date": ["2026-05-21", "2026-05-22", "2026-05-26"],
            "close": [139.0, 139.2, 141.0],
        })
        series = pd.concat([old, new])
        self.assertEqual(find_level_breaks(series), ["BNY"])
        kept = drop_before_last_break(series)
        self.assertEqual(kept["date"].tolist(), ["2026-05-21", "2026-05-22", "2026-05-26"])

    def test_small_move_across_gap_and_big_move_without_gap_are_ignored(self):
        rejoin = pd.DataFrame({
            "ticker": "RJN", "date": ["2026-01-02", "2026-03-02"], "close": [100.0, 120.0],
        })
        spike = _series("SPK", [100.0, 180.0, 181.0])
        frame = pd.concat([rejoin, spike])
        self.assertEqual(find_level_breaks(frame), [])
        self.assertEqual(len(drop_before_last_break(rejoin)), 2)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.addCleanup(self.loop.close)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "market_data.sqlite"
        patcher = mock.patch.object(prices, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(PriceService, "_load_universe_tickers", return_value={"VOO"}):
            self.service = PriceService()
        rows = [("OLD", "2024-01-02", 1, 1, 1, 500.0, 1)]  # stale pre-window row
        rows += [("OLD", d, 1, 1, 1, c, 1) for d, c in
                 [("2026-01-02", 100.0), ("2026-01-05", 10.0), ("2026-01-06", 100.0),
                  ("2026-01-07", 10.0), ("2026-01-08", 100.0)]]
        rows += [("KEEP", "2026-01-02", 1, 1, 1, 50.0, 1)]
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany("INSERT INTO daily_prices VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run(self, responses):
        calls = []

        async def fake_get(client, url):
            calls.append(url)
            return responses.pop(0)

        with mock.patch.object(self.service, "_rate_limited_get", side_effect=fake_get):
            repaired = self.loop.run_until_complete(
                self.service.repair_split_adjustments(date(2026, 1, 9))
            )
        return repaired, calls

    def _closes(self, ticker):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT date, close FROM daily_prices WHERE ticker = ? ORDER BY date", (ticker,)
            ).fetchall()

    def test_replaces_whole_history_and_advances_checkpoint(self):
        ms = lambda d: int(pd.Timestamp(d).timestamp() * 1000)
        refetch = {"results": [
            {"t": ms("2026-01-02"), "o": 1, "h": 1, "l": 1, "c": 10.0, "v": 1},
            {"t": ms("2026-01-05"), "o": 1, "h": 1, "l": 1, "c": 10.0, "v": 1},
        ]}
        repaired, calls = self._run([
            FakeResponse({"results": [{"ticker": "NOTCACHED", "execution_date": "2026-01-05"}]}),
            FakeResponse(refetch),
        ])

        self.assertEqual(repaired, ["OLD"])
        self.assertIn("/splits", calls[0])
        self.assertIn("/ticker/OLD/range/", calls[1])
        self.assertEqual(self._closes("OLD"), [("2026-01-02", 10.0), ("2026-01-05", 10.0)])
        self.assertEqual(self._closes("KEEP"), [("2026-01-02", 50.0)])
        with sqlite3.connect(self.db_path) as conn:
            checkpoint = conn.execute(
                "SELECT value FROM sync_state WHERE key = 'splits_checked_through'"
            ).fetchone()[0]
        self.assertEqual(checkpoint, "2026-01-09")

    def _reused_ticker_rows(self):
        old = [("BNY", d, 1, 1, 1, 10.0, 1) for d in ("2025-12-01", "2025-12-02")]
        new = [("BNY", d, 1, 1, 1, c, 1) for d, c in (("2026-01-05", 139.0), ("2026-01-06", 140.0))]
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany("INSERT INTO daily_prices VALUES (?, ?, ?, ?, ?, ?, ?)", old + new)

    def test_truncation_sets_floor_that_blocks_old_rows(self):
        ms = lambda d: int(pd.Timestamp(d).timestamp() * 1000)
        bar = lambda d, c: {"t": ms(d), "o": 1, "h": 1, "l": 1, "c": c, "v": 1}
        self._reused_ticker_rows()
        refetch = {"results": [bar("2025-12-01", 10.0), bar("2025-12-02", 10.0),
                               bar("2026-01-05", 139.0), bar("2026-01-06", 140.0)]}
        repaired, _ = self._run([
            FakeResponse({"results": [{"ticker": "BNY", "execution_date": "2026-01-05"}]}),
            FakeResponse(refetch),                       # BNY: split-feed hit, truncated
            FakeResponse({"results": []}),               # OLD flip refetch: nothing
        ])
        self.assertIn("BNY", repaired)
        self.assertEqual([d for d, _ in self._closes("BNY")], ["2026-01-05", "2026-01-06"])

        # A grouped snapshot of an old date must not restore the old security
        self.service.valid_tickers = {"BNY"}
        self.service._save_to_db([{"T": "BNY", "o": 1, "h": 1, "l": 1, "c": 10.1, "v": 1}], "2025-12-03")
        self.service._save_to_db([{"T": "BNY", "o": 1, "h": 1, "l": 1, "c": 141.0, "v": 1}], "2026-01-07")
        self.assertEqual([d for d, _ in self._closes("BNY")], ["2026-01-05", "2026-01-06", "2026-01-07"])

    def test_restored_rows_of_truncated_ticker_are_cut_without_refetch(self):
        self._reused_ticker_rows()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO price_repairs VALUES ('BNY', '2026-01-08', 'gap+truncated', 2)")
        repaired, calls = self._run([
            FakeResponse({"results": []}),               # split feed
            FakeResponse({"results": []}),               # OLD flip refetch: nothing
        ])
        self.assertNotIn("BNY", repaired)
        self.assertFalse(any("/ticker/BNY/" in url for url in calls))
        self.assertEqual([d for d, _ in self._closes("BNY")], ["2026-01-05", "2026-01-06"])
        with sqlite3.connect(self.db_path) as conn:
            floor = conn.execute("SELECT floor_date FROM history_floors WHERE ticker='BNY'").fetchone()[0]
        self.assertEqual(floor, "2026-01-05")

    def test_failed_split_repair_keeps_data_and_checkpoint(self):
        repaired, _ = self._run([
            FakeResponse({"results": [{"ticker": "KEEP", "execution_date": "2026-01-05"}]}),
            FakeResponse({}, status_code=500),  # KEEP refetch fails
            FakeResponse({"results": []}),        # OLD refetch returns nothing
        ])

        self.assertEqual(repaired, [])
        self.assertEqual(self._closes("KEEP"), [("2026-01-02", 50.0)])
        self.assertEqual(len(self._closes("OLD")), 6)
        with sqlite3.connect(self.db_path) as conn:
            checkpoint = conn.execute("SELECT value FROM sync_state").fetchone()
        self.assertIsNone(checkpoint)


if __name__ == "__main__":
    unittest.main()
