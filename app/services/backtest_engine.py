from __future__ import annotations

from bisect import bisect_left, bisect_right
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, timedelta
import calendar
import hashlib
import json
import logging
import math
import time
import uuid
from typing import Any

import numpy as np

from app.config import (
    REPO_COMMISSION_RATE_BY_TENOR,
    backtest_assets,
    fx_pair_for_currency,
    normalize_config,
    repo_rate_symbol,
    required_fx_pairs_for_assets,
    selected_money_fund_asset,
    selected_repo_option,
    validate_config,
)
from app.db import insert_many, json_dumps, utc_now
from app.services.calendar import add_business_days, business_days, first_business_day_by_month, parse_date, rebalance_days, repo_actual_days, repo_maturity_day
from app.services.fees import (
    CnEtfFeeConfig,
    FxFeeConfig,
    HkConnectEtfFeeConfig,
    IbkrFeeConfig,
    RepoFeeConfig,
    cny_cost_for_hkd,
    cny_cost_for_usd,
    cn_etf_fee,
    cny_to_usd,
    dict_to_dataclass,
    hk_connect_etf_trade_fee,
    hk_connect_portfolio_fee,
    hkd_to_cny,
    ibkr_us_etf_fee,
    ibkr_us_etf_sell_fee,
    repo_fee,
    repo_interest,
    usd_to_cny,
)

logger = logging.getLogger(__name__)
BACKTEST_ENGINE_VERSION = 44
RANKING_VERSION = 4
RANKING_MIN_EXCESS_ANNUALIZED_RETURN = 0.02
RANKING_MIN_DRAWDOWN = 0.08
RANKING_EXCESS_RETURN_CAP = 0.15
RANKING_CALMAR_CAP = 1.5
DIP_BUY_CASH_BUFFER_MONTHS = 24
REBALANCE_EDGE_GUARD_WEIGHT = 1e-5
_MONEY_FUND_UNSET = object()
ASSET_COMOVEMENT_SLEEVES = (
    ("cn_treasury_30y_index", "treasury_30y", "30年国债ETF"),
    ("cn_dividend_low_vol", "dividend_low_vol", "红利ETF"),
    ("cn_gold_etf", "gold", "黄金ETF"),
)
ASSET_COMOVEMENT_WINDOWS = (
    ("all", "全部历史", None),
    ("1y", "近1年", 1),
    ("3y", "近3年", 3),
    ("5y", "近5年", 5),
    ("10y", "近10年", 10),
)
ASSET_COMOVEMENT_EPSILON = 1e-12


@dataclass
class Position:
    symbol: str
    market: str
    currency: str
    asset_type: str
    quantity: float = 0.0
    cost_basis_cny: float = 0.0
    realized_pnl_cny: float = 0.0
    estimated_transaction_fees: bool = False


@dataclass
class RepoLot:
    principal: float
    maturity_date: date
    interest: float
    fee: float
    start_date: date | None = None
    actual_days: int = 1


@dataclass
class PortfolioState:
    cash_cny: float
    positions: dict[str, Position] = field(default_factory=dict)
    repo_lots: list[RepoLot] = field(default_factory=list)
    dividend_receivable_cny: float = 0.0
    dividend_receivables_by_pay_date: dict[str, float] = field(default_factory=dict)
    total_fees_cny: float = 0.0
    total_spend_cny: float = 0.0
    total_withheld_tax_cny: float = 0.0
    total_dividend_cny: float = 0.0
    repo_realized_interest_cny: float = 0.0
    repo_fees_cny: float = 0.0


class BacktestError(ValueError):
    pass


class BacktestCancelled(RuntimeError):
    pass


def raise_if_cancelled(should_cancel=None) -> None:
    if should_cancel and should_cancel():
        raise BacktestCancelled("回测任务已取消：页面没有继续请求结果")


