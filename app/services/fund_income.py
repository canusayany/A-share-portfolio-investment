"""Published cash and total-return data used by the portfolio's ETF routes."""

from datetime import date, datetime, timedelta
import html
import json
import math
import re
from zoneinfo import ZoneInfo

from app.services.calendar import market_business_days


def published_series(body, variable):
    match = re.search(r"var\s+" + re.escape(variable) + r"\s*=\s*(\[.*?\]);", body, re.S)
    if not match:
        raise ValueError(f"missing published series {variable}")
    return json.loads(match.group(1))


def shanghai_date(timestamp_ms):
    return datetime.fromtimestamp(int(timestamp_ms) / 1000, ZoneInfo("Asia/Shanghai")).date().isoformat()


def money_income_expected(conn, start, end):
    """Each open session plus the issuer's final publication in a consecutive holiday."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    sessions = market_business_days(conn, "CN", first - timedelta(days=20), last + timedelta(days=20))
    expected = set(sessions)
    for prev, current in zip(sessions, sessions[1:]):
        if (current - prev).days > 1:
            expected.add(current - timedelta(days=1))
    return {day.isoformat() for day in expected if first <= day <= last}


def missing_money_dates(conn, start, end):
    existing = conn.execute(
        "SELECT income_date FROM money_fund_income WHERE symbol='511990.SH' AND income_date BETWEEN ? AND ?",
        (start, end),
    )
    return money_income_expected(conn, start, end) - {row[0] for row in existing}


def parse_money_income(issuer, history, start, end):
    if not re.search(r'id="fundCode"\s+value="511990"', issuer) or "百份收益" not in issuer:
        raise ValueError("华宝官网的基金身份或单位校验失败")
    table = re.search(r'<tbody\s+id="navList"[^>]*>(.*?)</tbody>', issuer, re.S)
    if not table:
        raise ValueError("华宝官网缺少收益记录")
    official = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table[1], re.S):
        cells = [re.sub(r"<[^>]+>", "", cell).strip() for cell in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) == 3:
            official[date.fromisoformat(cells[0]).isoformat()] = float(cells[2])
    if not re.search(r'fS_code\s*=\s*"511990"', history):
        raise ValueError("华宝添益历史收益代码不符")
    values = {shanghai_date(ts): float(value) for ts, value in published_series(history, "Data_millionCopiesIncome")}
    overlap = values.keys() & official.keys()
    if not overlap or any(abs(values[d] - official[d]) > 0.000051 for d in overlap):
        raise ValueError("华宝添益历史收益与官网每百份收益不能交叉确认")
    values.update(official)
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError("华宝添益收益包含非数值")
    return [
        {"symbol": "511990.SH", "income_date": day, "income_per_100": value,
         "source": "fsfund:income_per_100" if day in official else "eastmoney:income_per_100:fsfund_checked"}
        for day, value in sorted(values.items()) if start <= day <= end
    ]


def verified_voo_dividends(issuer_page, archive, start, end):
    body = html.unescape(issuer_page)
    if '"ticker":"VOO"' not in body or '"fundId":"0968"' not in body:
        raise ValueError("Vanguard VOO identity mismatch")
    match = re.search(r'"incomeCapitalGains"\s*:\s*', body)
    if not match:
        raise ValueError("Vanguard returned no distribution history")
    records, _ = json.JSONDecoder().raw_decode(body[match.end():])
    events = {}
    for row in archive:
        if row["currency"] != "USD" or row["pay_date"] < row["ex_date"] or row["div_cash"] <= 0:
            raise ValueError("VOO dividend archive has an invalid payment")
        events[row["ex_date"]] = {**row, "source": "digrin:paid_dividend:vanguard_checked",
                                  "ann_date": None, "record_date": None}
    official_dates = set()
    for record in records:
        if record.get("type") != "Dividend":
            raise ValueError("unrecognized Vanguard distribution type")
        paid = datetime.strptime(record["payableDate"], "%m/%d/%Y").date().isoformat()
        record_date = datetime.strptime(record["recordDate"], "%m/%d/%Y").date().isoformat()
        amount = float(record["share"].replace("$", "").replace(",", ""))
        matches = [r for r in events.values() if r["pay_date"] == paid and abs(r["div_cash"] - amount) < 0.000051]
        if len(matches) != 1:
            raise ValueError(f"VOO archive and Vanguard disagree on payment {paid}")
        event = matches[0]
        event.update(record_date=record_date, div_cash=amount, source="vanguard:distribution:digrin_ex_date")
        official_dates.add(event["ex_date"])
    if not official_dates or max(events, default="") != max(official_dates):
        raise ValueError("VOO distribution archive lacks a matching issuer endpoint")
    # Check completed calendar quarters back to inception even when repairing
    # an isolated week, for which an empty query would otherwise look valid.
    quarters = {(int(d[:4]), (int(d[5:7]) - 1) // 3) for d in events}
    year, quarter = 2010, 2
    while True:
        if quarter == 3:
            next_quarter = date(year + 1, 1, 1)
        else:
            next_quarter = date(year, 3 * (quarter + 1) + 1, 1)
        if next_quarter > date.fromisoformat(end) + timedelta(days=1):
            break
        if (year, quarter) not in quarters:
            raise ValueError(f"VOO missing distribution in {year} Q{quarter + 1}")
        year, quarter = next_quarter.year, (next_quarter.month - 1) // 3
    asof_dates = [datetime.strptime(d, "%m/%d/%Y").date() for d in
                  re.findall(r'"asOfDate"\s*:\s*"(\d\d/\d\d/\d{4})"', body)]
    if not asof_dates or date.fromisoformat(end) > max(asof_dates):
        raise ValueError("Vanguard data has not reached the requested end date")
    return [event for day, event in sorted(events.items()) if start <= day <= end]
