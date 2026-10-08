from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
import unittest

from app.db import db_session
from app.services.backtest_engine import repo_fixed_target_weight, run_backtest
from tests.helpers import build_synced_db


class RebalanceFrequencyAuditTests(unittest.TestCase):
    @staticmethod
    def stock_cash_config(config: dict, *, weight: float = 0.5) -> dict:
        config = deepcopy(config)
        config.update(monthly_spend_cny=0, dip_buy_enabled=False)
        for asset in config["assets"]:
            asset["enabled"] = asset["symbol"] == "510300.SH"
            asset["target_weight"] = weight if asset["enabled"] else 0.0
        return config

    @staticmethod
    def flat_prices(conn) -> None:
        conn.execute("UPDATE prices SET open=1,high=1,low=1,close=1,adj_close=1 WHERE symbol='510300.SH'")
        conn.execute("DELETE FROM fund_dividends")
        conn.execute("UPDATE repo_rates SET open_rate=0,close_rate=0,high_rate=0,low_rate=0")

    @staticmethod
    def run_strategy(conn, config: dict) -> dict:
        return run_backtest(
            conn, config, include_comparison=False,
            include_month_analysis=False, include_rolling_analysis=False,
        )

    @staticmethod
    def events(conn, run_id: str) -> list[dict]:
        return [
            {"date": row["rebalance_date"], **json.loads(row["payload_json"])}
            for row in conn.execute(
                "SELECT rebalance_date,payload_json FROM rebalance_events WHERE run_id=? ORDER BY rebalance_date",
                (run_id,),
            )
        ]

    def test_all_frequencies_produce_distinct_calendar_checks_without_forcing_trades(self) -> None:
        db_path, original = build_synced_db("2020-01-02", "2020-12-31")
        config = self.stock_cash_config(original)
        config.update(rebalance_band=0.25, annual_rebalance_month=6)
        expected_counts = {"daily": 260, "weekly": 52, "monthly": 11, "quarterly": 3, "semiannual": 1, "yearly": 1}
        run_ids = set()
        with db_session(db_path) as conn:
            self.flat_prices(conn)
            for frequency, expected in expected_counts.items():
                with self.subTest(frequency=frequency):
                    config["rebalance_frequency"] = frequency
                    result = self.run_strategy(conn, config)
                    run_ids.add(result["run_id"])
                    summary = result["summary"]
                    events = self.events(conn, result["run_id"])
                    scheduled = [event for event in events if event["event_type"] == "scheduled"]
                    self.assertEqual(summary["rebalance_frequency"], frequency)
                    self.assertEqual(summary["rebalance_check_count"], expected)
                    self.assertEqual(summary["rebalance_no_trade_count"], expected)
                    self.assertEqual(summary["rebalance_within_band_count"], expected)
                    self.assertEqual(summary["rebalance_trade_count"], 0)
                    self.assertEqual(summary["initial_allocation_count"], 1)
                    self.assertEqual(summary["rebalance_count"], expected + 1)
                    self.assertTrue(all(event["executed_trade_count"] == 0 for event in scheduled))
                    self.assertTrue(all(not event["rebalanced"] for event in scheduled))
                    self.assertTrue(all(event["rebalance_reason"] == "within_band" for event in scheduled))
                    if frequency == "yearly":
                        self.assertEqual([event["date"] for event in scheduled], ["2020-06-01"])
                    if frequency == "quarterly":
                        self.assertEqual([event["date"] for event in scheduled], ["2020-04-01", "2020-07-01", "2020-10-01"])
                    if frequency == "semiannual":
                        self.assertEqual([event["date"] for event in scheduled], ["2020-07-01"])
                    if frequency == "weekly":
                        self.assertTrue(all(date.fromisoformat(event["date"]).weekday() == 0 for event in scheduled))
                    cached = self.run_strategy(conn, config)
                    self.assertEqual(cached["run_id"], result["run_id"])
                    self.assertTrue(cached["cache"]["hit"])
        self.assertEqual(len(run_ids), 6)

    def test_frequency_changes_real_trades_and_return_when_threshold_is_breached(self) -> None:
        db_path, original = build_synced_db("2020-01-02", "2020-12-31")
        config = self.stock_cash_config(original)
        config.update(rebalance_band=0.02, rebalance_to_target=True)
        results = {}
        with db_session(db_path) as conn:
            self.flat_prices(conn)
            rows = conn.execute("SELECT trade_date FROM prices WHERE symbol='510300.SH' ORDER BY trade_date").fetchall()
            for index, row in enumerate(rows):
                price = 1.0 + index * 0.01
                conn.execute(
                    "UPDATE prices SET open=?,high=?,low=?,close=?,adj_close=? WHERE symbol='510300.SH' AND trade_date=?",
                    (price, price, price, price, price, row["trade_date"]),
                )
            for frequency in ("daily", "weekly", "monthly", "quarterly", "semiannual", "yearly"):
                config["rebalance_frequency"] = frequency
                result = self.run_strategy(conn, config)
                results[frequency] = result["summary"]
                events = self.events(conn, result["run_id"])
                for event in events:
                    actual = conn.execute(
                        "SELECT COUNT(*) FROM trades WHERE run_id=? AND trade_date=? AND reason='rebalance'",
                        (result["run_id"], event["date"]),
                    ).fetchone()[0]
                    self.assertEqual(event["executed_trade_count"], actual)
                    self.assertEqual(event["rebalanced"], actual > 0)
            self.assertGreater(results["daily"]["rebalance_trade_count"], results["monthly"]["rebalance_trade_count"])
            self.assertGreater(results["monthly"]["rebalance_trade_count"], results["semiannual"]["rebalance_trade_count"])
            self.assertEqual(results["yearly"]["rebalance_trade_count"], 0)
            self.assertNotAlmostEqual(results["daily"]["final_asset_cny"], results["yearly"]["final_asset_cny"])

    def test_unfilled_rebalance_is_reported_as_constraints_not_success(self) -> None:
        db_path, original = build_synced_db("2020-01-02", "2020-03-02")
        config = self.stock_cash_config(original)
        config.update(initial_capital_cny=100, rebalance_frequency="monthly")
        with db_session(db_path) as conn:
            self.flat_prices(conn)
            result = self.run_strategy(conn, config)
            events = self.events(conn, result["run_id"])
        self.assertTrue(all(event["threshold_exceeded"] for event in events))
        self.assertTrue(all(event["rebalance_reason"] == "trade_constraints" for event in events))
        self.assertTrue(all(not event["rebalanced"] for event in events))
        self.assertEqual(result["summary"]["rebalance_trade_count"], 0)
        self.assertEqual(result["summary"]["rebalance_constrained_count"], 2)

    def test_first_allocation_ignores_tolerance_and_buys_target(self) -> None:
        db_path, original = build_synced_db("2020-01-02", "2020-01-10")
        config = self.stock_cash_config(original)
        config["rebalance_band"] = 1.0
        with db_session(db_path) as conn:
            self.flat_prices(conn)
            result = self.run_strategy(conn, config)
            first = self.events(conn, result["run_id"])[0]
            day = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? ORDER BY trade_date LIMIT 1",
                (result["run_id"],),
            ).fetchone()
        self.assertEqual(first["event_type"], "initial_allocation")
        self.assertFalse(first["threshold_exceeded"])
        self.assertTrue(first["rebalanced"])
        self.assertGreater(first["executed_trade_count"], 0)
        self.assertAlmostEqual(json.loads(day["payload_json"])["weights"]["510300.SH"], 0.5, delta=0.001)

    def test_nonannual_strategies_disable_dip_buy_and_annual_scenarios(self) -> None:
        db_path, original = build_synced_db("2020-01-02", "2020-03-02")
        config = self.stock_cash_config(original)
        config.update(rebalance_frequency="monthly", dip_buy_enabled=True, rebalance_month_analysis_enabled=True)
        with db_session(db_path) as conn:
            result = run_backtest(conn, config, include_comparison=False, include_rolling_analysis=False)
        self.assertFalse(result["summary"]["dip_buy_active"])
        self.assertEqual(result["summary"]["dip_buy_count"], 0)
        self.assertEqual(result["summary"]["rebalance_month_scenarios"], [])

    def test_fixed_cash_amount_uses_execution_value_after_overnight_gap(self) -> None:
        db_path, original = build_synced_db("2020-01-02", "2020-02-04")
        config = self.stock_cash_config(original)
        config.update(
            rebalance_frequency="monthly", rebalance_band=0, rebalance_to_target=True,
            repo_target_mode="fixed_bucket", repo_fixed_target_cny=500_000, repo_fixed_target_ratio=0,
        )
        for field, value in config["fees"]["cn_etf"].items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                config["fees"]["cn_etf"][field] = 0
        config["fees"]["repo"]["investor_commission_rate"] = 0
        with db_session(db_path) as conn:
            self.flat_prices(conn)
            conn.execute(
                "UPDATE prices SET open=2,high=2,low=2,close=2,adj_close=2 "
                "WHERE symbol='510300.SH' AND trade_date>='2020-02-03'"
            )
            result = self.run_strategy(conn, config)
            event = self.events(conn, result["run_id"])[1]
            row = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? AND trade_date='2020-02-03'",
                (result["run_id"],),
            ).fetchone()
        self.assertAlmostEqual(event["targets"]["REPO"], 1 / 3)
        self.assertAlmostEqual(json.loads(row["payload_json"])["values"]["REPO"], 500_000)

    def test_fixed_cash_bucket_over_capital_keeps_portfolio_liquid(self) -> None:
        db_path, original = build_synced_db("2020-01-02", "2020-01-10")
        config = self.stock_cash_config(original)
        config.update(repo_target_mode="fixed_bucket", repo_fixed_target_cny=2_000_000)
        config["fees"]["repo"]["investor_commission_rate"] = 0
        self.assertEqual(repo_fixed_target_weight({**config, "repo_fixed_target_cny": 50}, 0), 1.0)
        self.assertEqual(repo_fixed_target_weight(config, 1_000_000), 1.0)
        with db_session(db_path) as conn:
            self.flat_prices(conn)
            result = self.run_strategy(conn, config)
        self.assertAlmostEqual(result["summary"]["final_asset_cny"], 1_000_000)
        self.assertEqual(result["summary"]["rebalance_trade_count"], 0)

    def test_spending_shortfall_is_reported_without_overdrawing_depleted_cash(self) -> None:
        db_path, original = build_synced_db("2020-01-02", "2020-03-02")
        config = deepcopy(original)
        config.update(
            initial_capital_cny=100, monthly_spend_cny=60,
            repo_target_mode="fixed_bucket", repo_fixed_target_cny=50, repo_fixed_target_ratio=0,
        )
        for asset in config["assets"]:
            asset["enabled"] = False
            asset["target_weight"] = 0
        with db_session(db_path) as conn:
            self.flat_prices(conn)
            result = self.run_strategy(conn, config)
            rows = conn.execute(
                "SELECT total_asset_cny,payload_json FROM portfolio_daily WHERE run_id=? ORDER BY trade_date",
                (result["run_id"],),
            ).fetchall()
        summary = result["summary"]
        self.assertEqual(summary["total_planned_spend_cny"], 180)
        self.assertEqual(summary["total_spend_cny"], 100)
        self.assertEqual(summary["total_spend_shortfall_cny"], 80)
        self.assertEqual(summary["spend_shortfall_count"], 2)
        self.assertEqual(summary["first_spend_shortfall_date"], "2020-02-03")
        self.assertEqual(summary["final_asset_cny"], 0)
        self.assertAlmostEqual(summary["net_profit_cny"], 0)
        self.assertAlmostEqual(summary["total_return"], 0)
        self.assertTrue(all(row["total_asset_cny"] >= 0 for row in rows))
        self.assertEqual(json.loads(rows[-1]["payload_json"])["spending"]["shortfall_cny"], 60)
        self.assertEqual(json.loads(rows[-1]["payload_json"])["targets"], {"REPO": 1.0})


if __name__ == "__main__":
    unittest.main()
