from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import csv
import gzip
import io
import json
import sqlite3
import time
from threading import Thread
import unittest
from urllib import error, request

import app.main as main_module
from app.config import normalize_config
from app.db import add_leaderboard_membership, db_session, init_db
from app.identity import (
    DEFAULT_LEADERBOARD_KEY_ID,
    IDENTITY_COOKIE_MAX_AGE_SECONDS,
    IDENTITY_COOKIE_NAME,
    leaderboard_key_id,
)
from app.main import create_server, rebalance_display_payload
from app.services.calendar import business_days
from tests.helpers import build_synced_db, seed_fixture_data, temp_db_path


def http_json(
    url: str,
    payload: dict | None = None,
    method: str | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    opener = request.build_opener(request.ProxyHandler({}))
    if payload is None and method is None:
        req = request.Request(url, headers=headers or {})
        with opener.open(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(
        url,
        data=body,
        method=method or "POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
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
        self.assertFalse(cfg["rebalance_to_target"])
        self.assertTrue(status["status"])

    def test_rebalance_display_payload_exposes_annual_total_and_return_bases(self) -> None:
        payload = {
            "decision_total_asset_cny": 1_120_000.0,
            "year_profit_cny": 120_000.0,
            "year_profit_on_year_start": 0.12,
            "year_profit_on_original_capital": 0.10,
            "internal_only": "hidden",
        }

        displayed = rebalance_display_payload(payload)

        self.assertEqual(displayed["decision_total_asset_cny"], 1_120_000.0)
        self.assertEqual(displayed["year_profit_on_year_start"], 0.12)
        self.assertEqual(displayed["year_profit_on_original_capital"], 0.10)
        self.assertNotIn("internal_only", displayed)

    def test_identity_accepts_long_special_key_and_sets_persistent_cookie(self) -> None:
        initial = http_json(f"{self.base_url}/api/identity")
        self.assertFalse(initial["configured"])

        raw_key = "Aa1~!@#$%^&*()_+-=[]{}|;:',.<>/?中文" + "x" * 5000
        expected_key_id = leaderboard_key_id(raw_key)
        body = json.dumps({"key": raw_key}, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/identity",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        opener = request.build_opener(request.ProxyHandler({}))
        with opener.open(req, timeout=10) as response:
            saved = json.loads(response.read().decode("utf-8"))
            set_cookie = response.headers.get("Set-Cookie", "")

        self.assertTrue(saved["configured"])
        self.assertEqual(saved["key_hint"], expected_key_id[:8])
        self.assertIn(f"{IDENTITY_COOKIE_NAME}={expected_key_id}", set_cookie)
        self.assertIn(f"Max-Age={IDENTITY_COOKIE_MAX_AGE_SECONDS}", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Lax", set_cookie)
        self.assertNotIn(raw_key, set_cookie)

        identified = http_json(
            f"{self.base_url}/api/identity",
            headers={"Cookie": f"{IDENTITY_COOKIE_NAME}={expected_key_id}"},
        )
        self.assertTrue(identified["configured"])
        self.assertEqual(identified["key_hint"], expected_key_id[:8])

        result = http_json(
            f"{self.base_url}/api/backtest/run",
            {"config": self.config},
            headers={"Cookie": f"{IDENTITY_COOKIE_NAME}={expected_key_id}"},
        )
        with db_session(self.db_path) as conn:
            membership = conn.execute(
                "SELECT 1 FROM leaderboard_memberships WHERE key_id=? AND run_id=?",
                (expected_key_id, result["run_id"]),
            ).fetchone()
        self.assertIsNotNone(membership)

    def test_global_leaderboard_isolated_by_key_while_history_is_shared(self) -> None:
        second_key_id = leaderboard_key_id("Second-Key!大小写")
        xp_run_id = "identity-xp-run"
        second_run_id = "identity-second-run"
        config = {
            "start_date": "2020-01-01",
            "end_date": "2020-12-31",
            "assets": [{"symbol": "TEST", "name": "TEST", "enabled": True, "target_weight": 1.0}],
        }
        summary = {
            "start_date": "2020-01-01",
            "end_date": "2020-12-31",
            "annualized_return": 0.12,
            "max_drawdown": -0.10,
            "ranking_eligible": True,
            "ranking_score": 70.0,
        }
        try:
            with db_session(self.db_path) as conn:
                for run_id, created_at, key_id in (
                    (xp_run_id, "2030-01-01T00:00:00+00:00", DEFAULT_LEADERBOARD_KEY_ID),
                    (second_run_id, "2030-01-02T00:00:00+00:00", second_key_id),
                ):
                    conn.execute(
                        "INSERT INTO backtest_runs(run_id,created_at,config_json,summary_json) VALUES(?,?,?,?)",
                        (run_id, created_at, json.dumps(config), json.dumps(summary)),
                    )
                    add_leaderboard_membership(conn, key_id, run_id)

            xp_board = http_json(f"{self.base_url}/api/backtest/leaderboard?period=all")
            second_board = http_json(
                f"{self.base_url}/api/backtest/leaderboard?period=all",
                headers={"Cookie": f"{IDENTITY_COOKIE_NAME}={second_key_id}"},
            )
            shared_history = http_json(
                f"{self.base_url}/api/backtest/history",
                headers={"Cookie": f"{IDENTITY_COOKIE_NAME}={second_key_id}"},
            )

            xp_ids = {entry["run_id"] for entry in xp_board["records"]}
            second_ids = {entry["run_id"] for entry in second_board["records"]}
            history_ids = {entry["run_id"] for entry in shared_history["records"]}
            self.assertIn(xp_run_id, xp_ids)
            self.assertNotIn(second_run_id, xp_ids)
            self.assertIn(second_run_id, second_ids)
            self.assertNotIn(xp_run_id, second_ids)
            self.assertTrue({xp_run_id, second_run_id}.issubset(history_ids))
        finally:
            with db_session(self.db_path) as conn:
                conn.execute("DELETE FROM leaderboard_memberships WHERE run_id IN (?,?)", (xp_run_id, second_run_id))
                conn.execute("DELETE FROM backtest_runs WHERE run_id IN (?,?)", (xp_run_id, second_run_id))

    def test_existing_leaderboard_records_migrate_to_xp(self) -> None:
        db_path = temp_db_path()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE backtest_runs (
                  run_id TEXT PRIMARY KEY,
                  created_at TEXT NOT NULL,
                  config_json TEXT NOT NULL,
                  summary_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO backtest_runs(run_id,created_at,config_json,summary_json) VALUES(?,?,?,?)",
                ("legacy-run", "2026-01-01T00:00:00+00:00", "{}", "{}"),
            )
            conn.commit()
        finally:
            conn.close()

        init_db(db_path)
        with db_session(db_path) as migrated:
            membership = migrated.execute(
                "SELECT key_id FROM leaderboard_memberships WHERE run_id='legacy-run'"
            ).fetchone()
        self.assertEqual(membership["key_id"], DEFAULT_LEADERBOARD_KEY_ID)

    def test_daily_pnl_separates_daily_cumulative_and_drawdown_bases(self) -> None:
        config = {
            "assets": [
                {"symbol": "A", "name": "资产A", "enabled": True, "target_weight": 0.5},
                {"symbol": "B", "name": "资产B", "enabled": True, "target_weight": 0.5},
            ]
        }
        rows = [
            {
                "trade_date": "2020-01-02",
                "total_asset_cny": 200.0,
                "flow_cny": 0.0,
                "daily_return": 0.0,
                "cumulative_return": 0.0,
                "drawdown": 0.0,
                "benchmark_return": 0.0,
                "payload_json": json.dumps(
                    {"values": {"A": 100.0, "B": 100.0}, "asset_daily_profit_cny": {"A": 0.0, "B": 0.0}}
                ),
            },
            {
                "trade_date": "2020-01-03",
                "total_asset_cny": 210.0,
                "flow_cny": 0.0,
                "daily_return": 0.05,
                "cumulative_return": 0.05,
                "drawdown": 0.0,
                "benchmark_return": 0.02,
                "payload_json": json.dumps(
                    {"values": {"A": 50.0, "B": 160.0}, "asset_daily_profit_cny": {"A": 0.0, "B": 10.0}}
                ),
            },
            {
                "trade_date": "2020-01-06",
                "total_asset_cny": 189.0,
                "flow_cny": 0.0,
                "daily_return": -0.10,
                "cumulative_return": -0.055,
                "drawdown": -0.10,
                "benchmark_return": -0.01,
                "payload_json": json.dumps(
                    {"values": {"A": 45.0, "B": 144.0}, "asset_daily_profit_cny": {"A": -5.0, "B": -16.0}}
                ),
            },
        ]

        chart = main_module.daily_pnl_chart_payload(rows, config)

        self.assertAlmostEqual(chart["combined_returns"][1], 0.05)
        self.assertAlmostEqual(chart["combined_cumulative_returns"][2], -0.055)
        self.assertAlmostEqual(chart["combined_drawdowns"][2], -0.10)
        self.assertEqual(chart["portfolio_profits"], [0.0, 10.0, -21.0])
        self.assertEqual(chart["portfolio_returns"], [0.0, 0.05, -0.10])
        self.assertEqual(chart["portfolio_drawdowns"], [0.0, 0.0, -0.10])

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
        run_config = json.loads(json.dumps(self.config))
        run_config["rebalance_to_target"] = True
        result = http_json(f"{self.base_url}/api/backtest/run", {"config": run_config})
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
        daily_pnl_response = http_json(f"{self.base_url}/api/backtest/{run_id}/daily-pnl")
        comovement_response = http_json(f"{self.base_url}/api/backtest/{run_id}/asset-comovement")
        diagnostics_response = http_json(f"{self.base_url}/api/backtest/{run_id}/strategy-diagnostics?window=all")
        rebalance = http_json(f"{self.base_url}/api/backtest/{run_id}/rebalance")
        trades = http_json(f"{self.base_url}/api/backtest/{run_id}/trades")
        positions = http_json(f"{self.base_url}/api/backtest/{run_id}/positions?limit=2")
        self.assertEqual(detail["run_id"], run_id)
        self.assertTrue(detail["config"]["rebalance_to_target"])
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
        self.assertTrue(chart["values"])
        self.assertIn("year_profit_cny", rebalance["rebalance"][0]["payload"])
        self.assertIn("year_profit_on_year_start", rebalance["rebalance"][0]["payload"])
        self.assertIn("year_profit_on_original_capital", rebalance["rebalance"][0]["payload"])
        self.assertIn("total_asset_before", rebalance["rebalance"][0])
        self.assertTrue(chart["weights"])
        self.assertEqual(set(chart["values"]), set(chart["weights"]))
        self.assertAlmostEqual(
            sum(values[-1] for values in chart["values"].values()),
            chart["total_assets"][-1],
            places=6,
        )
        self.assertNotIn("repo_lots", chart)
        daily_pnl = daily_pnl_response["daily_pnl"]
        self.assertTrue(daily_pnl["available"])
        self.assertEqual(daily_pnl["source_points"], len(series["series"]))
        self.assertEqual(len(daily_pnl["dates"]), len(series["series"]))
        self.assertTrue(daily_pnl["symbols"])
        self.assertEqual(set(daily_pnl["symbols"]), set(daily_pnl["profits"]))
        self.assertEqual(set(daily_pnl["symbols"]), set(daily_pnl["returns"]))
        self.assertEqual(set(daily_pnl["symbols"]), set(daily_pnl["cumulative_returns"]))
        self.assertEqual(set(daily_pnl["symbols"]), set(daily_pnl["drawdowns"]))
        self.assertEqual(len(daily_pnl["benchmark_profits"]), len(series["series"]))
        self.assertEqual(len(daily_pnl["benchmark_returns"]), len(series["series"]))
        self.assertEqual(len(daily_pnl["portfolio_profits"]), len(series["series"]))
        self.assertEqual(len(daily_pnl["portfolio_returns"]), len(series["series"]))
        self.assertEqual(len(daily_pnl["portfolio_cumulative_returns"]), len(series["series"]))
        self.assertEqual(len(daily_pnl["portfolio_drawdowns"]), len(series["series"]))
        for index, combined_profit in enumerate(daily_pnl["combined_profits"]):
            self.assertAlmostEqual(
                combined_profit,
                sum(daily_pnl["profits"][symbol][index] for symbol in daily_pnl["symbols"]),
                places=6,
            )
            self.assertAlmostEqual(
                daily_pnl["portfolio_profits"][index],
                sum(series["series"][index]["payload"]["asset_daily_profit_cny"].values()),
                places=6,
            )
            self.assertAlmostEqual(
                daily_pnl["portfolio_returns"][index],
                series["series"][index]["daily_return"],
                places=12,
            )
            self.assertAlmostEqual(
                daily_pnl["portfolio_drawdowns"][index],
                series["series"][index]["drawdown"],
                places=12,
            )
        comovement = comovement_response["asset_comovement"]
        self.assertTrue(comovement["available"])
        self.assertEqual(comovement["window_order"], ["all", "1y", "3y", "5y", "10y"])
        self.assertEqual(len(comovement["assets"]), 3)
        all_window = comovement["windows"]["all"]
        self.assertEqual(sum(all_window["counts"].values()), all_window["comparable_days"])
        self.assertIn("hedge_positive", all_window["counts"])
        self.assertIn("hedge_negative", all_window["counts"])
        diagnostics = diagnostics_response["strategy_diagnostics"]
        self.assertEqual(diagnostics["window"]["key"], "all")
        self.assertEqual(len(diagnostics["asset_effects"]), 3)
        self.assertGreaterEqual(len(diagnostics["optimization_candidates"]), 2)
        self.assertIn("stress_protection", diagnostics)
        self.assertGreaterEqual(len(rebalance["rebalance"]), 1)
        self.assertGreater(len(trades["trades"]), 0)
        self.assertLessEqual(len(positions["positions"]), 2)
        cached = http_json(f"{self.base_url}/api/backtest/run", {"config": run_config})
        self.assertEqual(cached["run_id"], run_id)
        self.assertTrue(cached["cache"]["hit"])

    def test_csv_export_contains_selection_daily_prices_and_final_backtest_result(self) -> None:
        result = http_json(f"{self.base_url}/api/backtest/run", {"config": self.config})
        run_id = result["run_id"]
        headers, body = http_get_raw(
            f"{self.base_url}/api/backtest/{run_id}/export.csv"
            "?start_date=2020-01-06&end_date=2020-01-10"
            "&symbols=512890.SH,CBA21801,REPO,204001"
        )

        self.assertEqual(headers["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment;", headers["Content-Disposition"])
        decoded = body.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(decoded)))
        self.assertIn(["选择开始时间", "2020-01-06"], rows)
        self.assertIn(["选择结束时间", "2020-01-10"], rows)
        selected_row = next(row for row in rows if row and row[0] == "选择标的")
        self.assertIn("512890.SH 红利低波基金", selected_row[1])
        self.assertIn("CBA21801 30年国债ETF", selected_row[1])
        self.assertIn("REPO 现金部分（含逆回购及应收分红）", selected_row[1])
        self.assertIn("204001 1天国债逆回购", selected_row[1])
        metric_row = next(row for row in rows if row and row[0] == "价格与收益口径")
        self.assertIn("禁止跨路线代码直接比较价格", metric_row[1])
        price_header = [
            "交易日",
            "选择标的代码",
            "标的名称",
            "路线类型",
            "实际行情代码",
            "实际行情名称",
            "原始数据代码",
            "原始收盘价",
            "行情复权因子",
            "拆分连续倍率",
            "回测价格总倍率",
            "回测使用收盘价",
            "事件说明",
            "资产桶市值(元)",
            "资产桶单日盈亏(元)",
            "资产桶单日收益(小数)",
            "资产桶累计收益(小数)",
            "资产桶回撤(小数)",
            "组合总资产(元)",
            "外部现金流(元)",
            "组合单日收益(小数)",
            "组合累计收益(小数)",
            "组合回撤(小数)",
            "币种",
            "数据源",
        ]
        header_index = rows.index(price_header)
        price_rows = []
        for row in rows[header_index + 1 :]:
            if not row:
                break
            price_rows.append(row)
        self.assertTrue(price_rows)
        self.assertEqual({row[1] for row in price_rows}, {"512890.SH", "CBA21801", "REPO"})
        self.assertTrue(all("2020-01-06" <= row[0] <= "2020-01-10" for row in price_rows))
        self.assertTrue(all(len(row) == len(price_header) for row in price_rows))
        market_rows = [row for row in price_rows if row[1] != "REPO"]
        cash_rows = [row for row in price_rows if row[1] == "REPO"]
        self.assertTrue(all(row[11] for row in market_rows))
        self.assertTrue(all(row[15] for row in price_rows))
        self.assertTrue(all(row[20] for row in price_rows))
        self.assertTrue(all(row[22] for row in price_rows))
        self.assertTrue(cash_rows)
        self.assertTrue(all(row[3] == "现金管理" for row in cash_rows))
        self.assertTrue(all("现金余额=" in row[12] and "持有逆回购合约=" in row[12] for row in cash_rows))
        self.assertTrue(all(row[24] == "portfolio_daily" for row in cash_rows))
        series = http_json(f"{self.base_url}/api/backtest/{run_id}/series")["series"]
        series_by_date = {row["trade_date"]: row for row in series}
        cash_sample = cash_rows[0]
        source_sample = series_by_date[cash_sample[0]]["payload"]
        self.assertAlmostEqual(float(cash_sample[13]), float(source_sample["values"]["REPO"]), places=6)
        self.assertAlmostEqual(
            float(cash_sample[14]),
            float(source_sample["asset_daily_profit_cny"]["REPO"]),
            places=6,
        )
        repo_header = ["逆回购交易日", "逆回购代码", "逆回购名称", "收盘年化利率(%)", "数据源"]
        repo_header_index = rows.index(repo_header)
        repo_rows = []
        for row in rows[repo_header_index + 1 :]:
            if not row:
                break
            repo_rows.append(row)
        self.assertTrue(repo_rows)
        self.assertEqual({row[1] for row in repo_rows}, {"204001"})
        self.assertTrue(all("2020-01-06" <= row[0] <= "2020-01-10" for row in repo_rows))
        self.assertIn(["最终回测结果", "指标", "数值"], rows)
        self.assertTrue(any(row and row[0] == "最终回测结果" and row[1] == "年化收益" for row in rows))
        self.assertTrue(any(row and row[0] == "最终回测结果" and row[1] == "最大回撤" for row in rows))

        _default_headers, default_body = http_get_raw(
            f"{self.base_url}/api/backtest/{run_id}/export.csv"
        )
        default_rows = list(csv.reader(io.StringIO(default_body.decode("utf-8-sig"))))
        self.assertIn(["选择开始时间", self.config["start_date"]], default_rows)
        self.assertIn(["选择结束时间", self.config["end_date"]], default_rows)
        default_selection = next(row for row in default_rows if row and row[0] == "选择标的")
        self.assertIn("REPO 现金部分（含逆回购及应收分红）", default_selection[1])
        self.assertIn("204001 1天国债逆回购", default_selection[1])
        self.assertIn(repo_header, default_rows)

    def test_csv_export_uses_saved_run_assets_and_supports_cash_only(self) -> None:
        run_config = json.loads(json.dumps(self.config))
        for asset in run_config["assets"]:
            if asset["symbol"] == "512890.SH":
                asset["enabled"] = False
                asset["target_weight"] = 0.0
            elif asset["symbol"] == "510300.SH":
                asset["enabled"] = True
                asset["target_weight"] = 0.25
        result = http_json(f"{self.base_url}/api/backtest/run", {"config": run_config})
        run_id = result["run_id"]
        detail = http_json(f"{self.base_url}/api/backtest/{run_id}")
        selected = {
            asset["symbol"]
            for asset in detail["config"]["assets"]
            if asset["enabled"] and float(asset["target_weight"]) > 0
        }
        self.assertIn("510300.SH", selected)
        self.assertNotIn("512890.SH", selected)

        _headers, body = http_get_raw(
            f"{self.base_url}/api/backtest/{run_id}/export.csv?symbols=510300.SH,REPO"
        )
        rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
        selected_row = next(row for row in rows if row and row[0] == "选择标的")
        self.assertIn("510300.SH 沪深300 510300", selected_row[1])
        self.assertIn("REPO 现金部分（含逆回购及应收分红）", selected_row[1])
        self.assertNotIn("512890.SH", selected_row[1])
        self.assertTrue(any(row and len(row) > 1 and row[1] == "REPO" for row in rows))

    def test_csv_export_rejects_dates_outside_run_and_unselected_assets(self) -> None:
        result = http_json(f"{self.base_url}/api/backtest/run", {"config": self.config})
        run_id = result["run_id"]
        invalid_urls = [
            f"{self.base_url}/api/backtest/{run_id}/export.csv?start_date=2019-12-31&end_date=2020-01-10",
            f"{self.base_url}/api/backtest/{run_id}/export.csv?symbols=VOO",
        ]
        opener = request.build_opener(request.ProxyHandler({}))
        for url in invalid_urls:
            with self.subTest(url=url), self.assertRaises(error.HTTPError) as raised:
                opener.open(url, timeout=10)
            self.assertEqual(raised.exception.code, 400)

    def test_backtest_history_leaderboard_and_delete(self) -> None:
        result = http_json(f"{self.base_url}/api/backtest/run", {"config": self.config})
        run_id = result["run_id"]
        history = http_json(f"{self.base_url}/api/backtest/history")
        leaderboard = http_json(f"{self.base_url}/api/backtest/leaderboard")
        entry = next(item for item in history["records"] if item["run_id"] == run_id)
        self.assertLessEqual(len(history["records"]), 20)
        self.assertLessEqual(len(leaderboard["records"]), 100)
        self.assertIn("period", leaderboard)
        self.assertIn("available_years", leaderboard)
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
        self.assertIn(b"asset_performance_version", decoded_app_js)
        self.assertIn(b"/api/health", decoded_app_js)
        self.assertIn(b"recoverApiConnection", decoded_app_js)
        self.assertIn(b"/daily-pnl", decoded_app_js)
        self.assertIn(b"/asset-comovement", decoded_app_js)
        self.assertIn(b"/strategy-diagnostics", decoded_app_js)
        self.assertIn(b"/export.csv", decoded_app_js)
        self.assertIn(b"currentRunConfig", decoded_app_js)
        self.assertIn(b"response.blob()", decoded_app_js)
        self.assertIn(b"showToast(message, true)", decoded_app_js)
        self.assertIn(b'symbol: "REPO"', decoded_app_js)
        self.assertIn(b"await api(`/api/backtest/${encodeURIComponent(runId)}`", decoded_app_js)
        self.assertIn(b"rebalance_to_target", decoded_app_js)
        self.assertIn(b"tradeAssetName", decoded_app_js)
        self.assertIn(b"rebalanceAssetColumnName", decoded_app_js)
        self.assertIn(b"rebalanceCashEquivalentSymbols", decoded_app_js)
        self.assertIn(b"cashEquivalentSymbols.has(symbol)", decoded_app_js)
        self.assertIn(b"Object.keys(row.payload?.weights", decoded_app_js)
        self.assertIn(b"payload?.values", decoded_app_js)
        self.assertIn("各标的金额 · 组合占比".encode("utf-8"), decoded_app_js)
        self.assertIn("当年总资产".encode("utf-8"), decoded_app_js)
        self.assertIn("当年收益（按上年度总资产）".encode("utf-8"), decoded_app_js)
        self.assertIn("当年收益（按原始资金）".encode("utf-8"), decoded_app_js)
        self.assertIn("对冲为正".encode("utf-8"), decoded_app_js)
        self.assertIn("对冲为负".encode("utf-8"), decoded_app_js)
        self.assertIn("导出CSV".encode("utf-8"), decoded_app_js)
        self.assertIn("删掉一个标的".encode("utf-8"), decoded_app_js)

        _styles_headers, styles_body = http_get_raw(
            f"{self.base_url}/static/styles.css",
            {"Accept-Encoding": "gzip"},
        )
        decoded_styles = gzip.decompress(styles_body)
        self.assertIn(b".table-year-profit", decoded_styles)
        self.assertIn(b".asset-comovement-summary", decoded_styles)
        self.assertIn(b"background: transparent", decoded_styles)
        self.assertNotIn(b"background: color-mix(in srgb, var(--accent) 12%, var(--surface))", decoded_styles)
        self.assertNotIn(b"background: color-mix(in srgb, var(--danger) 12%, var(--surface))", decoded_styles)

        versioned_headers, _versioned_body = http_get_raw(
            f"{self.base_url}/static/app.js?v=20260715-perf-2",
            {"Accept-Encoding": "gzip"},
        )
        self.assertIn("immutable", versioned_headers.get("Cache-Control", ""))
        self.assertEqual(versioned_headers.get("CDN-Cache-Control"), versioned_headers.get("Cache-Control"))

    def test_time_aware_leaderboard_recomputes_each_year_and_excludes_partial_coverage(self) -> None:
        db_path = temp_db_path()
        init_db(db_path)
        run_ids = ("strategy_a", "strategy_b", "partial")
        with db_session(db_path) as conn:
            for index, run_id in enumerate(run_ids):
                config = {
                    "start_date": "2020-01-01",
                    "end_date": "2021-12-31",
                    "assets": [{"symbol": run_id, "name": run_id, "enabled": True, "target_weight": 1.0}],
                }
                summary = {
                    "start_date": "2020-01-01",
                    "end_date": "2021-12-31",
                    "annualized_return": 0.10,
                    "max_drawdown": -0.10,
                    "ranking_eligible": True,
                    "ranking_score": 50.0,
                }
                conn.execute(
                    "INSERT INTO backtest_runs(run_id,created_at,config_json,summary_json) VALUES(?,?,?,?)",
                    (run_id, f"2022-01-0{index + 1}T00:00:00+00:00", json.dumps(config), json.dumps(summary)),
                )

            daily_rows = []
            for year in (2020, 2021):
                dates = business_days(f"{year}-01-01", f"{year}-12-31")
                for run_id in ("strategy_a", "strategy_b"):
                    stronger = (year == 2020 and run_id == "strategy_a") or (year == 2021 and run_id == "strategy_b")
                    daily_return = 0.0008 if stronger else 0.0001
                    total = 1_000_000.0
                    for day in dates:
                        total *= 1.0 + daily_return
                        daily_rows.append(
                            (run_id, day.isoformat(), total, 0.0, daily_return, 0.0, 0.0, 0.0, "{}")
                        )
            partial_dates = business_days("2020-06-01", "2020-12-31")
            total = 1_000_000.0
            for day in partial_dates:
                total *= 1.001
                daily_rows.append(("partial", day.isoformat(), total, 0.0, 0.001, 0.0, 0.0, 0.0, "{}"))
            conn.executemany(
                """
                INSERT INTO portfolio_daily(
                  run_id,trade_date,total_asset_cny,flow_cny,daily_return,
                  cumulative_return,drawdown,benchmark_return,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                daily_rows,
            )

            ranking_2020 = main_module.time_aware_backtest_leaderboard(conn, "2020-01-01", "2020-12-31")
            ranking_2021 = main_module.time_aware_backtest_leaderboard(conn, "2021-01-01", "2021-12-31")
            years = main_module.leaderboard_available_years(conn)
            default_payload = main_module.backtest_leaderboard_payload(conn)
            year_payload = main_module.backtest_leaderboard_payload(conn, {"year": ["2020"]})

        self.assertEqual([entry["run_id"] for entry in ranking_2020], ["strategy_a", "strategy_b"])
        self.assertEqual([entry["run_id"] for entry in ranking_2021], ["strategy_b", "strategy_a"])
        self.assertEqual(years, [2021, 2020])
        self.assertEqual(ranking_2020[0]["period_metrics"]["peer_count"], 2)
        self.assertAlmostEqual(ranking_2020[0]["period_metrics"]["coverage_ratio"], 1.0)
        self.assertGreater(ranking_2020[0]["ranking_score"], ranking_2020[1]["ranking_score"])
        self.assertEqual(default_payload["period"]["label"], "2021年")
        self.assertEqual(default_payload["records"][0]["run_id"], "strategy_b")
        self.assertEqual(year_payload["records"][0]["run_id"], "strategy_a")

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
                "payload_json": json.dumps({
                    "values": {"BRIEF": 500.0} if index == 501 else {"REPO": float(index)},
                    "weights": {"BRIEF": 1.0} if index == 501 else {"REPO": 1.0},
                }),
            }
            for index in range(2000)
        ]
        chart = main_module.columnar_chart_payload(rows, max_points=100)

        self.assertIn("BRIEF", chart["weights"])
        self.assertIn("BRIEF", chart["values"])
        brief_index = chart["dates"].index("day-0501")
        self.assertEqual(chart["values"]["BRIEF"][brief_index], 500.0)
        self.assertIn("day-0501", chart["dates"])
        self.assertIn("payload_json", rows[0])

    def test_static_index_is_served(self) -> None:
        opener = request.build_opener(request.ProxyHandler({}))
        with opener.open(f"{self.base_url}/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
            self.assertIn("s-maxage=300", resp.headers.get("Cache-Control", ""))
        self.assertIn("永久投资策略", html)
        self.assertIn("dailyReturnChart", html)
        self.assertIn("dailyPnlChart", html)
        self.assertIn("rebalanceToTarget", html)
        self.assertIn("data-daily-pnl-scale", html)
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
        self.assertIn('id="leaderboardPeriod"', html)
        self.assertIn('id="leaderboardCustomPeriod"', html)
        self.assertIn('id="historySearch"', html)
        self.assertIn('id="historySort"', html)
        self.assertIn('data-history-view="leaderboard"', html)
        self.assertIn('id="identityGate"', html)
        self.assertIn('id="identityKeyInput"', html)
        self.assertIn('id="identityKeyButton"', html)
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
        self.assertIn("20260904-current-run-cash-export-3", detail)
        self.assertIn("策略诊断", detail)
        self.assertIn("导出CSV", detail)
        self.assertIn("时间窗口", detail)

        with opener.open(f"{self.base_url}/backtest/permanent-investment/static/app.js", timeout=10) as resp:
            app_js = resp.read().decode("utf-8")
        self.assertIn("实际到账现金分红", app_js)
        self.assertIn("2023—2025 年未实施利润分配", app_js)
        self.assertIn("annual_expense_drag_rate", app_js)
        self.assertIn("H20269全收益指数代理阶段", app_js)
        self.assertIn("0.63%/年", app_js)
        self.assertIn("/backtest/permanent-investment", app_js)
        self.assertIn("MAX_RUN_HISTORY = 20", app_js)
        self.assertIn("tradeAssetName", app_js)
        self.assertIn("标的名称: tradeAssetName(row.symbol)", app_js)
        self.assertIn("rebalanceAssetColumnName", app_js)
        self.assertIn("rebalanceCashEquivalentSymbols", app_js)
        self.assertIn('return `${name}（${code}）`', app_js)
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
