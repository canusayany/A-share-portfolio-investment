from __future__ import annotations

import json
import unittest
from datetime import date

from app.config import normalize_config
from app.db import db_session, init_db, rows_to_dicts
from app.services.calendar import business_days, repo_actual_days
from app.services.backtest_engine import (
    BacktestError,
    PortfolioState,
    Position,
    _apply_dividend_events,
    _asset_period_performance,
    _buy_position,
    _cover_cash_shortfall,
    _sell_position,
    _invest_idle_cash_in_repo,
    _invest_repo_cash,
    _mature_repo_lots,
    _portfolio_value,
    _repo_spend_reserve,
    benchmark_returns,
    adjusted_price_and_share_scale_series,
    adjusted_price_series,
    annual_return_drawdown_ratio,
    annual_expense_factor,
    attach_proxy_price_maps,
    comparison_assets,
    compute_metrics,
    dip_buy_assets,
    dip_buy_cash_buffer_cny,
    is_dip_buy_blackout_month,
    drawdown_recovery_metrics,
    effective_weights,
    minimal_rebalance_weights,
    repo_tenor_days,
    repo_fixed_target_weight,
    reference_trading_days,
    ranking_metrics,
    market_capture_metrics,
    rolling_window_ranges,
    run_backtest,
    has_investable_asset_target,
    load_dividend_events,
    should_rebalance,
    yearly_positive_return_count,
    yearly_return_counts,
    worst_calendar_periods,
)
from tests.helpers import build_synced_db, seed_fixture_data, temp_db_path


