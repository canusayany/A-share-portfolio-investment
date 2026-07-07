from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import tempfile
import unittest

from app.config import default_config, load_dotenv_if_present, normalize_config, validate_config
from app.services.calendar import business_days, first_business_day_by_month, rebalance_days, repo_actual_days
from app.services.fees import (
    CnEtfFeeConfig,
    FxFeeConfig,
    HkConnectEtfFeeConfig,
    IbkrFeeConfig,
    RepoFeeConfig,
    bps_to_rate,
    cn_etf_fee,
    cny_cost_for_hkd,
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
        cny, in_fee = usd_to_cny(usd, 7, cfg, include_wire=False)
        self.assertLess(cny, 7000)
        self.assertGreater(out_fee, 0)
        self.assertGreater(in_fee, 0)
        self.assertEqual(bps_to_rate(30), 0.003)

    def test_repo_interest_and_fee(self) -> None:
        self.assertAlmostEqual(repo_interest(100000, 1.825, 2), 10)
        self.assertEqual(repo_fee(100000, RepoFeeConfig(investor_commission_rate=0.00001, fee_cap_cny=30)), 1)

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
        self.assertGreater(hk_connect_portfolio_fee(100000, cfg), 0)
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
        self.assertIn(date(2020, 7, 1), rebalance_days(days, "semiannual"))
        self.assertIn(date(2020, 1, 1), rebalance_days(days, "yearly"))
        self.assertEqual(repo_actual_days(date(2020, 1, 3)), 3)

    def test_config_merge_and_validation(self) -> None:
        cfg = normalize_config({"initial_capital_cny": 500000, "fees": {"cn_etf": {"commission_rate": 0.00005}}})
        self.assertEqual(cfg["initial_capital_cny"], 500000)
        self.assertEqual(cfg["fees"]["cn_etf"]["commission_rate"], 0.00005)
        hk_asset = next(asset for asset in cfg["assets"] if asset["symbol"] == "03195.HK")
        self.assertEqual(hk_asset["expense_ratio"], 0.0079)
        self.assertEqual(cfg["fees"]["tax"]["us_dividend_withholding_rate"], 0.30)
        self.assertEqual(cfg["fees"]["hk_connect_etf"]["stock_settlement_fee_rate"], 0.000042)
        self.assertEqual(validate_config(cfg), [])
        bad = default_config()
        bad["assets"] = [{**bad["assets"][0], "target_weight": 1.2}]
        self.assertTrue(validate_config(bad))
        fixed_bucket = normalize_config({"repo_target_mode": "fixed_bucket"})
        fixed_bucket["assets"] = [{**fixed_bucket["assets"][0], "target_weight": 1.2}]
        self.assertFalse(any("target weights cannot exceed" in item for item in validate_config(fixed_bucket)))
        bad_fixed_bucket = normalize_config({"repo_target_mode": "fixed_bucket", "repo_fixed_target_ratio": 1.2})
        self.assertTrue(any("repo_fixed_target_ratio" in item for item in validate_config(bad_fixed_bucket)))
        duplicate_sp500 = normalize_config({})
        next(asset for asset in duplicate_sp500["assets"] if asset["symbol"] == "03195.HK")["enabled"] = True
        self.assertTrue(any("exclusive asset group sp500" in item for item in validate_config(duplicate_sp500)))

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
