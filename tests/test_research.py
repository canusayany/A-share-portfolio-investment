from __future__ import annotations

from copy import deepcopy
from datetime import date
import json
import unittest
from unittest.mock import patch

from app.config import normalize_config, validate_config
from app.db import db_session
from app.services.backtest_engine import (
    BacktestCancelled, _next_spend_reserve, _repo_spend_reserve,
    living_expense_reserve, monthly_spend_amount, run_backtest,
)
from app.services.research import run_research, validate_research_request
from tests.helpers import build_synced_db


def cash_config(config: dict, **changes) -> dict:
    config = deepcopy(config)
    for asset in config["assets"]:
        asset.update(enabled=False, target_weight=0)
    config.update(initial_capital_cny=100_000, monthly_spend_cny=1_000, dip_buy_enabled=False)
    config.update(changes)
    return config


def freeze_cash_yield(conn) -> None:
    conn.execute("UPDATE repo_rates SET open_rate=0,close_rate=0,high_rate=0,low_rate=0")


def run_simple(conn, config, *, persist=False, comparison=False) -> dict:
    return run_backtest(
        conn, config, persist=persist, include_comparison=comparison,
        include_month_analysis=False, include_rolling_analysis=False,
    )


class WithdrawalGrowthTests(unittest.TestCase):
    def test_growth_uses_twelve_month_periods_not_calendar_years(self):
        config = normalize_config({"start_date": "2020-06-15", "monthly_spend_cny": 1_000, "monthly_spend_annual_growth": 0.1})
        self.assertEqual(monthly_spend_amount(config, date(2021, 1, 1)), 1_000)
        self.assertEqual(monthly_spend_amount(config, date(2021, 5, 31)), 1_000)
        self.assertAlmostEqual(monthly_spend_amount(config, date(2021, 6, 1)), 1_100)
        self.assertAlmostEqual(monthly_spend_amount(config, date(2022, 6, 1)), 1_210)
        self.assertAlmostEqual(living_expense_reserve(config, date(2021, 5, 1), include_current_month=True), 1_000 + 12 * 1_100 + 11 * 1_210)
        self.assertAlmostEqual(living_expense_reserve(config, date(2021, 5, 1)), 12 * 1_100 + 12 * 1_210)

    def test_growth_validation_rejects_nonfinite_or_out_of_range(self):
        for value in (-0.01, 0.5001, float("nan"), float("inf"), True, "oops", None):
            with self.subTest(value=value):
                errors = validate_config(normalize_config({"monthly_spend_annual_growth": value}))
                self.assertTrue(any("monthly_spend_annual_growth" in error for error in errors))
        self.assertEqual(normalize_config({})["monthly_spend_annual_growth"], 0)

    def test_real_engine_and_comparison_apply_growth_at_thirteenth_withdrawal(self):
        db_path, original = build_synced_db("2020-06-15", "2021-07-05")
        config = cash_config(original, monthly_spend_annual_growth=0.1)
        with db_session(db_path) as conn:
            freeze_cash_yield(conn)
            result = run_simple(conn, config, persist=True, comparison=True)
            summary = result["summary"]
            self.assertAlmostEqual(summary["total_spend_cny"], 12 * 1_000 + 2 * 1_100)
            self.assertAlmostEqual(summary["final_asset_cny"], 100_000 - 14_200 - summary["total_fees_cny"])
            self.assertAlmostEqual(summary["comparison_final_asset_cny"], summary["final_asset_cny"])
            self.assertEqual(summary["total_spend_shortfall_cny"], 0)
            rows = conn.execute("SELECT trade_date,payload_json FROM portfolio_daily WHERE run_id=?", (result["run_id"],)).fetchall()
            by_month = {}
            for row in rows:
                spend = json.loads(row["payload_json"])["spending"]
                if spend["planned_cny"]:
                    by_month[row["trade_date"][:7]] = spend["planned_cny"]
            self.assertEqual(by_month["2021-01"], 1_000)
            self.assertEqual(by_month["2021-05"], 1_000)
            self.assertAlmostEqual(by_month["2021-06"], 1_100)

    def test_closed_start_month_does_not_advance_growth_early(self):
        db_path, original = build_synced_db("2020-10-31", "2021-11-05")
        with db_session(db_path) as conn:
            freeze_cash_yield(conn)
            summary = run_simple(conn, cash_config(original, monthly_spend_annual_growth=0.1))["summary"]
            self.assertEqual(summary["start_date"], "2020-11-02")
            self.assertAlmostEqual(summary["total_spend_cny"], 12 * 1_000 + 1_100)

    def test_zero_growth_preserves_existing_default_metrics(self):
        db_path, original = build_synced_db("2020-01-02", "2021-02-05")
        config = cash_config(original)
        config.pop("monthly_spend_annual_growth")
        with db_session(db_path) as conn:
            freeze_cash_yield(conn)
            default = run_simple(conn, config)["summary"]
            zero = run_simple(conn, {**config, "monthly_spend_annual_growth": 0})["summary"]
        self.assertEqual(default["total_spend_cny"], 14_000)
        for key in ("final_asset_cny", "total_spend_cny", "total_fees_cny", "annualized_return", "max_drawdown"):
            self.assertEqual(default[key], zero[key])

    def test_repo_reserves_the_increased_payment(self):
        day, spend_day = date(2021, 6, 30), date(2021, 7, 1)
        schedule = {spend_day: 1_100.0, date(2021, 8, 2): 1_100.0}
        self.assertEqual(_next_spend_reserve(day, set(schedule), 1_000, schedule), 1_100)
        # A seven-day repo must leave the upcoming larger payment liquid.
        reserve = _repo_spend_reserve(day, 7, set(schedule), 1_000, spend_amounts=schedule)
        self.assertEqual(reserve, 1_100)


