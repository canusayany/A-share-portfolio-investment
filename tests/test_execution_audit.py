from datetime import date
import json
import unittest

from app.config import normalize_config
from app.db import db_session, insert_many
from app.services.backtest_engine import (
    PortfolioState, Position, _apply_dividend_events, _buy_position, _sell_position,
    _invest_idle_cash_in_repo, run_backtest,
)
from tests.helpers import build_synced_db, fixture_repo_rates


def run(conn, config):
    return run_backtest(conn, config, include_comparison=False,
                        include_month_analysis=False, include_rolling_analysis=False)


def allocate(config, weights):
    for asset in config["assets"]:
        asset["target_weight"] = weights.get(asset["symbol"], 0)
        asset["enabled"] = asset["target_weight"] > 0


class ExecutionAuditTests(unittest.TestCase):
    def test_evening_us_open_cannot_change_domestic_execution_or_repo_funding(self):
        day = "2024-03-05"
        scenarios = []
        for us_open in (100, 110):
            path, cfg = build_synced_db("2024-03-04", "2024-03-06")
            cfg.update(monthly_spend_cny=0, rebalance_frequency="daily",
                       rebalance_to_target=True, rebalance_band=.01)
            cfg["fees"]["fx"].update(bank_in_spread_bps=0, bank_out_spread_bps=0, ibkr_auto_fx_markup=0)
            cfg["fees"]["cn_etf"]["commission_rate"] = 0
            cfg["fees"]["ibkr_us_etf"].update(fixed_per_share_usd=0, fixed_min_usd=0)
            allocate(cfg, {"VOO": .5, "512890.SH": .5})
            with db_session(path) as conn:
                conn.execute("UPDATE prices SET open=10,close=10 WHERE symbol='512890.SH'")
                conn.execute("UPDATE prices SET open=100,close=100 WHERE symbol='VOO'")
                conn.execute("UPDATE prices SET open=? WHERE symbol='VOO' AND trade_date=?", (us_open, day))
                conn.execute("UPDATE fx_rates SET rate=7 WHERE pair='USD/CNY'")
                run_id = run(conn, cfg)["run_id"]
                trades = conn.execute("SELECT symbol,side,quantity FROM trades WHERE run_id=? AND trade_date=?",
                                      (run_id, day)).fetchall()
                daily = conn.execute("SELECT payload_json FROM portfolio_daily WHERE run_id=? AND trade_date=?",
                                     (run_id, day)).fetchone()
                scenarios.append(([tuple(r) for r in trades if r["symbol"] != "VOO"],
                                  [r["side"] for r in trades if r["symbol"] == "VOO"],
                                  json.loads(daily[0])))
        self.assertEqual(scenarios[0][0], scenarios[1][0])
        self.assertEqual(scenarios[0][1], [])
        self.assertEqual(scenarios[1][1], ["SELL"])
        self.assertEqual(scenarios[0][2]["repo_lots"], scenarios[1][2]["repo_lots"])
        self.assertGreater(scenarios[1][2]["cash_cny"], scenarios[0][2]["cash_cny"] + 20_000)

    def test_us_pay_date_cash_is_unavailable_in_asia_and_at_us_open(self):
        cfg = normalize_config({})
        day = "2024-03-05"
        dividend = {"symbol": "VOO", "currency": "USD", "pay_date": day, "div_cash": 1}
        state = PortfolioState(0, {"VOO": Position("VOO", "US", "USD", "us_etf", quantity=100)})
        fx = {"USD/CNY": 7}
        _apply_dividend_events(state, day, {day: [dividend]}, {}, fx, cfg["fees"], asian_hours=True)
        self.assertEqual(state.cash_cny, 0)
        self.assertAlmostEqual(state.dividend_receivable_cny, 490)
        _apply_dividend_events(state, day, {}, {}, fx, cfg["fees"])
        self.assertAlmostEqual(state.cash_cny, 490)
        self.assertEqual(state.dividend_receivable_cny, 0)

    def test_monthly_domestic_purchase_waits_for_us_sale_proceeds(self):
        path, cfg = build_synced_db("2024-03-28", "2024-04-02")
        cfg.update(monthly_spend_cny=0, rebalance_frequency="monthly",
                   rebalance_to_target=True, rebalance_band=.01)
        allocate(cfg, {"VOO": .5, "512890.SH": .5})
        with db_session(path) as conn:
            conn.execute("UPDATE fx_rates SET rate=7 WHERE pair='USD/CNY'")
            conn.execute("UPDATE prices SET open=10,close=10 WHERE symbol='512890.SH'")
            conn.execute("UPDATE prices SET open=100,close=100 WHERE symbol='VOO'")
            conn.execute("UPDATE prices SET open=110,close=110 WHERE symbol='VOO' AND trade_date>='2024-03-29'")
            rid = run(conn, cfg)["run_id"]
            tx = list(conn.execute("SELECT trade_date,symbol,side,reason FROM trades WHERE run_id=? AND trade_date>='2024-04-01'", (rid,)))
        self.assertEqual([tuple(r) for r in tx], [
            ("2024-04-01", "VOO", "SELL", "rebalance"),
            ("2024-04-02", "512890.SH", "BUY", "rebalance_deferred"),
        ])

    def test_monthly_cash_shortfall_cannot_sell_new_domestic_equity_units(self):
        path, cfg = build_synced_db("2024-03-01", "2024-04-02")
        cfg.update(initial_capital_cny=110_000, monthly_spend_cny=50_000,
                   rebalance_frequency="monthly", rebalance_to_target=True, rebalance_band=.02)
        cfg["fees"]["cn_etf"]["commission_rate"] = cfg["fees"]["repo"]["investor_commission_rate"] = 0
        allocate(cfg, {"512890.SH": .5})
        with db_session(path) as conn:
            conn.execute("UPDATE repo_rates SET open_rate=0,close_rate=0")
            dates = list(conn.execute("SELECT trade_date FROM prices WHERE symbol='512890.SH' ORDER BY trade_date"))
            for i, (day,) in enumerate(dates):
                price = 10 - 3.5 * min(i, 20) / 20
                conn.execute("UPDATE prices SET open=?,close=? WHERE symbol='512890.SH' AND trade_date=?",
                             (price, price, day))
            result = run(conn, cfg)
            trades = list(conn.execute("SELECT side,quantity FROM trades WHERE trade_date='2024-04-01' ORDER BY rowid"))
        self.assertEqual([tuple(r) for r in trades], [("BUY", 900), ("SELL", 3000)])
        self.assertEqual(result["summary"]["total_spend_shortfall_cny"], 6350)

    def test_domestic_cross_border_bond_and_gold_allow_same_day_sales(self):
        fees = normalize_config({})["fees"]
        for symbol in ("513500.SH", "511090.SH", "518880.SH"):
            with self.subTest(symbol=symbol):
                position = Position(symbol, "CN", "CNY", "cn_etf")
                state, trades = PortfolioState(20_000, {symbol: position}), []
                day = date(2024, 1, 2)
                _buy_position(state, position, day, 10_000, 10, {}, fees, trades, False, "buy")
                _sell_position(state, position, day, position.quantity, 10, {}, fees, trades, "sell")
                self.assertEqual([t["side"] for t in trades], ["BUY", "SELL"])

    def test_region_slippage_is_adverse_in_price_and_disclosed_once(self):
        fees = normalize_config({})["fees"]
        rates = {"USD/CNY": 7.0, "HKD/CNY": .9}
        for market, currency, symbol in (("CN", "CNY", "512890.SH"), ("US", "USD", "VOO"), ("HK", "HKD", "03195.HK")):
            with self.subTest(market=market):
                pos = Position(symbol, market, currency, "etf")
                state, trades = PortfolioState(1_000_000, {symbol: pos}), []
                _buy_position(state, pos, date(2024, 1, 2), 500_000, 10, rates, fees, trades, True, "buy")
                _sell_position(state, pos, date(2024, 1, 3), pos.quantity, 10, rates, fees, trades, "sell")
                slip = fees["slippage"][market.lower() + "_bps"] / 10_000
                self.assertAlmostEqual(trades[0]["price"], 10 * (1 + slip))
                self.assertAlmostEqual(trades[1]["price"], 10 * (1 - slip))
                self.assertAlmostEqual(sum(json.loads(t["payload_json"])["slippage_cny"] for t in trades), state.total_slippage_cny)
                self.assertAlmostEqual(1_000_000 - state.cash_cny, state.total_fees_cny + state.total_slippage_cny, places=4)

    def test_repo_minimum_changes_on_exchange_effective_date(self):
        fees = normalize_config({})["fees"]
        for day, expected in ((date(2022, 5, 13), 200_000), (date(2022, 5, 16), 249_000)):
            state = PortfolioState(249_900)
            _invest_idle_cash_in_repo(state, day, 2.0, fees, 1)
            self.assertEqual(state.repo_lots[0].principal, expected)

    def test_outstanding_long_repo_and_cash_benchmark_ignore_final_long_quote(self):
        results = []
        for final_rate in (2.0, 6.0):
            path, cfg = build_synced_db("2024-03-04", "2024-03-08")
            allocate(cfg, {})
            cfg.update(repo_symbol="204007", monthly_spend_cny=0)
            cfg["fees"]["repo"]["investor_commission_rate"] = 0
            with db_session(path) as conn:
                insert_many(conn, "repo_rates", fixture_repo_rates(cfg["start_date"], cfg["end_date"], "204007"))
                conn.execute("UPDATE repo_rates SET close_rate=2")
                conn.execute("UPDATE repo_rates SET close_rate=? WHERE symbol='204007' AND trade_date=?",
                             (final_rate, cfg["end_date"]))
                summary = run(conn, cfg)["summary"]
                results.append((summary["final_asset_cny"], summary["repo_annualized_return"]))
        self.assertEqual(results[0], results[1])
        self.assertAlmostEqual(results[0][0], 1_000_219.17714, places=4)
