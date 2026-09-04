from __future__ import annotations

from copy import deepcopy
import csv
from datetime import date
import io
import math
from typing import Any

from app.config import normalize_config, selected_repo_option
from app.db import json_loads
from app.services.calendar import parse_date
from app.services.backtest_engine import (
    ASSET_COMOVEMENT_EPSILON,
    ASSET_COMOVEMENT_SLEEVES,
    active_route_symbols,
    attach_nontradable_route_expense_drag,
    attach_proxy_price_maps,
    configured_share_splits,
    load_dividend_events,
    load_price_map,
    prepare_active_asset_routes,
    repo_fixed_target_weight,
    run_backtest,
    simulation_assets,
)


DIAGNOSTIC_WINDOWS = (
    ("all", "全部历史", None),
    ("1y", "近1年", 1),
    ("3y", "近3年", 3),
    ("5y", "近5年", 5),
    ("10y", "近10年", 10),
)
DIAGNOSTIC_WINDOW_YEARS = {key: years for key, _label, years in DIAGNOSTIC_WINDOWS}
DIAGNOSTIC_WINDOW_LABELS = {key: label for key, label, _years in DIAGNOSTIC_WINDOWS}
DIAGNOSTIC_WEIGHT_STEP = 0.05
DIAGNOSTIC_METRICS = (
    "start_date",
    "end_date",
    "final_asset_cny",
    "net_profit_cny",
    "total_return",
    "annualized_return",
    "max_drawdown",
    "annual_return_drawdown_ratio",
    "positive_year_count",
    "complete_year_count",
    "total_fees_cny",
    "trade_count",
)


