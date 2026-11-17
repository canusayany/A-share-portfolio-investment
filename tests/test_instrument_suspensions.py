from __future__ import annotations

from datetime import date
import json
import unittest
from unittest.mock import patch

from app.db import db_session, insert_many
import app.services.calendar as calendar_module
from app.services.backtest_engine import (
    adjusted_price_and_share_scale_series, configured_share_splits, run_backtest,
)
from app.services.data_sync import (
    missing_date_ranges, missing_edge_date_ranges, missing_tail_date_ranges, price_row,
    required_data_missing, sync_all,
)
from tests.helpers import build_synced_db


class InstrumentSuspensionTests(unittest.TestCase):
    def fixture(self, start="2022-03-28", end="2022-03-30"):
        path, config = build_synced_db(start, end)
        config.update(monthly_spend_cny=0, dip_buy_enabled=False)
        for asset in config["assets"]:
            asset["enabled"] = asset["symbol"] == "513500.SH"
            asset["target_weight"] = 1.0 if asset["enabled"] else 0.0
        asset = next(item for item in config["assets"] if item["symbol"] == "513500.SH")
        with db_session(path) as conn:
            conn.execute("DELETE FROM prices WHERE symbol='513500.SH' AND trade_date='2022-03-29'")
        return path, config, asset

    def test_instrument_halt_does_not_close_the_exchange_or_other_funds(self):
        path, config, asset = self.fixture()
        with db_session(path) as conn:
            exchange_days = calendar_module.market_business_days(conn, "CN", "2022-03-28", "2022-03-30")
            asset_days = calendar_module.asset_business_days(conn, asset, "2022-03-28", "2022-03-30")
            self.assertIn(date(2022, 3, 29), exchange_days)
            self.assertEqual(asset_days, [date(2022, 3, 28), date(2022, 3, 30)])
            self.assertEqual(conn.execute("SELECT is_open FROM trading_calendar WHERE market='CN' AND trade_date='2022-03-29'").fetchone()[0], 1)
            self.assertNotIn("prices:513500.SH", required_data_missing(conn, config["start_date"], config["end_date"], [asset]))
            for helper in (missing_date_ranges, missing_edge_date_ranges, missing_tail_date_ranges):
                self.assertEqual(helper(conn, "prices", "symbol", "513500.SH", "trade_date", "2022-03-29", "2022-03-29"), [])
            other = next(item for item in config["assets"] if item["symbol"] == "510300.SH")
            other["enabled"] = True
            conn.execute("DELETE FROM prices WHERE symbol='510300.SH' AND trade_date='2022-03-29'")
            self.assertEqual(missing_date_ranges(conn, "prices", "symbol", "510300.SH", "trade_date", "2022-03-28", "2022-03-30"), [("2022-03-29", "2022-03-29")])
            self.assertIn("prices:510300.SH", required_data_missing(conn, config["start_date"], config["end_date"], [other]))

    def test_halt_only_price_sync_makes_no_quote_request_or_fake_bar(self):
        path, _config, asset = self.fixture()
        with db_session(path) as conn, patch(
            "app.services.data_sync.fetch_cn_fund_prices", side_effect=AssertionError("a documented halt has no real price")
        ) as fetch, patch(
            "app.services.data_sync.fetch_adj_factors", side_effect=AssertionError("no price repair is needed")
        ) as adjustments:
            result = sync_all(conn, "", "2022-03-29", "2022-03-29", [asset], missing_items=["prices:513500.SH"])
            row = conn.execute("SELECT close FROM prices WHERE symbol='513500.SH' AND trade_date='2022-03-29'").fetchone()
        fetch.assert_not_called()
        adjustments.assert_not_called()
        self.assertIsNone(row)
        self.assertEqual(result["inserted"]["prices"], 0)
        self.assertNotIn("prices:513500.SH", result["missing_data"])

    def test_real_session_after_halt_is_still_required(self):
        path, config, asset = self.fixture()
        with db_session(path) as conn:
            conn.execute("DELETE FROM prices WHERE symbol='513500.SH' AND trade_date='2022-03-30'")
            gaps = missing_date_ranges(conn, "prices", "symbol", "513500.SH", "trade_date", config["start_date"], config["end_date"])
            missing = required_data_missing(conn, config["start_date"], config["end_date"], [asset])
        self.assertEqual(gaps, [("2022-03-30", "2022-03-30")])
        self.assertIn("prices:513500.SH", missing)

    def test_first_listed_session_is_required_without_demanding_prelisting_quotes(self):
        path, _config, asset = self.fixture("2014-01-15", "2014-01-17")
        with db_session(path) as conn:
            self.assertNotIn("prices:513500.SH", required_data_missing(conn, "2013-12-05", "2014-01-17", [asset]))
            conn.execute("DELETE FROM prices WHERE symbol='513500.SH' AND trade_date='2014-01-15'")
            missing = required_data_missing(conn, "2013-12-05", "2014-01-17", [asset])
        self.assertIn("prices:513500.SH", missing)

    def test_official_split_overrides_imprecise_factor_without_fabricating_half_loss(self):
        _path, _config, asset = self.fixture()
        splits = configured_share_splits([asset])["513500.SH"]
        rows = [
            {"trade_date": "2022-03-28", "price": 3.8, "adj_factor": 1.0},
            {"trade_date": "2022-03-30", "price": 1.9, "adj_factor": 1.9848},
        ]
        prices, scales = adjusted_price_and_share_scale_series(rows, splits)
        self.assertEqual(scales["2022-03-30"], 2.0)
        self.assertAlmostEqual(prices["2022-03-30"] / prices["2022-03-28"] - 1, 0.0)

    def test_cached_halt_bar_cannot_be_used_for_execution(self):
        path, config, _asset = self.fixture("2022-03-28", "2022-03-31")
        config["start_date"] = "2022-03-29"
        with db_session(path) as conn:
            conn.execute("INSERT INTO prices(symbol,trade_date,open,high,low,close,adj_close,volume,amount,currency,source) VALUES('513500.SH','2022-03-29',0.001,0.001,0.001,0.001,0.001,0,0,'CNY','test:bad_cached_halt')")
            result = run_backtest(conn, config, include_comparison=False, include_month_analysis=False, include_rolling_analysis=False)
            halt_trades = conn.execute("SELECT COUNT(*) FROM trades WHERE run_id=? AND symbol='513500.SH' AND trade_date='2022-03-29'", (result["run_id"],)).fetchone()[0]
        self.assertEqual(halt_trades, 0)

    def test_512890_split_halt_excludes_cached_proxy_and_continues_at_resume(self):
        start, halt, resumed, end = "2021-10-21", "2021-10-22", "2021-10-25", "2021-10-26"
        path, config = build_synced_db(start, end)
        config.update(monthly_spend_cny=0, dip_buy_enabled=False)
        for asset in config["assets"]:
            asset["enabled"] = asset["symbol"] == "512890.SH"
            asset["target_weight"] = float(asset["enabled"])
        symbol = "512890.SH"
        asset = next(item for item in config["assets"] if item["symbol"] == symbol)
        with db_session(path) as conn:
            conn.execute("DELETE FROM prices WHERE symbol=? AND trade_date=?", (symbol, halt))
            insert_many(conn, "prices", [
                price_row(symbol, start, 1.639, "CNY", "test:real_close"),
                price_row(symbol, resumed, 0.801, "CNY", "test:real_close", open_=0.87),
                price_row(symbol, end, 0.796, "CNY", "test:real_close"),
            ])
            self.assertIn(date.fromisoformat(halt), calendar_module.market_business_days(conn, "CN", start, end))
            self.assertNotIn(date.fromisoformat(halt), calendar_module.asset_business_days(conn, asset, start, end))
            self.assertNotIn(f"prices:{symbol}", required_data_missing(conn, start, end, [asset]))

            insert_many(conn, "prices", [
                price_row(symbol, halt, 1.5352269333894708, "CNY", "csindex:index_perf:splice_scale_0.0001"),
            ])
            self.assertNotIn(f"prices:{symbol}", required_data_missing(conn, start, end, [asset]))
            result = run_backtest(conn, config, include_comparison=False, include_month_analysis=False, include_rolling_analysis=False)
            values = {}
            for row in conn.execute("SELECT trade_date,payload_json FROM portfolio_daily WHERE run_id=?", (result["run_id"],)):
                values[row["trade_date"]] = json.loads(row["payload_json"])["values"].get(symbol, 0)
            self.assertGreater(values[start], 0)
            self.assertEqual(values[halt], values[start])
            self.assertAlmostEqual(values[resumed] / values[halt], 2 * 0.801 / 1.639)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM trades WHERE run_id=? AND symbol=? AND trade_date=?", (result["run_id"], symbol, halt)).fetchone()[0], 0)

            conn.execute("DELETE FROM prices WHERE symbol=? AND trade_date=?", (symbol, resumed))
            self.assertIn(f"prices:{symbol}", required_data_missing(conn, start, end, [asset]))
