from __future__ import annotations

from datetime import date, datetime, timezone
import unittest
from unittest.mock import patch

from app.db import db_session
from app.services.backtest_engine import BacktestError, run_backtest
from app.services.calendar import market_business_days
from app.services.data_sync import effective_price_end_for_market
from tests.helpers import build_synced_db


RANGE_ERROR = "backtest needs at least two reference-market trading days"
MISSING_RATES_ERROR = "missing fx_rates or repo_rates; run data sync first"


class BeforeCnPublication(datetime):
    @classmethod
    def now(cls, tz=None):
        # 2026-10-21 17:02 in Singapore/Shanghai, before the 18:00 cutoff.
        instant = cls(2026, 10, 21, 9, 2, tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


class EmptyBacktestRangeTests(unittest.TestCase):
    def fixture(self, start, end, symbol="513500.SH"):
        path, config = build_synced_db(start, end)
        config["monthly_spend_cny"] = 0
        for asset in config["assets"]:
            asset["enabled"] = asset["symbol"] == symbol
            asset["target_weight"] = 0.2 if asset["enabled"] else 0.0
        return path, config

    def run_engine(self, conn, config):
        return run_backtest(conn, config, persist=False, include_comparison=False,
                            include_month_analysis=False, include_rolling_analysis=False)

    def assert_range_error(self, conn, config):
        with self.assertRaises(BacktestError) as raised:
            self.run_engine(conn, config)
        self.assertEqual(str(raised.exception), RANGE_ERROR)
        self.assertNotIn("fx_rates or repo_rates", str(raised.exception))

    def test_closed_weekend_is_an_insufficient_range_without_suggesting_rate_sync(self):
        path, config = self.fixture("2025-01-04", "2025-01-05")
        with db_session(path) as conn:
            self.assertEqual(market_business_days(conn, "CN", config["start_date"], config["end_date"]), [])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM repo_rates").fetchone()[0], 0)
            self.assert_range_error(conn, config)

    def test_one_published_session_has_range_error_before_missing_foreign_fx(self):
        path, config = self.fixture("2025-01-03", "2025-01-03", "VOO")
        with db_session(path) as conn:
            conn.execute("DELETE FROM fx_rates")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM prices WHERE symbol='000300.SH'").fetchone()[0], 1)
            self.assert_range_error(conn, config)

    def test_unpublished_current_day_and_future_window_have_range_error(self):
        # Only the completed October 20 session is in the cache. Trading
        # calendars may include future open dates without any published bars.
        for start, end in (("2026-10-21", "2026-10-21"), ("2026-10-22", "2026-10-23")):
            with self.subTest(start=start, end=end):
                path, config = self.fixture("2026-10-20", "2026-10-20", "VOO")
                config.update(start_date=start, end_date=end)
                with db_session(path) as conn, patch("app.services.data_sync.datetime", BeforeCnPublication):
                    self.assertEqual(effective_price_end_for_market("CN", end, conn), date(2026, 10, 20))
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM prices WHERE symbol='000300.SH' AND trade_date BETWEEN ? AND ?", (start, end)).fetchone()[0], 0)
                    self.assert_range_error(conn, config)

    def test_two_reference_sessions_still_report_missing_required_fx(self):
        path, config = self.fixture("2025-01-02", "2025-01-03", "VOO")
        with db_session(path) as conn:
            conn.execute("DELETE FROM fx_rates WHERE pair='USD/CNY'")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM prices WHERE symbol='000300.SH'").fetchone()[0], 2)
            with self.assertRaises(BacktestError) as raised:
                self.run_engine(conn, config)
        self.assertEqual(str(raised.exception), MISSING_RATES_ERROR)

    def test_two_reference_sessions_still_report_missing_repo_rates(self):
        path, config = self.fixture("2025-01-02", "2025-01-03")
        with db_session(path) as conn:
            conn.execute("DELETE FROM repo_rates")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM prices WHERE symbol='000300.SH'").fetchone()[0], 2)
            with self.assertRaises(BacktestError) as raised:
                self.run_engine(conn, config)
        self.assertEqual(str(raised.exception), MISSING_RATES_ERROR)

    def test_two_complete_sessions_can_still_run(self):
        path, config = self.fixture("2025-01-02", "2025-01-03", "VOO")
        with db_session(path) as conn:
            result = self.run_engine(conn, config)
        self.assertEqual(result["summary"]["start_date"], "2025-01-02")
        self.assertEqual(result["summary"]["end_date"], "2025-01-03")
