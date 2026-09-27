from datetime import date, datetime, timezone
from threading import Lock
import unittest
from unittest.mock import patch

from app.config import backtest_assets, get_settings
from app.db import db_session, insert_many
from app.main import execute_backtest_request
from app.services.calendar import calendar_missing_years, daterange, market_business_days
from app.services.data_sync import (
    SyncWarning, asset_price_sync_ranges, effective_price_end_for_asset,
    effective_price_end_for_market, fetch_trading_calendar, missing_date_ranges,
    missing_edge_date_ranges, missing_tail_date_ranges, required_data_missing,
    sync_all, sync_trading_calendars,
)
from tests.helpers import build_synced_db, fixture_price_series


class HolidayNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 27, 12, tzinfo=timezone.utc)


def annual_calendar(market, year, closed=()):
    return [
        {"market": market, "trade_date": day.isoformat(),
         "is_open": int(day.weekday() < 5 and day.isoformat() not in closed)}
        for day in daterange(date(year, 1, 1), date(year, 12, 31))
    ]


class MarketCalendarTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("app.services.data_sync.datetime", HolidayNow)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def holiday_db(self):
        path, config = build_synced_db("2026-09-21", "2026-09-24")
        config["end_date"] = "2026-09-27"
        with db_session(path) as conn:
            conn.execute("UPDATE trading_calendar SET is_open=0 WHERE market='CN' AND trade_date='2026-09-25'")
        return path, config

    def test_default_assets_on_mid_autumn_need_no_missing_prices_or_repos(self):
        path, config = self.holiday_db()
        with db_session(path) as conn:
            self.assertEqual(required_data_missing(conn, config["start_date"], config["end_date"], backtest_assets(config)), [])
            self.assertEqual(effective_price_end_for_market("CN", config["end_date"], conn), date(2026, 9, 24))
            # CN holidays must not suppress a real US/HK trading day.
            for market in ("US", "HK"):
                self.assertEqual(effective_price_end_for_market(market, config["end_date"], conn), date(2026, 9, 25))

    def test_real_trading_day_gap_is_rejected_even_with_future_rows(self):
        path, config = self.holiday_db()
        with db_session(path) as conn:
            conn.execute("DELETE FROM prices WHERE symbol IN ('000300.SH','512890.SH','511090.SH','518850.SH') AND trade_date='2026-09-24'")
            conn.execute("DELETE FROM repo_rates WHERE trade_date='2026-09-24'")
            insert_many(conn, "prices", fixture_price_series("000300.SH", "2026-09-28", "2026-09-28", "CNY", 3500))
            missing = required_data_missing(conn, config["start_date"], config["end_date"], backtest_assets(config))
        self.assertTrue({"prices:000300.SH", "prices:512890.SH", "prices:511090.SH", "prices:518850.SH", "repo_rates:204001"}.issubset(missing))

    def test_holiday_only_ranges_do_not_trigger_sync_or_proxy_rebuild(self):
        path, config = self.holiday_db()
        with db_session(path) as conn:
            for helper in (missing_date_ranges, missing_tail_date_ranges, missing_edge_date_ranges):
                self.assertEqual(helper(conn, "repo_rates", "symbol", "204001", "trade_date", "2026-09-24", "2026-09-27"), [])
                self.assertEqual(helper(conn, "prices", "symbol", "MISSING", "trade_date", "2026-09-25", "2026-09-27"), [])
            asset = dict(next(a for a in config["assets"] if a["symbol"] == "512890.SH"))
            asset["trade_start_date"] = "2027-01-04"
            self.assertEqual(asset_price_sync_ranges(conn, asset, "2026-09-21", "2026-09-27", missing_tail_date_ranges), [])

    def test_holiday_backtest_completes_with_original_end_date_and_no_network(self):
        path, config = self.holiday_db()
        with patch("app.main.sync_all", side_effect=AssertionError("no data sync needed")):
            result = execute_backtest_request(get_settings(path), Lock(), config)
        self.assertFalse(result["data_sync"]["triggered"])
        self.assertEqual(config["end_date"], "2026-09-27")
        self.assertEqual(result["summary"]["end_date"], "2026-09-24")

    def test_full_sync_also_ignores_closed_days_for_prices_rates_and_dividends(self):
        path, config = self.holiday_db()
        with db_session(path) as conn:
            result = sync_all(conn, "", config["start_date"], config["end_date"], backtest_assets(config), allow_network=False)
        self.assertEqual(result["missing_data"], [])
        self.assertEqual(sum(result["inserted"].values()), 0)

    def test_new_prices_apply_existing_factor_tail_policy_in_the_same_sync(self):
        path, config = build_synced_db("2026-08-31", "2026-09-01")
        asset = next(a for a in config["assets"] if a["symbol"] == "512890.SH")
        prices = fixture_price_series("512890.SH", "2026-09-02", "2026-09-24", "CNY", 1.0)
        with db_session(path) as conn, \
             patch("app.services.data_sync.fetch_cn_fund_prices", return_value=prices), \
             patch("app.services.data_sync.fetch_datasrc_market_prices", return_value=[]), \
             patch("app.services.data_sync.fetch_sohu_prices", return_value=[]), \
             patch("app.services.data_sync.fetch_eastmoney_prices", return_value=[]), \
             patch("app.services.data_sync.fetch_adj_factors", side_effect=SyncWarning("not published yet")):
            result = sync_all(conn, "token", "2026-08-31", "2026-09-24", [asset], missing_items=["prices:512890.SH"])
            factor = conn.execute("SELECT source FROM adj_factors WHERE symbol='512890.SH' AND trade_date='2026-09-24'").fetchone()
            self.assertEqual(factor["source"], "carry_forward:fixture:adj")
            self.assertEqual(result["inserted"]["adj_factors"], len(prices))
            self.assertNotIn("adj_factors:512890.SH", required_data_missing(conn, "2026-08-31", "2026-09-24", [asset]))

    def test_missing_calendar_is_cached_then_false_gaps_are_discarded(self):
        path, config = self.holiday_db()
        assets = backtest_assets(config)
        with db_session(path) as conn:
            conn.execute("DELETE FROM trading_calendar WHERE market='CN' AND trade_date BETWEEN '2026-01-01' AND '2026-12-31'")
            before = required_data_missing(conn, config["start_date"], config["end_date"], assets)
            self.assertIn("calendar:CN", before)
            self.assertIn("prices:000300.SH", before)
            with patch("app.services.data_sync.fetch_trading_calendar", return_value=annual_calendar("CN", 2026, {"2026-09-25"})) as fetch, \
                 patch("app.services.data_sync.fetch_cn_fund_prices", side_effect=AssertionError("holiday quotes must not be fetched")):
                result = sync_all(conn, "token", config["start_date"], config["end_date"], assets, missing_items=before)
                again = sync_all(conn, "token", config["start_date"], config["end_date"], assets, missing_items=[])
            fetch.assert_called_once_with("token", "CN", 2026)
            self.assertEqual(result["inserted"]["trading_calendar"], 365)
            self.assertEqual(result["inserted"]["prices"], 0)
            self.assertEqual(again["inserted"]["trading_calendar"], 0)
            self.assertEqual(result["missing_data"], [])
            self.assertEqual(required_data_missing(conn, config["start_date"], config["end_date"], assets), [])

    def test_calendar_provider_failure_stays_missing_without_guessing_closures(self):
        path, config = self.holiday_db()
        with db_session(path) as conn:
            conn.execute("DELETE FROM trading_calendar WHERE market='CN'")
            with patch("app.services.data_sync.fetch_trading_calendar", side_effect=SyncWarning("unavailable")) as fetch:
                inserted, warnings, missing = sync_trading_calendars(conn, "token", backtest_assets(config), "2012-01-01", config["end_date"])
            self.assertEqual(inserted, 0)
            self.assertEqual(missing, ["calendar:CN"])
            self.assertEqual(warnings, ["unavailable"])
            self.assertEqual(fetch.call_count, 1)
            self.assertIn("calendar:CN", required_data_missing(conn, config["start_date"], config["end_date"], backtest_assets(config)))

    def test_long_holiday_and_year_boundary_follow_published_sessions(self):
        path, config = build_synced_db("2026-01-01", "2026-01-05")
        with db_session(path) as conn:
            conn.execute("UPDATE trading_calendar SET is_open=0 WHERE market='CN' AND trade_date IN ('2026-01-01','2026-01-02')")
            self.assertEqual(effective_price_end_for_market("CN", "2026-01-04", conn), date(2025, 12, 31))
            for day in daterange(date(2026, 2, 15), date(2026, 2, 23)):
                conn.execute("UPDATE trading_calendar SET is_open=0 WHERE market='CN' AND trade_date=?", (day.isoformat(),))
            self.assertEqual(effective_price_end_for_market("CN", "2026-02-23", conn), date(2026, 2, 13))
            self.assertEqual(effective_price_end_for_market("CN", "2026-02-24", conn), date(2026, 2, 24))
            self.assertEqual(calendar_missing_years(conn, "CN", "2026-12-31", "2027-01-04"), [2027])

    def test_calendar_fetch_validates_complete_year_and_market_endpoint(self):
        raw = [{"cal_date": row["trade_date"].replace("-", ""), "is_open": row["is_open"], "pretrade_date": None}
               for row in annual_calendar("CN", 2026, {"2026-09-25"})]
        for market, api in (("CN", "trade_cal"), ("HK", "hk_tradecal"), ("US", "us_tradecal")):
            with patch("app.services.data_sync.tushare_call", return_value=raw) as call:
                rows = fetch_trading_calendar("token", market, 2026)
            self.assertEqual(len(rows), 365)
            self.assertEqual(call.call_args.args[1], api)
            self.assertEqual(call.call_args.args[2].get("exchange"), "SSE" if market == "CN" else None)
            self.assertEqual(next(r for r in rows if r["trade_date"] == "2026-09-25")["is_open"], 0)
        for invalid in ([], raw[:-1], [{**row, "is_open": 0} for row in raw], [{**raw[0], "is_open": 2}] + raw[1:], raw + [{**raw[0], "is_open": 1 - raw[0]["is_open"]}]):
            with patch("app.services.data_sync.tushare_call", return_value=invalid), self.assertRaises(SyncWarning):
                fetch_trading_calendar("token", "CN", 2026)


if __name__ == "__main__":
    unittest.main()
