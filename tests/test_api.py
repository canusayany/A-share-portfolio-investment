from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import time
from threading import Thread
import unittest
from urllib import request

import app.main as main_module
from app.config import normalize_config
from app.db import init_db
from app.main import create_server
from tests.helpers import build_synced_db, seed_fixture_data, temp_db_path


def http_json(url: str, payload: dict | None = None) -> dict:
    opener = request.build_opener(request.ProxyHandler({}))
    if payload is None:
        with opener.open(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db_path, cls.config = build_synced_db("2020-01-01", "2020-02-28")
        cls.server = create_server(port=0, db_path=cls.db_path)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.base_url = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_default_config_and_status(self) -> None:
        cfg = http_json(f"{self.base_url}/api/default-config")
        status = http_json(f"{self.base_url}/api/data/status")
        self.assertIn("assets", cfg)
        self.assertTrue(status["status"])

    def test_run_and_read_backtest_sections(self) -> None:
        result = http_json(f"{self.base_url}/api/backtest/run", {"config": self.config})
        run_id = result["run_id"]
        self.assertFalse(result["cache"]["hit"])
        self.assertGreater(result["summary"]["final_asset_cny"], 0)
        detail = http_json(f"{self.base_url}/api/backtest/{run_id}")
        series = http_json(f"{self.base_url}/api/backtest/{run_id}/series")
        rebalance = http_json(f"{self.base_url}/api/backtest/{run_id}/rebalance")
        trades = http_json(f"{self.base_url}/api/backtest/{run_id}/trades")
        positions = http_json(f"{self.base_url}/api/backtest/{run_id}/positions?limit=2")
        self.assertEqual(detail["run_id"], run_id)
        self.assertGreater(len(series["series"]), 20)
        self.assertNotIn("cumulative_return", series["series"][0])
        self.assertIn("benchmark_value", series["series"][0]["payload"])
        self.assertGreaterEqual(len(rebalance["rebalance"]), 1)
        self.assertGreater(len(trades["trades"]), 0)
        self.assertLessEqual(len(positions["positions"]), 2)
        cached = http_json(f"{self.base_url}/api/backtest/run", {"config": self.config})
        self.assertEqual(cached["run_id"], run_id)
        self.assertTrue(cached["cache"]["hit"])

    def test_static_index_is_served(self) -> None:
        opener = request.build_opener(request.ProxyHandler({}))
        with opener.open(f"{self.base_url}/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("跨市场组合回测", html)
        self.assertIn("dailyReturnChart", html)
        self.assertIn("repoTargetMode", html)
        self.assertIn("assetWeightTitle", html)
        self.assertIn("static/echarts.min.js", html)
        self.assertNotIn("cdn.jsdelivr.net", html)
        self.assertNotIn("syncBtn", html)

    def test_run_backtest_auto_syncs_when_data_is_missing(self) -> None:
        db_path = temp_db_path()
        init_db(db_path)
        cfg = normalize_config({"start_date": "2020-01-01", "end_date": "2020-02-28"})
        original_sync_all = main_module.sync_all

        def fake_sync_all(conn, token, start, end, assets, repo_symbol="204001", **_kwargs):
            seed_cfg = normalize_config({"start_date": start, "end_date": end, "assets": assets})
            seed_cfg["repo_symbol"] = repo_symbol
            seed_fixture_data(conn, seed_cfg, start, end)
            return {
                "inserted": {"prices": 1, "dividends": 0, "adj_factors": 0, "repo_rates": 1, "fx_rates": 1},
                "warnings": [],
                "missing_data": [],
            }

        server = create_server(port=0, db_path=db_path)
        thread = Thread(target=server.serve_forever, daemon=True)
        try:
            main_module.sync_all = fake_sync_all
            thread.start()
            host, port = server.server_address
            result = http_json(f"http://{host}:{port}/api/backtest/run", {"config": cfg})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            main_module.sync_all = original_sync_all

        self.assertTrue(result["data_sync"]["triggered"])
        self.assertGreater(result["summary"]["final_asset_cny"], 0)

    def test_concurrent_backtests_are_serialized(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-02-28")
        server = create_server(port=0, db_path=db_path)
        thread = Thread(target=server.serve_forever, daemon=True)
        try:
            thread.start()
            host, port = server.server_address
            url = f"http://{host}:{port}/api/backtest/run"
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _idx: http_json(url, {"config": cfg}), range(2)))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(results[0]["run_id"], results[1]["run_id"])
        self.assertTrue(any(result["cache"]["hit"] for result in results))
        self.assertFalse(any("error" in result and "database is locked" in result["error"].lower() for result in results))

    def test_async_backtest_job_completes(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-02-28")
        server = create_server(port=0, db_path=db_path)
        thread = Thread(target=server.serve_forever, daemon=True)
        try:
            thread.start()
            host, port = server.server_address
            base_url = f"http://{host}:{port}"
            job = http_json(f"{base_url}/api/backtest/start", {"config": cfg})
            self.assertEqual(job["status"], "queued")
            for _ in range(40):
                current = http_json(f"{base_url}/api/backtest/jobs/{job['job_id']}")
                if current["status"] == "completed":
                    break
                if current["status"] == "failed":
                    self.fail(current.get("error", "job failed"))
                time.sleep(0.1)
            else:
                self.fail("job did not complete")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(current["status"], "completed")
        self.assertGreater(current["result"]["summary"]["final_asset_cny"], 0)

    def test_async_backtest_start_reuses_client_request_id(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-02-28")
        original_execute = main_module.execute_backtest_request
        calls = 0

        def slow_execute(settings, write_lock, config, should_cancel=None):
            nonlocal calls
            calls += 1
            time.sleep(0.15)
            return {"run_id": "same-run", "summary": {"final_asset_cny": 1}, "cache": {"hit": False}}

        server = create_server(port=0, db_path=db_path)
        thread = Thread(target=server.serve_forever, daemon=True)
        try:
            main_module.execute_backtest_request = slow_execute
            thread.start()
            host, port = server.server_address
            base_url = f"http://{host}:{port}"
            payload = {"config": cfg, "client_request_id": "same-click"}
            first = http_json(f"{base_url}/api/backtest/start", payload)
            second = http_json(f"{base_url}/api/backtest/start", payload)
            for _ in range(20):
                current = http_json(f"{base_url}/api/backtest/jobs/{first['job_id']}")
                if current["status"] == "completed":
                    break
                time.sleep(0.05)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            main_module.execute_backtest_request = original_execute

        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(calls, 1)
        self.assertEqual(current["status"], "completed")

    def test_unpolled_async_backtest_job_is_cancelled(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-02-28")
        original_execute = main_module.execute_backtest_request

        def slow_execute(settings, write_lock, config, should_cancel=None):
            for _ in range(50):
                if should_cancel and should_cancel():
                    raise main_module.BacktestCancelled(main_module.CANCELLED_JOB_MESSAGE)
                time.sleep(0.02)
            return {"run_id": "late", "summary": {"final_asset_cny": 1}, "cache": {"hit": False}}

        server = create_server(port=0, db_path=db_path)
        server.job_abandoned_seconds = 0.05  # type: ignore[attr-defined]
        thread = Thread(target=server.serve_forever, daemon=True)
        try:
            main_module.execute_backtest_request = slow_execute
            thread.start()
            host, port = server.server_address
            base_url = f"http://{host}:{port}"
            job = http_json(f"{base_url}/api/backtest/start", {"config": cfg})
            time.sleep(0.12)
            main_module.cleanup_jobs(server)
            current = http_json(f"{base_url}/api/backtest/jobs/{job['job_id']}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            main_module.execute_backtest_request = original_execute

        self.assertEqual(current["status"], "cancelled")
        self.assertIn("页面没有继续请求结果", current["message"])


if __name__ == "__main__":
    unittest.main()
