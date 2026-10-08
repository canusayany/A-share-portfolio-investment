"""Bounded scenario research using cached market data and the real engine."""
from __future__ import annotations

import calendar
from copy import deepcopy
from datetime import date
from itertools import product
import math
from typing import Any

from app.config import backtest_assets, normalize_config, repo_rate_symbol, validate_config
from app.services.backtest_engine import BacktestCancelled, raise_if_cancelled, run_backtest
from app.services.data_sync import required_data_missing


MAX_RESEARCH_SCENARIOS = 24
FREQUENCIES = {"daily", "weekly", "monthly", "quarterly", "semiannual", "yearly"}
RESULT_METRICS = (
    "annualized_return", "max_drawdown", "total_fees_cny", "rebalance_trade_count",
    "rebalance_check_count", "final_asset_cny", "total_spend_cny", "total_planned_spend_cny",
    "total_spend_shortfall_cny", "first_spend_shortfall_date", "spend_shortfall_count",
    "initial_capital_cny", "net_profit_cny", "total_return", "annualized_return_basis",
    "rebalance_positive_ratio", "rebalance_positive_count", "rebalance_negative_count",
    "rebalance_flat_count", "rebalance_evaluated_count", "rebalance_cycle_return_basis",
)


def _array(payload: dict, key: str) -> list:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{key} must be a non-empty array")
    if len(value) > MAX_RESEARCH_SCENARIOS:
        raise ValueError(f"research is limited to {MAX_RESEARCH_SCENARIOS} scenarios")
    return value


def _numbers(payload: dict, key: str, *, maximum: float | None = None) -> list[float]:
    result = []
    for value in _array(payload, key):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must contain numeric values")
        try:
            number = float(value)
        except OverflowError as exc:
            raise ValueError(f"{key} must contain finite numeric values") from exc
        if not math.isfinite(number) or number < 0 or (maximum is not None and number > maximum):
            upper = f" and at most {maximum}" if maximum is not None else ""
            raise ValueError(f"{key} must be finite, non-negative{upper}")
        result.append(number)
    if len(set(result)) != len(result):
        raise ValueError(f"{key} must not contain duplicate values")
    return result


def _start_in_year(start: date, year: int) -> date:
    return start.replace(year=year, day=min(start.day, calendar.monthrange(year, start.month)[1]))


