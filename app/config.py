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
        "inception_date": "2010-09-07",
        "expense_ratio": 0.0003,
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
        "inception_date": "2012-05-04",
        "management_fee": 0.0015,
        "custodian_fee": 0.0005,
    },
    {
        "key": "cn_gold_etf",
        "symbol": "518880.SH",
        "name": "黄金基金",
        "target_weight": 0.10,
        "enabled": True,
        "currency": "CNY",
        "market": "CN",
        "asset_type": "cn_etf",
        "inception_date": "2013-07-18",
        "management_fee": 0.005,
        "custodian_fee": 0.001,
    },
]

REPO_OPTIONS: list[dict[str, Any]] = [
    {"symbol": "204001", "name": "1天国债逆回购", "tenor_days": 1},
    {"symbol": "204002", "name": "2天国债逆回购", "tenor_days": 2},
    {"symbol": "204003", "name": "3天国债逆回购", "tenor_days": 3},
    {"symbol": "204004", "name": "4天国债逆回购", "tenor_days": 4},
    {"symbol": "204007", "name": "7天国债逆回购", "tenor_days": 7},
    {"symbol": "204014", "name": "14天国债逆回购", "tenor_days": 14},
    {"symbol": "204028", "name": "28天国债逆回购", "tenor_days": 28},
    {"symbol": "204091", "name": "91天国债逆回购", "tenor_days": 91},
    {"symbol": "204182", "name": "182天国债逆回购", "tenor_days": 182},
]


DEFAULT_CONFIG: dict[str, Any] = {
    "initial_capital_cny": 1_000_000.0,
    "start_date": "2012-01-01",
    "end_date": date.today().isoformat(),
    "rebalance_frequency": "yearly",
    "rebalance_band": 0.02,
    "monthly_spend_cny": 5_000.0,
    "monthly_spend_day": "first_cn_trade_day",
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
        },
        "fx": {
            "bank_out_spread_bps": 30.0,
            "bank_in_spread_bps": 30.0,
            "outbound_wire_fee_cny": 150.0,
            "inbound_wire_fee_cny": 0.0,
            "ibkr_auto_fx_markup": 0.0003,
            "use_ibkr_auto_fx": True,
        },
        "tax": {
            "cn_fund_dividend_tax_rate": 0.0,
            "us_dividend_withholding_rate": 0.10,
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
    return deepcopy(DEFAULT_CONFIG)


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

    enabled_weight = sum(
        float(asset.get("target_weight", 0))
        for asset in config.get("assets", [])
        if asset.get("enabled", True)
    )
    if enabled_weight > 1.0000001:
        errors.append("enabled asset target weights cannot exceed 100%")
    if float(config.get("initial_capital_cny", 0)) <= 0:
        errors.append("initial_capital_cny must be positive")
    if config.get("rebalance_frequency") not in {"yearly", "semiannual"}:
        errors.append("rebalance_frequency must be yearly or semiannual")
    valid_repo_symbols = {item["symbol"] for item in config.get("repo_options", REPO_OPTIONS)}
    if config.get("repo_symbol") not in valid_repo_symbols:
        errors.append("repo_symbol must be one of configured repo options")
    return errors
