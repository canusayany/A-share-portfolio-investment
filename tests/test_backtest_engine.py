from __future__ import annotations

import json
import unittest
from datetime import date

from app.config import normalize_config
from app.db import db_session, init_db, rows_to_dicts
from app.services.backtest_engine import (
    BacktestError,
    PortfolioState,
    Position,
    _apply_dividend_events,
    _asset_period_performance,
    _buy_position,
    _invest_idle_cash_in_repo,
    _invest_repo_cash,
    _mature_repo_lots,
    _portfolio_value,
    _repo_spend_reserve,
    benchmark_returns,
    comparison_assets,
    compute_metrics,
    effective_weights,
    minimal_rebalance_weights,
    repo_tenor_days,
    repo_fixed_target_weight,
    reference_trading_days,
    run_backtest,
    has_investable_asset_target,
    should_rebalance,
)
from tests.helpers import build_synced_db, seed_fixture_data, temp_db_path


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
        self.assertAlmostEqual(weights["VOO"], 0.24)
        self.assertAlmostEqual(weights["512890.SH"], 0.096)
        self.assertAlmostEqual(sum(weights.values()), 1.0)

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

    def test_metrics_include_first_day_trading_cost(self) -> None:
        daily, cumulative, drawdowns = compute_metrics([990.0], [0.0], [1.0], initial_value=1000.0)
        self.assertAlmostEqual(daily[0], -0.01)
        self.assertAlmostEqual(cumulative[0], -0.01)
        self.assertAlmostEqual(drawdowns[0], -0.01)

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
        self.assertAlmostEqual(weights["510300.SH"], 0.40)

        self.assertAlmostEqual(weights["518880.SH"], 0.10)
        next(asset for asset in cfg["assets"] if asset["symbol"] == "VOO")["enabled"] = False
        cn_sp500 = next(asset for asset in cfg["assets"] if asset["symbol"] == "513500.SH")
        cn_sp500["enabled"] = True
        cn_sp500["target_weight"] = 0.20
        assets = comparison_assets(cfg)
        weights = {asset["symbol"]: asset["target_weight"] for asset in assets}
        self.assertAlmostEqual(weights["510300.SH"], 0.40)

        a100 = next(asset for asset in cfg["assets"] if asset["symbol"] == "159631.SZ")
        hs300 = next(asset for asset in cfg["assets"] if asset["symbol"] == "510300.SH")
        hs300["enabled"] = False
        a100["enabled"] = True
        a100["target_weight"] = 0.12
        assets = comparison_assets(cfg)
        weights = {asset["symbol"]: asset["target_weight"] for asset in assets}
        self.assertNotIn("510300.SH", weights)
        self.assertAlmostEqual(weights["159631.SZ"], 0.40)

    def test_benchmark_forward_fill_and_rebalance_band(self) -> None:
        self.assertAlmostEqual(benchmark_returns([None, 100, None, 110])[-1], 0.1)
        self.assertFalse(should_rebalance({"A": 0.51, "REPO": 0.49}, {"A": 0.50, "REPO": 0.50}, 0.02))
        self.assertTrue(should_rebalance({"A": 0.55, "REPO": 0.45}, {"A": 0.50, "REPO": 0.50}, 0.02))

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
        weights = effective_weights(cfg, __import__("datetime").date(2012, 1, 3), {"VOO": 1, "510300.SH": None})
        self.assertIn("VOO", weights)
        self.assertNotIn("510300.SH", weights)
        self.assertGreater(weights["REPO"], 0.7)

    def test_effective_weights_use_configured_price_proxy_before_primary_inception(self) -> None:
        cfg = normalize_config({"start_date": "2012-01-01"})
        weights = effective_weights(
            cfg,
            date(2012, 1, 3),
            {"VOO": 1, "510300.SH": 0.7, "160706": 0.7, "518880.SH": 2.5, "Au99.99": 2.5},
        )
        self.assertIn("160706", weights)
        self.assertNotIn("510300.SH", weights)
        self.assertIn("Au99.99", weights)
        self.assertNotIn("518880.SH", weights)

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

    def test_bond_etf_target_falls_back_to_repo_until_trade_start_and_price(self) -> None:
        for symbol, before, trade_start in (
            ("511010.SH", date(2013, 3, 22), date(2013, 3, 25)),
            ("511260.SH", date(2017, 8, 23), date(2017, 8, 24)),
            ("511090.SH", date(2023, 6, 12), date(2023, 6, 13)),
        ):
            cfg = normalize_config({"repo_symbol": symbol})
            prices = {asset["symbol"]: 1.0 for asset in cfg["assets"]}
            prices[symbol] = 100.0
            fallback = effective_weights(cfg, before, prices)
            available = effective_weights(cfg, trade_start, prices)
            missing_price = effective_weights(cfg, trade_start, {**prices, symbol: None})
            self.assertGreater(fallback.get("REPO", 0.0), 0)
            self.assertNotIn(symbol, fallback)
            self.assertGreater(available.get(symbol, 0.0), 0)
            self.assertNotIn("REPO", available)
            self.assertGreater(missing_price.get("REPO", 0.0), 0)

    def test_bond_etf_backtest_rolls_one_day_repo_until_first_tradable_day(self) -> None:
        cfg = normalize_config(
            {
                "start_date": "2013-03-20",
                "end_date": "2013-04-05",
                "repo_symbol": "511010.SH",
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
        before_listing = [json.loads(row["payload_json"]) for row in daily if row["trade_date"] < "2013-03-25"]
        self.assertTrue(before_listing)
        self.assertTrue(all(payload["treasury_fallback_active"] for payload in before_listing))
        bond_buys = [trade for trade in trades if trade["symbol"] == "511010.SH" and trade["side"] == "BUY"]
        self.assertTrue(bond_buys)
        self.assertGreaterEqual(bond_buys[0]["trade_date"], "2013-03-25")


if __name__ == "__main__":
    unittest.main()
