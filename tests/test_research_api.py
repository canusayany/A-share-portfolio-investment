from __future__ import annotations

import copy
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch
from urllib import error

import app.main as main_module
from app.db import db_session, json_dumps
from app.identity import IDENTITY_COOKIE_NAME, leaderboard_key_id
from app.main import create_server, execute_backtest_request
from app.services.backtest_engine import BACKTEST_ENGINE_VERSION, BacktestCancelled
from tests.helpers import build_synced_db
from tests.test_api import http_json


class ResearchApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path, cls.config = build_synced_db("2020-01-01", "2020-03-31")
        cls.server = create_server(port=0, db_path=cls.db_path)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.url = f"http://{host}:{port}"
        cls.base = execute_backtest_request(cls.server.settings, cls.server.write_lock, cls.config)
        cls.key_a = leaderboard_key_id("research-a")
        cls.key_b = leaderboard_key_id("research-b")
        cls.key_c = leaderboard_key_id("research-c")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server.research_executor.shutdown(wait=True)
        cls.thread.join(timeout=5)

    def tearDown(self):
        with self.server.research_jobs_lock:
            ids = list(self.server.research_jobs)
        for job_id in ids:
            main_module.cancel_research_job(self.server, job_id)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.server.research_jobs_lock:
                if not any(job["_active"] for job in self.server.research_jobs.values()):
                    break
            time.sleep(.01)
        self.server.research_max_pending = main_module.MAX_RESEARCH_PENDING_JOBS

    def call(self, path, payload=None, key=None):
        return http_json(self.url + path, payload, headers={"Cookie": f"{IDENTITY_COOKIE_NAME}={key or self.key_a}"})

    def start(self, key=None, **overrides):
        return self.call("/api/research/start", {"run_id": self.base["run_id"], "kind": "rebalance_grid", "frequencies": ["monthly"], "bands": [0, .25], **overrides}, key)

    def poll(self, job_id, key=None):
        return self.call(f"/api/research/jobs/{job_id}", key=key)

    def wait(self, job_id, key=None):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = self.poll(job_id, key)
            if job["status"] in {"completed", "failed", "cancelled"}:
                return job
            time.sleep(.015)
        self.fail("research job did not finish")

    def database_counts(self):
        with db_session(self.db_path) as conn:
            return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("backtest_runs", "portfolio_daily", "trades", "rebalance_events", "leaderboard_memberships", "run_metadata")}

    def blocking_runner(self, entered, release, captured=None):
        def run(conn, base_config, payload, should_cancel=None, on_progress=None):
            if captured is not None:
                captured.append(copy.deepcopy(base_config))
            rows = [{"id": "first", "status": "success", "inputs": {"rebalance_band": 0}}]
            on_progress(1, payload["total"], rows)
            entered.set()
            while not release.wait(.01):
                if should_cancel():
                    raise BacktestCancelled("cancelled by test")
            if should_cancel():
                raise BacktestCancelled("cancelled by test")
            return {"kind": payload["kind"], "total": payload["total"], "completed": 1, "rows": rows, "methodology": {"test": True}}
        return run

    def test_real_research_uses_saved_snapshot_without_history_or_data_sync(self):
        before = self.database_counts()
        with db_session(self.db_path) as conn:
            saved = tuple(conn.execute("SELECT config_json,summary_json FROM backtest_runs WHERE run_id=?", (self.base["run_id"],)).fetchone())
        with patch.object(main_module, "sync_all", side_effect=AssertionError("research must never sync")):
            submitted = self.start(config={"initial_capital_cny": 123456})
            self.assertEqual(submitted["total"], 2)
            result = self.wait(submitted["job_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["completed"], 2)
        self.assertTrue(all(row["status"] == "success" for row in result["rows"]), result)
        self.assertTrue(all(row["initial_capital_cny"] == self.config["initial_capital_cny"] for row in result["rows"]))
        self.assertFalse(result["methodology"]["persist_backtests"])
        self.assertEqual(self.database_counts(), before)
        with db_session(self.db_path) as conn:
            self.assertEqual(tuple(conn.execute("SELECT config_json,summary_json FROM backtest_runs WHERE run_id=?", (self.base["run_id"],)).fetchone()), saved)

    def test_withdrawal_research_reports_real_cash_flow_scenarios(self):
        job = self.start(kind="withdrawal_stress", monthly_spends=[0, 5000], annual_growth_rates=[.03], start_years=[2020])
        result = self.wait(job["job_id"])
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(result["rows"]), 2)
        self.assertTrue(all(row["status"] == "success" for row in result["rows"]), result)
        spends = sorted(row["total_spend_cny"] for row in result["rows"])
        self.assertEqual(spends[0], 0)
        self.assertGreater(spends[1], 0)

    def test_every_scenario_uses_one_snapshot_during_concurrent_market_sync(self):
        cash_config = copy.deepcopy(self.config)
        for asset in cash_config["assets"]:
            asset["enabled"] = False
            asset["target_weight"] = 0
        cash_config["monthly_spend_cny"] = 0
        cash_config["repo_symbol"] = "204001"
        cash_run = execute_backtest_request(self.server.settings, self.server.write_lock, cash_config)
        with db_session(self.db_path) as conn:
            previous_rates = [tuple(row) for row in conn.execute("SELECT open_rate,close_rate,symbol,trade_date FROM repo_rates")]
        runner = main_module.run_research
        synced = Event()
        def run(conn, base_config, payload, should_cancel=None, on_progress=None):
            def progress(completed, total, rows):
                if completed == 1:
                    # A separate WAL writer commits between two real engine
                    # runs. Identical cash strategies must still compare on
                    # the original data, while the writer is free to finish.
                    with db_session(self.db_path) as writer:
                        writer.execute("UPDATE repo_rates SET open_rate=100,close_rate=100 WHERE symbol='204001'")
                    synced.set()
                on_progress(completed, total, rows)
            return runner(conn, base_config, payload, should_cancel=should_cancel, on_progress=progress)
        try:
            with patch.object(main_module, "run_research", run):
                result = self.wait(self.start(run_id=cash_run["run_id"])["job_id"])
            self.assertTrue(synced.is_set())
            self.assertEqual(result["status"], "completed", result)
            self.assertTrue(all(row["status"] == "success" for row in result["rows"]), result)
            self.assertAlmostEqual(result["rows"][0]["final_asset_cny"], result["rows"][1]["final_asset_cny"], places=6)
            with db_session(self.db_path) as conn:
                self.assertEqual(conn.execute("SELECT close_rate FROM repo_rates WHERE symbol='204001' LIMIT 1").fetchone()[0], 100)
        finally:
            with db_session(self.db_path) as conn:
                conn.executemany("UPDATE repo_rates SET open_rate=?,close_rate=? WHERE symbol=? AND trade_date=?", previous_rates)

    def test_owner_isolation_progress_cancellation_and_http_responsiveness(self):
        entered, release = Event(), Event()
        with patch.object(main_module, "run_research", self.blocking_runner(entered, release)):
            try:
                job = self.start()
                self.assertTrue(entered.wait(3))
                progress = self.poll(job["job_id"])
                self.assertEqual((progress["status"], progress["completed"]), ("running", 1))
                self.assertEqual(len(progress["rows"]), 1)
                self.assertFalse(any(key.startswith("_") for key in progress))
                self.assertTrue(self.call("/api/health")["ok"])
                for suffix, body in (("", None), ("/cancel", {})):
                    with self.assertRaises(error.HTTPError) as raised:
                        self.call(f"/api/research/jobs/{job['job_id']}{suffix}", body, self.key_b)
                    self.assertEqual(raised.exception.code, 404)
                cancelled = self.call(f"/api/research/jobs/{job['job_id']}/cancel", {})
                self.assertEqual(cancelled["status"], "cancelled")
                self.assertEqual(cancelled["completed"], 1)
                self.assertEqual(self.call(f"/api/research/jobs/{job['job_id']}/cancel", {})["status"], "cancelled")
            finally:
                release.set()

    def test_per_key_and_global_queue_limits_and_queued_cancellation(self):
        entered, release = Event(), Event()
        self.server.research_max_pending = 2
        with patch.object(main_module, "run_research", self.blocking_runner(entered, release)):
            try:
                first = self.start()
                self.assertTrue(entered.wait(3))
                with self.assertRaises(error.HTTPError) as raised:
                    self.start()
                self.assertEqual(raised.exception.code, 429)
                queued = self.start(key=self.key_b)
                self.assertEqual(self.poll(queued["job_id"], self.key_b)["status"], "queued")
                with self.assertRaises(error.HTTPError) as raised:
                    self.start(key=self.key_c)
                self.assertEqual(raised.exception.code, 429)
                self.call(f"/api/research/jobs/{queued['job_id']}/cancel", {}, self.key_b)
                replacement = self.start(key=self.key_c)
                self.assertEqual(replacement["status"], "queued")
                self.call(f"/api/research/jobs/{replacement['job_id']}/cancel", {}, self.key_c)
                self.call(f"/api/research/jobs/{first['job_id']}/cancel", {})
            finally:
                release.set()

    def test_bad_requests_and_old_engine_do_not_enqueue(self):
        with self.server.research_jobs_lock:
            before = len(self.server.research_jobs)
        invalid = (
            {"frequencies": []}, {"frequencies": ["hourly"]}, {"bands": [float("nan")]}, {"bands": [True]},
            {"bands": [-.1]}, {"bands": [1.1]}, {"frequencies": ["monthly", "monthly"]},
            {"frequencies": ["daily", "weekly", "monthly", "quarterly", "semiannual", "yearly"], "bands": [0, .1, .2, .3, .4]},
            {"kind": "unknown"}, {"run_id": None},
            {"kind": "withdrawal_stress", "monthly_spends": [10], "annual_growth_rates": [.51], "start_years": [2020]},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(error.HTTPError) as raised:
                self.start(**payload)
            self.assertEqual(raised.exception.code, 400)
        with self.assertRaises(error.HTTPError) as raised:
            self.start(run_id="not-saved")
        self.assertEqual(raised.exception.code, 404)
        with db_session(self.db_path) as conn:
            conn.execute("INSERT INTO backtest_runs(run_id,created_at,config_json,summary_json) VALUES(?,?,?,?)", ("research-old-version", "2020-01-01", json_dumps(self.config), json_dumps({"engine_version": BACKTEST_ENGINE_VERSION - 1})))
        with self.assertRaises(error.HTTPError) as raised:
            self.start(run_id="research-old-version")
        self.assertEqual(raised.exception.code, 409)
        with self.server.research_jobs_lock:
            self.assertEqual(len(self.server.research_jobs), before)

    def test_research_connection_cannot_persist_even_if_runner_accidentally_writes(self):
        before = self.database_counts()
        def write(conn, *_args, **_kwargs):
            conn.execute("DELETE FROM backtest_runs")
        with patch.object(main_module, "run_research", write), self.assertLogs(main_module.logger, level="ERROR"):
            result = self.wait(self.start()["job_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("readonly", result["error"])
        self.assertEqual(self.database_counts(), before)

    def test_abandoned_job_cancels_and_terminal_job_expires(self):
        entered, release = Event(), Event()
        with patch.object(main_module, "run_research", self.blocking_runner(entered, release)):
            try:
                job_id = self.start()["job_id"]
                self.assertTrue(entered.wait(3))
                with self.server.research_jobs_lock:
                    self.server.research_activity[job_id] = time.monotonic() - self.server.job_abandoned_seconds - 1
                main_module.cleanup_research_jobs(self.server)
                self.assertEqual(self.poll(job_id)["status"], "cancelled")
            finally:
                release.set()
                self.server.research_futures[job_id].result(timeout=3)
        with self.server.research_jobs_lock:
            self.server.research_activity[job_id] = time.monotonic() - self.server.job_retention_seconds - 1
        main_module.cleanup_research_jobs(self.server)
        with self.assertRaises(error.HTTPError) as raised:
            self.poll(job_id)
        self.assertEqual(raised.exception.code, 404)

    def test_worker_failure_keeps_completed_rows_for_inspection(self):
        def fail(conn, base_config, payload, should_cancel=None, on_progress=None):
            on_progress(1, payload["total"], [{"id": "first", "status": "success"}])
            raise RuntimeError("temporary research failure")
        with patch.object(main_module, "run_research", fail), self.assertLogs(main_module.logger, level="ERROR"):
            result = self.wait(self.start()["job_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["rows"], [{"id": "first", "status": "success"}])
        self.assertEqual(result["error"], "temporary research failure")

    def test_server_close_cancels_running_and_queued_research(self):
        server = create_server(port=0, db_path=self.db_path)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        entered, release = Event(), Event()
        payload = {"run_id": self.base["run_id"], "kind": "rebalance_grid", "frequencies": ["monthly"], "bands": [0, .25]}
        try:
            with patch.object(main_module, "run_research", self.blocking_runner(entered, release)):
                first = http_json(f"http://{host}:{port}/api/research/start", payload, headers={"Cookie": f"{IDENTITY_COOKIE_NAME}={self.key_a}"})
                self.assertTrue(entered.wait(3))
                queued = http_json(f"http://{host}:{port}/api/research/start", payload, headers={"Cookie": f"{IDENTITY_COOKIE_NAME}={self.key_b}"})
                server.shutdown()
                server.server_close()
                server.research_futures[first["job_id"]].result(timeout=3)
                self.assertEqual(server.research_jobs[first["job_id"]]["status"], "cancelled")
                self.assertEqual(server.research_jobs[queued["job_id"]]["status"], "cancelled")
                self.assertTrue(server.research_futures[queued["job_id"]].cancelled())
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            server.research_executor.shutdown(wait=True)
            thread.join(timeout=5)

    def test_shutdown_stops_accepting_jobs_before_cancelling_its_snapshot(self):
        server = create_server(port=0, db_path=self.db_path)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        entered, release = Event(), Event()
        closing, continue_close = Event(), Event()
        original_cancel = main_module.cancel_research_job
        close_thread = None
        payload = {"run_id": self.base["run_id"], "kind": "rebalance_grid", "frequencies": ["monthly"], "bands": [0, .25]}
        def cancel(target, job_id, reason="研究已取消"):
            if target is server and "服务正在关闭" in reason:
                closing.set()
                continue_close.wait(5)
            return original_cancel(target, job_id, reason)
        try:
            with patch.object(main_module, "run_research", self.blocking_runner(entered, release)), patch.object(main_module, "cancel_research_job", cancel):
                first = http_json(f"http://{host}:{port}/api/research/start", payload, headers={"Cookie": f"{IDENTITY_COOKIE_NAME}={self.key_a}"})
                self.assertTrue(entered.wait(3))
                close_thread = Thread(target=server.server_close, daemon=True)
                close_thread.start()
                self.assertTrue(closing.wait(3))
                with self.assertRaises(error.HTTPError) as raised:
                    http_json(f"http://{host}:{port}/api/research/start", payload, headers={"Cookie": f"{IDENTITY_COOKIE_NAME}={self.key_b}"})
                self.assertEqual(raised.exception.code, 503)
                self.assertEqual(set(server.research_jobs), {first["job_id"]})
                server.shutdown()
                continue_close.set()
                close_thread.join(timeout=5)
                self.assertFalse(close_thread.is_alive())
        finally:
            continue_close.set()
            release.set()
            if close_thread:
                close_thread.join(timeout=5)
            server.shutdown()
            server.server_close()
            server.research_executor.shutdown(wait=True)
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
