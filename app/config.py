from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import os
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "app" / "static"
DEFAULT_DB_PATH = DATA_DIR / "backtest.sqlite3"
FX_PAIR_BY_CURRENCY = {
    "USD": "USD/CNY",
    "HKD": "HKD/CNY",
}


DEFAULT_ASSETS: list[dict[str, Any]] = [
    {
        "key": "us_sp500",
        "symbol": "VOO",
        "name": "标普500指数基金",
        "target_weight": 0.20,
        "enabled": True,
        "currency": "USD",
        "market": "US",
        "asset_type": "us_etf",
        "exclusive_group": "sp500",
        "choice_label": "美股 VOO",
        "inception_date": "2010-09-07",
        "expense_ratio": 0.0003,
    },
    {
        "key": "hk_sp500_connect",
        "symbol": "03195.HK",
        "name": "港股通标普500ETF",
        "target_weight": 0.0,
        "enabled": False,
        "currency": "HKD",
        "market": "HK",
        "asset_type": "hk_connect_etf",
        "exclusive_group": "sp500",
        "choice_label": "港股通 03195",
        "inception_date": "2015-05-18",
        "expense_ratio": 0.0079,
    },
    {
        "key": "cn_sp500_etf",
        "symbol": "513500.SH",
        "name": "标普500ETF博时",
        "target_weight": 0.0,
        "enabled": False,
        "currency": "CNY",
        "market": "CN",
        "asset_type": "cn_etf",
        "exclusive_group": "sp500",
        "choice_label": "A股 513500",
        "inception_date": "2013-12-05",
        "management_fee": 0.006,
        "custodian_fee": 0.002,
    },
    {
        "key": "cn_dividend_low_vol",
        "symbol": "512890.SH",
        "name": "红利低波基金",
        "target_weight": 0.08,
        "enabled": True,
        "currency": "CNY",
        "market": "CN",
        "asset_type": "cn_etf",
        "inception_date": "2018-12-19",
        "management_fee": 0.005,
        "custodian_fee": 0.001,
    },
    {
        "key": "cn_hs300_etf",
        "symbol": "510300.SH",
        "name": "沪深300基金",
        "target_weight": 0.12,
        "enabled": True,
        "currency": "CNY",
        "market": "CN",
        "asset_type": "cn_etf",
        "exclusive_group": "cn_broad_etf",
        "choice_label": "沪深300 510300",
        "inception_date": "2012-05-04",
        "trade_start_date": "2012-05-28",
        "allocation_start_date": "2013-01-01",
        "management_fee": 0.0015,
        "custodian_fee": 0.0005,
        "price_fallback": {
            "kind": "open_fund_nav",
            "symbol": "160706",
            "name": "嘉实沪深300ETF联接(LOF)A",
            "start_date": "2005-08-29",
            "scale_mode": "splice",
        },
    },
    {
        "key": "cn_a100_etf",
        "symbol": "159631.SZ",
        "name": "招商中证A100ETF",
        "target_weight": 0.0,
        "enabled": False,
        "currency": "CNY",
        "market": "CN",
        "asset_type": "cn_etf",
        "exclusive_group": "cn_broad_etf",
        "choice_label": "中证A100 159631",
        "inception_date": "2022-08-18",
        "management_fee": 0.005,
        "custodian_fee": 0.001,
        "price_fallback": {
            "kind": "index",
            "symbol": "000903.SH",
            "name": "中证100/中证A100指数",
            "start_date": "2005-12-30",
            "scale_mode": "splice",
        },
    },
    {
        "key": "cn_gold_etf",
        "symbol": "518880.SH",
        "name": "黄金基金（2021年起自动切换518850）",
        "target_weight": 0.10,
        "enabled": True,
        "currency": "CNY",
        "market": "CN",
        "asset_type": "cn_etf",
        "inception_date": "2013-07-18",
        "management_fee": 0.005,
        "custodian_fee": 0.001,
        "price_fallback": {
            "kind": "sge_au9999",
            "symbol": "Au99.99",
            "name": "上海金交所 Au99.99",
            "start_date": "2002-10-30",
            "scale_mode": "fixed",
            "price_scale": 0.01,
        },
        "replacement_assets": [
            {
                "key": "cn_gold_etf_518850",
                "symbol": "518850.SH",
                "name": "华夏黄金ETF（518850）",
                "currency": "CNY",
                "market": "CN",
                "asset_type": "cn_etf",
                "inception_date": "2020-04-13",
                "trade_start_date": "2020-06-05",
                "allocation_start_date": "2021-01-01",
                "management_fee": 0.0015,
                "custodian_fee": 0.0005,
            }
        ],
    },
]

