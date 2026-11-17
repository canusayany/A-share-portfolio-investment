from __future__ import annotations

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from app.db import db_session
from app.services.data_sync import SyncWarning, fetch_hang_seng_dividends, sync_all
from tests.helpers import build_synced_db


def official_history() -> dict:
    # Schema observed in the manager's production H01396 endpoint on 2026-10-21.
    classes = []
    for code, fund_class, currency in ((3195, "B", "HKD"), (9195, "NA", "USD")):
        classes.append({
            "Fund_code": code, "Fund_class": fund_class, "Fund_unit": "NA",
            "Class_currency": currency, "Class_ISIN_code": "HK0000998515",
            "Unlisted_class": "N", "Listing_date": "2024-04-23",
            "Dividend_fund_class": "NA", "Dividend_fund_unit": "NA",
            "Dealing_date": "2025-01-10", "Dividends": "",
        })
    return {"Fund": {"Trust_no": "H01396", "Base_Currency": "USD", "FundUnitClass": classes}}


class HangSengOfficialDividendTests(unittest.TestCase):
    def fetch_payload(self, payload: dict) -> list:
        with patch("app.services.data_sync.fetch_text", return_value=json.dumps(payload)):
            return fetch_hang_seng_dividends("03195.HK", "2025-01-02", "2025-01-10", "HKD")

    def test_verified_empty_history_does_not_invent_dividend_rows(self):
        self.assertEqual(self.fetch_payload(official_history()), [])

    def test_transport_errors_and_malformed_json_remain_failures(self):
        for response in ("not JSON", "[]", "null"):
            with self.subTest(response=response), patch("app.services.data_sync.fetch_text", return_value=response):
                with self.assertRaises(SyncWarning):
                    fetch_hang_seng_dividends("03195.HK", "2025-01-02", "2025-01-10", "HKD")
        with patch("app.services.data_sync.fetch_text", side_effect=SyncWarning("unavailable")):
            with self.assertRaises(SyncWarning):
                fetch_hang_seng_dividends("03195.HK", "2025-01-02", "2025-01-10", "HKD")

    def test_checks_shared_usd_distribution_counter(self):
        payload = official_history()
        payload["Fund"]["FundUnitClass"][1]["Dividends"] = {"Dividend": {"amount": 1}}
        with self.assertRaises(SyncWarning):
            self.fetch_payload(payload)

    def test_rejects_wrong_or_missing_fund_and_counter_identity(self):
        mutations = [
            ("fund", "Trust_no", "OTHER"), ("fund", "Base_Currency", "HKD"),
            ("class", "Class_ISIN_code", "OTHER"), ("class", "Fund_code", 9195),
            ("class", "Class_currency", "USD"), ("class", "Unlisted_class", "Y"),
            ("class", "Listing_date", "2015-05-18"),
            ("class", "Dividend_fund_class", "A"),
            ("class", "Dividend_fund_unit", "ACC"),
            ("class", "Dealing_date", "2025-01-09"),
            ("class", "Dealing_date", "invalid"),
        ]
        for target, key, value in mutations:
            with self.subTest(key=key, value=value):
                payload = official_history()
                row = payload["Fund"] if target == "fund" else payload["Fund"]["FundUnitClass"][0]
                row[key] = value
                with self.assertRaises(SyncWarning):
                    self.fetch_payload(payload)
        for key in ("Fund_code", "Dividends", "Dealing_date", "Class_ISIN_code"):
            with self.subTest(missing=key):
                payload = official_history()
                del payload["Fund"]["FundUnitClass"][0][key]
                with self.assertRaises(SyncWarning):
                    self.fetch_payload(payload)

    def test_rejects_ambiguous_classes_and_unrecognized_event_schema(self):
        for value in (None, [], {}, {"Dividend": []}, {"Dividend": [{"Ex_div_date": "2025-01-03"}]}):
            with self.subTest(dividends=value):
                payload = official_history()
                payload["Fund"]["FundUnitClass"][0]["Dividends"] = value
                with self.assertRaises(SyncWarning):
                    self.fetch_payload(payload)
        payload = official_history()
        payload["Fund"]["FundUnitClass"].append(deepcopy(payload["Fund"]["FundUnitClass"][0]))
        with self.assertRaises(SyncWarning):
            self.fetch_payload(payload)

    def test_rejects_unsupported_symbols_and_out_of_bounds_coverage(self):
        for symbol, start, end, currency in (
            ("03033.HK", "2025-01-02", "2025-01-10", "HKD"),
            ("03195.HK", "2025-01-02", "2025-01-10", "USD"),
            ("03195.HK", "2024-04-22", "2025-01-10", "HKD"),
            ("03195.HK", "2025-01-11", "2025-01-10", "HKD"),
            ("03195.HK", "2025-01-02", "2999-01-10", "HKD"),
        ):
            with self.subTest(symbol=symbol, start=start, end=end, currency=currency):
                with patch("app.services.data_sync.fetch_text") as fetch:
                    with self.assertRaises(SyncWarning):
                        fetch_hang_seng_dividends(symbol, start, end, currency)
                    fetch.assert_not_called()

    def test_sync_marks_only_requested_coverage_with_official_source(self):
        path, config = build_synced_db("2025-01-02", "2025-01-10")
        for asset in config["assets"]:
            if asset["symbol"] == "03195.HK":
                asset["enabled"] = True
                asset["target_weight"] = 0.1
        with db_session(path) as conn:
            conn.execute("DELETE FROM sync_coverage WHERE kind='dividends' AND symbol='03195.HK'")
            conn.execute("DELETE FROM fund_dividends WHERE symbol='03195.HK'")
            with patch("app.services.data_sync.fetch_text", return_value=json.dumps(official_history())), patch(
                "app.services.data_sync.fetch_hk_yahoo_dividends", side_effect=AssertionError("official history should suffice")
            ):
                result = sync_all(conn, "", config["start_date"], config["end_date"], config["assets"], missing_items=["dividends:03195.HK"])
            coverage = [dict(row) for row in conn.execute("SELECT start_date,end_date,source FROM sync_coverage WHERE kind='dividends' AND symbol='03195.HK'")]
            events = conn.execute("SELECT COUNT(*) FROM fund_dividends WHERE symbol='03195.HK'").fetchone()[0]
        self.assertEqual(coverage, [{"start_date": "2025-01-02", "end_date": "2025-01-10", "source": "hangseng:official:dividend"}])
        self.assertEqual(events, 0)
        self.assertNotIn("dividends:03195.HK", result["missing_data"])

    def test_sync_uses_yahoo_fallback_and_does_not_cover_failed_fetches(self):
        for yahoo_succeeds in (True, False):
            with self.subTest(yahoo_succeeds=yahoo_succeeds):
                path, config = build_synced_db("2025-01-02", "2025-01-10")
                for asset in config["assets"]:
                    if asset["symbol"] == "03195.HK":
                        asset["enabled"] = True
                        asset["target_weight"] = 0.1
                with db_session(path) as conn:
                    conn.execute("DELETE FROM sync_coverage WHERE kind='dividends' AND symbol='03195.HK'")
                    with patch("app.services.data_sync.fetch_hang_seng_dividends", side_effect=SyncWarning("stale official payload")), patch(
                        "app.services.data_sync.fetch_hk_yahoo_dividends", return_value=[],
                        side_effect=None if yahoo_succeeds else SyncWarning("Yahoo unavailable"),
                    ) as yahoo:
                        result = sync_all(conn, "", config["start_date"], config["end_date"], config["assets"], missing_items=["dividends:03195.HK"])
                    sources = [row["source"] for row in conn.execute("SELECT source FROM sync_coverage WHERE kind='dividends' AND symbol='03195.HK'")]
                yahoo.assert_called_once()
                self.assertEqual(sources, ["yahoo:chart:dividend"] if yahoo_succeeds else [])
                self.assertEqual("dividends:03195.HK" in result["missing_data"], not yahoo_succeeds)

    def test_changed_official_events_or_identity_cannot_be_hidden_by_empty_yahoo(self):
        for change in ("events", "identity"):
            with self.subTest(change=change):
                path, config = build_synced_db("2025-01-02", "2025-01-10")
                for asset in config["assets"]:
                    if asset["symbol"] == "03195.HK":
                        asset["enabled"] = True
                        asset["target_weight"] = 0.1
                payload = official_history()
                if change == "events":
                    payload["Fund"]["FundUnitClass"][1]["Dividends"] = {"Dividend": [{"amount": 1}]}
                else:
                    payload["Fund"]["Trust_no"] = "OTHER"
                with db_session(path) as conn:
                    conn.execute("DELETE FROM sync_coverage WHERE kind='dividends' AND symbol='03195.HK'")
                    with patch("app.services.data_sync.fetch_text", return_value=json.dumps(payload)), patch(
                        "app.services.data_sync.fetch_hk_yahoo_dividends", return_value=[]
                    ) as yahoo:
                        result = sync_all(conn, "", config["start_date"], config["end_date"], config["assets"], missing_items=["dividends:03195.HK"])
                    covered = conn.execute("SELECT COUNT(*) FROM sync_coverage WHERE kind='dividends' AND symbol='03195.HK'").fetchone()[0]
                yahoo.assert_not_called()
                self.assertEqual(covered, 0)
                self.assertIn("dividends:03195.HK", result["missing_data"])
