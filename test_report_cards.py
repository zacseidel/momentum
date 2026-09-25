import re
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import pandas as pd

from report import ReportService


def _stock(ticker: str, cohort: str) -> dict:
    return {
        "ticker": ticker, "name": ticker, "price": "$1.00", "description": "",
        "headlines": [], "chart_uri": "", "ma_dots": "", "cohort": cohort,
        "streak_html": "✨ <strong>New Entrant</strong>",
        "current_return": "10%", "last_week_return": "1%", "rank_change": 0,
    }


class ReportCardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.report = ReportService(Path(self.tmp.name) / "test.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def _render(self, top_picks: dict[str, list[str]]) -> tuple[str, dict]:
        charted = {}

        def fake_enrich(df, cohort, target_dates, as_of=None, chart_tickers=None):
            charted[cohort] = set(chart_tickers)
            return [_stock(t, cohort) for t in df["ticker"]]

        picks = {c: pd.DataFrame({"ticker": t}) for c, t in top_picks.items()}
        with mock.patch.object(self.report, "_enrich_data", side_effect=fake_enrich), \
             mock.patch.object(self.report, "_get_voo_stats", return_value={}), \
             mock.patch.object(self.report, "_get_universe_changes", return_value=[]), \
             mock.patch.object(self.report, "_get_dropped_tickers", return_value=[]):
            html = self.report.generate_html(picks, {}, date(2026, 9, 25))
        return html, charted

    def test_cards_limited_to_chosen_areas_and_deduplicated(self):
        sp500 = [f"S{i}" for i in range(10)]
        html, charted = self._render({
            "megalaggards": ["S1", "L1", "L2"],
            "sp500": sp500,
            "sp400": [f"M{i}" for i in range(10)],
            "rankmom500": ["S0", "S7", "R1"],
            "rankmom400": ["Q1"],
            "megacap": ["L1", "X1"],
            "munger": ["S2", "Z1"],
        })

        card_ids = re.findall(r'<div id="card-([^"]+)"', html)
        expected = ["S0", "S1", "S2", "S3", "S4", "S7", "R1",
                    "L1", "L2", "M0", "M1", "M2", "M3", "M4"]
        self.assertEqual(card_ids, expected)

        # Charts are only drawn for the cohort that owns each card
        self.assertEqual(charted["sp500"], {"S0", "S1", "S2", "S3", "S4"})
        self.assertEqual(charted["megalaggards"], {"L1", "L2"})
        self.assertEqual(charted["megacap"], set())

        # Every link resolves; names without a card anywhere are not linked
        links = set(re.findall(r'href="#(card-[^"]+)"', html))
        self.assertTrue(links <= {f"card-{t}" for t in card_ids})
        for unlinked in ("X1", "Z1", "Q1", "S5", "M5"):
            self.assertNotIn(f"card-{unlinked}", links)
        self.assertEqual(html.count('href="#card-L1"'), 2)  # laggards + mega cap


if __name__ == "__main__":
    unittest.main()
