from __future__ import annotations

from datetime import date
import sqlite3
import unittest
from unittest.mock import patch

from app.config import normalize_config
from app.db import SCHEMA, insert_many
from app.services import backtest_engine as engine
from app.services.data_sync import mark_sync_coverage, required_data_missing
from tests.helpers import fixture_repo_rates, seed_fixture_data


class VerifiedRepoNoQuoteExecutionTests(unittest.TestCase):
    def setUp(self):
        self.config = normalize_config({
            "start_date": "2010-01-06", "end_date": "2010-01-11", "repo_symbol": "204014",
            "initial_capital_cny": 100_000, "monthly_spend_cny": 0,
            "rebalance_frequency": "yearly", "rebalance_month_analysis_enabled": False,
            "fees": {"repo": {"investor_commission_rate": 0}},
        })
        for asset in self.config["assets"]:
            asset.update(enabled=False, target_weight=0.0)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        seed_fixture_data(self.conn, self.config, self.config["start_date"], self.config["end_date"])
        insert_many(self.conn, "repo_rates", fixture_repo_rates("2010-01-06", "2010-01-11", "204001"))
        for day, rate in (("2010-01-06", 10), ("2010-01-08", 12), ("2010-01-11", 13)):
            self.conn.execute(
                "UPDATE repo_rates SET open_rate=?,high_rate=?,low_rate=?,close_rate=? WHERE symbol='204014' AND trade_date=?",
                (rate, rate, rate, rate, day),
            )
        for day, rate in (("2010-01-06", 1), ("2010-01-07", 2), ("2010-01-08", 3), ("2010-01-11", 4)):
            self.conn.execute(
                "UPDATE repo_rates SET open_rate=?,high_rate=?,low_rate=?,close_rate=? WHERE symbol='204001' AND trade_date=?",
                (rate, rate, rate, rate, day),
            )
        self.conn.execute("DELETE FROM repo_rates WHERE symbol='204014' AND trade_date='2010-01-07'")

    def tearDown(self):
        self.conn.close()

    def mark_absence(self, day="2010-01-07"):
        mark_sync_coverage(
            self.conn, "repo_no_quote", "204014", day, day, "tushare:repo_daily:market_snapshot",
        )

    def run_observed(self):
        observed = []
        original = engine._invest_repo_cash

        def observe(state, day, selected, overnight, *args, **kwargs):
            observed.append((day.isoformat(), selected, overnight))
            return original(state, day, selected, overnight, *args, **kwargs)

        with patch("app.services.backtest_engine._invest_repo_cash", side_effect=observe):
            result = engine.run_backtest(
                self.conn, self.config, include_comparison=True,
                include_month_analysis=False, include_rolling_analysis=False,
            )
        return result, observed

    def test_main_and_comparison_reset_verified_absence_and_resume_next_real_quote(self):
        self.mark_absence()
        result, observed = self.run_observed()
        # Each session is visited once by the main loop and once by comparison.
        self.assertEqual(observed.count(("2010-01-06", 10, 1)), 2)
        self.assertEqual(observed.count(("2010-01-07", None, 2)), 2)
        self.assertEqual(observed.count(("2010-01-08", 12, 3)), 2)
        self.assertEqual(observed.count(("2010-01-11", 13, 4)), 2)
        self.assertEqual(result["summary"]["engine_version"], engine.BACKTEST_ENGINE_VERSION)

        # GC001 quotes 1%, 2%, 3% matured. The last loan is still outstanding.
        benchmark_nav = 1 + (2.78 + 5.56 + 8.33) / 100_000
        years = 5 / 365.25
        self.assertAlmostEqual(result["summary"]["repo_annualized_return"], benchmark_nav ** (1 / years) - 1)

    def test_verified_absence_invests_new_cash_only_at_real_overnight_rate(self):
        self.mark_absence()
        _result, observed = self.run_observed()
        _, selected, overnight = next(row for row in observed if row[0] == "2010-01-07")
        state = engine.PortfolioState(cash_cny=100_000)
        day = date(2010, 1, 7)
        engine._invest_repo_cash(state, day, selected, overnight, self.config["fees"], 14, set(), 0)
        self.assertEqual(len(state.repo_lots), 1)
        self.assertEqual(state.repo_lots[0].actual_days, 1)
        self.assertEqual(state.repo_lots[0].interest, round(100_000 * 0.02 / 360, 2))

    def test_an_unmarked_gap_still_fails_preflight_and_is_not_classified_as_verified_absence(self):
        self.assertIn("repo_rates:204014", required_data_missing(
            self.conn, self.config["start_date"], self.config["end_date"], self.config["assets"], "204014",
        ))
        result, observed = self.run_observed()
        self.assertEqual(sum(day == "2010-01-07" for day, _, _ in observed), 1)
        self.assertIn("repo_rates:204014", result["summary"]["data_coverage"]["comparison"]["gaps"])

    def test_actual_selected_quote_overrides_a_stale_absence_marker(self):
        self.mark_absence()
        self.mark_absence("2010-01-08")
        _result, observed = self.run_observed()
        self.assertEqual(observed.count(("2010-01-08", 12, 3)), 2)

    def test_window_without_selected_quotes_uses_existing_overnight_fallback(self):
        self.conn.execute("DELETE FROM repo_rates WHERE symbol='204014'")
        result, observed = self.run_observed()
        self.assertTrue(observed)
        self.assertTrue(all(selected is None for _day, selected, _overnight in observed))
        self.assertEqual(observed.count(("2010-01-07", None, 2)), 1)
        self.assertGreater(result["summary"]["final_asset_cny"], self.config["initial_capital_cny"])


if __name__ == "__main__":
    unittest.main()
