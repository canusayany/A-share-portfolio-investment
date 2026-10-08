from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import json
import math
import time
from threading import Thread
import unittest
from unittest.mock import patch
import uuid

from app.config import normalize_config
from app.db import db_session, insert_many, json_dumps
from app.main import backfill_market_capture_metrics, columnar_chart_payload, create_server
from app.services.backtest_engine import BACKTEST_ENGINE_VERSION, market_capture_metrics
from tests.helpers import build_synced_db, temp_db_path
from tests.test_api import http_json


MONTHLY_FIELDS = (
    "up_market_strategy_monthly_return", "up_market_benchmark_monthly_return",
    "down_market_strategy_monthly_return", "down_market_benchmark_monthly_return",
)


class MarketCaptureMetricsTests(unittest.TestCase):
    def test_negative_upside_capture_shows_portfolio_loss_in_rising_market(self):
        metrics = market_capture_metrics(["2020-01-31", "2020-02-28"], [0, -.02], [100, 110])
        self.assertEqual(metrics["up_market_months"], 1)
        self.assertAlmostEqual(metrics["up_market_strategy_monthly_return"], -.02)
        self.assertAlmostEqual(metrics["up_market_benchmark_monthly_return"], .1)
        self.assertAlmostEqual(metrics["upside_capture_ratio"], (.98 ** 12 - 1) / (1.1 ** 12 - 1))
        self.assertLess(metrics["upside_capture_ratio"], 0)
        self.assertIsNone(metrics["down_market_strategy_monthly_return"])

    def test_negative_downside_capture_shows_portfolio_gain_in_falling_market(self):
        metrics = market_capture_metrics(["2020-01-31", "2020-02-28"], [0, .03], [100, 90])
        self.assertEqual(metrics["down_market_months"], 1)
        self.assertAlmostEqual(metrics["down_market_strategy_monthly_return"], .03)
        self.assertAlmostEqual(metrics["down_market_benchmark_monthly_return"], -.1)
        self.assertAlmostEqual(metrics["downside_capture_ratio"], (1.03 ** 12 - 1) / (.9 ** 12 - 1))
        self.assertLess(metrics["downside_capture_ratio"], 0)
        self.assertIsNone(metrics["up_market_strategy_monthly_return"])

    def test_group_returns_are_geometric_and_capture_formula_is_unchanged(self):
        metrics = market_capture_metrics(
            ["2020-01-31", "2020-02-28", "2020-03-31", "2020-04-30", "2020-05-29"],
            [0, .1, -.05, -.04, .02], [100, 110, 132, 118.8, 95.04],
        )
        self.assertAlmostEqual(metrics["up_market_strategy_monthly_return"], (1.1 * .95) ** .5 - 1)
        self.assertAlmostEqual(metrics["up_market_benchmark_monthly_return"], (1.1 * 1.2) ** .5 - 1)
        self.assertAlmostEqual(metrics["down_market_strategy_monthly_return"], (.96 * 1.02) ** .5 - 1)
        self.assertAlmostEqual(metrics["down_market_benchmark_monthly_return"], (.9 * .8) ** .5 - 1)
        self.assertAlmostEqual(metrics["upside_capture_ratio"], ((1.1 * .95) ** 6 - 1) / ((1.1 * 1.2) ** 6 - 1))
        self.assertAlmostEqual(metrics["downside_capture_ratio"], ((.96 * 1.02) ** 6 - 1) / ((.9 * .8) ** 6 - 1))
        self.assertNotAlmostEqual(metrics["up_market_strategy_monthly_return"], (.1 - .05) / 2)

    def test_empty_single_endpoint_flat_and_missing_samples_return_null(self):
        cases = [([], [], []), (["2020-01-31"], [.1], [100]),
                 (["2020-01-31", "2020-02-28"], [0, .1], [100, 100]),
                 (["2020-01-31", "2020-02-28"], [0, .1], [None, None])]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                metrics = market_capture_metrics(*arguments)
                for field in (*MONTHLY_FIELDS, "upside_capture_ratio", "downside_capture_ratio"):
                    self.assertIsNone(metrics[field])
                self.assertEqual((metrics["up_market_months"], metrics["down_market_months"]), (0, 0))

    def test_tiny_returns_use_existing_group_threshold_and_remain_finite(self):
        dates = ["2020-01-31", "2020-02-28"]
        tiny = market_capture_metrics(dates, [0, 1e-12], [1, 1 + 2e-12])
        self.assertEqual(tiny["up_market_months"], 1)
        self.assertTrue(math.isfinite(tiny["upside_capture_ratio"]))
        self.assertAlmostEqual(tiny["up_market_benchmark_monthly_return"], 2e-12, delta=1e-15)
        flat = market_capture_metrics(dates, [0, 1e-12], [1, 1 + 1e-13])
        self.assertEqual(flat["up_market_months"], 0)
        self.assertIsNone(flat["up_market_strategy_monthly_return"])


class HistoricalMarketCaptureApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = temp_db_path()
        cls.server = create_server(port=0, db_path=cls.db_path)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.url = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def save_run(self, observations: list[dict], summary: dict | None = None):
        run_id = str(uuid.uuid4())
        summary = {"engine_version": BACKTEST_ENGINE_VERSION - 1, "upside_capture_ratio": 123, "downside_capture_ratio": 456, "up_market_months": 99, **(summary or {})}
        original_json = json_dumps(summary)
        rows = [
            {"run_id": run_id, "total_asset_cny": 1_000, "flow_cny": 0, "daily_return": 0,
             "cumulative_return": 0, "drawdown": 0, "benchmark_return": None, "payload_json": "{}", **item}
            for item in observations
        ]
        with db_session(self.db_path) as conn:
            conn.execute("INSERT INTO backtest_runs(run_id,created_at,config_json,summary_json) VALUES(?,?,?,?)",
                         (run_id, "2020-01-01", json_dumps(normalize_config({})), original_json))
            insert_many(conn, "portfolio_daily", rows)
        return run_id, original_json, rows

    def assert_saved_unchanged(self, run_id, original_json):
        with db_session(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT summary_json FROM backtest_runs WHERE run_id=?", (run_id,)).fetchone()[0], original_json)

    def test_history_get_uses_all_daily_returns_and_does_not_write_saved_summary(self):
        observations = []
        day, nav, benchmark = date(2020, 1, 1), 1.0, 100.0
        for index in range(1105):
            daily_return = .001 if index % 3 else -.0003
            nav *= 1 + daily_return
            benchmark *= 1.0008 if day.month % 2 else .9995
            observations.append({"trade_date": day.isoformat(), "daily_return": daily_return, "cumulative_return": nav - 1,
                                 "benchmark_return": benchmark / 100 - 1, "payload_json": json_dumps({"benchmark_value": benchmark})})
            day += timedelta(days=1)
        run_id, original_json, rows = self.save_run(observations)
        expected = market_capture_metrics([row["trade_date"] for row in rows], [row["daily_return"] for row in rows], [json.loads(row["payload_json"])["benchmark_value"] for row in rows])
        chart = columnar_chart_payload(rows)
        self.assertLess(chart["display_points"], len(rows))
        sampled = market_capture_metrics(chart["dates"], chart["daily_returns"], [1 + value for value in chart["benchmark_returns"]])
        self.assertNotAlmostEqual(sampled[MONTHLY_FIELDS[0]], expected[MONTHLY_FIELDS[0]])
        with patch("app.main.market_capture_metrics", wraps=market_capture_metrics) as calculate:
            detail = http_json(f"{self.url}/api/backtest/{run_id}")
        self.assertEqual(len(calculate.call_args.args[0]), 1105)
        for key, value in expected.items():
            self.assertEqual(detail["summary"][key], value)
        self.assertTrue(detail["summary"]["requires_recalculation"])
        self.assert_saved_unchanged(run_id, original_json)

    def test_payload_prices_take_precedence_and_explicit_missing_is_not_zero(self):
        run_id, original_json, _ = self.save_run([
            {"trade_date": "2020-01-31", "benchmark_return": 0, "payload_json": '{"benchmark_value":null}'},
            {"trade_date": "2020-02-28", "benchmark_return": 0, "payload_json": '{"benchmark_value":100}'},
            {"trade_date": "2020-03-31", "daily_return": -.02, "benchmark_return": 0, "payload_json": '{"benchmark_value":110}'},
        ])
        summary = http_json(f"{self.url}/api/backtest/{run_id}")["summary"]
        self.assertEqual(summary["up_market_months"], 1)
        self.assertAlmostEqual(summary["up_market_benchmark_monthly_return"], .1)
        self.assertAlmostEqual(summary["up_market_strategy_monthly_return"], -.02)
        self.assert_saved_unchanged(run_id, original_json)

    def test_legacy_normalized_benchmark_preserves_missing_leading_observation(self):
        run_id, _, _ = self.save_run([
            {"trade_date": "2020-01-31", "benchmark_return": None},
            {"trade_date": "2020-02-28", "benchmark_return": .1},
            {"trade_date": "2020-03-31", "daily_return": .02, "benchmark_return": .21},
        ])
        summary = http_json(f"{self.url}/api/backtest/{run_id}")["summary"]
        self.assertEqual(summary["up_market_months"], 1)
        self.assertAlmostEqual(summary["up_market_benchmark_monthly_return"], .1)
        self.assertAlmostEqual(summary["up_market_strategy_monthly_return"], .02)

    def test_explicit_all_missing_benchmark_does_not_use_zero_placeholders(self):
        run_id, _, _ = self.save_run([
            {"trade_date": "2020-01-31", "benchmark_return": 0, "payload_json": '{"benchmark_value":null}'},
            {"trade_date": "2020-02-28", "benchmark_return": .1, "payload_json": '{"benchmark_value":null}'},
        ])
        summary = http_json(f"{self.url}/api/backtest/{run_id}")["summary"]
        self.assertEqual(summary["up_market_months"], 0)
        self.assertTrue(all(summary[field] is None for field in MONTHLY_FIELDS))

    def test_already_present_metrics_do_not_recalculate_even_if_null(self):
        summary = {field: None for field in MONTHLY_FIELDS}
        original = deepcopy(summary)
        with db_session(self.db_path) as conn, patch("app.main.market_capture_metrics", side_effect=AssertionError("unexpected calculation")):
            result = backfill_market_capture_metrics(conn, "not-needed", summary)
        self.assertEqual(result, original)
        self.assertEqual(summary, original)


class CachedMarketCaptureApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.config = build_synced_db("2020-01-02", "2020-03-31")
        cls.server = create_server(port=0, db_path=cls.db_path)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.url = f"http://{host}:{port}"
        fresh = http_json(f"{cls.url}/api/backtest/run", {"config": cls.config})
        cls.run_id = fresh["run_id"]
        cls.expected = {key: fresh["summary"][key] for key in (*MONTHLY_FIELDS, "upside_capture_ratio", "downside_capture_ratio", "up_market_months", "down_market_months")}
        with db_session(cls.db_path) as conn:
            legacy_summary = json.loads(conn.execute("SELECT summary_json FROM backtest_runs WHERE run_id=?", (cls.run_id,)).fetchone()[0])
            for field in MONTHLY_FIELDS:
                legacy_summary.pop(field)
            # This is an existing version-52 cache, not an engine migration.
            legacy_summary["engine_version"] = BACKTEST_ENGINE_VERSION
            cls.legacy_json = json_dumps(legacy_summary)
            conn.execute("UPDATE backtest_runs SET summary_json=? WHERE run_id=?", (cls.legacy_json, cls.run_id))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def assert_response_and_snapshot(self, result):
        self.assertTrue(result["cache"]["hit"])
        self.assertEqual(result["run_id"], self.run_id)
        for field, expected in self.expected.items():
            self.assertEqual(result["summary"][field], expected)
        with db_session(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT summary_json FROM backtest_runs WHERE run_id=?", (self.run_id,)).fetchone()[0], self.legacy_json)

    def test_synchronous_cache_response_backfills_without_engine_or_snapshot_write(self):
        with patch("app.main.run_backtest", side_effect=AssertionError("cache must not rerun engine")):
            result = http_json(f"{self.url}/api/backtest/run", {"config": self.config})
        self.assert_response_and_snapshot(result)

    def test_async_job_cache_response_backfills_without_detail_get_or_snapshot_write(self):
        with patch("app.main.run_backtest", side_effect=AssertionError("cache must not rerun engine")):
            job = http_json(f"{self.url}/api/backtest/start", {"config": self.config, "client_request_id": str(uuid.uuid4())})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = http_json(f"{self.url}/api/backtest/jobs/{job['job_id']}")
                if state["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(.01)
            self.assertEqual(state["status"], "completed", state)
        self.assert_response_and_snapshot(state["result"])
