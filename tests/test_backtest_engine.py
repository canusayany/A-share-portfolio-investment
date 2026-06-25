from __future__ import annotations

import json
import unittest

from app.config import normalize_config
from app.db import db_session, rows_to_dicts
from app.services.backtest_engine import (
    BacktestError,
    _asset_period_performance,
    _repo_spend_reserve,
    benchmark_returns,
    comparison_assets,
    compute_metrics,
    effective_weights,
    minimal_rebalance_weights,
    repo_tenor_days,
    run_backtest,
    has_investable_asset_target,
    should_rebalance,
)
from tests.helpers import build_synced_db


class BacktestEngineTests(unittest.TestCase):
    def test_run_backtest_generates_summary_series_trades_and_rebalances(self) -> None:
        db_path, cfg = build_synced_db()
        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
            run_id = result["run_id"]
            daily = rows_to_dicts(conn.execute("SELECT * FROM portfolio_daily WHERE run_id=? ORDER BY trade_date", (run_id,)))
            trades = rows_to_dicts(conn.execute("SELECT * FROM trades WHERE run_id=?", (run_id,)))
            rebalances = rows_to_dicts(conn.execute("SELECT * FROM rebalance_events WHERE run_id=?", (run_id,)))
            cached = run_backtest(conn, cfg)
        self.assertGreater(result["summary"]["final_asset_cny"], 900000)
        self.assertEqual(result["summary"]["total_spend_cny"], 15000)
        self.assertGreaterEqual(result["summary"]["rebalance_count"], 1)
        self.assertGreater(len(daily), 40)
        self.assertGreater(len(trades), 0)
        self.assertGreater(len(rebalances), 0)
        payload = json.loads(daily[-1]["payload_json"])
        self.assertIn("weights", payload)
        self.assertIn("unrealized_pnl_cny", payload)
        self.assertIn("comparison", payload)
        self.assertGreater(payload["comparison"]["total_asset_cny"], 0)
        self.assertGreater(result["summary"]["comparison_final_asset_cny"], 0)
        rebalance_payload = json.loads(rebalances[0]["payload_json"])
        self.assertIn("asset_performance", rebalance_payload)
        self.assertIn("REPO", rebalance_payload["asset_performance"])
        self.assertIn("profit_cny", rebalance_payload["asset_performance"]["REPO"])
        self.assertIn("return", rebalance_payload["asset_performance"]["REPO"])
        self.assertIn("period_max_drawdown", rebalance_payload)
        self.assertLessEqual(rebalance_payload["period_max_drawdown"], 0)
        self.assertEqual(cached["run_id"], run_id)
        self.assertTrue(cached["cache"]["hit"])

    def test_disabled_asset_weight_flows_to_repo(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-02-28")
        cfg["assets"][0]["enabled"] = False
        cfg["assets"][1]["enabled"] = False
        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
            daily = rows_to_dicts(conn.execute("SELECT payload_json FROM portfolio_daily WHERE run_id=? ORDER BY trade_date", (result["run_id"],)))
        first_payload = json.loads(daily[0]["payload_json"])
        self.assertEqual(first_payload["targets"].get("VOO", 0), 0)
        self.assertGreater(first_payload["targets"]["REPO"], 0.6)

    def test_us_and_cn_purchase_rules_are_separate(self) -> None:
        db_path, cfg = build_synced_db("2013-01-01", "2013-03-31")
        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
            trades = rows_to_dicts(conn.execute("SELECT * FROM trades WHERE run_id=? AND side='BUY'", (result["run_id"],)))

        voo_buys = [trade for trade in trades if trade["symbol"] == "VOO"]
        cn_buys = [trade for trade in trades if trade["currency"] == "CNY"]
        self.assertTrue(voo_buys)
        self.assertTrue(cn_buys)
        self.assertTrue(any(abs(trade["quantity"] - round(trade["quantity"])) > 1e-6 for trade in voo_buys))
        self.assertTrue(all(trade["quantity"] % 100 == 0 for trade in cn_buys))
        self.assertTrue(all(trade["currency"] == "USD" for trade in voo_buys))

    def test_initial_buy_uses_first_calculable_day_after_missing_start_data(self) -> None:
        db_path, cfg = build_synced_db("2019-01-01", "2020-02-28")
        with db_session(db_path) as conn:
            conn.execute("DELETE FROM prices WHERE trade_date='2019-01-01'")
            conn.execute("DELETE FROM repo_rates WHERE trade_date='2019-01-01'")
            result = run_backtest(conn, cfg)
            first_buy = conn.execute(
                "SELECT trade_date FROM trades WHERE run_id=? AND side='BUY' ORDER BY trade_date LIMIT 1",
                (result["run_id"],),
            ).fetchone()
            first_rebalance = conn.execute(
                "SELECT rebalance_date FROM rebalance_events WHERE run_id=? ORDER BY rebalance_date LIMIT 1",
                (result["run_id"],),
            ).fetchone()

        self.assertIsNotNone(first_buy)
        self.assertIsNotNone(first_rebalance)
        self.assertEqual(first_buy["trade_date"], "2019-01-02")
        self.assertEqual(first_rebalance["rebalance_date"], "2019-01-02")

    def test_cash_only_targets_do_not_consume_initial_rebalance(self) -> None:
        self.assertFalse(has_investable_asset_target({"REPO": 1.0}))
        self.assertTrue(has_investable_asset_target({"VOO": 0.2, "REPO": 0.8}))

    def test_start_before_inception_keeps_unlisted_fund_in_repo(self) -> None:
        db_path, cfg = build_synced_db("2013-01-01", "2013-02-28")
        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
            first = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? ORDER BY trade_date LIMIT 1",
                (result["run_id"],),
            ).fetchone()
        payload = json.loads(first["payload_json"])
        self.assertNotIn("512890.SH", payload["targets"])
        self.assertGreater(payload["targets"]["REPO"], 0.5)

    def test_start_before_repo_data_keeps_cash_until_first_real_repo_rate(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-02-28")
        with db_session(db_path) as conn:
            conn.execute("DELETE FROM repo_rates WHERE trade_date < '2020-01-10'")
            result = run_backtest(conn, cfg)
            first = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? ORDER BY trade_date LIMIT 1",
                (result["run_id"],),
            ).fetchone()
            after_repo = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? AND trade_date >= '2020-01-10' ORDER BY trade_date LIMIT 1",
                (result["run_id"],),
            ).fetchone()
        first_payload = json.loads(first["payload_json"])
        after_payload = json.loads(after_repo["payload_json"])
        self.assertEqual(first_payload["repo_lots"], 0)
        self.assertGreater(after_payload["repo_lots"], 0)

    def test_repo_tenor_selection_uses_selected_symbol_and_duration(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-31")
        cfg["repo_symbol"] = "204007"
        with db_session(db_path) as conn:
            conn.execute("DELETE FROM repo_rates")
            from tests.helpers import fixture_repo_rates

            from app.db import insert_many

            insert_many(conn, "repo_rates", fixture_repo_rates(cfg["start_date"], cfg["end_date"], "204007"))
            insert_many(conn, "repo_rates", fixture_repo_rates(cfg["start_date"], cfg["end_date"], "204001"))
            result = run_backtest(conn, cfg)
            first = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? ORDER BY trade_date LIMIT 1",
                (result["run_id"],),
            ).fetchone()
        payload = json.loads(first["payload_json"])
        self.assertEqual(repo_tenor_days(cfg), 7)
        self.assertGreater(payload["repo_lots"], 0)

    def test_repo_purchase_reserves_monthly_spend_during_tenor(self) -> None:
        from datetime import date

        reserve = _repo_spend_reserve(date(2020, 1, 20), 28, {date(2020, 2, 3), date(2020, 3, 2)}, 5000)
        self.assertEqual(reserve, 5000)

    def test_reserved_cash_can_use_one_day_repo(self) -> None:
        db_path, cfg = build_synced_db("2020-01-20", "2020-02-10")
        cfg["repo_symbol"] = "204028"
        cfg["monthly_spend_cny"] = 5000
        with db_session(db_path) as conn:
            conn.execute("DELETE FROM repo_rates")
            from tests.helpers import fixture_repo_rates

            from app.db import insert_many

            insert_many(conn, "repo_rates", fixture_repo_rates(cfg["start_date"], cfg["end_date"], "204028"))
            insert_many(conn, "repo_rates", fixture_repo_rates(cfg["start_date"], cfg["end_date"], "204001"))
            result = run_backtest(conn, cfg)
            first = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? ORDER BY trade_date LIMIT 1",
                (result["run_id"],),
            ).fetchone()
        payload = json.loads(first["payload_json"])
        self.assertGreaterEqual(payload["repo_lots"], 2)

    def test_metrics_are_cash_flow_adjusted(self) -> None:
        daily, cumulative, drawdowns = compute_metrics([100, 105, 100], [0, 0, -10], [1, 1, 1])
        self.assertAlmostEqual(daily[1], 0.05)
        self.assertAlmostEqual(daily[2], (100 - 105 + 10) / 105)
        self.assertGreater(cumulative[-1], 0)
        self.assertLessEqual(min(drawdowns), 0)

    def test_minimal_rebalance_moves_only_to_band_edge(self) -> None:
        desired = minimal_rebalance_weights({"A": 0.60, "REPO": 0.40}, {"A": 0.50, "REPO": 0.50}, 0.02)
        self.assertAlmostEqual(desired["A"], 0.52)
        self.assertAlmostEqual(desired["REPO"], 0.48)

    def test_asset_performance_excludes_monthly_spend_flow(self) -> None:
        performance = _asset_period_performance(
            {"REPO": 1000000.0},
            {"REPO": 963995.7768520006},
            ["REPO"],
            {"REPO": -60000.0},
        )
        self.assertGreater(performance["REPO"]["profit_cny"], 0)
        self.assertGreater(performance["REPO"]["return"], 0)

    def test_us_dividends_create_withheld_tax(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-12-31")
        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
        self.assertGreater(result["summary"]["withheld_tax_cny"], 0)

    def test_comparison_assets_roll_risk_weights_into_hs300(self) -> None:
        cfg = normalize_config({})
        assets = comparison_assets(cfg)
        weights = {asset["symbol"]: asset["target_weight"] for asset in assets}
        self.assertAlmostEqual(weights["510300.SH"], 0.40)
        self.assertAlmostEqual(weights["518880.SH"], 0.10)

    def test_benchmark_forward_fill_and_rebalance_band(self) -> None:
        self.assertAlmostEqual(benchmark_returns([None, 100, None, 110])[-1], 0.1)
        self.assertFalse(should_rebalance({"A": 0.51, "REPO": 0.49}, {"A": 0.50, "REPO": 0.50}, 0.02))
        self.assertTrue(should_rebalance({"A": 0.55, "REPO": 0.45}, {"A": 0.50, "REPO": 0.50}, 0.02))

    def test_validation_errors_surface(self) -> None:
        db_path, _cfg = build_synced_db("2020-01-01", "2020-01-31")
        bad = normalize_config({"start_date": "2020-02-01", "end_date": "2020-01-01"})
        with db_session(db_path) as conn:
            with self.assertRaises(BacktestError):
                run_backtest(conn, bad)

    def test_effective_weights_respect_price_and_inception(self) -> None:
        cfg = normalize_config({"start_date": "2012-01-01"})
        weights = effective_weights(cfg, __import__("datetime").date(2012, 1, 3), {"VOO": 1, "510300.SH": None})
        self.assertIn("VOO", weights)
        self.assertNotIn("510300.SH", weights)
        self.assertGreater(weights["REPO"], 0.7)


if __name__ == "__main__":
    unittest.main()