def canonical_config_hash(config: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"engine_version": BACKTEST_ENGINE_VERSION, "config": config},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_cached_backtest_run(conn, user_config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    config = normalize_config(user_config)
    config_hash = canonical_config_hash(config)
    row = conn.execute(
        """
        SELECT run_id, summary_json FROM backtest_runs
        WHERE config_hash=?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (config_hash,),
    ).fetchone()
    if not row:
        return None
    return {
        "run_id": row["run_id"],
        "summary": json.loads(row["summary_json"]),
        "cache": {"hit": True, "mode": "参数一致"},
    }


def load_price_map(
    conn,
    symbols: list[str],
    start: str,
    end: str,
    field: str = "close",
    share_splits: dict[str, dict[str, float]] | None = None,
    share_scale_maps: dict[str, dict[str, float]] | None = None,
) -> dict[str, dict[str, float]]:
    if field not in {"open", "close"}:
        raise ValueError("price field must be open or close")
    result: dict[str, dict[str, float]] = {}
    for symbol in symbols:
        non_null_price = f" AND prices.{field} IS NOT NULL"
        rows = conn.execute(
            f"""
            SELECT prices.trade_date, prices.{field} AS price,
                   adj_factors.adj_factor
            FROM prices
            LEFT JOIN adj_factors
              ON adj_factors.symbol=prices.symbol AND adj_factors.trade_date=prices.trade_date
            WHERE prices.symbol=? AND prices.trade_date BETWEEN ? AND ?{non_null_price}
            ORDER BY prices.trade_date
            """,
            (symbol, start, end),
        ).fetchall()
        prices, scales = adjusted_price_and_share_scale_series(rows, (share_splits or {}).get(symbol))
        result[symbol] = prices
        if share_scale_maps is not None:
            share_scale_maps[symbol] = scales
    return result


def adjusted_price_series(rows: list[Any], known_splits: dict[str, float] | None = None) -> dict[str, float]:
    """Convert raw ETF prices to a continuous share-price series only at verified splits.

    Tushare's ``fund_adj`` factor changes for both cash distributions and ETF share
    consolidations. Cash distributions are already represented in ``fund_dividends``
    and must not be adjusted here. A factor change is therefore applied only when
    it makes the raw close continuous across the event; this is the signature of a
    share split/consolidation, such as 512100's 2022-09 consolidation. Fund
    adjustment factors can be published one or two market days after the price
    change, so an otherwise-unexplained large jump may use a near-future factor
    when that factor restores price continuity. CN ETF raw prices are explicitly
    kept out of Yahoo's adjusted-price feed before this function is called.
    """
    result, _scales = adjusted_price_and_share_scale_series(rows, known_splits)
    return result


def adjusted_price_and_share_scale_series(
    rows: list[Any],
    known_splits: dict[str, float] | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    """Return normalized prices and the matching actual-share multiplier.

    The engine keeps position quantities in one continuous unit across ETF share
    splits and consolidations.  Cash distributions are quoted per actual share,
    so they must use the same cumulative multiplier as normalized prices.
    """
    result: dict[str, float] = {}
    share_scales: dict[str, float] = {}
    previous_price: float | None = None
    previous_factor: float | None = None
    cumulative_split_scale = 1.0
    for index, row in enumerate(rows):
        price = float(row["price"])
        factor = row["adj_factor"]
        factor = float(factor) if factor not in (None, 0) else None
        normalized_price = price * cumulative_split_scale
        if previous_price is not None:
            raw_jump = abs(normalized_price / previous_price - 1.0)
            split_ratio: float | None = None
            configured_ratio = (known_splits or {}).get(str(row["trade_date"]))
            if configured_ratio and configured_ratio > 0 and raw_jump >= 0.25:
                configured_price = normalized_price * configured_ratio
                if abs(configured_price / previous_price - 1.0) <= 0.08:
                    split_ratio = configured_ratio
            if split_ratio is None and previous_factor and factor:
                factor_ratio = factor / previous_factor
                adjusted_price = normalized_price * factor_ratio
                adjusted_jump = abs(adjusted_price / previous_price - 1.0)
                if raw_jump >= 0.25 and adjusted_jump <= 0.08:
                    split_ratio = factor_ratio
            if split_ratio is None and previous_factor and raw_jump >= 0.25:
                for future_row in rows[index + 1 : index + 4]:
                    future_factor = future_row["adj_factor"]
                    future_factor = float(future_factor) if future_factor not in (None, 0) else None
                    if not future_factor:
                        continue
                    factor_ratio = future_factor / previous_factor
                    adjusted_price = normalized_price * factor_ratio
                    if abs(factor_ratio - 1.0) >= 0.05 and abs(adjusted_price / previous_price - 1.0) <= 0.08:
                        split_ratio = factor_ratio
                        break
            if split_ratio is not None:
                cumulative_split_scale *= split_ratio
                normalized_price *= split_ratio
        result[str(row["trade_date"])] = normalized_price
        share_scales[str(row["trade_date"])] = cumulative_split_scale
        previous_price = normalized_price
        if factor:
            previous_factor = factor
    return result, share_scales


def configured_share_splits(assets: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for asset in assets:
        for event in asset.get("share_splits") or []:
            if not isinstance(event, dict) or not event.get("effective_date"):
                continue
            try:
                multiplier = float(event.get("price_multiplier"))
            except (TypeError, ValueError):
                continue
            if multiplier > 0:
                result.setdefault(asset["symbol"], {})[str(event["effective_date"])] = multiplier
    return result


def price_proxy_asset(asset: dict[str, Any]) -> dict[str, Any] | None:
    fallback = asset.get("price_fallback")
    if not isinstance(fallback, dict) or not fallback.get("symbol"):
        return None
    proxy = {
        **asset,
        "key": f"{asset.get('key', asset['symbol'])}_proxy",
        "symbol": str(fallback["symbol"]),
        "name": fallback.get("name") or str(fallback["symbol"]),
        "currency": fallback.get("currency", asset.get("currency", "CNY")),
        "market": fallback.get("market", asset.get("market", "CN")),
        "asset_type": fallback.get("asset_type", asset.get("asset_type", "etf")),
        "inception_date": fallback.get("start_date") or asset.get("inception_date"),
        "price_proxy_for": asset["symbol"],
    }
    proxy.pop("price_fallback", None)
    proxy.pop("replacement_assets", None)
    proxy.pop("share_splits", None)
    return proxy


def replacement_assets(asset: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for replacement in asset.get("replacement_assets", []):
        candidate = {
            **asset,
            **replacement,
            "target_weight": asset.get("target_weight", 0.0),
            "enabled": asset.get("enabled", True),
            "replacement_for": asset["symbol"],
        }
        candidate.pop("price_fallback", None)
        candidate.pop("replacement_assets", None)
        result.append(candidate)
    return result


def simulation_assets(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asset in assets:
        for candidate in (asset, *replacement_assets(asset), price_proxy_asset(asset)):
            if not candidate or candidate["symbol"] in seen:
                continue
            result.append(candidate)
            seen.add(candidate["symbol"])
    return result


def annual_expense_factor(start: date, end: date, annual_rate: float) -> float:
    """Return the remaining NAV factor after daily calendar-day expenses.

    Fund management and custody fees accrue every calendar day.  Splitting the
    interval at year boundaries keeps leap years aligned with the usual
    ``annual rate / days in year`` fund-accounting convention.
    """
    if end <= start or annual_rate <= 0:
        return 1.0
    if not math.isfinite(annual_rate) or annual_rate >= 1:
        raise BacktestError("proxy annual expense drag rate must be between 0 and 1")
    factor = 1.0
    cursor = start
    while cursor < end:
        next_year = date(cursor.year + 1, 1, 1)
        segment_end = min(end, next_year)
        days_in_year = 366 if calendar.isleap(cursor.year) else 365
        factor *= (1.0 - annual_rate / days_in_year) ** (segment_end - cursor).days
        cursor = segment_end
    return factor


def apply_proxy_expense_drag(
    prices: dict[str, float],
    annual_rate: float,
) -> dict[str, float]:
    """Net a gross index proxy for ETF expenses while keeping the splice continuous.

    The stored index is already scaled to the real ETF at the listing boundary.
    Anchoring the last proxy close and increasing older synthetic prices by the
    accumulated expense factor reduces the simulated return without creating a
    jump when the engine switches to the real ETF.
    """
    if not prices or annual_rate <= 0:
        return dict(prices)
    ordered_dates = sorted(prices)
    anchor = parse_date(ordered_dates[-1])
    adjusted: dict[str, float] = {}
    for trade_date in ordered_dates:
        remaining_factor = annual_expense_factor(parse_date(trade_date), anchor, annual_rate)
        adjusted[trade_date] = prices[trade_date] / remaining_factor
    return adjusted


def attach_proxy_price_maps(price_maps: dict[str, dict[str, float]], assets: list[dict[str, Any]]) -> None:
    for asset in assets:
        proxy = price_proxy_asset(asset)
        if not proxy:
            continue
        proxy_symbol = proxy["symbol"]
        primary_start = parse_date(
            asset.get("allocation_start_date")
            or asset.get("trade_start_date")
            or asset.get("inception_date")
        )
        target_prices = price_maps.get(asset["symbol"], {})
        merged = dict(price_maps.get(proxy_symbol, {}))
        merged.update(target_prices)
        fallback = asset.get("price_fallback") or {}
        annual_rate = float(fallback.get("annual_expense_drag_rate", 0.0) or 0.0)
        if annual_rate > 0:
            proxy_segment = {
                trade_date: value
                for trade_date, value in merged.items()
                if parse_date(trade_date) < primary_start
            }
            merged.update(apply_proxy_expense_drag(proxy_segment, annual_rate))
            # Always keep genuine ETF observations untouched.  Their market
            # price/NAV already reflects fund operating expenses.
            merged.update(
                {
                    trade_date: value
                    for trade_date, value in target_prices.items()
                    if parse_date(trade_date) >= primary_start
                }
            )
        if merged:
            price_maps[proxy_symbol] = merged


def attach_nontradable_route_expense_drag(
    price_maps: dict[str, dict[str, float]],
    assets: list[dict[str, Any]],
) -> None:
    """Apply ETF-like operating expenses to a non-tradable index route.

    Some logical sleeves use an index before a real ETF exists.  Applying the
    drag to the fallback and official-index observations as one continuous
    series avoids a false jump at their splice.  Replacement ETF observations
    remain untouched because their quoted prices already include fund fees.
    """
    for asset in assets:
        annual_rate = float(asset.get("proxy_annual_expense_drag_rate", 0.0) or 0.0)
        if annual_rate <= 0:
            continue
        proxy = price_proxy_asset(asset)
        proxy_symbol = proxy["symbol"] if proxy else None
        merged = dict(price_maps.get(proxy_symbol, {})) if proxy_symbol else {}
        merged.update(price_maps.get(asset["symbol"], {}))
        replacement_starts = [
            parse_date(
                replacement.get("allocation_start_date")
                or replacement.get("trade_start_date")
                or replacement.get("inception_date")
            )
            for replacement in asset.get("replacement_assets", [])
            if isinstance(replacement, dict)
            and (
                replacement.get("allocation_start_date")
                or replacement.get("trade_start_date")
                or replacement.get("inception_date")
            )
        ]
        route_end = min(replacement_starts) if replacement_starts else None
        route_segment = {
            trade_date: value
            for trade_date, value in merged.items()
            if route_end is None or parse_date(trade_date) < route_end
        }
        adjusted = apply_proxy_expense_drag(route_segment, annual_rate)
        if not adjusted:
            continue
        if proxy_symbol:
            proxy_prices = dict(price_maps.get(proxy_symbol, {}))
            proxy_prices.update(adjusted)
            price_maps[proxy_symbol] = proxy_prices
        primary_prices = dict(price_maps.get(asset["symbol"], {}))
        for trade_date in set(primary_prices) & set(adjusted):
            primary_prices[trade_date] = adjusted[trade_date]
        price_maps[asset["symbol"]] = primary_prices


def attach_prior_published_close_execution_maps(
    open_price_maps: dict[str, dict[str, float]],
    close_price_maps: dict[str, dict[str, float]],
    assets: list[dict[str, Any]],
    trading_days: list[date],
) -> None:
    """Use only the latest previously published close for index-proxy trades.

    ChinaBond publishes a daily index level, not an exchange opening quote.
    Mapping today's index close into today's ``open`` is a future-data leak.
    For explicitly non-tradable routes, model an execution at the last close
    that was already known before the current session opened.
    """
    symbols: set[str] = set()
    for asset in assets:
        if asset.get("tradable", True) is not False:
            continue
        symbols.add(str(asset["symbol"]))
        proxy = price_proxy_asset(asset)
        if proxy:
            symbols.add(str(proxy["symbol"]))
    for symbol in symbols:
        closes = close_price_maps.get(symbol, {})
        if not closes:
            open_price_maps[symbol] = {}
            continue
        ordered = sorted((parse_date(trade_date), float(value)) for trade_date, value in closes.items())
        cursor = 0
        last_published: float | None = None
        safe_opens: dict[str, float] = {}
        for trading_day in trading_days:
            while cursor < len(ordered) and ordered[cursor][0] < trading_day:
                last_published = ordered[cursor][1]
                cursor += 1
            if last_published is not None:
                safe_opens[trading_day.isoformat()] = last_published
        open_price_maps[symbol] = safe_opens


def load_fx_map(conn, start: str, end: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT trade_date, rate FROM fx_rates WHERE pair='USD/CNY' AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (start, end),
    ).fetchall()
    return {row["trade_date"]: float(row["rate"]) for row in rows}


def load_fx_maps(conn, pairs: list[str], start: str, end: str) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {pair: {} for pair in pairs}
    if not pairs:
        return result
    placeholders = ",".join("?" for _ in pairs)
    rows = conn.execute(
        f"""
        SELECT pair, trade_date, rate FROM fx_rates
        WHERE pair IN ({placeholders}) AND trade_date BETWEEN ? AND ?
        ORDER BY pair, trade_date
        """,
        (*pairs, start, end),
    ).fetchall()
    for row in rows:
        result.setdefault(row["pair"], {})[row["trade_date"]] = float(row["rate"])
    return result


def load_repo_map(conn, symbol: str, start: str, end: str) -> dict[str, float]:
    rows = conn.execute(
        "SELECT trade_date, close_rate FROM repo_rates WHERE symbol=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (symbol, start, end),
    ).fetchall()
    return {row["trade_date"]: float(row["close_rate"]) for row in rows}


def load_dividend_events(
    conn,
    symbols: list[str],
    start: str,
    end: str,
    share_scale_maps: dict[str, dict[str, float]] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
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
    scale_dates = {
        symbol: sorted(values)
        for symbol, values in (share_scale_maps or {}).items()
    }
    for row in rows:
        event = dict(row)
        dates = scale_dates.get(row["symbol"], [])
        scale_index = bisect_right(dates, row["ex_date"]) - 1
        event["normalized_share_scale"] = (
            float((share_scale_maps or {})[row["symbol"]][dates[scale_index]])
            if scale_index >= 0
            else 1.0
        )
        ex_events.setdefault(row["ex_date"], []).append(event)
        pay_events.setdefault(row["pay_date"], []).append(event)
    return ex_events, pay_events


def forward_value(values: dict[str, float], day: date, last: float | None) -> float | None:
    current = values.get(day.isoformat())
    return current if current is not None else last


def currency_to_cny_rate(currency: str, fx_rates: dict[str, float]) -> float:
    normalized = (currency or "CNY").upper()
    if normalized == "CNY":
        return 1.0
    pair = fx_pair_for_currency(normalized)
    if not pair or pair not in fx_rates:
        raise BacktestError(f"missing FX rate for {normalized}/CNY")
    return fx_rates[pair]


def position_value_cny(position: Position, price: float, fx_rates: dict[str, float]) -> float:
    if position.quantity <= 0:
        return 0.0
    return position.quantity * price * currency_to_cny_rate(position.currency, fx_rates)


def position_lot_size(position: Position, fees: dict[str, Any]) -> int:
    # ChinaBond series are return indices used as a valuation benchmark, rather
    # than exchange-traded ETFs.  Model them as fractional index units so a
    # small portfolio can receive the stated index exposure without inventing
    # an exchange lot-size constraint.
    if position.asset_type == "cn_bond_index":
        return 1
    if position.currency == "HKD" or position.asset_type == "hk_connect_etf":
        return max(int(float(fees["hk_connect_etf"].get("lot_size", 100.0))), 1)
    if position.currency == "CNY":
        return 100
    return 1


def prepare_active_asset_routes(
    config: dict[str, Any],
) -> list[tuple[dict[str, Any], list[tuple[date, dict[str, Any]]], dict[str, Any] | None, date | None]]:
    """Pre-parse stable route dates once instead of rebuilding them every day."""
    routes = []
    for asset in config["assets"]:
        if not asset.get("enabled", True):
            continue
        candidates = [
            (
                parse_date(
                    candidate.get("allocation_start_date")
                    or candidate.get("trade_start_date")
                    or candidate.get("inception_date")
                    or config["start_date"]
                ),
                candidate,
            )
            for candidate in replacement_assets(asset) + [asset]
        ]
        candidates.sort(key=lambda item: item[0], reverse=True)
        proxy = price_proxy_asset(asset)
        proxy_start = parse_date(proxy.get("inception_date") or config["start_date"]) if proxy else None
        routes.append((asset, candidates, proxy, proxy_start))
    return routes


def active_assets(
    config: dict[str, Any],
    day: date,
    latest_prices: dict[str, float | None],
    prepared_routes: list[tuple[dict[str, Any], list[tuple[date, dict[str, Any]]], dict[str, Any] | None, date | None]] | None = None,
) -> list[dict[str, Any]]:
    result = []
    for asset, candidates, proxy, proxy_start in prepared_routes or prepare_active_asset_routes(config):
        selected = next(
            (
                candidate
                for start_day, candidate in candidates
                if day >= start_day and latest_prices.get(candidate["symbol"]) is not None
            ),
            None,
        )
        if selected:
            result.append(selected)
            continue
        if not proxy:
            continue
        if proxy_start is not None and day >= proxy_start and latest_prices.get(proxy["symbol"]) is not None:
            result.append(proxy)
    return result


def scheduled_target_price_view(
    latest_prices: dict[str, float | None],
    execution_day: date,
    prepared_routes: list[tuple[dict[str, Any], list[tuple[date, dict[str, Any]]], dict[str, Any] | None, date | None]],
) -> dict[str, float | None]:
    """Expose known replacement routes without reading their future open price."""
    price_view = dict(latest_prices)
    for _asset, candidates, _proxy, _proxy_start in prepared_routes:
        for start_day, candidate in candidates:
            if start_day <= execution_day and candidate.get("replacement_for") and price_view.get(candidate["symbol"]) is None:
                price_view[candidate["symbol"]] = 1.0
    return price_view


def active_route_symbols(
    day: date,
    available_prices: dict[str, float | None],
    prepared_routes: list[tuple[dict[str, Any], list[tuple[date, dict[str, Any]]], dict[str, Any] | None, date | None]],
) -> dict[str, str]:
    """Return the active physical symbol for each configured logical sleeve."""
    result: dict[str, str] = {}
    for asset, candidates, proxy, proxy_start in prepared_routes:
        selected = next(
            (
                candidate
                for start_day, candidate in candidates
                if day >= start_day and available_prices.get(candidate["symbol"]) is not None
            ),
            None,
        )
        if selected:
            result[str(asset["symbol"])] = str(selected["symbol"])
        elif proxy and proxy_start is not None and day >= proxy_start and available_prices.get(proxy["symbol"]) is not None:
            result[str(asset["symbol"])] = str(proxy["symbol"])
    return result


def classify_asset_comovement(daily_returns: list[float]) -> str:
    """Classify one shared trading day for the three defensive ETF sleeves."""
    if len(daily_returns) != len(ASSET_COMOVEMENT_SLEEVES) or any(
        not math.isfinite(float(value)) for value in daily_returns
    ):
        return "unclassified"
    positive = sum(float(value) > ASSET_COMOVEMENT_EPSILON for value in daily_returns)
    negative = sum(float(value) < -ASSET_COMOVEMENT_EPSILON for value in daily_returns)
    if positive == len(daily_returns):
        return "same_up"
    if negative == len(daily_returns):
        return "same_down"
    if positive and negative:
        equal_weight_return = sum(float(value) for value in daily_returns) / len(daily_returns)
        if equal_weight_return > ASSET_COMOVEMENT_EPSILON:
            return "hedge_positive"
        if equal_weight_return < -ASSET_COMOVEMENT_EPSILON:
            return "hedge_negative"
    return "unclassified"


def _asset_comovement_cutoff(end_day: date, years: int | None) -> date | None:
    if years is None:
        return None
    try:
        anchor = end_day.replace(year=end_day.year - years)
    except ValueError:
        anchor = end_day.replace(year=end_day.year - years, day=28)
    return anchor + timedelta(days=1)


def _summarize_asset_comovement_window(
    records: list[dict[str, Any]],
    end_day: date,
    window_key: str,
    label: str,
    years: int | None,
) -> dict[str, Any]:
    cutoff = _asset_comovement_cutoff(end_day, years)
    selected = [row for row in records if cutoff is None or parse_date(row["trade_date"]) >= cutoff]
    counts = {
        "same_up": 0,
        "same_down": 0,
        "hedge_positive": 0,
        "hedge_negative": 0,
        "unclassified": 0,
    }
    for row in selected:
        counts[row["category"]] += 1
    comparable_days = len(selected)
    classified_days = comparable_days - counts["unclassified"]
    percentages = {
        key: (value / comparable_days if comparable_days else 0.0)
        for key, value in counts.items()
    }
    return {
        "key": window_key,
        "label": label,
        "requested_start_date": cutoff.isoformat() if cutoff else None,
        "start_date": selected[0]["trade_date"] if selected else None,
        "end_date": selected[-1]["trade_date"] if selected else None,
        "comparable_days": comparable_days,
        "classified_days": classified_days,
        "same_direction_days": counts["same_up"] + counts["same_down"],
        "hedge_days": counts["hedge_positive"] + counts["hedge_negative"],
        "counts": counts,
        "percentages": percentages,
    }


def asset_comovement_statistics(conn, user_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Count exact shared trading days for 30Y treasury, dividend and gold sleeves.

    Each logical sleeve follows the same proxy and replacement routes as the
    backtest engine. Returns use normalized close prices and recognize cash
    distributions on the ex-date. Route-switch days use the selected physical
    instrument's own previous close instead of comparing unrelated price scales.
    """
    config = normalize_config(user_config)
    start = str(config["start_date"])
    end = str(config["end_date"])
    configured_by_key = {str(asset.get("key")): asset for asset in config["assets"]}
    sleeves: list[dict[str, Any]] = []
    public_assets: list[dict[str, Any]] = []
    for config_key, public_key, label in ASSET_COMOVEMENT_SLEEVES:
        source = configured_by_key.get(config_key)
        if not source:
            continue
        sleeve = deepcopy(source)
        sleeve["enabled"] = True
        sleeves.append(sleeve)
        public_assets.append(
            {
                "key": public_key,
                "config_key": config_key,
                "label": label,
                "logical_symbol": str(sleeve["symbol"]),
            }
        )
    if len(sleeves) != len(ASSET_COMOVEMENT_SLEEVES):
        return {
            "available": False,
            "assets": public_assets,
            "windows": {},
            "window_order": [key for key, _label, _years in ASSET_COMOVEMENT_WINDOWS],
            "message": "缺少三资产配置，无法计算联动统计",
        }

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
                float(event["div_cash"])
                * float(event.get("normalized_share_scale", 1.0) or 1.0)
            )

    route_config = {**config, "assets": sleeves}
    prepared_routes = prepare_active_asset_routes(route_config)
    logical_symbols = [str(asset["symbol"]) for asset in sleeves]
    public_key_by_symbol = {
        str(asset["symbol"]): public_key
        for asset, (_config_key, public_key, _label) in zip(sleeves, ASSET_COMOVEMENT_SLEEVES)
    }
    route_symbols: dict[str, set[str]] = {public_key: set() for _config_key, public_key, _label in ASSET_COMOVEMENT_SLEEVES}
    all_dates = sorted({trade_date for prices in price_maps.values() for trade_date in prices})
    latest_prices: dict[str, float | None] = {symbol: None for symbol in price_maps}
    previous_prices: dict[str, float] = {}
    records: list[dict[str, Any]] = []
    for trade_date in all_dates:
        day = parse_date(trade_date)
        current_prices: dict[str, float] = {}
        for symbol, prices in price_maps.items():
            current = prices.get(trade_date)
            if current is not None:
                current_prices[symbol] = float(current)
                latest_prices[symbol] = float(current)
        active_symbols = active_route_symbols(day, latest_prices, prepared_routes)
        daily_returns: list[float] = []
        complete = True
        for logical_symbol in logical_symbols:
            route_symbol = active_symbols.get(logical_symbol)
            current = current_prices.get(route_symbol) if route_symbol else None
            previous = previous_prices.get(route_symbol) if route_symbol else None
            if route_symbol is None or current is None or previous is None or previous <= 0:
                complete = False
                break
            total_return = (
                current + dividends.get((trade_date, route_symbol), 0.0)
            ) / previous - 1.0
            daily_returns.append(total_return)
            route_symbols[public_key_by_symbol[logical_symbol]].add(route_symbol)
        if complete:
            records.append(
                {
                    "trade_date": trade_date,
                    "category": classify_asset_comovement(daily_returns),
                }
            )
        previous_prices.update(current_prices)

    end_day = parse_date(end)
    windows = {
        key: _summarize_asset_comovement_window(records, end_day, key, label, years)
        for key, label, years in ASSET_COMOVEMENT_WINDOWS
    }
    for asset in public_assets:
        asset["route_symbols"] = sorted(route_symbols[asset["key"]])
    return {
        "available": bool(records),
        "assets": public_assets,
        "window_order": [key for key, _label, _years in ASSET_COMOVEMENT_WINDOWS],
        "windows": windows,
        "available_start_date": records[0]["trade_date"] if records else None,
        "available_end_date": records[-1]["trade_date"] if records else None,
        "methodology": {
            "same_up": "三项日总收益均大于0",
            "same_down": "三项日总收益均小于0",
            "hedge_positive": "至少一涨一跌，三项等权平均日总收益大于0",
            "hedge_negative": "至少一涨一跌，三项等权平均日总收益小于0",
            "unclassified": "含零涨跌且未同时出现正负，或等权平均收益为0",
            "return_basis": "复权连续价格，并在除息日计入现金分红；上市前代理和ETF替换沿用回测规则",
        },
        "message": None if records else "所选回测区间没有三项均可比的交易日",
    }


def repo_fixed_target_weight(config: dict[str, Any], total_value_cny: float | None) -> float:
    total_value = float(total_value_cny or config.get("initial_capital_cny") or 0.0)
    if total_value <= 0:
        return 1.0
    fixed_amount = max(float(config.get("repo_fixed_target_cny", 0.0) or 0.0), 0.0)
    fixed_ratio = min(max(float(config.get("repo_fixed_target_ratio", 0.0) or 0.0), 0.0), 1.0)
    return min(max(fixed_amount / total_value + fixed_ratio, 0.0), 1.0)


def effective_weights(
    config: dict[str, Any],
    day: date,
    latest_prices: dict[str, float | None],
    total_value_cny: float | None = None,
    *,
    prepared_routes: list[tuple[dict[str, Any], list[tuple[date, dict[str, Any]]], dict[str, Any] | None, date | None]] | None = None,
    money_fund_asset: dict[str, Any] | None | object = _MONEY_FUND_UNSET,
) -> dict[str, float]:
    weights: dict[str, float] = {}
    assets = active_assets(config, day, latest_prices, prepared_routes)
    if config.get("repo_target_mode", "residual_weight") == "fixed_bucket":
        repo_weight = repo_fixed_target_weight(config, total_value_cny)
        remaining_weight = max(1.0 - repo_weight, 0.0)
        enabled_total = sum(
            max(float(asset.get("target_weight", 0.0) or 0.0), 0.0)
            for asset in config["assets"]
            if asset.get("enabled", True)
        )
        active_total = 0.0
        if enabled_total > 0 and remaining_weight > 0:
            for asset in assets:
                weight = max(float(asset.get("target_weight", 0.0) or 0.0), 0.0) / enabled_total * remaining_weight
                if weight > 0:
                    weights[asset["symbol"]] = weight
                    active_total += weight
        weights["REPO"] = min(repo_weight + max(remaining_weight - active_total, 0.0), 1.0)
    else:
        total_risk = 0.0
        for asset in assets:
            weight = float(asset.get("target_weight", 0.0))
            weights[asset["symbol"]] = weight
            total_risk += weight
        weights["REPO"] = max(1.0 - total_risk, 0.0)
    money_fund = selected_money_fund_asset(config) if money_fund_asset is _MONEY_FUND_UNSET else money_fund_asset
    if money_fund:
        repo_weight = weights.pop("REPO", 0.0)
        fund_symbol = money_fund["symbol"]
        trade_start = parse_date(money_fund.get("trade_start_date") or money_fund.get("inception_date"))
        if day >= trade_start and latest_prices.get(fund_symbol) is not None:
            if repo_weight > 0:
                weights[fund_symbol] = weights.get(fund_symbol, 0.0) + repo_weight
        else:
            weights["REPO"] = repo_weight
    return weights


def exact_target_weights(targets: dict[str, float]) -> dict[str, float]:
    desired = {key: max(float(value), 0.0) for key, value in targets.items()}
    total = sum(desired.values())
    if total <= 0:
        return {"REPO": 1.0}
    return {key: value / total for key, value in desired.items() if value > 1e-10}


def should_rebalance(current_weights: dict[str, float], targets: dict[str, float], band: float) -> bool:
    keys = set(current_weights) | set(targets)
    for key in keys:
        target = max(float(targets.get(key, 0.0)), 0.0)
        current = max(float(current_weights.get(key, 0.0)), 0.0)
        # A 25% band around a 10% target is 7.5%..12.5%.  A zero target has
        # no relative tolerance: a removed asset must be fully unwound.
        tolerance = target * max(float(band), 0.0) if target > 1e-10 else 1e-10
        if abs(current - target) > tolerance + 1e-10:
            return True
    return False


def has_investable_asset_target(targets: dict[str, float]) -> bool:
    return any(key != "REPO" and value > 1e-10 for key, value in targets.items())


def dip_buy_assets(
    config: dict[str, Any],
    day: date,
    latest_prices: dict[str, float | None],
    prepared_routes: list[tuple[dict[str, Any], list[tuple[date, dict[str, Any]]], dict[str, Any] | None, date | None]] | None = None,
) -> list[dict[str, Any]]:
    """Return eligible broad, low-vol dividend, gold, and treasury routes."""
    return [
        asset
        for asset in active_assets(config, day, latest_prices, prepared_routes)
        if asset.get("tradable", True) is not False
        and (
            asset.get("exclusive_group") == "cn_broad_etf"
            or str(asset.get("key") or "").startswith("cn_dividend_low_vol")
            or str(asset.get("key") or "").startswith("cn_gold_etf")
            or str(asset.get("key") or "").startswith("cn_treasury_30y")
            or asset.get("asset_type") == "cn_bond_index"
        )
    ]


def dip_buy_cash_buffer_cny(monthly_spend_cny: float) -> float:
    """Return the fixed 24-month living-expense reserve for an annual cycle."""
    return DIP_BUY_CASH_BUFFER_MONTHS * max(float(monthly_spend_cny), 0.0)


def dip_buy_annual_budget(
    cash_equivalent_cny: float,
    monthly_spend_cny: float,
    total_parts: int,
) -> tuple[float, float, float, int]:
    """Lock the annual reserve, pool B, piece value and usable part count."""
    part_count = max(int(total_parts), 1)
    cash_buffer_cny = dip_buy_cash_buffer_cny(monthly_spend_cny)
    pool_cny = max(float(cash_equivalent_cny) - cash_buffer_cny, 0.0)
    piece_cny = pool_cny / part_count
    remaining_parts = part_count if pool_cny > 0 else 0
    return cash_buffer_cny, pool_cny, piece_cny, remaining_parts


def dip_buy_parts_for_level(level: int, mode: str) -> int:
    """Return the number of cash-pool pieces assigned to one drawdown level."""
    normalized_level = max(int(level), 1)
    return normalized_level if mode == "multiplier" else 1


def dip_buy_cycle_baselines(
    state: PortfolioState,
    config: dict[str, Any],
    day: date,
    latest_prices: dict[str, float | None],
    prepared_routes: list[tuple[dict[str, Any], list[tuple[date, dict[str, Any]]], dict[str, Any] | None, date | None]],
) -> tuple[dict[str, float], dict[str, float]]:
    """Capture each active route's actual post-allocation cost and investment.

    Baselines are keyed by the physical security.  When an index proxy switches
    to an ETF, the caller captures the ETF position again instead of comparing
    the ETF quote with the unrelated index level.
    """
    cost_prices: dict[str, float] = {}
    investments: dict[str, float] = {}
    for asset in dip_buy_assets(config, day, latest_prices, prepared_routes):
        symbol = str(asset["symbol"])
        position = state.positions.get(symbol)
        if not position or position.quantity <= 0 or position.cost_basis_cny <= 0:
            continue
        cost_prices[symbol] = position.cost_basis_cny / position.quantity
        investments[symbol] = position.cost_basis_cny
    return cost_prices, investments


def is_dip_buy_blackout_month(day: date, annual_rebalance_month: int, blackout_months: int) -> bool:
    """Return whether *day* is in one of the N calendar months before rebalance."""
    months_before_rebalance = (int(annual_rebalance_month) - day.month) % 12
    return 1 <= months_before_rebalance <= max(min(int(blackout_months), 11), 0)


def dip_buy_cash_equivalent_value_cny(
    state: PortfolioState,
    day: date,
    latest_prices: dict[str, float | None],
    fx_rates: dict[str, float],
    money_fund_symbol: str | None,
) -> float:
    value = state.cash_cny + sum(_repo_lot_value(lot, day) for lot in state.repo_lots)
    if money_fund_symbol:
        position = state.positions.get(money_fund_symbol)
        price = latest_prices.get(money_fund_symbol)
        if position and price is not None:
            value += position_value_cny(position, float(price), fx_rates)
    return value


def has_deferred_inception_target(config: dict[str, Any], day: date) -> bool:
    for asset in config["assets"]:
        if not asset.get("enabled", True) or float(asset.get("target_weight", 0.0) or 0.0) <= 1e-10:
            continue
        primary_start = parse_date(asset.get("inception_date") or config["start_date"])
        proxy = price_proxy_asset(asset)
        proxy_start = parse_date(proxy.get("inception_date") or config["start_date"]) if proxy else primary_start
        if day < min(primary_start, proxy_start):
            return True
    return False


def minimal_rebalance_weights(
    current_weights: dict[str, float],
    targets: dict[str, float],
    band: float,
    cash_equivalent_symbols: set[str] | None = None,
) -> dict[str, float]:
    """Project current weights onto the target bands with minimum turnover.

    Out-of-band sleeves move only to their nearest boundary.  Any residual is
    absorbed by the cash-equivalent sleeve first, avoiding unnecessary trades
    in risk assets that are already inside their permitted ranges.
    """
    cash_symbols = {"REPO"} | set(cash_equivalent_symbols or ())
    keys = sorted(
        set(current_weights) | set(targets),
        key=lambda key: (0 if key in cash_symbols else 1, key),
    )

    lower: dict[str, float] = {}
    upper: dict[str, float] = {}
    desired: dict[str, float] = {}
    for key in keys:
        target = max(float(targets.get(key, 0.0)), 0.0)
        tolerance = target * max(float(band), 0.0) if target > 1e-10 else 0.0
        lower[key] = max(target - tolerance, 0.0)
        upper[key] = min(target + tolerance, 1.0)
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


def compute_metrics(
    total_assets: list[float],
    flows: list[float],
    benchmark: list[float],
    initial_value: float | None = None,
) -> tuple[list[float], list[float], list[float]]:
    if not total_assets:
        return [], [], []
    daily_returns: list[float] = []
    cumulative: list[float] = []
    drawdowns: list[float] = []
    nav = 1.0
    peak = 1.0
    for idx, total in enumerate(total_assets):
        if idx == 0:
            previous = initial_value
            ret = 0.0 if previous is None or previous == 0 else (total - previous - flows[idx]) / previous
        else:
            previous = total_assets[idx - 1]
            ret = 0.0 if previous == 0 else (total - previous - flows[idx]) / previous
        nav *= 1.0 + ret
        peak = max(peak, nav)
        daily_returns.append(ret)
        cumulative.append(nav - 1.0)
        drawdowns.append(nav / peak - 1.0)
    return daily_returns, cumulative, drawdowns


def _shift_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        # Keep a leap-day anchor inside February in non-leap years.
        return value.replace(year=value.year + years, day=28)


def rolling_window_ranges(
    start: str,
    end: str,
    window_years: int = 3,
    step_years: int = 1,
) -> list[dict[str, Any]]:
    """Return complete inclusive ranges for independently initialized backtests.

    A five-year window starting on 2001-01-01 ends on 2005-12-31.  Each
    following range advances the start by one year, so the next range is
    2002-01-01 through 2006-12-31.
    """
    if window_years < 1 or step_years < 1:
        return []
    overall_start = parse_date(start)
    overall_end = parse_date(end)
    if overall_end < overall_start:
        return []
    rows: list[dict[str, Any]] = []
    anchor = overall_start
    sequence = 1
    while True:
        window_end = _shift_years(anchor, window_years) - timedelta(days=1)
        if window_end > overall_end:
            break
        rows.append(
            {
                "sequence": sequence,
                "period": f"{anchor.year}–{window_end.year}",
                "start_date": anchor.isoformat(),
                "end_date": window_end.isoformat(),
                "window_years": window_years,
            }
        )
        sequence += 1
        anchor = _shift_years(anchor, step_years)
    return rows


def annual_return_drawdown_ratio(annualized_return: float, max_drawdown: float) -> float | None:
    """Return the raw annualized-return/max-drawdown ratio (Calmar ratio)."""
    annualized = float(annualized_return or 0.0)
    drawdown = abs(float(max_drawdown or 0.0))
    if not math.isfinite(annualized) or not math.isfinite(drawdown) or drawdown <= 1e-12:
        return None
    return annualized / drawdown


def yearly_return_counts(dates: list[str], daily_returns: list[float]) -> tuple[int, int]:
    """Return positive and total complete calendar years from daily portfolio returns."""
    yearly_nav: dict[str, float] = {}
    year_dates: dict[str, list[str]] = {}
    for trade_date, daily_return in zip(dates, daily_returns):
        year = trade_date[:4]
        yearly_nav[year] = yearly_nav.get(year, 1.0) * (1.0 + float(daily_return or 0.0))
        year_dates.setdefault(year, []).append(trade_date)
    complete = []
    for year, nav in yearly_nav.items():
        first_day = parse_date(year_dates[year][0])
        last_day = parse_date(year_dates[year][-1])
        # A month-only check incorrectly treats Jan 31..Dec 1 as a complete
        # calendar year.  Allow normal market holidays around both boundaries,
        # but require coverage of the opening and closing week.
        if first_day.month == 1 and first_day.day <= 7 and last_day.month == 12 and last_day.day >= 24:
            complete.append(nav)
    return sum(1 for nav in complete if nav > 1.0 + 1e-12), len(complete)


def yearly_positive_return_count(dates: list[str], daily_returns: list[float]) -> int:
    return yearly_return_counts(dates, daily_returns)[0]


def ranking_metrics(
    annualized_return: float,
    repo_annualized_return: float,
    max_drawdown: float,
    positive_year_count: int,
    complete_year_count: int,
) -> dict[str, Any]:
    """Return the capped 0-100 leaderboard score and its transparent components."""
    excess_annualized_return = annualized_return - repo_annualized_return
    adjusted_calmar = excess_annualized_return / max(abs(max_drawdown), RANKING_MIN_DRAWDOWN)
    return_drawdown_ratio = annual_return_drawdown_ratio(annualized_return, max_drawdown)
    positive_year_ratio = positive_year_count / complete_year_count if complete_year_count else 0.0
    excess_return_score = min(max(excess_annualized_return / RANKING_EXCESS_RETURN_CAP, 0.0), 1.0)
    calmar_score = min(max(adjusted_calmar / RANKING_CALMAR_CAP, 0.0), 1.0)
    score = 55.0 * excess_return_score + 30.0 * calmar_score + 15.0 * positive_year_ratio
    return {
        "ranking_version": RANKING_VERSION,
        "repo_annualized_return": repo_annualized_return,
        "excess_annualized_return": excess_annualized_return,
        "adjusted_calmar": adjusted_calmar,
        "annual_return_drawdown_ratio": return_drawdown_ratio,
        "positive_year_ratio": positive_year_ratio,
        "ranking_eligible": excess_annualized_return >= RANKING_MIN_EXCESS_ANNUALIZED_RETURN,
        "ranking_score": score,
    }


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


def _compounded_return(values: list[float]) -> float:
    growth = 1.0
    for value in values:
        growth *= 1.0 + float(value or 0.0)
    return growth - 1.0


def worst_calendar_periods(dates: list[str], daily_returns: list[float]) -> dict[str, dict[str, Any] | None]:
    """Return the worst complete calendar year and calendar half-year.

    Partial periods are ignored when at least one complete peer exists. This
    prevents a few opening or closing trading days from being compared with a
    full year or half-year. Short backtests still return their available period.
    """
    year_groups: dict[int, dict[str, Any]] = {}
    half_groups: dict[tuple[int, int], dict[str, Any]] = {}
    for trade_date, daily_return in zip(dates, daily_returns):
        current = parse_date(trade_date)
        year_group = year_groups.setdefault(current.year, {"dates": [], "returns": []})
        year_group["dates"].append(trade_date)
        year_group["returns"].append(float(daily_return or 0.0))
        half = 1 if current.month <= 6 else 2
        half_group = half_groups.setdefault((current.year, half), {"dates": [], "returns": []})
        half_group["dates"].append(trade_date)
        half_group["returns"].append(float(daily_return or 0.0))

    def year_row(year: int, group: dict[str, Any]) -> dict[str, Any]:
        first = parse_date(group["dates"][0])
        last = parse_date(group["dates"][-1])
        complete = first.month == 1 and first.day <= 7 and last.month == 12 and last.day >= 24
        return {
            "period": f"{year}年",
            "start_date": group["dates"][0],
            "end_date": group["dates"][-1],
            "return": _compounded_return(group["returns"]),
            "complete": complete,
        }

    def half_row(key: tuple[int, int], group: dict[str, Any]) -> dict[str, Any]:
        year, half = key
        first = parse_date(group["dates"][0])
        last = parse_date(group["dates"][-1])
        if half == 1:
            complete = first.month == 1 and first.day <= 7 and last.month == 6 and last.day >= 24
        else:
            complete = first.month == 7 and first.day <= 7 and last.month == 12 and last.day >= 24
        return {
            "period": f"{year}年{'上' if half == 1 else '下'}半年",
            "start_date": group["dates"][0],
            "end_date": group["dates"][-1],
            "return": _compounded_return(group["returns"]),
            "complete": complete,
        }

    def choose_worst(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        complete_rows = [row for row in rows if row["complete"]]
        candidates = complete_rows or rows
        return min(candidates, key=lambda row: row["return"]) if candidates else None

    return {
        "worst_year": choose_worst([year_row(year, group) for year, group in year_groups.items()]),
        "worst_half_year": choose_worst([half_row(key, group) for key, group in half_groups.items()]),
    }


def drawdown_recovery_metrics(dates: list[str], cumulative_returns: list[float]) -> dict[str, Any] | None:
    """Describe recovery from the maximum-drawdown trough to its prior peak."""
    if not dates or not cumulative_returns:
        return None
    navs = [1.0 + float(value or 0.0) for value in cumulative_returns]
    peak_nav = 1.0
    peak_index = 0
    max_drawdown = 0.0
    trough_index = 0
    trough_peak_index = 0
    trough_peak_nav = peak_nav
    for index, nav in enumerate(navs):
        if nav > peak_nav:
            peak_nav = nav
            peak_index = index
        drawdown = nav / peak_nav - 1.0 if peak_nav else 0.0
        if drawdown < max_drawdown:
            max_drawdown = drawdown
            trough_index = index
            trough_peak_index = peak_index
            trough_peak_nav = peak_nav

    peak_date = parse_date(dates[trough_peak_index])
    trough_date = parse_date(dates[trough_index])
    recovery_index = next(
        (index for index in range(trough_index, len(navs)) if navs[index] >= trough_peak_nav * (1.0 - 1e-12)),
        None,
    )
    recovery_date = parse_date(dates[recovery_index]) if recovery_index is not None else None
    end_date = parse_date(dates[-1])
    return {
        "peak_date": peak_date.isoformat(),
        "trough_date": trough_date.isoformat(),
        "recovery_date": recovery_date.isoformat() if recovery_date else None,
        "recovery_days": (recovery_date - trough_date).days if recovery_date else None,
        "underwater_days": ((recovery_date or end_date) - peak_date).days,
        "ongoing_days": 0 if recovery_date else (end_date - trough_date).days,
        "recovered": recovery_date is not None,
    }


def market_capture_metrics(
    dates: list[str],
    daily_returns: list[float],
    benchmark_values: list[float | None],
) -> dict[str, Any]:
    """Return standard monthly upside/downside capture ratios versus the benchmark."""
    monthly_endpoints: dict[str, tuple[float, float]] = {}
    strategy_nav = 1.0
    last_benchmark: float | None = None
    for trade_date, daily_return, benchmark_value in zip(dates, daily_returns, benchmark_values):
        strategy_nav *= 1.0 + float(daily_return or 0.0)
        if benchmark_value is not None and float(benchmark_value) > 0:
            last_benchmark = float(benchmark_value)
        if last_benchmark is not None:
            monthly_endpoints[trade_date[:7]] = (strategy_nav, last_benchmark)

    strategy_up: list[float] = []
    benchmark_up: list[float] = []
    strategy_down: list[float] = []
    benchmark_down: list[float] = []
    endpoints = list(monthly_endpoints.values())
    for previous, current in zip(endpoints, endpoints[1:]):
        strategy_return = current[0] / previous[0] - 1.0 if previous[0] else 0.0
        benchmark_return = current[1] / previous[1] - 1.0 if previous[1] else 0.0
        if benchmark_return > 1e-12:
            strategy_up.append(strategy_return)
            benchmark_up.append(benchmark_return)
        elif benchmark_return < -1e-12:
            strategy_down.append(strategy_return)
            benchmark_down.append(benchmark_return)

    def annualized_period_return(values: list[float]) -> float | None:
        if not values:
            return None
        growth = 1.0
        for value in values:
            growth *= max(1.0 + value, 0.0)
        return growth ** (12.0 / len(values)) - 1.0

    def capture(strategy: list[float], benchmark: list[float]) -> float | None:
        strategy_annualized = annualized_period_return(strategy)
        benchmark_annualized = annualized_period_return(benchmark)
        if strategy_annualized is None or benchmark_annualized is None or abs(benchmark_annualized) <= 1e-12:
            return None
        return strategy_annualized / benchmark_annualized

    return {
        "upside_capture_ratio": capture(strategy_up, benchmark_up),
        "downside_capture_ratio": capture(strategy_down, benchmark_down),
        "up_market_months": len(benchmark_up),
        "down_market_months": len(benchmark_down),
    }


def reference_trading_days(
    start: str,
    end: str,
    reference_prices: dict[str, float],
    repo_rates: dict[str, float],
) -> list[date]:
    available_dates = set(reference_prices) or set(repo_rates)
    return [day for day in business_days(start, end) if day.isoformat() in available_dates]


def _sell_position(
    state: PortfolioState,
    pos: Position,
    day: date,
    qty: float,
    price: float,
    fx_rates: dict[str, float],
    fees: dict[str, Any],
    trades: list[dict[str, Any]],
    reason: str,
) -> float:
    qty = min(qty, pos.quantity)
    if qty <= 0:
        return 0.0
    gross_native = qty * price
    if pos.currency == "USD":
        fx = currency_to_cny_rate("USD", fx_rates)
        ibkr_fee = ibkr_us_etf_sell_fee(qty, gross_native, dict_to_dataclass(IbkrFeeConfig, fees["ibkr_us_etf"]))
        net_usd = max(gross_native - ibkr_fee, 0.0)
        cash_cny, fx_fee = usd_to_cny(net_usd, fx, dict_to_dataclass(FxFeeConfig, fees["fx"]), include_wire=False)
        fee_cny = ibkr_fee * fx + fx_fee
    elif pos.currency == "HKD" or pos.asset_type == "hk_connect_etf":
        fx = currency_to_cny_rate("HKD", fx_rates)
        hk_cfg = dict_to_dataclass(HkConnectEtfFeeConfig, fees["hk_connect_etf"])
        trade_fee_hkd = hk_connect_etf_trade_fee(gross_native, hk_cfg)
        net_hkd = max(gross_native - trade_fee_hkd, 0.0)
        cash_cny, fx_fee = hkd_to_cny(net_hkd, fx, hk_cfg)
        fee_cny = trade_fee_hkd * fx + fx_fee
    elif pos.asset_type == "cn_bond_index":
        # A disclosed pre-ETF proxy can carry an estimated implementation
        # commission even though its synthetic units remain fractional.
        fee_cny = (
            cn_etf_fee(gross_native, dict_to_dataclass(CnEtfFeeConfig, fees["cn_etf"]))
            if pos.estimated_transaction_fees
            else 0.0
        )
        cash_cny = max(gross_native - fee_cny, 0.0)
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
            "payload_json": json_dumps({"cash_cny": cash_cny, "fx_rates": fx_rates}),
        }
    )
    return cash_cny


