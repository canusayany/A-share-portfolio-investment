from __future__ import annotations

import unittest

from app.config import normalize_config
from app.db import data_status, db_session, init_db, json_dumps, json_loads, rows_to_dicts
from app.services.data_sync import (
    chunk_date_ranges,
    eastmoney_secid,
    from_tushare_date,
    merge_rows_by_trade_date,
    missing_date_ranges,
    parse_sohu_jsonp,
    required_data_missing,
    select_best_datasrc_price_rows,
    sohu_code_and_referer,
    sync_all,
    tushare_date,
    yahoo_period,
)
from tests.helpers import build_synced_db, fixture_dividends, fixture_fx_rates, fixture_price_series, fixture_repo_rates, temp_db_path


class DbAndSyncTests(unittest.TestCase):
    def test_sync_all_reports_missing_data_instead_of_mocking(self) -> None:
        db_path = temp_db_path()
        init_db(db_path)
        cfg = normalize_config({"start_date": "2020-01-01", "end_date": "2020-01-31"})
        with db_session(db_path) as conn:
            result = sync_all(conn, "", cfg["start_date"], cfg["end_date"], cfg["assets"], allow_network=False)
            status = data_status(conn)
        self.assertEqual(result["inserted"]["prices"], 0)
        self.assertTrue(result["warnings"])
        self.assertIn("prices:VOO", result["missing_data"])
        self.assertFalse(status)

    def test_fixture_series_are_business_day_aligned(self) -> None:
        prices = fixture_price_series("X", "2020-01-01", "2020-01-10", "CNY", 1.0)
        fx = fixture_fx_rates("2020-01-01", "2020-01-10")
        repo = fixture_repo_rates("2020-01-01", "2020-01-10")
        dividends = fixture_dividends("X", "2020-01-01", "2020-12-31", "CNY")
        self.assertEqual(len(prices), len(fx))
        self.assertEqual(len(repo), len(fx))
        self.assertGreater(dividends[0]["div_cash"], 0)
        self.assertNotIn("2020-01-04", {row["trade_date"] for row in prices})

    def test_required_data_missing_detects_coverage_gaps(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-02-28")
        with db_session(db_path) as conn:
            self.assertEqual(required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"]), [])
            conn.execute("DELETE FROM repo_rates WHERE symbol='204001'")
            self.assertIn("repo_rates:204001", required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"]))

    def test_required_data_missing_detects_early_core_series_gap(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-02-28")
        cfg["start_date"] = "2005-01-01"
        with db_session(db_path) as conn:
            missing = required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"])
        self.assertIn("prices:000300.SH", missing)
        self.assertIn("fx_rates:USD/CNY", missing)
        self.assertNotIn("repo_rates:204001", missing)

    def test_required_data_missing_needs_one_day_repo_for_reserved_cash(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-02-28")
        with db_session(db_path) as conn:
            conn.execute("UPDATE repo_rates SET symbol='204007' WHERE symbol='204001'")
            missing = required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"], "204007")
        self.assertIn("repo_rates:204001", missing)
        self.assertNotIn("repo_rates:204007", missing)

    def test_missing_date_ranges_only_returns_database_gaps(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-10")
        with db_session(db_path) as conn:
            conn.execute("DELETE FROM prices WHERE symbol='VOO' AND trade_date IN ('2020-01-03','2020-01-06')")
            gaps = missing_date_ranges(conn, "prices", "symbol", "VOO", "trade_date", "2020-01-01", "2020-01-10")
        self.assertEqual(gaps, [("2020-01-03", "2020-01-06")])

    def test_sync_all_keeps_existing_real_rows_when_no_gap(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-10")
        with db_session(db_path) as conn:
            before = conn.execute("SELECT COUNT(*) AS count FROM prices WHERE symbol='VOO'").fetchone()["count"]
            result = sync_all(conn, "", cfg["start_date"], cfg["end_date"], cfg["assets"], allow_network=False)
            after = conn.execute("SELECT COUNT(*) AS count FROM prices WHERE symbol='VOO'").fetchone()["count"]
        self.assertEqual(before, after)
        self.assertEqual(result["inserted"]["prices"], 0)
        self.assertNotIn("prices:VOO", result["missing_data"])

    def test_json_and_row_helpers(self) -> None:
        db_path = temp_db_path()
        init_db(db_path)
        data = {"a": 1, "中文": "ok"}
        self.assertEqual(json_loads(json_dumps(data)), data)
        with db_session(db_path) as conn:
            rows = conn.execute("SELECT 1 AS a, 'x' AS b").fetchall()
            self.assertEqual(rows_to_dicts(rows), [{"a": 1, "b": "x"}])

    def test_data_source_small_helpers(self) -> None:
        self.assertEqual(tushare_date("2026-06-23"), "20260623")
        self.assertEqual(from_tushare_date("20260623"), "2026-06-23")
        self.assertIsNone(from_tushare_date(None))
        self.assertEqual(eastmoney_secid("510300.SH"), "1.510300")
        self.assertEqual(eastmoney_secid("159919.SZ"), "0.159919")
        self.assertGreater(yahoo_period("2020-01-02"), yahoo_period("2020-01-01"))
        self.assertEqual(chunk_date_ranges("2020-01-01", "2020-03-31", 90), [("2020-01-01", "2020-03-30"), ("2020-03-31", "2020-03-31")])
        self.assertEqual(sohu_code_and_referer("000300.SH")[0], "zs_000300")
        self.assertEqual(sohu_code_and_referer("510300.SH")[0], "cn_510300")
        parsed = parse_sohu_jsonp('historySearchHandler([{"status":0,"hq":[["2026-06-01","1","2","0","0%","0.9","2.1","10","20","0%"]]}])')
        self.assertEqual(parsed[0]["hq"][0][0], "2026-06-01")
        selected = select_best_datasrc_price_rows(
            [
                {"trade_date": "2026-06-01", "source": "tdx", "close": 1},
                {"trade_date": "2026-06-01", "source": "akshare", "close": 2},
                {"trade_date": "2026-06-02", "source": "amazingdata", "close": 3},
            ]
        )
        self.assertEqual([row["close"] for row in selected], [2, 3])
        merged = merge_rows_by_trade_date(
            [{"trade_date": "2026-06-02", "close": 20}],
            [{"trade_date": "2026-06-01", "close": 10}, {"trade_date": "2026-06-02", "close": 2}],
        )
        self.assertEqual([row["close"] for row in merged], [10, 20])


if __name__ == "__main__":
    unittest.main()
