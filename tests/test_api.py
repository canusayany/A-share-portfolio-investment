from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import time
from threading import Thread
import unittest
from urllib import request

import app.main as main_module
from app.config import normalize_config
from app.db import db_session, init_db
from app.main import create_server
from tests.helpers import build_synced_db, seed_fixture_data, temp_db_path


def http_json(url: str, payload: dict | None = None, method: str | None = None) -> dict:
    opener = request.build_opener(request.ProxyHandler({}))
    if payload is None and method is None:
        with opener.open(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(url, data=body, method=method or "POST", headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_raw(url: str, headers: dict[str, str] | None = None) -> tuple[dict[str, str], bytes]:
    opener = request.build_opener(request.ProxyHandler({}))
    req = request.Request(url, headers=headers or {})
    with opener.open(req, timeout=10) as resp:
        return dict(resp.headers), resp.read()


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
        health = http_json(f"{self.base_url}/api/health")
        cfg = http_json(f"{self.base_url}/api/default-config")
        status = http_json(f"{self.base_url}/api/data/status")
        self.assertEqual(health["service"], "portfolio-backtest")
        self.assertTrue(health["ok"])
        self.assertGreaterEqual(self.server.request_queue_size, 64)
        self.assertTrue(self.server.daemon_threads)
        self.assertIn("assets", cfg)
        self.assertEqual(cfg["annual_rebalance_month"], 1)
        self.assertEqual(cfg["rolling_window_years"], 3)
        self.assertFalse(cfg["rebalance_month_analysis_enabled"])
        self.assertTrue(status["status"])

    def test_data_sync_invalidates_cached_backtests_when_rows_change(self) -> None:
        result = http_json(f"{self.base_url}/api/backtest/run", {"config": self.config})
        with db_session(self.db_path) as conn:
            before = conn.execute(
                "SELECT config_hash FROM backtest_runs WHERE run_id=?", (result["run_id"],)
            ).fetchone()
        self.assertIsNotNone(before["config_hash"])

        original_sync_all = main_module.sync_all
        main_module.sync_all = lambda *_args, **_kwargs: {
            "inserted": {"prices": 1, "dividends": 0, "adj_factors": 0, "repo_rates": 0, "fx_rates": 0},
            "warnings": [],
            "missing_data": [],
        }
        try:
            synced = http_json(f"{self.base_url}/api/data/sync", {"config": self.config})
        finally:
            main_module.sync_all = original_sync_all

        with db_session(self.db_path) as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) AS count FROM backtest_runs WHERE config_hash IS NOT NULL"
            ).fetchone()["count"]
        self.assertTrue(synced["cache_invalidated"])
        self.assertEqual(remaining, 0)

    def test_run_and_read_backtest_sections(self) -> None:
        result = http_json(f"{self.base_url}/api/backtest/run", {"config": self.config})
        run_id = result["run_id"]
        self.assertFalse(result["cache"]["hit"])
        self.assertGreater(result["summary"]["final_asset_cny"], 0)
        self.assertIn("rolling_periods", result["summary"])
        self.assertIn("rebalance_month_scenarios", result["summary"])
        self.assertIn("worst_year", result["summary"])
        self.assertIn("worst_half_year", result["summary"])
        self.assertIn("drawdown_recovery", result["summary"])
        self.assertIn("upside_capture_ratio", result["summary"])
        self.assertIn("downside_capture_ratio", result["summary"])
        detail = http_json(f"{self.base_url}/api/backtest/{run_id}")
        series = http_json(f"{self.base_url}/api/backtest/{run_id}/series")
        chart_series = http_json(f"{self.base_url}/api/backtest/{run_id}/chart-series")
        rebalance = http_json(f"{self.base_url}/api/backtest/{run_id}/rebalance")
        trades = http_json(f"{self.base_url}/api/backtest/{run_id}/trades")
        positions = http_json(f"{self.base_url}/api/backtest/{run_id}/positions?limit=2")
        self.assertEqual(detail["run_id"], run_id)
        self.assertGreater(len(series["series"]), 20)
        self.assertIn("daily_return", series["series"][0])
        self.assertIn("cumulative_return", series["series"][0])
        self.assertIn("drawdown", series["series"][0])
        self.assertIn("benchmark_return", series["series"][0])
        self.assertAlmostEqual(series["series"][-1]["cumulative_return"], result["summary"]["total_return"])
        self.assertIn("benchmark_value", series["series"][0]["payload"])
        chart = chart_series["chart"]
        self.assertEqual(len(chart["dates"]), len(series["series"]))
        self.assertEqual(chart["source_points"], len(series["series"]))
        self.assertEqual(chart["display_points"], len(chart["dates"]))
        self.assertEqual(len(chart["total_assets"]), len(series["series"]))
        self.assertEqual(len(chart["comparison_total_assets"]), len(series["series"]))
        self.assertTrue(chart["weights"])
        self.assertNotIn("repo_lots", chart)
        self.assertGreaterEqual(len(rebalance["rebalance"]), 1)
        self.assertGreater(len(trades["trades"]), 0)
        self.assertLessEqual(len(positions["positions"]), 2)
        cached = http_json(f"{self.base_url}/api/backtest/run", {"config": self.config})
        self.assertEqual(cached["run_id"], run_id)
        self.assertTrue(cached["cache"]["hit"])

    def test_backtest_history_leaderboard_and_delete(self) -> None:
        result = http_json(f"{self.base_url}/api/backtest/run", {"config": self.config})
        run_id = result["run_id"]
        history = http_json(f"{self.base_url}/api/backtest/history")
        leaderboard = http_json(f"{self.base_url}/api/backtest/leaderboard")
        entry = next(item for item in history["records"] if item["run_id"] == run_id)
        self.assertLessEqual(len(history["records"]), 20)
        self.assertLessEqual(len(leaderboard["records"]), 100)
        self.assertNotIn("config_json", entry)
        self.assertNotIn("summary_json", entry)
        self.assertIn("positive_year_count", entry["summary"])
        self.assertIn("ranking_score", entry["summary"])
        self.assertIn("excess_annualized_return", entry["summary"])
        self.assertIn("adjusted_calmar", entry["summary"])
        self.assertIn("annual_return_drawdown_ratio", entry["summary"])
        self.assertIn("positive_year_ratio", entry["summary"])
        self.assertEqual(entry["ranking_score"], entry["summary"]["ranking_score"])
        if leaderboard["records"]:
            self.assertEqual(leaderboard["records"][0]["rank"], 1)
        deleted = http_json(f"{self.base_url}/api/backtest/{run_id}", method="DELETE")
        self.assertEqual(deleted["deleted"], run_id)
        history_after_delete = http_json(f"{self.base_url}/api/backtest/history")
        self.assertNotIn(run_id, [item["run_id"] for item in history_after_delete["records"]])

    def test_z_large_json_and_static_assets_can_use_gzip(self) -> None:
        result = http_json(f"{self.base_url}/api/backtest/run", {"config": self.config})
        headers, body = http_get_raw(
            f"{self.base_url}/api/backtest/{result['run_id']}/chart-series",
            {"Accept-Encoding": "gzip"},
        )
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        decoded = json.loads(gzip.decompress(body).decode("utf-8"))
        self.assertTrue(decoded["chart"]["dates"])

        static_headers, static_body = http_get_raw(
            f"{self.base_url}/static/app.js",
            {"Accept-Encoding": "gzip"},
        )
        self.assertEqual(static_headers.get("Content-Encoding"), "gzip")
        self.assertEqual(static_headers.get("Cache-Control"), "public, max-age=3600")
        decoded_app_js = gzip.decompress(static_body)
        self.assertIn(b"currentRunId", decoded_app_js)
        self.assertIn(b"ApiNetworkError", decoded_app_js)
        self.assertNotIn(b"weight_label_", decoded_app_js)
        self.assertIn(b"/api/health", decoded_app_js)
        self.assertIn(b"recoverApiConnection", decoded_app_js)
        self.assertIn(b"Object.keys(row.payload?.weights", decoded_app_js)
        self.assertNotIn(b"payload?.values", decoded_app_js)

        versioned_headers, _versioned_body = http_get_raw(
            f"{self.base_url}/static/app.js?v=20260715-perf-2",
            {"Accept-Encoding": "gzip"},
        )
        self.assertIn("immutable", versioned_headers.get("Cache-Control", ""))
        self.assertEqual(versioned_headers.get("CDN-Cache-Control"), versioned_headers.get("Cache-Control"))

    def test_chart_payload_downsamples_long_series_and_keeps_endpoints(self) -> None:
        rows = [
            {
                "trade_date": f"day-{index:04d}",
                "total_asset_cny": float(index),
                "daily_return": 0.0,
                "cumulative_return": 0.0,
                "drawdown": 0.0,
                "benchmark_return": 0.0,
                "payload_json": json.dumps({"weights": {"REPO": 1.0}}),
            }
            for index in range(2501)
        ]
        chart = main_module.columnar_chart_payload(rows, max_points=1000)
        self.assertEqual(chart["source_points"], 2501)
        self.assertEqual(chart["display_points"], 1000)
        self.assertEqual(chart["dates"][0], "day-0000")
        self.assertEqual(chart["dates"][-1], "day-2500")

    def test_chart_payload_preserves_short_lived_routes_and_does_not_mutate_rows(self) -> None:
        rows = [
            {
                "trade_date": f"day-{index:04d}",
                "total_asset_cny": float(index),
                "daily_return": 0.0,
                "cumulative_return": 0.0,
                "drawdown": -0.5 if index == 501 else 0.0,
                "benchmark_return": 0.0,
                "payload_json": json.dumps({"weights": {"BRIEF": 1.0} if index == 501 else {"REPO": 1.0}}),
            }
            for index in range(2000)
        ]
        chart = main_module.columnar_chart_payload(rows, max_points=100)

        self.assertIn("BRIEF", chart["weights"])
        self.assertIn("day-0501", chart["dates"])
        self.assertIn("payload_json", rows[0])

    def test_static_index_is_served(self) -> None:
        opener = request.build_opener(request.ProxyHandler({}))
        with opener.open(f"{self.base_url}/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
            self.assertIn("s-maxage=300", resp.headers.get("Cache-Control", ""))
        self.assertIn("永久投资策略", html)
        self.assertIn("dailyReturnChart", html)
        self.assertIn("repoTargetMode", html)
        self.assertIn("assetWeightTitle", html)
        self.assertIn("annualRebalanceMonth", html)
        self.assertIn("rollingWindowYears", html)
        self.assertIn("rollingTable", html)
        self.assertIn("monthsTable", html)
        self.assertIn("controlSummary", html)
        self.assertIn('id="historyPanel"', html)
        self.assertIn('id="mobileHistoryToggle"', html)
        self.assertIn('id="historyRecentMeta"', html)
        self.assertIn('id="leaderboardList"', html)
        self.assertIn('id="historySearch"', html)
        self.assertIn('id="historySort"', html)
        self.assertIn('data-history-view="leaderboard"', html)
        self.assertIn("allocationSummary", html)
        self.assertIn("data-repo-mode", html)
        self.assertNotIn('<script src="static/echarts.min.js', html)
        self.assertNotIn("cdn.jsdelivr.net", html)
        self.assertNotIn("syncBtn", html)

    def test_backtest_catalog_and_nested_backtest_are_served(self) -> None:
        opener = request.build_opener(request.ProxyHandler({}))
        with opener.open(f"{self.base_url}/backtest/", timeout=10) as resp:
            catalog = resp.read().decode("utf-8")
            self.assertEqual(resp.status, 200)
        self.assertIn("永久投资策略", catalog)
        self.assertIn('href="permanent-investment/"', catalog)
        self.assertIn("全天候资产配置回测", catalog)
        self.assertIn('href="all-weather/"', catalog)

        with opener.open(f"{self.base_url}/backtest/permanent-investment/", timeout=10) as resp:
            detail = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn("永久投资策略", detail)
        self.assertIn("20260827-audit-1", detail)

        with opener.open(f"{self.base_url}/backtest/permanent-investment/static/app.js", timeout=10) as resp:
            app_js = resp.read().decode("utf-8")
        self.assertIn("实际到账现金分红", app_js)
        self.assertIn("2023—2025 年未实施利润分配", app_js)
        self.assertIn("annual_expense_drag_rate", app_js)
        self.assertIn("H20269全收益指数代理阶段", app_js)
        self.assertIn("0.63%/年", app_js)
        self.assertIn("/backtest/permanent-investment", app_js)
        self.assertIn("MAX_RUN_HISTORY = 20", app_js)
        self.assertIn("MAX_LEADERBOARD_RUNS = 100", app_js)
        self.assertIn("replayHistoryRun", app_js)
        self.assertIn("runHistory = recentRecords", app_js)
        self.assertIn("archiveEntries", app_js)

        nested_config = http_json(f"{self.base_url}/backtest/permanent-investment/api/default-config")
        self.assertIn("assets", nested_config)

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
        self.assertTrue(current["result"]["chart"]["dates"])

    def test_async_backtest_returns_primary_result_before_extended_analysis(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-02-28")
        cfg = json.loads(json.dumps(cfg))
        cfg["rebalance_frequency"] = "yearly"
        cfg["rebalance_month_analysis_enabled"] = True
        server = create_server(port=0, db_path=db_path)
        thread = Thread(target=server.serve_forever, daemon=True)
        try:
            thread.start()
            host, port = server.server_address
            base_url = f"http://{host}:{port}"
            job = http_json(f"{base_url}/api/backtest/start", {"config": cfg})
            for _ in range(60):
                current = http_json(f"{base_url}/api/backtest/jobs/{job['job_id']}")
                if current["status"] == "completed":
                    break
                if current["status"] == "failed":
                    self.fail(current.get("error", "job failed"))
                time.sleep(0.05)
            else:
                self.fail("primary backtest did not complete")

            result = current["result"]
            self.assertTrue(result["analysis_pending"])
            self.assertEqual(result["summary"]["analysis_status"], "pending")
            self.assertEqual(result["summary"]["rebalance_month_scenarios"], [])

            for _ in range(120):
                detail = http_json(f"{base_url}/api/backtest/{result['run_id']}")
                if detail["summary"].get("analysis_status") == "completed":
                    break
                if detail["summary"].get("analysis_status") == "failed":
                    self.fail(detail["summary"].get("analysis_error", "analysis failed"))
                time.sleep(0.05)
            else:
                self.fail("extended analysis did not complete")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(len(detail["summary"]["rebalance_month_scenarios"]), 12)

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