def _buy_position(
    state: PortfolioState,
    pos: Position,
    day: date,
    budget_cny: float,
    price: float,
    fx_rates: dict[str, float],
    fees: dict[str, Any],
    trades: list[dict[str, Any]],
    allow_fractional_us_shares: bool,
    reason: str,
) -> float:
    budget_cny = min(max(budget_cny, 0.0), state.cash_cny)
    if budget_cny <= 0:
        return 0.0
    if pos.currency == "USD":
        fx = currency_to_cny_rate("USD", fx_rates)
        fx_cfg = dict_to_dataclass(FxFeeConfig, fees["fx"])
        usd_budget, _budget_fx_fee = cny_to_usd(budget_cny, fx, fx_cfg, include_wire=False)
        raw_qty = usd_budget / price if price else 0.0
        qty = raw_qty if allow_fractional_us_shares else math.floor(raw_qty)
        gross_usd = qty * price
        ibkr_cfg = dict_to_dataclass(IbkrFeeConfig, fees["ibkr_us_etf"])
        commission_usd = ibkr_us_etf_fee(qty, gross_usd, ibkr_cfg) if qty > 0 else 0.0
        if gross_usd + commission_usd > usd_budget and price > 0:
            qty = max((usd_budget - commission_usd) / price, 0.0) if allow_fractional_us_shares else max(math.floor((usd_budget - commission_usd) / price), 0)
            gross_usd = qty * price
            commission_usd = ibkr_us_etf_fee(qty, gross_usd, ibkr_cfg) if qty > 0 else 0.0
        spent_cny, fx_fee = cny_cost_for_usd(gross_usd + commission_usd, fx, fx_cfg)
        fee_cny = commission_usd * fx + fx_fee
        gross_native = gross_usd
    elif pos.currency == "HKD" or pos.asset_type == "hk_connect_etf":
        fx = currency_to_cny_rate("HKD", fx_rates)
        hk_cfg = dict_to_dataclass(HkConnectEtfFeeConfig, fees["hk_connect_etf"])
        lot_size = max(int(hk_cfg.lot_size), 1)
        spread = hk_cfg.fx_spread_bps / 10000.0
        hkd_budget = budget_cny / (fx * (1.0 + spread)) if fx and price else 0.0
        qty = max(math.floor((hkd_budget / price) / lot_size) * lot_size, 0) if price else 0
        gross_native = qty * price
        trade_fee_hkd = hk_connect_etf_trade_fee(gross_native, hk_cfg) if qty else 0.0
        spent_cny, fx_fee = cny_cost_for_hkd(gross_native + trade_fee_hkd, fx, hk_cfg)
        while qty > 0 and spent_cny > budget_cny:
            qty -= lot_size
            gross_native = qty * price
            trade_fee_hkd = hk_connect_etf_trade_fee(gross_native, hk_cfg) if qty else 0.0
            spent_cny, fx_fee = cny_cost_for_hkd(gross_native + trade_fee_hkd, fx, hk_cfg)
        fee_cny = trade_fee_hkd * fx + fx_fee
    elif pos.asset_type == "cn_bond_index":
        # The ChinaBond total-return level already incorporates the bond
        # portfolio's coupon return.  It is not a listed ETF, so no 100-share
        # board lot applies.  A route may nevertheless charge the configured
        # ETF commission as a conservative implementation-cost estimate.
        qty = budget_cny / price if price else 0.0
        gross_native = qty * price
        fee_cny = (
            cn_etf_fee(gross_native, dict_to_dataclass(CnEtfFeeConfig, fees["cn_etf"]))
            if pos.estimated_transaction_fees and qty
            else 0.0
        )
        if gross_native + fee_cny > budget_cny and price > 0:
            # cn_etf_fee is rounded to cents.  A short fixed-point iteration
            # keeps the fractional proxy purchase inside its exact budget.
            qty = max((budget_cny - fee_cny) / price, 0.0)
            gross_native = qty * price
            fee_cny = cn_etf_fee(gross_native, dict_to_dataclass(CnEtfFeeConfig, fees["cn_etf"]))
        spent_cny = gross_native + fee_cny
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
            "payload_json": json_dumps({"spent_cny": spent_cny, "fx_rates": fx_rates}),
        }
    )
    return spent_cny


def _portfolio_value(
    state: PortfolioState,
    latest_prices: dict[str, float | None],
    fx_rates: dict[str, float],
    valuation_day: date | None = None,
) -> tuple[float, dict[str, float]]:
    values: dict[str, float] = {}
    for symbol, pos in state.positions.items():
        price = latest_prices.get(symbol)
        values[symbol] = position_value_cny(pos, price or 0.0, fx_rates) if price is not None else 0.0
    repo_value = state.cash_cny + sum(_repo_lot_value(lot, valuation_day) for lot in state.repo_lots) + state.dividend_receivable_cny
    values["REPO"] = repo_value
    total = sum(values.values())
    return total, values


def _repo_lot_value(lot: RepoLot, valuation_day: date | None) -> float:
    return lot.principal + _repo_lot_accrued_interest(lot, valuation_day) - lot.fee


def _repo_lot_accrued_interest(lot: RepoLot, valuation_day: date | None) -> float:
    if lot.start_date is None or valuation_day is None:
        return lot.interest
    elapsed_days = min(max((valuation_day - lot.start_date).days, 0), max(lot.actual_days, 1))
    return lot.interest * elapsed_days / max(lot.actual_days, 1)


def _repo_cumulative_profit_cny(state: PortfolioState, valuation_day: date | None) -> float:
    """Economic repo P&L, independent of cash moved to other asset sleeves."""
    accrued_interest = sum(
        _repo_lot_accrued_interest(lot, valuation_day)
        for lot in state.repo_lots
    )
    return state.repo_realized_interest_cny + accrued_interest - state.repo_fees_cny


