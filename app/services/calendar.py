from __future__ import annotations

from bisect import bisect_left
from datetime import date, timedelta
from functools import lru_cache


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def daterange(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def is_weekday(day: date) -> bool:
    return day.weekday() < 5


def business_days(start: str | date, end: str | date) -> list[date]:
    start_date = parse_date(start)
    end_date = parse_date(end)
    return [day for day in daterange(start_date, end_date) if is_weekday(day)]


def market_business_days(conn, market: str, start: str | date, end: str | date) -> list[date]:
    """Use published exchange sessions, never infer a closure from missing quotes.

    Unknown dates remain potential weekdays for conservative sync planning.
    The preflight separately rejects incomplete calendars before a backtest runs.
    """
    start_date, end_date = parse_date(start), parse_date(end)
    if conn is None:
        return business_days(start_date, end_date)
    rows = conn.execute(
        "SELECT trade_date, is_open FROM trading_calendar WHERE market=? AND trade_date BETWEEN ? AND ?",
        (market, start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    states = {row["trade_date"]: bool(row["is_open"]) for row in rows}
    return [day for day in daterange(start_date, end_date) if states.get(day.isoformat(), is_weekday(day))]


def market_day_on_or_before(conn, market: str, day: date) -> date:
    days = market_business_days(conn, market, day - timedelta(days=31), day)
    if not days:
        raise ValueError(f"{market} 交易日历缺少 {day.isoformat()} 前的开市日")
    return days[-1]


def calendar_missing_years(conn, market: str, start: str | date, end: str | date) -> list[int]:
    """Require an explicit open/closed entry for every natural day in range."""
    start_date, end_date = parse_date(start), parse_date(end)
    if start_date > end_date:
        return []
    rows = conn.execute(
        "SELECT trade_date FROM trading_calendar WHERE market=? AND trade_date BETWEEN ? AND ? AND is_open IN (0,1)",
        (market, start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    existing = {row["trade_date"] for row in rows}
    return sorted({day.year for day in daterange(start_date, end_date) if day.isoformat() not in existing})


def first_business_day_by_month(days: list[date]) -> set[date]:
    result: set[date] = set()
    seen: set[tuple[int, int]] = set()
    for day in days:
        key = (day.year, day.month)
        if key not in seen:
            result.add(day)
            seen.add(key)
    return result


def rebalance_days(days: list[date], frequency: str, annual_rebalance_month: int = 1) -> set[date]:
    result: set[date] = set()
    seen: set[tuple[int, ...]] = set()
    if frequency == "daily":
        return set(days)
    for day in days:
        if frequency == "weekly":
            iso_year, iso_week, _ = day.isocalendar()
            key = (iso_year, iso_week)
        elif frequency == "monthly":
            key = (day.year, day.month)
        elif frequency == "quarterly":
            if day.month not in {1, 4, 7, 10}:
                continue
            key = (day.year, day.month)
        elif frequency == "semiannual":
            if day.month not in {1, 7}:
                continue
            key = (day.year, day.month)
        else:
            if day.month != annual_rebalance_month:
                continue
            key = (day.year, day.month)
        if key in seen:
            continue
        result.add(day)
        seen.add(key)
    if days:
        result.add(days[0])
    return result


@lru_cache(maxsize=4096)
def next_business_day(day: date) -> date:
    current = day + timedelta(days=1)
    while not is_weekday(current):
        current += timedelta(days=1)
    return current


@lru_cache(maxsize=16384)
def add_business_days(day: date, count: int) -> date:
    current = day
    for _ in range(max(count, 1)):
        current = next_business_day(current)
    return current


def repo_maturity_day(
    trade_day: date,
    tenor_days: int = 1,
    trading_days: list[date] | None = None,
) -> date:
    """Return the repo settlement date using calendar-day tenor rules.

    Exchange repo tenors are calendar days.  If the contractual maturity is
    not a trading day, settlement rolls to the next trading day.  A supplied
    market calendar also handles mainland public holidays; the weekday
    fallback keeps isolated fee/unit tests deterministic.
    """
    contractual_maturity = trade_day + timedelta(days=max(int(tenor_days), 1))
    if trading_days:
        index = bisect_left(trading_days, contractual_maturity)
        if index < len(trading_days):
            return trading_days[index]
    maturity = contractual_maturity
    while not is_weekday(maturity):
        maturity += timedelta(days=1)
    return maturity


def repo_actual_days(
    trade_day: date,
    tenor_days: int = 1,
    trading_days: list[date] | None = None,
) -> int:
    maturity = repo_maturity_day(trade_day, tenor_days, trading_days)
    return max((maturity - trade_day).days, 1)
