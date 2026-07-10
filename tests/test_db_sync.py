from __future__ import annotations

from datetime import datetime, timezone
import unittest

from app.config import normalize_config
from app.db import connect, data_status, db_session, init_db, json_dumps, json_loads, rows_to_dicts
import app.services.data_sync as data_sync_module
from app.services.data_sync import (
    chunk_date_ranges,
    eastmoney_secid,
    fetch_currency_api_fx_rates,
    fetch_digrin_dividends,
    fetch_hk_yahoo_prices,
    fetch_nasdaq_prices,
    fetch_tencent_hk_prices,
    fetch_yahoo_prices,
    fetch_yahoo_spark_prices,
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
    stooq_hk_symbol,
    sync_all,
    tencent_hk_symbol,
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

    def test_required_data_missing_needs_hkd_fx_only_when_hk_asset_enabled(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-02-28")
        with db_session(db_path) as conn:
            conn.execute("DELETE FROM fx_rates WHERE pair='HKD/CNY'")
            self.assertNotIn("fx_rates:HKD/CNY", required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"]))
            next(asset for asset in cfg["assets"] if asset["symbol"] == "03195.HK")["enabled"] = True
            missing = required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"])

        self.assertIn("fx_rates:HKD/CNY", missing)

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

    def test_required_data_missing_accepts_weekend_end_date(self) -> None:
        db_path, cfg = build_synced_db("2023-06-01", "2023-06-25")
        with db_session(db_path) as conn:
            missing = required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"])
            conn.execute("DELETE FROM prices WHERE symbol='000300.SH' AND trade_date='2023-06-23'")
            conn.execute("DELETE FROM repo_rates WHERE symbol='204001' AND trade_date='2023-06-23'")
            missing_previous_weekday = required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"])

        self.assertNotIn("prices:000300.SH", missing)
        self.assertNotIn("repo_rates:204001", missing)
        self.assertIn("prices:000300.SH", missing_previous_weekday)
        self.assertIn("repo_rates:204001", missing_previous_weekday)

    def test_required_data_missing_accepts_holiday_end_gap_when_later_rows_exist(self) -> None:
        db_path, cfg = build_synced_db("2023-06-01", "2023-06-30")
        cfg["end_date"] = "2023-06-25"
        with db_session(db_path) as conn:
            conn.execute("DELETE FROM prices WHERE symbol='000300.SH' AND trade_date='2023-06-23'")
            conn.execute("DELETE FROM repo_rates WHERE symbol='204001' AND trade_date='2023-06-23'")
            missing = required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"])

        self.assertNotIn("prices:000300.SH", missing)
        self.assertNotIn("repo_rates:204001", missing)

    def test_us_price_for_today_uses_previous_completed_business_day(self) -> None:
        db_path, cfg = build_synced_db("2026-06-15", "2026-06-25")
        original_datetime = data_sync_module.datetime

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 6, 26, 2, 0, tzinfo=timezone.utc)

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

    def test_hk_price_for_today_uses_previous_completed_business_day(self) -> None:
        db_path, cfg = build_synced_db("2026-06-15", "2026-07-01")
        next(asset for asset in cfg["assets"] if asset["symbol"] == "03195.HK")["enabled"] = True
        original_datetime = data_sync_module.datetime

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 1, 10, 30, tzinfo=timezone.utc)

        try:
            data_sync_module.datetime = FixedDateTime
            with db_session(db_path) as conn:
                conn.execute("DELETE FROM prices WHERE symbol='03195.HK' AND trade_date='2026-07-01'")
                missing = required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"])
                conn.execute("DELETE FROM prices WHERE symbol='03195.HK' AND trade_date='2026-06-30'")
                missing_previous_day = required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"])
        finally:
            data_sync_module.datetime = original_datetime

        self.assertNotIn("prices:03195.HK", missing)
        self.assertIn("prices:03195.HK", missing_previous_day)

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

    def test_nasdaq_price_parser_pads_single_day_request(self) -> None:
        original_fetch_text = data_sync_module.fetch_text
        seen_urls: list[str] = []
        payload = {
            "data": {
                "symbol": "VOO",
                "tradesTable": {
                    "rows": [
                        {"date": "06/25/2026", "close": "675.71", "volume": "26,289,470", "open": "681.14", "high": "681.54", "low": "672.58"},
                        {"date": "06/24/2026", "close": "675.69", "volume": "9,676,956", "open": "677.68", "high": "682.07", "low": "673.68"},
                    ]
                },
            }
        }

        def fake_fetch_text(url, *_args, **_kwargs):
            seen_urls.append(url)
            return data_sync_module.json.dumps(payload)

        try:
            data_sync_module.fetch_text = fake_fetch_text
            rows = fetch_nasdaq_prices("VOO", "2026-06-25", "2026-06-25", "USD")
        finally:
            data_sync_module.fetch_text = original_fetch_text

        self.assertIn("fromdate=2026-06-24", seen_urls[0])
        self.assertEqual([row["trade_date"] for row in rows], ["2026-06-25"])
        self.assertEqual(rows[0]["close"], 675.71)

    def test_yahoo_price_parser_falls_back_to_query2(self) -> None:
        original_fetch_text = data_sync_module.fetch_text
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [1782394200],
                        "indicators": {
                            "quote": [{"open": [681.14], "high": [681.54], "low": [672.58], "close": [675.71], "volume": [26266500]}],
                            "adjclose": [{"adjclose": [675.71]}],
                        },
                    }
                ],
                "error": None,
            }
        }

        def fake_fetch_text(url, *_args, **_kwargs):
            if "query1.finance.yahoo.com" in url:
                raise data_sync_module.SyncWarning("query1 unavailable")
            return data_sync_module.json.dumps(payload)

        try:
            data_sync_module.fetch_text = fake_fetch_text
            rows = fetch_yahoo_prices("VOO", "2026-06-25", "2026-06-25", "USD")
        finally:
            data_sync_module.fetch_text = original_fetch_text

        self.assertEqual(rows[0]["trade_date"], "2026-06-25")
        self.assertEqual(rows[0]["source"], "yahoo:query2:chart")

    def test_hk_yahoo_price_parser_uses_yahoo_symbol_and_restores_config_symbol(self) -> None:
        original_fetch_yahoo_prices = data_sync_module.fetch_yahoo_prices
        seen_symbols: list[str] = []

        def fake_fetch_yahoo_prices(symbol, start, end, currency):
            seen_symbols.append(symbol)
            return [
                {
                    "symbol": symbol,
                    "trade_date": start,
                    "open": 8.0,
                    "high": 8.1,
                    "low": 7.9,
                    "close": 8.05,
                    "adj_close": 8.05,
                    "volume": 1000,
                    "amount": 0.0,
                    "currency": currency,
                    "source": "test:yahoo",
                }
            ]

        try:
            data_sync_module.fetch_yahoo_prices = fake_fetch_yahoo_prices
            rows = fetch_hk_yahoo_prices("03195.HK", "2026-06-25", "2026-06-25", "HKD")
        finally:
            data_sync_module.fetch_yahoo_prices = original_fetch_yahoo_prices

        self.assertEqual(seen_symbols, ["3195.HK"])
        self.assertEqual(rows[0]["symbol"], "03195.HK")
        self.assertEqual(rows[0]["currency"], "HKD")

    def test_tencent_hk_price_parser_reads_hkd_counter_prices(self) -> None:
        original_fetch_text = data_sync_module.fetch_text
        payload = {
            "code": 0,
            "msg": "",
            "data": {
                "hk03195": {
                    "day": [
                        ["2024-05-02", "8.150", "7.950", "8.235", "7.835", "374200.000"],
                        ["2024-08-12", "8.380", "8.400", "8.400", "8.380", "12800.000"],
                    ]
                }
            },
        }

        try:
            data_sync_module.fetch_text = lambda *_args, **_kwargs: data_sync_module.json.dumps(payload)
            rows = fetch_tencent_hk_prices("03195.HK", "2024-05-01", "2024-08-12", "HKD")
        finally:
            data_sync_module.fetch_text = original_fetch_text

        self.assertEqual(tencent_hk_symbol("03195.HK"), "hk03195")
        self.assertEqual(rows[0]["close"], 7.95)
        self.assertEqual(rows[0]["source"], "tencent:hk_qfq")
        self.assertEqual(rows[-1]["volume"], 12800.0)

    def test_yahoo_spark_price_parser_reads_recent_close(self) -> None:
        original_fetch_text = data_sync_module.fetch_text
        payload = {
            "VOO": {
                "timestamp": [1782221400, 1782307800, 1782394200],
                "close": [676.34, 675.69, 675.71],
            }
        }

        try:
            data_sync_module.fetch_text = lambda *_args, **_kwargs: data_sync_module.json.dumps(payload)
            rows = fetch_yahoo_spark_prices("VOO", "2026-06-24", "2026-06-25", "USD")
        finally:
            data_sync_module.fetch_text = original_fetch_text

        self.assertEqual([row["trade_date"] for row in rows], ["2026-06-24", "2026-06-25"])
        self.assertEqual(rows[-1]["close"], 675.71)
        self.assertEqual(rows[-1]["source"], "yahoo:query1:spark")

    def test_currency_api_fx_parser_reads_usd_cny(self) -> None:
        original_fetch_text = data_sync_module.fetch_text
        payload = {"date": "2026-06-25", "usd": {"cny": 6.80187662}}

        try:
            data_sync_module.fetch_text = lambda *_args, **_kwargs: data_sync_module.json.dumps(payload)
            rows = fetch_currency_api_fx_rates("2026-06-25", "2026-06-25")
        finally:
            data_sync_module.fetch_text = original_fetch_text

        self.assertEqual(rows, [{"pair": "USD/CNY", "trade_date": "2026-06-25", "rate": 6.80187662, "source": "currency-api:jsdelivr"}])

    def test_currency_api_fx_parser_reads_hkd_cny(self) -> None:
        original_fetch_text = data_sync_module.fetch_text
        payload = {"date": "2026-06-25", "hkd": {"cny": 0.9123}}

        try:
            data_sync_module.fetch_text = lambda *_args, **_kwargs: data_sync_module.json.dumps(payload)
            rows = fetch_currency_api_fx_rates("2026-06-25", "2026-06-25", "HKD/CNY")
        finally:
            data_sync_module.fetch_text = original_fetch_text

        self.assertEqual(rows, [{"pair": "HKD/CNY", "trade_date": "2026-06-25", "rate": 0.9123, "source": "currency-api:jsdelivr"}])

    def test_targeted_fx_sync_uses_later_fallback_sources(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-10")
        original_datasrc = data_sync_module.fetch_datasrc_fx_rates
        original_yahoo = data_sync_module.fetch_yahoo_fx_rates
        original_frankfurter = data_sync_module.fetch_frankfurter_fx_rates
        original_stooq = data_sync_module.fetch_stooq_fx_rates
        original_currency_api = data_sync_module.fetch_currency_api_fx_rates

        def fail_fx_source(*_args, **_kwargs):
            raise data_sync_module.SyncWarning("source unavailable")

        def fake_currency_api(_start, _end):
            return [{"pair": "USD/CNY", "trade_date": "2020-01-10", "rate": 7.01, "source": "test:currency-api"}]

        try:
            data_sync_module.fetch_datasrc_fx_rates = fail_fx_source
            data_sync_module.fetch_yahoo_fx_rates = fail_fx_source
            data_sync_module.fetch_frankfurter_fx_rates = fail_fx_source
            data_sync_module.fetch_stooq_fx_rates = fail_fx_source
            data_sync_module.fetch_currency_api_fx_rates = fake_currency_api
            with db_session(db_path) as conn:
                conn.execute("DELETE FROM fx_rates WHERE pair='USD/CNY' AND trade_date='2020-01-10'")
                result = sync_all(conn, "", cfg["start_date"], cfg["end_date"], cfg["assets"], missing_items=["fx_rates:USD/CNY"])
                row = conn.execute("SELECT rate, source FROM fx_rates WHERE pair='USD/CNY' AND trade_date='2020-01-10'").fetchone()
        finally:
            data_sync_module.fetch_datasrc_fx_rates = original_datasrc
            data_sync_module.fetch_yahoo_fx_rates = original_yahoo
            data_sync_module.fetch_frankfurter_fx_rates = original_frankfurter
            data_sync_module.fetch_stooq_fx_rates = original_stooq
            data_sync_module.fetch_currency_api_fx_rates = original_currency_api

        self.assertEqual(result["inserted"]["fx_rates"], 1)
        self.assertNotIn("fx_rates:USD/CNY", result["missing_data"])
        self.assertEqual(dict(row), {"rate": 7.01, "source": "test:currency-api"})

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

    def test_cn_dividend_sync_marks_public_empty_coverage(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-12-31")
        original_fetch_fund_dividends = data_sync_module.fetch_fund_dividends
        original_fetch_eastmoney_dividends = data_sync_module.fetch_eastmoney_fund_dividends

        def fail_tushare(*_args, **_kwargs):
            raise data_sync_module.SyncWarning("TUSHARE_TOKEN is not configured")

        try:
            data_sync_module.fetch_fund_dividends = fail_tushare
            data_sync_module.fetch_eastmoney_fund_dividends = lambda *_args, **_kwargs: []
            with db_session(db_path) as conn:
                conn.execute("DELETE FROM sync_coverage WHERE kind='dividends' AND symbol='510300.SH'")
                result = sync_all(conn, "", cfg["start_date"], cfg["end_date"], cfg["assets"], missing_items=["dividends:510300.SH"])
                coverage = rows_to_dicts(
                    conn.execute("SELECT kind, symbol, start_date, end_date, source FROM sync_coverage WHERE kind='dividends' AND symbol='510300.SH'")
                )
        finally:
            data_sync_module.fetch_fund_dividends = original_fetch_fund_dividends
            data_sync_module.fetch_eastmoney_fund_dividends = original_fetch_eastmoney_dividends

        self.assertEqual(result["inserted"]["dividends"], 0)
        self.assertNotIn("dividends:510300.SH", result["missing_data"])
        self.assertEqual(coverage[0]["source"], "eastmoney:fund_dividend")

    def test_dividend_sync_marks_empty_coverage_when_public_sources_fail(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-12-31")
        original_fetch_fund_dividends = data_sync_module.fetch_fund_dividends
        original_fetch_eastmoney_dividends = data_sync_module.fetch_eastmoney_fund_dividends

        def fail_source(*_args, **_kwargs):
            raise data_sync_module.SyncWarning("public dividend source timeout")

        try:
            data_sync_module.fetch_fund_dividends = fail_source
            data_sync_module.fetch_eastmoney_fund_dividends = fail_source
            with db_session(db_path) as conn:
                conn.execute("DELETE FROM sync_coverage WHERE kind='dividends' AND symbol='510300.SH'")
                result = sync_all(conn, "", cfg["start_date"], cfg["end_date"], cfg["assets"], missing_items=["dividends:510300.SH"])
                coverage = rows_to_dicts(
                    conn.execute("SELECT kind, symbol, start_date, end_date, source FROM sync_coverage WHERE kind='dividends' AND symbol='510300.SH'")
                )
        finally:
            data_sync_module.fetch_fund_dividends = original_fetch_fund_dividends
            data_sync_module.fetch_eastmoney_fund_dividends = original_fetch_eastmoney_dividends

        self.assertEqual(result["inserted"]["dividends"], 0)
        self.assertNotIn("dividends:510300.SH", result["missing_data"])
        self.assertEqual(coverage[0]["source"], "public:dividend_unavailable_empty")

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

    def test_targeted_cn_price_sync_uses_configured_price_fallback(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-10")
        original_fetch_cn = data_sync_module.fetch_cn_fund_prices
        original_datasrc = data_sync_module.fetch_datasrc_market_prices
        original_sohu = data_sync_module.fetch_sohu_prices
        original_eastmoney = data_sync_module.fetch_eastmoney_prices
        original_yahoo = data_sync_module.fetch_cn_yahoo_prices
        original_fallback = data_sync_module.fetch_price_fallback_rows

        def fail_source(*_args, **_kwargs):
            raise data_sync_module.SyncWarning("source unavailable")

        def fake_fallback(asset, range_start, range_end, target_rows):
            self.assertEqual(asset["symbol"], "510300.SH")
            self.assertEqual((range_start, range_end), ("2020-01-10", "2020-01-10"))
            self.assertTrue(target_rows)
            return [
                {
                    "symbol": "510300.SH",
                    "trade_date": "2020-01-10",
                    "open": 3.1,
                    "high": 3.1,
                    "low": 3.1,
                    "close": 3.1,
                    "adj_close": 3.1,
                    "volume": 0.0,
                    "amount": 0.0,
                    "currency": "CNY",
                    "source": "test:price_fallback",
                }
            ]

        try:
            data_sync_module.fetch_cn_fund_prices = fail_source
            data_sync_module.fetch_datasrc_market_prices = fail_source
            data_sync_module.fetch_sohu_prices = fail_source
            data_sync_module.fetch_eastmoney_prices = fail_source
            data_sync_module.fetch_cn_yahoo_prices = fail_source
            data_sync_module.fetch_price_fallback_rows = fake_fallback
            with db_session(db_path) as conn:
                conn.execute("DELETE FROM prices WHERE symbol='510300.SH' AND trade_date='2020-01-10'")
                result = sync_all(conn, "", cfg["start_date"], cfg["end_date"], cfg["assets"], missing_items=["prices:510300.SH"])
                row = conn.execute("SELECT close, source FROM prices WHERE symbol='510300.SH' AND trade_date='2020-01-10'").fetchone()
        finally:
            data_sync_module.fetch_cn_fund_prices = original_fetch_cn
            data_sync_module.fetch_datasrc_market_prices = original_datasrc
            data_sync_module.fetch_sohu_prices = original_sohu
            data_sync_module.fetch_eastmoney_prices = original_eastmoney
            data_sync_module.fetch_cn_yahoo_prices = original_yahoo
            data_sync_module.fetch_price_fallback_rows = original_fallback

        self.assertEqual(result["inserted"]["prices"], 1)
        self.assertNotIn("prices:510300.SH", result["missing_data"])
        self.assertEqual(dict(row), {"close": 3.1, "source": "test:price_fallback"})

    def test_hs300_fallback_nav_is_scaled_to_target_price_level(self) -> None:
        cfg = normalize_config({})
        asset = next(asset for asset in cfg["assets"] if asset["symbol"] == "510300.SH")
        original_fetch_nav = data_sync_module.fetch_fund_nav_proxy_prices

        def fake_fetch_nav(_proxy_symbol, target_symbol, _start, _end, currency):
            return [
                {
                    "symbol": target_symbol,
                    "trade_date": "2020-01-09",
                    "open": 2.0,
                    "high": 2.0,
                    "low": 2.0,
                    "close": 2.0,
                    "adj_close": 2.0,
                    "volume": 0.0,
                    "amount": 0.0,
                    "currency": currency,
                    "source": "test:nav",
                },
                {
                    "symbol": target_symbol,
                    "trade_date": "2020-01-10",
                    "open": 3.0,
                    "high": 3.0,
                    "low": 3.0,
                    "close": 3.0,
                    "adj_close": 3.0,
                    "volume": 0.0,
                    "amount": 0.0,
                    "currency": currency,
                    "source": "test:nav",
                },
            ]

        try:
            data_sync_module.fetch_fund_nav_proxy_prices = fake_fetch_nav
            rows = data_sync_module.fetch_price_fallback_rows(
                asset,
                "2020-01-10",
                "2020-01-10",
                [{"trade_date": "2020-01-09", "close": 6.0, "source": "fixture:price"}],
            )
        finally:
            data_sync_module.fetch_fund_nav_proxy_prices = original_fetch_nav

        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["close"], 9.0)
        self.assertIn("splice_scale_3", rows[0]["source"])

    def test_gold_fallback_au9999_price_is_scaled_to_etf_share_price(self) -> None:
        cfg = normalize_config({})
        asset = next(asset for asset in cfg["assets"] if asset["symbol"] == "518880.SH")
        original_fetch_gold = data_sync_module.fetch_au9999_proxy_prices

        def fake_fetch_gold(target_symbol, _start, _end, currency):
            return [
                {
                    "symbol": target_symbol,
                    "trade_date": "2020-01-10",
                    "open": 900.0,
                    "high": 901.0,
                    "low": 899.0,
                    "close": 900.0,
                    "adj_close": 900.0,
                    "volume": 0.0,
                    "amount": 0.0,
                    "currency": currency,
                    "source": "test:au9999",
                }
            ]

        try:
            data_sync_module.fetch_au9999_proxy_prices = fake_fetch_gold
            rows = data_sync_module.fetch_price_fallback_rows(asset, "2020-01-10", "2020-01-10", [])
        finally:
            data_sync_module.fetch_au9999_proxy_prices = original_fetch_gold

        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["close"], 9.0)
        self.assertIn("fixed_scale_0.01", rows[0]["source"])

    def test_required_data_missing_uses_fallback_price_start_but_not_dividend_start(self) -> None:
        db_path, cfg = build_synced_db("2012-01-01", "2012-01-31")
        with db_session(db_path) as conn:
            conn.execute("DELETE FROM prices WHERE symbol='510300.SH' AND trade_date='2012-01-31'")
            missing = required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"])

        self.assertIn("prices:510300.SH", missing)
        self.assertNotIn("dividends:510300.SH", missing)

    def test_targeted_us_price_sync_fills_missing_dates_from_multiple_sources(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-10")
        original_yahoo = data_sync_module.fetch_yahoo_prices
        original_nasdaq = data_sync_module.fetch_nasdaq_prices
        original_stooq = data_sync_module.fetch_stooq_prices
        original_spark = data_sync_module.fetch_yahoo_spark_prices

        def price_row(source, trade_date, close):
            return {
                "symbol": "VOO",
                "trade_date": trade_date,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "adj_close": close,
                "volume": 0.0,
                "amount": 0.0,
                "currency": "USD",
                "source": source,
            }

        try:
            data_sync_module.fetch_yahoo_prices = lambda *_args, **_kwargs: [price_row("test:yahoo", "2020-01-08", 280.0)]
            data_sync_module.fetch_nasdaq_prices = lambda *_args, **_kwargs: [price_row("test:nasdaq", "2020-01-09", 281.0)]
            data_sync_module.fetch_stooq_prices = lambda *_args, **_kwargs: []
            data_sync_module.fetch_yahoo_spark_prices = lambda *_args, **_kwargs: [price_row("test:spark", "2020-01-10", 282.0)]
            with db_session(db_path) as conn:
                conn.execute("DELETE FROM prices WHERE symbol='VOO' AND trade_date >= '2020-01-08'")
                result = sync_all(conn, "", cfg["start_date"], cfg["end_date"], cfg["assets"], missing_items=["prices:VOO"])
                rows = rows_to_dicts(
                    conn.execute(
                        "SELECT trade_date, close, source FROM prices WHERE symbol='VOO' AND trade_date >= '2020-01-08' ORDER BY trade_date"
                    )
                )
        finally:
            data_sync_module.fetch_yahoo_prices = original_yahoo
            data_sync_module.fetch_nasdaq_prices = original_nasdaq
            data_sync_module.fetch_stooq_prices = original_stooq
            data_sync_module.fetch_yahoo_spark_prices = original_spark

        self.assertEqual(result["inserted"]["prices"], 3)
        self.assertNotIn("prices:VOO", result["missing_data"])
        self.assertEqual([row["source"] for row in rows], ["test:yahoo", "test:nasdaq", "test:spark"])

    def test_targeted_hk_price_sync_uses_multiple_public_fallbacks(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-10")
        hk_asset = next(asset for asset in cfg["assets"] if asset["symbol"] == "03195.HK")
        hk_asset["enabled"] = True
        original_datasrc = data_sync_module.fetch_datasrc_market_prices
        original_yahoo = data_sync_module.fetch_hk_yahoo_prices
        original_eastmoney = data_sync_module.fetch_eastmoney_prices
        original_tencent = data_sync_module.fetch_tencent_hk_prices
        original_stooq = data_sync_module.fetch_hk_stooq_prices
        original_spark = data_sync_module.fetch_hk_yahoo_spark_prices

        def price_row(source, trade_date, close):
            return {
                "symbol": "03195.HK",
                "trade_date": trade_date,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "adj_close": close,
                "volume": 0.0,
                "amount": 0.0,
                "currency": "HKD",
                "source": source,
            }

        try:
            data_sync_module.fetch_datasrc_market_prices = lambda *_args, **_kwargs: [price_row("test:datasrc", "2020-01-08", 8.0)]
            data_sync_module.fetch_eastmoney_prices = lambda *_args, **_kwargs: [price_row("test:eastmoney", "2020-01-09", 8.1)]
            data_sync_module.fetch_tencent_hk_prices = lambda *_args, **_kwargs: [price_row("test:tencent", "2020-01-10", 8.2)]
            data_sync_module.fetch_hk_yahoo_prices = lambda *_args, **_kwargs: [price_row("test:yahoo-bad", "2020-01-09", 1.1), price_row("test:yahoo-bad", "2020-01-10", 1.1)]
            data_sync_module.fetch_hk_stooq_prices = lambda *_args, **_kwargs: []
            data_sync_module.fetch_hk_yahoo_spark_prices = lambda *_args, **_kwargs: []
            with db_session(db_path) as conn:
                conn.execute("DELETE FROM prices WHERE symbol='03195.HK' AND trade_date >= '2020-01-08'")
                result = sync_all(conn, "", cfg["start_date"], cfg["end_date"], cfg["assets"], missing_items=["prices:03195.HK"])
                rows = rows_to_dicts(
                    conn.execute(
                        "SELECT trade_date, close, source FROM prices WHERE symbol='03195.HK' AND trade_date >= '2020-01-08' ORDER BY trade_date"
                    )
                )
        finally:
            data_sync_module.fetch_datasrc_market_prices = original_datasrc
            data_sync_module.fetch_hk_yahoo_prices = original_yahoo
            data_sync_module.fetch_eastmoney_prices = original_eastmoney
            data_sync_module.fetch_tencent_hk_prices = original_tencent
            data_sync_module.fetch_hk_stooq_prices = original_stooq
            data_sync_module.fetch_hk_yahoo_spark_prices = original_spark

        self.assertEqual(result["inserted"]["prices"], 3)
        self.assertNotIn("prices:03195.HK", result["missing_data"])
        self.assertEqual([row["source"] for row in rows], ["test:datasrc", "test:eastmoney", "test:tencent"])

    def test_targeted_cn_price_sync_replaces_bad_primary_source_with_public_consensus(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-10")
        cn_sp500 = next(asset for asset in cfg["assets"] if asset["symbol"] == "513500.SH")
        cn_sp500["enabled"] = True
        original_fetch_cn = data_sync_module.fetch_cn_fund_prices
        original_datasrc = data_sync_module.fetch_datasrc_market_prices
        original_sohu = data_sync_module.fetch_sohu_prices
        original_eastmoney = data_sync_module.fetch_eastmoney_prices
        original_yahoo = data_sync_module.fetch_cn_yahoo_prices
        original_adj = data_sync_module.fetch_adj_factors

        def price_row(source, trade_date, close):
            return {
                "symbol": "513500.SH",
                "trade_date": trade_date,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "adj_close": close,
                "volume": 0.0,
                "amount": 0.0,
                "currency": "CNY",
                "source": source,
            }

        try:
            data_sync_module.fetch_cn_fund_prices = lambda *_args, **_kwargs: [
                price_row("tushare:fund_daily", "2020-01-08", 9.0),
                price_row("tushare:fund_daily", "2020-01-09", 1.02),
                price_row("tushare:fund_daily", "2020-01-10", 1.03),
            ]
            data_sync_module.fetch_datasrc_market_prices = lambda *_args, **_kwargs: []
            data_sync_module.fetch_sohu_prices = lambda *_args, **_kwargs: [
                price_row("sohu:hisHq", "2020-01-08", 1.01),
                price_row("sohu:hisHq", "2020-01-09", 1.02),
                price_row("sohu:hisHq", "2020-01-10", 1.03),
            ]
            data_sync_module.fetch_eastmoney_prices = lambda *_args, **_kwargs: [
                price_row("eastmoney:fund_kline", "2020-01-08", 1.01),
                price_row("eastmoney:fund_kline", "2020-01-09", 1.02),
                price_row("eastmoney:fund_kline", "2020-01-10", 1.03),
            ]
            data_sync_module.fetch_cn_yahoo_prices = lambda *_args, **_kwargs: [
                price_row("yahoo:query1:chart", "2020-01-08", 1.01),
                price_row("yahoo:query1:chart", "2020-01-09", 1.02),
                price_row("yahoo:query1:chart", "2020-01-10", 1.03),
            ]
            data_sync_module.fetch_adj_factors = lambda *_args, **_kwargs: []
            with db_session(db_path) as conn:
                conn.execute("DELETE FROM prices WHERE symbol='513500.SH' AND trade_date >= '2020-01-08'")
                result = sync_all(conn, "", cfg["start_date"], cfg["end_date"], cfg["assets"], missing_items=["prices:513500.SH"])
                rows = rows_to_dicts(
                    conn.execute(
                        "SELECT trade_date, close, source FROM prices WHERE symbol='513500.SH' AND trade_date >= '2020-01-08' ORDER BY trade_date"
                    )
                )
        finally:
            data_sync_module.fetch_cn_fund_prices = original_fetch_cn
            data_sync_module.fetch_datasrc_market_prices = original_datasrc
            data_sync_module.fetch_sohu_prices = original_sohu
            data_sync_module.fetch_eastmoney_prices = original_eastmoney
            data_sync_module.fetch_cn_yahoo_prices = original_yahoo
            data_sync_module.fetch_adj_factors = original_adj

        self.assertEqual(result["inserted"]["prices"], 3)
        self.assertNotIn("prices:513500.SH", result["missing_data"])
        self.assertEqual(rows[0], {"trade_date": "2020-01-08", "close": 1.01, "source": "sohu:hisHq"})
        self.assertEqual(rows[1]["source"], "tushare:fund_daily")

    def test_existing_cn_price_anomaly_triggers_targeted_resync(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-10")
        cn_sp500 = next(asset for asset in cfg["assets"] if asset["symbol"] == "513500.SH")
        cn_sp500["enabled"] = True
        original_fetch_cn = data_sync_module.fetch_cn_fund_prices
        original_datasrc = data_sync_module.fetch_datasrc_market_prices
        original_sohu = data_sync_module.fetch_sohu_prices
        original_eastmoney = data_sync_module.fetch_eastmoney_prices
        original_yahoo = data_sync_module.fetch_cn_yahoo_prices
        original_adj = data_sync_module.fetch_adj_factors

        def price_row(source, trade_date, close):
            return {
                "symbol": "513500.SH",
                "trade_date": trade_date,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "adj_close": close,
                "volume": 0.0,
                "amount": 0.0,
                "currency": "CNY",
                "source": source,
            }

        try:
            data_sync_module.fetch_cn_fund_prices = lambda *_args, **_kwargs: [price_row("tushare:fund_daily", "2020-01-08", 1.01)]
            data_sync_module.fetch_datasrc_market_prices = lambda *_args, **_kwargs: []
            data_sync_module.fetch_sohu_prices = lambda *_args, **_kwargs: [price_row("sohu:hisHq", "2020-01-08", 1.01)]
            data_sync_module.fetch_eastmoney_prices = lambda *_args, **_kwargs: [price_row("eastmoney:fund_kline", "2020-01-08", 1.01)]
            data_sync_module.fetch_cn_yahoo_prices = lambda *_args, **_kwargs: []
            data_sync_module.fetch_adj_factors = lambda *_args, **_kwargs: []
            with db_session(db_path) as conn:
                conn.execute(
                    """
                    UPDATE prices
                    SET open=100.0, high=100.0, low=100.0, close=100.0, adj_close=100.0, source='test:bad'
                    WHERE symbol='513500.SH' AND trade_date='2020-01-08'
                    """
                )
                missing = required_data_missing(conn, cfg["start_date"], cfg["end_date"], cfg["assets"])
                result = sync_all(conn, "", cfg["start_date"], cfg["end_date"], cfg["assets"], missing_items=missing)
                row = conn.execute(
                    "SELECT trade_date, close, source FROM prices WHERE symbol='513500.SH' AND trade_date='2020-01-08'"
                ).fetchone()
        finally:
            data_sync_module.fetch_cn_fund_prices = original_fetch_cn
            data_sync_module.fetch_datasrc_market_prices = original_datasrc
            data_sync_module.fetch_sohu_prices = original_sohu
            data_sync_module.fetch_eastmoney_prices = original_eastmoney
            data_sync_module.fetch_cn_yahoo_prices = original_yahoo
            data_sync_module.fetch_adj_factors = original_adj

        self.assertIn("prices:513500.SH", missing)
        self.assertEqual(result["inserted"]["prices"], 1)
        self.assertEqual(dict(row), {"trade_date": "2020-01-08", "close": 1.01, "source": "tushare:fund_daily"})

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
        self.assertEqual(eastmoney_secid("03195.HK"), "116.03195")
        self.assertEqual(stooq_hk_symbol("03195.HK"), "3195.hk")
        self.assertEqual(tencent_hk_symbol("03195.HK"), "hk03195")
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
        self.assertEqual(data_sync_module.iso_date_text("2020-01-02"), "2020-01-02")
        self.assertEqual(data_sync_module.finite_float("3.14"), 3.14)
        self.assertIsNone(data_sync_module.finite_float("bad"))
        self.assertIsNone(data_sync_module.finite_float(float("nan")))
        row = data_sync_module.price_row("X", "2020-01-02", 100.0, "CNY", "test")
        self.assertEqual(row["open"], 100.0)
        self.assertEqual(row["source"], "test")
        self.assertEqual(
            data_sync_module.price_scale_from_overlap(
                [{"trade_date": "2020-01-02", "close": 6.0, "source": "fixture"}],
                [{"trade_date": "2020-01-02", "close": 2.0, "source": "fallback"}],
            ),
            3.0,
        )
        scaled = data_sync_module.scale_price_rows([row], 0.01, "scaled")
        self.assertEqual(scaled[0]["close"], 1.0)
        self.assertIn("scaled", scaled[0]["source"])
        nav_rows = data_sync_module.parse_eastmoney_net_worth_trend(
            'var Data_netWorthTrend = [{"x":1577923200000,"y":1.23,"equityReturn":0}];',
            "160706",
            "510300.SH",
            "2020-01-01",
            "2020-01-03",
            "CNY",
        )
        self.assertEqual(nav_rows[0]["trade_date"], "2020-01-02")
        self.assertEqual(nav_rows[0]["close"], 1.23)
        self.assertEqual(nav_rows[0]["source"], "eastmoney:fund_nav:160706")
        self.assertEqual(data_sync_module.fetch_price_fallback_rows({"symbol": "X", "currency": "CNY"}, "2020-01-02", "2020-01-02", []), [])
        with self.assertRaises(data_sync_module.SyncWarning):
            data_sync_module.fetch_price_fallback_rows(
                {"symbol": "X", "currency": "CNY", "price_fallback": {"kind": "unknown"}},
                "2020-01-02",
                "2020-01-02",
                [],
            )


if __name__ == "__main__":
    unittest.main()
