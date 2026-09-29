from datetime import date
import unittest
from unittest.mock import patch

from app.config import backtest_assets
from app.db import db_session
from app.services.data_sync import SyncWarning, fetch_tushare_repo_rates, required_data_missing, sync_all
from tests.helpers import build_synced_db


class DataBackfillTests(unittest.TestCase):
    def test_gold_uses_spot_history_between_inception_and_exchange_listing(self):
        path, config = build_synced_db("2013-07-15", "2013-07-31")
        asset = next(asset for asset in config["assets"] if asset["symbol"] == "518880.SH")
        self.assertEqual(asset["inception_date"], "2013-07-18")
        self.assertEqual(asset["trade_start_date"], "2013-07-29")
        with db_session(path) as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM prices WHERE symbol='518880.SH' AND trade_date BETWEEN '2013-07-18' AND '2013-07-26'"
            )]
            self.assertEqual(len(rows), 7)
            for row in rows:
                row["source"] = "test:sge_au9999"
            conn.execute("DELETE FROM prices WHERE symbol='518880.SH' AND trade_date BETWEEN '2013-07-18' AND '2013-07-26'")
            with patch("app.services.data_sync.fetch_cn_fund_prices", side_effect=SyncWarning("not listed")), \
                 patch("app.services.data_sync.fetch_datasrc_market_prices", return_value=[]), \
                 patch("app.services.data_sync.fetch_sohu_prices", return_value=[]), \
                 patch("app.services.data_sync.fetch_eastmoney_prices", return_value=[]), \
                 patch("app.services.data_sync.fetch_price_fallback_rows", return_value=rows) as fallback:
                result = sync_all(conn, "test-token", config["start_date"], config["end_date"],
                                  backtest_assets(config), missing_items=["prices:518880.SH"])
            self.assertEqual(result["inserted"]["prices"], 7)
            self.assertEqual(fallback.call_args.args[2], "2013-07-28")
            self.assertNotIn("prices:518880.SH", required_data_missing(
                conn, config["start_date"], config["end_date"], backtest_assets(config)))

    def test_price_source_failure_does_not_skip_independent_fund_factors(self):
        path, config = build_synced_db("2020-01-02", "2020-01-10")
        with db_session(path) as conn:
            price = dict(conn.execute("SELECT * FROM prices WHERE symbol='512890.SH' AND trade_date='2020-01-06'").fetchone())
            factor = dict(conn.execute("SELECT * FROM adj_factors WHERE symbol='512890.SH' AND trade_date='2020-01-06'").fetchone())
            conn.execute("DELETE FROM prices WHERE symbol='512890.SH' AND trade_date='2020-01-06'")
            conn.execute("DELETE FROM adj_factors WHERE symbol='512890.SH' AND trade_date='2020-01-06'")
            with patch("app.services.data_sync.fetch_cn_fund_prices", side_effect=SyncWarning("no daily quote")), \
                 patch("app.services.data_sync.fetch_datasrc_market_prices", return_value=[]), \
                 patch("app.services.data_sync.fetch_sohu_prices", return_value=[price]), \
                 patch("app.services.data_sync.fetch_eastmoney_prices", return_value=[]), \
                 patch("app.services.data_sync.fetch_adj_factors", return_value=[factor]) as fetch_factors:
                result = sync_all(conn, "test-token", config["start_date"], config["end_date"],
                                  backtest_assets(config), missing_items=["prices:512890.SH", "adj_factors:512890.SH"])
            fetch_factors.assert_called_once_with("test-token", "512890.SH", "2020-01-06", "2020-01-06")
            self.assertEqual(result["inserted"]["adj_factors"], 1)
            self.assertNotIn("adj_factors:512890.SH", required_data_missing(
                conn, config["start_date"], config["end_date"], backtest_assets(config)))

    def test_repo_gap_uses_tushare_when_cached_and_public_sources_are_empty(self):
        path, config = build_synced_db("2018-12-14", "2018-12-25")
        quote = {"ts_code": "204001.SH", "trade_date": "20181214", "open": 2.86,
                 "high": 3.8, "low": 2.7, "close": 3.48, "amount": 64968530.0}
        with db_session(path) as conn:
            conn.execute("DELETE FROM repo_rates WHERE symbol='204001' AND trade_date='2018-12-14'")
            with patch("app.services.data_sync.fetch_datasrc_repo_rates", side_effect=SyncWarning("unavailable")), \
                 patch("app.services.data_sync.fetch_sohu_repo_rates", side_effect=SyncWarning("empty")), \
                 patch("app.services.data_sync.tushare_call", return_value=[quote]) as fetch, \
                 patch("app.services.data_sync.fetch_akshare_repo_rates") as unused:
                result = sync_all(conn, "test-token", config["start_date"], config["end_date"],
                                  backtest_assets(config), missing_items=["repo_rates:204001"])
            self.assertEqual(fetch.call_args.args[1], "repo_daily")
            unused.assert_not_called()
            self.assertEqual(result["inserted"]["repo_rates"], 1)
            self.assertEqual(result["missing_data"], [])
            restored = dict(conn.execute("SELECT * FROM repo_rates WHERE symbol='204001' AND trade_date='2018-12-14'").fetchone())
            self.assertEqual(restored["source"], "tushare:repo_daily")
            self.assertEqual(restored["open_rate"], 2.86)
            self.assertEqual(restored["close_rate"], 3.48)

    def test_tushare_repo_bounds_queries_and_filters_symbol_and_date(self):
        requests = []
        def response(_token, _api, params, _fields):
            requests.append(params)
            valid = {"ts_code": params["ts_code"], "trade_date": params["start_date"],
                     "open": 0, "high": 1, "low": 0, "close": 0.5, "amount": 10}
            return [valid, {**valid, "ts_code": "204007.SH"}, {**valid, "trade_date": "20100101"}]
        with patch("app.services.data_sync.tushare_call", side_effect=response):
            rows = fetch_tushare_repo_rates("test-token", "204001", "2018-01-01", "2021-01-01")
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["symbol"] == "204001" and row["open_rate"] == 0 for row in rows))
        for params in requests:
            self.assertLess((date.fromisoformat(params["end_date"]) - date.fromisoformat(params["start_date"])).days, 365)

    def test_tushare_repo_rejects_unusable_quotes(self):
        quote = {"ts_code": "204001.SH", "trade_date": "20181214", "open": 1,
                 "high": 2, "low": 1, "close": 1.5}
        for rows in ([], [{**quote, "close": None}], [{**quote, "close": float("nan")}]):
            with self.subTest(rows=rows), patch("app.services.data_sync.tushare_call", return_value=rows):
                with self.assertRaises(SyncWarning):
                    fetch_tushare_repo_rates("test-token", "204001", "2018-12-14", "2018-12-14")
