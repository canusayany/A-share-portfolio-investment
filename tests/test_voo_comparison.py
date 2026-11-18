from datetime import date
import json
import unittest

from app.db import db_session
from app.services.backtest_engine import run_backtest
from app.services.calendar import market_business_days
from app.services.voo_comparison import run_voo_comparison
from tests.helpers import build_synced_db


class VooComparisonTests(unittest.TestCase):
    def fixture(self, start="2024-03-04", end="2024-03-08"):
        path, cfg = build_synced_db(start, end)
        cfg.update(initial_capital_cny=70_000, monthly_spend_cny=0)
        cfg["fees"]["fx"].update(bank_in_spread_bps=0, bank_out_spread_bps=0, ibkr_auto_fx_markup=0)
        cfg["fees"]["ibkr_us_etf"].update(fixed_per_share_usd=0, fixed_min_usd=0,
                                         sec_transaction_fee_rate=0, finra_taf_per_share_usd=0)
        with db_session(path) as conn:
            conn.execute("UPDATE fx_rates SET rate=7 WHERE pair='USD/CNY'")
            conn.execute("UPDATE prices SET open=100,close=100 WHERE symbol='VOO'")
            conn.execute("DELETE FROM fund_dividends WHERE symbol='VOO'")
            dates = [d.isoformat() for d in market_business_days(conn, "CN", start, end)]
        return path, cfg, dates

    def test_actual_valuation_fx_and_previous_execution_fixing(self):
        path, cfg, dates = self.fixture()
        with db_session(path) as conn:
            conn.execute("UPDATE fx_rates SET rate=8 WHERE pair='USD/CNY' AND trade_date>=?", (dates[0],))
            points, coverage, _ = run_voo_comparison(conn, cfg, dates, {})
            # The opening budget used yesterday's fixing of 7. The 100 shares
            # are marked in CNY at today's fixing of 8, not a fixed FX rate.
            self.assertAlmostEqual(points[dates[0]]["total_asset_cny"], 80_000, places=4)
            self.assertEqual(coverage["gaps"], [])
            conn.execute("DELETE FROM fx_rates WHERE pair='USD/CNY' AND trade_date<?", (dates[0],))
            conn.execute("UPDATE prices SET close=110 WHERE symbol='VOO' AND trade_date>=?", (dates[1],))
            waiting, _, _ = run_voo_comparison(conn, cfg, dates, {})
            self.assertEqual(waiting[dates[0]]["total_asset_cny"], 70_000)
            self.assertAlmostEqual(waiting[dates[1]]["total_asset_cny"], 70_000, places=4)

    def test_ex_date_entitlement_paid_cash_and_daily_fx_through_cn_holidays(self):
        path, cfg, dates = self.fixture()
        with db_session(path) as conn:
            # The market convenes in the US while CN is closed on Tuesday.
            dates.pop(1)
            for ex, pay in (("2024-03-04", "2024-03-04"), ("2024-03-05", "2024-03-07")):
                conn.execute("INSERT INTO fund_dividends VALUES('VOO',NULL,NULL,?,?,2,'USD','fixture')", (ex, pay))
            conn.execute("UPDATE prices SET open=98,close=98 WHERE symbol='VOO' AND trade_date>='2024-03-05'")
            conn.execute("UPDATE fx_rates SET rate=8 WHERE pair='USD/CNY' AND trade_date>='2024-03-07'")
            points, _, _ = run_voo_comparison(conn, cfg, dates, {})
        self.assertAlmostEqual(points["2024-03-04"]["total_asset_cny"], 70_000, places=4)
        self.assertAlmostEqual(points["2024-03-06"]["total_asset_cny"], (9800 + 140) * 7, places=4)
        for day in ("2024-03-07", "2024-03-08"):
            self.assertAlmostEqual(points[day]["total_asset_cny"], (9800 + 140) * 8, places=4)

    def test_monthly_sale_prefunded_at_us_open_and_unchanged_by_later_close(self):
        path, cfg, dates = self.fixture("2024-03-27", "2024-04-02")
        schedule = {date(2024, 3, 27): 700, date(2024, 4, 1): 7_000}
        with db_session(path) as conn:
            conn.execute("UPDATE trading_calendar SET is_open=0 WHERE market='US' AND trade_date='2024-03-29'")
            first, _, stats = run_voo_comparison(conn, cfg, dates, schedule)
            conn.execute("UPDATE prices SET close=105 WHERE symbol='VOO' AND trade_date='2024-03-28'")
            changed, _, _ = run_voo_comparison(conn, cfg, dates, schedule)
        self.assertAlmostEqual(first["2024-04-01"]["actual_spend_cny"], 7000, places=4)
        self.assertAlmostEqual(first["2024-04-02"]["total_asset_cny"], 62_300, places=4)
        self.assertAlmostEqual(changed["2024-04-01"]["total_asset_cny"], first["2024-04-01"]["total_asset_cny"], places=4)
        self.assertEqual(stats["shortfall_cny"], 0)

    def test_no_repo_or_equity_before_listing_and_graceful_optional_gaps(self):
        path, cfg, dates = self.fixture("2010-09-06", "2010-09-14")
        with db_session(path) as conn:
            conn.execute("UPDATE repo_rates SET close_rate=100")
            conn.execute("DELETE FROM trading_calendar WHERE market='US' AND trade_date<'2010-09-09'")
            conn.execute("DELETE FROM prices WHERE symbol='VOO' AND trade_date='2010-09-13'")
            cfg["monthly_spend_cny"] = 100
            result = run_backtest(conn, cfg, include_month_analysis=False, include_rolling_analysis=False)
            summary = result["summary"]
            daily = {r[0]: json.loads(r[1]) for r in conn.execute(
                "SELECT trade_date,payload_json FROM portfolio_daily WHERE run_id=?", (result["run_id"],))}
        self.assertEqual(daily["2010-09-06"]["voo_comparison"]["total_asset_cny"], 69_900)
        self.assertEqual(daily["2010-09-08"]["voo_comparison"]["total_asset_cny"], 69_900)
        self.assertEqual(summary["data_coverage"]["voo_comparison"]["end_date"], "2010-09-10")
        self.assertIn("prices:VOO", summary["data_coverage"]["voo_comparison"]["gaps"])
        self.assertIsNone(daily["2010-09-13"]["voo_comparison"]["total_asset_cny"])
        self.assertIsNone(summary["voo_comparison_final_asset_cny"])
        self.assertEqual(summary["end_date"], "2010-09-14")

    def test_paid_usd_distribution_funds_next_month_without_selling_shares(self):
        path, cfg, dates = self.fixture("2024-03-25", "2024-04-02")
        with db_session(path) as conn:
            conn.execute("INSERT INTO fund_dividends VALUES('VOO',NULL,NULL,'2024-03-26','2024-03-28',1,'USD','fixture')")
            conn.execute("UPDATE prices SET close=99,open=99 WHERE symbol='VOO' AND trade_date>'2024-03-25'")
            conn.execute("UPDATE prices SET close=110,open=110 WHERE symbol='VOO' AND trade_date>='2024-04-01'")
            points, _, stats = run_voo_comparison(conn, cfg, dates, {date(2024, 4, 1): 350})
        self.assertAlmostEqual(stats["actual_cny"], 350)
        # 100 shares are still held. From the 70 USD net distribution, 50
        # USD were exchanged to fund the withdrawal and 20 USD remain in cash.
        self.assertAlmostEqual(points[dates[-1]]["total_asset_cny"], (100 * 110 + 20) * 7, places=4)

    def test_missing_fx_or_dividend_coverage_stops_only_voo(self):
        for kind in ("fx_rates:USD/CNY", "dividends:VOO", "calendar:US"):
            with self.subTest(kind=kind):
                path, cfg, dates = self.fixture()
                with db_session(path) as conn:
                    if kind.startswith("fx"):
                        conn.execute("DELETE FROM fx_rates WHERE pair='USD/CNY' AND trade_date='2024-03-06'")
                    elif kind.startswith("calendar"):
                        conn.execute("DELETE FROM trading_calendar WHERE market='US' AND trade_date='2024-03-06'")
                    else:
                        conn.execute("UPDATE sync_coverage SET end_date='2024-03-05' WHERE kind='dividends' AND symbol='VOO'")
                    points, covered, _ = run_voo_comparison(conn, cfg, dates, {})
                self.assertEqual(list(points), dates[:2])
                self.assertEqual(covered["gaps"], [kind])

    def test_spending_growth_is_identical_on_all_three_saved_lines(self):
        path, cfg, _ = self.fixture("2023-12-29", "2025-01-03")
        cfg.update(monthly_spend_cny=100, monthly_spend_annual_growth=.1)
        for a in cfg["assets"]:
            a["enabled"] = a["symbol"] == "518880.SH"
            a["target_weight"] = float(a["enabled"])
        with db_session(path) as conn:
            result = run_backtest(conn, cfg, include_month_analysis=False, include_rolling_analysis=False)
            out = result["summary"]
            payloads = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload_json FROM portfolio_daily WHERE run_id=? ORDER BY trade_date", (result["run_id"],))]
        self.assertAlmostEqual(out["total_planned_spend_cny"], 1420)
        self.assertAlmostEqual(out["voo_comparison_spending"]["planned_cny"], 1420)
        self.assertAlmostEqual(out["voo_comparison_spending"]["actual_cny"], 1420, places=4)
        self.assertAlmostEqual(out["total_spend_cny"], 1420)
        self.assertAlmostEqual(out["comparison_final_asset_cny"], out["final_asset_cny"])
        for p in payloads:
            self.assertEqual(p["voo_comparison"]["planned_spend_cny"], p["spending"]["planned_cny"])
        self.assertAlmostEqual(out["voo_comparison_final_asset_cny"], 70_000 - 1420, places=4)

    def test_one_initial_purchase_with_slippage_and_account_depletion(self):
        path, cfg, dates = self.fixture("2024-03-27", "2024-04-02")
        cfg["fees"]["slippage"]["us_bps"] = 100
        with db_session(path) as conn:
            points, _, _ = run_voo_comparison(conn, cfg, dates, {})
            self.assertAlmostEqual(points[dates[-1]]["total_asset_cny"], 70_000 / 1.01, places=4)
            points, _, stats = run_voo_comparison(conn, cfg, dates, {date(2024, 4, 1): 80_000})
        proceeds = 70_000 * .99 / 1.01
        self.assertAlmostEqual(stats["shortfall_cny"], 80_000 - proceeds, places=4)
        self.assertEqual(stats["first_shortfall_date"], "2024-04-01")
        self.assertAlmostEqual(points[dates[-1]]["total_asset_cny"], 0, places=4)