def validate_research_request(base_config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and copy a request; never mutate the saved configuration."""
    if not isinstance(base_config, dict) or not isinstance(payload, dict):
        raise ValueError("base_config and research payload must be objects")
    config = normalize_config(deepcopy(base_config))
    errors = validate_config(config)
    if errors:
        raise ValueError("invalid base configuration: " + "; ".join(errors))
    kind = payload.get("kind")
    if kind == "rebalance_grid":
        frequencies = _array(payload, "frequencies")
        if any(not isinstance(item, str) or item not in FREQUENCIES for item in frequencies):
            raise ValueError("frequencies contains an unsupported rebalance frequency")
        if len(set(frequencies)) != len(frequencies):
            raise ValueError("frequencies must not contain duplicate values")
        bands = _numbers(payload, "bands", maximum=1.0)
        normalized = {"kind": kind, "frequencies": list(frequencies), "bands": bands}
        total = len(frequencies) * len(bands)
    elif kind == "withdrawal_stress":
        spends = _numbers(payload, "monthly_spends")
        growth = _numbers(payload, "annual_growth_rates", maximum=0.5)
        years = _array(payload, "start_years")
        start, end = date.fromisoformat(config["start_date"]), date.fromisoformat(config["end_date"])
        for year in years:
            if isinstance(year, bool) or not isinstance(year, int) or not 1 <= year <= 9999:
                raise ValueError("start_years must contain integer years from 1 to 9999")
            scenario_start = _start_in_year(start, year)
            if not start <= scenario_start < end:
                raise ValueError("each start_year must produce a start within the base interval and before its end")
        if len(set(years)) != len(years):
            raise ValueError("start_years must not contain duplicate values")
        normalized = {"kind": kind, "monthly_spends": spends, "annual_growth_rates": growth, "start_years": list(years)}
        total = len(spends) * len(growth) * len(years)
    else:
        raise ValueError("kind must be rebalance_grid or withdrawal_stress")
    if total > MAX_RESEARCH_SCENARIOS:
        raise ValueError(f"research is limited to {MAX_RESEARCH_SCENARIOS} scenarios; requested {total}")
    return {**normalized, "total": total}


def _scenario_inputs(request: dict) -> list[dict]:
    if request["kind"] == "rebalance_grid":
        return [
            {"rebalance_frequency": frequency, "rebalance_band": band}
            for frequency, band in product(request["frequencies"], request["bands"])
        ]
    return [
        {"monthly_spend_cny": spend, "monthly_spend_annual_growth": growth, "start_year": year}
        for spend, growth, year in product(request["monthly_spends"], request["annual_growth_rates"], request["start_years"])
    ]


def run_research(conn, base_config: dict[str, Any], payload: dict[str, Any], should_cancel=None, on_progress=None) -> dict[str, Any]:
    """Run independent scenarios without syncing data or persisting backtests."""
    request = validate_research_request(base_config, payload)
    base = normalize_config(deepcopy(base_config))
    raise_if_cancelled(should_cancel)
    rows = []
    coverage_cache: dict[tuple[str, str], list[str]] = {}
    for index, inputs in enumerate(_scenario_inputs(request), 1):
        raise_if_cancelled(should_cancel)
        config = deepcopy(base)
        config.update({key: value for key, value in inputs.items() if key != "start_year"})
        if "start_year" in inputs:
            config["start_date"] = _start_in_year(date.fromisoformat(base["start_date"]), inputs["start_year"]).isoformat()
        dip_buy_active = bool(config.get("dip_buy_enabled")) and config["rebalance_frequency"] == "yearly"
        if not config.get("dip_buy_enabled"):
            dip_note = "基准配置未启用逢跌补仓。"
        elif dip_buy_active:
            dip_note = "年度调仓，沿用基准配置的逢跌补仓规则。"
        else:
            dip_note = "逢跌补仓仅适用于年度调仓，本情景不执行；其他配置保持不变。"
        row = {
            "id": f"{request['kind']}:{index}", "inputs": inputs,
            "requested_start_date": config["start_date"], "requested_end_date": config["end_date"],
            "start_date": None, "end_date": None,
            "dip_buy_active": dip_buy_active, "dip_buy_applicability_note": dip_note,
        }
        try:
            interval = (config["start_date"], config["end_date"])
            if interval not in coverage_cache:
                coverage_cache[interval] = required_data_missing(
                    conn, *interval, assets=backtest_assets(config), repo_symbol=repo_rate_symbol(config),
                )
            missing = coverage_cache[interval]
            raise_if_cancelled(should_cancel)
            if missing:
                raise ValueError("研究所需的本地数据不完整，请先同步基准区间数据：" + ", ".join(missing))
            summary = run_backtest(
                conn, config, should_cancel=should_cancel, persist=False,
                include_comparison=False, include_month_analysis=False, include_rolling_analysis=False,
            )["summary"]
            row.update({key: summary.get(key) for key in RESULT_METRICS})
            row.update(status="success", start_date=summary["start_date"], end_date=summary["end_date"])
        except BacktestCancelled:
            raise
        except Exception as exc:
            row.update(status="failed", error=str(exc))
        rows.append(row)
        if on_progress is not None:
            on_progress(len(rows), request["total"], deepcopy(rows))
        raise_if_cancelled(should_cancel)
    return {
        "kind": request["kind"], "total": request["total"], "completed": len(rows), "rows": rows,
        "methodology": {
            "engine": "same_backtest_engine", "persist_backtests": False, "external_sync": False,
            "fixed_conditions": "除情景输入外，资产配置、初始资金、费用、结束日期及其他规则均沿用基准配置。",
            "withdrawal_growth": "以各情景首个可用交易月份为第1个提取月份，第13、25个月起按年增幅复利递增。",
            "start_dates": "起始年替换基准开始日期的年份；2月29日在非闰年取2月28日，实际日期取可用交易日。",
            "returns": "收益率剔除消费现金流；同时报告实际提取与消费缺口。不同起始年回测时长不同。",
            "annual_only_rules": "逢跌补仓和年度调仓月份仅在年度频率生效。",
            "rebalance_positive_ratio": "调仓间盈利占比=盈利周期数/已完成周期数，持平也计入分母。首次建仓完成只作起点，每次实际成交的周期调仓完成作终点；带内或未成交检查、现金标的启用不重置起点，末尾未再调仓的区间不统计。按成交后组合净值比较，剔除消费现金流，包含期间费用及终点调仓费用；初次建仓费用已在起点净值中，不是调仓动作本身的盈利率。",
        },
    }
