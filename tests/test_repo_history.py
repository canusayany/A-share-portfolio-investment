from __future__ import annotations

from datetime import date, datetime, timezone
import unittest
from unittest.mock import patch

from app.db import db_session, init_db, insert_many
from app.services.calendar import business_days
from app.services.data_sync import SyncWarning, mark_sync_coverage
from app.services.repo_history import (
    REPO_ABSENCE_KIND, REPO_ABSENCE_SOURCE, fetch_verified_repo_absences,
    verified_repo_absence_dates,
)
from tests.helpers import temp_db_path


class RepoHistoryNow(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = cls(2026, 10, 21, 4, tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


def quote(code: str, day: str, close=2.0) -> dict:
    return {"ts_code": code, "trade_date": day.replace("-", ""), "close": close}


class RepoHistoryTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("app.services.repo_history.datetime", RepoHistoryNow)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def fetch(self, rows, expected=None, symbol="204014"):
        expected = {"2010-01-04", "2010-01-05"} if expected is None else expected
        with patch("app.services.data_sync.tushare_call", return_value=rows):
            return fetch_verified_repo_absences("token", symbol, "2010-01-04", "2010-01-15", expected)

    def test_sparse_selected_quotes_are_proved_only_by_same_day_anchor(self):
        days = {day.isoformat() for day in business_days("2010-01-04", "2010-01-15")}
        rows = [quote("204001.SH", day) for day in days]
        rows.extend([quote("204014.SH", "2010-01-07"), quote("204014.SH", "2010-01-11")])
        # Other repo types in a full-market response, including IB, are valid.
        rows.append(quote("DR001.IB", "2010-01-04"))
        self.assertEqual(self.fetch(rows, days), days - {"2010-01-07", "2010-01-11"})

    def test_missing_anchor_is_not_evidence_of_selected_absence(self):
        self.assertEqual(self.fetch([quote("204014.SH", "2010-01-04")]), set())
        rows = [quote("204001.SH", "2010-01-04"), quote("204002.SH", "2010-01-05")]
        self.assertEqual(self.fetch(rows), {"2010-01-04"})

    def test_selected_quote_always_prevents_absence_including_zero_rate(self):
        rows = [quote("204001.SH", "2010-01-04"), quote("204014.SH", "2010-01-04", 0)]
        self.assertEqual(self.fetch(rows), set())
        self.assertEqual(self.fetch(rows + [quote("204014.SH", "2010-01-04", 0)]), set())

    def test_shenzhen_selected_symbol_uses_same_market_snapshot_anchor(self):
        rows = [quote("204001.SH", "2010-01-04"), quote("131810.SZ", "2010-01-05")]
        self.assertEqual(self.fetch(rows, symbol="131810"), {"2010-01-04"})

    def test_empty_malformed_nonfinite_and_truncated_responses_are_rejected(self):
        malformed = [
            [], None, {}, [None], [{}],
            [quote("", "2010-01-04")], [quote(None, "2010-01-04")],
            [{"ts_code": "204001.SH", "trade_date": "invalid", "close": 2}],
            [{"ts_code": "204001.SH", "trade_date": "20100230", "close": 2}],
            [quote("204001.SH", "2010-01-03")],
            [quote("204001.SH", "2010-01-04", None)],
            [quote("204001.SH", "2010-01-04", True)],
            [quote("204001.SH", "2010-01-04", "not-a-rate")],
            [quote("204001.SH", "2010-01-04", float("nan"))],
            [quote("204001.SH", "2010-01-04", float("inf"))],
            [quote("204001.SH", "2010-01-04"), quote("204014.SH", "2010-01-04", float("nan"))],
            [quote("204001.SH", "2010-01-04"), quote("204001.SH", "2010-01-04", 3)],
            [quote("204001.SH", "2010-01-04")] * 2000,
        ]
        for rows in malformed:
            with self.subTest(rows=str(rows)[:120]):
                with self.assertRaises(SyncWarning):
                    self.fetch(rows)

    def test_recent_days_are_not_proved_or_requested_but_seven_day_boundary_is_allowed(self):
        with patch("app.services.data_sync.tushare_call", return_value=[quote("204001.SH", "2026-10-14")]) as fetch:
            result = fetch_verified_repo_absences("token", "204014", "2026-10-14", "2026-10-22", {"2026-10-14", "2026-10-15", "2026-10-21", "2026-10-22"})
        self.assertEqual(result, {"2026-10-14"})
        self.assertEqual(fetch.call_args.args[2], {"start_date": "20261014", "end_date": "20261014"})
        with patch("app.services.data_sync.tushare_call") as fetch:
            self.assertEqual(fetch_verified_repo_absences("token", "204014", "2026-10-15", "2026-10-22", {"2026-10-15"}), set())
            fetch.assert_not_called()

    def test_one_day_contract_empty_requests_and_unsupported_symbols_are_never_exempted(self):
        with patch("app.services.data_sync.tushare_call") as fetch:
            self.assertEqual(fetch_verified_repo_absences("token", "204001", "2010-01-04", "2010-01-15", {"2010-01-04"}), set())
            self.assertEqual(fetch_verified_repo_absences("token", "204014", "2010-01-04", "2010-01-15", set()), set())
            with self.assertRaises(SyncWarning):
                fetch_verified_repo_absences("token", "BAD", "2010-01-04", "2010-01-15", {"2010-01-04"})
            with self.assertRaises(SyncWarning):
                fetch_verified_repo_absences("token", "204014", "bad", "2010-01-15", {"2010-01-04"})
            fetch.assert_not_called()

    def test_snapshots_are_unfiltered_and_chunked_at_thirty_calendar_days(self):
        expected = {"2010-01-04", "2010-02-08", "2010-03-08"}
        def response(_token, api, params, fields):
            self.assertEqual(api, "repo_daily")
            self.assertNotIn("ts_code", params)
            first = datetime.strptime(params["start_date"], "%Y%m%d").date()
            last = datetime.strptime(params["end_date"], "%Y%m%d").date()
            self.assertLessEqual((last - first).days, 29)
            self.assertEqual(fields, "ts_code,trade_date,close")
            return [quote("204001.SH", day) for day in expected if first <= date.fromisoformat(day) <= last]
        with patch("app.services.data_sync.tushare_call", side_effect=response) as fetch:
            result = fetch_verified_repo_absences("token", "204014", "2010-01-01", "2010-03-08", expected)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(result, expected)

    def test_any_chunk_failure_prevents_partial_absence_results(self):
        failures = [SyncWarning("upstream failure"), RuntimeError("transport failure"), [], [quote("204001.SH", "2010-02-08", float("nan"))]]
        for failure in failures:
            with self.subTest(failure=str(failure)), patch(
                "app.services.data_sync.tushare_call", side_effect=[[quote("204001.SH", "2010-01-04")], failure]
            ) as fetch:
                with self.assertRaises(SyncWarning):
                    fetch_verified_repo_absences("token", "204014", "2010-01-01", "2010-02-08", {"2010-01-04", "2010-02-08"})
                self.assertEqual(fetch.call_count, 2)

    def test_reader_only_accepts_trusted_proofs_and_real_quotes_override_them(self):
        path = temp_db_path()
        init_db(path)
        with db_session(path) as conn:
            mark_sync_coverage(conn, REPO_ABSENCE_KIND, "204014", "2010-01-04", "2010-01-07", REPO_ABSENCE_SOURCE)
            conn.execute("INSERT INTO sync_coverage VALUES(?,?,?,?,?,?)", (REPO_ABSENCE_KIND, "204014", "2010-01-08", "2010-01-08", "untrusted:source", "test"))
            mark_sync_coverage(conn, "repo_rates", "204014", "2010-01-11", "2010-01-11", REPO_ABSENCE_SOURCE)
            mark_sync_coverage(conn, REPO_ABSENCE_KIND, "204002", "2010-01-12", "2010-01-12", REPO_ABSENCE_SOURCE)
            insert_many(conn, "repo_rates", [
                {"symbol": "204014", "trade_date": "2010-01-05", "close_rate": 2.0, "source": "tushare:repo_daily"},
                {"symbol": "204014", "trade_date": "2010-01-06", "close_rate": 2.0, "source": "generated:invalid"},
            ])
            self.assertEqual(verified_repo_absence_dates(conn, "204014", "2010-01-04", "2010-01-15"), {"2010-01-04", "2010-01-06", "2010-01-07"})
            self.assertEqual(verified_repo_absence_dates(conn, "204014", "2010-01-06", "2010-01-06"), {"2010-01-06"})

    def test_reader_excludes_recent_dates_one_day_contract_and_malformed_proofs(self):
        path = temp_db_path()
        init_db(path)
        with db_session(path) as conn:
            for symbol in ("204001", "204014"):
                mark_sync_coverage(conn, REPO_ABSENCE_KIND, symbol, "2026-10-13", "2026-10-21", REPO_ABSENCE_SOURCE)
            conn.execute("INSERT INTO sync_coverage VALUES(?,?,?,?,?,?)", (REPO_ABSENCE_KIND, "204014", "2026-10-01", "invalid", REPO_ABSENCE_SOURCE, "test"))
            self.assertEqual(verified_repo_absence_dates(conn, "204014", "2026-10-01", "2026-10-21"), {"2026-10-13", "2026-10-14"})
            self.assertEqual(verified_repo_absence_dates(conn, "204014", "2026-10-15", "2026-10-21"), set())
            self.assertEqual(verified_repo_absence_dates(conn, "204001", "2026-10-01", "2026-10-21"), set())
            self.assertEqual(verified_repo_absence_dates(conn, "BAD", "2026-10-01", "2026-10-21"), set())
