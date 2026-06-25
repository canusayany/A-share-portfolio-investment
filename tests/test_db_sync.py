from __future__ import annotations

from datetime import datetime, timezone
import unittest

from app.config import normalize_config
from app.db import connect, data_status, db_session, init_db, json_dumps, json_loads, rows_to_dicts
import app.services.data_sync as data_sync_module
from app.services.data_sync import (
    chunk_date_ranges,
    eastmoney_secid,
    fetch_digrin_dividends,
    fetch_nasdaq_prices,
    from_tushare_date,
    mark_sync_coverage,
    merge_rows_by_trade_date,
    missing_coverage_ranges,
    missing_date_ranges,
    missing_tail_date_ranges,
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

    def test_required_data_missing_detects_end_date_gaps_without_tolerance(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-02-28")
        with db_session(db_path) as conn:
            conn.execute("DELETE FROM prices WHERE symbol='VOO' AND trade_date='2020-02-28'")
            conn.execute("DELETE FROM prices WHERE symbol='000300.SH' AND trade_date='2020-02-28'")
            conn.execute("DELETE FROM fx_rates WHERE pair='USD/CNY' AND trade_date='2020-02-28'")
            conn.execute("DELETE FROM repo_rates WHERE symbol='204001' AND trade_date='2020-02-28'")
            missing = required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"])
        self.assertIn("prices:VOO", missing)
        self.assertIn("prices:000300.SH", missing)
        self.assertIn("fx_rates:USD/CNY", missing)
        self.assertIn("repo_rates:204001", missing)

    def test_us_price_for_today_uses_previous_completed_business_day(self) -> None:
        db_path, cfg = build_synced_db("2026-06-15", "2026-06-25")
        original_datetime = data_sync_module.datetime

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 6, 25, tzinfo=timezone.utc)

        try:
            data_sync_module.datetime = FixedDateTime
            with db_session(db_path) as conn:
                conn.execute("DELETE FROM prices WHERE symbol='VOO' AND trade_date='2026-06-25'")
                missing = required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"])
                conn.execute("DELETE FROM prices WHERE symbol='VOO' AND trade_date='2026-06-24'")
                missing_previous_day = required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"])
        finally:
            data_sync_module.datetime = original_datetime

        self.assertNotIn("prices:VOO", missing)
        self.assertIn("prices:VOO", missing_previous_day)

    def test_cn_series_for_today_before_close_use_previous_completed_business_day(self) -> None:
        db_path, cfg = build_synced_db("2026-06-15", "2026-06-25")
        original_datetime = data_sync_module.datetime

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 6, 25, 2, 0, tzinfo=timezone.utc)

        try:
            data_sync_module.datetime = FixedDateTime
            with db_session(db_path) as conn:
                conn.execute("DELETE FROM prices WHERE symbol IN ('000300.SH','510300.SH') AND trade_date='2026-06-25'")
                conn.execute("DELETE FROM fx_rates WHERE pair='USD/CNY' AND trade_date='2026-06-25'")
                conn.execute("DELETE FROM repo_rates WHERE symbol='204001' AND trade_date='2026-06-25'")
                missing = required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"])
                conn.execute("DELETE FROM prices WHERE symbol IN ('000300.SH','510300.SH') AND trade_date='2026-06-24'")
                conn.execute("DELETE FROM fx_rates WHERE pair='USD/CNY' AND trade_date='2026-06-24'")
                conn.execute("DELETE FROM repo_rates WHERE symbol='204001' AND trade_date='2026-06-24'")
                missing_previous_day = required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"])
        finally:
            data_sync_module.datetime = original_datetime

        self.assertNotIn("prices:000300.SH", missing)
        self.assertNotIn("prices:510300.SH", missing)
        self.assertNotIn("fx_rates:USD/CNY", missing)
        self.assertNotIn("repo_rates:204001", missing)
        self.assertIn("prices:000300.SH", missing_previous_day)
        self.assertIn("prices:510300.SH", missing_previous_day)
        self.assertIn("fx_rates:USD/CNY", missing_previous_day)
        self.assertIn("repo_rates:204001", missing_previous_day)

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

    def test_missing_tail_date_ranges_ignores_historical_internal_holes(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-10")
        with db_session(db_path) as conn:
            conn.execute("DELETE FROM prices WHERE symbol='VOO' AND trade_date='2020-01-03'")
            gaps = missing_tail_date_ranges(conn, "prices", "symbol", "VOO", "trade_date", "2020-01-01", "2020-01-10")
            conn.execute("DELETE FROM prices WHERE symbol='VOO' AND trade_date='2020-01-10'")
            tail_gaps = missing_tail_date_ranges(conn, "prices", "symbol", "VOO", "trade_date", "2020-01-01", "2020-01-10")
        self.assertEqual(gaps, [])
        self.assertEqual(tail_gaps, [("2020-01-10", "2020-01-10")])

    def test_dividend_coverage_ranges_are_merged(self) -> None:
        db_path = temp_db_path()
        init_db(db_path)
        with db_session(db_path) as conn:
            mark_sync_coverage(conn, "dividends", "VOO", "2020-01-01", "2020-01-31", "test")
            mark_sync_coverage(conn, "dividends", "VOO", "2020-02-01", "2020-02-29", "test")
            rows = conn.execute("SELECT start_date, end_date FROM sync_coverage WHERE kind='dividends' AND symbol='VOO'").fetchall()
            gaps = missing_coverage_ranges(conn, "dividends", "VOO", "2020-01-01", "2020-03-31")
        self.assertEqual([dict(row) for row in rows], [{"start_date": "2020-01-01", "end_date": "2020-02-29"}])
        self.assertEqual(gaps, [("2020-03-01", "2020-03-31")])

    def test_sync_all_keeps_existing_real_rows_when_no_gap(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-10")
        with db_session(db_path) as conn:
            before = conn.execute("SELECT COUNT(*) AS count FROM prices WHERE symbol='VOO'").fetchone()["count"]
            result = sync_all(conn, "", cfg["start_date"], cfg["end_date"], cfg["assets"], allow_network=False)
            after = conn.execute("SELECT COUNT(*) AS count FROM prices WHERE symbol='VOO'").fetchone()["count"]
        self.assertEqual(before, after)
        self.assertEqual(result["inserted"]["prices"], 0)
        self.assertNotIn("prices:VOO", result["missing_data"])

    def test_dividend_sync_is_independent_from_price_gaps(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-12-31")
        original_fetch = data_sync_module.fetch_yahoo_dividends

        def fake_fetch_yahoo_dividends(symbol, start, end, currency):
            self.assertEqual(symbol, "VOO")
            return [
                {
                    "symbol": symbol,
                    "ann_date": "2020-06-20",
                    "record_date": "2020-06-20",
                    "ex_date": "2020-06-20",
                    "pay_date": "2020-06-20",
                    "div_cash": 1.23,
                    "currency": currency,
                    "source": "test:yahoo:dividend",
                }
            ]

        try:
            data_sync_module.fetch_yahoo_dividends = fake_fetch_yahoo_dividends
            with db_session(db_path) as conn:
                conn.execute("DELETE FROM fund_dividends WHERE symbol='VOO'")
                conn.execute("DELETE FROM sync_coverage WHERE kind='dividends' AND symbol='VOO'")
                self.assertEqual(missing_date_ranges(conn, "prices", "symbol", "VOO", "trade_date", "2020-01-01", "2020-12-31"), [])
                self.assertTrue(missing_coverage_ranges(conn, "dividends", "VOO", "2020-01-01", "2020-12-31"))
                result = sync_all(conn, "", cfg["start_date"], cfg["end_date"], cfg["assets"])
                dividend_count = conn.execute("SELECT COUNT(*) AS count FROM fund_dividends WHERE symbol='VOO'").fetchone()["count"]
                coverage_count = conn.execute("SELECT COUNT(*) AS count FROM sync_coverage WHERE kind='dividends' AND symbol='VOO'").fetchone()["count"]
        finally:
            data_sync_module.fetch_yahoo_dividends = original_fetch

        self.assertEqual(result["inserted"]["prices"], 0)
        self.assertEqual(result["inserted"]["dividends"], 1)
        self.assertEqual(dividend_count, 1)
        self.assertGreaterEqual(coverage_count, 1)

    def test_digrin_dividend_parser_reads_table_rows(self) -> None:
        original_fetch_text = data_sync_module.fetch_text
        html = """
        <table><tbody>
          <tr>
            <td>2026-03-27</td>
            <td>2026-03-31</td>
            <td>1.8724 USD <span>ignored</span></td>
            <td>594.92 USD</td>
          </tr>
          <tr>
            <td>2025-12-22</td>
            <td>2025-12-24</td>
            <td>1.7710 USD</td>
          </tr>
        </tbody></table>
        """

        try:
            data_sync_module.fetch_text = lambda *_args, **_kwargs: html
            rows = fetch_digrin_dividends("VOO", "2026-01-01", "2026-12-31", "USD")
        finally:
            data_sync_module.fetch_text = original_fetch_text

        self.assertEqual(
            rows,
            [
                {
                    "symbol": "VOO",
                    "ann_date": "2026-03-27",
                    "record_date": "2026-03-27",
                    "ex_date": "2026-03-27",
                    "pay_date": "2026-03-31",
                    "div_cash": 1.8724,
                    "currency": "USD",
                    "source": "digrin:html:dividend",
                }
            ],
        )

    def test_digrin_dividend_parser_allows_empty_requested_range(self) -> None:
        original_fetch_text = data_sync_module.fetch_text
        html = """
        <table><tbody>
          <tr><td>2026-03-27</td><td>2026-03-31</td><td>1.8724 USD</td></tr>
        </tbody></table>
        """

        try:
            data_sync_module.fetch_text = lambda *_args, **_kwargs: html
            rows = fetch_digrin_dividends("VOO", "2026-06-25", "2026-06-25", "USD")
        finally:
            data_sync_module.fetch_text = original_fetch_text

        self.assertEqual(rows, [])

    def test_nasdaq_price_parser_reads_rows(self) -> None:
        original_fetch_text = data_sync_module.fetch_text
        payload = {
            "data": {
                "symbol": "VOO",
                "tradesTable": {
                    "rows": [
                        {
                            "date": "06/24/2026",
                            "close": "$675.69",
                            "volume": "9,676,956",
                            "open": "677.68",
                            "high": "682.07",
                            "low": "673.68",
                        },
                        {
                            "date": "06/23/2026",
                            "close": "676.34",
                            "volume": "17,581,730",
                            "open": "676.355",
                            "high": "681.7288",
                            "low": "675.02",
                        },
                    ]
                },
            }
        }

        try:
            data_sync_module.fetch_text = lambda *_args, **_kwargs: data_sync_module.json.dumps(payload)
            rows = fetch_nasdaq_prices("VOO", "2026-06-23", "2026-06-24", "USD")
        finally:
            data_sync_module.fetch_text = original_fetch_text

        self.assertEqual([row["trade_date"] for row in rows], ["2026-06-23", "2026-06-24"])
        self.assertEqual(rows[0]["close"], 676.34)
        self.assertEqual(rows[1]["volume"], 9676956.0)
        self.assertEqual(rows[1]["source"], "nasdaq:historical")

    def test_targeted_dividend_sync_does_not_fetch_missing_prices(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-12-31")
        original_fetch_prices = data_sync_module.fetch_yahoo_prices
        original_fetch_dividends = data_sync_module.fetch_yahoo_dividends

        def fail_fetch_prices(*_args, **_kwargs):
            raise AssertionError("price fetch should not run for dividend-only sync")

        def fake_fetch_yahoo_dividends(symbol, start, end, currency):
            return [
                {
                    "symbol": symbol,
                    "ann_date": start,
                    "record_date": start,
                    "ex_date": start,
                    "pay_date": start,
                    "div_cash": 1.0,
                    "currency": currency,
                    "source": "test:yahoo:dividend",
                }
            ]

        try:
            data_sync_module.fetch_yahoo_prices = fail_fetch_prices
            data_sync_module.fetch_yahoo_dividends = fake_fetch_yahoo_dividends
            with db_session(db_path) as conn:
                conn.execute("DELETE FROM prices WHERE symbol='VOO' AND trade_date >= '2020-06-01'")
                conn.execute("DELETE FROM fund_dividends WHERE symbol='VOO'")
                conn.execute("DELETE FROM sync_coverage WHERE kind='dividends' AND symbol='VOO'")
                result = sync_all(conn, "", cfg["start_date"], cfg["end_date"], cfg["assets"], missing_items=["dividends:VOO"])
        finally:
            data_sync_module.fetch_yahoo_prices = original_fetch_prices
            data_sync_module.fetch_yahoo_dividends = original_fetch_dividends

        self.assertEqual(result["inserted"]["prices"], 0)
        self.assertEqual(result["inserted"]["dividends"], 1)

    def test_targeted_price_sync_uses_tail_ranges(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-10")
        original_fetch_prices = data_sync_module.fetch_yahoo_prices
        requested_ranges: list[tuple[str, str]] = []

        def fake_fetch_yahoo_prices(symbol, start, end, currency):
            requested_ranges.append((start, end))
            return fixture_price_series(symbol, start, end, currency, 280.0)

        try:
            data_sync_module.fetch_yahoo_prices = fake_fetch_yahoo_prices
            with db_session(db_path) as conn:
                conn.execute("DELETE FROM prices WHERE symbol='VOO' AND trade_date IN ('2020-01-03','2020-01-10')")
                result = sync_all(conn, "", cfg["start_date"], cfg["end_date"], cfg["assets"], missing_items=["prices:VOO"])
        finally:
            data_sync_module.fetch_yahoo_prices = original_fetch_prices

        self.assertEqual(requested_ranges, [("2020-01-10", "2020-01-10")])
        self.assertEqual(result["inserted"]["prices"], 1)
        self.assertNotIn("prices:VOO", result["missing_data"])

    def test_json_and_row_helpers(self) -> None:
        db_path = temp_db_path()
        init_db(db_path)
        data = {"a": 1, "中文": "ok"}
        self.assertEqual(json_loads(json_dumps(data)), data)
        with db_session(db_path) as conn:
            rows = conn.execute("SELECT 1 AS a, 'x' AS b").fetchall()
            self.assertEqual(rows_to_dicts(rows), [{"a": 1, "b": "x"}])

    def test_sqlite_connections_wait_for_busy_writers(self) -> None:
        db_path = temp_db_path()
        init_db(db_path)
        conn = connect(db_path)
        try:
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)
        finally:
            conn.close()

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
