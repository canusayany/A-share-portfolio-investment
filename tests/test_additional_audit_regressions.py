from datetime import date
import json
import unittest

from app.db import db_session
from app.services.backtest_engine import (
    _AssetCashAdjustmentLedger,
    PortfolioState, Position, _buy_position, _minimum_rebalance_buy_budget_cny,
    _portfolio_value, _sell_position, adjusted_price_and_share_scale_series,
    reference_value_maps, run_backtest,
)
from tests.helpers import build_synced_db


class AdditionalAuditRegressionTests(unittest.TestCase):
    def fixture(self, start="2020-01-02", end="2020-01-10", symbol="VOO"):
        path, config = build_synced_db(start, end)
        config.update(monthly_spend_cny=0, rebalance_frequency="weekly", rebalance_band=0, rebalance_to_target=True)
        for asset in config["assets"]:
            asset["enabled"] = asset["symbol"] == symbol
            asset["target_weight"] = 0.5 if asset["enabled"] else 0
        config["fees"]["cn_etf"].update(commission_rate=0, min_commission_cny=0)
        config["fees"]["repo"]["investor_commission_rate"] = 0
        config["fees"]["fx"].update(bank_in_spread_bps=0, bank_out_spread_bps=0, ibkr_auto_fx_markup=0)
        config["fees"]["ibkr_us_etf"].update(fixed_per_share_usd=0, fixed_min_usd=0, sec_transaction_fee_rate=0, finra_taf_per_share_usd=0)
        config["fees"]["hk_connect_etf"].update(broker_commission_rate=0, min_broker_commission_hkd=0, trading_fee_rate=0, transaction_levy_rate=0, afrc_transaction_levy_rate=0, stock_settlement_fee_rate=0, fx_spread_bps=0)
        with db_session(path) as conn:
            conn.execute("UPDATE prices SET open=100,close=100 WHERE symbol=?", (symbol,))
            conn.execute("UPDATE fx_rates SET rate=1")
            conn.execute("UPDATE repo_rates SET open_rate=0,close_rate=0")
        return path, config

    def run_backtest(self, conn, config):
        return run_backtest(conn, config, include_comparison=False, include_month_analysis=False, include_rolling_analysis=False)

    def test_verified_split_is_applied_across_missing_quote(self):
        rows = [{"trade_date": "2021-10-21", "price": 2, "adj_factor": 1},
                {"trade_date": "2021-10-26", "price": 1, "adj_factor": 2.0462}]
        prices, scales = adjusted_price_and_share_scale_series(rows, {"2021-10-25": 2})
        self.assertEqual(prices["2021-10-26"], 2)
        self.assertEqual(scales["2021-10-26"], 2)
        # A freshly initialized position already uses actual post-split units.
        _, fresh = adjusted_price_and_share_scale_series(rows[1:], {"2021-10-25": 2})
        self.assertEqual(fresh["2021-10-26"], 1)

    def test_hk_split_minimum_budget_buys_one_real_board_lot(self):
        _, config = self.fixture()
        pos = Position("03195.HK", "HK", "HKD", "hk_connect_etf", share_scale=2)
        budget = _minimum_rebalance_buy_budget_cny(pos, 100, 2, {"HKD/CNY": 1}, config["fees"], False)
        self.assertEqual(budget, 100)
        state = PortfolioState(cash_cny=1000, positions={pos.symbol: pos})
        trades = []
        _buy_position(state, pos, date(2020, 1, 2), budget, 2, {"HKD/CNY": 1}, config["fees"], trades, False, "rebalance")
        self.assertEqual(trades[0]["quantity"], 100)
        self.assertEqual(_portfolio_value(state, {pos.symbol: 2}, {"HKD/CNY": 1})[0], 1000)

    def test_us_split_whole_share_buy_and_minimum_budget(self):
        _, config = self.fixture()
        pos = Position("VOO", "US", "USD", "us_etf", share_scale=2)
        rates = {"USD/CNY": 1}
        budget = _minimum_rebalance_buy_budget_cny(pos, 100, 200, rates, config["fees"], False)
        self.assertEqual(budget, 100)
        state = PortfolioState(cash_cny=100, positions={pos.symbol: pos})
        trades = []
        _buy_position(state, pos, date(2020, 1, 2), budget, 200, rates, config["fees"], trades, False, "rebalance")
        self.assertEqual(trades[0]["quantity"], 1)
        self.assertEqual(trades[0]["price"], 100)
        self.assertEqual(_portfolio_value(state, {pos.symbol: 200}, rates)[0], 100)

    def test_us_per_share_commission_uses_actual_shares(self):
        _, config = self.fixture()
        config["fees"]["ibkr_us_etf"]["fixed_per_share_usd"] = 0.005
        pos = Position("VOO", "US", "USD", "us_etf", share_scale=2)
        state = PortfolioState(cash_cny=20001, positions={pos.symbol: pos})
        trades = []
        _buy_position(state, pos, date(2020, 1, 2), 20001, 200, {"USD/CNY": 1}, config["fees"], trades, False, "rebalance")
        self.assertEqual(trades[0]["quantity"], 200)
        self.assertEqual(trades[0]["fee"], 1)
        _sell_position(state, pos, date(2020, 1, 3), pos.quantity, 200, {"USD/CNY": 1}, config["fees"], trades, "rebalance")
        self.assertEqual(trades[1]["fee"], 1)
        self.assertEqual(state.cash_cny, 19999)

    def test_minimum_sale_commission_above_gross_is_still_debited(self):
        _, config = self.fixture()
        config["fees"]["cn_etf"]["min_commission_cny"] = 5
        config["fees"]["hk_connect_etf"]["min_broker_commission_hkd"] = 5
        config["fees"]["ibkr_us_etf"].update(fixed_min_usd=5, fixed_max_trade_pct=100)
        for symbol, market, currency, asset_type in (
            ("512890.SH", "CN", "CNY", "cn_etf"),
            ("03195.HK", "HK", "HKD", "hk_connect_etf"),
            ("VOO", "US", "USD", "us_etf"),
        ):
            with self.subTest(currency=currency):
                pos = Position(symbol, market, currency, asset_type, quantity=1)
                state = PortfolioState(cash_cny=10, positions={symbol: pos})
                rates = {"USD/CNY": 1, "HKD/CNY": 1}
                trades = []
                _sell_position(state, pos, date(2020, 1, 2), 1, 1, rates, config["fees"], trades, "rebalance")
                self.assertEqual(state.cash_cny, 6)
                self.assertEqual(state.total_fees_cny, 5)
                self.assertEqual(_portfolio_value(state, {symbol: 1}, rates)[0], 11 - 5)

    def test_zero_consumption_cannot_become_a_cash_injection_on_hk_holiday(self):
        path, config = self.fixture("2020-01-31", "2020-02-04", "03195.HK")
        next(asset for asset in config["assets"] if asset["enabled"])["target_weight"] = 1
        with db_session(path) as conn:
            conn.execute("UPDATE trading_calendar SET is_open=0 WHERE market='HK' AND trade_date='2020-02-03'")
            result = self.run_backtest(conn, config)
            row = conn.execute("SELECT flow_cny,total_asset_cny FROM portfolio_daily WHERE run_id=? AND trade_date='2020-02-03'", (result["run_id"],)).fetchone()
        self.assertEqual(row["flow_cny"], 0)
        self.assertEqual(result["summary"]["total_spend_cny"], 0)
        self.assertAlmostEqual(row["total_asset_cny"], 1_000_000 - 1_000_000 * 0.00008 * 3 / 365, places=5)

    def test_proxy_switch_retries_when_old_opening_quote_returns(self):
        path, config = self.fixture("2020-12-31", "2021-01-07", "518880.SH")
        config.update(rebalance_frequency="yearly", annual_rebalance_month=7)
        with db_session(path) as conn:
            conn.execute("UPDATE trading_calendar SET is_open=0 WHERE market='CN' AND trade_date='2021-01-01'")
            conn.execute("UPDATE prices SET open=NULL WHERE symbol='518880.SH' AND trade_date='2021-01-04'")
            result = self.run_backtest(conn, config)
            trades = conn.execute("SELECT trade_date,symbol,side FROM trades WHERE run_id=? AND reason='asset_replacement'", (result["run_id"],)).fetchall()
        self.assertEqual([(row["trade_date"], row["symbol"], row["side"]) for row in trades], [
            ("2021-01-05", "518880.SH", "SELL"), ("2021-01-05", "518850.SH", "BUY"),
        ])

    def test_open_rebalance_cannot_use_later_same_day_fx_close(self):
        path, config = self.fixture()
        with db_session(path) as conn:
            conn.execute("UPDATE fx_rates SET rate=2 WHERE pair='USD/CNY' AND trade_date>='2020-01-06'")
            result = self.run_backtest(conn, config)
            trades = conn.execute("SELECT * FROM trades WHERE run_id=? AND trade_date='2020-01-06'", (result["run_id"],)).fetchall()
            asset = conn.execute("SELECT total_asset_cny FROM portfolio_daily WHERE run_id=? AND trade_date='2020-01-06'", (result["run_id"],)).fetchone()[0]
        self.assertEqual(len(trades), 0)
        self.assertAlmostEqual(asset, 1_500_000)

    def test_open_fx_snapshot_includes_foreign_sessions_during_cn_closure(self):
        rates = {"USD/CNY": {"2020-09-30": 1, "2020-10-08": 2, "2020-10-09": 3}}
        snapshot = reference_value_maps(rates, [date(2020, 10, 9)], prior_only=True)
        self.assertEqual(snapshot["USD/CNY"]["2020-10-09"], 2)

    def test_disabled_us_fractional_shares_applies_to_sales(self):
        path, config = self.fixture()
        config["allow_fractional_us_shares"] = False
        with db_session(path) as conn:
            conn.execute("UPDATE prices SET open=110,close=110 WHERE symbol='VOO' AND trade_date>='2020-01-06'")
            result = self.run_backtest(conn, config)
            trades = conn.execute("SELECT quantity FROM trades WHERE run_id=? AND trade_date='2020-01-06' AND side='SELL'", (result["run_id"],)).fetchall()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["quantity"], 228)

    def annual_fixture(self, spend=0, weight=0.5):
        path, config = self.fixture("2022-01-03", "2023-01-06", "512890.SH")
        config.update(monthly_spend_cny=spend, rebalance_frequency="yearly", annual_rebalance_month=1)
        next(asset for asset in config["assets"] if asset["enabled"])["target_weight"] = weight
        with db_session(path) as conn:
            conn.execute("DELETE FROM fund_dividends")
        return path, config

    def annual_payload(self, conn, run_id):
        payloads = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload_json FROM rebalance_events WHERE run_id=? ORDER BY rebalance_date", (run_id,)
        )]
        return [payload for payload in payloads if payload.get("year_label") == 2022][-1]

    def test_annual_consumption_sale_does_not_create_asset_loss(self):
        path, config = self.annual_fixture(spend=5000, weight=1)
        with db_session(path) as conn:
            result = self.run_backtest(conn, config)
            annual = self.annual_payload(conn, result["run_id"])
        self.assertEqual(annual["year_profit_cny"], 0)
        self.assertEqual(annual["year_asset_performance"]["512890.SH"]["profit_cny"], 0)
        self.assertEqual(annual["asset_performance"]["512890.SH"]["profit_cny"], 0)
        self.assertEqual(annual["asset_return_basis"], "profit_on_start_position_value")

    def test_annual_rebalance_purchase_excludes_transferred_principal(self):
        path, config = self.annual_fixture()
        config.update(rebalance_frequency="monthly")
        with db_session(path) as conn:
            conn.execute("UPDATE prices SET open=80,close=80 WHERE symbol='512890.SH' AND trade_date>='2022-01-10'")
            result = self.run_backtest(conn, config)
            annual = self.annual_payload(conn, result["run_id"])
        self.assertAlmostEqual(annual["year_asset_performance"]["512890.SH"]["profit_cny"], -100000)
        self.assertAlmostEqual(sum(item["profit_cny"] for item in annual["year_asset_performance"].values()), annual["year_profit_cny"])

    def test_annual_pnl_includes_initial_and_later_fees(self):
        path, config = self.annual_fixture(spend=5000, weight=1)
        config["fees"]["cn_etf"]["min_commission_cny"] = 5
        with db_session(path) as conn:
            result = self.run_backtest(conn, config)
            annual = self.annual_payload(conn, result["run_id"])
        self.assertAlmostEqual(sum(item["profit_cny"] for item in annual["year_asset_performance"].values()), annual["year_profit_cny"])
        self.assertAlmostEqual(annual["year_asset_performance"]["512890.SH"]["profit_cny"], -annual["year_fee_cny"])

    def test_annual_dividend_is_profit_despite_cash_transfer(self):
        path, config = self.annual_fixture()
        with db_session(path) as conn:
            conn.execute("INSERT INTO fund_dividends(symbol,ann_date,record_date,ex_date,pay_date,div_cash,currency,source) VALUES ('512890.SH','2022-06-20','2022-06-21','2022-06-22','2022-06-24',1,'CNY','fixture')")
            conn.execute("UPDATE prices SET open=99,close=99 WHERE symbol='512890.SH' AND trade_date>='2022-06-22'")
            result = self.run_backtest(conn, config)
            annual = self.annual_payload(conn, result["run_id"])
        self.assertAlmostEqual(annual["year_asset_performance"]["512890.SH"]["profit_cny"], 0)
        self.assertAlmostEqual(sum(item["profit_cny"] for item in annual["year_asset_performance"].values()), annual["year_profit_cny"])

    def test_cash_adjustment_snapshots_never_double_count_intraday_flows(self):
        ledger = _AssetCashAdjustmentLedger()
        trades = [{"symbol": "X", "side": "BUY", "payload_json": json.dumps({"spent_cny": 105})}]
        ledger.update(trades, "2022-01-03", {"X": 2}, {"X": 1})
        ledger.update(trades, "2022-01-03", {"X": 2}, {"X": 1})
        self.assertEqual(ledger.adjustments["X"], -104)
        baseline = dict(ledger.adjustments)
        trades.append({"symbol": "X", "side": "SELL", "payload_json": json.dumps({"cash_cny": 98})})
        ledger.update(trades, "2022-01-03", {"X": 3}, {"X": 2})
        self.assertEqual(ledger.change_since(baseline)["X"], 98)
        ledger.update(trades, "2022-01-04", {"X": 3}, {})
        self.assertEqual(ledger.change_since(baseline)["X"], 101)


if __name__ == "__main__":
    unittest.main()
