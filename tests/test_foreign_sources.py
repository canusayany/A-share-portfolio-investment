from __future__ import annotations

from datetime import datetime
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from app.services.data_sync import (
    SyncWarning, fetch_eastmoney_us_prices, fetch_yahoo_chart_prices, fetch_yahoo_fx_rates, yahoo_period,
)


class YahooSessionDateTests(unittest.TestCase):
    def payload(self, zone, dates, closes, adjusted=None):
        timestamps = [
            int(datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(zone)).timestamp())
            for day in dates
        ]
        return json.dumps({"chart": {"result": [{
            "meta": {"exchangeTimezoneName": zone},
            "timestamp": timestamps,
            "indicators": {
                "quote": [{"open": closes, "high": closes, "low": closes, "close": closes, "volume": [0] * len(closes)}],
                "adjclose": [{"adjclose": adjusted or closes}],
            },
        }]}})

    def test_fx_summer_midnight_keeps_friday_source_date(self):
        body = self.payload("Europe/London", ["2012-03-30"], [6.306])
        with patch("app.services.data_sync.fetch_text", return_value=body) as fetch:
            rows = fetch_yahoo_fx_rates("2012-03-30", "2012-03-30")
        self.assertEqual(rows, [{
            "pair": "USD/CNY", "trade_date": "2012-03-30", "rate": 6.306,
            "source": "yahoo:CNY=X",
        }])
        query = parse_qs(urlparse(fetch.call_args.args[0]).query)
        self.assertLess(int(query["period1"][0]), yahoo_period("2012-03-30"))

    def test_fx_dates_follow_historical_dst_transition(self):
        dates = ["2012-03-23", "2012-03-26", "2012-10-26", "2012-10-29"]
        body = self.payload("Europe/London", dates, [6.3, 6.31, 6.32, 6.33])
        with patch("app.services.data_sync.fetch_text", return_value=body):
            rows = fetch_yahoo_fx_rates("2012-03-23", "2012-10-29")
        self.assertEqual([row["trade_date"] for row in rows], dates)

    def test_padded_request_filters_adjacent_local_sessions(self):
        body = self.payload("Europe/London", ["2012-03-29", "2012-03-30", "2012-04-02"], [6.3, 6.31, 6.32])
        with patch("app.services.data_sync.fetch_text", return_value=body):
            rows = fetch_yahoo_fx_rates("2012-03-30", "2012-03-30")
        self.assertEqual([(row["trade_date"], row["rate"]) for row in rows], [("2012-03-30", 6.31)])

    def test_voo_retains_raw_close_separately_from_adjusted_close(self):
        body = self.payload("America/New_York", ["2020-01-02T09:30:00"], [274.58], [250.0])
        with patch("app.services.data_sync.fetch_text", return_value=body):
            rows = fetch_yahoo_chart_prices("VOO", "2020-01-02", "2020-01-02", "USD", "query1", "yahoo:query1:chart")
        self.assertEqual(rows[0]["trade_date"], "2020-01-02")
        self.assertEqual(rows[0]["close"], 274.58)
        self.assertEqual(rows[0]["adj_close"], 250.0)


class EastmoneyUsPriceTests(unittest.TestCase):
    def test_voo_only_split_adjustment_preserves_dividend_cash_effect(self):
        body = json.dumps({"data": {"klines": [
            "2013-10-23,80.080,79.970,80.100,79.690,948200,1000",
            "2013-10-24,160.220,160.460,160.640,159.860,795600,2000",
        ]}})
        with patch("app.services.data_sync.fetch_text", return_value=body) as fetch:
            rows = fetch_eastmoney_us_prices("VOO", "2013-10-23", "2013-10-24", "USD")
        query = parse_qs(urlparse(fetch.call_args.args[0]).query)
        self.assertEqual(query["secid"], ["107.VOO"])
        self.assertEqual(query["fqt"], ["0"])
        self.assertEqual(rows[0]["open"], 160.16)
        self.assertEqual(rows[0]["close"], 159.94)
        self.assertEqual(rows[0]["adj_close"], 159.94)
        self.assertEqual(rows[0]["volume"], 948200)
        self.assertEqual(rows[1]["close"], 160.46)
        self.assertEqual(rows[1]["source"], "eastmoney:us_kline:split_only")

    def test_unverified_us_market_mapping_fails_without_network_request(self):
        with patch("app.services.data_sync.fetch_text") as fetch:
            with self.assertRaises(SyncWarning):
                fetch_eastmoney_us_prices("SPY", "2020-01-02", "2020-01-03", "USD")
        fetch.assert_not_called()

    def test_out_of_range_rows_are_not_imported(self):
        body = json.dumps({"data": {"klines": [
            "2020-01-01,270,270,270,270,0,0",
            "2020-01-02,274,275,276,273,100,0",
        ]}})
        with patch("app.services.data_sync.fetch_text", return_value=body):
            rows = fetch_eastmoney_us_prices("VOO", "2020-01-02", "2020-01-02", "USD")
        self.assertEqual([row["trade_date"] for row in rows], ["2020-01-02"])


if __name__ == "__main__":
    unittest.main()
