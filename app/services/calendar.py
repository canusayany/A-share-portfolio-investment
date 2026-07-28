from __future__ import annotations

from datetime import date, timedelta


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


def first_business_day_by_month(days: list[date]) -> set[date]:
    result: set[date] = set()
    seen: set[tuple[int, int]] = set()
    for day in days:
        key = (day.year, day.month)
        if key not in seen:
            result.add(day)
            seen.add(key)
    return result


def rebalance_days(days: list[date], frequency: str) -> set[date]:
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
            if day.month != 1:
                continue
            key = (day.year, day.month)
        if key in seen:
            continue
        result.add(day)
        seen.add(key)
    if days:
        result.add(days[0])
    return result


def next_business_day(day: date) -> date:
    current = day + timedelta(days=1)
    while not is_weekday(current):
        current += timedelta(days=1)
    return current


def add_business_days(day: date, count: int) -> date:
    current = day
    for _ in range(max(count, 1)):
        current = next_business_day(current)
    return current


def repo_actual_days(trade_day: date, tenor_days: int = 1) -> int:
    maturity = add_business_days(trade_day, tenor_days)
    return max((maturity - trade_day).days, 1)
