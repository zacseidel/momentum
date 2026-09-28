import unittest

import numpy as np
import pandas as pd

from ranking import ISOLATED_STALL_MAX, STALL_SESSIONS, STALL_WINDOW, assess_stalls, sessions_since_high
from report import ReportService


def _closes(peaks: dict[str, int], window: int = STALL_WINDOW) -> pd.DataFrame:
    """One column per ticker, rising to a peak `sessions_ago` sessions before the end, then flat below it."""
    data = {}
    for ticker, sessions_ago in peaks.items():
        peak_at = window - 1 - sessions_ago
        values = np.arange(window, dtype=float) + 100
        values[peak_at + 1:] = values[peak_at] - 1
        data[ticker] = values
    return pd.DataFrame(data, index=[f"d{i}" for i in range(window)])


class SessionsSinceHighTests(unittest.TestCase):
    def test_counts_sessions_since_highest_close(self):
        since = sessions_since_high(_closes({"NEW": 0, "OLD": 20}))
        self.assertEqual(since["NEW"], 0)
        self.assertEqual(since["OLD"], 20)

    def test_thin_or_stale_history_is_unmeasured(self):
        closes = _closes({"GAPPY": 3, "STALE": 3})
        closes.iloc[:10, 0] = np.nan          # 50/60 observations < 90%
        closes.iloc[-1, 1] = np.nan           # no latest close
        since = sessions_since_high(closes)
        self.assertTrue(np.isnan(since["GAPPY"]))
        self.assertTrue(np.isnan(since["STALE"]))


class AssessStallsTests(unittest.TestCase):
    def setUp(self):
        self.stalled = STALL_SESSIONS + 5
        self.picks = [f"P{i}" for i in range(10)]

    def _since(self, stalled_names, extra=()):
        names = self.picks + list(extra)
        return pd.Series({t: self.stalled if t in stalled_names else 2 for t in names}, dtype=float)

    def test_isolated_stall_flags_and_swaps_next_unstalled_pick(self):
        since = self._since({"P1", "P5"})
        flags, ctx = assess_stalls(self.picks, self.picks, since)
        self.assertEqual(flags, {"P1": (self.stalled, "isolated"), "P5": (self.stalled, "isolated")})
        self.assertTrue(ctx["isolated"])
        self.assertEqual(ctx["swaps"], [("P1", "P6")])   # P5 is stalled too, so P6 is next in line

    def test_threshold_is_inclusive(self):
        stalled = {f"P{i}" for i in range(ISOLATED_STALL_MAX)}
        _, ctx = assess_stalls(self.picks, self.picks, self._since(stalled))
        self.assertTrue(ctx["isolated"])

    def test_broad_stall_flags_without_swaps(self):
        stalled = {f"P{i}" for i in range(ISOLATED_STALL_MAX + 1)}
        flags, ctx = assess_stalls(self.picks, self.picks, self._since(stalled))
        self.assertEqual({k for k, (_, kind) in flags.items() if kind == "broad"}, stalled)
        self.assertFalse(ctx["isolated"])
        self.assertEqual(ctx["swaps"], [])

    def test_breadth_uses_unfiltered_top10_not_picks(self):
        others = [f"X{i}" for i in range(ISOLATED_STALL_MAX)]
        top10 = others + ["P0"] + self.picks[1:10 - len(others)]
        since = self._since(set(others) | {"P0"}, extra=others)
        flags, ctx = assess_stalls(self.picks, top10, since)
        self.assertEqual(ctx["stalled"], others + ["P0"])
        self.assertEqual(flags, {"P0": (self.stalled, "broad")})

    def test_unmeasured_names_do_not_count(self):
        since = self._since({"P0"})
        since["P9"] = np.nan
        _, ctx = assess_stalls(self.picks, self.picks, since)
        self.assertEqual(ctx["measured"], 9)


class StallRenderingTests(unittest.TestCase):
    def test_badges(self):
        iso = ReportService._render_stall_badge({"stall": "isolated", "stall_sessions": 18.0})
        broad = ReportService._render_stall_badge({"stall": "broad", "stall_sessions": 22})
        self.assertIn("isolated stall 18d", iso)
        self.assertIn("stalled 22d", broad)
        self.assertEqual(ReportService._render_stall_badge({"stall": ""}), "")
        self.assertEqual(ReportService._render_stall_badge({}), "")

    def test_summary_mentions_swap_when_isolated(self):
        html = ReportService._render_stall_summary(
            {"stalled": ["AMD"], "measured": 10, "isolated": True, "swaps": [("AMD", "CRWD")]}
        )
        self.assertIn("1 of the top 10", html)
        self.assertIn("AMD → CRWD", html)

    def test_summary_calls_broad_stalls_a_pullback(self):
        html = ReportService._render_stall_summary(
            {"stalled": ["A", "B", "C", "D"], "measured": 10, "isolated": False, "swaps": []}
        )
        self.assertIn("broad", html)
        self.assertNotIn("swap", html)


if __name__ == "__main__":
    unittest.main()