REPO_OPTIONS: list[dict[str, Any]] = [
    {"symbol": "204001", "name": "1天国债逆回购", "instrument_type": "repo", "tenor_days": 1},
    {"symbol": "204002", "name": "2天国债逆回购", "instrument_type": "repo", "tenor_days": 2},
    {"symbol": "204003", "name": "3天国债逆回购", "instrument_type": "repo", "tenor_days": 3},
    {"symbol": "204004", "name": "4天国债逆回购", "instrument_type": "repo", "tenor_days": 4},
    {"symbol": "204007", "name": "7天国债逆回购", "instrument_type": "repo", "tenor_days": 7},
    {"symbol": "204014", "name": "14天国债逆回购", "instrument_type": "repo", "tenor_days": 14},
    {"symbol": "204028", "name": "28天国债逆回购", "instrument_type": "repo", "tenor_days": 28},
    {"symbol": "204091", "name": "91天国债逆回购", "instrument_type": "repo", "tenor_days": 91},
    {"symbol": "204182", "name": "182天国债逆回购", "instrument_type": "repo", "tenor_days": 182},
    {
        "key": "cn_bond_etf_511010",
        "symbol": "511010.SH",
        "display_symbol": "511010",
        "name": "5年期国债ETF（511010）",
        "instrument_type": "cn_bond_etf",
        "currency": "CNY",
        "market": "CN",
        "asset_type": "cn_etf",
        "inception_date": "2013-03-05",
        "trade_start_date": "2013-03-25",
    },
    {
        "key": "cn_bond_etf_511260",
        "symbol": "511260.SH",
        "display_symbol": "511260",
        "name": "10年期国债ETF（511260）",
        "instrument_type": "cn_bond_etf",
        "currency": "CNY",
        "market": "CN",
        "asset_type": "cn_etf",
        "inception_date": "2017-08-04",
        "trade_start_date": "2017-08-24",
    },
    {
        "key": "cn_bond_etf_511090",
        "symbol": "511090.SH",
        "display_symbol": "511090",
        "name": "30年期国债ETF（511090）",
        "instrument_type": "cn_bond_etf",
        "currency": "CNY",
        "market": "CN",
        "asset_type": "cn_etf",
        "inception_date": "2023-05-19",
        "trade_start_date": "2023-06-13",
    },
]


