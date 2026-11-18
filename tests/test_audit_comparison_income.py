import json
import unittest

from app.config import normalize_config
from app.db import db_session, insert_many
from app.services.backtest_engine import run_backtest
from app.services.money_fund import MoneyFundIncome
from tests.helpers import build_synced_db, seed_fixture_data


def one_asset(config, symbol=None, **overrides):
    return normalize_config({
        **config,
        "monthly_spend_cny": 0,
        "fees": {
            **config["fees"],
            "cn_etf": {**config["fees"]["cn_etf"], "commission_rate": 0},
            "slippage": {"cn_bps": 0, "hk_bps": 0, "us_bps": 0},
        },
        "assets": [
            {**asset, "enabled": asset["symbol"] == symbol, "target_weight": int(asset["symbol"] == symbol)}
            for asset in config["assets"]
        ],
        **overrides,
    })


class AuditComparisonIncomeTests(unittest.TestCase):
    def test_comparison_stops_at_first_missing_session(self):
        db_path, base = build_synced_db("2024-03-04", "2024-03-08")
        config = one_asset(base, "512890.SH")
        with db_session(db_path) as conn:
            conn.execute("DELETE FROM prices WHERE symbol='510300.SH' AND trade_date='2024-03-06'")
            result = run_backtest(conn, config)
            summary = result["summary"]
            self.assertEqual(summary["data_coverage"]["strategy"]["end_date"], "2024-03-08")
            self.assertEqual(summary["data_coverage"]["comparison"]["end_date"], "2024-03-05")
            self.assertIn("prices:510300.SH", summary["data_coverage"]["comparison"]["gaps"])
            last_payload = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? AND trade_date='2024-03-08'",
                (result["run_id"],),
            ).fetchone()[0]
            self.assertIsNone(json.loads(last_payload)["comparison"]["total_asset_cny"])

    def test_identical_gold_comparison_executes_same_switch(self):
        db_path, base = build_synced_db("2020-12-29", "2021-01-12")
        config = one_asset(base, "518880.SH", annual_rebalance_month=8)
        with db_session(db_path) as conn:
            summary = run_backtest(conn, config)["summary"]
        self.assertEqual(summary["data_coverage"]["comparison"]["end_date"], "2021-01-12")
        self.assertAlmostEqual(summary["comparison_final_asset_cny"], summary["final_asset_cny"])

    def test_money_fund_holiday_income_rolls_into_whole_shares(self):
        start, end = "2024-01-05", "2024-01-08"
        db_path, base = build_synced_db(start, end)
        config = one_asset(base, repo_symbol="511990.SH")
        with db_session(db_path) as conn:
            seed_fixture_data(conn, config, start, end)
            conn.execute("UPDATE prices SET open=100, high=100, low=100, close=100 WHERE symbol='511990.SH'")
            insert_many(conn, "money_fund_income", [
                {"symbol": "511990.SH", "income_date": day, "income_per_100": income, "source": "fixture"}
                for day, income in [(start, 0.5), ("2024-01-07", 1.0), (end, 0.5)]
            ])
            summary = run_backtest(conn, config)["summary"]
        self.assertAlmostEqual(summary["total_dividend_cny"], 200)
        self.assertAlmostEqual(summary["final_asset_cny"], 1_000_200)

    def test_money_fund_remaining_distribution_settles_once(self):
        ledger = MoneyFundIncome()
        self.assertEqual(ledger.record(100, 0.56789), 0.56)
        ledger.schedule_payment()
        self.assertEqual(ledger.receivable_cny, 0.56)
        self.assertEqual(ledger.settle(), 0.56)
        self.assertEqual(ledger.settle(), 0)
        self.assertEqual(ledger.receivable_cny, 0)

    def test_csi1000_share_consolidation_uses_official_ratio_and_rounds_up(self):
        start, end = "2022-09-01", "2022-09-05"
        db_path, base = build_synced_db(start, end)
        config = one_asset(base, "512100.SH", initial_capital_cny=100)
        with db_session(db_path) as conn:
            conn.execute(
                """UPDATE prices SET open=CASE trade_date WHEN ? THEN 0.982 ELSE 2.713 END,
                   close=CASE trade_date WHEN ? THEN 0.982 ELSE 2.713 END WHERE symbol='512100.SH'""",
                (start, start),
            )
            result = run_backtest(conn, config)
        # 100 old shares at 0.982 and 1.80 uninvested. The fund rounds
        # 36.555 shares up to 37 despite the vendor's factor being unchanged.
        self.assertAlmostEqual(result["summary"]["final_asset_cny"], 37 * 2.713 + 1.8)
