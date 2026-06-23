from __future__ import annotations

import json
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
        self.assertGreater(result["summary"]["final_asset_cny"], 0)
        detail = http_json(f"{self.base_url}/api/backtest/{run_id}")
        series = http_json(f"{self.base_url}/api/backtest/{run_id}/series")
        rebalance = http_json(f"{self.base_url}/api/backtest/{run_id}/rebalance")
        trades = http_json(f"{self.base_url}/api/backtest/{run_id}/trades")
        positions = http_json(f"{self.base_url}/api/backtest/{run_id}/positions?limit=2")
        self.assertEqual(detail["run_id"], run_id)
        self.assertGreater(len(series["series"]), 20)
        self.assertGreaterEqual(len(rebalance["rebalance"]), 1)
        self.assertGreater(len(trades["trades"]), 0)
        self.assertLessEqual(len(positions["positions"]), 2)

    def test_static_index_is_served(self) -> None:
        opener = request.build_opener(request.ProxyHandler({}))
        with opener.open(f"{self.base_url}/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("跨市场组合回测", html)
        self.assertNotIn("syncBtn", html)

    def test_run_backtest_auto_syncs_when_data_is_missing(self) -> None:
        db_path = temp_db_path()
        init_db(db_path)
        cfg = normalize_config({"start_date": "2020-01-01", "end_date": "2020-02-28"})
        original_sync_all = main_module.sync_all

        def fake_sync_all(conn, token, start, end, assets, repo_symbol="204001"):
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


if __name__ == "__main__":
    unittest.main()
