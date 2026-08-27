from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import tempfile
import unittest

from app.config import (
    backtest_assets,
    default_config,
    load_dotenv_if_present,
    normalize_config,
    repo_rate_symbol,
    selected_bond_etf_asset,
    selected_money_fund_asset,
    selected_repo_option,
    validate_config,
)
from app.services.calendar import business_days, first_business_day_by_month, rebalance_days, repo_actual_days, repo_maturity_day
from app.services.fees import (
    CnEtfFeeConfig,
    FxFeeConfig,
    HkConnectEtfFeeConfig,
    IbkrFeeConfig,
    RepoFeeConfig,
    bps_to_rate,
    cn_etf_fee,
    cny_cost_for_hkd,
    cny_cost_for_usd,
    cny_to_usd,
    hk_connect_etf_trade_fee,
    hk_connect_portfolio_fee,
    hkd_to_cny,
    ibkr_us_etf_fee,
    ibkr_us_etf_sell_fee,
    repo_fee,
    repo_interest,
    usd_to_cny,
)


class FeeTests(unittest.TestCase):
    def test_cn_etf_fee_uses_wan_rate_and_optional_handling(self) -> None:
        fee = cn_etf_fee(100000, CnEtfFeeConfig(commission_rate=0.00025, include_exchange_in_commission=True))
        self.assertAlmostEqual(fee, 25)
        fee_with_handling = cn_etf_fee(100000, CnEtfFeeConfig(commission_rate=0.00025, include_exchange_in_commission=False))
        self.assertAlmostEqual(fee_with_handling, 29)

    def test_ibkr_fixed_tiered_and_lite(self) -> None:
        self.assertEqual(ibkr_us_etf_fee(10, 5000, IbkrFeeConfig(plan="lite")), 0)
        self.assertEqual(ibkr_us_etf_fee(10, 5000, IbkrFeeConfig(plan="pro_fixed")), 1)
        self.assertEqual(ibkr_us_etf_fee(1000, 100000, IbkrFeeConfig(plan="pro_fixed")), 5)
        self.assertEqual(ibkr_us_etf_fee(10, 5000, IbkrFeeConfig(plan="pro_tiered")), 0.35)

    def test_ibkr_sell_fee_includes_us_regulatory_charges(self) -> None:
        cfg = IbkrFeeConfig(plan="pro_fixed", sec_transaction_fee_rate=0.0000206, finra_taf_per_share_usd=0.000195, finra_taf_cap_usd=9.79)
        self.assertAlmostEqual(ibkr_us_etf_sell_fee(10, 5000, cfg), 1 + 0.103 + 0.00195)

    def test_fx_round_trip_fees_are_positive(self) -> None:
        cfg = FxFeeConfig(bank_out_spread_bps=30, bank_in_spread_bps=30, outbound_wire_fee_cny=0)
        usd, out_fee = cny_to_usd(7000, 7, cfg, include_wire=False)
        usd_cost, sized_out_fee = cny_cost_for_usd(500, 7, cfg)
        cny, in_fee = usd_to_cny(usd, 7, cfg, include_wire=False)
        self.assertLess(cny, 7000)
        self.assertGreater(out_fee, 0)
        self.assertAlmostEqual(usd_cost, 3500 + sized_out_fee)
        self.assertLess(sized_out_fee, out_fee)
        self.assertGreater(in_fee, 0)
        self.assertEqual(bps_to_rate(30), 0.003)

    def test_repo_interest_and_fee(self) -> None:
        self.assertAlmostEqual(repo_interest(100000, 1.825, 2), 10)
        self.assertEqual(repo_fee(100000, RepoFeeConfig(investor_commission_rate=0.00001, fee_cap_cny=30)), 1)
        self.assertEqual(repo_fee(1_000_000, RepoFeeConfig(investor_commission_rate=0.00030, fee_cap_cny=0)), 300)

    def test_hk_connect_etf_fees_include_connect_charges_and_fx_spread(self) -> None:
        cfg = HkConnectEtfFeeConfig(
            broker_commission_rate=0.0003,
            trading_fee_rate=0.0000565,
            transaction_levy_rate=0.000027,
            afrc_transaction_levy_rate=0.0000015,
            stock_settlement_fee_rate=0.000042,
            min_stock_settlement_fee_hkd=0,
            max_stock_settlement_fee_hkd=1_000_000_000,
            stamp_duty_rate=0,
            portfolio_fee_annual_rate=0.00008,
            fx_spread_bps=20,
        )
        fee = hk_connect_etf_trade_fee(100000, cfg)
        self.assertAlmostEqual(fee, 42.7)
        self.assertEqual(hk_connect_etf_trade_fee(1000, cfg), 0.43)
        self.assertGreater(hk_connect_portfolio_fee(100000, cfg), 0)
        self.assertAlmostEqual(hk_connect_portfolio_fee(100000, cfg, 3), hk_connect_portfolio_fee(100000, cfg) * 3, places=5)
        cny_cost, buy_fx_fee = cny_cost_for_hkd(10000, 0.9, cfg)
        cny_cash, sell_fx_fee = hkd_to_cny(10000, 0.9, cfg)
        self.assertGreater(cny_cost, 9000)
        self.assertLess(cny_cash, 9000)
        self.assertGreater(buy_fx_fee, 0)
        self.assertGreater(sell_fx_fee, 0)