def _normalize_historical_config(user_config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    config = deepcopy(normalize_config(user_config))
    adjustments: list[str] = []
    valid_repo_symbols = [str(option["symbol"]) for option in config.get("repo_options", [])]
    if config.get("repo_symbol") not in valid_repo_symbols and valid_repo_symbols:
        retired_symbol = str(config.get("repo_symbol") or "")
        config["repo_symbol"] = valid_repo_symbols[0]
        adjustments.append(f"历史现金管理标的 {retired_symbol} 已停用，重算时使用 {valid_repo_symbols[0]}")
    return config, adjustments


def _subtract_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def diagnostic_window_config(user_config: dict[str, Any], window_key: str) -> tuple[dict[str, Any], dict[str, Any]]:
    config, adjustments = _normalize_historical_config(user_config)
    if window_key not in DIAGNOSTIC_WINDOW_YEARS:
        raise ValueError("时间窗口必须是 all、1y、3y、5y 或 10y")
    original_start = parse_date(config["start_date"])
    end_day = parse_date(config["end_date"])
    years = DIAGNOSTIC_WINDOW_YEARS[window_key]
    requested_start = _subtract_years(end_day, years) if years else original_start
    selected_start = max(original_start, requested_start)
    if selected_start >= end_day:
        raise ValueError("所选时间窗口没有足够的回测区间")
    config["start_date"] = selected_start.isoformat()
    config["end_date"] = end_day.isoformat()
    config["rebalance_month_analysis_enabled"] = False
    return config, {
        "key": window_key,
        "label": DIAGNOSTIC_WINDOW_LABELS[window_key],
        "start_date": selected_start.isoformat(),
        "end_date": end_day.isoformat(),
        "configuration_adjustments": adjustments,
    }


def _selected_assets(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        asset
        for asset in config["assets"]
        if asset.get("enabled", True) and float(asset.get("target_weight", 0.0) or 0.0) > 1e-12
    ]


def _asset_label(asset: dict[str, Any]) -> str:
    return str(asset.get("choice_label") or asset.get("name") or asset["symbol"])


def portfolio_target_weights(user_config: dict[str, Any]) -> dict[str, float]:
    config = normalize_config(user_config)
    selected = _selected_assets(config)
    raw_weights = {
        str(asset["symbol"]): max(float(asset.get("target_weight", 0.0) or 0.0), 0.0)
        for asset in selected
    }
    if config.get("repo_target_mode", "residual_weight") == "fixed_bucket":
        repo_weight = repo_fixed_target_weight(config, float(config.get("initial_capital_cny") or 0.0))
        investable = max(1.0 - repo_weight, 0.0)
        raw_total = sum(raw_weights.values())
        weights = {
            symbol: (weight / raw_total * investable if raw_total > 0 else 0.0)
            for symbol, weight in raw_weights.items()
        }
        weights["REPO"] = repo_weight
        return weights
    weights = raw_weights
    weights["REPO"] = max(1.0 - sum(weights.values()), 0.0)
    return weights


def _config_with_weights(user_config: dict[str, Any], weights: dict[str, float]) -> dict[str, Any]:
    config = deepcopy(normalize_config(user_config))
    for asset in config["assets"]:
        symbol = str(asset["symbol"])
        if symbol not in weights:
            continue
        weight = max(float(weights[symbol]), 0.0)
        asset["target_weight"] = weight
        asset["enabled"] = weight > 1e-12
    return config


def leave_one_out_configs(user_config: dict[str, Any]) -> list[dict[str, Any]]:
    config = normalize_config(user_config)
    base_weights = portfolio_target_weights(config)
    scenarios: list[dict[str, Any]] = []
    fixed_repo = config.get("repo_target_mode", "residual_weight") == "fixed_bucket"
    for asset in _selected_assets(config):
        removed_symbol = str(asset["symbol"])
        removed_weight = base_weights[removed_symbol]
        target_total = 1.0 - base_weights["REPO"] if fixed_repo else 1.0
        remaining_total = target_total - removed_weight
        if remaining_total <= 1e-12:
            continue
        weights = {
            symbol: (
                base_weights["REPO"]
                if symbol == "REPO" and fixed_repo
                else 0.0
                if symbol == removed_symbol
                else weight * target_total / remaining_total
            )
            for symbol, weight in base_weights.items()
        }
        scenarios.append(
            {
                "id": f"without:{removed_symbol}",
                "removed_symbol": removed_symbol,
                "removed_name": _asset_label(asset),
                "removed_weight": removed_weight,
                "weights": weights,
                "config": _config_with_weights(config, weights),
            }
        )
    return scenarios


def local_weight_candidate_configs(
    user_config: dict[str, Any],
    step: float = DIAGNOSTIC_WEIGHT_STEP,
) -> list[dict[str, Any]]:
    config = normalize_config(user_config)
    base_weights = portfolio_target_weights(config)
    labels = {str(asset["symbol"]): _asset_label(asset) for asset in _selected_assets(config)}
    labels["REPO"] = "现金管理"
    scenarios: list[dict[str, Any]] = []
    fixed_repo = config.get("repo_target_mode", "residual_weight") == "fixed_bucket"
    adjustable = {
        symbol: weight
        for symbol, weight in base_weights.items()
        if not (fixed_repo and symbol == "REPO")
    }
    for from_symbol, from_weight in adjustable.items():
        if from_weight + 1e-12 < step:
            continue
        for to_symbol, to_weight in adjustable.items():
            if to_symbol == from_symbol or to_weight + step > 1.0 + 1e-12:
                continue
            weights = dict(base_weights)
            weights[from_symbol] = from_weight - step
            weights[to_symbol] = to_weight + step
            scenarios.append(
                {
                    "id": f"transfer:{from_symbol}:{to_symbol}:{step:.4f}",
                    "label": f"{labels[from_symbol]} -{step:.0%} → {labels[to_symbol]} +{step:.0%}",
                    "from_symbol": from_symbol,
                    "to_symbol": to_symbol,
                    "step": step,
                    "weights": weights,
                    "config": _config_with_weights(config, weights),
                }
            )
    return scenarios


def _summary_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    return {key: summary.get(key) for key in DIAGNOSTIC_METRICS}


def _run_summary(conn, config: dict[str, Any]) -> dict[str, Any]:
    return run_backtest(
        conn,
        config,
        persist=False,
        include_comparison=False,
        include_month_analysis=False,
        include_rolling_analysis=False,
    )["summary"]


def _correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    variance_x = sum((x - mean_x) ** 2 for x in xs)
    variance_y = sum((y - mean_y) ** 2 for y in ys)
    denominator = math.sqrt(variance_x * variance_y)
    return covariance / denominator if denominator > 1e-18 else None


def _asset_daily_return_records(conn, config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    configured_by_key = {str(asset.get("key")): asset for asset in config["assets"]}
    sleeves: list[dict[str, Any]] = []
    public_assets: list[dict[str, Any]] = []
    for config_key, public_key, label in ASSET_COMOVEMENT_SLEEVES:
        source = configured_by_key.get(config_key)
        if not source or not source.get("enabled", True) or float(source.get("target_weight", 0.0) or 0.0) <= 0:
            continue
        sleeve = deepcopy(source)
        sleeves.append(sleeve)
        public_assets.append(
            {
                "key": public_key,
                "config_key": config_key,
                "symbol": str(sleeve["symbol"]),
                "name": _asset_label(sleeve),
                "label": label,
                "role": "risk_asset" if public_key == "dividend_low_vol" else "defensive_asset",
            }
        )
    if len(sleeves) != len(ASSET_COMOVEMENT_SLEEVES):
        return public_assets, []

    start = str(config["start_date"])
    end = str(config["end_date"])
    sim_assets = simulation_assets(sleeves)
    symbols = [str(asset["symbol"]) for asset in sim_assets]
    share_scale_maps: dict[str, dict[str, float]] = {}
    price_maps = load_price_map(
        conn,
        symbols,
        start,
        end,
        share_splits=configured_share_splits(sleeves),
        share_scale_maps=share_scale_maps,
    )
    attach_proxy_price_maps(price_maps, sleeves)
    attach_nontradable_route_expense_drag(price_maps, sleeves)
    ex_events, _pay_events = load_dividend_events(conn, symbols, start, end, share_scale_maps)
    dividends: dict[tuple[str, str], float] = {}
    for trade_date, events in ex_events.items():
        for event in events:
            key = (trade_date, str(event["symbol"]))
            dividends[key] = dividends.get(key, 0.0) + (
                float(event["div_cash"]) * float(event.get("normalized_share_scale", 1.0) or 1.0)
            )

    prepared_routes = prepare_active_asset_routes({**config, "assets": sleeves})
    public_key_by_symbol = {
        str(sleeve["symbol"]): public_key
        for sleeve, (_config_key, public_key, _label) in zip(sleeves, ASSET_COMOVEMENT_SLEEVES)
    }
    all_dates = sorted({trade_date for prices in price_maps.values() for trade_date in prices})
    latest_prices: dict[str, float | None] = {symbol: None for symbol in price_maps}
    previous_prices: dict[str, float] = {}
    records: list[dict[str, Any]] = []
    for trade_date in all_dates:
        current_prices: dict[str, float] = {}
        for symbol, prices in price_maps.items():
            current = prices.get(trade_date)
            if current is not None:
                current_prices[symbol] = float(current)
                latest_prices[symbol] = float(current)
        routes = active_route_symbols(parse_date(trade_date), latest_prices, prepared_routes)
        returns: dict[str, float] = {}
        for sleeve in sleeves:
            logical_symbol = str(sleeve["symbol"])
            route_symbol = routes.get(logical_symbol)
            current = current_prices.get(route_symbol) if route_symbol else None
            previous = previous_prices.get(route_symbol) if route_symbol else None
            if route_symbol is None or current is None or previous is None or previous <= 0:
                break
            returns[public_key_by_symbol[logical_symbol]] = (
                current + dividends.get((trade_date, route_symbol), 0.0)
            ) / previous - 1.0
        if len(returns) == len(sleeves):
            records.append({"trade_date": trade_date, "returns": returns})
        previous_prices.update(current_prices)
    return public_assets, records


def stress_protection_statistics(
    conn,
    user_config: dict[str, Any],
    window_key: str = "all",
) -> dict[str, Any]:
    config, window = diagnostic_window_config(user_config, window_key)
    assets, records = _asset_daily_return_records(conn, config)
    if not records:
        return {
            "available": False,
            "frequency": "monthly",
            "stress_definition": "红利ETF月度总收益小于0",
            "window": window,
            "assets": assets,
            "comparable_periods": 0,
            "stress_periods": 0,
            "message": "所选时间窗口缺少三项标的的共同收益数据",
        }

    monthly: dict[str, dict[str, float]] = {}
    for row in records:
        month = row["trade_date"][:7]
        bucket = monthly.setdefault(month, {asset["key"]: 1.0 for asset in assets})
        for key, daily_return in row["returns"].items():
            bucket[key] *= 1.0 + float(daily_return)
    periods = [
        {"period": month, "returns": {key: value - 1.0 for key, value in values.items()}}
        for month, values in sorted(monthly.items())
    ]
    dividend_key = "dividend_low_vol"
    stress_periods = [row for row in periods if row["returns"][dividend_key] < -ASSET_COMOVEMENT_EPSILON]
    worst_count = max(1, math.ceil(len(periods) * 0.10))
    worst_periods = sorted(periods, key=lambda row: row["returns"][dividend_key])[:worst_count]
    result_assets: list[dict[str, Any]] = []
    for asset in assets:
        key = asset["key"]
        stress_returns = [row["returns"][key] for row in stress_periods]
        dividend_stress_returns = [row["returns"][dividend_key] for row in stress_periods]
        worst_returns = [row["returns"][key] for row in worst_periods]
        positive_stress = sum(value > ASSET_COMOVEMENT_EPSILON for value in stress_returns)
        positive_worst = sum(value > ASSET_COMOVEMENT_EPSILON for value in worst_returns)
        result_assets.append(
            {
                **asset,
                "stress_positive_periods": positive_stress,
                "stress_positive_rate": positive_stress / len(stress_returns) if stress_returns else None,
                "stress_average_return": sum(stress_returns) / len(stress_returns) if stress_returns else None,
                "worst_decile_positive_periods": positive_worst,
                "worst_decile_positive_rate": positive_worst / len(worst_returns) if worst_returns else None,
                "worst_decile_average_return": sum(worst_returns) / len(worst_returns) if worst_returns else None,
                "stress_correlation_with_dividend": _correlation(stress_returns, dividend_stress_returns),
            }
        )
    return {
        "available": True,
        "frequency": "monthly",
        "stress_definition": "红利ETF月度总收益小于0",
        "window": window,
        "assets": result_assets,
        "comparable_periods": len(periods),
        "stress_periods": len(stress_periods),
        "worst_decile_periods": len(worst_periods),
        "methodology": "使用与回测一致的复权连续价格、现金分红、上市前代理和ETF替换路径",
        "message": None,
    }


def _effect_conclusion(annual_delta: float, drawdown_delta: float) -> tuple[str, str]:
    tolerance = 0.0005
    if annual_delta > tolerance and drawdown_delta > tolerance:
        return "可能冗余", "删掉后收益和回撤同时改善，优先检查是否减仓"
    if annual_delta > tolerance and drawdown_delta < -tolerance:
        return "防守保险", "删掉后收益提高但回撤恶化，说明它用收益换保护"
    if annual_delta < -tolerance and drawdown_delta > tolerance:
        return "收益引擎", "删掉后回撤改善但收益下降，说明它主要提供增长"
    if annual_delta < -tolerance and drawdown_delta < -tolerance:
        return "核心贡献", "删掉后收益和回撤同时恶化，当前样本中贡献明确"
    return "作用有限", "变化较小，需要结合其他时间窗口判断"


def _candidate_result(summary: dict[str, Any], weights: dict[str, float], **extra: Any) -> dict[str, Any]:
    return {**extra, "weights": weights, **_summary_metrics(summary)}


def strategy_diagnostics(
    conn,
    user_config: dict[str, Any],
    stored_summary: dict[str, Any] | None = None,
    window_key: str = "all",
) -> dict[str, Any]:
    config, window = diagnostic_window_config(user_config, window_key)
    can_use_stored = (
        window_key == "all"
        and stored_summary is not None
        and str(stored_summary.get("start_date") or "") >= str(config["start_date"])
        and str(stored_summary.get("end_date") or "") <= str(config["end_date"])
    )
    base_summary = stored_summary if can_use_stored else _run_summary(conn, config)
    base_metrics = _summary_metrics(base_summary)
    base_weights = portfolio_target_weights(config)

    asset_effects: list[dict[str, Any]] = []
    for scenario in leave_one_out_configs(config):
        summary = _run_summary(conn, scenario["config"])
        annual_delta = float(summary.get("annualized_return") or 0.0) - float(base_summary.get("annualized_return") or 0.0)
        drawdown_delta = float(summary.get("max_drawdown") or 0.0) - float(base_summary.get("max_drawdown") or 0.0)
        ratio_delta = float(summary.get("annual_return_drawdown_ratio") or 0.0) - float(base_summary.get("annual_return_drawdown_ratio") or 0.0)
        conclusion, explanation = _effect_conclusion(annual_delta, drawdown_delta)
        asset_effects.append(
            {
                "removed_symbol": scenario["removed_symbol"],
                "removed_name": scenario["removed_name"],
                "removed_weight": scenario["removed_weight"],
                "weights": scenario["weights"],
                "annualized_return_delta": annual_delta,
                "max_drawdown_delta": drawdown_delta,
                "annual_return_drawdown_ratio_delta": ratio_delta,
                "conclusion": conclusion,
                "explanation": explanation,
                "result": _summary_metrics(summary),
            }
        )

    optimization_candidates = [
        _candidate_result(base_summary, base_weights, id="current", label="当前权重", current=True)
    ]
    for scenario in local_weight_candidate_configs(config):
        summary = _run_summary(conn, scenario["config"])
        optimization_candidates.append(
            _candidate_result(
                summary,
                scenario["weights"],
                id=scenario["id"],
                label=scenario["label"],
                current=False,
                from_symbol=scenario["from_symbol"],
                to_symbol=scenario["to_symbol"],
                step=scenario["step"],
                annualized_return_delta=float(summary.get("annualized_return") or 0.0) - float(base_summary.get("annualized_return") or 0.0),
                max_drawdown_delta=float(summary.get("max_drawdown") or 0.0) - float(base_summary.get("max_drawdown") or 0.0),
            )
        )

    base_annualized = float(base_summary.get("annualized_return") or 0.0)
    base_drawdown = float(base_summary.get("max_drawdown") or 0.0)
    feasible = [
        row
        for row in optimization_candidates
        if row.get("annualized_return") is not None
        and row.get("annual_return_drawdown_ratio") is not None
        and float(row["annualized_return"]) >= base_annualized - 0.0025
        and float(row.get("max_drawdown") or 0.0) >= base_drawdown - 0.01
    ]
    recommended = max(
        feasible or optimization_candidates[:1],
        key=lambda row: (
            -math.inf if row.get("annual_return_drawdown_ratio") is None else float(row["annual_return_drawdown_ratio"]),
            -math.inf if row.get("annualized_return") is None else float(row["annualized_return"]),
        ),
    )
    if recommended["current"]:
        recommendation_text = "当前权重已是本地5%调整范围内收益/回撤比较优的方案"
    else:
        recommendation_text = f"本窗口可优先复核：{recommended['label']}"

    return {
        "available": True,
        "window": window,
        "window_order": [key for key, _label, _years in DIAGNOSTIC_WINDOWS],
        "base": {**base_metrics, "weights": base_weights},
        "asset_effects": asset_effects,
        "stress_protection": stress_protection_statistics(conn, config, "all"),
        "optimization_candidates": optimization_candidates,
        "recommendation": {
            "candidate_id": recommended["id"],
            "label": recommended["label"],
            "text": recommendation_text,
            "weights": recommended["weights"],
            "annualized_return": recommended.get("annualized_return"),
            "max_drawdown": recommended.get("max_drawdown"),
            "annual_return_drawdown_ratio": recommended.get("annual_return_drawdown_ratio"),
            "guardrails": {
                "max_annualized_return_sacrifice": 0.0025,
                "max_additional_drawdown": 0.01,
            },
        },
        "methodology": {
            "counterfactual_engine": "完整回测引擎",
            "leave_one_out": "删除一个标的后，将其权重按原比例分配给其余标的和现金管理",
            "weight_step": DIAGNOSTIC_WEIGHT_STEP,
            "optimization_scope": "当前权重附近任意两个资金桶之间移动5个百分点",
            "costs_included": True,
            "warning": "优化结果是历史条件下的本地候选，需比较多个时间窗口，不代表未来收益",
        },
    }


def _stored_backtest_daily_metrics(
    conn,
    run_id: str | None,
    selected_assets: list[dict[str, Any]],
    end_date: str,
    include_cash: bool = False,
) -> dict[tuple[str, str], dict[str, float | None]]:
    if not run_id or (not selected_assets and not include_cash):
        return {}
    rows = conn.execute(
        """
        SELECT trade_date,total_asset_cny,flow_cny,daily_return,
               cumulative_return,drawdown,payload_json
        FROM portfolio_daily
        WHERE run_id=? AND trade_date<=?
        ORDER BY trade_date
        """,
        (run_id, end_date),
    ).fetchall()
    aliases = {
        str(asset["symbol"]): list(
            dict.fromkeys(
                [
                    str(asset["symbol"]),
                    *(
                        [str(asset["price_fallback"]["symbol"])]
                        if isinstance(asset.get("price_fallback"), dict)
                        and asset["price_fallback"].get("symbol")
                        else []
                    ),
                    *(
                        str(replacement["symbol"])
                        for replacement in asset.get("replacement_assets", [])
                        if isinstance(replacement, dict) and replacement.get("symbol")
                    ),
                ]
            )
        )
        for asset in selected_assets
    }
    if include_cash:
        aliases["REPO"] = ["REPO"]
    previous_values = {symbol: 0.0 for symbol in aliases}
    navs = {symbol: 1.0 for symbol in aliases}
    peaks = {symbol: 1.0 for symbol in aliases}
    result: dict[tuple[str, str], dict[str, float | None]] = {}
    for row in rows:
        trade_date = str(row["trade_date"])
        payload = json_loads(row["payload_json"], {})
        source_values = payload.get("values", {})
        source_profits = payload.get("asset_daily_profit_cny")
        for logical_symbol, group_aliases in aliases.items():
            current_value = sum(float(source_values.get(alias, 0.0) or 0.0) for alias in group_aliases)
            profit = (
                sum(float(source_profits.get(alias, 0.0) or 0.0) for alias in group_aliases)
                if isinstance(source_profits, dict)
                else None
            )
            daily_return: float | None = None
            if profit is not None:
                capital_base = max(previous_values[logical_symbol], current_value - profit, 0.0)
                daily_return = profit / capital_base if capital_base > 1e-9 else None
                if daily_return is not None:
                    navs[logical_symbol] = max(navs[logical_symbol] * (1.0 + daily_return), 0.0)
                peaks[logical_symbol] = max(peaks[logical_symbol], navs[logical_symbol])
            result[(trade_date, logical_symbol)] = {
                "asset_value_cny": current_value,
                "asset_daily_profit_cny": profit,
                "asset_daily_return": daily_return,
                "asset_cumulative_return": navs[logical_symbol] - 1.0 if profit is not None else None,
                "asset_drawdown": (
                    navs[logical_symbol] / peaks[logical_symbol] - 1.0
                    if profit is not None and peaks[logical_symbol] > 1e-12
                    else None
                ),
                "portfolio_total_asset_cny": float(row["total_asset_cny"]),
                "portfolio_flow_cny": float(row["flow_cny"] or 0.0),
                "portfolio_daily_return": float(row["daily_return"] or 0.0),
                "portfolio_cumulative_return": float(row["cumulative_return"] or 0.0),
                "portfolio_drawdown": float(row["drawdown"] or 0.0),
            }
            previous_values[logical_symbol] = current_value
    return result


def _cash_bucket_rows(
    conn,
    run_id: str | None,
    repo_option: dict[str, Any],
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    if not run_id:
        return []
    metrics = _stored_backtest_daily_metrics(
        conn,
        run_id,
        [],
        end_date,
        include_cash=True,
    )
    rows = conn.execute(
        """
        SELECT trade_date,payload_json
        FROM portfolio_daily
        WHERE run_id=? AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        (run_id, start_date, end_date),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        trade_date = str(row["trade_date"])
        payload = json_loads(row["payload_json"], {})
        cash_cny = float(payload.get("cash_cny", 0.0) or 0.0)
        receivable_cny = float(payload.get("dividend_receivable_cny", 0.0) or 0.0)
        repo_lots = int(payload.get("repo_lots", 0) or 0)
        repo_symbol = str(payload.get("treasury_instrument") or repo_option.get("symbol") or "REPO")
        result.append(
            {
                "trade_date": trade_date,
                "logical_symbol": "REPO",
                "logical_name": "现金部分（含逆回购及应收分红）",
                "route_type": "现金管理",
                "actual_symbol": repo_symbol,
                "actual_name": str(repo_option.get("name") or repo_symbol),
                "raw_data_symbol": repo_symbol,
                "raw_close": None,
                "adj_factor": None,
                "split_scale": None,
                "total_multiplier": None,
                "used_close": None,
                "event_note": (
                    f"现金余额={cash_cny:.2f}元；应收分红={receivable_cny:.2f}元；"
                    f"持有逆回购合约={repo_lots}笔"
                ),
                "currency": "CNY",
                "source": "portfolio_daily",
                **metrics.get((trade_date, "REPO"), {}),
            }
        )
    return result


def _raw_route_price_rows(
    conn,
    config: dict[str, Any],
    selected_assets: list[dict[str, Any]],
    start_date: str,
    end_date: str,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    sim_assets = simulation_assets(selected_assets)
    symbols = [str(asset["symbol"]) for asset in sim_assets]
    if not symbols:
        return []
    # Price continuity is path dependent: a split multiplier and the route in
    # force immediately before a narrow export window both come from earlier
    # observations.  Always reconstruct from the original backtest start, then
    # emit only the user-selected dates.
    calculation_start = str(config["start_date"])
    placeholders = ",".join("?" for _ in symbols)
    rows = conn.execute(
        f"""
        SELECT prices.symbol,prices.trade_date,prices.close,prices.currency,prices.source,
               adj_factors.adj_factor
        FROM prices
        LEFT JOIN adj_factors
          ON adj_factors.symbol=prices.symbol AND adj_factors.trade_date=prices.trade_date
        WHERE prices.symbol IN ({placeholders}) AND prices.trade_date BETWEEN ? AND ?
        ORDER BY prices.trade_date,prices.symbol
        """,
        (*symbols, calculation_start, end_date),
    ).fetchall()
    raw_by_symbol: dict[str, dict[str, dict[str, Any]]] = {symbol: {} for symbol in symbols}
    for row in rows:
        raw_by_symbol[str(row["symbol"])][str(row["trade_date"])] = {
            "close": float(row["close"]),
            "currency": str(row["currency"] or "CNY"),
            "source": str(row["source"] or ""),
            "adj_factor": float(row["adj_factor"]) if row["adj_factor"] not in (None, 0) else None,
        }
    share_scale_maps: dict[str, dict[str, float]] = {}
    price_maps = load_price_map(
        conn,
        symbols,
        calculation_start,
        end_date,
        share_splits=configured_share_splits(selected_assets),
        share_scale_maps=share_scale_maps,
    )
    attach_proxy_price_maps(price_maps, selected_assets)
    attach_nontradable_route_expense_drag(price_maps, selected_assets)
    names = {str(asset["symbol"]): _asset_label(asset) for asset in sim_assets}
    asset_by_symbol = {str(asset["symbol"]): asset for asset in sim_assets}
    prepared_routes = prepare_active_asset_routes({**config, "assets": selected_assets})
    all_dates = sorted({trade_date for values in price_maps.values() for trade_date in values})
    latest: dict[str, float | None] = {symbol: None for symbol in symbols}
    previous_routes: dict[str, str] = {}
    daily_metrics = _stored_backtest_daily_metrics(conn, run_id, selected_assets, end_date)
    configured_splits = configured_share_splits(selected_assets)
    result: list[dict[str, Any]] = []
    for trade_date in all_dates:
        for symbol, values in price_maps.items():
            if trade_date in values:
                latest[symbol] = values[trade_date]
        routes = active_route_symbols(parse_date(trade_date), latest, prepared_routes)
        for asset in selected_assets:
            logical_symbol = str(asset["symbol"])
            route_symbol = routes.get(logical_symbol)
            used_close = price_maps.get(route_symbol or "", {}).get(trade_date)
            if route_symbol is None or used_close is None:
                continue
            proxy_symbol = (
                str(asset["price_fallback"]["symbol"])
                if isinstance(asset.get("price_fallback"), dict) and asset["price_fallback"].get("symbol")
                else None
            )
            route_type = "上市前代理" if route_symbol == proxy_symbol else (
                "替换ETF" if route_symbol != logical_symbol else "原始标的"
            )
            raw_data_symbol = route_symbol
            raw = raw_by_symbol.get(route_symbol, {}).get(trade_date)
            if raw is None and route_symbol == proxy_symbol:
                raw_data_symbol = logical_symbol
                raw = raw_by_symbol.get(logical_symbol, {}).get(trade_date)
            raw_close = float(raw["close"]) if raw else None
            split_scale = share_scale_maps.get(raw_data_symbol, {}).get(trade_date, 1.0)
            total_multiplier = (
                float(used_close) / raw_close
                if raw_close is not None and abs(raw_close) > 1e-18
                else None
            )
            notes: list[str] = []
            configured_split = configured_splits.get(raw_data_symbol, {}).get(trade_date)
            if configured_split:
                notes.append(f"基金份额拆分/合并，连续价倍率={configured_split:g}")
            previous_route = previous_routes.get(logical_symbol)
            if previous_route and previous_route != route_symbol:
                notes.append(f"路线切换 {previous_route}→{route_symbol}，禁止跨代码直接比较价格")
            if route_type == "上市前代理":
                notes.append("上市前代理行情")
            if start_date <= trade_date <= end_date:
                metric = daily_metrics.get((trade_date, logical_symbol), {})
                route_asset = asset_by_symbol.get(route_symbol, {})
                result.append(
                    {
                        "trade_date": trade_date,
                        "logical_symbol": logical_symbol,
                        "logical_name": _asset_label(asset),
                        "route_type": route_type,
                        "actual_symbol": route_symbol,
                        "actual_name": names.get(route_symbol, route_symbol),
                        "raw_data_symbol": raw_data_symbol,
                        "raw_close": raw_close,
                        "adj_factor": raw.get("adj_factor") if raw else None,
                        "split_scale": split_scale,
                        "total_multiplier": total_multiplier,
                        "used_close": float(used_close),
                        "event_note": "；".join(notes),
                        "currency": raw.get("currency") if raw else str(route_asset.get("currency") or "CNY"),
                        "source": raw.get("source") if raw else "",
                        **metric,
                    }
                )
            previous_routes[logical_symbol] = route_symbol
    return result


def _raw_repo_rate_rows(
    conn,
    symbol: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    return [
        {
            "trade_date": str(row["trade_date"]),
            "symbol": str(row["symbol"]),
            "close_rate": float(row["close_rate"]),
            "source": str(row["source"] or ""),
        }
        for row in conn.execute(
            """
            SELECT symbol, trade_date, close_rate, source
            FROM repo_rates
            WHERE symbol=? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            (symbol, start_date, end_date),
        ).fetchall()
    ]


def _csv_number(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return "" if value is None else format(float(value), ".12g")


def build_backtest_csv(
    conn,
    user_config: dict[str, Any],
    stored_summary: dict[str, Any],
    run_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    symbols: list[str] | None = None,
) -> tuple[bytes, str]:
    config, configuration_adjustments = _normalize_historical_config(user_config)
    configured_start = str(config["start_date"])
    configured_end = str(config["end_date"])
    selected_start = str(start_date or configured_start)
    selected_end = str(end_date or configured_end)
    try:
        start_day = parse_date(selected_start)
        end_day = parse_date(selected_end)
    except ValueError as exc:
        raise ValueError("导出时间必须使用 YYYY-MM-DD 格式") from exc
    if start_day > end_day:
        raise ValueError("导出开始时间不能晚于结束时间")
    if selected_start < configured_start or selected_end > configured_end:
        raise ValueError("导出时间必须位于原回测配置区间内")

    available_assets = {str(asset["symbol"]): asset for asset in _selected_assets(config)}
    repo_option = selected_repo_option(config)
    selected_repo_symbol = (
        str(repo_option["symbol"])
        if repo_option.get("instrument_type", "repo") == "repo"
        else None
    )
    available_symbols = [*available_assets]
    available_symbols.append("REPO")
    if selected_repo_symbol:
        available_symbols.append(selected_repo_symbol)
    requested_symbols = list(dict.fromkeys(symbol for symbol in (symbols or available_symbols) if symbol))
    invalid = [symbol for symbol in requested_symbols if symbol not in available_symbols]
    if invalid:
        raise ValueError(f"导出标的不属于当前回测：{', '.join(invalid)}")
    if not requested_symbols:
        raise ValueError("至少选择一个导出标的")
    selected_assets = [available_assets[symbol] for symbol in requested_symbols if symbol in available_assets]
    include_cash = "REPO" in requested_symbols
    include_repo = bool(selected_repo_symbol and selected_repo_symbol in requested_symbols)
    price_rows = _raw_route_price_rows(
        conn,
        config,
        selected_assets,
        selected_start,
        selected_end,
        run_id=run_id,
    )
    if include_cash:
        price_rows.extend(
            _cash_bucket_rows(
                conn,
                run_id,
                repo_option,
                selected_start,
                selected_end,
            )
        )
    row_order = {symbol: index for index, symbol in enumerate(requested_symbols)}
    price_rows.sort(key=lambda row: (row["trade_date"], row_order.get(row["logical_symbol"], len(row_order))))
    repo_rate_rows = (
        _raw_repo_rate_rows(conn, selected_repo_symbol, selected_start, selected_end)
        if include_repo and selected_repo_symbol
        else []
    )

    use_stored_summary = selected_start == configured_start and selected_end == configured_end
    if use_stored_summary:
        result_summary = stored_summary
    else:
        export_config = deepcopy(config)
        export_config["start_date"] = selected_start
        export_config["end_date"] = selected_end
        export_config["rebalance_month_analysis_enabled"] = False
        result_summary = _run_summary(conn, export_config)

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["选择开始时间", selected_start])
    writer.writerow(["选择结束时间", selected_end])
    writer.writerow(
        [
            "选择标的",
            "；".join(
                f"{symbol} "
                + (
                    _asset_label(available_assets[symbol])
                    if symbol in available_assets
                    else "现金部分（含逆回购及应收分红）"
                    if symbol == "REPO"
                    else str(repo_option.get("name") or symbol)
                )
                for symbol in requested_symbols
            ),
        ]
    )
    if configuration_adjustments:
        writer.writerow(["配置兼容调整", "；".join(configuration_adjustments)])
    writer.writerow(
        [
            "价格与收益口径",
            "原始收盘价仅供核对；收益和回撤以回测使用价、资产桶和组合列为准，禁止跨路线代码直接比较价格；累计收益和回撤为原回测全程截至当日口径，底部区间结果按导出日期重算",
        ]
    )
    writer.writerow([])
    writer.writerow(
        [
            "交易日",
            "选择标的代码",
            "标的名称",
            "路线类型",
            "实际行情代码",
            "实际行情名称",
            "原始数据代码",
            "原始收盘价",
            "行情复权因子",
            "拆分连续倍率",
            "回测价格总倍率",
            "回测使用收盘价",
            "事件说明",
            "资产桶市值(元)",
            "资产桶单日盈亏(元)",
            "资产桶单日收益(小数)",
            "资产桶累计收益(小数)",
            "资产桶回撤(小数)",
            "组合总资产(元)",
            "外部现金流(元)",
            "组合单日收益(小数)",
            "组合累计收益(小数)",
            "组合回撤(小数)",
            "币种",
            "数据源",
        ]
    )
    for row in price_rows:
        writer.writerow(
            [
                row["trade_date"],
                row["logical_symbol"],
                row["logical_name"],
                row["route_type"],
                row["actual_symbol"],
                row["actual_name"],
                row["raw_data_symbol"],
                _csv_number(row, "raw_close"),
                _csv_number(row, "adj_factor"),
                _csv_number(row, "split_scale"),
                _csv_number(row, "total_multiplier"),
                _csv_number(row, "used_close"),
                row["event_note"],
                _csv_number(row, "asset_value_cny"),
                _csv_number(row, "asset_daily_profit_cny"),
                _csv_number(row, "asset_daily_return"),
                _csv_number(row, "asset_cumulative_return"),
                _csv_number(row, "asset_drawdown"),
                _csv_number(row, "portfolio_total_asset_cny"),
                _csv_number(row, "portfolio_flow_cny"),
                _csv_number(row, "portfolio_daily_return"),
                _csv_number(row, "portfolio_cumulative_return"),
                _csv_number(row, "portfolio_drawdown"),
                row["currency"],
                row["source"],
            ]
        )
    if include_repo:
        writer.writerow([])
        writer.writerow(["逆回购交易日", "逆回购代码", "逆回购名称", "收盘年化利率(%)", "数据源"])
        for row in repo_rate_rows:
            writer.writerow(
                [
                    row["trade_date"],
                    row["symbol"],
                    repo_option.get("name") or row["symbol"],
                    format(float(row["close_rate"]), ".10g"),
                    row["source"],
                ]
            )
    writer.writerow([])
    writer.writerow(["最终回测结果", "指标", "数值"])
    summary_rows = (
        ("实际回测开始", result_summary.get("start_date")),
        ("实际回测结束", result_summary.get("end_date")),
        ("期末总资产", result_summary.get("final_asset_cny")),
        ("净盈亏", result_summary.get("net_profit_cny")),
        ("累计收益", result_summary.get("total_return")),
        ("年化收益", result_summary.get("annualized_return")),
        ("最大回撤", result_summary.get("max_drawdown")),
        ("收益/回撤", result_summary.get("annual_return_drawdown_ratio")),
        ("正收益年份", result_summary.get("positive_year_count")),
        ("完整年份", result_summary.get("complete_year_count")),
        ("总费用", result_summary.get("total_fees_cny")),
        ("交易次数", result_summary.get("trade_count")),
    )
    percentage_metrics = {"累计收益", "年化收益", "最大回撤"}
    for label, value in summary_rows:
        if label in percentage_metrics and value is not None:
            formatted = f"{float(value):.6%}"
        elif isinstance(value, float):
            formatted = format(value, ".10g")
        else:
            formatted = "" if value is None else str(value)
        writer.writerow(["最终回测结果", label, formatted])
    filename = f"permanent-investment-{selected_start.replace('-', '')}-{selected_end.replace('-', '')}.csv"
    return ("\ufeff" + output.getvalue()).encode("utf-8"), filename