class ResearchValidationTests(unittest.TestCase):
    def setUp(self):
        self.base = normalize_config({"start_date": "2020-02-29", "end_date": "2023-12-31"})

    def test_grid_cartesian_limit_and_normalized_total(self):
        request = {"kind": "rebalance_grid", "frequencies": ["daily", "weekly", "monthly", "quarterly", "semiannual", "yearly"], "bands": [0, 0.01, 0.1, 1]}
        self.assertEqual(validate_research_request(self.base, request)["total"], 24)
        request["bands"].append(0.5)
        with self.assertRaisesRegex(ValueError, "24"):
            validate_research_request(self.base, request)

    def test_invalid_grid_inputs_are_rejected(self):
        for changes in ({"frequencies": []}, {"frequencies": ["never"]}, {"bands": []}, {"bands": [True]}, {"bands": [-0.1]}, {"bands": [1.1]}, {"bands": [float("nan")]}, {"bands": ["0.1"]}, {"bands": [0, 0]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_research_request(self.base, {"kind": "rebalance_grid", "frequencies": ["yearly"], "bands": [0.1], **changes})

    def test_invalid_stress_inputs_and_cartesian_size_are_rejected(self):
        valid = {"kind": "withdrawal_stress", "monthly_spends": [0, 1_000], "annual_growth_rates": [0, 0.1], "start_years": [2020, 2021]}
        for changes in ({"monthly_spends": [-1]}, {"monthly_spends": [float("inf")]}, {"annual_growth_rates": [0.51]}, {"start_years": [True]}, {"start_years": [2021.0]}, {"start_years": [2019]}, {"start_years": [2024]}, {"monthly_spends": list(range(7))}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_research_request(self.base, {**valid, **changes})


class ResearchExecutionTests(unittest.TestCase):
    def test_real_grid_matches_engine_without_writes_or_config_mutation(self):
        db_path, config = build_synced_db("2020-01-02", "2020-03-31")
        config.update(monthly_spend_cny=0, dip_buy_enabled=True)
        original = deepcopy(config)
        request = {"kind": "rebalance_grid", "frequencies": ["monthly", "yearly"], "bands": [0, 0.1]}
        progress = []
        with db_session(db_path) as conn:
            conn.execute("PRAGMA query_only=ON")
            changes_before = conn.total_changes
            result = run_research(conn, config, request, on_progress=lambda completed, total, rows: progress.append((completed, total, rows)))
            self.assertEqual([row["status"] for row in result["rows"]], ["success"] * 4, result)
            expected = run_simple(conn, {**config, "rebalance_frequency": "monthly", "rebalance_band": 0})["summary"]
            self.assertAlmostEqual(result["rows"][0]["final_asset_cny"], expected["final_asset_cny"])
            self.assertEqual(conn.total_changes, changes_before)
            for table in ("backtest_runs", "portfolio_daily", "trades", "rebalance_events", "leaderboard_memberships"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(config, original)
        self.assertEqual([item[:2] for item in progress], [(1, 4), (2, 4), (3, 4), (4, 4)])
        self.assertEqual(len(progress[0][2]), 1)
        self.assertFalse(result["rows"][0]["dip_buy_active"])
        self.assertTrue(result["rows"][2]["dip_buy_active"])

    def test_stress_reports_real_shortfalls_and_full_cartesian_inputs(self):
        db_path, original = build_synced_db("2020-01-02", "2021-02-05")
        config = cash_config(original, initial_capital_cny=13_000)
        request = {"kind": "withdrawal_stress", "monthly_spends": [0, 1_000], "annual_growth_rates": [0, 0.1], "start_years": [2020, 2021]}
        with db_session(db_path) as conn:
            freeze_cash_yield(conn)
            result = run_research(conn, config, request)
        self.assertEqual(result["total"], 8)
        self.assertTrue(all(row["status"] == "success" for row in result["rows"]), result)
        stressed = result["rows"][6]
        self.assertEqual(stressed["inputs"], {"monthly_spend_cny": 1_000, "monthly_spend_annual_growth": 0.1, "start_year": 2020})
        self.assertAlmostEqual(stressed["total_planned_spend_cny"], 14_200)
        self.assertAlmostEqual(stressed["total_spend_cny"], 13_000 - stressed["total_fees_cny"])
        self.assertAlmostEqual(stressed["total_spend_shortfall_cny"], 1_200 + stressed["total_fees_cny"])
        self.assertEqual(stressed["first_spend_shortfall_date"], "2021-01-01")
        self.assertEqual(stressed["spend_shortfall_count"], 2)
        self.assertEqual(stressed["final_asset_cny"], 0)

    def test_leap_day_start_is_clamped_and_end_is_fixed(self):
        db_path, original = build_synced_db("2020-02-29", "2021-03-05")
        config = cash_config(original)
        with db_session(db_path) as conn:
            result = run_research(conn, config, {"kind": "withdrawal_stress", "monthly_spends": [0], "annual_growth_rates": [0], "start_years": [2021]})
        row = result["rows"][0]
        self.assertEqual(row["status"], "success", row)
        self.assertEqual(row["requested_start_date"], "2021-02-28")
        self.assertEqual(row["start_date"], "2021-03-01")
        self.assertEqual(row["end_date"], config["end_date"])

    def test_missing_local_data_fails_rows_without_calling_engine(self):
        db_path, config = build_synced_db("2020-01-02", "2020-03-31")
        with db_session(db_path) as conn:
            conn.execute("DELETE FROM prices WHERE symbol='000300.SH'")
            with patch("app.services.research.run_backtest") as engine:
                result = run_research(conn, config, {"kind": "rebalance_grid", "frequencies": ["monthly", "yearly"], "bands": [0]})
            engine.assert_not_called()
        self.assertEqual(result["completed"], 2)
        self.assertTrue(all(row["status"] == "failed" and "000300.SH" in row["error"] for row in result["rows"]))

    def test_cancel_before_start_and_between_rows_preserves_partial_progress(self):
        db_path, config = build_synced_db("2020-01-02", "2020-03-31")
        request = {"kind": "rebalance_grid", "frequencies": ["monthly", "yearly"], "bands": [0]}
        progress = []
        with db_session(db_path) as conn:
            with self.assertRaises(BacktestCancelled):
                run_research(conn, config, request, should_cancel=lambda: True)
            with self.assertRaises(BacktestCancelled):
                run_research(conn, config, request, should_cancel=lambda: bool(progress), on_progress=lambda completed, total, rows: progress.append(rows))
        self.assertEqual(len(progress), 1)
        self.assertEqual(len(progress[0]), 1)

    def test_cancel_during_engine_is_not_converted_to_failed_row(self):
        db_path, config = build_synced_db("2020-01-02", "2020-03-31")
        checks = []
        progress = []
        def cancel_in_engine():
            checks.append(True)
            return len(checks) >= 7
        with db_session(db_path) as conn, self.assertRaises(BacktestCancelled):
            run_research(conn, config, {"kind": "rebalance_grid", "frequencies": ["yearly"], "bands": [0]}, should_cancel=cancel_in_engine, on_progress=lambda *args: progress.append(args))
        self.assertEqual(progress, [])

    def test_one_scenario_failure_does_not_stop_remaining_rows(self):
        db_path, config = build_synced_db("2020-01-02", "2020-03-31")
        real_engine = run_backtest
        def fail_monthly(conn, scenario, **kwargs):
            if scenario["rebalance_frequency"] == "monthly":
                raise ValueError("fixture scenario failure")
            return real_engine(conn, scenario, **kwargs)
        with db_session(db_path) as conn, patch("app.services.research.run_backtest", side_effect=fail_monthly):
            result = run_research(conn, config, {"kind": "rebalance_grid", "frequencies": ["monthly", "yearly"], "bands": [0]})
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["rows"][0]["status"], "failed")
        self.assertEqual(result["rows"][0]["error"], "fixture scenario failure")
        self.assertEqual(result["rows"][1]["status"], "success")

    def test_rebalance_events_preserve_actual_post_trade_weights(self):
        db_path, config = build_synced_db("2020-01-02", "2020-03-31")
        config.update(monthly_spend_cny=0, rebalance_frequency="monthly", rebalance_band=0, dip_buy_enabled=False)
        with db_session(db_path) as conn:
            result = run_simple(conn, config, persist=True)
            rows = conn.execute("SELECT * FROM rebalance_events WHERE run_id=?", (result["run_id"],)).fetchall()
        self.assertGreater(len(rows), 1)
        for row in rows:
            payload = json.loads(row["payload_json"])
            self.assertEqual(payload["before_weights"], payload["current_weights"])
            self.assertAlmostEqual(sum(payload["before_weights"].values()), 1)
            self.assertAlmostEqual(sum(payload["after_weights"].values()), 1)
            self.assertIn("REPO", payload["after_weights"])
            self.assertIn("dividend_receivables", payload["weight_basis"])
        first = json.loads(rows[0]["payload_json"])
        self.assertNotEqual(first["before_weights"], first["after_weights"])
