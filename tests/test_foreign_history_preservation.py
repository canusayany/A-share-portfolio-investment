from __future__ import annotations

from copy import deepcopy
from contextlib import ExitStack
import unittest
from unittest.mock import patch

from app.db import db_session, insert_many, upsert_assets
from app.services.data_sync import sync_all
from tests.helpers import build_synced_db, fixture_price_series


class ForeignHistoryPreservationTests(unittest.TestCase):
    def assert_tail_repair_preserves_history(self, symbol: str, currency: str, seed: float):
        start, end = "2025-01-02", "2025-01-10"
        path, config = build_synced_db(start, end)
        for asset in config["assets"]:
            asset["enabled"] = asset["symbol"] == symbol
            asset["target_weight"] = 1.0 if asset["enabled"] else 0.0
        selected = next(asset for asset in config["assets"] if asset["symbol"] == symbol)
        original = fixture_price_series(symbol, start, end, currency, seed)
        for row in original:
            row["source"] = "yahoo:query1:chart"
        tail = deepcopy(original[-1])
        tail["source"] = "test:repaired_tail"
        with db_session(path) as conn:
            upsert_assets(conn, [{**selected, "source": "fixture"}])
            conn.execute("DELETE FROM prices WHERE symbol=?", (symbol,))
            insert_many(conn, "prices", original[:-1])
            previous = [dict(row) for row in conn.execute(
                "SELECT * FROM prices WHERE symbol=? ORDER BY trade_date", (symbol,)
            )]
            # All sources are deterministic. The Yahoo chart supplies only the
            # missing tail; the preserved Yahoo prefix must survive CN cleanup.
            with ExitStack() as stack:
                for fetcher in (
                    "fetch_datasrc_market_prices", "fetch_eastmoney_prices", "fetch_tencent_hk_prices",
                    "fetch_eastmoney_us_prices", "fetch_nasdaq_prices", "fetch_stooq_prices",
                    "fetch_yahoo_spark_prices", "fetch_hk_stooq_prices", "fetch_hk_yahoo_spark_prices",
                ):
                    stack.enter_context(patch(f"app.services.data_sync.{fetcher}", return_value=[]))
                fetcher = "fetch_hk_yahoo_prices" if currency == "HKD" else "fetch_yahoo_prices"
                yahoo = stack.enter_context(patch(f"app.services.data_sync.{fetcher}", return_value=[tail]))
                result = sync_all(conn, "", start, end, config["assets"], missing_items=[f"prices:{symbol}"])
            after = [dict(row) for row in conn.execute(
                "SELECT * FROM prices WHERE symbol=? ORDER BY trade_date", (symbol,)
            )]
        yahoo.assert_called_once_with(symbol, end, end, currency)
        self.assertEqual(result["inserted"]["prices"], 1)
        self.assertEqual(after[:-1], previous)
        self.assertEqual(len(after), len(original))
        self.assertEqual(after[0]["trade_date"], start)
        self.assertEqual(after[-1]["trade_date"], end)
        self.assertEqual(after[-1]["source"], "test:repaired_tail")
        self.assertTrue(all(row["source"] == "yahoo:query1:chart" for row in after[:-1]))
        self.assertNotIn(f"prices:{symbol}", result["missing_data"])

    def test_us_tail_repair_retains_genuine_yahoo_prefix(self):
        self.assert_tail_repair_preserves_history("VOO", "USD", 280.0)

    def test_hk_tail_repair_retains_genuine_yahoo_prefix(self):
        self.assert_tail_repair_preserves_history("03195.HK", "HKD", 8.0)