DEFAULT_CONFIG: dict[str, Any] = {
    "initial_capital_cny": 1_000_000.0,
    "start_date": "2012-01-01",
    "end_date": date.today().isoformat(),
    "rebalance_frequency": "yearly",
    "rebalance_band": 0.02,
    "monthly_spend_cny": 5_000.0,
    "monthly_spend_day": "first_cn_trade_day",
    "repo_target_mode": "residual_weight",
    "repo_fixed_target_cny": 360_000.0,
    "repo_fixed_target_ratio": 0.0,
    "repo_symbol": "204001",
    "repo_options": REPO_OPTIONS,
    "allow_fractional_us_shares": True,
    "liquidity_policy": "sell_overweight",
    "assets": DEFAULT_ASSETS,
    "fees": {
        "cn_etf": {
            "commission_rate": 0.00025,
            "min_commission_cny": 0.0,
            "exchange_handling_rate": 0.00004,
            "include_exchange_in_commission": True,
            "stamp_tax_rate": 0.0,
            "transfer_fee_rate": 0.0,
        },
        "repo": {
            "investor_commission_rate": 0.00001,
            "fee_cap_cny": 30.0,
            "lot_size_cny": 1000.0,
        },
        "ibkr_us_etf": {
            "plan": "pro_fixed",
            "fixed_per_share_usd": 0.005,
            "fixed_min_usd": 1.0,
            "fixed_max_trade_pct": 0.01,
            "tiered_per_share_usd": 0.0035,
            "tiered_min_usd": 0.35,
            "lite_commission_usd": 0.0,
            "sec_transaction_fee_rate": 0.0000206,
            "finra_taf_per_share_usd": 0.000195,
            "finra_taf_cap_usd": 9.79,
        },
        "fx": {
            "bank_out_spread_bps": 30.0,
            "bank_in_spread_bps": 30.0,
            "outbound_wire_fee_cny": 150.0,
            "inbound_wire_fee_cny": 0.0,
            "ibkr_auto_fx_markup": 0.0003,
            "use_ibkr_auto_fx": True,
        },
        "hk_connect_etf": {
            "broker_commission_rate": 0.0003,
            "min_broker_commission_hkd": 0.0,
            "trading_fee_rate": 0.0000565,
            "transaction_levy_rate": 0.000027,
            "afrc_transaction_levy_rate": 0.0000015,
            "stock_settlement_fee_rate": 0.000042,
            "min_stock_settlement_fee_hkd": 0.0,
            "max_stock_settlement_fee_hkd": 1_000_000_000.0,
            "stamp_duty_rate": 0.0,
            "portfolio_fee_annual_rate": 0.00008,
            "fx_spread_bps": 20.0,
            "lot_size": 100.0,
        },
        "tax": {
            "cn_fund_dividend_tax_rate": 0.0,
            "us_dividend_withholding_rate": 0.30,
            "hk_dividend_withholding_rate": 0.0,
            "us_capital_gain_tax_rate": 0.0,
        },
    },
}


@dataclass(frozen=True)
class Settings:
    db_path: Path
    tushare_token: str