def _cover_cash_shortfall(
    state: PortfolioState,
    shortfall: float,
    day: date,
    latest_prices: dict[str, float | None],
    fx_rates: dict[str, float],
    fees: dict[str, Any],
    trades: list[dict[str, Any]],
) -> None:
    if state.cash_cny >= shortfall:
        return
    ranked = sorted(
        state.positions.values(),
        # A money-fund selection is the cash pool.  It must be sold before any
        # risk asset regardless of its smaller market value.
        key=lambda p: (
            0 if p.asset_type == "money_fund" else 1,
            -position_value_cny(p, latest_prices.get(p.symbol) or 0.0, fx_rates),
        ),
    )
    for pos in ranked:
        price = latest_prices.get(pos.symbol)
        if not price or pos.quantity <= 0:
            continue
        value = position_value_cny(pos, price, fx_rates)
        needed = shortfall - state.cash_cny
        if needed <= 0:
            break
        qty = min(pos.quantity, pos.quantity * min(1.0, needed / max(value, 1e-9) * 1.05))
        if pos.currency in {"CNY", "HKD"} and pos.asset_type != "cn_bond_index":
            lot_size = position_lot_size(pos, fees)
            qty = math.ceil(qty / lot_size) * lot_size
        _sell_position(state, pos, day, qty, price, fx_rates, fees, trades, "liquidity_shortfall")


def _minimum_rebalance_buy_budget_cny(
    pos: Position,
    minimum_value_cny: float,
    price: float,
    fx_rates: dict[str, float],
    fees: dict[str, Any],
    allow_fractional_us_shares: bool,
) -> float:
    """Return the cost of the smallest executable buy reaching a band edge."""
    minimum_value_cny = max(float(minimum_value_cny), 0.0)
    if minimum_value_cny <= 0 or price <= 0:
        return 0.0
    fx = currency_to_cny_rate(pos.currency, fx_rates)
    minimum_quantity = minimum_value_cny / (price * fx)

    if pos.currency == "USD":
        quantity = minimum_quantity if allow_fractional_us_shares else math.ceil(max(minimum_quantity - 1e-12, 0.0))
        gross_usd = quantity * price
        commission_usd = ibkr_us_etf_fee(
            quantity,
            gross_usd,
            dict_to_dataclass(IbkrFeeConfig, fees["ibkr_us_etf"]),
        )
        cost_cny, _ = cny_cost_for_usd(
            gross_usd + commission_usd,
            fx,
            dict_to_dataclass(FxFeeConfig, fees["fx"]),
        )
        return cost_cny

    if pos.currency == "HKD" or pos.asset_type == "hk_connect_etf":
        hk_cfg = dict_to_dataclass(HkConnectEtfFeeConfig, fees["hk_connect_etf"])
        lot_size = max(int(hk_cfg.lot_size), 1)
        quantity = math.ceil(max(minimum_quantity / lot_size - 1e-12, 0.0)) * lot_size
        gross_hkd = quantity * price
        trade_fee_hkd = hk_connect_etf_trade_fee(gross_hkd, hk_cfg)
        cost_cny, _ = cny_cost_for_hkd(gross_hkd + trade_fee_hkd, fx, hk_cfg)
        return cost_cny

    if pos.asset_type == "cn_bond_index":
        if not pos.estimated_transaction_fees:
            return minimum_value_cny
        return minimum_value_cny + cn_etf_fee(
            minimum_value_cny,
            dict_to_dataclass(CnEtfFeeConfig, fees["cn_etf"]),
        )

    lot_size = position_lot_size(pos, fees)
    quantity = math.ceil(max(minimum_quantity / lot_size - 1e-12, 0.0)) * lot_size
    gross_cny = quantity * price
    return gross_cny + cn_etf_fee(gross_cny, dict_to_dataclass(CnEtfFeeConfig, fees["cn_etf"]))


def _rebalance_state_to_band(
    state: PortfolioState,
    assets: list[dict[str, Any]],
    day: date,
    latest_prices: dict[str, float | None],
    fx_rates: dict[str, float],
    fees: dict[str, Any],
    trades: list[dict[str, Any]],
    allow_fractional_us_shares: bool,
    targets: dict[str, float],
    band: float,
    rebalance_to_target: bool = False,
) -> tuple[float, float, float, float, dict[str, float]]:
    before_rebalance, values = _portfolio_value(state, latest_prices, fx_rates, day)
    current_weights = {key: (value / before_rebalance if before_rebalance else 0.0) for key, value in values.items()}
    cash_symbols = {asset["symbol"] for asset in assets if asset.get("asset_type") == "money_fund"}
    desired_weights = (
        exact_target_weights(targets)
        if rebalance_to_target
        else minimal_rebalance_weights(current_weights, targets, band, cash_symbols)
    )
    fee_before = state.total_fees_cny

    # Freeze trade direction from the pre-rebalance portfolio.  An asset can
    # therefore be bought or sold once, never sold and bought back in the same
    # rebalance merely because fees changed the denominator.
    sell_symbols: set[str] = set()
    buy_symbols: set[str] = set()
    for asset in assets:
        symbol = asset["symbol"]
        price = latest_prices.get(symbol)
        if price is None:
            continue
        current_value = values.get(symbol, 0.0)
        desired_value = before_rebalance * (desired_weights.get(symbol, 0.0) if symbol in targets else 0.0)
        if desired_value - current_value < -1.0:
            sell_symbols.add(symbol)
        elif desired_value - current_value > 1.0 and targets.get(symbol, 0.0) > 0:
            buy_symbols.add(symbol)

    def execute_plan(
        plan_state: PortfolioState,
        plan_trades: list[dict[str, Any]],
        final_total_estimate: float,
    ) -> float:
        plan_turnover = 0.0

        def executable_target_value(symbol: str, side: str) -> float:
            desired_weight = desired_weights.get(symbol, 0.0) if symbol in targets else 0.0
            desired_value = final_total_estimate * desired_weight
            target_weight = max(float(targets.get(symbol, 0.0)), 0.0)
            band_width = target_weight * max(float(band), 0.0)
            if rebalance_to_target or band_width <= 0:
                return desired_value
            # The theoretical minimum lies exactly on the band edge. Keep a
            # tiny interior guard so later same-day fees (for example the repo
            # commission paid when residual cash is invested) cannot make the
            # just-completed rebalance immediately appear out of band.
            guard_weight = min(REBALANCE_EDGE_GUARD_WEIGHT, band_width * 0.1)
            guard_value = final_total_estimate * guard_weight
            return max(desired_value - guard_value, 0.0) if side == "SELL" else desired_value + guard_value

        for asset in assets:
            symbol = asset["symbol"]
            if symbol not in sell_symbols:
                continue
            pos = plan_state.positions[symbol]
            price = latest_prices.get(symbol)
            if price is None:
                continue
            desired_value = executable_target_value(symbol, "SELL")
            current_value = position_value_cny(pos, price, fx_rates)
            sell_value = max(current_value - desired_value, 0.0)
            qty = sell_value / (price * currency_to_cny_rate(pos.currency, fx_rates))
            if pos.currency in {"CNY", "HKD"} and pos.asset_type != "cn_bond_index":
                lot_size = position_lot_size(pos, fees)
                qty = math.ceil(max(qty / lot_size - 1e-12, 0.0)) * lot_size
            prev_cash = plan_state.cash_cny
            _sell_position(plan_state, pos, day, qty, price, fx_rates, fees, plan_trades, "rebalance")
            plan_turnover += max(plan_state.cash_cny - prev_cash, 0.0)

        for asset in assets:
            symbol = asset["symbol"]
            if symbol not in buy_symbols:
                continue
            pos = plan_state.positions[symbol]
            price = latest_prices.get(symbol)
            if price is None:
                continue
            desired_value = executable_target_value(symbol, "BUY")
            current_value = position_value_cny(pos, price, fx_rates)
            minimum_value_cny = max(desired_value - current_value, 0.0)
            budget_cny = _minimum_rebalance_buy_budget_cny(
                pos,
                minimum_value_cny,
                price,
                fx_rates,
                fees,
                allow_fractional_us_shares,
            )
            spent = _buy_position(
                plan_state,
                pos,
                day,
                budget_cny,
                price,
                fx_rates,
                fees,
                plan_trades,
                allow_fractional_us_shares,
                "rebalance",
            )
            plan_turnover += spent
        return plan_turnover

    # Fees reduce final NAV and therefore slightly move every weight boundary.
    # Iterate on a copy until the executable quantities and post-fee NAV agree,
    # then submit each planned symbol only once to the real portfolio.
    final_total_estimate = before_rebalance
    previous_signature: tuple[tuple[str, str, float], ...] | None = None
    for _ in range(8):
        simulated_state = deepcopy(state)
        simulated_trades: list[dict[str, Any]] = []
        execute_plan(simulated_state, simulated_trades, final_total_estimate)
        simulated_total, _ = _portfolio_value(simulated_state, latest_prices, fx_rates, day)
        signature = tuple(
            (str(trade["symbol"]), str(trade["side"]), round(float(trade["quantity"]), 8))
            for trade in simulated_trades
        )
        converged = abs(simulated_total - final_total_estimate) <= 0.01 and signature == previous_signature
        final_total_estimate = simulated_total
        previous_signature = signature
        if converged:
            break

    turnover = execute_plan(state, trades, final_total_estimate)

    after_rebalance, _ = _portfolio_value(state, latest_prices, fx_rates, day)
    return before_rebalance, after_rebalance, turnover, state.total_fees_cny - fee_before, desired_weights


def repo_tenor_days(config: dict[str, Any]) -> int:
    option = selected_repo_option(config)
    symbol = option.get("symbol", "204001")
    if option.get("instrument_type", "repo") != "repo":
        return 1
    if option:
        return int(option.get("tenor_days") or 1)
    try:
        return int(str(symbol)[-3:])
    except ValueError:
        return 1


def repo_fee_config_for_tenor(config: dict[str, Any], tenor_days: int) -> RepoFeeConfig:
    values = dict(config["fees"]["repo"])
    configured_rate = float(values.get("investor_commission_rate", REPO_COMMISSION_RATE_BY_TENOR[1]))
    # The default one-day value activates the official tenor schedule.  A
    # non-default value remains an explicit broker-specific override (including
    # zero-commission accounts) and is applied to every selected tenor.
    if math.isclose(configured_rate, REPO_COMMISSION_RATE_BY_TENOR[1], rel_tol=0.0, abs_tol=1e-12):
        values["investor_commission_rate"] = REPO_COMMISSION_RATE_BY_TENOR.get(
            int(tenor_days),
            REPO_COMMISSION_RATE_BY_TENOR[1],
        )
    return dict_to_dataclass(RepoFeeConfig, values)


def _repo_spend_reserve(
    day: date,
    tenor_days: int,
    monthly_spend_days: set[date],
    monthly_spend_cny: float,
    spend_day_ordinals: list[int] | None = None,
    trading_days: list[date] | None = None,
) -> float:
    maturity = repo_maturity_day(day, tenor_days, trading_days)
    if spend_day_ordinals is None:
        spend_count = sum(1 for spend_day in monthly_spend_days if day < spend_day < maturity)
    else:
        spend_count = bisect_left(spend_day_ordinals, maturity.toordinal()) - bisect_right(
            spend_day_ordinals, day.toordinal()
        )
    return spend_count * monthly_spend_cny


def _invest_idle_cash_in_repo(
    state: PortfolioState,
    day: date,
    repo_rate: float | None,
    fees: dict[str, Any],
    tenor_days: int,
    reserve_cny: float = 0.0,
    repo_fee_config: RepoFeeConfig | None = None,
    trading_days: list[date] | None = None,
) -> None:
    if repo_rate is None:
        return
    lot_size = float(fees["repo"].get("lot_size_cny", 1000.0))
    investable_cash = max(state.cash_cny - max(reserve_cny, 0.0), 0.0)
    investable = math.floor(investable_cash / lot_size) * lot_size
    if investable >= lot_size:
        actual_days = repo_actual_days(day, tenor_days, trading_days)
        interest = repo_interest(investable, repo_rate, actual_days)
        if repo_fee_config is None:
            fallback_values = dict(fees["repo"])
            configured_rate = float(
                fallback_values.get("investor_commission_rate", REPO_COMMISSION_RATE_BY_TENOR[1])
            )
            if math.isclose(configured_rate, REPO_COMMISSION_RATE_BY_TENOR[1], rel_tol=0.0, abs_tol=1e-12):
                fallback_values["investor_commission_rate"] = REPO_COMMISSION_RATE_BY_TENOR.get(
                    int(tenor_days),
                    REPO_COMMISSION_RATE_BY_TENOR[1],
                )
            repo_fee_config = dict_to_dataclass(RepoFeeConfig, fallback_values)
        fee = repo_fee(investable, repo_fee_config)
        state.cash_cny -= investable
        state.total_fees_cny += fee
        state.repo_fees_cny += fee
        state.repo_lots.append(
            RepoLot(
                principal=investable,
                maturity_date=repo_maturity_day(day, tenor_days, trading_days),
                interest=interest,
                fee=fee,
                start_date=day,
                actual_days=actual_days,
            )
        )


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
    rebalance_days_set: set[date] | None = None,
    extra_reserve_cny: float = 0.0,
    repo_fee_config: RepoFeeConfig | None = None,
    spend_day_ordinals: list[int] | None = None,
    trading_days: list[date] | None = None,
    one_day_repo_fee_config: RepoFeeConfig | None = None,
) -> None:
    reserve_cny = _repo_spend_reserve(
        day,
        selected_tenor_days,
        monthly_spend_days,
        monthly_spend_cny,
        spend_day_ordinals,
        trading_days,
    ) + max(extra_reserve_cny, 0.0)
    maturity = repo_maturity_day(day, selected_tenor_days, trading_days)
    crosses_rebalance = any(day < rebalance_day < maturity for rebalance_day in (rebalance_days_set or set()))
    rate_for_selected_tenor = None if crosses_rebalance else selected_repo_rate
    _invest_idle_cash_in_repo(
        state,
        day,
        rate_for_selected_tenor,
        fees,
        selected_tenor_days,
        reserve_cny,
        repo_fee_config,
        trading_days,
    )
    overnight_reserve_cny = _next_spend_reserve(day, monthly_spend_days, monthly_spend_cny) + max(extra_reserve_cny, 0.0)
    _invest_idle_cash_in_repo(
        state,
        day,
        one_day_repo_rate,
        fees,
        1,
        overnight_reserve_cny,
        one_day_repo_fee_config,
        trading_days,
    )


def _mature_repo_lots(state: PortfolioState, day: date) -> None:
    matured = [lot for lot in state.repo_lots if lot.maturity_date <= day]
    state.repo_lots = [lot for lot in state.repo_lots if lot.maturity_date > day]
    for lot in matured:
        state.cash_cny += lot.principal + lot.interest - lot.fee
        state.repo_realized_interest_cny += lot.interest


def _execute_dip_buys(
    state: PortfolioState,
    orders: list[dict[str, Any]],
    day: date,
    open_prices: dict[str, float | None],
    fx_rates: dict[str, float],
    fees: dict[str, Any],
    trades: list[dict[str, Any]],
    allow_fractional_us_shares: bool,
    money_fund_symbol: str | None = None,
    max_total_budget_cny: float | None = None,
) -> list[dict[str, Any]]:
    """Execute queued orders at this open without spending the protected pool.

    A selected money fund is sold at the same open when cash is insufficient.
    Repo lots are not sold early: the caller matures lots before execution, so
    only proceeds contractually available at this open can be used.
    """
    executed: list[dict[str, Any]] = []
    remaining_budget = math.inf if max_total_budget_cny is None else max(float(max_total_budget_cny), 0.0)
    for order in orders:
        if remaining_budget <= 0:
            break
        symbol = str(order["symbol"])
        position = state.positions.get(symbol)
        price = open_prices.get(symbol)
        if not position or price is None or price <= 0:
            continue
        requested_budget = min(max(float(order["budget_cny"]), 0.0), remaining_budget)
        if requested_budget <= 0:
            continue
        if state.cash_cny < requested_budget and money_fund_symbol and money_fund_symbol != symbol:
            money_position = state.positions.get(money_fund_symbol)
            money_price = open_prices.get(money_fund_symbol)
            if money_position and money_position.quantity > 0 and money_price is not None and money_price > 0:
                needed = requested_budget - state.cash_cny
                lot_size = position_lot_size(money_position, fees)
                raw_quantity = needed / (float(money_price) * currency_to_cny_rate(money_position.currency, fx_rates))
                sell_quantity = min(money_position.quantity, math.ceil(raw_quantity / lot_size) * lot_size)
                _sell_position(
                    state,
                    money_position,
                    day,
                    sell_quantity,
                    float(money_price),
                    fx_rates,
                    fees,
                    trades,
                    "dip_buy_funding",
                )
        if state.cash_cny <= 0:
            continue
        quantity_before = position.quantity
        spent = _buy_position(
            state,
            position,
            day,
            min(requested_budget, state.cash_cny),
            float(price),
            fx_rates,
            fees,
            trades,
            allow_fractional_us_shares,
            "dip_buy",
        )
        if spent > 0:
            executed_order = dict(order)
            level_orders = list(order.get("level_orders", []))
            if level_orders:
                funded_level_orders: list[dict[str, Any]] = []
                cumulative_budget_cny = 0.0
                for level_order in level_orders:
                    if spent > cumulative_budget_cny + 1e-8:
                        funded_level_orders.append(level_order)
                    cumulative_budget_cny += max(float(level_order.get("budget_cny", 0.0)), 0.0)
                if funded_level_orders:
                    funded_levels = [int(item["level"]) for item in funded_level_orders]
                    executed_order["levels"] = funded_levels
                    executed_order["level"] = max(funded_levels)
                    executed_order["requested_parts"] = sum(
                        int(item.get("requested_parts", 1)) for item in funded_level_orders
                    )
                    executed_order["parts"] = sum(
                        int(item.get("parts", 1)) for item in funded_level_orders
                    )
                    executed_order["budget_cny"] = sum(
                        float(item.get("budget_cny", 0.0)) for item in funded_level_orders
                    )
            executed.append(
                {
                    **executed_order,
                    "spent_cny": spent,
                    "bought_quantity": max(position.quantity - quantity_before, 0.0),
                    "execution_date": day.isoformat(),
                }
            )
            remaining_budget -= spent
    return executed


