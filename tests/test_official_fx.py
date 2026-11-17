from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from app.services.data_sync import SyncWarning, fetch_safe_fx_rates


class OfficialFxTests(unittest.TestCase):
    def table(self, rows, headers=("日期", "美元", "欧元", "日元", "港元")):
        header = "".join(f"<th><!-- provider comment -->{value}</th>" for value in headers)
        body = "".join("<tr>" + "".join(f"<td td>{value}</td>" for value in row) + "</tr>" for row in rows)
        return f'<table id="InfoTable"><tr>{header}</tr>{body}</table>'

    def test_good_friday_rates_keep_official_date_and_convert_per_100_units(self):
        body = self.table([("2012-04-06", "630.72", "824.13", "7.6632", "81.232")])
        with patch("app.services.data_sync.fetch_text", return_value=body):
            usd = fetch_safe_fx_rates("2012-04-06", "2012-04-06")
            hkd = fetch_safe_fx_rates("2012-04-06", "2012-04-06", "HKD/CNY")
        self.assertEqual(usd, [{
            "pair": "USD/CNY", "trade_date": "2012-04-06", "rate": 6.3072,
            "source": "safe:cfets_central_parity",
        }])
        self.assertAlmostEqual(hkd[0]["rate"], 0.81232)
        self.assertEqual(hkd[0]["trade_date"], "2012-04-06")

    def test_columns_are_found_by_name_and_outside_dates_are_excluded(self):
        body = self.table([
            ("85.619", "2026-10-21", "671.92"),
            ("85.628", "2026-10-20", "672.0"),
        ], headers=("港元", "日期", "美元"))
        with patch("app.services.data_sync.fetch_text", return_value=body):
            rows = fetch_safe_fx_rates("2026-10-21", "2026-10-21", "HKD/CNY")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_date"], "2026-10-21")
        self.assertAlmostEqual(rows[0]["rate"], 0.85619)

    def test_missing_dates_and_invalid_rates_are_never_filled(self):
        body = self.table([
            ("2012-04-05", "630.35", "", "", "81.2"),
            ("2012-04-06", "-", "", "", "-"),
            ("2012-04-09", "NaN", "", "", "0"),
            ("2012-04-10", "-1", "", "", "inf"),
        ])
        with patch("app.services.data_sync.fetch_text", return_value=body):
            rows = fetch_safe_fx_rates("2012-04-05", "2012-04-10")
        self.assertEqual([row["trade_date"] for row in rows], ["2012-04-05"])

    def test_long_requests_are_split_into_official_query_limit(self):
        def response(url, **kwargs):
            query = parse_qs(urlparse(url).query)
            start, end = query["startDate"][0], query["endDate"][0]
            self.assertLessEqual((date.fromisoformat(end) - date.fromisoformat(start)).days, 365)
            return self.table([(start, "630.72", "", "", "81.232")])

        with patch("app.services.data_sync.fetch_text", side_effect=response) as fetch:
            rows = fetch_safe_fx_rates("2012-01-01", "2013-12-31")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([row["trade_date"] for row in rows], ["2012-01-01", "2013-01-01"])

    def test_empty_or_changed_official_response_raises(self):
        for body in ("<html>service unavailable</html>", self.table([])):
            with self.subTest(body=body), patch("app.services.data_sync.fetch_text", return_value=body):
                with self.assertRaises(SyncWarning):
                    fetch_safe_fx_rates("2012-04-06", "2012-04-06")

    def test_unsupported_currency_does_not_fetch(self):
        with patch("app.services.data_sync.fetch_text") as fetch:
            with self.assertRaises(SyncWarning):
                fetch_safe_fx_rates("2012-04-06", "2012-04-06", "EUR/CNY")
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