class BacktestEngineTests(unittest.TestCase):
    def test_dividend_low_vol_proxy_deducts_expenses_without_double_charging_real_etf(self) -> None:
        asset = next(
            item for item in normalize_config({})["assets"] if item["symbol"] == "512890.SH"
        )
        price_maps = {
            "H20269.CSI": {},
            "512890.SH": {
                "2005-12-30": 100.0,
                "2018-12-28": 190.0,
                "2019-01-17": 200.0,
                "2019-01-18": 201.0,
                "2020-01-02": 210.0,
            },
        }

        attach_proxy_price_maps(price_maps, [asset])

        proxy = price_maps["H20269.CSI"]
        expense_factor = annual_expense_factor(date(2005, 12, 30), date(2019, 1, 17), 0.0063)
        self.assertAlmostEqual(proxy["2005-12-30"], 100.0 / expense_factor)
        self.assertAlmostEqual(proxy["2019-01-17"], 200.0)
        self.assertLess(proxy["2019-01-17"] / proxy["2005-12-30"] - 1.0, 1.0)
        self.assertEqual(proxy["2019-01-18"], 201.0)
        self.assertEqual(proxy["2020-01-02"], 210.0)

    def test_verified_etf_share_consolidation_is_continuous_but_cash_dividend_is_not_adjusted(self) -> None:
        rows = [
            {"trade_date": "2022-09-01", "price": 0.982, "adj_factor": 1.0},
            {"trade_date": "2022-09-02", "price": 2.686, "adj_factor": 1.0},
            {"trade_date": "2022-09-05", "price": 2.713, "adj_factor": 0.3622},
            {"trade_date": "2022-09-06", "price": 2.720, "adj_factor": 0.3622},
            {"trade_date": "2023-06-01", "price": 2.600, "adj_factor": 0.3622},
            {"trade_date": "2023-06-02", "price": 2.540, "adj_factor": 0.3580},
        ]

        prices = adjusted_price_series(rows)

        self.assertAlmostEqual(prices["2022-09-05"], 0.98265, places=4)
        self.assertAlmostEqual(prices["2022-09-02"], 2.686 * 0.3622, places=6)
        self.assertAlmostEqual(prices["2022-09-06"], 0.98518, places=4)
        self.assertAlmostEqual(prices["2023-06-02"], 2.540 * 0.3622, places=6)

    def test_configured_512890_share_split_is_continuous_without_adjustment_factors(self) -> None:
        rows = [
            {"trade_date": "2021-10-21", "price": 1.639, "adj_factor": None},
            {"trade_date": "2021-10-25", "price": 0.801, "adj_factor": None},
            {"trade_date": "2021-10-26", "price": 0.810, "adj_factor": None},
            {"trade_date": "2021-10-27", "price": 0.405, "adj_factor": None},
        ]

        prices = adjusted_price_series(rows, {"2021-10-25": 2.0})

        self.assertAlmostEqual(prices["2021-10-25"], 1.602)
        self.assertAlmostEqual(prices["2021-10-26"], 1.620)
        self.assertAlmostEqual(prices["2021-10-27"], 0.810)

    def test_cash_dividend_uses_the_same_share_scale_as_normalized_prices(self) -> None:
        rows = [
            {"trade_date": "2021-10-21", "price": 1.639, "adj_factor": None},
            {"trade_date": "2021-10-25", "price": 0.801, "adj_factor": None},
            {"trade_date": "2022-01-20", "price": 0.900, "adj_factor": None},
        ]
        _prices, share_scales = adjusted_price_and_share_scale_series(rows, {"2021-10-25": 2.0})
        state = PortfolioState(cash_cny=0.0)
        state.positions["512890.SH"] = Position("512890.SH", "CN", "CNY", "cn_etf", quantity=100)
        event = {
            "symbol": "512890.SH",
            "pay_date": "2022-01-20",
            "div_cash": 0.03,
            "currency": "CNY",
            "normalized_share_scale": share_scales["2022-01-20"],
        }

        _apply_dividend_events(state, "2022-01-20", {"2022-01-20": [event]}, {}, {}, normalize_config({})["fees"])

        self.assertEqual(share_scales["2022-01-20"], 2.0)
        self.assertAlmostEqual(state.total_dividend_cny, 6.0)
        self.assertAlmostEqual(state.cash_cny, 6.0)

    def test_dividend_loader_carries_forward_share_scale_to_ex_date(self) -> None:
        db_path = temp_db_path()
        init_db(db_path)
        with db_session(db_path) as conn:
            conn.execute(
                """
                INSERT INTO fund_dividends(symbol,ann_date,record_date,ex_date,pay_date,div_cash,currency,source)
                VALUES('512890.SH','2022-01-10','2022-01-19','2022-01-20','2022-01-25',0.03,'CNY','test:dividend')
                """
            )
            ex_events, _pay_events = load_dividend_events(
                conn,
                ["512890.SH"],
                "2022-01-01",
                "2022-01-31",
                {"512890.SH": {"2021-10-21": 1.0, "2021-10-25": 2.0}},
            )

        self.assertEqual(ex_events["2022-01-20"][0]["normalized_share_scale"], 2.0)

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
        first_expected_return = (
            daily[0]["total_asset_cny"]
            - cfg["initial_capital_cny"]
            - daily[0]["flow_cny"]
        ) / cfg["initial_capital_cny"]
        self.assertAlmostEqual(daily[0]["daily_return"], first_expected_return)
        self.assertAlmostEqual(daily[-1]["cumulative_return"], result["summary"]["total_return"])
        rebalance_payload = json.loads(rebalances[0]["payload_json"])
        self.assertIn("asset_performance", rebalance_payload)
        self.assertIn("REPO", rebalance_payload["asset_performance"])
        self.assertIn("profit_cny", rebalance_payload["asset_performance"]["REPO"])
        self.assertIn("return", rebalance_payload["asset_performance"]["REPO"])
        self.assertIn("period_max_drawdown", rebalance_payload)
        self.assertLessEqual(rebalance_payload["period_max_drawdown"], 0)
        self.assertEqual(cached["run_id"], run_id)
        self.assertTrue(cached["cache"]["hit"])

    def test_initial_rebalance_uses_exact_targets_instead_of_band_edges(self) -> None:
        db_path, cfg = build_synced_db("2020-01-02", "2020-01-31")
        selected_symbols = {"VOO", "CBA03101", "CBA06501", "CBA21801"}
        cfg["monthly_spend_cny"] = 0.0
        cfg["rebalance_band"] = 0.25
        for asset in cfg["assets"]:
            selected = asset["symbol"] in selected_symbols
            asset["enabled"] = selected
            asset["target_weight"] = 0.25 if selected else 0.0

        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
            first_rebalance = conn.execute(
                "SELECT payload_json FROM rebalance_events WHERE run_id=? ORDER BY rebalance_date LIMIT 1",
                (result["run_id"],),
            ).fetchone()
            first_daily = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? ORDER BY trade_date LIMIT 1",
                (result["run_id"],),
            ).fetchone()

        rebalance_payload = json.loads(first_rebalance["payload_json"])
        daily_payload = json.loads(first_daily["payload_json"])
        for symbol in selected_symbols:
            self.assertAlmostEqual(rebalance_payload["targets"][symbol], 0.25)
            self.assertAlmostEqual(rebalance_payload["desired_weights"][symbol], 0.25)
            self.assertAlmostEqual(daily_payload["weights"][symbol], 0.25, delta=0.002)
        self.assertLess(daily_payload["weights"].get("REPO", 0.0), 0.002)

    def test_disabled_asset_weight_flows_to_repo(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-02-28")
        next(asset for asset in cfg["assets"] if asset["symbol"] == "512890.SH")["enabled"] = False
        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
            daily = rows_to_dicts(conn.execute("SELECT payload_json FROM portfolio_daily WHERE run_id=? ORDER BY trade_date", (result["run_id"],)))
        first_payload = json.loads(daily[0]["payload_json"])
        self.assertEqual(first_payload["targets"].get("VOO", 0), 0)
        self.assertAlmostEqual(first_payload["targets"]["REPO"], 0.5)

    def test_fixed_bucket_mode_allocates_repo_first_then_scales_risk_assets(self) -> None:
        cfg = normalize_config(
            {
                "repo_target_mode": "fixed_bucket",
                "repo_fixed_target_cny": 360000,
                "repo_fixed_target_ratio": 0.04,
            }
        )
        latest_prices = {asset["symbol"]: 1.0 for asset in cfg["assets"]}
        weights = effective_weights(cfg, date(2020, 1, 2), latest_prices, 1_000_000)

        self.assertAlmostEqual(repo_fixed_target_weight(cfg, 1_000_000), 0.40)
        self.assertAlmostEqual(weights["REPO"], 0.40)
        self.assertNotIn("VOO", weights)
        self.assertAlmostEqual(weights["512890.SH"], 0.20)
        self.assertAlmostEqual(weights["CBA21801"], 0.20)
        self.assertAlmostEqual(weights["518880.SH"], 0.20)
        self.assertAlmostEqual(sum(weights.values()), 1.0)

    def test_dip_buy_uses_cost_basis_cash_pool_parts_and_next_day_open(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-10")
        for asset in cfg["assets"]:
            asset["enabled"] = asset["symbol"] == "510500.SH"
            asset["target_weight"] = 0.50 if asset["enabled"] else 0.0
        cfg["dip_buy_enabled"] = True
        cfg["dip_buy_total_parts"] = 10
        cfg["dip_buy_parts_per_trigger"] = 2
        cfg["monthly_spend_cny"] = 10_000.0
        with db_session(db_path) as conn:
            conn.execute("UPDATE prices SET open=10.0, high=10.0, low=10.0, close=10.0 WHERE symbol='510500.SH'")
            for trade_date, open_price, close_price in (
                ("2020-01-01", 10.0, 10.0),
                ("2020-01-02", 10.0, 11.0),
                # This is more than 5% below the local peak, but remains above
                # cost and must not trigger the new rule.
                ("2020-01-03", 10.0, 10.3),
                ("2020-01-06", 10.0, 9.4),
                ("2020-01-07", 8.75, 9.4),
            ):
                conn.execute(
                    "UPDATE prices SET open=?, high=?, low=?, close=? WHERE symbol='510500.SH' AND trade_date=?",
                    (open_price, max(open_price, close_price), min(open_price, close_price), close_price, trade_date),
                )
            result = run_backtest(conn, cfg)
            dip_trades = rows_to_dicts(
                conn.execute(
                    "SELECT trade_date, reason, price, gross_amount, fee FROM trades WHERE run_id=? AND reason='dip_buy' ORDER BY trade_date",
                    (result["run_id"],),
                )
            )
            first_day = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? AND trade_date='2020-01-01'",
                (result["run_id"],),
            ).fetchone()

        self.assertEqual(result["summary"]["dip_buy_count"], 1)
        self.assertEqual(len(dip_trades), 1)
        self.assertEqual(dip_trades[0]["trade_date"], "2020-01-07")
        self.assertEqual(dip_trades[0]["price"], 8.75)
        first_payload = json.loads(first_day["payload_json"])
        expected_budget = first_payload["dip_buy"]["piece_cny"] * 2
        actual_spend = dip_trades[0]["gross_amount"] + dip_trades[0]["fee"]
        self.assertLessEqual(actual_spend, expected_budget)
        self.assertLess(expected_budget - actual_spend, 8.75 * 100 + 1)
        self.assertEqual(first_payload["dip_buy"]["cash_buffer_cny"], 230_000.0)
        self.assertGreater(first_payload["repo_lots"], 0)

    def test_dip_buy_waits_when_the_next_session_open_is_missing(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-08")
        for asset in cfg["assets"]:
            asset["enabled"] = asset["symbol"] == "510500.SH"
            asset["target_weight"] = 0.50 if asset["enabled"] else 0.0
        cfg["dip_buy_enabled"] = True
        cfg["monthly_spend_cny"] = 0.0
        with db_session(db_path) as conn:
            conn.execute("UPDATE prices SET open=10,high=10,low=10,close=10 WHERE symbol='510500.SH'")
            conn.execute(
                "UPDATE prices SET low=9,close=9 WHERE symbol='510500.SH' AND trade_date='2020-01-02'"
            )
            conn.execute(
                "UPDATE prices SET open=NULL,low=9,close=9 WHERE symbol='510500.SH' AND trade_date='2020-01-03'"
            )
            conn.execute(
                "UPDATE prices SET open=8.5,low=8.5,close=9 WHERE symbol='510500.SH' AND trade_date='2020-01-06'"
            )
            result = run_backtest(conn, cfg)
            dip_trades = rows_to_dicts(
                conn.execute(
                    "SELECT trade_date,price FROM trades WHERE run_id=? AND reason='dip_buy' ORDER BY trade_date",
                    (result["run_id"],),
                )
            )

        self.assertTrue(dip_trades)
        self.assertEqual(dip_trades[0]["trade_date"], "2020-01-06")
        self.assertEqual(dip_trades[0]["price"], 8.5)

    def test_dip_buy_requires_cash_equivalents_above_living_expense_buffer(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-06")
        for asset in cfg["assets"]:
            asset["enabled"] = asset["symbol"] == "510500.SH"
            asset["target_weight"] = 0.80 if asset["enabled"] else 0.0
        cfg["dip_buy_enabled"] = True
        cfg["monthly_spend_cny"] = 10_000.0
        with db_session(db_path) as conn:
            conn.execute("UPDATE prices SET open=10.0, high=10.0, low=9.0, close=9.0 WHERE symbol='510500.SH'")
            conn.execute("UPDATE prices SET open=10.0, high=10.0, low=10.0, close=10.0 WHERE symbol='510500.SH' AND trade_date='2020-01-01'")
            result = run_backtest(conn, cfg)
            dip_count = conn.execute(
                "SELECT COUNT(*) AS count FROM trades WHERE run_id=? AND reason='dip_buy'",
                (result["run_id"],),
            ).fetchone()["count"]

        self.assertEqual(dip_count, 0)
        self.assertEqual(result["summary"]["dip_buy_count"], 0)

    def test_dip_buy_is_inactive_when_rebalance_frequency_is_not_yearly(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-10")
        for asset in cfg["assets"]:
            asset["enabled"] = asset["symbol"] == "510500.SH"
            asset["target_weight"] = 0.50 if asset["enabled"] else 0.0
        cfg["dip_buy_enabled"] = True
        cfg["rebalance_frequency"] = "monthly"
        cfg["monthly_spend_cny"] = 10_000.0
        with db_session(db_path) as conn:
            conn.execute("UPDATE prices SET open=9.0, high=9.0, low=9.0, close=9.0 WHERE symbol='510500.SH'")
            conn.execute("UPDATE prices SET open=10.0, high=10.0, low=10.0, close=10.0 WHERE symbol='510500.SH' AND trade_date='2020-01-01'")
            result = run_backtest(conn, cfg)
            final_daily = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? ORDER BY trade_date DESC LIMIT 1",
                (result["run_id"],),
            ).fetchone()

        self.assertEqual(result["summary"]["dip_buy_count"], 0)
        self.assertFalse(json.loads(final_daily["payload_json"])["dip_buy"]["active"])

    def test_dip_buy_sells_selected_money_fund_at_next_open_for_funding(self) -> None:
        cfg = normalize_config(
            {
                "start_date": "2020-01-01",
                "end_date": "2020-01-06",
                "repo_symbol": "511990.SH",
                "dip_buy_enabled": True,
                "monthly_spend_cny": 10_000.0,
            }
        )
        for asset in cfg["assets"]:
            asset["enabled"] = asset["symbol"] == "510500.SH"
            asset["target_weight"] = 0.50 if asset["enabled"] else 0.0
        db_path = temp_db_path()
        init_db(db_path)
        with db_session(db_path) as conn:
            seed_fixture_data(conn, cfg, cfg["start_date"], cfg["end_date"])
            conn.execute("UPDATE prices SET open=10.0, high=10.0, low=9.0, close=9.0 WHERE symbol='510500.SH'")
            conn.execute("UPDATE prices SET open=10.0, high=10.0, low=10.0, close=10.0 WHERE symbol='510500.SH' AND trade_date='2020-01-01'")
            conn.execute("UPDATE prices SET open=8.5 WHERE symbol='510500.SH' AND trade_date='2020-01-03'")
            result = run_backtest(conn, cfg)
            funding_trades = rows_to_dicts(
                conn.execute(
                    "SELECT trade_date,symbol,side,reason,price FROM trades WHERE run_id=? AND reason='dip_buy_funding'",
                    (result["run_id"],),
                )
            )
            dip_trade = conn.execute(
                "SELECT trade_date,price FROM trades WHERE run_id=? AND reason='dip_buy'",
                (result["run_id"],),
            ).fetchone()

        self.assertEqual(len(funding_trades), 1)
        self.assertEqual(funding_trades[0]["symbol"], "511990.SH")
        self.assertEqual(funding_trades[0]["side"], "SELL")
        self.assertEqual(funding_trades[0]["trade_date"], "2020-01-03")
        self.assertEqual(dip_trade["trade_date"], "2020-01-03")
        self.assertEqual(dip_trade["price"], 8.5)

    def test_multi_day_repo_rechecks_at_maturity_then_trades_following_open(self) -> None:
        cfg = normalize_config(
            {
                "start_date": "2020-01-01",
                "end_date": "2020-01-15",
                "repo_symbol": "204007",
                "dip_buy_enabled": True,
                "monthly_spend_cny": 10_000.0,
            }
        )
        for asset in cfg["assets"]:
            asset["enabled"] = asset["symbol"] == "510500.SH"
            asset["target_weight"] = 0.50 if asset["enabled"] else 0.0
        db_path = temp_db_path()
        init_db(db_path)
        with db_session(db_path) as conn:
            seed_fixture_data(conn, cfg, cfg["start_date"], cfg["end_date"])
            conn.execute("UPDATE prices SET open=9.0, high=9.0, low=9.0, close=9.0 WHERE symbol='510500.SH'")
            conn.execute("UPDATE prices SET open=10.0, high=10.0, low=10.0, close=10.0 WHERE symbol='510500.SH' AND trade_date='2020-01-01'")
            conn.execute("UPDATE prices SET open=8.25 WHERE symbol='510500.SH' AND trade_date='2020-01-13'")
            result = run_backtest(conn, cfg)
            dip_trades = rows_to_dicts(
                conn.execute(
                    "SELECT trade_date,price FROM trades WHERE run_id=? AND reason='dip_buy' ORDER BY trade_date",
                    (result["run_id"],),
                )
            )
            jan_09 = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? AND trade_date='2020-01-09'",
                (result["run_id"],),
            ).fetchone()
            maturity_day = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? AND trade_date='2020-01-10'",
                (result["run_id"],),
            ).fetchone()

        jan_09_dip = json.loads(jan_09["payload_json"])["dip_buy"]
        maturity_dip = json.loads(maturity_day["payload_json"])["dip_buy"]
        self.assertEqual(jan_09_dip["deferred_recheck_dates"]["510500.SH"], "2020-01-10")
        self.assertEqual(maturity_dip["deferred_count"], 0)
        self.assertEqual(maturity_dip["pending_count"], 1)
        self.assertEqual(dip_trades, [{"trade_date": "2020-01-13", "price": 8.25}])

    def test_dip_buy_asset_scope_and_declining_monthly_cash_buffer(self) -> None:
        cfg = normalize_config({})
        for asset in cfg["assets"]:
            asset["enabled"] = asset["symbol"] in {"VOO", "512890.SH", "510300.SH", "518880.SH", "CBA03101"}
        prices = {asset["symbol"]: 100.0 for asset in cfg["assets"]}

        symbols = {asset["symbol"] for asset in dip_buy_assets(cfg, date(2020, 1, 15), prices)}

        self.assertEqual(symbols, {"512890.SH", "510300.SH", "518880.SH", "CBA03101"})
        self.assertEqual(dip_buy_cash_buffer_cny(10_000, date(2020, 1, 15), date(2020, 1, 31)), 230_000)
        self.assertEqual(dip_buy_cash_buffer_cny(10_000, date(2020, 1, 15), date(2020, 2, 1)), 220_000)
        self.assertEqual(dip_buy_cash_buffer_cny(10_000, date(2020, 1, 15), date(2020, 12, 1)), 120_000)
        self.assertEqual(dip_buy_cash_buffer_cny(10_000, date(2020, 1, 15), date(2022, 1, 1)), 120_000)
        # A new annual rebalance date refreshes the post-spend ledger to 23 months.
        self.assertEqual(dip_buy_cash_buffer_cny(10_000, date(2021, 1, 15), date(2021, 1, 15)), 230_000)

    def test_dip_buy_blackout_months_wrap_year_and_exclude_rebalance_month(self) -> None:
        self.assertTrue(is_dip_buy_blackout_month(date(2020, 12, 1), 1, 1))
        self.assertTrue(is_dip_buy_blackout_month(date(2020, 11, 1), 1, 2))
        self.assertFalse(is_dip_buy_blackout_month(date(2021, 1, 1), 1, 2))
        self.assertFalse(is_dip_buy_blackout_month(date(2020, 10, 1), 1, 2))
        self.assertTrue(is_dip_buy_blackout_month(date(2020, 5, 1), 6, 1))
        self.assertFalse(is_dip_buy_blackout_month(date(2020, 5, 1), 6, 0))

    def test_dip_buy_optional_blackout_cancels_orders_crossing_into_quiet_month(self) -> None:
        db_path, cfg = build_synced_db("2020-04-27", "2020-05-08")
        for asset in cfg["assets"]:
            asset["enabled"] = asset["symbol"] == "510500.SH"
            asset["target_weight"] = 0.50 if asset["enabled"] else 0.0
        cfg.update(
            {
                "dip_buy_enabled": True,
                "dip_buy_blackout_enabled": True,
                "dip_buy_blackout_months": 1,
                "annual_rebalance_month": 6,
                "monthly_spend_cny": 0.0,
            }
        )
        with db_session(db_path) as conn:
            conn.execute("UPDATE prices SET open=10,high=10,low=10,close=10 WHERE symbol='510500.SH'")
            conn.execute(
                "UPDATE prices SET close=9,low=9 WHERE symbol='510500.SH' AND trade_date='2020-04-30'"
            )
            result = run_backtest(conn, cfg)
            dip_count = conn.execute(
                "SELECT COUNT(*) AS count FROM trades WHERE run_id=? AND reason='dip_buy'",
                (result["run_id"],),
            ).fetchone()["count"]
            may_payload = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? AND trade_date LIKE '2020-05-%' ORDER BY trade_date LIMIT 1",
                (result["run_id"],),
            ).fetchone()

        self.assertEqual(dip_count, 0)
        self.assertTrue(json.loads(may_payload["payload_json"])["dip_buy"]["blackout"])

        cfg["dip_buy_blackout_enabled"] = False
        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
            dip_trade = conn.execute(
                "SELECT trade_date FROM trades WHERE run_id=? AND reason='dip_buy'",
                (result["run_id"],),
            ).fetchone()
        self.assertIsNotNone(dip_trade)
        self.assertTrue(dip_trade["trade_date"].startswith("2020-05-"))

    def test_dip_buy_cooldown_blocks_exact_number_of_following_trading_days(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-31")
        for asset in cfg["assets"]:
            asset["enabled"] = asset["symbol"] == "510500.SH"
            asset["target_weight"] = 0.50 if asset["enabled"] else 0.0
        cfg.update(
            {
                "dip_buy_enabled": True,
                "dip_buy_cooldown_trading_days": 10,
                "dip_buy_total_parts": 10,
                "dip_buy_parts_per_trigger": 1,
                "monthly_spend_cny": 0.0,
            }
        )
        with db_session(db_path) as conn:
            conn.execute("UPDATE prices SET open=9,high=9,low=9,close=9 WHERE symbol='510500.SH'")
            conn.execute("UPDATE prices SET open=10,high=10,low=10,close=10 WHERE symbol='510500.SH' AND trade_date='2020-01-01'")
            result = run_backtest(conn, cfg)
            dip_trades = rows_to_dicts(
                conn.execute(
                    "SELECT trade_date FROM trades WHERE run_id=? AND reason='dip_buy' ORDER BY trade_date",
                    (result["run_id"],),
                )
            )

        self.assertGreaterEqual(len(dip_trades), 2)
        trading_dates = [day.isoformat() for day in business_days("2020-01-01", "2020-01-31")]
        first_idx = trading_dates.index(dip_trades[0]["trade_date"])
        second_idx = trading_dates.index(dip_trades[1]["trade_date"])
        self.assertGreater(second_idx - first_idx, 10)

    def test_fixed_bucket_mode_runs_backtest_with_fixed_repo_rebalance_target(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-02-28")
        cfg["repo_target_mode"] = "fixed_bucket"
        cfg["repo_fixed_target_cny"] = 360000
        cfg["repo_fixed_target_ratio"] = 0.02
        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
            first_rebalance = conn.execute(
                "SELECT * FROM rebalance_events WHERE run_id=? ORDER BY rebalance_date LIMIT 1",
                (result["run_id"],),
            ).fetchone()

        self.assertIsNotNone(first_rebalance)
        payload = json.loads(first_rebalance["payload_json"])
        expected_repo_target = 360000 + first_rebalance["total_asset_before"] * 0.02
        self.assertEqual(payload["repo_target_mode"], "fixed_bucket")
        self.assertAlmostEqual(payload["repo_target_value_cny"], expected_repo_target, delta=1)
        self.assertAlmostEqual(payload["desired_weights"]["REPO"], payload["targets"]["REPO"])

    def test_us_and_cn_purchase_rules_are_separate(self) -> None:
        db_path, cfg = build_synced_db("2013-01-01", "2013-03-31")
        voo = next(asset for asset in cfg["assets"] if asset["symbol"] == "VOO")
        voo["enabled"] = True
        voo["target_weight"] = 0.20
        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
            trades = rows_to_dicts(conn.execute("SELECT * FROM trades WHERE run_id=? AND side='BUY'", (result["run_id"],)))

        voo_buys = [trade for trade in trades if trade["symbol"] == "VOO"]
        cn_buys = [
            trade
            for trade in trades
            if trade["currency"] == "CNY" and trade["symbol"] not in {"CBA21801", "CN30Y.YIELD-TR"}
        ]
        self.assertTrue(voo_buys)
        self.assertTrue(cn_buys)
        self.assertTrue(any(abs(trade["quantity"] - round(trade["quantity"])) > 1e-6 for trade in voo_buys))
        self.assertTrue(all(trade["quantity"] % 100 == 0 for trade in cn_buys))
        self.assertTrue(all(trade["currency"] == "USD" for trade in voo_buys))

    def test_non_fractional_us_buy_charges_fx_spread_only_on_executed_amount(self) -> None:
        cfg = normalize_config({})
        state = PortfolioState(cash_cny=7000.0)
        position = Position(symbol="VOO", market="US", currency="USD", asset_type="us_etf")
        trades: list[dict] = []

        spent = _buy_position(
            state,
            position,
            date(2026, 1, 2),
            7000.0,
            500.0,
            {"USD/CNY": 7.0},
            cfg["fees"],
            trades,
            False,
            "test",
        )

        self.assertEqual(position.quantity, 1.0)
        self.assertLess(spent, 3600.0)
        self.assertAlmostEqual(state.cash_cny, 7000.0 - spent)
        self.assertAlmostEqual(state.total_fees_cny, trades[0]["fee"])

    def test_hk_connect_etf_uses_hkd_fx_and_board_lot_rules(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-03-31")
        hk_asset = next(asset for asset in cfg["assets"] if asset["symbol"] == "03195.HK")
        hk_asset["enabled"] = True
        hk_asset["target_weight"] = 0.10
        cfg["assets"][0]["enabled"] = False
        cfg["assets"][0]["target_weight"] = 0.0
        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
            trades = rows_to_dicts(conn.execute("SELECT * FROM trades WHERE run_id=? AND symbol='03195.HK'", (result["run_id"],)))
            final_payload = json.loads(
                conn.execute(
                    "SELECT payload_json FROM portfolio_daily WHERE run_id=? ORDER BY trade_date DESC LIMIT 1",
                    (result["run_id"],),
                ).fetchone()["payload_json"]
            )

        hk_buys = [trade for trade in trades if trade["side"] == "BUY"]
        self.assertTrue(hk_buys)
        self.assertTrue(all(trade["currency"] == "HKD" for trade in hk_buys))
        self.assertTrue(all(trade["quantity"] % 100 == 0 for trade in hk_buys))
        self.assertIn("03195.HK", final_payload["values"])
        self.assertGreater(result["summary"]["total_fees_cny"], 0)

    def test_cn_sp500_waits_for_next_rebalance_after_inception(self) -> None:
        db_path, cfg = build_synced_db("2013-01-01", "2014-02-28")
        cfg["monthly_spend_cny"] = 0.0
        for asset in cfg["assets"]:
            is_cn_sp500 = asset["symbol"] == "513500.SH"
            asset["enabled"] = is_cn_sp500
            asset["target_weight"] = 0.20 if is_cn_sp500 else 0.0

        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
            trades = rows_to_dicts(
                conn.execute(
                    "SELECT * FROM trades WHERE run_id=? AND symbol='513500.SH' ORDER BY trade_date",
                    (result["run_id"],),
                )
            )
            first = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? ORDER BY trade_date LIMIT 1",
                (result["run_id"],),
            ).fetchone()

        first_payload = json.loads(first["payload_json"])
        buys = [trade for trade in trades if trade["side"] == "BUY"]
        self.assertEqual(first_payload["targets"], {"REPO": 1.0})
        self.assertTrue(buys)
        self.assertTrue(all(trade["trade_date"] >= "2014-01-01" for trade in buys))
        self.assertTrue(all(trade["currency"] == "CNY" for trade in buys))
        self.assertTrue(all(trade["quantity"] % 100 == 0 for trade in buys))
        self.assertGreater(result["summary"]["total_fees_cny"], 0)

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

    def test_hs300_price_proxy_switches_to_510300_from_2013(self) -> None:
        db_path, cfg = build_synced_db("2012-01-01", "2013-01-31")
        cfg["rebalance_frequency"] = "yearly"
        cfg["monthly_spend_cny"] = 0.0
        for asset in cfg["assets"]:
            is_hs300 = asset["symbol"] == "510300.SH"
            asset["enabled"] = is_hs300
            asset["target_weight"] = 0.50 if is_hs300 else 0.0

        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
            trades = rows_to_dicts(
                conn.execute(
                    "SELECT trade_date, symbol, side FROM trades WHERE run_id=? ORDER BY trade_date, side",
                    (result["run_id"],),
                )
            )
            rebalances = rows_to_dicts(
                conn.execute(
                    "SELECT rebalance_date, payload_json FROM rebalance_events WHERE run_id=? ORDER BY rebalance_date",
                    (result["run_id"],),
                )
            )

        self.assertTrue(any(trade["symbol"] == "160706" and trade["side"] == "BUY" for trade in trades))
        self.assertTrue(any(trade["symbol"] == "160706" and trade["side"] == "SELL" and trade["trade_date"] >= "2013-01-01" for trade in trades))
        self.assertTrue(any(trade["symbol"] == "510300.SH" and trade["side"] == "BUY" and trade["trade_date"] >= "2013-01-01" for trade in trades))
        first_targets = json.loads(rebalances[0]["payload_json"])["targets"]
        switch_targets = json.loads(rebalances[1]["payload_json"])["targets"]
        self.assertIn("160706", first_targets)
        self.assertNotIn("510300.SH", first_targets)
        self.assertIn("510300.SH", switch_targets)
        self.assertNotIn("160706", switch_targets)

    def test_yearly_rebalance_records_within_band_check_without_trade(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2022-01-31")
        cfg["monthly_spend_cny"] = 0.0
        cfg["rebalance_band"] = 0.02
        for asset in cfg["assets"]:
            is_hs300 = asset["symbol"] == "510300.SH"
            asset["enabled"] = is_hs300
            asset["target_weight"] = 0.97 if is_hs300 else 0.0

        with db_session(db_path) as conn:
            conn.execute(
                """
                UPDATE prices
                SET open=1.0, high=1.0, low=1.0, close=1.0, adj_close=1.0
                WHERE symbol='510300.SH'
                """
            )
            conn.execute("DELETE FROM fund_dividends")
            conn.execute("UPDATE repo_rates SET open_rate=0.0, close_rate=0.0, high_rate=0.0, low_rate=0.0")
            result = run_backtest(conn, cfg)
            rebalances = rows_to_dicts(
                conn.execute(
                    "SELECT * FROM rebalance_events WHERE run_id=? ORDER BY rebalance_date",
                    (result["run_id"],),
                )
            )

        rows_by_year = {row["rebalance_date"][:4]: row for row in rebalances}
        self.assertIn("2020", rows_by_year)
        self.assertIn("2021", rows_by_year)
        self.assertIn("2022", rows_by_year)
        no_trade_payload = json.loads(rows_by_year["2021"]["payload_json"])
        self.assertEqual(no_trade_payload["rebalance_action"], "record_only")
        self.assertEqual(no_trade_payload["rebalance_reason"], "within_band")
        self.assertFalse(no_trade_payload["rebalanced"])
        self.assertAlmostEqual(rows_by_year["2021"]["turnover_cny"], 0.0)
        self.assertAlmostEqual(rows_by_year["2021"]["fee_cny"], 0.0)
        self.assertEqual(result["summary"]["rebalance_count"], len(rebalances))

    def test_start_before_etf_inception_uses_dividend_low_vol_total_return_proxy(self) -> None:
        db_path, cfg = build_synced_db("2013-01-01", "2013-02-28")
        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
            first = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? ORDER BY trade_date LIMIT 1",
                (result["run_id"],),
            ).fetchone()
        payload = json.loads(first["payload_json"])
        self.assertNotIn("512890.SH", payload["targets"])
        self.assertAlmostEqual(payload["targets"]["H20269.CSI"], 0.25)
        self.assertAlmostEqual(payload["targets"]["REPO"], 0.25)

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

    def test_repo_interest_accrues_until_maturity_and_fee_is_counted_once(self) -> None:
        cfg = normalize_config({})
        trade_day = date(2026, 7, 10)
        state = PortfolioState(cash_cny=10000.0)

        _invest_idle_cash_in_repo(state, trade_day, 3.65, cfg["fees"], 7)

        self.assertEqual(len(state.repo_lots), 1)
        lot = state.repo_lots[0]
        same_day_value = _portfolio_value(state, {}, {}, trade_day)[0]
        mid_day = date(2026, 7, 15)
        mid_value = _portfolio_value(state, {}, {}, mid_day)[0]
        maturity_value = _portfolio_value(state, {}, {}, lot.maturity_date)[0]
        self.assertAlmostEqual(same_day_value, 10000.0 - lot.fee)
        self.assertGreater(mid_value, same_day_value)
        self.assertLess(mid_value, maturity_value)
        self.assertAlmostEqual(maturity_value, 10000.0 + lot.interest - lot.fee)
        self.assertAlmostEqual(state.total_fees_cny, lot.fee)

        _mature_repo_lots(state, lot.maturity_date)
        self.assertAlmostEqual(state.cash_cny, maturity_value)
        self.assertAlmostEqual(state.total_fees_cny, lot.fee)

    def test_repo_benchmark_return_includes_first_reference_day(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2020-01-10")
        cfg["monthly_spend_cny"] = 0.0
        with db_session(db_path) as conn:
            conn.execute("UPDATE repo_rates SET open_rate=3.65,close_rate=3.65,high_rate=3.65,low_rate=3.65")
            result = run_backtest(conn, cfg)
            dates = [
                date.fromisoformat(row["trade_date"])
                for row in conn.execute(
                    "SELECT trade_date FROM portfolio_daily WHERE run_id=? ORDER BY trade_date",
                    (result["run_id"],),
                )
            ]

        nav = 1.0
        for trade_day in dates:
            nav *= 1.0 + 0.0365 * repo_actual_days(trade_day, 1) / 365.0
        years = max((dates[-1] - dates[0]).days / 365.25, 1 / 365.25)
        expected_annualized = nav ** (1.0 / years) - 1.0
        self.assertAlmostEqual(result["summary"]["repo_annualized_return"], expected_annualized)

    def test_long_tenor_repo_stays_liquid_for_upcoming_rebalance(self) -> None:
        cfg = normalize_config({})
        trade_day = date(2020, 1, 3)
        rebalance_day = date(2020, 1, 6)
        state = PortfolioState(cash_cny=10000.0)

        _invest_repo_cash(
            state,
            trade_day,
            3.0,
            2.0,
            cfg["fees"],
            28,
            set(),
            0.0,
            {rebalance_day},
        )

        self.assertTrue(state.repo_lots)
        self.assertTrue(all(lot.maturity_date <= rebalance_day for lot in state.repo_lots))

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

    def test_worst_calendar_periods_prefer_complete_years_and_halves(self) -> None:
        dates = [
            "2020-01-02", "2020-06-30", "2020-07-01", "2020-12-31",
            "2021-01-04", "2021-06-30", "2021-07-01", "2021-12-31",
        ]
        metrics = worst_calendar_periods(dates, [0.0, 0.10, -0.20, 0.0, 0.0, 0.05, -0.10, 0.0])

        self.assertEqual(metrics["worst_year"]["period"], "2020年")
        self.assertAlmostEqual(metrics["worst_year"]["return"], -0.12)
        self.assertTrue(metrics["worst_year"]["complete"])
        self.assertEqual(metrics["worst_half_year"]["period"], "2020年下半年")
        self.assertAlmostEqual(metrics["worst_half_year"]["return"], -0.20)

    def test_drawdown_recovery_tracks_trough_to_prior_peak(self) -> None:
        metrics = drawdown_recovery_metrics(
            ["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04", "2020-01-05"],
            [0.0, 0.10, -0.20, -0.05, 0.12],
        )

        self.assertEqual(metrics["peak_date"], "2020-01-02")
        self.assertEqual(metrics["trough_date"], "2020-01-03")
        self.assertEqual(metrics["recovery_date"], "2020-01-05")
        self.assertEqual(metrics["recovery_days"], 2)
        self.assertTrue(metrics["recovered"])

    def test_drawdown_recovery_includes_opening_day_cost_against_initial_nav(self) -> None:
        metrics = drawdown_recovery_metrics(
            ["2020-01-02", "2020-01-03"],
            [-0.01, 0.01],
        )

        self.assertEqual(metrics["trough_date"], "2020-01-02")
        self.assertEqual(metrics["recovery_date"], "2020-01-03")
        self.assertEqual(metrics["recovery_days"], 1)

    def test_market_capture_uses_monthly_up_and_down_benchmark_periods(self) -> None:
        metrics = market_capture_metrics(
            ["2020-01-31", "2020-02-28", "2020-03-31", "2020-04-30", "2020-05-29"],
            [0.0, 0.05, -0.04, 0.05, -0.04],
            [100.0, 110.0, 99.0, 108.9, 98.01],
        )

        expected_up = ((1.05 ** 2) ** 6 - 1.0) / ((1.10 ** 2) ** 6 - 1.0)
        expected_down = ((0.96 ** 2) ** 6 - 1.0) / ((0.90 ** 2) ** 6 - 1.0)
        self.assertAlmostEqual(metrics["upside_capture_ratio"], expected_up)
        self.assertAlmostEqual(metrics["downside_capture_ratio"], expected_down)
        self.assertEqual(metrics["up_market_months"], 2)
        self.assertEqual(metrics["down_market_months"], 2)

    def test_rolling_window_ranges_use_inclusive_years_and_annual_steps(self) -> None:
        rows = rolling_window_ranges("2001-01-01", "2008-12-31", window_years=5)

        self.assertEqual(len(rows), 4)
        self.assertEqual(
            [(row["start_date"], row["end_date"]) for row in rows],
            [
                ("2001-01-01", "2005-12-31"),
                ("2002-01-01", "2006-12-31"),
                ("2003-01-01", "2007-12-31"),
                ("2004-01-01", "2008-12-31"),
            ],
        )

    def test_rolling_windows_are_independent_backtests_with_the_same_settings(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2022-12-31")
        cfg["rolling_window_years"] = 1
        cfg["annual_rebalance_month"] = 5
        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
            first_config = {**cfg, "start_date": "2020-01-01", "end_date": "2020-12-31"}
            first = run_backtest(conn, first_config, persist=False, include_comparison=False, include_month_analysis=False, include_rolling_analysis=False)

        rolling = result["summary"]["rolling_periods"]
        self.assertEqual([row["period"] for row in rolling], ["2020–2020", "2021–2021", "2022–2022"])
        self.assertAlmostEqual(rolling[0]["annualized_return"], first["summary"]["annualized_return"])
        self.assertEqual(rolling[0]["annual_return_drawdown_ratio"], first["summary"]["annual_return_drawdown_ratio"])

    def test_annual_rebalance_month_analysis_uses_engine_without_extra_history(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2021-06-30")
        cfg["annual_rebalance_month"] = 5
        cfg["rolling_window_years"] = 1
        cfg["rebalance_month_analysis_enabled"] = True
        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
            run_count = conn.execute("SELECT COUNT(*) AS count FROM backtest_runs").fetchone()["count"]
            rebalance_dates = [
                row["rebalance_date"]
                for row in conn.execute(
                    "SELECT rebalance_date FROM rebalance_events WHERE run_id=? ORDER BY rebalance_date",
                    (result["run_id"],),
                )
            ]

        scenarios = result["summary"]["rebalance_month_scenarios"]
        self.assertEqual(run_count, 1)
        self.assertEqual([row["month"] for row in scenarios], list(range(1, 13)))
        self.assertEqual([row["month"] for row in scenarios if row["selected"]], [5])
        self.assertTrue(all("annualized_return" in row and "max_drawdown" in row and "annual_return_drawdown_ratio" in row for row in scenarios))
        self.assertTrue(result["summary"]["rolling_periods"])
        self.assertTrue(all(date.fromisoformat(value).month == 5 for value in rebalance_dates[1:]))

    def test_ranking_counts_only_complete_positive_calendar_years(self) -> None:
        dates = ["2020-01-02", "2020-12-31", "2021-01-04", "2021-12-31", "2022-01-04"]
        self.assertEqual(yearly_positive_return_count(dates, [0.1, 0.1, -0.2, -0.1, 0.9]), 1)
        self.assertEqual(yearly_return_counts(["2020-01-31", "2020-12-01"], [0.1, 0.1]), (0, 0))
        ranking = ranking_metrics(0.12, 0.03, -0.18, 3, 4)
        self.assertAlmostEqual(ranking["excess_annualized_return"], 0.09)
        self.assertAlmostEqual(ranking["adjusted_calmar"], 0.5)
        self.assertAlmostEqual(ranking["annual_return_drawdown_ratio"], 2 / 3)
        self.assertAlmostEqual(ranking["positive_year_ratio"], 0.75)
        self.assertTrue(ranking["ranking_eligible"])
        self.assertIsNone(annual_return_drawdown_ratio(0.12, 0.0))

    def test_annual_metrics_include_opening_day_fees_and_cash_flow(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2021-01-08")
        cfg["fees"]["repo"]["investor_commission_rate"] = 0.0
        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
            rebalances = rows_to_dicts(
                conn.execute(
                    "SELECT rebalance_date,fee_cny,payload_json FROM rebalance_events WHERE run_id=? ORDER BY rebalance_date",
                    (result["run_id"],),
                )
            )

        opening_fee = rebalances[0]["fee_cny"]
        annual_payload = next(
            payload
            for payload in (json.loads(row["payload_json"]) for row in rebalances)
            if "year_fee_cny" in payload
        )
        self.assertAlmostEqual(annual_payload["year_fee_cny"], opening_fee)
        self.assertEqual(annual_payload["year_asset_performance"]["REPO"]["external_flow_cny"], -60_000.0)

    def test_ranking_rejects_repo_like_return_and_caps_tiny_drawdown(self) -> None:
        ranking = ranking_metrics(0.031, 0.03, -0.001, 0, 0)
        self.assertFalse(ranking["ranking_eligible"])
        self.assertAlmostEqual(ranking["adjusted_calmar"], 0.0125)

    def test_metrics_include_first_day_trading_cost(self) -> None:
        daily, cumulative, drawdowns = compute_metrics([990.0], [0.0], [1.0], initial_value=1000.0)
        self.assertAlmostEqual(daily[0], -0.01)
        self.assertAlmostEqual(cumulative[0], -0.01)
        self.assertAlmostEqual(drawdowns[0], -0.01)

    def test_minimal_rebalance_moves_only_to_band_edge(self) -> None:
        desired = minimal_rebalance_weights({"A": 0.60, "REPO": 0.40}, {"A": 0.50, "REPO": 0.50}, 0.02)
        self.assertAlmostEqual(desired["A"], 0.51)
        self.assertAlmostEqual(desired["REPO"], 0.49)

    def test_relative_rebalance_band_scales_with_target_weight(self) -> None:
        targets = {"BOND": 0.10, "REPO": 0.90}
        self.assertFalse(should_rebalance({"BOND": 0.075, "REPO": 0.925}, targets, 0.25))
        self.assertFalse(should_rebalance({"BOND": 0.125, "REPO": 0.875}, targets, 0.25))
        self.assertTrue(should_rebalance({"BOND": 0.0749, "REPO": 0.9251}, targets, 0.25))
        self.assertTrue(should_rebalance({"BOND": 0.1251, "REPO": 0.8749}, targets, 0.25))
        desired = minimal_rebalance_weights({"BOND": 0.20, "REPO": 0.80}, targets, 0.25)
        self.assertAlmostEqual(desired["BOND"], 0.125)
        self.assertAlmostEqual(desired["REPO"], 0.875)

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
        voo = next(asset for asset in cfg["assets"] if asset["symbol"] == "VOO")
        voo["enabled"] = True
        voo["target_weight"] = 0.20
        with db_session(db_path) as conn:
            result = run_backtest(conn, cfg)
        self.assertGreater(result["summary"]["withheld_tax_cny"], 0)

    def test_dividend_receivables_are_released_only_on_their_own_pay_date(self) -> None:
        cfg = normalize_config({})
        state = PortfolioState(
            cash_cny=0.0,
            positions={
                "TEST": Position(
                    symbol="TEST",
                    market="CN",
                    currency="CNY",
                    asset_type="cn_etf",
                    quantity=100.0,
                )
            },
        )
        first = {"symbol": "TEST", "pay_date": "2026-01-05", "div_cash": 1.0, "currency": "CNY"}
        second = {"symbol": "TEST", "pay_date": "2026-01-12", "div_cash": 2.0, "currency": "CNY"}

        paid_on_ex_date = _apply_dividend_events(
            state,
            "2026-01-02",
            {"2026-01-02": [first, second]},
            {},
            {},
            cfg["fees"],
        )
        paid_first = _apply_dividend_events(state, "2026-01-05", {}, {}, {}, cfg["fees"])
        paid_second = _apply_dividend_events(state, "2026-01-12", {}, {}, {}, cfg["fees"])

        self.assertEqual(paid_on_ex_date, 0.0)
        self.assertEqual(paid_first, 100.0)
        self.assertEqual(paid_second, 200.0)
        self.assertEqual(state.cash_cny, 300.0)
        self.assertEqual(state.dividend_receivable_cny, 0.0)

    def test_raw_ex_dividend_price_drop_plus_cash_dividend_preserves_value(self) -> None:
        cfg = normalize_config({})
        state = PortfolioState(cash_cny=0.0)
        state.positions["510300.SH"] = Position("510300.SH", "CN", "CNY", "cn_etf", quantity=100)
        before, _ = _portfolio_value(state, {"510300.SH": 10.0}, {}, date(2022, 1, 18))
        event = {
            "symbol": "510300.SH",
            "ex_date": "2022-01-19",
            "pay_date": "2022-01-21",
            "div_cash": 1.0,
            "currency": "CNY",
        }
        _apply_dividend_events(state, "2022-01-19", {"2022-01-19": [event]}, {}, {}, cfg["fees"])
        after, values = _portfolio_value(state, {"510300.SH": 9.0}, {}, date(2022, 1, 19))
        self.assertAlmostEqual(before, after)
        self.assertAlmostEqual(values["REPO"], 100.0)
        self.assertAlmostEqual(state.total_dividend_cny, 100.0)

    def test_fund_dividend_payment_is_available_before_rebalance(self) -> None:
        db_path, cfg = build_synced_db("2020-01-01", "2021-01-08")
        cfg["monthly_spend_cny"] = 0.0
        cfg["rebalance_band"] = 0.0
        for asset in cfg["assets"]:
            enabled = asset["symbol"] == "510300.SH"
            asset["enabled"] = enabled
            asset["target_weight"] = 1.0 if enabled else 0.0
        with db_session(db_path) as conn:
            conn.execute("DELETE FROM fund_dividends WHERE symbol='510300.SH'")
            conn.execute(
                """
                INSERT INTO fund_dividends(symbol, ann_date, record_date, ex_date, pay_date, div_cash, currency, source)
                VALUES('510300.SH', '2020-12-30', '2020-12-30', '2020-12-30', '2021-01-01', 0.10, 'CNY', 'test:dividend')
                """
            )
            result = run_backtest(conn, cfg)
            row = conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? AND trade_date='2021-01-01'",
                (result["run_id"],),
            ).fetchone()
            pay_day_buys = conn.execute(
                "SELECT COUNT(*) AS count FROM trades WHERE run_id=? AND trade_date='2021-01-01' AND side='BUY'",
                (result["run_id"],),
            ).fetchone()["count"]

        payload = json.loads(row["payload_json"])
        self.assertEqual(payload["dividend_receivable_cny"], 0.0)
        self.assertGreater(pay_day_buys, 0)

    def test_comparison_assets_roll_risk_weights_into_hs300(self) -> None:
        cfg = normalize_config({})
        assets = comparison_assets(cfg)
        weights = {asset["symbol"]: asset["target_weight"] for asset in assets}
        self.assertAlmostEqual(weights["510300.SH"], 0.25)

        self.assertAlmostEqual(weights["518880.SH"], 0.25)
        next(asset for asset in cfg["assets"] if asset["symbol"] == "VOO")["enabled"] = False
        cn_sp500 = next(asset for asset in cfg["assets"] if asset["symbol"] == "513500.SH")
        cn_sp500["enabled"] = True
        cn_sp500["target_weight"] = 0.20
        assets = comparison_assets(cfg)
        weights = {asset["symbol"]: asset["target_weight"] for asset in assets}
        self.assertAlmostEqual(weights["510300.SH"], 0.45)

        a100 = next(asset for asset in cfg["assets"] if asset["symbol"] == "159631.SZ")
        hs300 = next(asset for asset in cfg["assets"] if asset["symbol"] == "510300.SH")
        hs300["enabled"] = False
        a100["enabled"] = True
        a100["target_weight"] = 0.12
        assets = comparison_assets(cfg)
        weights = {asset["symbol"]: asset["target_weight"] for asset in assets}
        self.assertNotIn("510300.SH", weights)
        self.assertAlmostEqual(weights["159631.SZ"], 0.57)

    def test_benchmark_forward_fill_and_rebalance_band(self) -> None:
        self.assertAlmostEqual(benchmark_returns([None, 100, None, 110])[-1], 0.1)
        self.assertFalse(should_rebalance({"A": 0.51, "REPO": 0.49}, {"A": 0.50, "REPO": 0.50}, 0.02))
        self.assertTrue(should_rebalance({"A": 0.52, "REPO": 0.48}, {"A": 0.50, "REPO": 0.50}, 0.02))

    def test_reference_trading_days_exclude_weekday_market_holidays(self) -> None:
        days = reference_trading_days(
            "2026-01-01",
            "2026-01-06",
            {"2026-01-02": 100.0, "2026-01-05": 101.0, "2026-01-06": 102.0},
            {"2026-01-01": 1.5},
        )
        self.assertEqual(days, [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)])

    def test_validation_errors_surface(self) -> None:
        db_path, _cfg = build_synced_db("2020-01-01", "2020-01-31")
        bad = normalize_config({"start_date": "2020-02-01", "end_date": "2020-01-01"})
        with db_session(db_path) as conn:
            with self.assertRaises(BacktestError):
                run_backtest(conn, bad)

    def test_effective_weights_respect_price_and_inception(self) -> None:
        cfg = normalize_config({"start_date": "2012-01-01"})
        for asset in cfg["assets"]:
            asset["enabled"] = asset["symbol"] in {"VOO", "510300.SH"}
            asset["target_weight"] = 0.20 if asset["symbol"] == "VOO" else (0.12 if asset["symbol"] == "510300.SH" else 0.0)
        weights = effective_weights(cfg, __import__("datetime").date(2012, 1, 3), {"VOO": 1, "510300.SH": None})
        self.assertIn("VOO", weights)
        self.assertNotIn("510300.SH", weights)
        self.assertGreater(weights["REPO"], 0.7)

    def test_effective_weights_use_configured_price_proxy_before_primary_inception(self) -> None:
        cfg = normalize_config({"start_date": "2012-01-01"})
        for asset in cfg["assets"]:
            asset["enabled"] = asset["symbol"] in {"510300.SH", "518880.SH"}
            asset["target_weight"] = 0.25 if asset["enabled"] else 0.0
        weights = effective_weights(
            cfg,
            date(2012, 1, 3),
            {"VOO": 1, "510300.SH": 0.7, "160706": 0.7, "518880.SH": 2.5, "Au99.99": 2.5},
        )
        self.assertIn("160706", weights)
        self.assertNotIn("510300.SH", weights)
        self.assertIn("Au99.99", weights)
        self.assertNotIn("518880.SH", weights)

    def test_2008_backtest_buys_gold_and_30y_treasury_proxies(self) -> None:
        cfg = normalize_config(
            {
                "start_date": "2008-01-02",
                "end_date": "2008-02-01",
                "monthly_spend_cny": 0,
            }
        )
        for asset in cfg["assets"]:
            selected = asset["symbol"] in {"518880.SH", "CBA21801"}
            asset["enabled"] = selected
            asset["target_weight"] = 0.45 if selected else 0.0

        db_path = temp_db_path()
        init_db(db_path)
        with db_session(db_path) as conn:
            seed_fixture_data(conn, cfg, cfg["start_date"], cfg["end_date"])
            result = run_backtest(conn, cfg)
            buys = rows_to_dicts(
                conn.execute(
                    "SELECT symbol,side FROM trades WHERE run_id=? AND side='BUY'",
                    (result["run_id"],),
                )
            )

        bought_symbols = {trade["symbol"] for trade in buys}
        self.assertIn("Au99.99", bought_symbols)
        self.assertIn("CN30Y.YIELD-TR", bought_symbols)
        self.assertNotIn("518880.SH", bought_symbols)
        self.assertNotIn("CBA21801", bought_symbols)

    def test_30y_treasury_switches_from_proxy_to_official_index(self) -> None:
        cfg = normalize_config({"start_date": "2008-01-02"})
        for asset in cfg["assets"]:
            selected = asset["symbol"] == "CBA21801"
            asset["enabled"] = selected
            asset["target_weight"] = 0.50 if selected else 0.0

        prices = {asset["symbol"]: None for asset in cfg["assets"]}
        prices["CN30Y.YIELD-TR"] = 100.0
        before = effective_weights(cfg, date(2008, 1, 3), prices)
        prices["CBA21801"] = 1000.0
        after = effective_weights(cfg, date(2011, 1, 4), prices)

        self.assertAlmostEqual(before["CN30Y.YIELD-TR"], 0.50)
        self.assertNotIn("CBA21801", before)
        self.assertAlmostEqual(after["CBA21801"], 0.50)
        self.assertNotIn("CN30Y.YIELD-TR", after)

    def test_a100_uses_csi100_proxy_before_etf_inception(self) -> None:
        cfg = normalize_config({"start_date": "2021-01-01"})
        for asset in cfg["assets"]:
            is_a100 = asset["symbol"] == "159631.SZ"
            asset["enabled"] = is_a100
            asset["target_weight"] = 0.50 if is_a100 else 0.0

        prices = {asset["symbol"]: 1.0 for asset in cfg["assets"]}
        prices.update({"159631.SZ": None, "000903.SH": 1000.0})
        before = effective_weights(cfg, date(2021, 12, 31), prices)
        prices["159631.SZ"] = 1.2
        after = effective_weights(cfg, date(2022, 8, 18), prices)

        self.assertAlmostEqual(before["000903.SH"], 0.50)
        self.assertNotIn("159631.SZ", before)
        self.assertAlmostEqual(after["159631.SZ"], 0.50)
        self.assertNotIn("000903.SH", after)

    def test_dividend_low_vol_uses_total_return_index_before_etf_trading(self) -> None:
        cfg = normalize_config({"start_date": "2006-01-04"})
        for asset in cfg["assets"]:
            is_dividend_low_vol = asset["symbol"] == "512890.SH"
            asset["enabled"] = is_dividend_low_vol
            asset["target_weight"] = 0.50 if is_dividend_low_vol else 0.0

        prices = {asset["symbol"]: None for asset in cfg["assets"]}
        prices["H20269.CSI"] = 1000.0
        before = effective_weights(cfg, date(2018, 12, 28), prices)
        prices["512890.SH"] = 0.98
        after = effective_weights(cfg, date(2019, 1, 18), prices)

        self.assertAlmostEqual(before["H20269.CSI"], 0.50)
        self.assertNotIn("512890.SH", before)
        self.assertAlmostEqual(after["512890.SH"], 0.50)
        self.assertNotIn("H20269.CSI", after)

    def test_new_broad_etfs_use_their_index_proxy_before_inception(self) -> None:
        for symbol, proxy_symbol, before_day, switch_day in (
            ("510500.SH", "000905.SH", date(2013, 1, 4), date(2013, 3, 15)),
            ("512100.SH", "000852.SH", date(2016, 10, 31), date(2016, 11, 4)),
        ):
            cfg = normalize_config({"start_date": before_day.isoformat()})
            for asset in cfg["assets"]:
                selected = asset["symbol"] == symbol
                asset["enabled"] = selected
                asset["target_weight"] = 0.50 if selected else 0.0

            prices = {asset["symbol"]: 1.0 for asset in cfg["assets"]}
            prices.update({symbol: None, proxy_symbol: 1000.0})
            before = effective_weights(cfg, before_day, prices)
            prices[symbol] = 1.2
            after = effective_weights(cfg, switch_day, prices)

            self.assertAlmostEqual(before[proxy_symbol], 0.50)
            self.assertNotIn(symbol, before)
            self.assertAlmostEqual(after[symbol], 0.50)
            self.assertNotIn(proxy_symbol, after)

    def test_gold_switches_from_518880_to_518850_from_2021(self) -> None:
        cfg = normalize_config({})
        prices = {asset["symbol"]: 1.0 for asset in cfg["assets"]}
        prices.update({"518880.SH": 4.0, "518850.SH": 4.0})
        before = effective_weights(cfg, date(2020, 12, 31), prices)
        after = effective_weights(cfg, date(2021, 1, 4), prices)
        self.assertIn("518880.SH", before)
        self.assertNotIn("518850.SH", before)
        self.assertIn("518850.SH", after)
        self.assertNotIn("518880.SH", after)

    def test_gold_backtest_switches_symbol_and_collects_replacement_dividend(self) -> None:
        cfg = normalize_config(
            {
                "start_date": "2020-12-21",
                "end_date": "2021-07-02",
                "monthly_spend_cny": 0,
            }
        )
        for asset in cfg["assets"]:
            is_gold = asset["symbol"] == "518880.SH"
            asset["enabled"] = is_gold
            asset["target_weight"] = 0.9 if is_gold else 0.0
        db_path = temp_db_path()
        init_db(db_path)
        with db_session(db_path) as conn:
            seed_fixture_data(conn, cfg, cfg["start_date"], cfg["end_date"])
            result = run_backtest(conn, cfg)
            trades = rows_to_dicts(
                conn.execute(
                    "SELECT trade_date,symbol,side FROM trades WHERE run_id=? ORDER BY trade_date,side",
                    (result["run_id"],),
                )
            )
        self.assertTrue(any(row["symbol"] == "518880.SH" and row["side"] == "BUY" for row in trades))
        self.assertTrue(
            any(
                row["symbol"] == "518880.SH" and row["side"] == "SELL" and row["trade_date"] >= "2021-01-01"
                for row in trades
            )
        )
        self.assertTrue(
            any(
                row["symbol"] == "518850.SH" and row["side"] == "BUY" and row["trade_date"] >= "2021-01-01"
                for row in trades
            )
        )
        self.assertGreater(result["summary"]["total_dividend_cny"], 0.0)

    def test_money_fund_target_falls_back_to_repo_until_trade_start_and_price(self) -> None:
        cfg = normalize_config({"repo_symbol": "511990.SH"})
        prices = {asset["symbol"]: 1.0 for asset in cfg["assets"]}
        prices["511990.SH"] = 100.0
        fallback = effective_weights(cfg, date(2013, 1, 25), prices)
        available = effective_weights(cfg, date(2013, 1, 28), prices)
        missing_price = effective_weights(cfg, date(2013, 1, 28), {**prices, "511990.SH": None})
        self.assertGreater(fallback.get("REPO", 0.0), 0)
        self.assertNotIn("511990.SH", fallback)
        self.assertGreater(available.get("511990.SH", 0.0), 0)
        self.assertNotIn("REPO", available)
        self.assertGreater(missing_price.get("REPO", 0.0), 0)

    def test_treasury_index_weights_are_combinable(self) -> None:
        cfg = normalize_config({})
        cfg["assets"] = [
            {**asset, "enabled": asset.get("asset_type") == "cn_bond_index", "target_weight": 0.1 if asset.get("asset_type") == "cn_bond_index" else 0.0}
            for asset in cfg["assets"]
        ]
        prices = {asset["symbol"]: 100.0 for asset in cfg["assets"]}
        weights = effective_weights(cfg, date(2020, 1, 2), prices)
        self.assertAlmostEqual(weights["CBA03101"], 0.1)
        self.assertAlmostEqual(weights["CBA06501"], 0.1)
        self.assertAlmostEqual(weights["CBA21801"], 0.1)
        self.assertAlmostEqual(weights["REPO"], 0.7)

    def test_treasury_index_uses_fractional_fee_free_return_units(self) -> None:
        cfg = normalize_config({})
        state = PortfolioState(cash_cny=2000.0)
        position = Position("CBA21801", "CN", "CNY", "cn_bond_index")
        trades: list[dict] = []

        spent = _buy_position(
            state, position, date(2020, 1, 2), 1234.56, 100.0, {}, cfg["fees"], trades, False, "rebalance"
        )

        self.assertAlmostEqual(spent, 1234.56)
        self.assertAlmostEqual(position.quantity, 12.3456)
        self.assertEqual(trades[-1]["fee"], 0.0)
        self.assertEqual(state.total_fees_cny, 0.0)
        proceeds = _sell_position(
            state, position, date(2020, 1, 3), 12.3456, 101.0, {}, cfg["fees"], trades, "rebalance"
        )
        self.assertAlmostEqual(proceeds, 1246.9056)
        self.assertEqual(trades[-1]["fee"], 0.0)
        self.assertAlmostEqual(position.quantity, 0.0)

    def test_money_fund_backtest_rolls_one_day_repo_until_first_tradable_day(self) -> None:
        cfg = normalize_config(
            {
                "start_date": "2013-01-21",
                "end_date": "2013-02-15",
                "repo_symbol": "511990.SH",
                "monthly_spend_cny": 0,
            }
        )
        cfg["assets"] = [{**asset, "enabled": False, "target_weight": 0.0} for asset in cfg["assets"]]
        db_path = temp_db_path()
        init_db(db_path)
        with db_session(db_path) as conn:
            seed_fixture_data(conn, cfg, cfg["start_date"], cfg["end_date"])
            result = run_backtest(conn, cfg)
            daily = rows_to_dicts(
                conn.execute("SELECT trade_date,payload_json FROM portfolio_daily WHERE run_id=? ORDER BY trade_date", (result["run_id"],))
            )
            trades = rows_to_dicts(
                conn.execute("SELECT trade_date,symbol,side FROM trades WHERE run_id=? ORDER BY trade_date", (result["run_id"],))
            )
        before_listing = [json.loads(row["payload_json"]) for row in daily if row["trade_date"] < "2013-01-28"]
        self.assertTrue(before_listing)
        self.assertTrue(all(payload["treasury_fallback_active"] for payload in before_listing))
        money_fund_buys = [trade for trade in trades if trade["symbol"] == "511990.SH" and trade["side"] == "BUY"]
        self.assertTrue(money_fund_buys)
        self.assertGreaterEqual(money_fund_buys[0]["trade_date"], "2013-01-28")

    def test_cash_shortfall_sells_money_fund_before_risk_assets(self) -> None:
        cfg = normalize_config({})
        state = PortfolioState(
            cash_cny=0.0,
            positions={
                "510300.SH": Position("510300.SH", "CN", "CNY", "cn_etf", quantity=10_000, cost_basis_cny=10_000),
                "511990.SH": Position("511990.SH", "CN", "CNY", "money_fund", quantity=100, cost_basis_cny=10_000),
            },
        )
        trades: list[dict] = []
        _cover_cash_shortfall(state, 5_000.0, date(2020, 1, 2), {"510300.SH": 1.0, "511990.SH": 100.0}, {}, cfg["fees"], trades)

        self.assertTrue(trades)
        self.assertEqual(trades[0]["symbol"], "511990.SH")
        self.assertEqual(trades[0]["side"], "SELL")
        self.assertEqual(state.positions["510300.SH"].quantity, 10_000)

    def test_money_fund_total_return_price_ignores_legacy_cash_dividend_rows(self) -> None:
        cfg = normalize_config({"start_date": "2020-01-01", "end_date": "2020-01-10", "monthly_spend_cny": 0, "repo_symbol": "511990.SH"})
        cfg["assets"] = [{**asset, "enabled": False, "target_weight": 0.0} for asset in cfg["assets"]]
        db_path = temp_db_path()
        init_db(db_path)
        with db_session(db_path) as conn:
            seed_fixture_data(conn, cfg, cfg["start_date"], cfg["end_date"])
            conn.execute(
                "INSERT INTO fund_dividends(symbol,ann_date,record_date,ex_date,pay_date,div_cash,currency,source) VALUES(?,?,?,?,?,?,?,?)",
                ("511990.SH", "2020-01-02", "2020-01-02", "2020-01-02", "2020-01-02", 1.0, "CNY", "legacy:test"),
            )
            result = run_backtest(conn, cfg)
        self.assertEqual(result["summary"]["total_dividend_cny"], 0.0)


if __name__ == "__main__":
    unittest.main()
