from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
from threading import Thread
import time
import unittest
from urllib import error, request

import app.main as main_module
from app.db import db_session, json_dumps
from app.identity import IDENTITY_COOKIE_NAME, leaderboard_key_id
from app.main import create_server, execute_backtest_request
from app.services.backtest_engine import BACKTEST_ENGINE_VERSION
from tests.helpers import build_synced_db
from tests.test_api import http_json


class UiApiClosureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_path, cls.config = build_synced_db("2020-01-01", "2020-02-28")
        cls.server = create_server(port=0, db_path=cls.db_path)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.url = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.job_executor.shutdown(wait=True)
        cls.server.analysis_executor.shutdown(wait=True)
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def call(self, path, payload=None, method=None, headers=None):
        return http_json(self.url + path, payload, method, headers)

    def wait_for_job(self, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.call(f"/api/backtest/jobs/{job_id}")
            if job["status"] == "completed":
                return job["result"]
            self.assertNotIn(job["status"], {"failed", "cancelled"}, job)
            time.sleep(0.03)
        self.fail("backtest job did not finish")

    def wait_for_analysis(self, run_id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            entry = self.call(f"/api/backtest/{run_id}")
            status = entry["summary"].get("analysis_status")
            if status == "completed":
                return entry
            self.assertNotEqual(status, "failed", entry["summary"].get("analysis_error"))
            time.sleep(0.03)
        self.fail("extended analysis did not finish")

    def pending_run(self, suffix):
        config = copy.deepcopy(self.config)
        config["initial_capital_cny"] += suffix
        config["rebalance_month_analysis_enabled"] = True
        result = execute_backtest_request(
            self.server.settings, self.server.write_lock, config,
            defer_extended_analysis=True,
        )
        self.assertEqual(result["summary"]["analysis_status"], "pending")
        return config, result

    def test_concurrent_submission_retries_create_one_job(self):
        payload = {"config": self.config, "client_request_id": "closure-concurrent"}
        with ThreadPoolExecutor(max_workers=12) as executor:
            jobs = list(executor.map(lambda _: self.call("/api/backtest/start", payload), range(12)))
        self.assertEqual(len({job["job_id"] for job in jobs}), 1)
        self.wait_for_job(jobs[0]["job_id"])

    def test_request_id_does_not_silently_reuse_different_parameters(self):
        payload = {"config": self.config, "client_request_id": "closure-changed"}
        first = self.call("/api/backtest/start", payload)
        changed = copy.deepcopy(payload)
        changed["config"]["rebalance_frequency"] = "monthly"
        with self.assertRaises(error.HTTPError) as raised:
            self.call("/api/backtest/start", changed)
        self.assertEqual(raised.exception.code, 409)
        self.wait_for_job(first["job_id"])

    def test_same_request_id_is_independent_between_keys(self):
        payload = {"config": self.config, "client_request_id": "closure-keys"}
        keys = [leaderboard_key_id("closure-one"), leaderboard_key_id("closure-two")]
        jobs = [self.call("/api/backtest/start", payload, headers={"Cookie": f"{IDENTITY_COOKIE_NAME}={key}"}) for key in keys]
        self.assertNotEqual(jobs[0]["job_id"], jobs[1]["job_id"])
        results = [self.wait_for_job(job["job_id"]) for job in jobs]
        self.assertEqual(results[0]["run_id"], results[1]["run_id"])
        with db_session(self.db_path) as conn:
            for key in keys:
                self.assertIsNotNone(conn.execute("SELECT 1 FROM leaderboard_memberships WHERE key_id=? AND run_id=?", (key, results[0]["run_id"])).fetchone())

    def test_reopening_interrupted_analysis_resumes_it(self):
        _config, result = self.pending_run(11)
        entry = self.wait_for_analysis(result["run_id"])
        self.assertEqual(len(entry["summary"]["rebalance_month_scenarios"]), 12)
        self.assertEqual(entry["run_id"], result["run_id"])

    def test_failed_analysis_can_retry_without_recreating_main_result(self):
        _config, result = self.pending_run(12)
        main_module.update_deferred_analysis_status(self.server, result["run_id"], "failed", "temporary source failure")
        response = self.call(f"/api/backtest/{result['run_id']}/analysis", {})
        self.assertEqual(response["analysis_status"], "pending")
        entry = self.wait_for_analysis(result["run_id"])
        self.assertIsNone(entry["summary"]["analysis_error"])
        self.assertEqual(len(entry["summary"]["rebalance_month_scenarios"]), 12)

    def test_synchronous_request_finishes_partial_cached_analysis(self):
        config, pending = self.pending_run(13)
        completed = self.call("/api/backtest/run", {"config": config})
        self.assertEqual(completed["run_id"], pending["run_id"])
        self.assertTrue(completed["cache"]["hit"])
        self.assertFalse(completed["analysis_pending"])
        self.assertEqual(completed["summary"]["analysis_status"], "completed")
        self.assertEqual(len(completed["summary"]["rebalance_month_scenarios"]), 12)

    def test_old_engine_analysis_must_be_recalculated(self):
        _config, result = self.pending_run(14)
        with db_session(self.db_path) as conn:
            summary = result["summary"]
            summary["engine_version"] = BACKTEST_ENGINE_VERSION - 1
            conn.execute("UPDATE backtest_runs SET summary_json=? WHERE run_id=?", (json_dumps(summary), result["run_id"]))
        with self.assertRaises(error.HTTPError) as raised:
            self.call(f"/api/backtest/{result['run_id']}/analysis", {})
        self.assertEqual(raised.exception.code, 409)
        entry = self.call(f"/api/backtest/{result['run_id']}")
        self.assertTrue(entry["summary"]["requires_recalculation"])
        with self.assertRaises(error.HTTPError) as raised:
            self.call(f"/api/backtest/{result['run_id']}/strategy-diagnostics")
        self.assertEqual(raised.exception.code, 409)

    def test_invalid_json_and_config_are_client_errors(self):
        opener = request.build_opener(request.ProxyHandler({}))
        for body in (b"[]", b"null", b"{", b'{"config": []}', b'{"config": null}'):
            req = request.Request(self.url + "/api/backtest/start", data=body, headers={"Content-Type": "application/json"})
            with self.subTest(body=body), self.assertRaises(error.HTTPError) as raised:
                opener.open(req, timeout=10)
            self.assertEqual(raised.exception.code, 400)

    def test_retried_delete_remains_successful_and_removes_all_sections(self):
        config = {**self.config, "initial_capital_cny": self.config["initial_capital_cny"] + 15}
        result = self.call("/api/backtest/run", {"config": config})
        run_id = result["run_id"]
        self.server.diagnostics_cache[(run_id, "all")] = {"available": True}
        self.assertFalse(self.call(f"/api/backtest/{run_id}", method="DELETE")["already_deleted"])
        self.assertTrue(self.call(f"/api/backtest/{run_id}", method="DELETE")["already_deleted"])
        self.assertNotIn((run_id, "all"), self.server.diagnostics_cache)
        for section in ("", "/chart-series", "/rebalance", "/trades", "/daily-pnl", "/export.csv"):
            with self.subTest(section=section), self.assertRaises(error.HTTPError) as raised:
                self.call(f"/api/backtest/{run_id}{section}")
            self.assertEqual(raised.exception.code, 404)
        with db_session(self.db_path) as conn:
            for table in ("backtest_runs", "portfolio_daily", "trades", "rebalance_events", "leaderboard_memberships"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id=?", (run_id,)).fetchone()[0], 0)

    def test_rebalance_details_include_actual_execution_fields(self):
        result = self.call("/api/backtest/run", {"config": self.config})
        rows = self.call(f"/api/backtest/{result['run_id']}/rebalance")["rebalance"]
        self.assertTrue(rows)
        self.assertIn("total_asset_after", rows[0])
        self.assertIn("turnover_cny", rows[0])
        payload = {"event_type": "scheduled", "threshold_exceeded": False, "executed_trade_count": 0, "rebalanced": False, "rebalance_reason": "within_band"}
        self.assertEqual(main_module.rebalance_display_payload(payload), payload)


if __name__ == "__main__":
    unittest.main()
