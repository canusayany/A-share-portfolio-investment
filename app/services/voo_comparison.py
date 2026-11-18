"""Unlevered VOO buy-and-hold reference, reported on the strategy's CNY valuation dates."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import date
import math

from app.config import asset_trade_start_date
from app.services.backtest_engine import (
    PortfolioState, Position, _buy_position, _execution_price, _sell_position,
    _us_sale_proceeds, load_dividend_events, load_fx_maps, load_price_map, raise_if_cancelled,
)
from app.services.calendar import daterange, parse_date
from app.services.data_sync import missing_coverage_ranges, price_anomaly_ranges
from app.services.fees import FxFeeConfig, dict_to_dataclass, usd_to_cny


def _convert_paid_usd(state, usd_cash, needed, rate, fees):
    deficit = needed - state.cash_cny
    if deficit > 0 and usd_cash > 0:
        fx = dict_to_dataclass(FxFeeConfig, fees["fx"])
        net, _ = usd_to_cny(usd_cash, rate, fx, include_wire=False)
        if net > 0:
            converted = usd_cash * min(deficit / net, 1.0)
            received, charge = usd_to_cny(converted, rate, fx, include_wire=False)
            state.cash_cny += received
            state.total_fees_cny += charge
            usd_cash -= converted
    return usd_cash


def _fund_withdrawal(state, position, usd_cash, needed, day, opening, rate, fees, fractional, trades):
    """Sell only enough shares at the opening quote to cover the known net requirement."""
    usd_cash = _convert_paid_usd(state, usd_cash, needed, rate, fees)
    deficit = needed - state.cash_cny
    if deficit <= 1e-6 or position.quantity <= 0:
        return usd_cash
    fill = _execution_price(position, opening, fees, "SELL")
    low, high = 0.0, position.quantity
    proceeds, _ = _us_sale_proceeds(high * position.share_scale, high * fill, rate, fees)
    if proceeds <= 0:
        return usd_cash
    if proceeds > deficit:
        # Commission caps, minimums and TAF prevent a single proportional
        # division from finding the exact net sale proceeds.
        for _ in range(48):
            middle = (low + high) / 2
            net, _ = _us_sale_proceeds(middle * position.share_scale, middle * fill, rate, fees)
            if net < deficit:
                low = middle
            else:
                high = middle
    if not fractional:
        high = math.ceil(high * position.share_scale - 1e-12) / position.share_scale
    _sell_position(state, position, day, high, opening, {"USD/CNY": rate}, fees, trades, "voo_withdrawal")
    return usd_cash


def run_voo_comparison(conn, config, dates: list[str], spend_amounts: dict[date, float], should_cancel=None):
    start, end = dates[0], dates[-1]
    asset = next(a for a in config["assets"] if a["symbol"] == "VOO")
    listed = asset_trade_start_date(asset, start)
    first_active = max(start, listed)
    report_dates = set(dates)
    close_prices = load_price_map(conn, ["VOO"], first_active, end)["VOO"]
    open_prices = load_price_map(conn, ["VOO"], first_active, end, "open")["VOO"]
    rates = load_fx_maps(conn, ["USD/CNY"], first_active, end)["USD/CNY"]
    fx_dates = sorted(rates)
    ex_events, _ = load_dividend_events(conn, ["VOO"], first_active, end)
    dividend_gaps = missing_coverage_ranges(conn, "dividends", "VOO", first_active, end)
    bad_prices = {d for d, _ in price_anomaly_ranges(conn, "VOO", first_active, end)}
    calendar = dict(conn.execute(
        "SELECT trade_date,is_open FROM trading_calendar WHERE market='US' AND trade_date BETWEEN ? AND ?",
        (first_active, end),
    ))
    us_sessions = sorted(d for d, flag in calendar.items() if flag == 1 and parse_date(d).weekday() < 5)
    # The upcoming expenses are set by the user's dated plan. The cash reserve
    # looks only at that plan and the calendar, never at future prices or FX.
    next_spend = {}
    for withdraw_day, amount in spend_amounts.items():
        withdraw = withdraw_day.isoformat()
        i = bisect_left(us_sessions, withdraw) - 1  # CN withdrawal precedes same-date US trading.
        if i >= 0 and start <= withdraw <= end:
            last_open = us_sessions[i]
            next_spend[last_open] = next_spend.get(last_open, 0.0) + amount

    position = Position("VOO", "US", "USD", "us_etf")
    state = PortfolioState(float(config["initial_capital_cny"]), {"VOO": position})
    fees = config["fees"]
    fractional = config["allow_fractional_us_shares"]
    withholding = float(fees["tax"]["us_dividend_withholding_rate"])
    points, receivable, trades = {}, {}, []
    usd_cash = 0.0
    latest_close = 0.0
    invested = False
    gaps = []
    stats = {"planned_cny": 0.0, "actual_cny": 0.0, "shortfall_cny": 0.0,
             "first_shortfall_date": None}
    first_gap = None
    for index, day in enumerate(daterange(parse_date(start), parse_date(end))):
        if index % 64 == 0:
            raise_if_cancelled(should_cancel)
        d = day.isoformat()
        active = d >= listed
        us_open = active and calendar.get(d) == 1
        fi = bisect_right(fx_dates, d)
        mark_fx = rates[fx_dates[fi - 1]] if fi else None
        prev = fi - 1 - int(d in rates)
        execution_fx = rates[fx_dates[prev]] if prev >= 0 else None
        if active:
            if calendar.get(d) not in (0, 1) or (us_open and day.weekday() >= 5):
                gaps.append("calendar:US")
            if us_open:
                quote = close_prices.get(d)
                if not quote or not math.isfinite(quote) or quote <= 0 or d in bad_prices:
                    gaps.append("prices:VOO")
                if any(first <= d <= last for first, last in dividend_gaps):
                    gaps.append("dividends:VOO")
                if invested and (not execution_fx or not math.isfinite(execution_fx) or execution_fx <= 0):
                    gaps.append("fx_rates:USD/CNY")
                if any(not e["pay_date"] or e["pay_date"] < d or e["currency"] != "USD"
                       or not math.isfinite(e["div_cash"]) or e["div_cash"] < 0 for e in ex_events.get(d, [])):
                    gaps.append("dividends:VOO")
            if d in report_dates and (d not in rates or not mark_fx or not math.isfinite(mark_fx) or mark_fx <= 0):
                gaps.append("fx_rates:USD/CNY")
            if gaps:
                first_gap = d
                break

        planned = spend_amounts.get(day, 0.0)
        if planned and usd_cash > 0 and execution_fx:
            usd_cash = _convert_paid_usd(state, usd_cash, planned, execution_fx, fees)
        reserve = next_spend.get(d, 0.0)
        opening = open_prices.get(d)
        cash_after_spend = max(state.cash_cny - planned, 0.0)
        if us_open and invested and reserve > cash_after_spend and position.quantity > 0:
            net_usd, _ = usd_to_cny(usd_cash, execution_fx, dict_to_dataclass(FxFeeConfig, fees["fx"]), False)
            if reserve > cash_after_spend + net_usd + 1e-6 and (not opening or not math.isfinite(opening) or opening <= 0):
                gaps.append("prices:VOO")
                first_gap = d
                break
        actual = min(planned, max(state.cash_cny, 0.0))
        state.cash_cny -= actual
        shortfall = max(planned - actual, 0.0)
        if shortfall <= 1e-6:
            shortfall = 0.0
        if shortfall and stats["first_shortfall_date"] is None:
            stats["first_shortfall_date"] = d
        stats["planned_cny"] += planned
        stats["actual_cny"] += actual
        stats["shortfall_cny"] += shortfall

        if us_open:
            latest_close = close_prices[d]
            # Source quotes and distributions use post-split units. Before the
            # 2013-10-24 reverse split, each such unit represented two actual
            # exchange shares for whole-share orders and per-share commissions.
            position.share_scale = 2.0 if d < "2013-10-24" else 1.0
            for event in ex_events.get(d, []):
                net = position.quantity * float(event["div_cash"]) * (1.0 - withholding)
                due = event["pay_date"]
                receivable[due] = receivable.get(due, 0.0) + net
            if not invested and execution_fx and math.isfinite(execution_fx) and execution_fx > 0:
                _buy_position(state, position, day, max(state.cash_cny - reserve, 0.0), latest_close,
                              {"USD/CNY": execution_fx}, fees, trades, fractional, "voo_initial")
                invested = position.quantity > 0
            elif invested and reserve > state.cash_cny:
                usd_cash = _fund_withdrawal(state, position, usd_cash, reserve, day, opening,
                                            execution_fx, fees, fractional, trades)
        # A payment with only a pay date becomes spendable after that US session.
        usd_cash += receivable.pop(d, 0.0)
        if d in report_dates:
            usd_value = position.quantity * latest_close + usd_cash + sum(receivable.values())
            total = state.cash_cny + usd_value * (mark_fx or 0.0)
            points[d] = {"total_asset_cny": total, "planned_spend_cny": planned,
                         "actual_spend_cny": actual, "spend_shortfall_cny": shortfall}

    coverage = {"end_date": next(reversed(points), None), "gaps": sorted(set(gaps)), "first_gap_date": first_gap}
    return points, coverage, stats
