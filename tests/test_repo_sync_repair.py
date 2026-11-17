from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
import sqlite3
import subprocess
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.config import normalize_config
from app.db import SCHEMA, insert_many
from app.services.data_sync import (
    mark_sync_coverage, required_data_missing, run_curl_with_optional_direct_retry, sync_all,
)
from app.services.repo_history import verified_repo_absence_dates
from tests.helpers import fixture_repo_rates, seed_fixture_data


REPO_SOURCES = (
    "fetch_datasrc_repo_rates",
    "fetch_tushare_repo_rates",
    "fetch_sohu_repo_rates",
    "fetch_eastmoney_repo_rates",
)
ABSENCE_SOURCE = "tushare:repo_daily:market_snapshot"


class RepoSyncRepairTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.config = self.fixture("2010-01-04", "2010-01-08")

    def fixture(self, start, end):
        config = normalize_config({"start_date": start, "end_date": end, "repo_symbol": "204014"})
        for asset in config["assets"]:
            asset["enabled"] = False
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript(SCHEMA)
        seed_fixture_data(conn, config, start, end)
        insert_many(conn, "repo_rates", fixture_repo_rates(start, end, "204001"))
        return conn, config

    def row(self, day, rate=1.0, *, symbol="204014", source="test:repo"):
        return {
            "symbol": symbol, "trade_date": day,
            "open_rate": rate, "close_rate": rate, "high_rate": rate, "low_rate": rate,
            "volume": 0.0, "amount": 0.0, "source": source,
        }

    def missing(self):
        return required_data_missing(
            self.conn, self.config["start_date"], self.config["end_date"], self.config["assets"], "204014",
        )

    def sync(self, symbol="204014"):
        return sync_all(
            self.conn, "test-token", self.config["start_date"], self.config["end_date"],
            self.config["assets"], repo_symbol="204014", missing_items=[f"repo_rates:{symbol}"],
        )

    @contextmanager
    def sources(self, responses=None, *, absences=()):
        responses = responses or {}
        calls = []
        with ExitStack() as stack:
            mocks = {}
            for name in REPO_SOURCES:
                def fetch(*_args, source=name, **_kwargs):
                    calls.append(source)
                    return responses.get(source, [])
                mocks[name] = stack.enter_context(patch(f"app.services.data_sync.{name}", side_effect=fetch))
            def verify_absence(*args):
                proof.expected_date_snapshots.append(frozenset(args[4]))
                return set(absences)
            proof = stack.enter_context(patch(
                "app.services.data_sync.fetch_verified_repo_absences", side_effect=verify_absence,
            ))
            proof.expected_date_snapshots = []
            yield calls, mocks, proof

    def mark_absent(self, day, symbol="204014", *, source=ABSENCE_SOURCE):
        mark_sync_coverage(self.conn, "repo_no_quote", symbol, day, day, source)

    def test_partial_sources_fill_remaining_dates_without_overwriting_priority(self):
        self.conn.execute("DELETE FROM repo_rates WHERE symbol='204014'")
        dates = [f"2010-01-{day:02d}" for day in range(4, 9)]
        responses = {
            name: [self.row(day, idx + 1.0, source=f"test:source-{idx}")]
            for idx, (name, day) in enumerate(zip(REPO_SOURCES, dates))
        }
        responses[REPO_SOURCES[1]].insert(0, self.row(dates[0], 99.0, source="test:lower-priority"))
        responses[REPO_SOURCES[-1]].append(self.row(dates[-1], 5.0))
        with self.sources(responses) as (calls, _mocks, proof):
            result = self.sync()
        rows = [dict(row) for row in self.conn.execute(
            "SELECT trade_date,close_rate,source FROM repo_rates WHERE symbol='204014' ORDER BY trade_date"
        )]
        self.assertEqual(calls, list(REPO_SOURCES))
        self.assertEqual([row["trade_date"] for row in rows], dates)
        self.assertEqual([row["close_rate"] for row in rows], [1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual(rows[0]["source"], "test:source-0")
        self.assertEqual(result["inserted"]["repo_rates"], 5)
        self.assertNotIn("repo_rates:204014", result["missing_data"])
        self.assertNotIn("repo_rates:204014", self.missing())
        self.assertEqual(proof.expected_date_snapshots, [frozenset(dates[2:])])

    def test_sparse_gaps_are_batched_without_overwriting_cached_rates(self):
        missing_days = ["2010-01-04", "2010-01-06", "2010-01-08"]
        self.conn.execute("DELETE FROM repo_rates WHERE symbol='204014' AND trade_date IN (?,?,?)", missing_days)
        rows = [self.row(day, rate=9.0) for day in (*missing_days, "2010-01-07")]
        with self.sources({"fetch_tushare_repo_rates": rows}) as (calls, mocks, proof):
            result = self.sync()
        self.assertEqual(calls, list(REPO_SOURCES[:2]))
        mocks["fetch_tushare_repo_rates"].assert_called_once_with("test-token", "204014", missing_days[0], missing_days[-1])
        proof.assert_not_called()
        self.assertEqual(result["inserted"]["repo_rates"], 3)
        self.assertNotIn("repo_rates:204014", self.missing())
        cached_rate = self.conn.execute("SELECT close_rate FROM repo_rates WHERE symbol='204014' AND trade_date='2010-01-07'").fetchone()[0]
        self.assertNotEqual(cached_rate, 9.0)

    def test_repo_timeout_stops_before_transport_retry(self):
        self.conn.execute("DELETE FROM repo_rates WHERE symbol='204014' AND trade_date IN ('2010-01-06','2010-01-08')")
        clock = [0.0]

        def expire_curl(cmd, *, timeout, **_kwargs):
            self.assertEqual(timeout, 1.0)
            clock[0] = 1.0
            raise subprocess.TimeoutExpired(cmd, timeout)

        cached_quote = self.row("2010-01-06")
        with self.sources({"fetch_datasrc_repo_rates": [cached_quote]}) as (_calls, mocks, proof), \
                patch("app.services.data_sync.REPO_SYNC_TIMEOUT_SECONDS", 1), \
                patch("app.services.data_sync.time.monotonic", side_effect=lambda: clock[0]), \
                patch("app.services.data_sync.subprocess.run", side_effect=expire_curl) as curl:
            mocks["fetch_tushare_repo_rates"].side_effect = lambda *_args: run_curl_with_optional_direct_retry(["curl", "-sS"])
            result = self.sync()
        curl.assert_called_once()
        self.assertEqual(result["inserted"]["repo_rates"], 1)
        self.assertIn("repo_rates:204014", result["missing_data"])
        self.assertIn("逆回购行情补录已达到 1 秒时限", result["warnings"])
        mocks["fetch_sohu_repo_rates"].assert_not_called()
        proof.assert_not_called()

    def test_wrong_symbol_dates_and_nonfinite_ohlc_do_not_block_later_sources(self):
        self.conn.execute("DELETE FROM repo_rates WHERE symbol='204014'")
        invalid = [
            self.row("2010-01-04", 99.0, symbol="204007"),
            self.row("2010-01-01", 99.0),
            self.row("2010-01-09", 99.0),
            self.row("2010-01-04", float("nan")),
            {**self.row("2010-01-05"), "open_rate": None},
            {**self.row("2010-01-06"), "high_rate": float("inf")},
            {**self.row("2010-01-07"), "low_rate": "invalid"},
            {**self.row("2010-01-08"), "close_rate": None},
        ]
        good = [self.row(f"2010-01-{day:02d}", 2.0, source="test:valid-fallback") for day in range(4, 9)]
        with self.sources({REPO_SOURCES[0]: invalid, REPO_SOURCES[1]: good}) as (calls, _mocks, proof):
            result = self.sync()
        self.assertEqual(calls, list(REPO_SOURCES[:2]))
        self.assertEqual(result["inserted"]["repo_rates"], 5)
        self.assertNotIn("repo_rates:204014", result["missing_data"])
        rows = list(self.conn.execute("SELECT * FROM repo_rates WHERE symbol='204014'"))
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(row["source"] == "test:valid-fallback" for row in rows))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM repo_rates WHERE symbol='204007'").fetchone()[0], 0)
        proof.assert_not_called()

    def test_partial_quotes_still_report_unverified_missing_dates(self):
        self.conn.execute("DELETE FROM repo_rates WHERE symbol='204014'")
        partial = [self.row("2010-01-04"), self.row("2010-01-05")]
        with self.sources({REPO_SOURCES[0]: partial}) as (calls, _mocks, proof):
            result = self.sync()
        self.assertEqual(calls, list(REPO_SOURCES))
        self.assertEqual(result["inserted"]["repo_rates"], 2)
        self.assertIn("repo_rates:204014", result["missing_data"])
        self.assertIn("repo_rates:204014", self.missing())
        proof.assert_called_once()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM repo_rates WHERE symbol='204014'").fetchone()[0], 2)

    def test_verified_absence_is_recorded_without_inventing_a_rate(self):
        self.conn.execute("DELETE FROM repo_rates WHERE symbol='204014' AND trade_date='2010-01-06'")
        with self.sources(absences={"2010-01-06"}) as (_calls, _mocks, proof):
            result = self.sync()
        proof.assert_called_once()
        self.assertEqual(proof.call_args.args[:4], ("test-token", "204014", "2010-01-06", "2010-01-06"))
        self.assertEqual(proof.expected_date_snapshots, [frozenset({"2010-01-06"})])
        self.assertEqual(result["inserted"]["repo_rates"], 0)
        self.assertEqual(result["inserted"]["repo_quote_absences"], 1)
        self.assertNotIn("repo_rates:204014", result["missing_data"])
        self.assertNotIn("repo_rates:204014", self.missing())
        self.assertIsNone(self.conn.execute(
            "SELECT * FROM repo_rates WHERE symbol='204014' AND trade_date='2010-01-06'"
        ).fetchone())
        marker = self.conn.execute(
            "SELECT kind,symbol,start_date,end_date,source FROM sync_coverage WHERE kind='repo_no_quote'"
        ).fetchone()
        self.assertEqual(dict(marker), {
            "kind": "repo_no_quote", "symbol": "204014", "start_date": "2010-01-06",
            "end_date": "2010-01-06", "source": ABSENCE_SOURCE,
        })

    def test_internal_long_tenor_gap_without_verified_marker_is_blocked(self):
        self.conn.execute("DELETE FROM repo_rates WHERE symbol='204014' AND trade_date='2010-01-06'")
        self.assertIn("repo_rates:204014", self.missing())
        self.assertNotIn("repo_rates:204001", self.missing())

    def test_new_proof_does_not_upgrade_neighboring_untrusted_absence_dates(self):
        self.conn.execute(
            "DELETE FROM repo_rates WHERE symbol='204014' AND trade_date BETWEEN '2010-01-05' AND '2010-01-07'"
        )
        mark_sync_coverage(
            self.conn, "repo_no_quote", "204014", "2010-01-05", "2010-01-07", "test:unverified-provider",
        )
        with self.sources(absences={"2010-01-06"}):
            result = self.sync()
        trusted = verified_repo_absence_dates(self.conn, "204014", "2010-01-04", "2010-01-08")
        self.assertEqual(trusted, {"2010-01-06"})
        self.assertEqual(result["inserted"]["repo_quote_absences"], 1)
        self.assertIn("repo_rates:204014", result["missing_data"])
        self.assertIn("repo_rates:204014", self.missing())
        marker = self.conn.execute(
            "SELECT start_date,end_date FROM sync_coverage WHERE kind='repo_no_quote' AND source=?",
            (ABSENCE_SOURCE,),
        ).fetchall()
        self.assertEqual([tuple(row) for row in marker], [("2010-01-06", "2010-01-06")])

    def test_verified_historical_marker_allows_internal_gap_with_real_one_day_rate(self):
        self.conn.execute("DELETE FROM repo_rates WHERE symbol='204014' AND trade_date='2010-01-06'")
        self.mark_absent("2010-01-06")
        self.assertNotIn("repo_rates:204014", self.missing())
        self.assertNotIn("repo_rates:204001", self.missing())

    def test_wrong_marker_source_cannot_waive_an_internal_gap(self):
        self.conn.execute("DELETE FROM repo_rates WHERE symbol='204014' AND trade_date='2010-01-06'")
        self.mark_absent("2010-01-06", source="test:unverified-empty-provider")
        self.assertIn("repo_rates:204014", self.missing())

    def test_one_day_first_session_gap_is_strict_even_with_a_marker(self):
        self.conn.execute("DELETE FROM repo_rates WHERE symbol='204001' AND trade_date='2010-01-04'")
        self.assertIn("repo_rates:204001", self.missing())
        self.mark_absent("2010-01-04", symbol="204001")
        self.assertIn("repo_rates:204001", self.missing())

    def test_long_tenor_marker_never_waives_missing_one_day_fallback(self):
        self.conn.execute("DELETE FROM repo_rates WHERE trade_date='2010-01-06'")
        self.mark_absent("2010-01-06")
        self.mark_absent("2010-01-06", symbol="204001")
        self.assertIn("repo_rates:204001", self.missing())

    def test_one_day_sync_never_uses_no_quote_verification(self):
        self.conn.execute("DELETE FROM repo_rates WHERE symbol='204001' AND trade_date='2010-01-06'")
        with self.sources(absences={"2010-01-06"}) as (_calls, _mocks, proof):
            result = self.sync("204001")
        self.assertIn("repo_rates:204001", result["missing_data"])
        proof.assert_not_called()

    def test_recent_tail_remains_missing_even_with_an_apparently_verified_marker(self):
        tail = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
        while tail.weekday() >= 5:
            tail -= timedelta(days=1)
        start = tail - timedelta(days=14)
        self.conn, self.config = self.fixture(start.isoformat(), tail.isoformat())
        self.conn.execute(
            "DELETE FROM repo_rates WHERE symbol='204014' AND trade_date=?", (tail.isoformat(),),
        )
        self.mark_absent(tail.isoformat())
        self.assertIn("repo_rates:204014", self.missing())
        self.assertNotIn("repo_rates:204001", self.missing())


if __name__ == "__main__":
    unittest.main()
