from __future__ import annotations

import unittest
from threading import Thread
from urllib import error

from app.config import normalize_config
from app.db import add_leaderboard_membership, db_session, init_db, json_dumps
from app.identity import IDENTITY_COOKIE_NAME, leaderboard_key_id
from app.main import create_server, rebalance_display_payload
from app.services.backtest_engine import BACKTEST_ENGINE_VERSION
from tests.helpers import temp_db_path
from tests.test_api import http_json


class RunMetadataTests(unittest.TestCase):
    def setUp(self):
        self.db_path = temp_db_path()
        self.server = create_server(port=0, db_path=self.db_path)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}"
        self.key_a = leaderboard_key_id("metadata-private-a")
        self.key_b = leaderboard_key_id("metadata-private-b")
        self.config = normalize_config({"start_date": "2020-01-01", "end_date": "2020-12-31"})
        self.summary = {
            "engine_version": BACKTEST_ENGINE_VERSION, "start_date": "2020-01-02", "end_date": "2020-12-31",
            "annualized_return": 0.12, "max_drawdown": -0.08, "ranking_eligible": True, "ranking_score": 80,
            "total_fees_cny": 345.6, "total_spend_cny": 60000, "rebalance_trade_count": 3, "trade_count": 9,
            "final_asset_cny": 1060000,
        }
        with db_session(self.db_path) as conn:
            for index in range(31):
                run_id = f"metadata-{index:02}"
                conn.execute(
                    "INSERT INTO backtest_runs(run_id,created_at,config_json,summary_json) VALUES(?,?,?,?)",
                    (run_id, f"2020-01-01T00:{index:02}:00", json_dumps(self.config), json_dumps(self.summary)),
                )
                add_leaderboard_membership(conn, self.key_a, run_id)
                add_leaderboard_membership(conn, self.key_b, run_id)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(self, path, payload=None, method=None, key=None):
        return http_json(self.url + path, payload, method, {"Cookie": f"{IDENTITY_COOKIE_NAME}={key or self.key_a}"})

    def test_metadata_is_private_and_reloaded_from_all_entry_points(self):
        expected = {"name": "季度稳健", "note": "第一行\n第二行", "favorite": True}
        saved = self.call("/api/backtest/metadata-30/metadata", expected)
        self.assertEqual(saved["metadata"], expected)
        self.assertEqual(self.call("/api/backtest/metadata-30")["metadata"], expected)
        for path in ("/api/backtest/history", "/api/backtest/leaderboard?period=all"):
            entries = self.call(path)["records"]
            self.assertEqual(next(row for row in entries if row["run_id"] == "metadata-30")["metadata"], expected)
            other = self.call(path, key=self.key_b)["records"]
            self.assertEqual(next(row for row in other if row["run_id"] == "metadata-30")["metadata"], {"name": "", "note": "", "favorite": False})
        self.call("/api/backtest/metadata-30/metadata", {"name": "另一人的名字"}, key=self.key_b)
        self.assertEqual(self.call("/api/backtest/metadata-30")["metadata"], expected)
        with db_session(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM run_metadata WHERE run_id='metadata-30'").fetchone()[0], 2)

    def test_partial_updates_preserve_other_fields_and_can_clear_favorite(self):
        self.call("/api/backtest/metadata-30/metadata", {"name": " 原名 ", "note": "保留说明", "favorite": True})
        result = self.call("/api/backtest/metadata-30/metadata", {"favorite": False})
        self.assertEqual(result["metadata"], {"name": "原名", "note": "保留说明", "favorite": False})
        self.assertEqual(self.call("/api/backtest/history?favorites=1")["records"], [])

    def test_all_favorites_are_available_beyond_recent_twenty(self):
        recent = self.call("/api/backtest/history")["records"]
        self.assertEqual(len(recent), 20)
        self.assertNotIn("metadata-00", {row["run_id"] for row in recent})
        for index in range(25):
            self.call(f"/api/backtest/metadata-{index:02}/metadata", {"favorite": True})
        favorites = self.call("/api/backtest/history?favorites=1")["records"]
        self.assertEqual(len(favorites), 25)
        self.assertIn("metadata-00", {row["run_id"] for row in favorites})
        self.assertEqual(self.call("/api/backtest/history?favorites=1", key=self.key_b)["records"], [])

    def test_metadata_validation_is_strict_and_does_not_write(self):
        for payload in ({}, {"name": "长" * 81}, {"note": "x" * 1001}, {"name": None}, {"note": 1}, {"favorite": 1}, {"favorite": "false"}, {"key_id": self.key_b}):
            with self.subTest(payload=payload), self.assertRaises(error.HTTPError) as raised:
                self.call("/api/backtest/metadata-30/metadata", payload)
            self.assertEqual(raised.exception.code, 400)
        self.assertEqual(self.call("/api/backtest/metadata-30")["metadata"], {"name": "", "note": "", "favorite": False})
        self.call("/api/backtest/metadata-30/metadata", {"name": "名" * 80, "note": "n" * 1000})
        with self.assertRaises(error.HTTPError) as raised:
            self.call("/api/backtest/missing/metadata", {"favorite": True})
        self.assertEqual(raised.exception.code, 404)

    def test_additive_migration_preserves_runs_and_metadata(self):
        # Simulate an existing installation before private metadata existed.
        with db_session(self.db_path) as conn:
            conn.execute("DROP TABLE run_metadata")
        init_db(self.db_path)
        self.call("/api/backtest/metadata-00/metadata", {"name": "保留"})
        init_db(self.db_path)
        init_db(self.db_path)
        self.assertEqual(self.call("/api/backtest/metadata-00")["metadata"]["name"], "保留")
        with db_session(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0], 31)
        self.call("/api/backtest/metadata-00", method="DELETE")
        with db_session(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM run_metadata WHERE run_id='metadata-00'").fetchone()[0], 0)

    def test_archive_keeps_comparison_inputs_and_actual_execution_metrics(self):
        entry = self.call("/api/backtest/history")["records"][0]
        for key in ("initial_capital_cny", "monthly_spend_cny", "monthly_spend_annual_growth", "repo_target_mode", "repo_fixed_target_cny", "fees", "dip_buy_enabled", "allow_fractional_us_shares"):
            self.assertEqual(entry["config"][key], self.config[key])
        for key in ("start_date", "end_date", "total_fees_cny", "total_spend_cny", "rebalance_trade_count", "trade_count", "final_asset_cny"):
            self.assertEqual(entry["summary"][key], self.summary[key])
        self.assertIn("key", entry["config"]["assets"][0])

    def test_rebalance_exposes_stored_weights_without_inventing_old_values(self):
        payload = {"before_weights": {"TEST": .6}, "after_weights": {"TEST": .5}, "targets": {"TEST": .5}, "desired_weights": {"TEST": .5}, "weight_basis": "open", "rebalance_reason": "threshold_exceeded"}
        self.assertEqual(rebalance_display_payload(payload), payload)
        self.assertEqual(rebalance_display_payload({"year_label": "2020"}), {"year_label": "2020"})


if __name__ == "__main__":
    unittest.main()