def _execute_dip_recovery_sells(
    state: PortfolioState,
    orders: list[dict[str, Any]],
    day: date,
    open_prices: dict[str, float | None],
    fx_rates: dict[str, float],
    fees: dict[str, Any],
    trades: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Sell only quantities bought by the dip ladder at the next real open."""
    executed: list[dict[str, Any]] = []
    for order in orders:
        symbol = str(order["symbol"])
        position = state.positions.get(symbol)
        price = open_prices.get(symbol)
        requested_quantity = max(float(order.get("quantity", 0.0)), 0.0)
        if not position or position.quantity <= 0 or price is None or price <= 0 or requested_quantity <= 0:
            continue
        quantity_before = position.quantity
        proceeds = _sell_position(
            state,
            position,
            day,
            min(requested_quantity, position.quantity),
            float(price),
            fx_rates,
            fees,
            trades,
            "dip_buy_recovery",
        )
        sold_quantity = max(quantity_before - position.quantity, 0.0)
        if proceeds > 0 and sold_quantity > 0:
            executed.append(
                {
                    **order,
                    "sold_quantity": sold_quantity,
                    "proceeds_cny": proceeds,
                    "execution_date": day.isoformat(),
                }
            )
    return executed


def _execute_auto_route_switches(
    state: PortfolioState,
    previous_routes: dict[str, str],
    current_routes: dict[str, str],
    day: date,
    execution_prices: dict[str, float | None],
    fx_rates: dict[str, float],
    fees: dict[str, Any],
    trades: list[dict[str, Any]],
    assets_by_symbol: dict[str, dict[str, Any]],
    allow_fractional_us_shares: bool,
) -> list[dict[str, Any]]:
    """Move an existing proxy holding into its real ETF at the first open.

    Only replacements explicitly marked ``auto_switch_on_trade_start`` are
    eligible.  The old proxy is valued at its latest previously published
    close, while the new ETF uses that session's real opening quote.
    """
    switches: list[dict[str, Any]] = []
    for logical_symbol, new_symbol in current_routes.items():
        old_symbol = previous_routes.get(logical_symbol)
        if not old_symbol or old_symbol == new_symbol:
            continue
        new_asset = assets_by_symbol.get(new_symbol, {})
        if not new_asset.get("auto_switch_on_trade_start"):
            continue
        old_position = state.positions.get(old_symbol)
        new_position = state.positions.get(new_symbol)
        old_price = execution_prices.get(old_symbol)
        new_price = execution_prices.get(new_symbol)
        if (
            not old_position
            or not new_position
            or old_position.quantity <= 0
            or old_price is None
            or old_price <= 0
            or new_price is None
            or new_price <= 0
        ):
            continue
        fee_before = state.total_fees_cny
        proceeds = _sell_position(
            state,
            old_position,
            day,
            old_position.quantity,
            float(old_price),
            fx_rates,
            fees,
            trades,
            "asset_replacement",
        )
        spent = _buy_position(
            state,
            new_position,
            day,
            proceeds,
            float(new_price),
            fx_rates,
            fees,
            trades,
            allow_fractional_us_shares,
            "asset_replacement",
        )
        switches.append(
            {
                "trade_date": day.isoformat(),
                "logical_symbol": logical_symbol,
                "from_symbol": old_symbol,
                "to_symbol": new_symbol,
                "proceeds_cny": proceeds,
                "spent_cny": spent,
                "fee_cny": state.total_fees_cny - fee_before,
            }
        )
    return switches


def _apply_dividend_events(
    state: PortfolioState,
    day_str: str,
    ex_events: dict[str, list[dict[str, Any]]],
    pay_events: dict[str, list[dict[str, Any]]],
    fx_rates: dict[str, float],
    fees: dict[str, Any],
    dividends_by_symbol: dict[str, float] | None = None,
) -> float:
    _ = pay_events  # The receivable schedule is keyed from each ex-date event's own pay date.
    for event in ex_events.get(day_str, []):
        pos = state.positions.get(event["symbol"])
        if not pos or pos.quantity <= 0:
            continue
        dividend = (
            pos.quantity
            * float(event["div_cash"])
            * float(event.get("normalized_share_scale", 1.0) or 1.0)
        )
        if event["currency"] == "USD":
            fx = currency_to_cny_rate("USD", fx_rates)
            tax_rate = float(fees["tax"].get("us_dividend_withholding_rate", 0.10))
            tax = dividend * tax_rate * fx
            state.total_withheld_tax_cny += tax
            net_dividend_cny = dividend * (1.0 - tax_rate) * fx
        elif event["currency"] == "HKD":
            fx = currency_to_cny_rate("HKD", fx_rates)
            tax_rate = float(fees["tax"].get("hk_dividend_withholding_rate", 0.0))
            tax = dividend * tax_rate * fx
            state.total_withheld_tax_cny += tax
            net_dividend_cny = dividend * (1.0 - tax_rate) * fx
        else:
            tax_rate = float(fees["tax"].get("cn_fund_dividend_tax_rate", 0.0))
            tax = dividend * tax_rate
            state.total_withheld_tax_cny += tax
            net_dividend_cny = dividend * (1.0 - tax_rate)
        pay_date = str(event.get("pay_date") or day_str)
        state.dividend_receivable_cny += net_dividend_cny
        state.total_dividend_cny += net_dividend_cny
        if dividends_by_symbol is not None:
            dividends_by_symbol[event["symbol"]] = dividends_by_symbol.get(event["symbol"], 0.0) + net_dividend_cny
        state.dividend_receivables_by_pay_date[pay_date] = (
            state.dividend_receivables_by_pay_date.get(pay_date, 0.0) + net_dividend_cny
        )

    due_dates = [pay_date for pay_date in state.dividend_receivables_by_pay_date if pay_date <= day_str]
    paid_dividend_cny = sum(state.dividend_receivables_by_pay_date.pop(pay_date) for pay_date in due_dates)
    if paid_dividend_cny > 0:
        state.dividend_receivable_cny = max(state.dividend_receivable_cny - paid_dividend_cny, 0.0)
        state.cash_cny += paid_dividend_cny
    return paid_dividend_cny


def _apply_hk_connect_portfolio_fee(
    state: PortfolioState,
    latest_prices: dict[str, float | None],
    fx_rates: dict[str, float],
    fees: dict[str, Any],
    calendar_days: int = 1,
    fees_by_symbol: dict[str, float] | None = None,
) -> None:
    hkd_cny = fx_rates.get("HKD/CNY")
    if hkd_cny is None:
        return
    hk_cfg = dict_to_dataclass(HkConnectEtfFeeConfig, fees["hk_connect_etf"])
    total_fee_cny = 0.0
    for pos in state.positions.values():
        if pos.quantity <= 0 or not (pos.currency == "HKD" or pos.asset_type == "hk_connect_etf"):
            continue
        price = latest_prices.get(pos.symbol)
        if price is None:
            continue
        fee_hkd = hk_connect_portfolio_fee(pos.quantity * price, hk_cfg, calendar_days)
        fee_cny = fee_hkd * hkd_cny
        total_fee_cny += fee_cny
        if fees_by_symbol is not None and fee_cny > 0:
            fees_by_symbol[pos.symbol] = fees_by_symbol.get(pos.symbol, 0.0) + fee_cny
    if total_fee_cny > 0:
        state.cash_cny -= total_fee_cny
        state.total_fees_cny += total_fee_cny


def _initial_state(capital_cny: float, assets: list[dict[str, Any]]) -> PortfolioState:
    state = PortfolioState(cash_cny=capital_cny)
    for asset in assets:
        state.positions[asset["symbol"]] = Position(
            symbol=asset["symbol"],
            market=asset["market"],
            currency=asset["currency"],
            asset_type=asset.get("asset_type", "etf"),
            estimated_transaction_fees=bool(asset.get("estimated_transaction_fees", False)),
        )
    return state


def instrument_coverage_summaries(
    config: dict[str, Any],
    trading_days: list[date],
    price_maps: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    """Describe how much of the requested period used proxy versus real ETF data."""
    summaries: list[dict[str, Any]] = []
    for asset in config.get("assets", []):
        if (
            not asset.get("enabled", True)
            or float(asset.get("target_weight", 0.0) or 0.0) <= 0
            or not asset.get("methodology_disclosure")
        ):
            continue
        route_config = {**config, "assets": [asset]}
        prepared = prepare_active_asset_routes(route_config)
        route_assets = [asset, *replacement_assets(asset)]
        proxy = price_proxy_asset(asset)
        if proxy:
            route_assets.append(proxy)
        metadata = {str(item["symbol"]): item for item in route_assets}
        latest: dict[str, float | None] = {symbol: None for symbol in metadata}
        proxy_dates: list[str] = []
        tradable_dates: list[str] = []
        tradable_symbols: set[str] = set()
        for trading_day in trading_days:
            day_text = trading_day.isoformat()
            for symbol in latest:
                observed = price_maps.get(symbol, {}).get(day_text)
                if observed is not None:
                    latest[symbol] = observed
            selected_symbol = active_route_symbols(trading_day, latest, prepared).get(str(asset["symbol"]))
            if not selected_symbol:
                continue
            selected = metadata.get(selected_symbol, {})
            if selected.get("tradable", True) is False:
                proxy_dates.append(day_text)
            else:
                tradable_dates.append(day_text)
                tradable_symbols.add(selected_symbol)
        covered_days = len(proxy_dates) + len(tradable_dates)
        if not covered_days:
            continue
        if proxy_dates and tradable_dates:
            coverage_mode = "mixed_proxy_and_etf"
        elif tradable_dates:
            coverage_mode = "actual_etf_only"
        else:
            coverage_mode = "proxy_only"
        replacements = replacement_assets(asset)
        actual_etf = next((item for item in replacements if item.get("tradable", True)), None)
        summaries.append(
            {
                "key": asset.get("key") or asset["symbol"],
                "name": asset.get("name") or asset["symbol"],
                "logical_symbol": asset["symbol"],
                "coverage_mode": coverage_mode,
                "covered_days": covered_days,
                "proxy_days": len(proxy_dates),
                "proxy_start_date": proxy_dates[0] if proxy_dates else None,
                "proxy_end_date": proxy_dates[-1] if proxy_dates else None,
                "tradable_etf_days": len(tradable_dates),
                "tradable_etf_start_date": tradable_dates[0] if tradable_dates else None,
                "tradable_etf_end_date": tradable_dates[-1] if tradable_dates else None,
                "tradable_etf_coverage_ratio": len(tradable_dates) / covered_days,
                "tradable_symbols": sorted(tradable_symbols),
                "configured_etf_symbol": actual_etf.get("symbol") if actual_etf else None,
                "configured_etf_trade_start_date": (
                    actual_etf.get("allocation_start_date")
                    or actual_etf.get("trade_start_date")
                    or actual_etf.get("inception_date")
                    if actual_etf
                    else None
                ),
                "proxy_annual_expense_drag_rate": float(asset.get("proxy_annual_expense_drag_rate", 0.0) or 0.0),
                "proxy_transaction_fee_model": "configured_cn_etf_commission"
                if asset.get("estimated_transaction_fees")
                else "none",
                "proxy_execution_price_model": "initial_available_close_then_previous_published_close",
                "actual_etf_fund_expense_in_market_price": True,
            }
        )
    return summaries


def _daily_asset_profit_cny(
    previous_values: dict[str, float],
    current_values: dict[str, float],
    daily_trades: list[dict[str, Any]],
    dividends_by_symbol: dict[str, float],
    holding_fees_by_symbol: dict[str, float],
    repo_profit_change_cny: float,
) -> dict[str, float]:
    """Attribute daily mark-to-market P&L without treating traded principal as profit."""
    symbols = (
        set(previous_values)
        | set(current_values)
        | {str(trade["symbol"]) for trade in daily_trades}
        | set(dividends_by_symbol)
        | set(holding_fees_by_symbol)
    )
    symbols.discard("REPO")
    profit = {
        symbol: float(current_values.get(symbol, 0.0)) - float(previous_values.get(symbol, 0.0))
        for symbol in symbols
    }
    for trade in daily_trades:
        symbol = str(trade["symbol"])
        payload = json.loads(trade.get("payload_json") or "{}")
        if trade.get("side") == "BUY":
            profit[symbol] = profit.get(symbol, 0.0) - float(payload.get("spent_cny") or 0.0)
        elif trade.get("side") == "SELL":
            profit[symbol] = profit.get(symbol, 0.0) + float(payload.get("cash_cny") or 0.0)
    for symbol, dividend_cny in dividends_by_symbol.items():
        profit[symbol] = profit.get(symbol, 0.0) + float(dividend_cny)
    for symbol, fee_cny in holding_fees_by_symbol.items():
        profit[symbol] = profit.get(symbol, 0.0) - float(fee_cny)
    profit["REPO"] = float(repo_profit_change_cny)
    return {symbol: value for symbol, value in profit.items() if abs(value) > 1e-10 or symbol in current_values}


def _asset_period_performance(
    previous_values: dict[str, float],
    current_values: dict[str, float],
    ordered_symbols: list[str],
    external_flows: dict[str, float] | None = None,
    profit_overrides: dict[str, float] | None = None,
) -> dict[str, dict[str, float | None]]:
    flows = external_flows or {}
    overrides = profit_overrides or {}
    result: dict[str, dict[str, float | None]] = {}
    keys = [symbol for symbol in ordered_symbols if symbol in previous_values or symbol in current_values]
    for key in sorted((set(previous_values) | set(current_values)) - set(keys)):
        keys.append(key)
    for key in keys:
        start_value = float(previous_values.get(key, 0.0) or 0.0)
        end_value = float(current_values.get(key, 0.0) or 0.0)
        external_flow = float(flows.get(key, 0.0) or 0.0)
        profit = float(overrides[key]) if key in overrides else end_value - start_value - external_flow
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
    broad_assets = [
        asset
        for asset in config["assets"]
        if asset.get("exclusive_group") == "cn_broad_etf" and asset.get("enabled", True)
    ]
    broad_asset = broad_assets[0] if broad_assets else by_symbol.get("510300.SH")
    hs300_weight = sum(
        float(by_symbol[symbol].get("target_weight", 0.0))
        for symbol in ("VOO", "03195.HK", "513500.SH", "512890.SH")
        if by_symbol.get(symbol, {}).get("enabled", True)
    )
    if broad_asset and broad_asset.get("enabled", True):
        hs300_weight += float(broad_asset.get("target_weight", 0.0))
    result: list[dict[str, Any]] = []
    if broad_asset and hs300_weight > 0:
        result.append({**broad_asset, "target_weight": hs300_weight, "enabled": True})
    gold = by_symbol.get("518880.SH")
    if gold and gold.get("enabled", True) and float(gold.get("target_weight", 0.0)) > 0:
        result.append({**gold, "enabled": True})
    return result


def _simulate_comparison_series(
    config: dict[str, Any],
    days: list[date],
    price_maps: dict[str, dict[str, float]],
    open_price_maps: dict[str, dict[str, float]],
    fx_maps: dict[str, dict[str, float]],
    repo_map: dict[str, float],
    one_day_repo_map: dict[str, float],
    ex_events: dict[str, list[dict[str, Any]]],
    pay_events: dict[str, list[dict[str, Any]]],
    monthly_spend_days: set[date],
    reb_days: set[date],
    should_cancel=None,
) -> dict[str, float]:
    assets = comparison_assets(config)
    comparison_config = {**config, "assets": assets, "repo_symbol": repo_rate_symbol(config)}
    sim_assets = simulation_assets(assets)
    initial_capital_cny = float(config["initial_capital_cny"])
    state = _initial_state(initial_capital_cny, sim_assets)
    symbols = [asset["symbol"] for asset in sim_assets]
    latest_prices: dict[str, float | None] = {symbol: None for symbol in symbols}
    latest_open_prices: dict[str, float | None] = {symbol: None for symbol in symbols}
    comparison_fx_pairs = required_fx_pairs_for_assets(assets)
    latest_fx_rates: dict[str, float | None] = {pair: None for pair in comparison_fx_pairs}
    latest_repo_rate: float | None = None
    latest_one_day_repo_rate: float | None = None
    tenor_days = repo_tenor_days(comparison_config)
    repo_fee_config = repo_fee_config_for_tenor(config, tenor_days)
    one_day_repo_fee_config = repo_fee_config_for_tenor(config, 1)
    prepared_routes = prepare_active_asset_routes(comparison_config)
    monthly_spend_ordinals = sorted(day.toordinal() for day in monthly_spend_days)
    totals: dict[str, float] = {}
    trades: list[dict[str, Any]] = []
    initial_rebalance_done = False
    previous_fee_day: date | None = None
    pending_rebalance: dict[str, Any] | None = None
    rebalance_to_target = bool(config.get("rebalance_to_target", False))

    for idx, day in enumerate(days):
        if idx % 64 == 0:
            raise_if_cancelled(should_cancel)
        day_str = day.isoformat()
        for symbol in latest_open_prices:
            latest_open_prices[symbol] = forward_value(open_price_maps.get(symbol, {}), day, latest_open_prices.get(symbol))
        for symbol in latest_prices:
            latest_prices[symbol] = forward_value(price_maps.get(symbol, {}), day, latest_prices.get(symbol))
        for pair in latest_fx_rates:
            latest_fx_rates[pair] = forward_value(fx_maps.get(pair, {}), day, latest_fx_rates.get(pair))
        repo_rate = repo_map.get(day_str)
        if repo_rate is not None:
            latest_repo_rate = repo_rate
        one_day_repo_rate = one_day_repo_map.get(day_str)
        if one_day_repo_rate is not None:
            latest_one_day_repo_rate = one_day_repo_rate
        if any(value is None for value in latest_fx_rates.values()):
            continue
        fx_rates = {pair: float(value) for pair, value in latest_fx_rates.items() if value is not None}

        _mature_repo_lots(state, day)
        _apply_dividend_events(state, day_str, ex_events, pay_events, fx_rates, config["fees"])
        if pending_rebalance and pending_rebalance["execution_date"] == day_str:
            open_prices = {
                symbol: latest_open_prices.get(symbol) if latest_open_prices.get(symbol) is not None else latest_prices.get(symbol)
                for symbol in latest_prices
            }
            open_total, open_values = _portfolio_value(state, open_prices, fx_rates, day)
            open_weights = {key: (value / open_total if open_total else 0.0) for key, value in open_values.items()}
            if should_rebalance(open_weights, pending_rebalance["targets"], float(config["rebalance_band"])):
                _rebalance_state_to_band(
                    state,
                    sim_assets,
                    day,
                    open_prices,
                    fx_rates,
                    config["fees"],
                    trades,
                    False,
                    pending_rebalance["targets"],
                    float(config["rebalance_band"]),
                    rebalance_to_target,
                )
            pending_rebalance = None
        fee_days = max((day - previous_fee_day).days, 1) if previous_fee_day else 1
        _apply_hk_connect_portfolio_fee(state, latest_prices, fx_rates, config["fees"], fee_days)
        previous_fee_day = day

        if day in monthly_spend_days:
            spend = float(config["monthly_spend_cny"])
            if state.cash_cny < spend:
                _cover_cash_shortfall(state, spend, day, latest_prices, fx_rates, config["fees"], trades)
            actual_spend = min(spend, state.cash_cny)
            state.cash_cny -= actual_spend
            state.total_spend_cny += actual_spend

        before_total, before_values = _portfolio_value(state, latest_prices, fx_rates, day)
        targets = effective_weights(
            comparison_config,
            day,
            latest_prices,
            before_total,
            prepared_routes=prepared_routes,
            money_fund_asset=None,
        )
        current_weights = {key: (value / before_total if before_total else 0.0) for key, value in before_values.items()}
        is_rebalance_day = not initial_rebalance_done
        if is_rebalance_day and should_rebalance(current_weights, targets, float(config["rebalance_band"])):
            _rebalance_state_to_band(
                state,
                sim_assets,
                day,
                latest_prices,
                fx_rates,
                config["fees"],
                trades,
                False,
                targets,
                float(config["rebalance_band"]),
                not initial_rebalance_done or rebalance_to_target,
            )
        if is_rebalance_day and has_investable_asset_target(targets):
            initial_rebalance_done = True
        elif is_rebalance_day and not initial_rebalance_done and has_deferred_inception_target(comparison_config, day):
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
            reb_days,
            0.0,
            repo_fee_config,
            monthly_spend_ordinals,
            days,
            one_day_repo_fee_config,
        )
        total, _values = _portfolio_value(state, latest_prices, fx_rates, day)
        next_day = days[idx + 1] if idx + 1 < len(days) else None
        if next_day is not None and next_day in reb_days and initial_rebalance_done and pending_rebalance is None:
            next_day_str = next_day.isoformat()
            scheduled_prices = scheduled_target_price_view(latest_prices, next_day, prepared_routes)
            pending_rebalance = {
                "execution_date": next_day_str,
                "targets": effective_weights(
                    comparison_config,
                    next_day,
                    scheduled_prices,
                    total,
                    prepared_routes=prepared_routes,
                    money_fund_asset=None,
                ),
            }
        totals[day_str] = total
    return totals


def run_backtest(
    conn,
    user_config: dict[str, Any] | None = None,
    should_cancel=None,
    *,
    persist: bool = True,
    include_comparison: bool = True,
    include_month_analysis: bool = True,
    include_rolling_analysis: bool = True,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    raise_if_cancelled(should_cancel)
    config = normalize_config(user_config)
    errors = validate_config(config)
    if errors:
        raise BacktestError("; ".join(errors))
    dip_buy_active = bool(config.get("dip_buy_enabled")) and config.get("rebalance_frequency") == "yearly"
    config_hash = canonical_config_hash(config)
    if persist:
        cached = get_cached_backtest_run(conn, config)
        if cached:
            logger.info("run_backtest cache hit range=%s..%s seconds=%.3f", config["start_date"], config["end_date"], time.perf_counter() - started_at)
            return cached

    raise_if_cancelled(should_cancel)
    start = config["start_date"]
    end = config["end_date"]
    all_assets = backtest_assets(config)
    repo_symbol = repo_rate_symbol(config)
    logger.info(
        "run_backtest start range=%s..%s assets=%s treasury=%s repo_rate=%s",
        start,
        end,
        [asset["symbol"] for asset in all_assets],
        config["repo_symbol"],
        repo_symbol,
    )
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
    raise_if_cancelled(should_cancel)
    sim_assets = simulation_assets(all_assets)
    symbols = [asset["symbol"] for asset in sim_assets]
    share_splits = configured_share_splits(all_assets)
    benchmark_symbol = "000300.SH"
    load_started_at = time.perf_counter()
    share_scale_maps: dict[str, dict[str, float]] = {}
    price_maps = load_price_map(
        conn,
        symbols + [benchmark_symbol],
        start,
        end,
        share_splits=share_splits,
        share_scale_maps=share_scale_maps,
    )
    open_price_maps = load_price_map(conn, symbols + [benchmark_symbol], start, end, "open", share_splits)
    attach_proxy_price_maps(price_maps, all_assets)
    attach_proxy_price_maps(open_price_maps, all_assets)
    attach_nontradable_route_expense_drag(price_maps, all_assets)
    needed_fx_pairs = required_fx_pairs_for_assets(all_assets)
    fx_maps = load_fx_maps(conn, needed_fx_pairs, start, end)
    repo_map = load_repo_map(conn, repo_symbol, start, end)
    one_day_repo_map = load_repo_map(conn, "204001", start, end)
    # 511990's exchange price includes its daily holding-period income.  Old
    # databases may contain rows from the generic dividend synchronizer; never
    # add those to the already total-return market price.
    dividend_symbols = [asset["symbol"] for asset in sim_assets if asset.get("asset_type") != "money_fund"]
    ex_events, pay_events = load_dividend_events(
        conn,
        dividend_symbols,
        start,
        end,
        share_scale_maps,
    )
    days = reference_trading_days(start, end, price_maps.get(benchmark_symbol, {}), repo_map)
    attach_prior_published_close_execution_maps(open_price_maps, price_maps, all_assets, days)
    logger.info(
        "run_backtest data loaded days=%d price_rows=%d fx_rows=%d repo_rows=%d one_day_repo_rows=%d dividend_events=%d seconds=%.3f",
        len(days),
        sum(len(values) for values in price_maps.values()),
        sum(len(values) for values in fx_maps.values()),
        len(repo_map),
        len(one_day_repo_map),
        sum(len(events) for events in ex_events.values()),
        time.perf_counter() - load_started_at,
    )

    raise_if_cancelled(should_cancel)
    if any(not fx_maps.get(pair) for pair in needed_fx_pairs) or not repo_map:
        raise BacktestError("missing fx_rates or repo_rates; run data sync first")
    if len(days) < 2:
        raise BacktestError("backtest needs at least two reference-market trading days")

    initial_capital_cny = float(config["initial_capital_cny"])
    state = _initial_state(initial_capital_cny, sim_assets)

    run_id = str(uuid.uuid4())
    trades: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    daily_payloads: list[dict[str, Any]] = []
    rebalance_rows: list[dict[str, Any]] = []
    daily_total_assets: list[float] = []
    daily_flows: list[float] = []
    benchmark_values: list[float | None] = []
    repo_benchmark_nav = 1.0
    repo_benchmark_values: list[float] = []

    monthly_spend_days = first_business_day_by_month(days)
    monthly_spend_ordinals = sorted(day.toordinal() for day in monthly_spend_days)
    reb_days = rebalance_days(days, config["rebalance_frequency"], int(config["annual_rebalance_month"]))
    latest_prices: dict[str, float | None] = {symbol: None for symbol in symbols + [benchmark_symbol]}
    latest_open_prices: dict[str, float | None] = {symbol: None for symbol in symbols + [benchmark_symbol]}
    latest_fx_rates: dict[str, float | None] = {pair: None for pair in needed_fx_pairs}
    latest_repo_rate: float | None = None
    latest_one_day_repo_rate: float | None = None
    tenor_days = repo_tenor_days(config)
    repo_fee_config = repo_fee_config_for_tenor(config, tenor_days)
    one_day_repo_fee_config = repo_fee_config_for_tenor(config, 1)
    period_start_nav = 1.0
    nav_for_period = 1.0
    period_peak_nav = 1.0
    period_max_drawdown = 0.0
    previous_rebalance_values: dict[str, float] = {"REPO": float(config["initial_capital_cny"])}
    previous_rebalance_repo_profit_cny = 0.0
    performance_symbols = [asset["symbol"] for asset in sim_assets] + ["REPO"]
    period_external_flows: dict[str, float] = {"REPO": 0.0}
    initial_rebalance_done = False
    money_fund = selected_money_fund_asset(config)
    prepared_routes = prepare_active_asset_routes(config)
    assets_by_symbol = {str(asset["symbol"]): asset for asset in sim_assets}
    previous_route_symbols: dict[str, str] = {}
    route_switch_events: list[dict[str, Any]] = []
    rebalance_to_target = bool(config.get("rebalance_to_target", False))
    previous_treasury_target = "REPO"
    previous_fee_day: date | None = None
    pending_rebalance: dict[str, Any] | None = None
    pending_dip_buys: list[dict[str, Any]] = []
    pending_dip_recovery_sells: list[dict[str, Any]] = []
    last_rebalance_day: date | None = None
    dip_buy_confirmed_cash_equivalent_cny = 0.0
    dip_buy_cash_buffer_locked_cny = 0.0
    dip_buy_pool_cny = 0.0
    dip_buy_piece_cny = 0.0
    dip_buy_remaining_parts = 0
    dip_buy_triggered_levels: dict[str, set[int]] = {}
    dip_buy_initial_cost_prices: dict[str, float] = {}
    dip_buy_initial_investment_cny: dict[str, float] = {}
    dip_buy_cumulative_spend_cny: dict[str, float] = {}
    dip_buy_recovery_lots: dict[str, list[dict[str, Any]]] = {}
    deferred_dip_rechecks: dict[str, date] = {}
    dip_buy_execution_count = 0
    dip_buy_recovery_sell_count = 0
    year_nav = 1.0
    year_peak_nav = 1.0
    year_max_drawdown = 0.0
    year_fee_start = 0.0
    year_start_values: dict[str, float] = {"REPO": float(config["initial_capital_cny"])}
    year_start_repo_profit_cny = 0.0
    year_external_flows: dict[str, float] = {"REPO": 0.0}
    year_start_total_cny = initial_capital_cny
    year_external_flow_cny = 0.0
    year_start_date = start
    current_year: int | None = None
    last_close_values: dict[str, float] = {"REPO": float(config["initial_capital_cny"])}
    last_close_repo_profit_cny = 0.0

    loop_started_at = time.perf_counter()
    for idx, day in enumerate(days):
        if idx % 64 == 0:
            raise_if_cancelled(should_cancel)
        day_str = day.isoformat()
        flow = 0.0
        daily_trade_start_index = len(trades)
        daily_dividends_by_symbol: dict[str, float] = {}
        daily_holding_fees_by_symbol: dict[str, float] = {}
        for symbol in latest_open_prices:
            latest_open_prices[symbol] = forward_value(open_price_maps.get(symbol, {}), day, latest_open_prices.get(symbol))
        for symbol in latest_prices:
            latest_prices[symbol] = forward_value(price_maps.get(symbol, {}), day, latest_prices.get(symbol))
        for pair in latest_fx_rates:
            latest_fx_rates[pair] = forward_value(fx_maps.get(pair, {}), day, latest_fx_rates.get(pair))
        repo_rate = repo_map.get(day_str)
        if repo_rate is not None:
            latest_repo_rate = repo_rate
        one_day_repo_rate = one_day_repo_map.get(day_str)
        if one_day_repo_rate is not None:
            latest_one_day_repo_rate = one_day_repo_rate
        if any(value is None for value in latest_fx_rates.values()):
            continue
        fx_rates = {pair: float(value) for pair, value in latest_fx_rates.items() if value is not None}
        if current_year != day.year:
            # Establish the annual baseline before this year's first maturity,
            # spend, fee or trade.  Resetting after the day's work omitted the
            # opening rebalance fees and January cash flow from annual metrics.
            current_year = day.year
            year_nav = 1.0
            year_peak_nav = 1.0
            year_max_drawdown = 0.0
            year_fee_start = state.total_fees_cny
            year_start_values = dict(last_close_values)
            year_start_repo_profit_cny = last_close_repo_profit_cny
            year_external_flows = {"REPO": 0.0}
            year_start_total_cny = daily_total_assets[-1] if daily_total_assets else initial_capital_cny
            year_external_flow_cny = 0.0
            year_start_date = day_str
        if latest_repo_rate is not None:
            repo_benchmark_nav *= 1.0 + (latest_repo_rate / 100.0) * repo_actual_days(day, 1, days) / 365.0

        dip_buy_blackout_today = bool(
            dip_buy_active
            and config.get("dip_buy_blackout_enabled", True)
            and is_dip_buy_blackout_month(
                day,
                int(config.get("annual_rebalance_month", 1)),
                int(config.get("dip_buy_blackout_months", 1)),
            )
        )

        rebalance_reset_today = False
        _mature_repo_lots(state, day)
        _apply_dividend_events(
            state,
            day_str,
            ex_events,
            pay_events,
            fx_rates,
            config["fees"],
            daily_dividends_by_symbol,
        )
        # Route availability is evaluated from prices known at this open.  A
        # real ETF requires today's actual opening quote; the non-tradable
        # index leg exposes only its prior published close.
        route_execution_prices = {
            symbol: open_price_maps.get(symbol, {}).get(day_str)
            for symbol in latest_prices
        }
        current_route_symbols = active_route_symbols(day, route_execution_prices, prepared_routes)
        switches_today: list[dict[str, Any]] = []
        if previous_route_symbols:
            switches_today = _execute_auto_route_switches(
                state,
                previous_route_symbols,
                current_route_symbols,
                day,
                route_execution_prices,
                fx_rates,
                config["fees"],
                trades,
                assets_by_symbol,
                bool(config.get("allow_fractional_us_shares", True)),
            )
            route_switch_events.extend(switches_today)
        for switch in switches_today:
            old_symbol = str(switch["from_symbol"])
            new_symbol = str(switch["to_symbol"])
            pending_dip_buys = [order for order in pending_dip_buys if str(order["symbol"]) != old_symbol]
            pending_dip_recovery_sells = [
                order for order in pending_dip_recovery_sells if str(order["symbol"]) != old_symbol
            ]
            dip_buy_triggered_levels.pop(old_symbol, None)
            dip_buy_initial_cost_prices.pop(old_symbol, None)
            dip_buy_initial_investment_cny.pop(old_symbol, None)
            dip_buy_cumulative_spend_cny.pop(old_symbol, None)
            dip_buy_recovery_lots.pop(old_symbol, None)
            deferred_dip_rechecks.pop(old_symbol, None)
            new_position = state.positions.get(new_symbol)
            if new_position and new_position.quantity > 0 and new_position.cost_basis_cny > 0:
                # The replacement ETF starts a fresh physical-security ladder.
                # Its own transaction-adjusted cost is the only valid baseline.
                dip_buy_initial_cost_prices[new_symbol] = new_position.cost_basis_cny / new_position.quantity
                dip_buy_initial_investment_cny[new_symbol] = new_position.cost_basis_cny
                dip_buy_cumulative_spend_cny[new_symbol] = 0.0
                dip_buy_triggered_levels[new_symbol] = set()
                dip_buy_recovery_lots[new_symbol] = []
        for logical_symbol, physical_symbol in current_route_symbols.items():
            previous_route_symbols[logical_symbol] = physical_symbol
        open_prices = {
            symbol: latest_open_prices.get(symbol) if latest_open_prices.get(symbol) is not None else latest_prices.get(symbol)
            for symbol in latest_prices
        }
        if pending_rebalance and pending_rebalance.get("execution_date") == day_str:
            before_open_total, before_open_values = _portfolio_value(state, open_prices, fx_rates, day)
            open_weights = {key: (value / before_open_total if before_open_total else 0.0) for key, value in before_open_values.items()}
            # The prior close only schedules the order.  Re-evaluate against
            # the actual execution open so an overnight gap cannot leave the
            # portfolio outside its bands without trading (or force a trade
            # after prices have already moved back inside).
            rebalance_needed = should_rebalance(
                open_weights,
                pending_rebalance["targets"],
                float(config["rebalance_band"]),
            )
            if rebalance_needed:
                before_rebalance, after_rebalance, turnover, fee_cny, desired_weights = _rebalance_state_to_band(
                    state,
                    sim_assets,
                    day,
                    open_prices,
                    fx_rates,
                    config["fees"],
                    trades,
                    bool(config.get("allow_fractional_us_shares", True)),
                    pending_rebalance["targets"],
                    float(config["rebalance_band"]),
                    rebalance_to_target,
                )
                _after_total, after_values = _portfolio_value(state, open_prices, fx_rates, day)
                rebalance_action = "trade"
                rebalance_reason = pending_rebalance.get("rebalance_reason", "scheduled_open")
            else:
                before_rebalance = before_open_total
                after_rebalance = before_open_total
                turnover = 0.0
                fee_cny = 0.0
                desired_weights = minimal_rebalance_weights(
                    open_weights,
                    pending_rebalance["targets"],
                    float(config["rebalance_band"]),
                    {asset["symbol"] for asset in sim_assets if asset.get("asset_type") == "money_fund"},
                )
                after_values = before_open_values
                rebalance_action = "record_only"
                rebalance_reason = "within_band"
            payload = {
                **pending_rebalance["payload"],
                "desired_weights": desired_weights,
                "rebalance_action": rebalance_action,
                "rebalance_reason": rebalance_reason,
                "rebalanced": rebalance_needed,
            }
            rebalance_rows.append(
                {
                    "run_id": run_id,
                    "rebalance_date": day_str,
                    "period_return": pending_rebalance["period_return"],
                    "total_asset_before": before_rebalance,
                    "total_asset_after": after_rebalance,
                    "turnover_cny": turnover,
                    "fee_cny": fee_cny,
                    "payload_json": json_dumps(payload),
                }
            )
            previous_rebalance_values = after_values
            previous_rebalance_repo_profit_cny = _repo_cumulative_profit_cny(state, day)
            initial_rebalance_done = True
            pending_rebalance = None
            last_rebalance_day = day
            if dip_buy_active:
                dip_buy_confirmed_cash_equivalent_cny = dip_buy_cash_equivalent_value_cny(
                    state,
                    day,
                    open_prices,
                    fx_rates,
                    money_fund["symbol"] if money_fund else None,
                )
                (
                    dip_buy_cash_buffer_locked_cny,
                    dip_buy_pool_cny,
                    dip_buy_piece_cny,
                    dip_buy_remaining_parts,
                ) = dip_buy_annual_budget(
                    dip_buy_confirmed_cash_equivalent_cny,
                    float(config["monthly_spend_cny"]),
                    int(config.get("dip_buy_total_parts", 10)),
                )
            else:
                dip_buy_confirmed_cash_equivalent_cny = 0.0
                dip_buy_cash_buffer_locked_cny = 0.0
                dip_buy_pool_cny = 0.0
                dip_buy_piece_cny = 0.0
                dip_buy_remaining_parts = 0
            (
                dip_buy_initial_cost_prices,
                dip_buy_initial_investment_cny,
            ) = dip_buy_cycle_baselines(state, config, day, open_prices, prepared_routes)
            dip_buy_triggered_levels = {symbol: set() for symbol in dip_buy_initial_cost_prices}
            dip_buy_cumulative_spend_cny = {symbol: 0.0 for symbol in dip_buy_initial_cost_prices}
            dip_buy_recovery_lots = {symbol: [] for symbol in dip_buy_initial_cost_prices}
            deferred_dip_rechecks.clear()
            rebalance_reset_today = True
        if rebalance_reset_today:
            # The scheduled rebalance at this same open supersedes any order
            # decided under the preceding cycle's cost basis and cash buffer.
            pending_dip_buys = []
            pending_dip_recovery_sells = []
        if dip_buy_blackout_today:
            # Orders decided in the preceding month must not cross into the
            # configured pre-rebalance quiet period. Deferred repo-maturity
            # rechecks are also discarded until the new annual cycle begins.
            pending_dip_buys = []
            deferred_dip_rechecks.clear()
        due_recovery_sells = [order for order in pending_dip_recovery_sells if order["execution_date"] == day_str]
        pending_dip_recovery_sells = [
            order for order in pending_dip_recovery_sells if order["execution_date"] != day_str
        ]
        if due_recovery_sells:
            recovery_open_prices = {
                symbol: open_price_maps.get(symbol, {}).get(day_str)
                for symbol in latest_prices
            }
            executed_recovery_sells = _execute_dip_recovery_sells(
                state,
                due_recovery_sells,
                day,
                recovery_open_prices,
                fx_rates,
                config["fees"],
                trades,
            )
            executed_by_lot = {
                str(order["lot_id"]): order
                for order in executed_recovery_sells
            }
            for order in due_recovery_sells:
                symbol = str(order["symbol"])
                lot_id = str(order["lot_id"])
                completed_lot: dict[str, Any] | None = None
                for lot in dip_buy_recovery_lots.get(symbol, []):
                    if str(lot["lot_id"]) != lot_id:
                        continue
                    executed_order = executed_by_lot.get(lot_id)
                    sold_quantity = float(executed_order.get("sold_quantity", 0.0)) if executed_order else 0.0
                    quantity_before = max(float(lot.get("quantity_remaining", 0.0)), 0.0)
                    spent_before = max(float(lot.get("spent_cny", 0.0)), 0.0)
                    sold_fraction = (
                        min(sold_quantity / quantity_before, 1.0)
                        if quantity_before > 1e-10
                        else 0.0
                    )
                    released_spend_cny = spent_before * sold_fraction
                    lot["quantity_remaining"] = max(quantity_before - sold_quantity, 0.0)
                    lot["spent_cny"] = max(spent_before - released_spend_cny, 0.0)
                    lot["pending"] = False
                    if released_spend_cny > 0:
                        dip_buy_cumulative_spend_cny[symbol] = max(
                            dip_buy_cumulative_spend_cny.get(symbol, 0.0) - released_spend_cny,
                            0.0,
                        )
                    if lot["quantity_remaining"] <= 1e-10 and sold_quantity > 0:
                        completed_lot = lot
                    break
                if completed_lot is not None:
                    # A fully recovered lot no longer consumes a ladder slot,
                    # pool part, or per-asset cap.  If the price later falls
                    # through the same level again, that level can buy again.
                    completed_levels = [
                        int(level)
                        for level in completed_lot.get("levels", [completed_lot["level"]])
                    ]
                    dip_buy_triggered_levels.setdefault(symbol, set()).difference_update(completed_levels)
                    dip_buy_remaining_parts = min(
                        dip_buy_remaining_parts + int(completed_lot.get("parts", 1)),
                        int(config.get("dip_buy_total_parts", 10)),
                    )
                dip_buy_recovery_lots[symbol] = [
                    lot for lot in dip_buy_recovery_lots.get(symbol, [])
                    if float(lot.get("quantity_remaining", 0.0)) > 1e-10
                ]
            dip_buy_recovery_sell_count += len(executed_recovery_sells)

        due_dip_buys = [order for order in pending_dip_buys if order["execution_date"] == day_str]
        pending_dip_buys = [order for order in pending_dip_buys if order["execution_date"] != day_str]
        if due_dip_buys:
            # A dip order is decided from the preceding close and must execute
            # only against this session's actual opening quote.  Do not forward
            # an older open or fall back to today's close: either shortcut would
            # turn a missing quote into a fabricated execution price.
            dip_execution_open_prices = {
                symbol: open_price_maps.get(symbol, {}).get(day_str)
                for symbol in latest_prices
            }
            money_fund_symbol = money_fund["symbol"] if money_fund else None
            execution_cash_equivalent_cny = dip_buy_cash_equivalent_value_cny(
                state,
                day,
                dip_execution_open_prices,
                fx_rates,
                money_fund_symbol,
            )
            execution_available_budget_cny = min(
                execution_cash_equivalent_cny,
                max(dip_buy_piece_cny * dip_buy_remaining_parts, 0.0),
            )
            executed_dip_buys = _execute_dip_buys(
                state,
                due_dip_buys,
                day,
                dip_execution_open_prices,
                fx_rates,
                config["fees"],
                trades,
                bool(config.get("allow_fractional_us_shares", True)),
                money_fund_symbol,
                execution_available_budget_cny,
            )
            for order in executed_dip_buys:
                symbol = str(order["symbol"])
                levels = [
                    int(level)
                    for level in order.get("levels", [order["level"]])
                ]
                level = max(levels)
                dip_buy_triggered_levels.setdefault(symbol, set()).update(levels)
                dip_buy_cumulative_spend_cny[symbol] = (
                    dip_buy_cumulative_spend_cny.get(symbol, 0.0) + float(order.get("spent_cny", 0.0))
                )
                bought_quantity = float(order.get("bought_quantity", 0.0))
                recovery_price = dip_buy_initial_cost_prices.get(symbol)
                if (
                    config.get("dip_buy_recovery_sell_enabled", False)
                    and bought_quantity > 0
                    and recovery_price is not None
                    and recovery_price > 0
                ):
                    dip_buy_recovery_lots.setdefault(symbol, []).append(
                        {
                            "lot_id": f"{day_str}:{symbol}:{'-'.join(str(item) for item in levels)}",
                            "symbol": symbol,
                            "level": level,
                            "levels": levels,
                            "parts": int(order.get("parts", 1)),
                            "buy_date": day_str,
                            "quantity_remaining": bought_quantity,
                            "spent_cny": float(order.get("spent_cny", 0.0)),
                            "recovery_price": float(recovery_price),
                            "pending": False,
                        }
                    )
            dip_buy_remaining_parts = max(
                dip_buy_remaining_parts - sum(int(order.get("parts", 1)) for order in executed_dip_buys),
                0,
            )
            dip_buy_execution_count += len(executed_dip_buys)
        fee_days = max((day - previous_fee_day).days, 1) if previous_fee_day else 1
        _apply_hk_connect_portfolio_fee(
            state,
            latest_prices,
            fx_rates,
            config["fees"],
            fee_days,
            daily_holding_fees_by_symbol,
        )
        previous_fee_day = day

        if day in monthly_spend_days:
            spend = float(config["monthly_spend_cny"])
            if state.cash_cny < spend:
                _cover_cash_shortfall(state, spend, day, latest_prices, fx_rates, config["fees"], trades)
            actual_spend = min(spend, state.cash_cny)
            state.cash_cny -= actual_spend
            state.total_spend_cny += actual_spend
            flow -= actual_spend
            period_external_flows["REPO"] = period_external_flows.get("REPO", 0.0) - actual_spend
            year_external_flows["REPO"] = year_external_flows.get("REPO", 0.0) - actual_spend
            year_external_flow_cny -= actual_spend

        before_total, before_values = _portfolio_value(state, latest_prices, fx_rates, day)
        targets = effective_weights(
            config,
            day,
            latest_prices,
            before_total,
            prepared_routes=prepared_routes,
            money_fund_asset=money_fund,
        )
        treasury_target = money_fund["symbol"] if money_fund and targets.get(money_fund["symbol"], 0.0) > 0 else "REPO"
        treasury_became_available = bool(money_fund and previous_treasury_target == "REPO" and treasury_target == money_fund["symbol"])
        current_weights = {key: (value / before_total if before_total else 0.0) for key, value in before_values.items()}
        is_rebalance_day = not initial_rebalance_done or treasury_became_available
        should_record_rebalance = is_rebalance_day and has_investable_asset_target(targets)
        if should_record_rebalance:
            starts_dip_buy_cycle = not initial_rebalance_done
            rebalance_band = float(config["rebalance_band"])
            event_nav = nav_for_period
            event_year_nav = year_nav
            if daily_total_assets and daily_total_assets[-1] != 0:
                event_return = (before_total - daily_total_assets[-1] - flow) / daily_total_assets[-1]
                event_nav *= 1.0 + event_return
                event_year_nav *= 1.0 + event_return
            event_peak_nav = max(period_peak_nav, event_nav)
            event_drawdown = event_nav / event_peak_nav - 1.0 if event_peak_nav else 0.0
            event_max_drawdown = min(period_max_drawdown, event_drawdown)
            event_year_peak_nav = max(year_peak_nav, event_year_nav)
            event_year_drawdown = event_year_nav / event_year_peak_nav - 1.0 if event_year_peak_nav else 0.0
            event_year_max_drawdown = min(year_max_drawdown, event_year_drawdown)
            current_repo_profit_cny = _repo_cumulative_profit_cny(state, day)
            asset_performance = _asset_period_performance(
                previous_rebalance_values,
                before_values,
                performance_symbols,
                period_external_flows,
                {"REPO": current_repo_profit_cny - previous_rebalance_repo_profit_cny},
            )
            year_asset_performance = _asset_period_performance(
                year_start_values,
                before_values,
                performance_symbols,
                year_external_flows,
                {"REPO": current_repo_profit_cny - year_start_repo_profit_cny},
            )
            rebalance_needed = should_rebalance(current_weights, targets, rebalance_band)
            if rebalance_needed:
                before_rebalance, after_rebalance, turnover, fee_cny, desired_weights = _rebalance_state_to_band(
                    state,
                    sim_assets,
                    day,
                    latest_prices,
                    fx_rates,
                    config["fees"],
                    trades,
                    bool(config.get("allow_fractional_us_shares", True)),
                    targets,
                    rebalance_band,
                    not initial_rebalance_done or rebalance_to_target,
                )
                _after_total, after_values = _portfolio_value(state, latest_prices, fx_rates, day)
                rebalance_action = "trade"
                rebalance_reason = "treasury_available" if treasury_became_available else "threshold_exceeded"
            else:
                before_rebalance = before_total
                after_rebalance = before_total
                turnover = 0.0
                fee_cny = 0.0
                desired_weights = minimal_rebalance_weights(
                    current_weights,
                    targets,
                    rebalance_band,
                    {asset["symbol"] for asset in sim_assets if asset.get("asset_type") == "money_fund"},
                )
                after_values = before_values
                rebalance_action = "record_only"
                rebalance_reason = "within_band"
            previous_total = daily_total_assets[-1] if daily_total_assets else float(config["initial_capital_cny"])
            period_return = event_nav / period_start_nav - 1.0
            year_profit_cny = before_rebalance - year_start_total_cny - year_external_flow_cny
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
                            "asset_performance_version": 2,
                            "targets": targets,
                            "desired_weights": desired_weights,
                            "repo_target_mode": config.get("repo_target_mode", "residual_weight"),
                            "repo_target_value_cny": before_rebalance * desired_weights.get(treasury_target, 0.0),
                            "treasury_instrument": config.get("repo_symbol", "204001"),
                            "asset_performance": asset_performance,
                            "period_max_drawdown": event_max_drawdown,
                            "previous_total": previous_total,
                            "year_label": day.year,
                            "year_start_date": year_start_date,
                            "year_return": event_year_nav - 1.0,
                            "year_max_drawdown": event_year_max_drawdown,
                            "year_fee_cny": state.total_fees_cny - year_fee_start,
                            "year_asset_performance": year_asset_performance,
                            "year_start_total_cny": year_start_total_cny,
                            "year_external_flow_cny": year_external_flow_cny,
                            "year_profit_cny": year_profit_cny,
                            "year_profit_on_year_start": (
                                year_profit_cny / year_start_total_cny if year_start_total_cny else None
                            ),
                            "year_profit_on_original_capital": (
                                year_profit_cny / initial_capital_cny if initial_capital_cny else None
                            ),
                            "year_return_basis": "cash_flow_adjusted_daily_compound",
                            "year_profit_basis": "asset_change_excluding_external_flows",
                            "rebalance_action": rebalance_action,
                            "rebalance_reason": rebalance_reason,
                            "rebalanced": rebalance_needed,
                        }
                    ),
                }
            )
            previous_rebalance_values = after_values
            previous_rebalance_repo_profit_cny = _repo_cumulative_profit_cny(state, day)
            period_external_flows = {"REPO": 0.0}
            period_start_nav = event_nav
            period_peak_nav = event_nav
            period_max_drawdown = 0.0
            initial_rebalance_done = True
            if starts_dip_buy_cycle:
                # The first allocation is the actual per-asset annual baseline.
                # Starting REPO at the entire initial capital makes purchases of
                # the other sleeves look like a cash loss.
                year_start_values = dict(after_values)
                year_start_repo_profit_cny = previous_rebalance_repo_profit_cny
                year_external_flows = {"REPO": 0.0}
                last_rebalance_day = day
                if dip_buy_active:
                    dip_buy_confirmed_cash_equivalent_cny = dip_buy_cash_equivalent_value_cny(
                        state,
                        day,
                        latest_prices,
                        fx_rates,
                        money_fund["symbol"] if money_fund else None,
                    )
                    (
                        dip_buy_cash_buffer_locked_cny,
                        dip_buy_pool_cny,
                        dip_buy_piece_cny,
                        dip_buy_remaining_parts,
                    ) = dip_buy_annual_budget(
                        dip_buy_confirmed_cash_equivalent_cny,
                        float(config["monthly_spend_cny"]),
                        int(config.get("dip_buy_total_parts", 10)),
                    )
                else:
                    dip_buy_confirmed_cash_equivalent_cny = 0.0
                    dip_buy_cash_buffer_locked_cny = 0.0
                    dip_buy_pool_cny = 0.0
                    dip_buy_piece_cny = 0.0
                    dip_buy_remaining_parts = 0
                (
                    dip_buy_initial_cost_prices,
                    dip_buy_initial_investment_cny,
                ) = dip_buy_cycle_baselines(state, config, day, latest_prices, prepared_routes)
                dip_buy_triggered_levels = {symbol: set() for symbol in dip_buy_initial_cost_prices}
                dip_buy_cumulative_spend_cny = {symbol: 0.0 for symbol in dip_buy_initial_cost_prices}
                dip_buy_recovery_lots = {symbol: [] for symbol in dip_buy_initial_cost_prices}
                pending_dip_buys = []
                pending_dip_recovery_sells = []
                deferred_dip_rechecks.clear()
        elif is_rebalance_day and not initial_rebalance_done and has_deferred_inception_target(config, day):
            initial_rebalance_done = True
        previous_treasury_target = treasury_target

        next_day = days[idx + 1] if idx + 1 < len(days) else None
        dip_buy_blackout_next_day = bool(
            next_day
            and dip_buy_active
            and config.get("dip_buy_blackout_enabled", True)
            and is_dip_buy_blackout_month(
                next_day,
                int(config.get("annual_rebalance_month", 1)),
                int(config.get("dip_buy_blackout_months", 1)),
            )
        )
        dip_buy_reserve_cny = 0.0
        if (
            dip_buy_active
            and config.get("dip_buy_recovery_sell_enabled", False)
            and next_day is not None
            and last_rebalance_day is not None
        ):
            pending_recovery_lot_ids = {
                str(order["lot_id"])
                for order in pending_dip_recovery_sells
            }
            for symbol, lots in dip_buy_recovery_lots.items():
                close = latest_prices.get(symbol)
                position = state.positions.get(symbol)
                if close is None or close <= 0 or not position or position.quantity <= 0:
                    continue
                # Other portfolio operations can reduce the position before a
                # recovery sale.  Never allow tracked dip quantities to exceed
                # the shares that still exist.
                quantity_available = position.quantity
                for lot in lots:
                    lot_quantity = min(max(float(lot.get("quantity_remaining", 0.0)), 0.0), quantity_available)
                    lot["quantity_remaining"] = lot_quantity
                    quantity_available = max(quantity_available - lot_quantity, 0.0)
                dip_buy_recovery_lots[symbol] = [
                    lot for lot in lots if float(lot.get("quantity_remaining", 0.0)) > 1e-10
                ]
                for lot in dip_buy_recovery_lots[symbol]:
                    lot_id = str(lot["lot_id"])
                    recovery_price = float(lot.get("recovery_price", 0.0))
                    if (
                        recovery_price <= 0
                        or float(close) + 1e-10 < recovery_price
                        or bool(lot.get("pending"))
                        or lot_id in pending_recovery_lot_ids
                    ):
                        continue
                    pending_dip_recovery_sells.append(
                        {
                            "lot_id": lot_id,
                            "symbol": symbol,
                            "level": int(lot["level"]),
                            "levels": [
                                int(level)
                                for level in lot.get("levels", [lot["level"]])
                            ],
                            "decision_date": day_str,
                            "execution_date": next_day.isoformat(),
                            "quantity": float(lot["quantity_remaining"]),
                            "trigger_close_price": float(close),
                            "recovery_price": recovery_price,
                        }
                    )
                    lot["pending"] = True
                    pending_recovery_lot_ids.add(lot_id)

        if (
            dip_buy_active
            and next_day is not None
            and last_rebalance_day is not None
            and not dip_buy_blackout_today
            and not dip_buy_blackout_next_day
        ):
            drawdown_trigger = float(config.get("dip_buy_drawdown", 0.05))
            total_parts = int(config.get("dip_buy_total_parts", 10))
            level_mode = str(config.get("dip_buy_level_mode", "fixed"))
            cost_basis_mode = str(config.get("dip_buy_cost_basis_mode", "current_average"))
            asset_cap_enabled = bool(config.get("dip_buy_asset_cap_enabled", False))
            asset_cap_ratio = float(config.get("dip_buy_asset_cap_ratio", 0.50))
            money_fund_symbol = money_fund["symbol"] if money_fund else None
            cash_equivalent_cny = dip_buy_cash_equivalent_value_cny(
                state,
                day,
                latest_prices,
                fx_rates,
                money_fund_symbol,
            )
            cash_buffer_cny = dip_buy_cash_buffer_locked_cny
            pending_parts = sum(int(order.get("parts", 1)) for order in pending_dip_buys)
            available_parts = max(dip_buy_remaining_parts - pending_parts, 0)
            pending_budget_cny = sum(float(order.get("budget_cny", 0.0)) for order in pending_dip_buys)
            remaining_budget_cny = max(dip_buy_piece_cny * dip_buy_remaining_parts, 0.0)
            available_budget_cny = min(
                max(remaining_budget_cny - pending_budget_cny, 0.0),
                cash_equivalent_cny,
            )
            liquid_cash_equivalent_cny = state.cash_cny
            if money_fund_symbol:
                money_position = state.positions.get(money_fund_symbol)
                money_price = latest_prices.get(money_fund_symbol)
                if money_position and money_price is not None:
                    liquid_cash_equivalent_cny += position_value_cny(money_position, float(money_price), fx_rates)
            for asset in dip_buy_assets(config, day, latest_prices, prepared_routes):
                symbol = str(asset["symbol"])
                close = latest_prices.get(symbol)
                position = state.positions.get(symbol)
                if close is None or close <= 0 or not position or position.quantity <= 0 or position.cost_basis_cny <= 0:
                    deferred_dip_rechecks.pop(symbol, None)
                    continue
                deferred_until = deferred_dip_rechecks.get(symbol)
                if deferred_until and day < deferred_until:
                    continue
                if deferred_until:
                    # A multi-day repo lot matured this morning. Re-evaluate at
                    # today's close and, if still eligible, trade next open.
                    deferred_dip_rechecks.pop(symbol, None)
                average_cost_price = position.cost_basis_cny / position.quantity
                initial_cost_price = dip_buy_initial_cost_prices.get(symbol)
                if initial_cost_price is None or initial_cost_price <= 0:
                    initial_cost_price = average_cost_price
                    dip_buy_initial_cost_prices[symbol] = average_cost_price
                    dip_buy_initial_investment_cny[symbol] = position.cost_basis_cny
                    dip_buy_cumulative_spend_cny.setdefault(symbol, 0.0)
                    dip_buy_triggered_levels.setdefault(symbol, set())
                    dip_buy_recovery_lots.setdefault(symbol, [])
                cost_reference_price = (
                    initial_cost_price
                    if cost_basis_mode == "initial"
                    else average_cost_price
                )
                drawdown_from_cost = float(close) / cost_reference_price - 1.0
                reached_level = min(
                    max(int(math.floor((-drawdown_from_cost + 1e-12) / drawdown_trigger)), 0),
                    total_parts,
                )
                if reached_level <= 0:
                    deferred_dip_rechecks.pop(symbol, None)
                    continue
                if available_parts <= 0 or available_budget_cny <= 0:
                    continue
                pending_levels = {
                    int(level)
                    for order in pending_dip_buys
                    if str(order["symbol"]) == symbol
                    for level in order.get("levels", [order["level"]])
                }
                used_levels = dip_buy_triggered_levels.setdefault(symbol, set()) | pending_levels
                pending_symbol_budget_cny = sum(
                    float(order.get("budget_cny", 0.0))
                    for order in pending_dip_buys
                    if str(order["symbol"]) == symbol
                )
                asset_cap_cny = (
                    max(dip_buy_initial_investment_cny.get(symbol, 0.0), 0.0) * asset_cap_ratio
                    if asset_cap_enabled
                    else math.inf
                )
                asset_cap_remaining_cny = max(
                    asset_cap_cny
                    - dip_buy_cumulative_spend_cny.get(symbol, 0.0)
                    - pending_symbol_budget_cny,
                    0.0,
                )
                new_level_orders: list[dict[str, Any]] = []
                for level in range(1, reached_level + 1):
                    if level in used_levels or available_parts <= 0 or available_budget_cny <= 0:
                        continue
                    requested_parts = dip_buy_parts_for_level(level, level_mode)
                    order_parts = min(requested_parts, available_parts)
                    budget_cny = min(
                        dip_buy_piece_cny * order_parts,
                        available_budget_cny,
                        asset_cap_remaining_cny,
                    )
                    if budget_cny <= 0:
                        break
                    if money_fund_symbol is None and tenor_days > 1 and liquid_cash_equivalent_cny + 1e-9 < budget_cny:
                        future_maturities = [lot.maturity_date for lot in state.repo_lots if lot.maturity_date > day]
                        if future_maturities:
                            deferred_dip_rechecks[symbol] = min(future_maturities)
                        break
                    new_level_orders.append(
                        {
                            "symbol": symbol,
                            "level": level,
                            "decision_date": day_str,
                            "execution_date": next_day.isoformat(),
                            "average_cost_price": average_cost_price,
                            "initial_cost_price": initial_cost_price,
                            "cost_reference_price": cost_reference_price,
                            "cost_basis_mode": cost_basis_mode,
                            "level_mode": level_mode,
                            "trigger_close_price": float(close),
                            "drawdown": drawdown_from_cost,
                            "cash_equivalent_cny": cash_equivalent_cny,
                            "cash_buffer_cny": cash_buffer_cny,
                            "confirmed_cash_equivalent_cny": dip_buy_confirmed_cash_equivalent_cny,
                            "excess_cash_cny": remaining_budget_cny,
                            "pool_cny": dip_buy_pool_cny,
                            "piece_cny": dip_buy_piece_cny,
                            "requested_parts": requested_parts,
                            "parts": order_parts,
                            "budget_cny": budget_cny,
                            "asset_cap_cny": None if math.isinf(asset_cap_cny) else asset_cap_cny,
                        }
                    )
                    used_levels.add(level)
                    available_parts -= order_parts
                    available_budget_cny -= budget_cny
                    asset_cap_remaining_cny = max(asset_cap_remaining_cny - budget_cny, 0.0)
                    liquid_cash_equivalent_cny = max(liquid_cash_equivalent_cny - budget_cny, 0.0)
                    dip_buy_reserve_cny += budget_cny
                if new_level_orders:
                    # A close can cross several previously unused levels at
                    # once.  Execute those levels as one market order for this
                    # asset instead of emitting duplicate-looking trades at
                    # the same next-session open.
                    combined_order = dict(new_level_orders[0])
                    combined_order["level_orders"] = [
                        {
                            "level": int(order["level"]),
                            "requested_parts": int(order.get("requested_parts", 1)),
                            "parts": int(order.get("parts", 1)),
                            "budget_cny": float(order.get("budget_cny", 0.0)),
                        }
                        for order in new_level_orders
                    ]
                    combined_order["levels"] = [int(order["level"]) for order in new_level_orders]
                    combined_order["level"] = max(combined_order["levels"])
                    combined_order["requested_parts"] = sum(
                        int(order.get("requested_parts", 1)) for order in new_level_orders
                    )
                    combined_order["parts"] = sum(int(order.get("parts", 1)) for order in new_level_orders)
                    combined_order["budget_cny"] = sum(
                        float(order.get("budget_cny", 0.0)) for order in new_level_orders
                    )
                    pending_dip_buys.append(combined_order)

        _invest_repo_cash(
            state,
            day,
            latest_repo_rate,
            latest_one_day_repo_rate,
            config["fees"],
            tenor_days,
            monthly_spend_days,
            float(config["monthly_spend_cny"]),
            reb_days,
            dip_buy_reserve_cny,
            repo_fee_config,
            monthly_spend_ordinals,
            days,
            one_day_repo_fee_config,
        )

        total, values = _portfolio_value(state, latest_prices, fx_rates, day)
        daily_total_assets.append(total)
        daily_flows.append(flow)
        benchmark_values.append(latest_prices.get(benchmark_symbol))
        repo_benchmark_values.append(repo_benchmark_nav)

        period_previous_total = (
            daily_total_assets[-2]
            if len(daily_total_assets) > 1
            else float(config["initial_capital_cny"])
        )
        if period_previous_total != 0:
            ret_for_period = (daily_total_assets[-1] - period_previous_total - flow) / period_previous_total
            nav_for_period *= 1.0 + ret_for_period
            period_peak_nav = max(period_peak_nav, nav_for_period)
            period_drawdown = nav_for_period / period_peak_nav - 1.0 if period_peak_nav else 0.0
            period_max_drawdown = min(period_max_drawdown, period_drawdown)
            year_nav *= 1.0 + ret_for_period
            year_peak_nav = max(year_peak_nav, year_nav)
            year_drawdown = year_nav / year_peak_nav - 1.0 if year_peak_nav else 0.0
            year_max_drawdown = min(year_max_drawdown, year_drawdown)

        should_schedule_next_open = (
            next_day is not None
            and next_day in reb_days
            and initial_rebalance_done
            and pending_rebalance is None
            and has_investable_asset_target(targets)
        )
        if should_schedule_next_open:
            close_weights = {key: (value / total if total else 0.0) for key, value in values.items()}
            rebalance_band = float(config["rebalance_band"])
            scheduled_prices = scheduled_target_price_view(latest_prices, next_day, prepared_routes)
            scheduled_targets = effective_weights(
                config,
                next_day,
                scheduled_prices,
                total,
                prepared_routes=prepared_routes,
                money_fund_asset=money_fund,
            )
            desired_weights = (
                exact_target_weights(scheduled_targets)
                if rebalance_to_target
                else minimal_rebalance_weights(
                    close_weights,
                    scheduled_targets,
                    rebalance_band,
                    {asset["symbol"] for asset in sim_assets if asset.get("asset_type") == "money_fund"},
                )
            )
            current_repo_profit_cny = _repo_cumulative_profit_cny(state, day)
            year_asset_performance = _asset_period_performance(
                year_start_values,
                values,
                performance_symbols,
                year_external_flows,
                {"REPO": current_repo_profit_cny - year_start_repo_profit_cny},
            )
            period_asset_performance = _asset_period_performance(
                previous_rebalance_values,
                values,
                performance_symbols,
                period_external_flows,
                {"REPO": current_repo_profit_cny - previous_rebalance_repo_profit_cny},
            )
            pending_rebalance = {
                "execution_date": next_day.isoformat(),
                "decision_date": day_str,
                "targets": scheduled_targets,
                "rebalance_needed": should_rebalance(close_weights, scheduled_targets, rebalance_band),
                "rebalance_reason": "scheduled_open",
                "period_return": nav_for_period / period_start_nav - 1.0,
                "payload": {
                    "asset_performance_version": 2,
                    "decision_date": day_str,
                    "targets": scheduled_targets,
                    "desired_weights": desired_weights,
                    "rebalance_to_target": rebalance_to_target,
                    "repo_target_mode": config.get("repo_target_mode", "residual_weight"),
                    "treasury_instrument": config.get("repo_symbol", "204001"),
                    "asset_performance": period_asset_performance,
                    "period_max_drawdown": period_max_drawdown,
                    "year_return": year_nav - 1.0,
                    "year_max_drawdown": year_max_drawdown,
                    "year_fee_cny": state.total_fees_cny - year_fee_start,
                    "year_asset_performance": year_asset_performance,
                    "year_label": day.year,
                    "year_start_date": year_start_date,
                    "year_start_total_cny": year_start_total_cny,
                    "year_external_flow_cny": year_external_flow_cny,
                    "year_profit_cny": total - year_start_total_cny - year_external_flow_cny,
                    "year_profit_on_year_start": (
                        (total - year_start_total_cny - year_external_flow_cny) / year_start_total_cny
                        if year_start_total_cny
                        else None
                    ),
                    "year_profit_on_original_capital": (
                        (total - year_start_total_cny - year_external_flow_cny) / initial_capital_cny
                        if initial_capital_cny
                        else None
                    ),
                    "year_return_basis": "cash_flow_adjusted_daily_compound",
                    "year_profit_basis": "asset_change_excluding_external_flows",
                    "decision_total_asset_cny": total,
                    "previous_total": period_previous_total,
                },
            }
            period_external_flows = {"REPO": 0.0}
            period_start_nav = nav_for_period
            period_peak_nav = nav_for_period
            period_max_drawdown = 0.0

        current_close_repo_profit_cny = _repo_cumulative_profit_cny(state, day)
        daily_asset_profit_cny = (
            _daily_asset_profit_cny(
                last_close_values,
                values,
                trades[daily_trade_start_index:],
                daily_dividends_by_symbol,
                daily_holding_fees_by_symbol,
                current_close_repo_profit_cny - last_close_repo_profit_cny,
            )
            if persist
            else {}
        )
        if persist:
            payload_money_fund_symbol = money_fund["symbol"] if money_fund else None
            payload_cash_equivalent_cny = dip_buy_cash_equivalent_value_cny(
                state,
                day,
                latest_prices,
                fx_rates,
                payload_money_fund_symbol,
            )
            payload_cash_buffer_cny = dip_buy_cash_buffer_locked_cny
            payload_remaining_budget_cny = max(dip_buy_piece_cny * dip_buy_remaining_parts, 0.0)
            daily_payloads.append(
                {
                    "cash_cny": state.cash_cny,
                    "dividend_receivable_cny": state.dividend_receivable_cny,
                    "treasury_instrument": config.get("repo_symbol", "204001"),
                    "treasury_fallback_active": bool(money_fund and targets.get("REPO", 0.0) > 0),
                    "values": values,
                    "weights": {key: value / total if total else 0.0 for key, value in values.items()},
                    "targets": targets,
                    "asset_daily_profit_cny": daily_asset_profit_cny,
                    "unrealized_pnl_cny": {
                        symbol: values.get(symbol, 0.0) - pos.cost_basis_cny
                        for symbol, pos in state.positions.items()
                    },
                    "benchmark_value": latest_prices.get(benchmark_symbol),
                    "repo_lots": len(state.repo_lots),
                    "dip_buy": {
                        "enabled": bool(config.get("dip_buy_enabled")),
                        "active": dip_buy_active,
                        "blackout": dip_buy_blackout_today,
                        "blackout_enabled": bool(config.get("dip_buy_blackout_enabled", True)),
                        "blackout_months": int(config.get("dip_buy_blackout_months", 1)),
                        "pending_count": len(pending_dip_buys),
                        "pending_recovery_sell_count": len(pending_dip_recovery_sells),
                        "deferred_count": len(deferred_dip_rechecks),
                        "deferred_recheck_dates": {symbol: recheck.isoformat() for symbol, recheck in deferred_dip_rechecks.items()},
                        "execution_count": dip_buy_execution_count,
                        "recovery_sell_count": dip_buy_recovery_sell_count,
                        "cash_equivalent_cny": payload_cash_equivalent_cny,
                        "confirmed_cash_equivalent_cny": dip_buy_confirmed_cash_equivalent_cny,
                        "cash_buffer_cny": payload_cash_buffer_cny,
                        "excess_cash_cny": payload_remaining_budget_cny,
                        "remaining_budget_cny": payload_remaining_budget_cny,
                        "pool_cny": dip_buy_pool_cny,
                        "piece_cny": dip_buy_piece_cny,
                        "remaining_parts": dip_buy_remaining_parts,
                        "total_parts": int(config.get("dip_buy_total_parts", 10)),
                        "level_mode": str(config.get("dip_buy_level_mode", "fixed")),
                        "cost_basis_mode": str(config.get("dip_buy_cost_basis_mode", "current_average")),
                        "recovery_sell_enabled": bool(config.get("dip_buy_recovery_sell_enabled", False)),
                        "asset_cap_enabled": bool(config.get("dip_buy_asset_cap_enabled", False)),
                        "asset_cap_ratio": float(config.get("dip_buy_asset_cap_ratio", 0.50)),
                        "triggered_levels": {
                            symbol: sorted(levels)
                            for symbol, levels in dip_buy_triggered_levels.items()
                        },
                        "initial_cost_prices": dict(dip_buy_initial_cost_prices),
                        "initial_investment_cny": dict(dip_buy_initial_investment_cny),
                        "cumulative_spend_cny": dict(dip_buy_cumulative_spend_cny),
                        "drawdown": float(config.get("dip_buy_drawdown", 0.05)),
                        "last_rebalance_date": last_rebalance_day.isoformat() if last_rebalance_day else None,
                    },
                }
            )
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
                "payload_json": "",
            }
        )
        last_close_values = dict(values)
        last_close_repo_profit_cny = current_close_repo_profit_cny

    if not daily_rows:
        raise BacktestError("no daily rows created; check data coverage")
    logger.info("run_backtest main loop complete rows=%d trades=%d rebalance=%d seconds=%.3f", len(daily_rows), len(trades), len(rebalance_rows), time.perf_counter() - loop_started_at)

    raise_if_cancelled(should_cancel)
    comparison_started_at = time.perf_counter()
    comparison_totals = {}
    if include_comparison and persist:
        comparison_totals = _simulate_comparison_series(
            config,
            days,
            price_maps,
            open_price_maps,
            fx_maps,
            repo_map,
            one_day_repo_map,
            ex_events,
            pay_events,
            set(monthly_spend_days),
            set(reb_days),
            should_cancel,
        )
    for row, payload in zip(daily_rows, daily_payloads):
        if include_comparison and persist:
            comparison_total = comparison_totals.get(row["trade_date"])
            payload["comparison"] = {
                "name": "沪深300基金加黄金基金加国债逆回购",
                "total_asset_cny": comparison_total,
            }
        row["payload_json"] = json_dumps(payload)
    logger.info("run_backtest comparison complete rows=%d seconds=%.3f", len(comparison_totals), time.perf_counter() - comparison_started_at)

    raise_if_cancelled(should_cancel)
    metrics_started_at = time.perf_counter()
    daily_returns, cumulative_returns, drawdowns = compute_metrics(
        daily_total_assets,
        daily_flows,
        benchmark_values,
        initial_value=float(config["initial_capital_cny"]),
    )
    bench_returns = benchmark_returns(benchmark_values)
    total_return = cumulative_returns[-1]
    years = max((parse_date(daily_rows[-1]["trade_date"]) - parse_date(daily_rows[0]["trade_date"])).days / 365.25, 1 / 365.25)
    annualized = (1.0 + total_return) ** (1.0 / years) - 1.0
    net_external_flow_cny = sum(daily_flows)
    net_profit_cny = daily_total_assets[-1] - initial_capital_cny - net_external_flow_cny
    original_capital_return = net_profit_cny / initial_capital_cny if initial_capital_cny else None
    original_capital_growth = 1.0 + original_capital_return if original_capital_return is not None else None
    original_capital_annualized_return = (
        original_capital_growth ** (1.0 / years) - 1.0
        if original_capital_growth is not None and original_capital_growth > 0
        else None
    )
    # repo_benchmark_nav starts at 1.0 and is accrued before each daily row is
    # appended. Dividing by the first already-accrued row dropped day one.
    repo_total_return = repo_benchmark_values[-1] - 1.0
    repo_annualized_return = (1.0 + repo_total_return) ** (1.0 / years) - 1.0
    trade_dates = [row["trade_date"] for row in daily_rows]
    positive_year_count, complete_year_count = yearly_return_counts(trade_dates, daily_returns)
    max_drawdown = min(drawdowns)
    ranking = ranking_metrics(
        annualized,
        repo_annualized_return,
        max_drawdown,
        positive_year_count,
        complete_year_count,
    )
    returns_np = np.array(daily_returns or [0.0], dtype=float)
    volatility = float(np.std(returns_np) * math.sqrt(252))
    calendar_risk = worst_calendar_periods(trade_dates, daily_returns)
    drawdown_recovery = drawdown_recovery_metrics(trade_dates, cumulative_returns)
    market_capture = market_capture_metrics(trade_dates, daily_returns, benchmark_values)
    instrument_coverage = instrument_coverage_summaries(config, days, price_maps)
    for idx, row in enumerate(daily_rows):
        row["daily_return"] = daily_returns[idx]
        row["cumulative_return"] = cumulative_returns[idx]
        row["drawdown"] = drawdowns[idx]
        row["benchmark_return"] = bench_returns[idx]

    final_payload = daily_payloads[-1] if daily_payloads else {}
    final_unrealized_pnl_cny = (
        sum(final_payload.get("unrealized_pnl_cny", {}).values())
        if final_payload
        else sum(
            last_close_values.get(symbol, 0.0) - position.cost_basis_cny
            for symbol, position in state.positions.items()
        )
    )
    summary = {
        "run_id": run_id,
        "start_date": daily_rows[0]["trade_date"],
        "end_date": daily_rows[-1]["trade_date"],
        "final_asset_cny": daily_total_assets[-1],
        "initial_capital_cny": initial_capital_cny,
        "total_return": total_return,
        "annualized_return": annualized,
        "annualized_return_basis": "cash_flow_adjusted_daily_compound",
        "net_external_flow_cny": net_external_flow_cny,
        "net_profit_cny": net_profit_cny,
        "original_capital_return": original_capital_return,
        "original_capital_annualized_return": original_capital_annualized_return,
        "original_capital_return_basis": "final_asset_minus_net_external_flow_over_initial_capital",
        "max_drawdown": max_drawdown,
        **calendar_risk,
        "drawdown_recovery": drawdown_recovery,
        **market_capture,
        "positive_year_count": positive_year_count,
        "complete_year_count": complete_year_count,
        **ranking,
        "volatility": volatility,
        "total_fees_cny": state.total_fees_cny,
        "total_spend_cny": state.total_spend_cny,
        "withheld_tax_cny": state.total_withheld_tax_cny,
        "total_dividend_cny": state.total_dividend_cny,
        "trade_count": len(trades),
        "route_switch_count": len(route_switch_events),
        "route_switches": route_switch_events,
        "instrument_coverage": instrument_coverage,
        "dip_buy_count": dip_buy_execution_count,
        "dip_buy_recovery_sell_count": dip_buy_recovery_sell_count,
        "rebalance_count": len(rebalance_rows),
        "final_unrealized_pnl_cny": final_unrealized_pnl_cny,
        "comparison_final_asset_cny": final_payload.get("comparison", {}).get("total_asset_cny"),
        "rolling_window_years": int(config["rolling_window_years"]),
        "rolling_periods": [],
        "annual_rebalance_month": int(config["annual_rebalance_month"]),
        "rebalance_month_scenarios": [],
    }

    if include_rolling_analysis:
        rolling_rows = []
        for window in rolling_window_ranges(start, end, int(config["rolling_window_years"])):
            raise_if_cancelled(should_cancel)
            scenario_config = dict(config)
            scenario_config["start_date"] = window["start_date"]
            scenario_config["end_date"] = window["end_date"]
            scenario_config["rebalance_month_analysis_enabled"] = False
            scenario_summary = run_backtest(
                conn,
                scenario_config,
                should_cancel=should_cancel,
                persist=False,
                include_comparison=False,
                include_month_analysis=False,
                include_rolling_analysis=False,
            )["summary"]
            rolling_rows.append(
                {
                    **window,
                    "actual_start_date": scenario_summary["start_date"],
                    "actual_end_date": scenario_summary["end_date"],
                    "total_return": scenario_summary["total_return"],
                    "annualized_return": scenario_summary["annualized_return"],
                    "max_drawdown": scenario_summary["max_drawdown"],
                    "annual_return_drawdown_ratio": scenario_summary["annual_return_drawdown_ratio"],
                }
            )
        summary["rolling_periods"] = rolling_rows

    if (
        include_month_analysis
        and config["rebalance_frequency"] == "yearly"
        and config.get("rebalance_month_analysis_enabled", False)
    ):
        month_rows = []
        selected_month = int(config["annual_rebalance_month"])
        for month in range(1, 13):
            raise_if_cancelled(should_cancel)
            if month == selected_month:
                scenario_summary = summary
            else:
                scenario_config = dict(config)
                scenario_config["annual_rebalance_month"] = month
                scenario_config["rebalance_month_analysis_enabled"] = False
                scenario_summary = run_backtest(
                    conn,
                    scenario_config,
                    should_cancel=should_cancel,
                    persist=False,
                    include_comparison=False,
                    include_month_analysis=False,
                    include_rolling_analysis=False,
                )["summary"]
            month_rows.append(
                {
                    "month": month,
                    "month_name": f"{month}月",
                    "selected": month == selected_month,
                    "final_asset_cny": scenario_summary["final_asset_cny"],
                    "total_return": scenario_summary["total_return"],
                    "annualized_return": scenario_summary["annualized_return"],
                    "max_drawdown": scenario_summary["max_drawdown"],
                    "annual_return_drawdown_ratio": scenario_summary["annual_return_drawdown_ratio"],
                }
            )
        summary["rebalance_month_scenarios"] = month_rows
    logger.info("run_backtest metrics complete seconds=%.3f", time.perf_counter() - metrics_started_at)

    raise_if_cancelled(should_cancel)
    if not persist:
        logger.info("run_backtest analysis complete month=%s total_seconds=%.3f", config["annual_rebalance_month"], time.perf_counter() - started_at)
        return {"run_id": run_id, "summary": summary, "cache": {"hit": False, "mode": "分析计算"}}

    persist_started_at = time.perf_counter()
    conn.execute(
        "INSERT INTO backtest_runs(run_id, created_at, config_hash, config_json, summary_json) VALUES(?,?,?,?,?)",
        (run_id, utc_now(), config_hash, json_dumps(config), json_dumps(summary)),
    )
    insert_many(conn, "portfolio_daily", daily_rows)
    for trade in trades:
        trade["run_id"] = run_id
    insert_many(conn, "trades", trades)
    insert_many(conn, "rebalance_events", rebalance_rows)
    logger.info("run_backtest persisted run_id=%s rows=%d trades=%d rebalance=%d seconds=%.3f total_seconds=%.3f", run_id, len(daily_rows), len(trades), len(rebalance_rows), time.perf_counter() - persist_started_at, time.perf_counter() - started_at)
    return {"run_id": run_id, "summary": summary, "cache": {"hit": False, "mode": "重新计算"}}