def load_dotenv_if_present(path: Path | None = None) -> None:
    env_path = path or (BASE_DIR / ".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def get_settings(db_path: str | Path | None = None) -> Settings:
    load_dotenv_if_present()
    return Settings(
        db_path=Path(db_path) if db_path else Path(os.getenv("DATABASE_PATH", DEFAULT_DB_PATH)),
        tushare_token=os.getenv("TUSHARE_TOKEN", ""),
    )


def default_config() -> dict[str, Any]:
    config = deepcopy(DEFAULT_CONFIG)
    config["end_date"] = date.today().isoformat()
    return config


def fx_pair_for_currency(currency: str) -> str | None:
    return FX_PAIR_BY_CURRENCY.get((currency or "CNY").upper())


def required_fx_pairs_for_assets(assets: list[dict[str, Any]]) -> list[str]:
    pairs = {
        pair
        for asset in assets
        if asset.get("enabled", True)
        for pair in [fx_pair_for_currency(asset.get("currency", "CNY"))]
        if pair
    }
    return sorted(pairs)


def selected_repo_option(config: dict[str, Any]) -> dict[str, Any]:
    symbol = str(config.get("repo_symbol") or "204001")
    return next(
        (deepcopy(option) for option in config.get("repo_options", REPO_OPTIONS) if option.get("symbol") == symbol),
        {"symbol": "204001", "name": "1天国债逆回购", "instrument_type": "repo", "tenor_days": 1},
    )


def selected_bond_etf_asset(config: dict[str, Any]) -> dict[str, Any] | None:
    option = selected_repo_option(config)
    if option.get("instrument_type") != "cn_bond_etf":
        return None
    return {
        **option,
        "key": option.get("key") or f"cn_bond_etf_{str(option['symbol']).split('.')[0]}",
        "target_weight": 0.0,
        "enabled": True,
    }


def backtest_assets(config: dict[str, Any]) -> list[dict[str, Any]]:
    assets = deepcopy(config.get("assets", []))
    for asset in list(assets):
        for replacement in asset.get("replacement_assets", []):
            replacement_asset = {
                **deepcopy(replacement),
                "target_weight": 0.0,
                "enabled": bool(asset.get("enabled", True)),
                "replacement_for": asset["symbol"],
            }
            replacement_asset.pop("replacement_assets", None)
            if all(item.get("symbol") != replacement_asset["symbol"] for item in assets):
                assets.append(replacement_asset)
    bond_asset = selected_bond_etf_asset(config)
    if bond_asset and all(asset.get("symbol") != bond_asset["symbol"] for asset in assets):
        assets.append(bond_asset)
    return assets


def repo_rate_symbol(config: dict[str, Any]) -> str:
    option = selected_repo_option(config)
    return str(option.get("symbol") or "204001") if option.get("instrument_type", "repo") == "repo" else "204001"


def asset_price_start_date(asset: dict[str, Any], default_start: str) -> str:
    candidates = [asset_trade_start_date(asset, default_start)]
    fallback = asset.get("price_fallback")
    if isinstance(fallback, dict) and fallback.get("start_date"):
        candidates.append(str(fallback["start_date"]))
    return min(candidates)


def asset_trade_start_date(asset: dict[str, Any], default_start: str) -> str:
    return str(
        asset.get("allocation_start_date")
        or asset.get("trade_start_date")
        or asset.get("inception_date")
        or default_start
    )


def normalize_config(user_config: dict[str, Any] | None) -> dict[str, Any]:
    config = default_config()
    if not user_config:
        return config
    for key, value in user_config.items():
        if key == "fees" and isinstance(value, dict):
            for fee_key, fee_value in value.items():
                if isinstance(fee_value, dict) and fee_key in config["fees"]:
                    config["fees"][fee_key].update(fee_value)
                else:
                    config["fees"][fee_key] = fee_value
        elif key == "assets" and isinstance(value, list):
            config["assets"] = value
        else:
            config[key] = value
    return config


def validate_config(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        start = date.fromisoformat(config["start_date"])
        end = date.fromisoformat(config["end_date"])
        if start > end:
            errors.append("start_date must be before or equal to end_date")
    except (KeyError, TypeError, ValueError):
        errors.append("start_date and end_date must use YYYY-MM-DD")

    repo_target_mode = config.get("repo_target_mode", "residual_weight")
    enabled_weight = sum(
        float(asset.get("target_weight", 0))
        for asset in config.get("assets", [])
        if asset.get("enabled", True)
    )
    if repo_target_mode == "residual_weight" and enabled_weight > 1.0000001:
        errors.append("enabled asset target weights cannot exceed 100%")
    enabled_groups: dict[str, list[str]] = {}
    for asset in config.get("assets", []):
        group = asset.get("exclusive_group")
        if group and asset.get("enabled", True):
            enabled_groups.setdefault(group, []).append(asset.get("symbol", asset.get("key", group)))
    for group, symbols in enabled_groups.items():
        if len(symbols) > 1:
            errors.append(f"exclusive asset group {group} can enable only one asset")
    if float(config.get("initial_capital_cny", 0)) <= 0:
        errors.append("initial_capital_cny must be positive")
    valid_rebalance_frequencies = {"daily", "weekly", "monthly", "quarterly", "semiannual", "yearly"}
    if config.get("rebalance_frequency") not in valid_rebalance_frequencies:
        errors.append("rebalance_frequency must be daily, weekly, monthly, quarterly, semiannual, or yearly")
    if repo_target_mode not in {"residual_weight", "fixed_bucket"}:
        errors.append("repo_target_mode must be residual_weight or fixed_bucket")
    try:
        if float(config.get("repo_fixed_target_cny", 0)) < 0:
            errors.append("repo_fixed_target_cny must be non-negative")
        repo_fixed_target_ratio = float(config.get("repo_fixed_target_ratio", 0))
        if not 0 <= repo_fixed_target_ratio <= 1:
            errors.append("repo_fixed_target_ratio must be between 0 and 1")
    except (TypeError, ValueError):
        errors.append("repo fixed target parameters must be numeric")
    valid_repo_symbols = {item["symbol"] for item in config.get("repo_options", REPO_OPTIONS)}
    if config.get("repo_symbol") not in valid_repo_symbols:
        errors.append("repo_symbol must be one of configured repo options")
    return errors
