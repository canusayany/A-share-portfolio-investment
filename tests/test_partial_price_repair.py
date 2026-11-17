from __future__ import annotations

from contextlib import ExitStack
import sqlite3
import unittest
from unittest.mock import patch

from app.config import normalize_config
from app.db import SCHEMA
from app.services.data_sync import SyncWarning, required_data_missing, sync_all
from tests.helpers import seed_fixture_data


class PartialPriceRepairTests(unittest.TestCase):
    def setUp(self):
        self.config = normalize_config({"start_date": "2020-01-01", "end_date": "2020-01-10"})
        asset = next(asset for asset in self.config["assets"] if asset["symbol"] == "513500.SH")
        asset.update(enabled=True, target_weight=0.5)
        self.config["assets"] = [asset]
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        seed_fixture_data(self.conn, self.config, self.config["start_date"], self.config["end_date"])
        self.conn.execute(
            "UPDATE prices SET open=9.99,high=9.99,low=9.99,close=9.99,adj_close=9.99,source='yahoo:query1:chart' "
            "WHERE symbol='513500.SH' AND trade_date='2020-01-08'"
        )
        self.conn.execute(
            "UPDATE prices SET open=9999,high=9999,low=9999,close=9999,adj_close=9999,source='csindex:index_perf:splice_scale_1' "
            "WHERE symbol='513500.SH' AND trade_date='2020-01-09'"
        )
        self.original = self.rows()

    def tearDown(self):
        self.conn.close()

    def rows(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM prices WHERE symbol='513500.SH' ORDER BY trade_date"
        )]

    def sync(self, *, allow_network=True):
        return sync_all(
            self.conn, "test-token", self.config["start_date"], self.config["end_date"], self.config["assets"],
            allow_network=allow_network, missing_items=["prices:513500.SH"],
        )

    def assert_still_blocked(self, result):
        self.assertIn("prices:513500.SH", result["missing_data"])
        self.assertIn("prices:513500.SH", required_data_missing(
            self.conn, self.config["start_date"], self.config["end_date"], self.config["assets"],
        ))

    def replacement_rows(self, *dates):
        return [{
            **next(row for row in self.original if row["trade_date"] == trade_date),
            "open": 1.08, "high": 1.08, "low": 1.08, "close": 1.08, "adj_close": None,
            "source": "tushare:fund_daily",
        } for trade_date in dates]

    def test_network_disabled_preserves_unreplaced_legacy_rows(self):
        result = self.sync(allow_network=False)
        self.assertEqual(self.rows(), self.original)
        self.assert_still_blocked(result)

    def test_failed_providers_preserve_both_legacy_price_bases(self):
        with ExitStack() as stack:
            for name in ("fetch_cn_fund_prices", "fetch_datasrc_market_prices", "fetch_sohu_prices", "fetch_eastmoney_prices"):
                stack.enter_context(patch(f"app.services.data_sync.{name}", side_effect=SyncWarning("upstream unavailable")))
            result = self.sync()
        self.assertEqual(self.rows(), self.original)
        self.assert_still_blocked(result)

    def test_partial_repair_replaces_one_date_and_preserves_the_other(self):
        with patch("app.services.data_sync.fetch_cn_fund_prices", return_value=self.replacement_rows("2020-01-08")), \
             patch("app.services.data_sync.fetch_datasrc_market_prices", return_value=[]), \
             patch("app.services.data_sync.fetch_sohu_prices", return_value=[]), \
             patch("app.services.data_sync.fetch_eastmoney_prices", return_value=[]):
            result = self.sync()
        after = {row["trade_date"]: row for row in self.rows()}
        self.assertEqual(len(after), len(self.original))
        self.assertEqual(after["2020-01-08"]["source"], "tushare:fund_daily")
        self.assertEqual(after["2020-01-08"]["close"], 1.08)
        for row in self.original:
            if row["trade_date"] != "2020-01-08":
                self.assertEqual(after[row["trade_date"]], row)
        self.assert_still_blocked(result)

    def test_complete_repair_upserts_only_its_dates_and_clears_legacy_gate(self):
        with patch("app.services.data_sync.fetch_cn_fund_prices", return_value=self.replacement_rows("2020-01-08", "2020-01-09")), \
             patch("app.services.data_sync.fetch_datasrc_market_prices", return_value=[]), \
             patch("app.services.data_sync.fetch_sohu_prices", return_value=[]), \
             patch("app.services.data_sync.fetch_eastmoney_prices", return_value=[]):
            result = self.sync()
        after = {row["trade_date"]: row for row in self.rows()}
        self.assertEqual(len(after), len(self.original))
        for row in self.original:
            if row["trade_date"] in {"2020-01-08", "2020-01-09"}:
                self.assertEqual(after[row["trade_date"]]["source"], "tushare:fund_daily")
            else:
                self.assertEqual(after[row["trade_date"]], row)
        self.assertNotIn("prices:513500.SH", result["missing_data"])
        self.assertNotIn("prices:513500.SH", required_data_missing(
            self.conn, self.config["start_date"], self.config["end_date"], self.config["assets"],
        ))


if __name__ == "__main__":
    unittest.main()
