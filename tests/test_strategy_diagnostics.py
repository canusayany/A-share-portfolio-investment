from __future__ import annotations

import json
import unittest

from app.db import db_session
from app.services.backtest_engine import run_backtest
from app.services.strategy_diagnostics import (
    diagnostic_window_config,
    leave_one_out_configs,
    local_weight_candidate_configs,
    portfolio_target_weights,
    strategy_diagnostics,
    stress_protection_statistics,
)
from tests.helpers import build_synced_db


class StrategyDiagnosticsTests(unittest.TestCase):
    def test_historical_rerun_replaces_retired_repo_selection_with_current_default(self) -> None:
        _db_path, config = build_synced_db("2020-01-01", "2020-02-28")
        config["repo_symbol"] = "511260.SH"

        normalized, _window = diagnostic_window_config(config, "all")

        self.assertEqual(normalized["repo_symbol"], "204001")

    def test_leave_one_out_redistributes_removed_weight_across_remaining_sleeves(self) -> None:
        _db_path, config = build_synced_db("2020-01-01", "2020-02-28")

        scenarios = leave_one_out_configs(config)

        self.assertEqual(
            [row["removed_symbol"] for row in scenarios],
            ["512890.SH", "CBA21801", "518880.SH"],
        )
        for row in scenarios:
            weights = row["weights"]
            self.assertAlmostEqual(sum(weights.values()), 1.0)
            self.assertEqual(weights[row["removed_symbol"]], 0.0)
            self.assertGreater(weights["REPO"], 0.25)
            removed = next(
                asset for asset in row["config"]["assets"] if asset["symbol"] == row["removed_symbol"]
            )
            self.assertFalse(removed["enabled"])
            self.assertEqual(removed["target_weight"], 0.0)

    def test_local_weight_candidates_transfer_five_percent_and_preserve_total(self) -> None:
        _db_path, config = build_synced_db("2020-01-01", "2020-02-28")

        scenarios = local_weight_candidate_configs(config, step=0.05)

        self.assertEqual(len(scenarios), 12)
        self.assertTrue(all(row["step"] == 0.05 for row in scenarios))
        for row in scenarios:
            self.assertAlmostEqual(sum(row["weights"].values()), 1.0)
            self.assertAlmostEqual(row["weights"][row["from_symbol"]], 0.20)
            self.assertAlmostEqual(row["weights"][row["to_symbol"]], 0.30)

    def test_fixed_cash_bucket_keeps_its_weight_outside_risk_asset_optimization(self) -> None:
        _db_path, config = build_synced_db("2020-01-01", "2020-02-28")
        config["repo_target_mode"] = "fixed_bucket"
        config["repo_fixed_target_cny"] = 0.0
        config["repo_fixed_target_ratio"] = 0.20

        base = portfolio_target_weights(config)
        removed = leave_one_out_configs(config)
        candidates = local_weight_candidate_configs(config, step=0.05)

        self.assertAlmostEqual(base["REPO"], 0.20)
        self.assertTrue(all(abs(row["weights"]["REPO"] - 0.20) < 1e-12 for row in removed))
        self.assertEqual(len(candidates), 6)
        self.assertTrue(all("REPO" not in {row["from_symbol"], row["to_symbol"]} for row in candidates))
        self.assertTrue(all(abs(row["weights"]["REPO"] - 0.20) < 1e-12 for row in candidates))

    def test_stress_protection_uses_monthly_total_returns_for_selected_window(self) -> None:
        db_path, config = build_synced_db("2020-01-01", "2022-12-31")

        with db_session(db_path) as conn:
            result = stress_protection_statistics(conn, config, "all")

        self.assertTrue(result["available"])
        self.assertEqual(result["frequency"], "monthly")
        self.assertEqual(result["stress_definition"], "红利ETF月度总收益小于0")
        self.assertGreater(result["comparable_periods"], 0)
        self.assertEqual(
            [asset["key"] for asset in result["assets"]],
            ["treasury_30y", "dividend_low_vol", "gold"],
        )
        for asset in result["assets"]:
            self.assertIn("stress_positive_periods", asset)
            self.assertIn("stress_positive_rate", asset)
            self.assertIn("stress_average_return", asset)
            self.assertIn("worst_decile_positive_rate", asset)
            self.assertIn("stress_correlation_with_dividend", asset)

    def test_strategy_diagnostics_runs_real_counterfactuals_and_local_candidates(self) -> None:
        db_path, config = build_synced_db("2020-01-01", "2020-02-28")
        with db_session(db_path) as conn:
            stored = run_backtest(conn, config)
            result = strategy_diagnostics(conn, config, stored["summary"], "all")

        self.assertEqual(result["window"]["key"], "all")
        self.assertEqual(result["base"]["annualized_return"], stored["summary"]["annualized_return"])
        self.assertEqual(len(result["asset_effects"]), 3)
        self.assertEqual(len(result["optimization_candidates"]), 13)
        self.assertTrue(any(row["current"] for row in result["optimization_candidates"]))
        self.assertAlmostEqual(sum(result["recommendation"]["weights"].values()), 1.0)
        self.assertEqual(result["methodology"]["weight_step"], 0.05)
        self.assertEqual(result["methodology"]["counterfactual_engine"], "完整回测引擎")
        self.assertTrue(result["stress_protection"]["available"])


if __name__ == "__main__":
    unittest.main()
