import json
import unittest
from unittest.mock import patch

from app.services import data_sync
from app.services.fund_income import verify_voo_uk_coverage


class VooDividendCoverageTests(unittest.TestCase):
    def setUp(self):
        self.us = ('{"ticker":"VOO","fundId":"0968","asOfDate":"01/03/2011","incomeCapitalGains":'
                   '[{"type":"Dividend","share":"$0.526","recordDate":"12/29/2010","payableDate":"12/31/2010"}]}')
        self.archive = [
            dict(symbol="VOO", currency="USD", ex_date="2010-09-24", pay_date="2010-09-30", div_cash=0.558),
            dict(symbol="VOO", currency="USD", ex_date="2010-12-27", pay_date="2010-12-31", div_cash=0.526),
        ]
        self.history = [
            dict(exDividendDate=r["ex_date"], recordDate=d, payableDate=r["pay_date"], taxDetails=[
                dict(distributionLevelCode="ACTL", distributionType={"distCode": "INC"}, currencyCode="USD",
                     distributionAmount=r["div_cash"] / 2)])
            for r, d in zip(self.archive, ["2010-09-28", "2010-12-29"])
        ]
        profile = {"portId": "0968", "fundCurrency": "USD"}
        self.prices = {"portId": "0968", "items": [{"asOfDate": "2011-01-05", "currencyCode": "USD", "price": 52.3}]}
        self.state = {
            "unrelated-response": {"b": {"data": []}},
            "gpx.fund-page-heading": {"portId": "0968"},
            "gpx.fund-facts": {"facts": {"isin": {"value": "US9229083632"}, "currency": {"value": "USD"}}},
            "distribution-response": {"b": {"data": {"funds": [
                {"profile": profile, "portId": "0968", "distributionDetails": {"periodicDistributions": {"items": self.history}}}
            ]}}},
            "price-response": {"b": {"data": {"funds": [
                {"profile": profile, "pricingDetails": {"marketPrices": {"items": [self.prices]}}}
            ]}}},
        }

    def page(self):
        return '<script id="serverApp-state" type="application/json">' + json.dumps(self.state) + '</script>'

    def sync(self, start, end):
        with patch.object(data_sync, "fetch_text", side_effect=[self.us, self.page()]) as fetch:
            with patch.object(data_sync, "fetch_digrin_dividends", return_value=self.archive):
                rows = data_sync.fetch_verified_voo_dividends(start, end)
        return rows, fetch.call_count

    def test_only_stale_verified_history_fetches_second_issuer_page(self):
        self.assertEqual(self.sync("2011-01-03", "2011-01-03"), ([], 1))
        self.assertEqual(self.sync("2011-01-04", "2011-01-05"), ([], 2))
        rows, calls = self.sync("2010-12-27", "2011-01-05")
        self.assertEqual((calls, len(rows), rows[0]["div_cash"], rows[0]["source"]),
                         (2, 1, 0.526, "vanguard:distribution:digrin_ex_date"))
        with self.assertRaisesRegex(data_sync.SyncWarning, "has not reached"):
            self.sync("2011-01-06", "2011-01-06")

    def test_primary_mismatch_never_falls_back(self):
        self.archive[-1]["pay_date"] = "2011-01-03"
        with patch.object(data_sync, "fetch_text", return_value=self.us) as fetch:
            with patch.object(data_sync, "fetch_digrin_dividends", return_value=self.archive):
                with self.assertRaisesRegex(data_sync.SyncWarning, "disagree on payment"):
                    data_sync.fetch_verified_voo_dividends("2011-01-04", "2011-01-05")
        self.assertEqual(fetch.call_count, 1)

    def test_every_ex_date_amount_and_pay_date_must_match(self):
        with self.subTest("missing older payment"):
            original = self.history.pop(0)
            with self.assertRaisesRegex(data_sync.SyncWarning, "different distribution dates"):
                self.sync("2011-01-04", "2011-01-05")
            self.history.insert(0, original)
        for key, value in [("exDividendDate", "2010-09-23"), ("payableDate", "2010-10-01"),
                           ("taxDetails", [{**original["taxDetails"][0], "distributionAmount": 0.558}])]:
            with self.subTest(key), patch.dict(original, {key: value}):
                with self.assertRaisesRegex(data_sync.SyncWarning, "disagree"):
                    self.sync("2011-01-04", "2011-01-05")
        self.history.append({**original, "exDividendDate": "2010-09-23"})
        with self.assertRaisesRegex(data_sync.SyncWarning, "disagree"):
            self.sync("2011-01-04", "2011-01-05")

    def test_only_identified_fund_usd_price_can_confirm_date(self):
        for target, replacement in [
            (self.state["gpx.fund-facts"]["facts"]["isin"], {"value": "IE00B3XXRP09"}),
            (self.prices, {"portId": "9999"}),
            (self.prices["items"][0], {"currencyCode": "GBP"}),
            (self.prices["items"][0], {"price": 0}),
        ]:
            with self.subTest(replacement), patch.dict(target, replacement):
                with self.assertRaises(data_sync.SyncWarning):
                    self.sync("2011-01-04", "2011-01-05")

    def test_post_split_dividend_uses_published_per_share_amount(self):
        ex = "2026-09-28"
        self.history[:] = [{
            **self.history[0], "exDividendDate": ex, "recordDate": ex, "payableDate": "2026-09-30",
            "taxDetails": [{**self.history[0]["taxDetails"][0], "distributionAmount": 1.8226}],
        }]
        self.prices["items"][0]["asOfDate"] = "2026-11-17"
        event = {"div_cash": 1.8226, "pay_date": "2026-09-30", "record_date": ex}
        verify_voo_uk_coverage(self.page(), {ex: event}, "2026-11-17")
        with self.assertRaisesRegex(ValueError, "disagree"):
            verify_voo_uk_coverage(self.page(), {ex: {**event, "div_cash": 1.823}}, "2026-11-17")
