from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import math
import uuid
from typing import Any

import numpy as np

from app.config import normalize_config, validate_config
from app.db import insert_many, json_dumps, utc_now
from app.services.calendar import add_business_days, business_days, first_business_day_by_month, parse_date, rebalance_days, repo_actual_days
from app.services.fees import (
    CnEtfFeeConfig,
    FxFeeConfig,
    IbkrFeeConfig,
    RepoFeeConfig,
    cn_etf_fee,
    cny_to_usd,
    dict_to_dataclass,
    ibkr_us_etf_fee,
    repo_fee,
    repo_interest,
    usd_to_cny,
)


@dataclass
class Position:
    symbol: str
    market: str
    currency: str
    asset_type: str
    quantity: float = 0.0
    cost_basis_cny: float = 0.0
    realized_pnl_cny: float = 0.0


@dataclass
class RepoLot:
    principal: float
    maturity_date: date
    interest: float
    fee: float


@dataclass
class PortfolioState:
    cash_cny: float
    positions: dict[str, Position] = field(default_factory=dict)
    repo_lots: list[RepoLot] = field(default_factory=list)
    dividend_receivable_cny: float = 0.0
    total_fees_cny: float = 0.0
    total_spend_cny: float = 0.0
    total_withheld_tax_cny: float = 0.0


class BacktestError(ValueError):
    pass