class CalendarAndConfigTests(unittest.TestCase):
    def test_business_rebalance_and_spend_days(self) -> None:
        days = business_days("2020-01-01", "2020-07-10")
        self.assertNotIn(date(2020, 1, 4), days)
        self.assertIn(date(2020, 1, 1), first_business_day_by_month(days))
        self.assertIn(date(2020, 1, 6), rebalance_days(days, "weekly"))
        self.assertIn(date(2020, 2, 3), rebalance_days(days, "monthly"))
        self.assertIn(date(2020, 4, 1), rebalance_days(days, "quarterly"))
        self.assertIn(date(2020, 7, 1), rebalance_days(days, "semiannual"))
        self.assertIn(date(2020, 1, 1), rebalance_days(days, "yearly"))
        self.assertEqual(rebalance_days(days, "yearly", 5), {date(2020, 1, 1), date(2020, 5, 1)})
        self.assertEqual(rebalance_days(days, "daily"), set(days))
        self.assertEqual(repo_actual_days(date(2020, 1, 3)), 3)
        self.assertEqual(repo_maturity_day(date(2026, 7, 10), 7), date(2026, 7, 17))
        self.assertEqual(repo_actual_days(date(2026, 7, 10), 7), 7)

    def test_config_merge_and_validation(self) -> None:
        cfg = normalize_config({"initial_capital_cny": 500000, "fees": {"cn_etf": {"commission_rate": 0.00005}}})
        self.assertEqual(cfg["initial_capital_cny"], 500000)
        self.assertEqual(cfg["fees"]["cn_etf"]["commission_rate"], 0.00005)
        self.assertEqual(cfg["fees"]["repo"]["fee_cap_cny"], 0.0)
        self.assertEqual(
            next(option for option in cfg["repo_options"] if option["symbol"] == "204091")["commission_rate"],
            0.00030,
        )
        hk_asset = next(asset for asset in cfg["assets"] if asset["symbol"] == "03195.HK")
        self.assertEqual(hk_asset["expense_ratio"], 0.0079)
        cn_sp500 = next(asset for asset in cfg["assets"] if asset["symbol"] == "513500.SH")
        self.assertEqual(cn_sp500["exclusive_group"], "sp500")
        self.assertEqual(cn_sp500["inception_date"], "2013-12-05")
        self.assertEqual(cn_sp500["management_fee"], 0.006)
        self.assertEqual(cn_sp500["custodian_fee"], 0.002)
        dividend_low_vol = next(asset for asset in cfg["assets"] if asset["symbol"] == "512890.SH")
        self.assertEqual(dividend_low_vol["trade_start_date"], "2019-01-18")
        self.assertEqual(dividend_low_vol["price_fallback"]["symbol"], "H20269.CSI")
        self.assertEqual(dividend_low_vol["price_fallback"]["start_date"], "2005-12-30")
        self.assertEqual(dividend_low_vol["price_fallback"]["scale_mode"], "splice")
        self.assertEqual(dividend_low_vol["price_fallback"]["annual_expense_drag_rate"], 0.0063)
        self.assertTrue(dividend_low_vol["allow_adj_factor_tail_carry_forward"])
        self.assertEqual(dividend_low_vol["share_splits"][0]["effective_date"], "2021-10-25")
        self.assertEqual(dividend_low_vol["share_splits"][0]["price_multiplier"], 2.0)
        a100 = next(asset for asset in cfg["assets"] if asset["symbol"] == "159631.SZ")
        self.assertFalse(a100["enabled"])
        self.assertEqual(a100["exclusive_group"], "cn_broad_etf")
        self.assertEqual(a100["inception_date"], "2022-08-18")
        self.assertEqual(a100["price_fallback"]["kind"], "index")
        self.assertEqual(a100["price_fallback"]["symbol"], "000903.SH")
        self.assertEqual(a100["price_fallback"]["start_date"], "2005-12-30")
        self.assertEqual(a100["price_fallback"]["scale_mode"], "splice")
        self.assertFalse(a100["price_fallback"]["required"])
        for symbol, fallback_symbol, inception, trade_start in (
            ("510500.SH", "000905.SH", "2013-02-06", "2013-03-15"),
            ("512100.SH", "000852.SH", "2016-09-29", "2016-11-04"),
        ):
            asset = next(item for item in cfg["assets"] if item["symbol"] == symbol)
            self.assertFalse(asset["enabled"])
            self.assertEqual(asset["exclusive_group"], "cn_broad_etf")
            self.assertEqual(asset["inception_date"], inception)
            self.assertEqual(asset["trade_start_date"], trade_start)
            self.assertEqual(asset["price_fallback"]["kind"], "index")
            self.assertEqual(asset["price_fallback"]["symbol"], fallback_symbol)
            self.assertEqual(asset["price_fallback"]["start_date"], "2004-12-31")
        self.assertEqual(cfg["fees"]["tax"]["us_dividend_withholding_rate"], 0.30)
        self.assertEqual(cfg["fees"]["hk_connect_etf"]["stock_settlement_fee_rate"], 0.000042)
        self.assertEqual(cfg["annual_rebalance_month"], 1)
        self.assertEqual(cfg["rolling_window_years"], 3)
        self.assertFalse(cfg["rebalance_month_analysis_enabled"])
        self.assertEqual(cfg["dip_buy_drawdown"], 0.05)
        self.assertEqual(cfg["dip_buy_total_parts"], 10)
        self.assertEqual(cfg["dip_buy_parts_per_trigger"], 1)
        self.assertEqual(cfg["dip_buy_cooldown_trading_days"], 10)
        self.assertTrue(cfg["dip_buy_blackout_enabled"])
        self.assertEqual(cfg["dip_buy_blackout_months"], 1)
        enabled_allocations = {
            asset["symbol"]: asset["target_weight"]
            for asset in cfg["assets"]
            if asset["enabled"]
        }
        self.assertEqual(
            enabled_allocations,
            {"512890.SH": 0.25, "CBA21801": 0.25, "518880.SH": 0.25},
        )

        invalid = normalize_config(
            {
                "annual_rebalance_month": 13,
                "rolling_window_years": 0,
                "rebalance_month_analysis_enabled": "yes",
            }
        )
        errors = validate_config(invalid)
        self.assertTrue(any("annual_rebalance_month" in error for error in errors))
        self.assertTrue(any("rolling_window_years" in error for error in errors))
        self.assertTrue(any("rebalance_month_analysis_enabled" in error for error in errors))
        self.assertEqual(validate_config(cfg), [])
        bad = default_config()
        bad["assets"] = [{**bad["assets"][0], "enabled": True, "target_weight": 1.2}]
        self.assertTrue(validate_config(bad))
        fixed_bucket = normalize_config({"repo_target_mode": "fixed_bucket"})
        fixed_bucket["assets"] = [{**fixed_bucket["assets"][0], "target_weight": 1.2}]
        self.assertFalse(any("target weights cannot exceed" in item for item in validate_config(fixed_bucket)))
        bad_fixed_bucket = normalize_config({"repo_target_mode": "fixed_bucket", "repo_fixed_target_ratio": 1.2})
        self.assertTrue(any("repo_fixed_target_ratio" in item for item in validate_config(bad_fixed_bucket)))
        self.assertTrue(any("dip_buy_drawdown" in item for item in validate_config(normalize_config({"dip_buy_drawdown": 1.0}))))
        self.assertTrue(any("dip_buy_total_parts" in item for item in validate_config(normalize_config({"dip_buy_total_parts": 0}))))
        self.assertTrue(
            any(
                "dip_buy_parts_per_trigger" in item
                for item in validate_config(normalize_config({"dip_buy_parts_per_trigger": 0}))
            )
        )
        self.assertTrue(
            any(
                "dip_buy_parts_per_trigger" in item
                for item in validate_config(normalize_config({"dip_buy_total_parts": 2, "dip_buy_parts_per_trigger": 3}))
            )
        )
        self.assertTrue(
            any(
                "dip buy part parameters" in item
                for item in validate_config(normalize_config({"dip_buy_total_parts": "ten"}))
            )
        )
        self.assertTrue(
            any(
                "dip_buy_cooldown_trading_days" in item
                for item in validate_config(normalize_config({"dip_buy_cooldown_trading_days": -1}))
            )
        )
        self.assertTrue(
            any(
                "dip_buy_blackout_months" in item
                for item in validate_config(normalize_config({"dip_buy_blackout_months": 12}))
            )
        )
        duplicate_sp500 = normalize_config({})
        next(asset for asset in duplicate_sp500["assets"] if asset["symbol"] == "VOO")["enabled"] = True
        next(asset for asset in duplicate_sp500["assets"] if asset["symbol"] == "03195.HK")["enabled"] = True
        self.assertTrue(any("exclusive asset group sp500" in item for item in validate_config(duplicate_sp500)))
        duplicate_broad = normalize_config({})
        next(asset for asset in duplicate_broad["assets"] if asset["symbol"] == "159631.SZ")["enabled"] = True
        next(asset for asset in duplicate_broad["assets"] if asset["symbol"] == "510500.SH")["enabled"] = True
        self.assertTrue(any("exclusive asset group cn_broad_etf" in item for item in validate_config(duplicate_broad)))

    def test_blank_date_inputs_keep_default_dates(self) -> None:
        defaults = default_config()
        cfg = normalize_config({"start_date": "", "end_date": ""})

        self.assertEqual(cfg["start_date"], defaults["start_date"])
        self.assertEqual(cfg["end_date"], defaults["end_date"])

    def test_asset_selection_does_not_allow_stale_client_metadata_to_override_server_rules(self) -> None:
        stale_gold = next(asset for asset in default_config()["assets"] if asset["symbol"] == "518880.SH")
        stale_gold["enabled"] = False
        stale_gold["target_weight"] = 0.25
        stale_gold["price_fallback"].pop("required")
        stale_gold.pop("replacement_assets")

        cfg = normalize_config({"assets": [stale_gold]})
        gold = next(asset for asset in cfg["assets"] if asset["symbol"] == "518880.SH")

        self.assertFalse(gold["enabled"])
        self.assertEqual(gold["target_weight"], 0.25)
        self.assertTrue(gold["price_fallback"]["required"])
        self.assertTrue(gold["replacement_assets"])
        self.assertEqual(validate_config(cfg), [])

    def test_repo_selection_does_not_allow_stale_client_metadata_to_override_server_rules(self) -> None:
        stale_options = default_config()["repo_options"]
        next(item for item in stale_options if item["symbol"] == "204007")["tenor_days"] = 1

        cfg = normalize_config({"repo_symbol": "204007", "repo_options": stale_options})

        self.assertEqual(selected_repo_option(cfg)["tenor_days"], 7)
        self.assertEqual(repo_rate_symbol(cfg), "204007")

    def test_validation_rejects_malformed_and_non_finite_fee_inputs(self) -> None:
        malformed_assets = normalize_config({"assets": "not-a-list"})
        self.assertTrue(any("assets must be a list" in item for item in validate_config(malformed_assets)))

        negative_fee = normalize_config({"fees": {"cn_etf": {"commission_rate": -0.01}}})
        self.assertTrue(any("fees.cn_etf.commission_rate" in item for item in validate_config(negative_fee)))

        non_finite_fixed_cash = normalize_config({"repo_fixed_target_cny": float("nan")})
        self.assertTrue(any("repo_fixed_target_cny" in item for item in validate_config(non_finite_fixed_cash)))

    def test_money_fund_selection_uses_one_day_repo_as_rate_fallback(self) -> None:
        cfg = normalize_config({"repo_symbol": "511990.SH"})
        money_fund = selected_money_fund_asset(cfg)
        self.assertIsNotNone(money_fund)
        self.assertEqual(money_fund["inception_date"], "2012-12-27")
        self.assertEqual(money_fund["trade_start_date"], "2013-01-28")
        self.assertEqual(money_fund["asset_type"], "money_fund")
        self.assertEqual(repo_rate_symbol(cfg), "204001")
        self.assertIn("511990.SH", {asset["symbol"] for asset in backtest_assets(cfg)})
        self.assertEqual(validate_config(cfg), [])

    def test_legacy_bond_etf_selector_is_disabled(self) -> None:
        self.assertIsNone(selected_bond_etf_asset(normalize_config({})))

    def test_validation_rejects_negative_financial_inputs(self) -> None:
        self.assertTrue(any("monthly_spend_cny" in item for item in validate_config(normalize_config({"monthly_spend_cny": -1}))))
        negative_weight = normalize_config({})
        negative_weight["assets"][0]["target_weight"] = -0.01
        self.assertTrue(any("target_weight" in item for item in validate_config(negative_weight)))
        self.assertTrue(any("rebalance_band" in item for item in validate_config(normalize_config({"rebalance_band": 1.1}))))

    def test_treasury_indices_can_be_combined(self) -> None:
        cfg = normalize_config({})
        treasuries = [asset for asset in cfg["assets"] if asset.get("asset_type") == "cn_bond_index"]
        self.assertEqual({asset["symbol"] for asset in treasuries}, {"CBA03101", "CBA06501", "CBA21801"})
        self.assertEqual(next(asset for asset in treasuries if asset["symbol"] == "CBA03101")["inception_date"], "2008-01-02")
        thirty_year = next(asset for asset in treasuries if asset["symbol"] == "CBA21801")
        self.assertEqual(thirty_year["price_fallback"]["kind"], "chinabond_30y_yield_total_return")
        self.assertEqual(thirty_year["price_fallback"]["symbol"], "CN30Y.YIELD-TR")
        self.assertEqual(thirty_year["price_fallback"]["start_date"], "2006-03-01")
        for asset in treasuries:
            asset["enabled"] = True
            asset["target_weight"] = 0.1
        self.assertEqual(validate_config(cfg), [])

    def test_backtest_assets_include_low_fee_gold_replacement(self) -> None:
        cfg = normalize_config({})
        assets = {asset["symbol"]: asset for asset in backtest_assets(cfg)}
        self.assertIn("518850.SH", assets)
        self.assertEqual(assets["518850.SH"]["replacement_for"], "518880.SH")
        self.assertEqual(assets["518850.SH"]["allocation_start_date"], "2021-01-01")

    def test_default_end_date_uses_current_day(self) -> None:
        self.assertEqual(default_config()["end_date"], date.today().isoformat())

    def test_dotenv_loader_sets_missing_values_only(self) -> None:
        path = Path(tempfile.mkdtemp(prefix="dotenv_test_")) / ".env"
        path.write_text("A_TOKEN=abc\nEXISTING_TOKEN=file\n", encoding="utf-8")
        os.environ.pop("A_TOKEN", None)
        os.environ["EXISTING_TOKEN"] = "env"
        load_dotenv_if_present(path)
        self.assertEqual(os.environ["A_TOKEN"], "abc")
        self.assertEqual(os.environ["EXISTING_TOKEN"], "env")


if __name__ == "__main__":
    unittest.main()
