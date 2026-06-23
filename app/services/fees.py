from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CnEtfFeeConfig:
    commission_rate: float = 0.00025
    min_commission_cny: float = 0.0
    exchange_handling_rate: float = 0.00004
    include_exchange_in_commission: bool = True
    stamp_tax_rate: float = 0.0
    transfer_fee_rate: float = 0.0


@dataclass(frozen=True)
class IbkrFeeConfig:
    plan: str = "pro_fixed"
    fixed_per_share_usd: float = 0.005
    fixed_min_usd: float = 1.0
    fixed_max_trade_pct: float = 0.01
    tiered_per_share_usd: float = 0.0035
    tiered_min_usd: float = 0.35
    lite_commission_usd: float = 0.0


@dataclass(frozen=True)
class FxFeeConfig:
    bank_out_spread_bps: float = 30.0
    bank_in_spread_bps: float = 30.0
    outbound_wire_fee_cny: float = 150.0
    inbound_wire_fee_cny: float = 0.0
    ibkr_auto_fx_markup: float = 0.0003
    use_ibkr_auto_fx: bool = True


@dataclass(frozen=True)
class RepoFeeConfig:
    investor_commission_rate: float = 0.00001
    fee_cap_cny: float = 30.0
    lot_size_cny: float = 1000.0


def dict_to_dataclass(cls, data: dict) -> object:
    defaults = cls()
    values = {field: getattr(defaults, field) for field in defaults.__dataclass_fields__}
    values.update(data or {})
    return cls(**values)


def cn_etf_fee(gross_amount_cny: float, config: CnEtfFeeConfig) -> float:
    commission = max(gross_amount_cny * config.commission_rate, config.min_commission_cny)
    handling = 0.0 if config.include_exchange_in_commission else gross_amount_cny * config.exchange_handling_rate
    taxes = gross_amount_cny * (config.stamp_tax_rate + config.transfer_fee_rate)
    return round(commission + handling + taxes, 6)


def ibkr_us_etf_fee(shares: float, gross_amount_usd: float, config: IbkrFeeConfig) -> float:
    plan = (config.plan or "pro_fixed").lower()
    if plan == "lite":
        return round(config.lite_commission_usd, 6)
    if plan == "pro_tiered":
        return round(max(shares * config.tiered_per_share_usd, config.tiered_min_usd), 6)
    fixed = max(shares * config.fixed_per_share_usd, config.fixed_min_usd)
    return round(min(fixed, gross_amount_usd * config.fixed_max_trade_pct), 6)


def bps_to_rate(bps: float) -> float:
    return bps / 10000.0


def cny_to_usd(cny_amount: float, usd_cny_rate: float, config: FxFeeConfig, include_wire: bool) -> tuple[float, float]:
    fixed_fee = config.outbound_wire_fee_cny if include_wire else 0.0
    usable = max(cny_amount - fixed_fee, 0.0)
    spread = bps_to_rate(config.bank_out_spread_bps)
    if config.use_ibkr_auto_fx:
        spread += config.ibkr_auto_fx_markup
    usd = usable / (usd_cny_rate * (1.0 + spread))
    fee_cny = cny_amount - usd * usd_cny_rate
    return round(usd, 8), round(fee_cny, 6)


def usd_to_cny(usd_amount: float, usd_cny_rate: float, config: FxFeeConfig, include_wire: bool) -> tuple[float, float]:
    spread = bps_to_rate(config.bank_in_spread_bps)
    if config.use_ibkr_auto_fx:
        spread += config.ibkr_auto_fx_markup
    gross_mid = usd_amount * usd_cny_rate
    cny = gross_mid * (1.0 - spread)
    if include_wire:
        cny -= config.inbound_wire_fee_cny
    fee_cny = gross_mid - cny
    return round(max(cny, 0.0), 6), round(max(fee_cny, 0.0), 6)


def repo_fee(principal_cny: float, config: RepoFeeConfig) -> float:
    return round(min(principal_cny * config.investor_commission_rate, config.fee_cap_cny), 6)


def repo_interest(principal_cny: float, annual_rate_percent: float, actual_days: int) -> float:
    return round(principal_cny * (annual_rate_percent / 100.0) * actual_days / 365.0, 6)