def load_price_map(conn, symbols: list[str], start: str, end: str) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for symbol in symbols:
        rows = conn.execute(
            """
            SELECT trade_date, close FROM prices
            WHERE symbol=? AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            (symbol, start, end),
        ).fetchall()
        result[symbol] = {row["trade_date"]: float(row["close"]) for row in rows}
    return result


def load_fx_map(conn, start: str, end: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT trade_date, rate FROM fx_rates WHERE pair='USD/CNY' AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (start, end),
    ).fetchall()
    return {row["trade_date"]: float(row["rate"]) for row in rows}


def load_repo_map(conn, symbol: str, start: str, end: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT trade_date, close_rate FROM repo_rates WHERE symbol=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (symbol, start, end),
    ).fetchall()
    return {row["trade_date"]: float(row["close_rate"]) for row in rows}


def load_dividend_events(conn, symbols: list[str], start: str, end: str) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    ex_events: dict[str, list[dict[str, Any]]] = {}
    pay_events: dict[str, list[dict[str, Any]]] = {}
    if not symbols:
        return ex_events, pay_events
    placeholders = ",".join("?" for _ in symbols)
    rows = conn.execute(
        f"""
        SELECT symbol, ex_date, pay_date, div_cash, currency FROM fund_dividends
        WHERE symbol IN ({placeholders}) AND ex_date BETWEEN ? AND ?
        ORDER BY ex_date
        """,
        (*symbols, start, end),
    ).fetchall()
    for row in rows:
        event = dict(row)
        ex_events.setdefault(row["ex_date"], []).append(event)
        pay_events.setdefault(row["pay_date"], []).append(event)
    return ex_events, pay_events


def forward_value(values: dict[str, float], day: date, last: float | None) -> float | None:
    current = values.get(day.isoformat())
    return current if current is not None else last


def position_value_cny(position: Position, price: float, fx: float) -> float:
    value = position.quantity * price
    if position.currency == "USD":
        value *= fx
    return value


def active_assets(config: dict[str, Any], day: date, latest_prices: dict[str, float | None]) -> list[dict[str, Any]]:
    result = []
    for asset in config["assets"]:
        inception = parse_date(asset.get("inception_date") or config["start_date"])
        if asset.get("enabled", True) and day >= inception and latest_prices.get(asset["symbol"]) is not None:
            result.append(asset)
    return result


def effective_weights(config: dict[str, Any], day: date, latest_prices: dict[str, float | None]) -> dict[str, float]:
    weights: dict[str, float] = {}
    total_risk = 0.0
    for asset in active_assets(config, day, latest_prices):
        weight = float(asset.get("target_weight", 0.0))
        weights[asset["symbol"]] = weight
        total_risk += weight
    weights["REPO"] = max(1.0 - total_risk, 0.0)
    return weights


def should_rebalance(current_weights: dict[str, float], targets: dict[str, float], band: float) -> bool:
    keys = set(current_weights) | set(targets)
    return any(abs(current_weights.get(key, 0.0) - targets.get(key, 0.0)) > band for key in keys)


def has_investable_asset_target(targets: dict[str, float]) -> bool:
    return any(key != "REPO" and value > 1e-10 for key, value in targets.items())


def minimal_rebalance_weights(current_weights: dict[str, float], targets: dict[str, float], band: float) -> dict[str, float]:
    keys = sorted(set(current_weights) | set(targets))
    if "REPO" in keys:
        keys.remove("REPO")
        keys.insert(0, "REPO")

    lower: dict[str, float] = {}
    upper: dict[str, float] = {}
    desired: dict[str, float] = {}
    for key in keys:
        target = max(float(targets.get(key, 0.0)), 0.0)
        lower[key] = max(target - band, 0.0)
        upper[key] = min(target + band, 1.0)
        desired[key] = min(max(float(current_weights.get(key, 0.0)), lower[key]), upper[key])

    residual = 1.0 - sum(desired.values())
    if residual > 0:
        for key in keys:
            room = upper[key] - desired[key]
            add = min(room, residual)
            if add > 0:
                desired[key] += add
                residual -= add
            if residual <= 1e-10:
                break
    elif residual < 0:
        residual = -residual
        for key in keys:
            room = desired[key] - lower[key]
            take = min(room, residual)
            if take > 0:
                desired[key] -= take
                residual -= take
            if residual <= 1e-10:
                break

    total = sum(desired.values())
    if total > 0 and abs(total - 1.0) > 1e-8:
        desired = {key: value / total for key, value in desired.items()}
    return {key: value for key, value in desired.items() if value > 1e-10}


def compute_metrics(total_assets: list[float], flows: list[float], benchmark: list[float]) -> tuple[list[float], list[float], list[float]]:
    daily_returns: list[float] = [0.0]
    cumulative: list[float] = [0.0]
    drawdowns: list[float] = [0.0]
    nav = 1.0
    peak = 1.0
    for idx in range(1, len(total_assets)):
        previous = total_assets[idx - 1]
        ret = 0.0 if previous == 0 else (total_assets[idx] - previous - flows[idx]) / previous
        nav *= 1.0 + ret
        peak = max(peak, nav)
        daily_returns.append(ret)
        cumulative.append(nav - 1.0)
        drawdowns.append(nav / peak - 1.0)
    return daily_returns, cumulative, drawdowns


def benchmark_returns(benchmark_values: list[float | None]) -> list[float]:
    clean = [value for value in benchmark_values if value is not None]
    if not clean:
        return [0.0 for _ in benchmark_values]
    base = clean[0]
    last = base
    returns = []
    for value in benchmark_values:
        if value is not None:
            last = value
        returns.append(last / base - 1.0 if base else 0.0)
    return returns


def _sell_position(
    state: PortfolioState,
    pos: Position,
    day: date,
    qty: float,
    price: float,
    fx: float,
    fees: dict[str, Any],
    trades: list[dict[str, Any]],
    reason: str,
) -> float:
    qty = min(qty, pos.quantity)
    if qty <= 0:
        return 0.0
    gross_native = qty * price
    if pos.currency == "USD":
        ibkr_fee = ibkr_us_etf_fee(qty, gross_native, dict_to_dataclass(IbkrFeeConfig, fees["ibkr_us_etf"]))
        net_usd = max(gross_native - ibkr_fee, 0.0)
        cash_cny, fx_fee = usd_to_cny(net_usd, fx, dict_to_dataclass(FxFeeConfig, fees["fx"]), include_wire=False)
        fee_cny = ibkr_fee * fx + fx_fee
    else:
        fee_cny = cn_etf_fee(gross_native, dict_to_dataclass(CnEtfFeeConfig, fees["cn_etf"]))
        cash_cny = max(gross_native - fee_cny, 0.0)
    cost_sold = pos.cost_basis_cny * (qty / pos.quantity) if pos.quantity else 0.0
    pos.quantity -= qty
    pos.cost_basis_cny -= cost_sold
    pos.realized_pnl_cny += cash_cny - cost_sold
    state.cash_cny += cash_cny
    state.total_fees_cny += fee_cny
    trades.append(
        {
            "trade_date": day.isoformat(),
            "symbol": pos.symbol,
            "side": "SELL",
            "quantity": qty,
            "price": price,
            "gross_amount": gross_native,
            "fee": fee_cny,
            "currency": pos.currency,
            "reason": reason,
            "payload_json": json_dumps({"cash_cny": cash_cny, "fx": fx}),
        }
    )
    return cash_cny


def _buy_position(
    state: PortfolioState,
    pos: Position,
    day: date,
    budget_cny: float,
    price: float,
    fx: float,
    fees: dict[str, Any],
    trades: list[dict[str, Any]],
    allow_fractional_us_shares: bool,
    reason: str,
) -> float:
    budget_cny = min(max(budget_cny, 0.0), state.cash_cny)
    if budget_cny <= 0:
        return 0.0
    if pos.currency == "USD":
        fx_cfg = dict_to_dataclass(FxFeeConfig, fees["fx"])
        usd_budget, fx_fee = cny_to_usd(budget_cny, fx, fx_cfg, include_wire=False)
        raw_qty = usd_budget / price if price else 0.0
        qty = raw_qty if allow_fractional_us_shares else math.floor(raw_qty)
        gross_usd = qty * price
        ibkr_cfg = dict_to_dataclass(IbkrFeeConfig, fees["ibkr_us_etf"])
        commission_usd = ibkr_us_etf_fee(qty, gross_usd, ibkr_cfg) if qty > 0 else 0.0
        if gross_usd + commission_usd > usd_budget and price > 0:
            qty = max((usd_budget - commission_usd) / price, 0.0) if allow_fractional_us_shares else max(math.floor((usd_budget - commission_usd) / price), 0)
            gross_usd = qty * price
            commission_usd = ibkr_us_etf_fee(qty, gross_usd, ibkr_cfg) if qty > 0 else 0.0
        spent_cny = (gross_usd + commission_usd) * fx + fx_fee
        fee_cny = commission_usd * fx + fx_fee
        gross_native = gross_usd
    else:
        lot_size = 100
        raw_qty = math.floor((budget_cny / price) / lot_size) * lot_size if price else 0
        qty = max(raw_qty, 0)
        gross_native = qty * price
        fee_cny = cn_etf_fee(gross_native, dict_to_dataclass(CnEtfFeeConfig, fees["cn_etf"])) if qty else 0.0
        while qty > 0 and gross_native + fee_cny > budget_cny:
            qty -= lot_size
            gross_native = qty * price
            fee_cny = cn_etf_fee(gross_native, dict_to_dataclass(CnEtfFeeConfig, fees["cn_etf"])) if qty else 0.0
        spent_cny = gross_native + fee_cny
    if qty <= 0 or spent_cny <= 0:
        return 0.0
    state.cash_cny -= spent_cny
    state.total_fees_cny += fee_cny
    pos.cost_basis_cny += spent_cny
    pos.quantity += qty
    trades.append(
        {
            "trade_date": day.isoformat(),
            "symbol": pos.symbol,
            "side": "BUY",
            "quantity": qty,
            "price": price,
            "gross_amount": gross_native,
            "fee": fee_cny,
            "currency": pos.currency,
            "reason": reason,
            "payload_json": json_dumps({"spent_cny": spent_cny, "fx": fx}),
        }
    )
    return spent_cny


def _portfolio_value(
    state: PortfolioState,
    latest_prices: dict[str, float | None],
    fx: float,
) -> tuple[float, dict[str, float]]:
    values: dict[str, float] = {}
    for symbol, pos in state.positions.items():
        price = latest_prices.get(symbol)
        values[symbol] = position_value_cny(pos, price or 0.0, fx) if price is not None else 0.0
    repo_value = state.cash_cny + sum(lot.principal + lot.interest - lot.fee for lot in state.repo_lots) + state.dividend_receivable_cny
    values["REPO"] = repo_value
    total = sum(values.values())
    return total, values


def _liquidate_repo_to_cash(state: PortfolioState) -> None:
    for lot in state.repo_lots:
        state.cash_cny += lot.principal + lot.interest - lot.fee
        state.total_fees_cny += lot.fee
    state.repo_lots.clear()


def _cover_cash_shortfall(
    state: PortfolioState,
    shortfall: float,
    day: date,
    latest_prices: dict[str, float | None],
    fx: float,
    fees: dict[str, Any],
    trades: list[dict[str, Any]],
) -> None:
    _liquidate_repo_to_cash(state)
    if state.cash_cny >= shortfall:
        return
    ranked = sorted(
        state.positions.values(),
        key=lambda p: position_value_cny(p, latest_prices.get(p.symbol) or 0.0, fx),
        reverse=True,
    )
    for pos in ranked:
        price = latest_prices.get(pos.symbol)
        if not price or pos.quantity <= 0:
            continue
        value = position_value_cny(pos, price, fx)
        needed = shortfall - state.cash_cny
        if needed <= 0:
            break
        qty = min(pos.quantity, pos.quantity * min(1.0, needed / max(value, 1e-9) * 1.05))
        if pos.currency == "CNY":
            qty = math.ceil(qty / 100) * 100
        _sell_position(state, pos, day, qty, price, fx, fees, trades, "liquidity_shortfall")


def _rebalance_state_to_band(
    state: PortfolioState,
    assets: list[dict[str, Any]],
    day: date,
    latest_prices: dict[str, float | None],
    fx: float,
    fees: dict[str, Any],
    trades: list[dict[str, Any]],
    allow_fractional_us_shares: bool,
    targets: dict[str, float],
    band: float,
) -> tuple[float, float, float, float, dict[str, float]]:
    _liquidate_repo_to_cash(state)
    before_rebalance, values = _portfolio_value(state, latest_prices, fx)
    current_weights = {key: (value / before_rebalance if before_rebalance else 0.0) for key, value in values.items()}
    desired_weights = minimal_rebalance_weights(current_weights, targets, band)
    turnover = 0.0
    fee_before = state.total_fees_cny

    for asset in assets:
        symbol = asset["symbol"]
        pos = state.positions[symbol]
        price = latest_prices.get(symbol)
        if price is None:
            continue
        desired_value = before_rebalance * desired_weights.get(symbol, 0.0)
        current_value = position_value_cny(pos, price, fx)
        diff = desired_value - current_value
        if diff < -1.0:
            sell_value = -diff
            qty = sell_value / (price * (fx if pos.currency == "USD" else 1.0))
            if pos.currency == "CNY":
                qty = math.floor(qty / 100) * 100
            prev_cash = state.cash_cny
            _sell_position(state, pos, day, qty, price, fx, fees, trades, "rebalance")
            turnover += max(state.cash_cny - prev_cash, 0.0)

    for asset in assets:
        symbol = asset["symbol"]
        pos = state.positions[symbol]
        price = latest_prices.get(symbol)
        if price is None:
            continue
        after_sell_total, _ = _portfolio_value(state, latest_prices, fx)
        desired_value = after_sell_total * desired_weights.get(symbol, 0.0)
        current_value = position_value_cny(pos, price, fx)
        diff = desired_value - current_value
        if diff > 1.0:
            spent = _buy_position(
                state,
                pos,
                day,
                diff,
                price,
                fx,
                fees,
                trades,
                allow_fractional_us_shares,
                "rebalance",
            )
            turnover += spent

    after_rebalance, _ = _portfolio_value(state, latest_prices, fx)
    return before_rebalance, after_rebalance, turnover, state.total_fees_cny - fee_before, desired_weights


def repo_tenor_days(config: dict[str, Any]) -> int:
    symbol = config.get("repo_symbol", "204001")
    for option in config.get("repo_options", []):
        if option.get("symbol") == symbol:
            return int(option.get("tenor_days") or 1)
    try:
        return int(str(symbol)[-3:])
    except ValueError:
        return 1


def _repo_spend_reserve(day: date, tenor_days: int, monthly_spend_days: set[date], monthly_spend_cny: float) -> float:
    maturity = add_business_days(day, tenor_days)
    spend_count = sum(1 for spend_day in monthly_spend_days if day < spend_day < maturity)
    return spend_count * monthly_spend_cny


def _invest_idle_cash_in_repo(
    state: PortfolioState,
    day: date,
    repo_rate: float | None,
    fees: dict[str, Any],
    tenor_days: int,
    reserve_cny: float = 0.0,
) -> None:
    if repo_rate is None:
        return
    lot_size = float(fees["repo"].get("lot_size_cny", 1000.0))
    investable_cash = max(state.cash_cny - max(reserve_cny, 0.0), 0.0)
    investable = math.floor(investable_cash / lot_size) * lot_size
    if investable >= lot_size:
        actual_days = repo_actual_days(day, tenor_days)
        interest = repo_interest(investable, repo_rate, actual_days)
        fee = repo_fee(investable, dict_to_dataclass(RepoFeeConfig, fees["repo"]))
        state.cash_cny -= investable
        state.repo_lots.append(RepoLot(investable, add_business_days(day, tenor_days), interest, fee))


def _next_spend_reserve(day: date, monthly_spend_days: set[date], monthly_spend_cny: float) -> float:
    next_day = add_business_days(day, 1)
    return monthly_spend_cny if next_day in monthly_spend_days else 0.0


def _invest_repo_cash(
    state: PortfolioState,
    day: date,
    selected_repo_rate: float | None,
    one_day_repo_rate: float | None,
    fees: dict[str, Any],
    selected_tenor_days: int,
    monthly_spend_days: set[date],
    monthly_spend_cny: float,
) -> None:
    reserve_cny = _repo_spend_reserve(day, selected_tenor_days, monthly_spend_days, monthly_spend_cny)
    _invest_idle_cash_in_repo(state, day, selected_repo_rate, fees, selected_tenor_days, reserve_cny)
    overnight_reserve_cny = _next_spend_reserve(day, monthly_spend_days, monthly_spend_cny)
    _invest_idle_cash_in_repo(state, day, one_day_repo_rate, fees, 1, overnight_reserve_cny)


def _mature_repo_lots(state: PortfolioState, day: date) -> None:
    matured = [lot for lot in state.repo_lots if lot.maturity_date <= day]
    state.repo_lots = [lot for lot in state.repo_lots if lot.maturity_date > day]
    for lot in matured:
        state.cash_cny += lot.principal + lot.interest - lot.fee
        state.total_fees_cny += lot.fee


def _apply_dividend_events(
    state: PortfolioState,
    day_str: str,
    ex_events: dict[str, list[dict[str, Any]]],
    pay_events: dict[str, list[dict[str, Any]]],
    fx: float,
    fees: dict[str, Any],
) -> None:
    for event in ex_events.get(day_str, []):
        pos = state.positions.get(event["symbol"])
        if not pos or pos.quantity <= 0:
            continue
        dividend = pos.quantity * float(event["div_cash"])
        if event["currency"] == "USD":
            tax_rate = float(fees["tax"].get("us_dividend_withholding_rate", 0.10))
            tax = dividend * tax_rate * fx
            state.total_withheld_tax_cny += tax
            state.dividend_receivable_cny += dividend * (1.0 - tax_rate) * fx
        else:
            tax_rate = float(fees["tax"].get("cn_fund_dividend_tax_rate", 0.0))
            tax = dividend * tax_rate
            state.total_withheld_tax_cny += tax
            state.dividend_receivable_cny += dividend * (1.0 - tax_rate)

    for _event in pay_events.get(day_str, []):
        if state.dividend_receivable_cny > 0:
            state.cash_cny += state.dividend_receivable_cny
            state.dividend_receivable_cny = 0.0


def _initial_state(capital_cny: float, assets: list[dict[str, Any]]) -> PortfolioState:
    state = PortfolioState(cash_cny=capital_cny)
    for asset in assets:
        state.positions[asset["symbol"]] = Position(
            symbol=asset["symbol"],
            market=asset["market"],
            currency=asset["currency"],
            asset_type=asset.get("asset_type", "etf"),
        )
    return state


def _asset_period_performance(
    previous_values: dict[str, float],
    current_values: dict[str, float],
    ordered_symbols: list[str],
    external_flows: dict[str, float] | None = None,
) -> dict[str, dict[str, float | None]]:
    flows = external_flows or {}
    result: dict[str, dict[str, float | None]] = {}
    keys = [symbol for symbol in ordered_symbols if symbol in previous_values or symbol in current_values]
    for key in sorted((set(previous_values) | set(current_values)) - set(keys)):
        keys.append(key)
    for key in keys:
        start_value = float(previous_values.get(key, 0.0) or 0.0)
        end_value = float(current_values.get(key, 0.0) or 0.0)
        external_flow = float(flows.get(key, 0.0) or 0.0)
        profit = end_value - start_value - external_flow
        result[key] = {
            "start_value_cny": start_value,
            "end_value_cny": end_value,
            "external_flow_cny": external_flow,
            "profit_cny": profit,
            "return": (profit / start_value) if start_value > 0 else None,
        }
    return result


def comparison_assets(config: dict[str, Any]) -> list[dict[str, Any]]:
    by_symbol = {asset["symbol"]: asset for asset in config["assets"]}
    hs300_weight = sum(
        float(by_symbol[symbol].get("target_weight", 0.0))
        for symbol in ("VOO", "510300.SH", "512890.SH")
        if by_symbol.get(symbol, {}).get("enabled", True)
    )
    result: list[dict[str, Any]] = []
    hs300 = by_symbol.get("510300.SH")
    if hs300 and hs300_weight > 0:
        result.append({**hs300, "target_weight": hs300_weight, "enabled": True})
    gold = by_symbol.get("518880.SH")
    if gold and gold.get("enabled", True) and float(gold.get("target_weight", 0.0)) > 0:
        result.append({**gold, "enabled": True})
    return result


def _simulate_comparison_series(
    config: dict[str, Any],
    days: list[date],
    price_maps: dict[str, dict[str, float]],
    fx_map: dict[str, float],
    repo_map: dict[str, float],
    one_day_repo_map: dict[str, float],
    ex_events: dict[str, list[dict[str, Any]]],
    pay_events: dict[str, list[dict[str, Any]]],
    monthly_spend_days: set[date],
    reb_days: set[date],
) -> dict[str, float]:
    assets = comparison_assets(config)
    state = _initial_state(float(config["initial_capital_cny"]), assets)
    symbols = [asset["symbol"] for asset in assets]
    latest_prices: dict[str, float | None] = {symbol: None for symbol in symbols}
    latest_fx: float | None = None
    latest_repo_rate: float | None = None
    latest_one_day_repo_rate: float | None = None
    tenor_days = repo_tenor_days(config)
    totals: dict[str, float] = {}
    trades: list[dict[str, Any]] = []
    initial_rebalance_done = False

    for day in days:
        day_str = day.isoformat()
        for symbol in latest_prices:
            latest_prices[symbol] = forward_value(price_maps.get(symbol, {}), day, latest_prices.get(symbol))
        latest_fx = forward_value(fx_map, day, latest_fx)
        repo_rate = repo_map.get(day_str)
        if repo_rate is not None:
            latest_repo_rate = repo_rate
        one_day_repo_rate = one_day_repo_map.get(day_str)
        if one_day_repo_rate is not None:
            latest_one_day_repo_rate = one_day_repo_rate
        if latest_fx is None:
            continue

        _mature_repo_lots(state, day)
        _apply_dividend_events(state, day_str, ex_events, pay_events, latest_fx, config["fees"])

        if day in monthly_spend_days:
            spend = float(config["monthly_spend_cny"])
            if state.cash_cny < spend:
                _cover_cash_shortfall(state, spend, day, latest_prices, latest_fx, config["fees"], trades)
            actual_spend = min(spend, state.cash_cny)
            state.cash_cny -= actual_spend
            state.total_spend_cny += actual_spend

        before_total, before_values = _portfolio_value(state, latest_prices, latest_fx)
        targets = effective_weights({**config, "assets": assets}, day, latest_prices)
        current_weights = {key: (value / before_total if before_total else 0.0) for key, value in before_values.items()}
        is_rebalance_day = day in reb_days or not initial_rebalance_done
        if is_rebalance_day and should_rebalance(current_weights, targets, float(config["rebalance_band"])):
            _rebalance_state_to_band(
                state,
                assets,
                day,
                latest_prices,
                latest_fx,
                config["fees"],
                trades,
                False,
                targets,
                float(config["rebalance_band"]),
            )
        if is_rebalance_day and has_investable_asset_target(targets):
            initial_rebalance_done = True

        _invest_repo_cash(
            state,
            day,
            latest_repo_rate,
            latest_one_day_repo_rate,
            config["fees"],
            tenor_days,
            monthly_spend_days,
            float(config["monthly_spend_cny"]),
        )
        total, _values = _portfolio_value(state, latest_prices, latest_fx)
        totals[day_str] = total
    return totals


def run_backtest(conn, user_config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = normalize_config(user_config)
    errors = validate_config(config)
    if errors:
        raise BacktestError("; ".join(errors))

    start = config["start_date"]
    end = config["end_date"]
    stale_generated = []
    for table, date_col in (
        ("prices", "trade_date"),
        ("fund_dividends", "ex_date"),
        ("adj_factors", "trade_date"),
        ("fx_rates", "trade_date"),
        ("repo_rates", "trade_date"),
    ):
        row = conn.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE source LIKE 'generated:%' AND {date_col} BETWEEN ? AND ?",
            (start, end),
        ).fetchone()
        if row and row["count"]:
            stale_generated.append(f"{table}:{row['count']}")
    if stale_generated:
        raise BacktestError("database contains generated/mock data in the requested range; purge and resync real/public data first: " + ", ".join(stale_generated))
    days = business_days(start, end)
    if len(days) < 2:
        raise BacktestError("backtest needs at least two business days")

    symbols = [asset["symbol"] for asset in config["assets"]]
    benchmark_symbol = "000300.SH"
    price_maps = load_price_map(conn, symbols + [benchmark_symbol], start, end)
    fx_map = load_fx_map(conn, start, end)
    repo_map = load_repo_map(conn, config["repo_symbol"], start, end)
    one_day_repo_map = load_repo_map(conn, "204001", start, end)
    ex_events, pay_events = load_dividend_events(conn, symbols, start, end)

    if not fx_map or not repo_map:
        raise BacktestError("missing fx_rates or repo_rates; run data sync first")

    state = _initial_state(float(config["initial_capital_cny"]), config["assets"])

    run_id = str(uuid.uuid4())
    trades: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    rebalance_rows: list[dict[str, Any]] = []
    daily_total_assets: list[float] = []
    daily_flows: list[float] = []
    benchmark_values: list[float | None] = []

    monthly_spend_days = first_business_day_by_month(days)
    reb_days = rebalance_days(days, config["rebalance_frequency"])
    latest_prices: dict[str, float | None] = {symbol: None for symbol in symbols + [benchmark_symbol]}
    latest_fx: float | None = None
    latest_repo_rate: float | None = None
    latest_one_day_repo_rate: float | None = None
    tenor_days = repo_tenor_days(config)
    period_start_nav = 1.0
    nav_for_period = 1.0
    period_peak_nav = 1.0
    period_max_drawdown = 0.0
    previous_rebalance_values: dict[str, float] = {"REPO": float(config["initial_capital_cny"])}
    performance_symbols = [asset["symbol"] for asset in config["assets"]] + ["REPO"]
    period_external_flows: dict[str, float] = {"REPO": 0.0}
    initial_rebalance_done = False

    for idx, day in enumerate(days):
        day_str = day.isoformat()
        flow = 0.0
        for symbol in latest_prices:
            latest_prices[symbol] = forward_value(price_maps.get(symbol, {}), day, latest_prices.get(symbol))
        latest_fx = forward_value(fx_map, day, latest_fx)
        repo_rate = repo_map.get(day_str)
        if repo_rate is not None:
            latest_repo_rate = repo_rate
        one_day_repo_rate = one_day_repo_map.get(day_str)
        if one_day_repo_rate is not None:
            latest_one_day_repo_rate = one_day_repo_rate
        if latest_fx is None:
            continue

        _mature_repo_lots(state, day)
        _apply_dividend_events(state, day_str, ex_events, pay_events, latest_fx, config["fees"])

        if day in monthly_spend_days:
            spend = float(config["monthly_spend_cny"])
            if state.cash_cny < spend:
                _cover_cash_shortfall(state, spend, day, latest_prices, latest_fx, config["fees"], trades)
            actual_spend = min(spend, state.cash_cny)
            state.cash_cny -= actual_spend
            state.total_spend_cny += actual_spend
            flow -= actual_spend
            period_external_flows["REPO"] = period_external_flows.get("REPO", 0.0) - actual_spend

        before_total, before_values = _portfolio_value(state, latest_prices, latest_fx)
        targets = effective_weights(config, day, latest_prices)
        current_weights = {key: (value / before_total if before_total else 0.0) for key, value in before_values.items()}
        is_rebalance_day = day in reb_days or not initial_rebalance_done
        if is_rebalance_day and should_rebalance(current_weights, targets, float(config["rebalance_band"])):
            event_nav = nav_for_period
            if daily_total_assets and daily_total_assets[-1] != 0:
                event_return = (before_total - daily_total_assets[-1] - flow) / daily_total_assets[-1]
                event_nav *= 1.0 + event_return
            event_peak_nav = max(period_peak_nav, event_nav)
            event_drawdown = event_nav / event_peak_nav - 1.0 if event_peak_nav else 0.0
            event_max_drawdown = min(period_max_drawdown, event_drawdown)
            asset_performance = _asset_period_performance(previous_rebalance_values, before_values, performance_symbols, period_external_flows)
            before_rebalance, after_rebalance, turnover, fee_cny, desired_weights = _rebalance_state_to_band(
                state,
                config["assets"],
                day,
                latest_prices,
                latest_fx,
                config["fees"],
                trades,
                bool(config.get("allow_fractional_us_shares", True)),
                targets,
                float(config["rebalance_band"]),
            )
            _after_total, after_values = _portfolio_value(state, latest_prices, latest_fx)
            previous_total = daily_total_assets[-1] if daily_total_assets else float(config["initial_capital_cny"])
            period_return = event_nav / period_start_nav - 1.0
            rebalance_rows.append(
                {
                    "run_id": run_id,
                    "rebalance_date": day_str,
                    "period_return": period_return,
                    "total_asset_before": before_rebalance,
                    "total_asset_after": after_rebalance,
                    "turnover_cny": turnover,
                    "fee_cny": fee_cny,
                    "payload_json": json_dumps(
                        {
                            "targets": targets,
                            "desired_weights": desired_weights,
                            "asset_performance": asset_performance,
                            "period_max_drawdown": event_max_drawdown,
                            "previous_total": previous_total,
                        }
                    ),
                }
            )
            previous_rebalance_values = after_values
            period_external_flows = {"REPO": 0.0}
            period_start_nav = event_nav
            period_peak_nav = event_nav
            period_max_drawdown = 0.0
        if is_rebalance_day and has_investable_asset_target(targets):
            initial_rebalance_done = True

        _invest_repo_cash(
            state,
            day,
            latest_repo_rate,
            latest_one_day_repo_rate,
            config["fees"],
            tenor_days,
            monthly_spend_days,
            float(config["monthly_spend_cny"]),
        )

        total, values = _portfolio_value(state, latest_prices, latest_fx)
        daily_total_assets.append(total)
        daily_flows.append(flow)
        benchmark_values.append(latest_prices.get(benchmark_symbol))

        if len(daily_total_assets) > 1 and daily_total_assets[-2] != 0:
            ret_for_period = (daily_total_assets[-1] - daily_total_assets[-2] - flow) / daily_total_assets[-2]
            nav_for_period *= 1.0 + ret_for_period
            period_peak_nav = max(period_peak_nav, nav_for_period)
            period_drawdown = nav_for_period / period_peak_nav - 1.0 if period_peak_nav else 0.0
            period_max_drawdown = min(period_max_drawdown, period_drawdown)

        unrealized = {
            symbol: values.get(symbol, 0.0) - pos.cost_basis_cny
            for symbol, pos in state.positions.items()
        }
        daily_rows.append(
            {
                "run_id": run_id,
                "trade_date": day_str,
                "total_asset_cny": total,
                "flow_cny": flow,
                "daily_return": 0.0,
                "cumulative_return": 0.0,
                "drawdown": 0.0,
                "benchmark_return": 0.0,
                "payload_json": json_dumps(
                    {
                        "cash_cny": state.cash_cny,
                        "values": values,
                        "weights": {key: value / total if total else 0.0 for key, value in values.items()},
                        "targets": targets,
                        "unrealized_pnl_cny": unrealized,
                        "repo_lots": len(state.repo_lots),
                    }
                ),
            }
        )

    if not daily_rows:
        raise BacktestError("no daily rows created; check data coverage")

    comparison_totals = _simulate_comparison_series(
        config,
        days,
        price_maps,
        fx_map,
        repo_map,
        one_day_repo_map,
        ex_events,
        pay_events,
        set(monthly_spend_days),
        set(reb_days),
    )
    for row in daily_rows:
        payload = __import__("json").loads(row["payload_json"])
        comparison_total = comparison_totals.get(row["trade_date"])
        payload["comparison"] = {
            "name": "沪深300基金加黄金基金加国债逆回购",
            "total_asset_cny": comparison_total,
        }
        row["payload_json"] = json_dumps(payload)

    daily_returns, cumulative_returns, drawdowns = compute_metrics(daily_total_assets, daily_flows, benchmark_values)
    bench_returns = benchmark_returns(benchmark_values)
    total_return = cumulative_returns[-1]
    years = max((parse_date(daily_rows[-1]["trade_date"]) - parse_date(daily_rows[0]["trade_date"])).days / 365.25, 1 / 365.25)
    annualized = (1.0 + total_return) ** (1.0 / years) - 1.0
    returns_np = np.array(daily_returns[1:] or [0.0], dtype=float)
    volatility = float(np.std(returns_np) * math.sqrt(252))

    for idx, row in enumerate(daily_rows):
        row["daily_return"] = daily_returns[idx]
        row["cumulative_return"] = cumulative_returns[idx]
        row["drawdown"] = drawdowns[idx]
        row["benchmark_return"] = bench_returns[idx]

    final_payload = __import__("json").loads(daily_rows[-1]["payload_json"])
    summary = {
        "run_id": run_id,
        "start_date": daily_rows[0]["trade_date"],
        "end_date": daily_rows[-1]["trade_date"],
        "final_asset_cny": daily_total_assets[-1],
        "total_return": total_return,
        "annualized_return": annualized,
        "max_drawdown": min(drawdowns),
        "volatility": volatility,
        "total_fees_cny": state.total_fees_cny,
        "total_spend_cny": state.total_spend_cny,
        "withheld_tax_cny": state.total_withheld_tax_cny,
        "trade_count": len(trades),
        "rebalance_count": len(rebalance_rows),
        "final_unrealized_pnl_cny": sum(final_payload.get("unrealized_pnl_cny", {}).values()),
        "comparison_final_asset_cny": final_payload.get("comparison", {}).get("total_asset_cny"),
    }

    conn.execute(
        "INSERT INTO backtest_runs(run_id, created_at, config_json, summary_json) VALUES(?,?,?,?)",
        (run_id, utc_now(), json_dumps(config), json_dumps(summary)),
    )
    insert_many(conn, "portfolio_daily", daily_rows)
    for trade in trades:
        trade["run_id"] = run_id
    insert_many(conn, "trades", trades)
    insert_many(conn, "rebalance_events", rebalance_rows)
    return {"run_id": run_id, "summary": summary}
