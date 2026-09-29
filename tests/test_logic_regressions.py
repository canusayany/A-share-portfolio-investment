from datetime import date
import json
from threading import Event, Lock
import unittest
from unittest.mock import patch

from app.config import backtest_assets, get_settings, normalize_config
from app.db import db_session, init_db, insert_many
from app.main import _period_repo_annualized_return, execute_backtest_request, repo_annualized_return_from_daily
from app.services.backtest_engine import (
    BacktestCancelled, BacktestError, PortfolioState, _invest_idle_cash_in_repo,
    _mature_repo_lots, _portfolio_value, get_cached_backtest_run, run_backtest,
)
from app.services.calendar import repo_actual_days, repo_maturity_day
from app.services.data_sync import required_data_missing, sync_all
from app.services.fees import repo_interest
from tests.helpers import seed_fixture_data, temp_db_path


class LogicRegressionTests(unittest.TestCase):
    def fixture(self, start="2020-01-01", end="2020-01-31", symbol="510300.SH"):
        config = normalize_config({
            "start_date": start, "end_date": end, "initial_capital_cny": 100000,
            "monthly_spend_cny": 0, "rebalance_frequency": "weekly",
            "rebalance_band": 0, "rebalance_to_target": True,
            "fees": {
                "cn_etf": {"commission_rate": 0},
                "repo": {"investor_commission_rate": 0},
                "fx": {"bank_in_spread_bps": 0, "bank_out_spread_bps": 0, "ibkr_auto_fx_markup": 0},
                "ibkr_us_etf": {"fixed_per_share_usd": 0, "fixed_min_usd": 0, "sec_transaction_fee_rate": 0, "finra_taf_per_share_usd": 0},
            },
        })
        for asset in config["assets"]:
            asset["enabled"] = asset["symbol"] == symbol
            asset["target_weight"] = 0.5 if asset["enabled"] else 0
        path = temp_db_path()
        init_db(path)
        with db_session(path) as conn:
            seed_fixture_data(conn, config, start, end)
            conn.execute("UPDATE fx_rates SET rate=1")
            conn.execute("UPDATE repo_rates SET open_rate=0,close_rate=0")
            conn.execute("UPDATE prices SET open=100,close=100 WHERE symbol=?", (symbol,))
        return path, config

    def run_backtest(self, conn, config):
        return run_backtest(conn, config, include_comparison=False,
                            include_month_analysis=False, include_rolling_analysis=False)

    def test_repo_uses_settlement_days_and_historical_rule(self):
        # SSE's published 2017 Dragon Boat holiday example.
        sessions = [date(2017, 5, 25), date(2017, 5, 26), date(2017, 5, 31), date(2017, 6, 1)]
        self.assertEqual(repo_maturity_day(sessions[0], 1, sessions), date(2017, 5, 26))
        self.assertEqual(repo_actual_days(sessions[0], 1, sessions), 5)
        self.assertEqual(repo_actual_days(date(2017, 5, 26), 4, sessions), 1)
        self.assertEqual(repo_actual_days(date(2020, 1, 2)), 3)
        self.assertEqual(repo_actual_days(date(2020, 1, 3)), 1)
        self.assertEqual(repo_actual_days(date(2017, 5, 18)), 1)
        self.assertEqual(repo_interest(100000, 3.6, 1, trade_day=date(2017, 5, 18)), 10)
        self.assertEqual(repo_interest(100000, 3.65, 3, trade_day=date(2020, 1, 2)), 30)

    def test_repo_interest_is_fully_earned_when_cash_becomes_available(self):
        config = normalize_config({"fees": {"repo": {"investor_commission_rate": 0}}})
        for day, expected_interest in ((date(2020, 1, 2), 30), (date(2020, 1, 3), 10)):
            with self.subTest(day=day):
                state = PortfolioState(100000)
                _invest_idle_cash_in_repo(state, day, 3.65, config["fees"], 1)
                lot = state.repo_lots[0]
                self.assertEqual(lot.interest, expected_interest)
                self.assertEqual(_portfolio_value(state, {}, {}, day)[0], 100000)
                self.assertEqual(_portfolio_value(state, {}, {}, lot.maturity_date)[0], 100000 + expected_interest)
                _mature_repo_lots(state, lot.maturity_date)
                self.assertEqual(state.cash_cny, 100000 + expected_interest)

    def test_repo_benchmarks_use_same_settlement_day_rule(self):
        path, config = self.fixture("2020-01-02", "2020-01-03")
        with db_session(path) as conn:
            conn.execute("UPDATE repo_rates SET close_rate=10 WHERE trade_date='2020-01-02'")
            conn.execute("UPDATE repo_rates SET close_rate=1 WHERE trade_date='2020-01-03'")
            result = self.run_backtest(conn, config)
            persisted = repo_annualized_return_from_daily(conn, result["run_id"])
            period = _period_repo_annualized_return(conn, config["start_date"], config["end_date"])
        growth = (1 + 0.10 * 3 / 365) * (1 + 0.01 / 365)
        self.assertAlmostEqual(result["summary"]["repo_annualized_return"], growth ** 365.25 - 1)
        self.assertAlmostEqual(persisted, growth ** 365.25 - 1)
        self.assertAlmostEqual(period, growth ** (365.25 / 2) - 1)

    def test_repo_interest_uses_calendar_beyond_requested_backtest_end(self):
        path, config = self.fixture("2020-01-02", "2020-01-03")
        with db_session(path) as conn:
            conn.execute("UPDATE trading_calendar SET is_open=0 WHERE market='CN' AND trade_date='2020-01-06'")
            conn.execute("UPDATE repo_rates SET close_rate=3.65")
            result = self.run_backtest(conn, config)
        # Thursday's first settlement is Friday; its final settlement is Tuesday.
        # Friday's one-day trade settles Tuesday -> Wednesday (one interest day).
        growth = (1 + 0.0365 * 4 / 365) * (1 + 0.0365 / 365)
        self.assertAlmostEqual(result["summary"]["repo_annualized_return"], growth ** 365.25 - 1)

    def test_interior_price_rate_and_fx_gaps_are_detected(self):
        path, config = self.fixture(symbol="VOO")
        with db_session(path) as conn:
            self.assertEqual(required_data_missing(conn, config["start_date"], config["end_date"], backtest_assets(config)), [])
            deleted = conn.execute("DELETE FROM prices WHERE symbol='VOO' AND trade_date BETWEEN '2020-01-06' AND '2020-01-30'").rowcount
            self.assertEqual(deleted, 19)
            conn.execute("DELETE FROM repo_rates WHERE trade_date='2020-01-15'")
            conn.execute("DELETE FROM fx_rates WHERE pair='USD/CNY' AND trade_date='2020-01-16'")
            missing = required_data_missing(conn, config["start_date"], config["end_date"], backtest_assets(config))
        self.assertTrue({"prices:VOO", "repo_rates:204001", "fx_rates:USD/CNY"}.issubset(missing))

    def test_targeted_sync_repairs_interior_price_repo_and_fx_rows(self):
        path, config = self.fixture(symbol="VOO")
        with db_session(path) as conn:
            price = dict(conn.execute("SELECT * FROM prices WHERE symbol='VOO' AND trade_date='2020-01-15'").fetchone())
            rate = dict(conn.execute("SELECT * FROM repo_rates WHERE trade_date='2020-01-15'").fetchone())
            fx = dict(conn.execute("SELECT * FROM fx_rates WHERE pair='USD/CNY' AND trade_date='2020-01-15'").fetchone())
            conn.execute("DELETE FROM prices WHERE symbol='VOO' AND trade_date='2020-01-15'")
            conn.execute("DELETE FROM repo_rates WHERE trade_date='2020-01-15'")
            conn.execute("DELETE FROM fx_rates WHERE pair='USD/CNY' AND trade_date='2020-01-15'")
            with patch("app.services.data_sync.fetch_yahoo_prices", return_value=[price]), \
                 patch("app.services.data_sync.fetch_datasrc_repo_rates", return_value=[rate]), \
                 patch("app.services.data_sync.fetch_datasrc_fx_rates", return_value=[fx]):
                result = sync_all(conn, "", config["start_date"], config["end_date"], backtest_assets(config),
                                  missing_items=["prices:VOO", "repo_rates:204001", "fx_rates:USD/CNY"])
            self.assertEqual(result["missing_data"], [])
            self.assertEqual(required_data_missing(conn, config["start_date"], config["end_date"], backtest_assets(config)), [])

    def test_sync_keeps_both_repo_tenors_on_the_same_date(self):
        path, config = self.fixture()
        with db_session(path) as conn:
            rate = dict(conn.execute("SELECT * FROM repo_rates WHERE trade_date='2020-01-15'").fetchone())
            conn.execute("DELETE FROM repo_rates")

            def rates(symbol, start, end):
                return [{**rate, "symbol": symbol, "trade_date": "2020-01-15"}]

            with patch("app.services.data_sync.fetch_datasrc_repo_rates", side_effect=rates):
                sync_all(conn, "", "2020-01-15", "2020-01-15", backtest_assets(config), "204007",
                         missing_items=["repo_rates:204001", "repo_rates:204007"])
            rows = conn.execute("SELECT symbol FROM repo_rates ORDER BY symbol").fetchall()
        self.assertEqual([row["symbol"] for row in rows], ["204001", "204007"])

    def test_api_repairs_an_interior_gap_before_calculating(self):
        path, config = self.fixture(symbol="VOO")
        with db_session(path) as conn:
            price = dict(conn.execute("SELECT * FROM prices WHERE symbol='VOO' AND trade_date='2020-01-15'").fetchone())
            conn.execute("DELETE FROM prices WHERE symbol='VOO' AND trade_date='2020-01-15'")
        with patch("app.services.data_sync.fetch_yahoo_prices", return_value=[price]) as fetch:
            result = execute_backtest_request(get_settings(path), Lock(), config)
        self.assertTrue(result["data_sync"]["triggered"])
        self.assertIn("prices:VOO", result["data_sync"]["missing_before"])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["summary"]["end_date"], "2020-01-31")

    def test_rebalance_waits_for_real_open_without_losing_valuation(self):
        for mode in ("holiday", "missing_open"):
            with self.subTest(mode=mode):
                path, config = self.fixture("2020-09-01", "2020-09-09", "VOO")
                with db_session(path) as conn:
                    conn.execute("UPDATE prices SET open=200,close=200 WHERE symbol='VOO' AND trade_date>='2020-09-04'")
                    conn.execute("UPDATE prices SET open=180,close=180 WHERE symbol='VOO' AND trade_date>='2020-09-08'")
                    if mode == "holiday":
                        # Even a stray provider quote on a closed day must not trade.
                        conn.execute("UPDATE trading_calendar SET is_open=0 WHERE market='US' AND trade_date='2020-09-07'")
                    else:
                        conn.execute("UPDATE prices SET open=NULL WHERE symbol='VOO' AND trade_date='2020-09-07'")
                    result = self.run_backtest(conn, config)
                    trades = [dict(row) for row in conn.execute("SELECT * FROM trades WHERE run_id=? AND symbol='VOO'", (result["run_id"],))]
                    row = conn.execute("SELECT payload_json FROM portfolio_daily WHERE run_id=? AND trade_date='2020-09-07'", (result["run_id"],)).fetchone()
                self.assertFalse(any(trade["trade_date"] == "2020-09-07" for trade in trades))
                later = [trade for trade in trades if trade["trade_date"] == "2020-09-08"]
                self.assertEqual(len(later), 1)
                self.assertEqual(later[0]["side"], "SELL")
                self.assertEqual(later[0]["price"], 180)
                self.assertEqual(json.loads(row["payload_json"])["values"]["VOO"], 100000)

    def holiday_dividend_fixture(self, start="2020-09-29"):
        path, config = self.fixture(start, "2020-10-12", "VOO")
        config["rebalance_frequency"] = "yearly"
        with db_session(path) as conn:
            conn.execute("UPDATE trading_calendar SET is_open=0 WHERE market='CN' AND trade_date BETWEEN '2020-10-01' AND '2020-10-08'")
            conn.execute("DELETE FROM prices WHERE symbol='000300.SH' AND trade_date BETWEEN '2020-10-01' AND '2020-10-08'")
            insert_many(conn, "fund_dividends", [{
                "symbol": "VOO", "ann_date": "2020-09-20", "record_date": "2020-09-30",
                "ex_date": "2020-10-01", "pay_date": "2020-10-12", "div_cash": 1,
                "currency": "USD", "source": "fixture:review",
            }])
        return path, config

    def test_foreign_dividend_in_cn_holiday_is_booked_once_with_ex_date_fx(self):
        path, config = self.holiday_dividend_fixture()
        with db_session(path) as conn:
            # The ex-date FX is 1; the next CN session's FX must not value this entitlement.
            conn.execute("UPDATE fx_rates SET rate=2 WHERE pair='USD/CNY' AND trade_date>='2020-10-09'")
            result = self.run_backtest(conn, config)
            rows = {row["trade_date"]: json.loads(row["payload_json"]) for row in conn.execute("SELECT * FROM portfolio_daily WHERE run_id=?", (result["run_id"],))}
        self.assertEqual(result["summary"]["total_dividend_cny"], 350)
        self.assertEqual(result["summary"]["withheld_tax_cny"], 150)
        self.assertEqual(rows["2020-10-09"]["dividend_receivable_cny"], 350)
        self.assertEqual(rows["2020-10-12"]["dividend_receivable_cny"], 0)

    def test_skipped_ex_date_does_not_entitle_later_buyers(self):
        path, config = self.holiday_dividend_fixture("2020-10-01")
        with db_session(path) as conn:
            result = self.run_backtest(conn, config)
        self.assertEqual(result["summary"]["total_dividend_cny"], 0)

    def test_valuation_uses_last_foreign_session_inside_cn_holiday(self):
        path, config = self.holiday_dividend_fixture()
        with db_session(path) as conn:
            conn.execute("UPDATE prices SET open=120,close=120 WHERE symbol='VOO' AND trade_date='2020-10-08'")
            conn.execute("DELETE FROM prices WHERE symbol='VOO' AND trade_date='2020-10-09'")
            conn.execute("UPDATE trading_calendar SET is_open=0 WHERE market='US' AND trade_date='2020-10-09'")
            result = self.run_backtest(conn, config)
            row = conn.execute("SELECT payload_json FROM portfolio_daily WHERE run_id=? AND trade_date='2020-10-09'", (result["run_id"],)).fetchone()
        self.assertEqual(json.loads(row["payload_json"])["values"]["VOO"], 60000)

    def test_partial_sync_invalidates_cache_even_if_preflight_fails_or_cancelled(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                path, config = self.fixture()
                with db_session(path) as conn:
                    self.run_backtest(conn, config)
                    self.assertIsNotNone(get_cached_backtest_run(conn, config))
                cancel = Event()

                def partial_sync(conn, *args, **kwargs):
                    conn.execute("UPDATE prices SET close=110 WHERE symbol='510300.SH' AND trade_date='2020-01-31'")
                    if cancelled:
                        cancel.set()
                    return {"inserted": {"prices": 1}, "warnings": ["repo source failed"]}

                with patch("app.main.required_data_missing", return_value=["repo_rates:204001"]), \
                     patch("app.main.sync_all", side_effect=partial_sync), \
                     self.assertRaises(BacktestCancelled if cancelled else BacktestError):
                    execute_backtest_request(get_settings(path), Lock(), {**config, "end_date": "2020-02-03"}, should_cancel=cancel.is_set)
                with db_session(path) as conn:
                    self.assertEqual(conn.execute("SELECT close FROM prices WHERE symbol='510300.SH' AND trade_date='2020-01-31'").fetchone()[0], 110)
                    self.assertIsNone(get_cached_backtest_run(conn, config))

    def test_failed_sync_transaction_preserves_matching_old_data_and_cache(self):
        path, config = self.fixture()
        with db_session(path) as conn:
            before = self.run_backtest(conn, config)

        def failed_sync(conn, *args, **kwargs):
            conn.execute("UPDATE prices SET close=110 WHERE symbol='510300.SH' AND trade_date='2020-01-31'")
            raise RuntimeError("source crashed")

        with patch("app.main.required_data_missing", return_value=["repo_rates:204001"]), \
             patch("app.main.sync_all", side_effect=failed_sync), self.assertRaises(RuntimeError):
            execute_backtest_request(get_settings(path), Lock(), {**config, "end_date": "2020-02-03"})
        with db_session(path) as conn:
            self.assertEqual(conn.execute("SELECT close FROM prices WHERE symbol='510300.SH' AND trade_date='2020-01-31'").fetchone()[0], 100)
            self.assertEqual(get_cached_backtest_run(conn, config)["run_id"], before["run_id"])


if __name__ == "__main__":
    unittest.main()
