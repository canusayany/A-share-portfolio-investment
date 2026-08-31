from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import html
from io import StringIO
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any
from zoneinfo import ZoneInfo
from app.config import DEFAULT_ASSETS, asset_price_start_date, asset_trade_start_date, required_fx_pairs_for_assets
from app.db import insert_many, upsert_assets, utc_now
from app.services.calendar import business_days, daterange, parse_date


TUSHARE_URL = "http://api.tushare.pro"
HTTP_TIMEOUT_SECONDS = 2
CURL_TIMEOUT_SECONDS = 8
CHINABOND_CONNECT_TIMEOUT_SECONDS = 10
CHINABOND_TOTAL_RETURN_TIMEOUT_SECONDS = 90
CHINABOND_YIELD_TIMEOUT_SECONDS = 30
CHINABOND_RETRY_COUNT = 2
CSINDEX_TOTAL_RETURN_TIMEOUT_SECONDS = 60
TROY_OUNCE_GRAMS = 31.1034768
DATASRC_MARKET_APPSETTINGS = Path.home() / "Documents" / "code" / "DataSrc" / "market-data-platform" / "src" / "Market.Api" / "appsettings.json"
DATASRC_SOURCE_PRIORITY = {"tushare": 0, "akshare": 1, "amazingdata": 2, "tdx": 3}
CN_PRICE_SOURCE_PRIORITY = {
    "tushare:fund_daily": 0,
    "datasrc:tushare": 1,
    "datasrc:akshare": 2,
    "datasrc:amazingdata": 3,
    "datasrc:tdx": 4,
    "sohu:hisHq": 5,
    "eastmoney:fund_kline": 6,
    "yahoo:": 7,
}
PRICE_CROSS_SOURCE_MAX_DEVIATION = 0.15
PRICE_ISOLATED_JUMP_THRESHOLD = 0.70
PRICE_NEIGHBOR_MAX_DEVIATION = 0.25
INDEX_PROXY_PRICE_SOURCES = {
    "csindex:index_perf",
    "tushare:index_daily",
    "datasrc:index",
    "sohu:index_kline",
    "eastmoney:index_kline",
}
DIVIDEND_SOURCE_PRIORITY = {
    "tushare:fund_div": 0,
    "eastmoney:fund_dividend": 1,
    "yahoo:chart:dividend": 1,
    "digrin:html:dividend": 2,
    "sina:etf_cumulative_dividend": 9,
}
logger = logging.getLogger(__name__)


class SyncWarning(RuntimeError):
    pass


class SyncCancelled(RuntimeError):
    pass


def raise_if_cancelled(should_cancel=None) -> None:
    if should_cancel and should_cancel():
        raise SyncCancelled("数据同步任务已取消")


def curl_executable() -> str:
    return "curl.exe" if os.name == "nt" else "curl"


def _coverage_gap(
    conn,
    table: str,
    code_col: str,
    code: str,
    date_col: str,
    start: str,
    end: str,
    *,
    require_start: bool = False,
    start_tolerance_days: int = 7,
    end_tolerance_days: int = 7,
) -> bool:
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count, MIN({date_col}) AS first_date, MAX({date_col}) AS last_date
        FROM {table}
        WHERE {code_col}=?
          AND {date_col} BETWEEN ? AND ?
          AND source NOT LIKE 'generated:%'
        """,
        (code, start, end),
    ).fetchone()
    if not row or not row["count"]:
        return True

    first_date = parse_date(row["first_date"])
    last_date = parse_date(row["last_date"])
    expected_days = business_days(start, end)
    if not expected_days:
        return False
    required_start = expected_days[0]
    requested_end = expected_days[-1]
    check_end = min(requested_end, datetime.now(timezone.utc).date())
    if require_start and first_date > required_start + timedelta(days=start_tolerance_days):
        return True
    if last_date < check_end - timedelta(days=end_tolerance_days):
        future_row = conn.execute(
            f"""
            SELECT MIN({date_col}) AS next_date FROM {table}
            WHERE {code_col}=?
              AND {date_col}>?
              AND {date_col}<=?
              AND source NOT LIKE 'generated:%'
            """,
            (code, end, (check_end + timedelta(days=10)).isoformat()),
        ).fetchone()
        if future_row and future_row["next_date"] and last_date >= check_end - timedelta(days=7):
            return False
        return True
    return False


def _has_generated_rows(conn, table: str, code_col: str, codes: list[str], date_col: str, start: str, end: str) -> bool:
    if not codes:
        return False
    placeholders = ",".join("?" for _ in codes)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count FROM {table}
        WHERE {code_col} IN ({placeholders})
          AND {date_col} BETWEEN ? AND ?
          AND source LIKE 'generated:%'
        """,
        (*codes, start, end),
    ).fetchone()
    return bool(row and row["count"])


def missing_date_ranges(
    conn,
    table: str,
    code_col: str,
    code: str,
    date_col: str,
    start: str,
    end: str,
) -> list[tuple[str, str]]:
    expected = business_days(start, end)
    if not expected:
        return []
    rows = conn.execute(
        f"""
        SELECT {date_col} AS trade_date FROM {table}
        WHERE {code_col}=?
          AND {date_col} BETWEEN ? AND ?
          AND source NOT LIKE 'generated:%'
        """,
        (code, start, end),
    ).fetchall()
    existing = {row["trade_date"] for row in rows}
    ranges: list[tuple[str, str]] = []
    range_start = None
    range_end = None
    for day in expected:
        day_text = day.isoformat()
        if day_text not in existing:
            if range_start is None:
                range_start = day
            range_end = day
        elif range_start is not None and range_end is not None:
            ranges.append((range_start.isoformat(), range_end.isoformat()))
            range_start = None
            range_end = None
    if range_start is not None and range_end is not None:
        ranges.append((range_start.isoformat(), range_end.isoformat()))
    return ranges


def missing_adjustment_factor_ranges(
    conn,
    symbol: str,
    start: str,
    end: str,
    *,
    exclude_proxy_prices: bool = False,
) -> list[tuple[str, str]]:
    """Return price dates which need a fund adjustment factor.

    ETF trading calendars can differ from the generic weekday calendar. Requiring
    a factor on a day without a real ETF close creates a false missing-data error,
    so adjustment coverage follows the stored real price dates exactly. An index
    proxy used before an ETF begins trading is already a continuous synthetic
    price level and must not require an ETF fund-adjustment factor.
    """
    price_rows = conn.execute(
        """
        SELECT trade_date FROM prices
        WHERE symbol=? AND trade_date BETWEEN ? AND ? AND source NOT LIKE 'generated:%'
          AND (?=0 OR source NOT LIKE '%:splice_%')
        ORDER BY trade_date
        """,
        (symbol, start, end, int(exclude_proxy_prices)),
    ).fetchall()
    factor_dates = {
        row["trade_date"]
        for row in conn.execute(
            """
            SELECT trade_date FROM adj_factors
            WHERE symbol=? AND trade_date BETWEEN ? AND ? AND source NOT LIKE 'generated:%'
            """,
            (symbol, start, end),
        ).fetchall()
    }
    missing_dates = [row["trade_date"] for row in price_rows if row["trade_date"] not in factor_dates]
    if not missing_dates:
        return []
    ranges: list[tuple[str, str]] = []
    range_start = missing_dates[0]
    previous = parse_date(range_start)
    for trade_date in missing_dates[1:]:
        current = parse_date(trade_date)
        if (current - previous).days > 4:
            ranges.append((range_start, previous.isoformat()))
            range_start = trade_date
        previous = current
    ranges.append((range_start, previous.isoformat()))
    return ranges


def adjustment_factor_tail_is_tolerable(
    conn,
    symbol: str,
    missing_ranges: list[tuple[str, str]],
    max_calendar_days: int = 7,
) -> bool:
    """Allow only a short missing tail after the latest published factor."""
    if len(missing_ranges) != 1:
        return False
    latest = conn.execute(
        """
        SELECT trade_date FROM adj_factors
        WHERE symbol=? AND source NOT LIKE 'generated:%'
          AND source NOT LIKE 'carry_forward:%'
        ORDER BY trade_date DESC LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    if not latest:
        return False
    latest_date = parse_date(latest["trade_date"])
    gap_start = parse_date(missing_ranges[0][0])
    gap_end = parse_date(missing_ranges[0][1])
    return gap_start > latest_date and 0 < (gap_end - latest_date).days <= max_calendar_days


def carry_forward_adjustment_factor_rows(
    conn,
    symbol: str,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Carry the latest published factor across a real-price tail.

    Fund adjustment factors are step functions. Providers may publish the
    current factor a few sessions after the exchange price, so requiring a new
    provider row on every price date can falsely block an otherwise complete
    backtest. Only dates after the latest non-derived factor are filled; no
    historical interior gap is inferred, and a later official upsert wins.
    """
    latest = conn.execute(
        """
        SELECT trade_date, adj_factor, source FROM adj_factors
        WHERE symbol=? AND trade_date<=? AND source NOT LIKE 'generated:%'
          AND source NOT LIKE 'carry_forward:%'
        ORDER BY trade_date DESC LIMIT 1
        """,
        (symbol, end),
    ).fetchone()
    if not latest:
        return []
    tail_start = max(parse_date(start), parse_date(latest["trade_date"]) + timedelta(days=1))
    if tail_start > parse_date(end):
        return []
    existing = {
        row["trade_date"]
        for row in conn.execute(
            """
            SELECT trade_date FROM adj_factors
            WHERE symbol=? AND trade_date BETWEEN ? AND ?
              AND source NOT LIKE 'generated:%'
            """,
            (symbol, tail_start.isoformat(), end),
        )
    }
    price_dates = [
        row["trade_date"]
        for row in conn.execute(
            """
            SELECT trade_date FROM prices
            WHERE symbol=? AND trade_date BETWEEN ? AND ?
              AND source NOT LIKE 'generated:%' AND source NOT LIKE '%:splice_%'
            ORDER BY trade_date
            """,
            (symbol, tail_start.isoformat(), end),
        )
    ]
    source = str(latest["source"] or "unknown")
    return [
        {
            "symbol": symbol,
            "trade_date": trade_date,
            "adj_factor": float(latest["adj_factor"]),
            "source": f"carry_forward:{source}",
        }
        for trade_date in price_dates
        if trade_date not in existing
    ]


def missing_tail_date_ranges(
    conn,
    table: str,
    code_col: str,
    code: str,
    date_col: str,
    start: str,
    end: str,
) -> list[tuple[str, str]]:
    requested_start = parse_date(start)
    requested_end = parse_date(end)
    if requested_start > requested_end:
        return []
    row = conn.execute(
        f"""
        SELECT MAX({date_col}) AS last_date FROM {table}
        WHERE {code_col}=?
          AND {date_col} BETWEEN ? AND ?
          AND source NOT LIKE 'generated:%'
        """,
        (code, start, end),
    ).fetchone()
    if not row or not row["last_date"]:
        return missing_date_ranges(conn, table, code_col, code, date_col, start, end)
    tail_start = max(parse_date(row["last_date"]) + timedelta(days=1), requested_start)
    if tail_start > requested_end:
        return []
    return missing_date_ranges(conn, table, code_col, code, date_col, tail_start.isoformat(), end)


def missing_edge_date_ranges(
    conn,
    table: str,
    code_col: str,
    code: str,
    date_col: str,
    start: str,
    end: str,
    *,
    start_tolerance_days: int = 7,
) -> list[tuple[str, str]]:
    """Return uncovered prefix and suffix ranges without refetching holiday gaps."""
    requested_start = parse_date(start)
    requested_end = parse_date(end)
    if requested_start > requested_end:
        return []
    row = conn.execute(
        f"""
        SELECT MIN({date_col}) AS first_date, MAX({date_col}) AS last_date
        FROM {table}
        WHERE {code_col}=?
          AND {date_col} BETWEEN ? AND ?
          AND source NOT LIKE 'generated:%'
        """,
        (code, start, end),
    ).fetchone()
    if not row or not row["first_date"] or not row["last_date"]:
        return [(start, end)]

    expected_days = business_days(start, end)
    if not expected_days:
        return []
    required_start = expected_days[0]
    first_date = parse_date(row["first_date"])
    last_date = parse_date(row["last_date"])
    ranges: list[tuple[str, str]] = []
    if first_date > required_start + timedelta(days=start_tolerance_days):
        ranges.append((requested_start.isoformat(), (first_date - timedelta(days=1)).isoformat()))
    if last_date < requested_end:
        ranges.append(((last_date + timedelta(days=1)).isoformat(), requested_end.isoformat()))
    return ranges


def missing_coverage_ranges(conn, kind: str, symbol: str, start: str, end: str) -> list[tuple[str, str]]:
    requested_start = parse_date(start)
    requested_end = parse_date(end)
    if requested_start > requested_end:
        return []
    rows = conn.execute(
        """
        SELECT start_date, end_date FROM sync_coverage
        WHERE kind=? AND symbol=? AND end_date>=? AND start_date<=?
        """,
        (kind, symbol, start, end),
    ).fetchall()
    intervals: list[tuple[Any, Any]] = []
    for row in rows:
        coverage_start = max(parse_date(row["start_date"]), requested_start)
        coverage_end = min(parse_date(row["end_date"]), requested_end)
        if coverage_start <= coverage_end:
            intervals.append((coverage_start, coverage_end))

    if not intervals:
        return [(start, end)]

    intervals.sort()
    ranges: list[tuple[str, str]] = []
    cursor = requested_start
    for coverage_start, coverage_end in intervals:
        if coverage_end < cursor:
            continue
        if coverage_start > cursor:
            ranges.append((cursor.isoformat(), (coverage_start - timedelta(days=1)).isoformat()))
        cursor = max(cursor, coverage_end + timedelta(days=1))
        if cursor > requested_end:
            break
    if cursor <= requested_end:
        ranges.append((cursor.isoformat(), requested_end.isoformat()))
    return ranges


def mark_sync_coverage(conn, kind: str, symbol: str, start: str, end: str, source: str) -> None:
    start_date = parse_date(start)
    end_date = parse_date(end)
    if start_date > end_date:
        return
    overlap_start = (start_date - timedelta(days=1)).isoformat()
    overlap_end = (end_date + timedelta(days=1)).isoformat()
    rows = conn.execute(
        """
        SELECT start_date, end_date FROM sync_coverage
        WHERE kind=? AND symbol=? AND end_date>=? AND start_date<=?
        """,
        (kind, symbol, overlap_start, overlap_end),
    ).fetchall()
    for row in rows:
        start_date = min(start_date, parse_date(row["start_date"]))
        end_date = max(end_date, parse_date(row["end_date"]))
    conn.execute(
        """
        DELETE FROM sync_coverage
        WHERE kind=? AND symbol=? AND end_date>=? AND start_date<=?
        """,
        (kind, symbol, overlap_start, overlap_end),
    )
    conn.execute(
        """
        INSERT INTO sync_coverage(kind, symbol, start_date, end_date, source, updated_at)
        VALUES(?,?,?,?,?,?)
        """,
        (kind, symbol, start_date.isoformat(), end_date.isoformat(), source, utc_now()),
    )


def _sync_plan(missing_items: list[str] | None, assets: list[dict[str, Any]], repo_symbol: str) -> dict[str, Any]:
    asset_symbols = {asset["symbol"] for asset in assets if asset.get("enabled", True)}
    fx_pairs = set(required_fx_pairs_for_assets(assets))
    if missing_items is None or any(item.startswith("generated:") for item in missing_items):
        return {
            "asset_prices": set(asset_symbols),
            "asset_dividends": set(asset_symbols),
            "asset_adjustments": {asset["symbol"] for asset in assets if asset.get("enabled", True) and asset.get("market") == "CN"},
            "index_prices": True,
            "repo_symbols": set(sorted({"204001", repo_symbol})),
            "fx_pairs": fx_pairs,
            "full": True,
        }

    plan = {
        "asset_prices": set(),
        "asset_dividends": set(),
        "asset_adjustments": set(),
        "index_prices": False,
        "repo_symbols": set(),
        "fx_pairs": set(),
        "full": False,
    }
    for item in missing_items:
        if ":" not in item:
            continue
        kind, symbol = item.split(":", 1)
        if kind == "prices" and symbol == "000300.SH":
            plan["index_prices"] = True
        elif kind == "prices" and symbol in asset_symbols:
            plan["asset_prices"].add(symbol)
        elif kind == "dividends" and symbol in asset_symbols:
            plan["asset_dividends"].add(symbol)
        elif kind == "adj_factors" and symbol in asset_symbols:
            plan["asset_adjustments"].add(symbol)
        elif kind == "repo_rates":
            plan["repo_symbols"].add(symbol)
        elif kind == "fx_rates":
            plan["fx_pairs"].add(symbol)
    if plan["repo_symbols"]:
        plan["repo_symbols"].add("204001")
        plan["repo_symbols"].add(repo_symbol)
    return plan


def previous_weekday(day):
    current = day - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def latest_completed_market_day(market: str, close_hour: int | None = None):
    now_utc = datetime.now(timezone.utc)
    if market == "US":
        now_market = now_utc.astimezone(ZoneInfo("America/New_York"))
        return previous_weekday(now_market.date())
    if market == "HK":
        now_market = now_utc.astimezone(ZoneInfo("Asia/Hong_Kong"))
        return previous_weekday(now_market.date())
    else:
        now_market = now_utc.astimezone(ZoneInfo("Asia/Shanghai"))
        close_hour = 18 if close_hour is None else close_hour
    today = now_market.date()
    if today.weekday() >= 5 or now_market.hour < close_hour:
        return previous_weekday(today)
    return today


def effective_price_end_for_market(market: str, end: str):
    requested_end = parse_date(end)
    latest_completed = latest_completed_market_day(market)
    return min(requested_end, latest_completed)


def replacement_allocation_start(asset: dict[str, Any]) -> date | None:
    starts = []
    for replacement in asset.get("replacement_assets", []):
        if not isinstance(replacement, dict):
            continue
        start = (
            replacement.get("allocation_start_date")
            or replacement.get("trade_start_date")
            or replacement.get("inception_date")
        )
        if start:
            starts.append(parse_date(str(start)))
    return min(starts) if starts else None


def effective_asset_end(asset: dict[str, Any], end: str) -> date:
    """Stop requiring the original symbol once a configured replacement takes over."""
    requested_end = parse_date(end)
    replacement_start = replacement_allocation_start(asset)
    if replacement_start is None:
        return requested_end
    return min(requested_end, replacement_start - timedelta(days=1))


def effective_price_end_for_asset(asset: dict[str, Any], end: str):
    if asset.get("asset_type") == "cn_bond_index":
        # ChinaBond total-return indices are published after the exchange close
        # and the exact release time is not guaranteed.  Treat the current
        # natural day's value as eligible on the following day.  Historical
        # end dates remain strict.
        market_end = min(
            parse_date(end),
            latest_completed_market_day("CN", close_hour=24),
        )
    else:
        market_end = effective_price_end_for_market(asset.get("market", "CN"), end)
    return min(market_end, effective_asset_end(asset, end))


def required_data_missing(
    conn,
    start: str,
    end: str,
    assets: list[dict[str, Any]] | None = None,
    repo_symbol: str = "204001",
) -> list[str]:
    assets = assets or DEFAULT_ASSETS
    requested_end = parse_date(end)
    missing: set[str] = set()
    price_symbols = [asset["symbol"] for asset in assets if asset.get("enabled", True)] + ["000300.SH"]

    selected_asset_symbols = {asset["symbol"] for asset in assets if asset.get("enabled", True)}
    for asset in assets:
        if not asset.get("enabled", True):
            continue
        fallback = asset.get("price_fallback")
        fallback_requires_history = isinstance(fallback, dict) and fallback.get("required", True) is not False
        price_fetch_start = max(
            parse_date(start),
            parse_date(asset_price_start_date(asset, start))
            if fallback_requires_history
            else parse_date(asset_trade_start_date(asset, start)),
        )
        dividend_fetch_start = max(parse_date(start), parse_date(asset_trade_start_date(asset, start)))
        price_end = effective_price_end_for_asset(asset, end)
        fallback_covers_pre_inception = (
            isinstance(fallback, dict)
            and fallback_requires_history
            and price_fetch_start < parse_date(asset_trade_start_date(asset, start))
        )
        if price_fetch_start <= price_end and _coverage_gap(
            conn,
            "prices",
            "symbol",
            asset["symbol"],
            "trade_date",
            price_fetch_start.isoformat(),
            price_end.isoformat(),
            require_start=fallback_covers_pre_inception,
            end_tolerance_days=0,
        ):
            missing.add(f"prices:{asset['symbol']}")
        legacy_yahoo_ranges = (
            legacy_cn_yahoo_price_ranges(conn, asset["symbol"], price_fetch_start.isoformat(), price_end.isoformat())
            if asset.get("market") == "CN"
            else []
        )
        unscaled_proxy_ranges = (
            legacy_unscaled_index_proxy_price_ranges(conn, asset["symbol"], price_fetch_start.isoformat(), price_end.isoformat())
            if asset.get("market") == "CN"
            else []
        )
        if price_fetch_start <= price_end and (
            price_anomaly_ranges(conn, asset["symbol"], price_fetch_start.isoformat(), price_end.isoformat())
            or legacy_yahoo_ranges
            or unscaled_proxy_ranges
        ):
            missing.add(f"prices:{asset['symbol']}")
        dividend_end = effective_asset_end(asset, end)
        if asset.get("asset_type") != "money_fund" and dividend_fetch_start <= dividend_end and missing_coverage_ranges(
            conn, "dividends", asset["symbol"], dividend_fetch_start.isoformat(), dividend_end.isoformat()
        ):
            missing.add(f"dividends:{asset['symbol']}")
        if asset["symbol"] in selected_asset_symbols and asset.get("market") == "CN" and asset.get("asset_type") not in {"cn_bond_index", "money_fund"}:
            adjustment_gaps = missing_adjustment_factor_ranges(
                conn,
                asset["symbol"],
                dividend_fetch_start.isoformat(),
                price_end.isoformat(),
                exclude_proxy_prices=isinstance(asset.get("price_fallback"), dict),
            )
            tail_is_tolerable = (
                asset.get("allow_adj_factor_tail_carry_forward")
                and adjustment_factor_tail_is_tolerable(conn, asset["symbol"], adjustment_gaps)
            )
            if adjustment_gaps and not tail_is_tolerable:
                missing.add(f"adj_factors:{asset['symbol']}")

    cn_data_end = effective_price_end_for_market("CN", end)
    cn_data_end_text = cn_data_end.isoformat()
    if parse_date(start) <= cn_data_end and _coverage_gap(conn, "prices", "symbol", "000300.SH", "trade_date", start, cn_data_end_text, require_start=True, end_tolerance_days=0):
        missing.add("prices:000300.SH")
    fx_pairs = required_fx_pairs_for_assets(assets)
    for pair in fx_pairs:
        if parse_date(start) <= cn_data_end and _coverage_gap(conn, "fx_rates", "pair", pair, "trade_date", start, cn_data_end_text, require_start=True, end_tolerance_days=0):
            missing.add(f"fx_rates:{pair}")
    repo_symbols = sorted({"204001", repo_symbol})
    for current_repo_symbol in repo_symbols:
        if parse_date(start) <= cn_data_end and _coverage_gap(conn, "repo_rates", "symbol", current_repo_symbol, "trade_date", start, cn_data_end_text, require_start=True, end_tolerance_days=0):
            missing.add(f"repo_rates:{current_repo_symbol}")

    generated_checks = [
        ("prices", "symbol", price_symbols, "trade_date"),
        ("fund_dividends", "symbol", price_symbols, "ex_date"),
        ("adj_factors", "symbol", price_symbols, "trade_date"),
        ("fx_rates", "pair", fx_pairs, "trade_date"),
        ("repo_rates", "symbol", repo_symbols, "trade_date"),
    ]
    for table, code_col, codes, date_col in generated_checks:
        if _has_generated_rows(conn, table, code_col, codes, date_col, start, end):
            missing.add(f"generated:{table}")

    return sorted(missing)


def tushare_call(token: str, api_name: str, params: dict[str, Any], fields: str = "") -> list[dict[str, Any]]:
    if not token:
        raise SyncWarning("TUSHARE_TOKEN is not configured")
    payload = json.dumps(
        {"api_name": api_name, "token": token, "params": params, "fields": fields},
        ensure_ascii=False,
    )
    payload_bytes = payload.encode("utf-8")
    body = tushare_call_with_curl(payload_bytes)
    if body.get("code") != 0:
        raise SyncWarning(str(body.get("msg") or body))
    fields_list = body.get("data", {}).get("fields", [])
    items = body.get("data", {}).get("items", [])
    return [dict(zip(fields_list, item)) for item in items]


def tushare_call_with_curl(payload: bytes) -> dict[str, Any]:
    base_cmd = [
        curl_executable(),
        "-sS",
        "-L",
        "-A",
        "Mozilla/5.0",
        "--connect-timeout",
        "2",
        "--max-time",
        str(CURL_TIMEOUT_SECONDS),
        "-X",
        "POST",
        "-H",
        "Content-Type: application/json",
        "--data-binary",
        "@-",
        TUSHARE_URL,
    ]
    stdout = run_curl_with_optional_direct_retry(base_cmd, payload)
    try:
        return json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as json_exc:
        text = stdout.decode("utf-8", errors="replace")[:200]
        raise SyncWarning(f"Tushare curl returned invalid JSON: {text}") from json_exc


def run_curl_with_optional_direct_retry(base_cmd: list[str], payload: bytes | None = None) -> bytes:
    errors: list[str] = []
    for cmd in (base_cmd, [curl_executable(), "-sS", "--noproxy", "*", *base_cmd[2:]]):
        try:
            completed = subprocess.run(cmd, input=payload, capture_output=True, check=False, timeout=CURL_TIMEOUT_SECONDS + 3)
        except (OSError, subprocess.SubprocessError) as curl_exc:
            errors.append(str(curl_exc))
            continue
        if completed.returncode == 0:
            return completed.stdout
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        errors.append(f"exit {completed.returncode}: {stderr}")
    raise SyncWarning("; ".join(errors))


def load_datasrc_postgres_kwargs() -> dict[str, Any]:
    raw = os.getenv("DATASRC_MARKET_POSTGRES_DSN") or os.getenv("MARKET_POSTGRES_DSN")
    if not raw:
        settings_path = Path(os.getenv("DATASRC_MARKET_APPSETTINGS", DATASRC_MARKET_APPSETTINGS))
        if not settings_path.exists():
            raise SyncWarning("DataSrc Market.Api appsettings not found")
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SyncWarning(f"DataSrc Market.Api appsettings unreadable: {exc}") from exc
        raw = (settings.get("ConnectionStrings") or {}).get("Postgres", "")
    if not raw:
        raise SyncWarning("DataSrc Market.Api Postgres connection string not configured")

    mapping = {
        "host": "host",
        "port": "port",
        "database": "dbname",
        "username": "user",
        "user id": "user",
        "password": "password",
    }
    kwargs: dict[str, Any] = {}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        mapped = mapping.get(key.strip().lower())
        if mapped and value:
            kwargs[mapped] = value.strip()
    if not {"host", "port", "dbname", "user", "password"} <= set(kwargs):
        raise SyncWarning("DataSrc Market.Api Postgres connection string is incomplete")
    kwargs["connect_timeout"] = int(os.getenv("DATASRC_POSTGRES_TIMEOUT_SECONDS", "2"))
    return kwargs


def datasrc_symbol_variants(symbol: str) -> list[str]:
    normalized = symbol.strip().upper()
    bare = normalized.split(".")[0]
    variants = [bare, normalized]
    if bare != normalized:
        variants.extend([f"{bare}.SH", f"{bare}.SZ"])
    return sorted(set(variants))


def select_best_datasrc_price_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[str, tuple[int, dict[str, Any]]] = {}
    for row in rows:
        trade_date = row["trade_date"]
        source = str(row.get("source") or "")
        priority = DATASRC_SOURCE_PRIORITY.get(source, 50)
        current = selected.get(trade_date)
        if current is None or priority < current[0]:
            selected[trade_date] = (priority, row)
    return [item[1] for item in sorted(selected.values(), key=lambda item: item[1]["trade_date"])]


def merge_rows_by_trade_date(primary: list[dict[str, Any]], fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date = {row["trade_date"]: row for row in fallback}
    by_date.update({row["trade_date"]: row for row in primary})
    return [by_date[key] for key in sorted(by_date)]


def merge_date_ranges(ranges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    parsed = sorted((parse_date(start), parse_date(end)) for start, end in ranges if parse_date(start) <= parse_date(end))
    if not parsed:
        return []
    merged: list[tuple[Any, Any]] = []
    current_start, current_end = parsed[0]
    for start, end in parsed[1:]:
        if start <= current_end + timedelta(days=1):
            current_end = max(current_end, end)
        else:
            merged.append((current_start, current_end))
            current_start, current_end = start, end
    merged.append((current_start, current_end))
    return [(start.isoformat(), end.isoformat()) for start, end in merged]


def asset_price_sync_ranges(
    conn,
    asset: dict[str, Any],
    start: str,
    end: str,
    price_range_func,
) -> list[tuple[str, str]]:
    """Return efficient price ranges while preserving pre-inception fallback coverage."""
    requested_start = parse_date(start)
    requested_end = parse_date(end)
    fallback = asset.get("price_fallback")
    fallback_requires_history = isinstance(fallback, dict) and fallback.get("required", True) is not False
    price_start = max(
        requested_start,
        parse_date(asset_price_start_date(asset, start))
        if fallback_requires_history
        else parse_date(asset_trade_start_date(asset, start)),
    )
    if price_start > requested_end:
        return []
    if not isinstance(fallback, dict):
        return price_range_func(conn, "prices", "symbol", asset["symbol"], "trade_date", price_start.isoformat(), requested_end.isoformat())

    primary_start = max(price_start, parse_date(asset_trade_start_date(asset, start)))
    ranges: list[tuple[str, str]] = []
    fallback_end = min(requested_end, primary_start - timedelta(days=1))
    if price_start <= fallback_end:
        fallback_range_func = (
            missing_tail_date_ranges
            if fallback.get("kind") in {"sge_au9999", "chinabond_30y_yield_total_return"}
            else missing_date_ranges
        )
        fallback_gaps = fallback_range_func(
            conn,
            "prices",
            "symbol",
            asset["symbol"],
            "trade_date",
            price_start.isoformat(),
            fallback_end.isoformat(),
        )
        if fallback_gaps:
            # A splice scale must be shared by the complete proxy segment. If
            # an early row is repaired in isolation, its local window may not
            # contain the later real ETF close required as an anchor. Rebuild
            # the full pre-inception segment once instead.
            ranges.append((price_start.isoformat(), fallback_end.isoformat()))
    if primary_start <= requested_end:
        ranges.extend(
            price_range_func(
                conn,
                "prices",
                "symbol",
                asset["symbol"],
                "trade_date",
                primary_start.isoformat(),
                requested_end.isoformat(),
            )
        )
    return merge_date_ranges(ranges)


def chinabond_price_sync_ranges(
    conn,
    _table: str,
    _code_col: str,
    code: str,
    _date_col: str,
    start: str,
    end: str,
) -> list[tuple[str, str]]:
    """Return one initial fetch and then only a tail fetch for ChinaBond data.

    The official series has its own bond-market calendar, so treating every
    weekday as a missing price causes repeated refetches over public holidays.
    The endpoint returns the complete series; the most recent official value is
    therefore the only meaningful incremental coverage check.
    """
    return missing_tail_date_ranges(conn, "prices", "symbol", code, "trade_date", start, end)


def legacy_chinabond_modeled_overlap_ranges(
    conn,
    asset: dict[str, Any],
    start: str,
    end: str,
) -> list[tuple[str, str]]:
    """Find modeled 30Y rows that incorrectly overlap the official index era."""
    fallback = asset.get("price_fallback")
    if not isinstance(fallback, dict) or fallback.get("kind") != "chinabond_30y_yield_total_return":
        return []
    official_start = max(parse_date(start), parse_date(asset_trade_start_date(asset, start)))
    requested_end = parse_date(end)
    if official_start > requested_end:
        return []
    row = conn.execute(
        """
        SELECT MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
        FROM prices
        WHERE symbol=? AND trade_date BETWEEN ? AND ?
          AND source LIKE 'chinabond:30y_yield_curve:%modeled_total_return%'
        """,
        (asset["symbol"], official_start.isoformat(), requested_end.isoformat()),
    ).fetchone()
    if not row or not row["first_date"]:
        return []
    return [(str(row["first_date"]), str(row["last_date"]))]


def _row_close(row: dict[str, Any]) -> float | None:
    close = finite_float(row.get("close"))
    return close if close and close > 0 else None


def _relative_deviation(value: float, reference: float) -> float:
    if reference <= 0:
        return math.inf
    return abs(value - reference) / reference


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _source_priority(source: str, priorities: dict[str, int]) -> int:
    for prefix, priority in priorities.items():
        if source.startswith(prefix):
            return priority
    return 100


def _sort_price_candidates(rows: list[dict[str, Any]], priorities: dict[str, int]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (_source_priority(str(row.get("source") or ""), priorities), str(row.get("source") or "")))


def detect_isolated_price_anomaly_dates(
    rows: list[dict[str, Any]],
    *,
    jump_threshold: float = PRICE_ISOLATED_JUMP_THRESHOLD,
    neighbor_threshold: float = PRICE_NEIGHBOR_MAX_DEVIATION,
) -> list[str]:
    ordered = [row for row in sorted(rows, key=lambda item: item["trade_date"]) if _row_close(row) is not None]
    result: list[str] = []
    for index in range(1, len(ordered) - 1):
        previous_close = _row_close(ordered[index - 1])
        current_close = _row_close(ordered[index])
        next_close = _row_close(ordered[index + 1])
        if previous_close is None or current_close is None or next_close is None:
            continue
        if _relative_deviation(previous_close, next_close) > neighbor_threshold:
            continue
        if _relative_deviation(current_close, previous_close) > jump_threshold and _relative_deviation(current_close, next_close) > jump_threshold:
            result.append(str(ordered[index]["trade_date"]))
    return result


def price_anomaly_ranges(conn, symbol: str, start: str, end: str) -> list[tuple[str, str]]:
    requested_start = parse_date(start)
    requested_end = parse_date(end)
    if requested_start > requested_end:
        return []
    anchor_start = (requested_start - timedelta(days=10)).isoformat()
    anchor_end = (requested_end + timedelta(days=10)).isoformat()
    rows = conn.execute(
        """
        SELECT trade_date, close, source FROM prices
        WHERE symbol=? AND trade_date BETWEEN ? AND ?
          AND source NOT LIKE 'generated:%'
        ORDER BY trade_date
        """,
        (symbol, anchor_start, anchor_end),
    ).fetchall()
    anomaly_dates = [
        trade_date
        for trade_date in detect_isolated_price_anomaly_dates([dict(row) for row in rows])
        if requested_start <= parse_date(trade_date) <= requested_end
    ]
    return [(trade_date, trade_date) for trade_date in anomaly_dates]


def legacy_cn_yahoo_price_ranges(conn, symbol: str, start: str, end: str) -> list[tuple[str, str]]:
    """Find legacy CN ETF rows sourced from Yahoo's adjusted-price feed.

    Yahoo's China ETF chart endpoint can return a back-adjusted series even
    though the application requests raw prices. It must never be blended with
    Tushare/Sohu/Eastmoney raw closes: 512100 otherwise jumps from roughly 0.53
    to 1.43 in January 2019 without any corresponding corporate action.
    """
    rows = conn.execute(
        """
        SELECT trade_date FROM prices
        WHERE symbol=? AND trade_date BETWEEN ? AND ? AND source LIKE 'yahoo:%'
        ORDER BY trade_date
        """,
        (symbol, start, end),
    ).fetchall()
    if not rows:
        return []
    ranges: list[tuple[str, str]] = []
    range_start = rows[0]["trade_date"]
    previous = parse_date(range_start)
    for row in rows[1:]:
        current = parse_date(row["trade_date"])
        if (current - previous).days > 4:
            ranges.append((range_start, previous.isoformat()))
            range_start = row["trade_date"]
        previous = current
    ranges.append((range_start, previous.isoformat()))
    return ranges


def legacy_unscaled_index_proxy_price_ranges(conn, symbol: str, start: str, end: str) -> list[tuple[str, str]]:
    """Find index-proxy rows that were incorrectly inserted without scaling.

    The configured CSI proxies use index points. A large value from an index
    source, either with ``:splice_scale_1`` or with no splice suffix at all,
    therefore means the proxy was saved without a genuine ETF-price anchor.
    Re-fetching the affected dates lets the normal splice logic use a genuine
    ETF close instead.
    """
    rows = conn.execute(
        """
        SELECT trade_date FROM prices
        WHERE symbol=? AND trade_date BETWEEN ? AND ? AND ABS(close) >= 100
          AND (source LIKE '%:splice_scale_1' OR source IN (?, ?, ?, ?, ?))
        ORDER BY trade_date
        """,
        (symbol, start, end, *sorted(INDEX_PROXY_PRICE_SOURCES)),
    ).fetchall()
    if not rows:
        return []
    ranges: list[tuple[str, str]] = []
    range_start = rows[0]["trade_date"]
    previous = parse_date(range_start)
    for row in rows[1:]:
        current = parse_date(row["trade_date"])
        if (current - previous).days > 4:
            ranges.append((range_start, previous.isoformat()))
            range_start = row["trade_date"]
        previous = current
    ranges.append((range_start, previous.isoformat()))
    return ranges


def select_price_rows_from_sources(
    rows: list[dict[str, Any]],
    *,
    expected_dates: set[str] | None = None,
    existing_rows: list[dict[str, Any]] | None = None,
    source_priorities: dict[str, int] | None = None,
    symbol: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    priorities = source_priorities or {}
    candidates_by_date: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        trade_date = str(row.get("trade_date") or "")
        if expected_dates is not None and trade_date not in expected_dates:
            continue
        if _row_close(row) is None:
            continue
        candidates_by_date.setdefault(trade_date, []).append(row)

    warnings: list[str] = []
    selected_by_date: dict[str, dict[str, Any]] = {}
    for trade_date, candidates in sorted(candidates_by_date.items()):
        ordered = _sort_price_candidates(candidates, priorities)
        closes = [_row_close(row) for row in ordered]
        valid_closes = [close for close in closes if close is not None]
        if len(valid_closes) <= 1:
            selected_by_date[trade_date] = ordered[0]
            continue
        median = _median(valid_closes)
        near_median = [row for row in ordered if (close := _row_close(row)) is not None and _relative_deviation(close, median) <= PRICE_CROSS_SOURCE_MAX_DEVIATION]
        selected = near_median[0] if near_median else ordered[0]
        if selected is not ordered[0]:
            top_close = _row_close(ordered[0])
            selected_close = _row_close(selected)
            warnings.append(
                "price source anomaly "
                f"symbol={symbol or selected.get('symbol')} date={trade_date} "
                f"primary={ordered[0].get('source')} close={top_close:g} "
                f"median={median:g} selected={selected.get('source')} selected_close={selected_close:g}"
            )
        selected_by_date[trade_date] = selected

    context_by_date: dict[str, dict[str, Any]] = {}
    for row in existing_rows or []:
        trade_date = str(row.get("trade_date") or "")
        if trade_date and _row_close(row) is not None:
            context_by_date[trade_date] = row
    context_by_date.update(selected_by_date)
    ordered_dates = sorted(context_by_date)
    date_index = {trade_date: index for index, trade_date in enumerate(ordered_dates)}
    for trade_date in sorted(selected_by_date):
        index = date_index.get(trade_date)
        if index is None or index <= 0 or index >= len(ordered_dates) - 1:
            continue
        previous_close = _row_close(context_by_date[ordered_dates[index - 1]])
        current_close = _row_close(selected_by_date[trade_date])
        next_close = _row_close(context_by_date[ordered_dates[index + 1]])
        if previous_close is None or current_close is None or next_close is None:
            continue
        if _relative_deviation(previous_close, next_close) > PRICE_NEIGHBOR_MAX_DEVIATION:
            continue
        if _relative_deviation(current_close, previous_close) <= PRICE_ISOLATED_JUMP_THRESHOLD or _relative_deviation(current_close, next_close) <= PRICE_ISOLATED_JUMP_THRESHOLD:
            continue
        expected_close = (previous_close + next_close) / 2.0
        alternatives = [
            row
            for row in candidates_by_date.get(trade_date, [])
            if (close := _row_close(row)) is not None and _relative_deviation(close, expected_close) <= PRICE_NEIGHBOR_MAX_DEVIATION
        ]
        if not alternatives:
            continue
        alternatives.sort(key=lambda row: (_relative_deviation(_row_close(row) or 0.0, expected_close), _source_priority(str(row.get("source") or ""), priorities)))
        replacement = alternatives[0]
        if replacement is not selected_by_date[trade_date]:
            warnings.append(
                "isolated price jump replaced "
                f"symbol={symbol or replacement.get('symbol')} date={trade_date} "
                f"bad_source={selected_by_date[trade_date].get('source')} bad_close={current_close:g} "
                f"selected={replacement.get('source')} selected_close={_row_close(replacement):g}"
            )
            selected_by_date[trade_date] = replacement
            context_by_date[trade_date] = replacement

    return [selected_by_date[key] for key in sorted(selected_by_date)], warnings


def fetch_datasrc_market_prices(symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    try:
        import psycopg  # type: ignore
    except ImportError as exc:
        raise SyncWarning("psycopg is not installed; DataSrc Postgres cache unavailable") from exc

    variants = datasrc_symbol_variants(symbol)
    sql = """
        SELECT source, source_dataset, bar_time::date AS trade_date,
               open, high, low, close, volume, amount, adj_factor
        FROM market_bar_1d
        WHERE symbol = ANY(%s)
          AND adjust_type = 'none'
          AND bar_time::date BETWEEN %s::date AND %s::date
          AND bar_time::date >= DATE '1900-01-01'
          AND close IS NOT NULL
          AND close > 0
        ORDER BY trade_date, source
    """
    try:
        with psycopg.connect(**load_datasrc_postgres_kwargs()) as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(sql, (variants, start, end))
                fetched = cur.fetchall()
    except Exception as exc:
        raise SyncWarning(f"DataSrc Postgres price cache fetch failed for {symbol}: {exc}") from exc

    rows: list[dict[str, Any]] = []
    for source, source_dataset, trade_date, open_, high, low, close, volume, amount, adj_factor in fetched:
        dataset = f":{source_dataset}" if source_dataset else ""
        close_float = float(close)
        rows.append(
            {
                "symbol": symbol,
                "trade_date": trade_date.isoformat(),
                "open": float(open_ or close_float),
                "high": float(high or close_float),
                "low": float(low or close_float),
                "close": close_float,
                "adj_close": close_float * float(adj_factor) if adj_factor else close_float,
                "volume": float(volume or 0),
                "amount": float(amount or 0),
                "currency": currency,
                "source": f"datasrc:{source}{dataset}",
            }
        )
    rows = select_best_datasrc_price_rows(rows)
    if not rows:
        raise SyncWarning(f"DataSrc Postgres returned no price rows for {symbol}")
    return rows


def fetch_datasrc_series(series_code: str, series_type: str, start: str, end: str) -> list[dict[str, Any]]:
    try:
        import psycopg  # type: ignore
    except ImportError as exc:
        raise SyncWarning("psycopg is not installed; DataSrc Postgres series cache unavailable") from exc

    codes = [series_code, series_code.replace("/", "")]
    sql = """
        SELECT series_code, series_type, source, point_date, value
        FROM series_point
        WHERE series_code = ANY(%s)
          AND series_type = %s
          AND point_date BETWEEN %s::date AND %s::date
        ORDER BY point_date, source
    """
    try:
        with psycopg.connect(**load_datasrc_postgres_kwargs()) as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute(sql, (sorted(set(codes)), series_type, start, end))
                rows = cur.fetchall()
    except Exception as exc:
        raise SyncWarning(f"DataSrc Postgres series cache fetch failed for {series_code}: {exc}") from exc
    return [
        {
            "series_code": row[0],
            "series_type": row[1],
            "source": row[2],
            "point_date": row[3].isoformat(),
            "value": float(row[4]),
        }
        for row in rows
    ]


def fx_pair_parts(pair: str) -> tuple[str, str]:
    if "/" not in pair:
        raise SyncWarning(f"unsupported FX pair {pair}")
    base, quote = pair.upper().split("/", 1)
    if quote != "CNY":
        raise SyncWarning(f"unsupported FX quote currency for {pair}")
    return base, quote


def fetch_datasrc_fx_rates(start: str, end: str, pair: str = "USD/CNY") -> list[dict[str, Any]]:
    base, _quote = fx_pair_parts(pair)
    rows = fetch_datasrc_series(f"{base}CNY", "fx", start, end)
    result = [
        {"pair": pair, "trade_date": row["point_date"], "rate": row["value"], "source": f"datasrc:{row['source']}:series_point"}
        for row in rows
    ]
    if not result:
        raise SyncWarning(f"DataSrc Postgres returned no {pair} rows")
    return result


def fetch_datasrc_repo_rates(symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    rows = fetch_datasrc_series(symbol, "rate", start, end)
    result = [
        {
            "symbol": symbol,
            "trade_date": row["point_date"],
            "open_rate": row["value"],
            "close_rate": row["value"],
            "high_rate": row["value"],
            "low_rate": row["value"],
            "volume": 0.0,
            "amount": 0.0,
            "source": f"datasrc:{row['source']}:series_point",
        }
        for row in rows
    ]
    if not result:
        raise SyncWarning(f"DataSrc Postgres returned no repo rows for {symbol}")
    return result


def tushare_date(value: str) -> str:
    return value.replace("-", "")


def fetch_text(url: str, timeout: int = HTTP_TIMEOUT_SECONDS, referer: str | None = None) -> str:
    return fetch_text_with_curl(url, timeout, referer)


def fetch_text_with_curl(url: str, timeout: int = HTTP_TIMEOUT_SECONDS, referer: str | None = None) -> str:
    cmd = [curl_executable(), "-sS", "-L", "-A", "Mozilla/5.0", "--connect-timeout", "2", "--max-time", str(timeout)]
    if referer:
        cmd.extend(["-e", referer])
    cmd.append(url)
    try:
        return run_curl_with_optional_direct_retry(cmd).decode("utf-8", errors="replace")
    except SyncWarning as exc:
        raise SyncWarning(f"public source request failed: {exc}") from exc


def from_tushare_date(value: str | None) -> str | None:
    if not value:
        return None
    value = str(value)
    if len(value) == 8 and value.isdigit():
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"
    return value


def fetch_cn_fund_prices(token: str, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    rows = tushare_call(
        token,
        "fund_daily",
        {"ts_code": symbol, "start_date": tushare_date(start), "end_date": tushare_date(end)},
        "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
    )
    if not rows:
        raise SyncWarning(f"Tushare returned no fund_daily rows for {symbol}")
    return [
        {
            "symbol": row["ts_code"],
            "trade_date": from_tushare_date(row["trade_date"]),
            "open": float(row.get("open") or row["close"]),
            "high": float(row.get("high") or row["close"]),
            "low": float(row.get("low") or row["close"]),
            "close": float(row["close"]),
            "adj_close": None,
            "volume": float(row.get("vol") or 0),
            "amount": float(row.get("amount") or 0),
            "currency": "CNY",
            "source": "tushare:fund_daily",
        }
        for row in rows
    ]


def fetch_tushare_sge_au9999_prices(
    token: str,
    target_symbol: str,
    start: str,
    end: str,
    currency: str,
) -> list[dict[str, Any]]:
    """Fetch Shanghai Gold Exchange Au99.99 daily bars from Tushare.

    The endpoint is capped at 2,000 rows.  Chunking keeps the full history
    available when a backtest begins years before the gold ETFs were listed.
    """
    rows: list[dict[str, Any]] = []
    for chunk_start, chunk_end in chunk_date_ranges(start, end, 1460):
        fetched = tushare_call(
            token,
            "sge_daily",
            {
                "ts_code": "Au99.99",
                "start_date": tushare_date(chunk_start),
                "end_date": tushare_date(chunk_end),
            },
            "ts_code,trade_date,open,high,low,close,price_avg,change,pct_change,vol,amount,oi,settle_vol,settle_dire",
        )
        for row in fetched:
            close = finite_float(row.get("close"))
            trade_date = from_tushare_date(row.get("trade_date"))
            if not trade_date or close is None or close <= 0:
                continue
            rows.append(
                price_row(
                    target_symbol,
                    trade_date,
                    close,
                    currency,
                    "tushare:sge_daily:Au99.99",
                    open_=finite_float(row.get("open")),
                    high=finite_float(row.get("high")),
                    low=finite_float(row.get("low")),
                    volume=finite_float(row.get("vol")) or 0.0,
                    amount=finite_float(row.get("amount")) or 0.0,
                )
            )
    rows = merge_rows_by_trade_date(rows, [])
    if not rows:
        raise SyncWarning("Tushare returned no sge_daily rows for Au99.99")
    return rows


def eastmoney_secid(symbol: str) -> str:
    code = symbol.split(".")[0]
    suffix = symbol.split(".")[-1].upper() if "." in symbol else ""
    if suffix == "HK":
        return f"116.{code.zfill(5)}"
    market = "1" if suffix == "SH" or code.startswith(("5", "6")) else "0"
    return f"{market}.{code}"


def yahoo_hk_symbol(symbol: str) -> str:
    if symbol.upper().endswith(".HK"):
        code = symbol.split(".")[0].lstrip("0") or "0"
        return f"{code}.HK"
    return symbol


def yahoo_cn_symbol(symbol: str) -> str:
    code = symbol.split(".")[0]
    suffix = symbol.split(".")[-1].upper() if "." in symbol else ""
    if suffix == "SH":
        return f"{code}.SS"
    if suffix == "SZ":
        return f"{code}.SZ"
    return symbol


def restore_symbol(rows: list[dict[str, Any]], symbol: str) -> list[dict[str, Any]]:
    for row in rows:
        row["symbol"] = symbol
    return rows


def fetch_eastmoney_prices(symbol: str, start: str, end: str, currency: str, source_name: str) -> list[dict[str, Any]]:
    url = (
        "http://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={eastmoney_secid(symbol)}&klt=101&fqt=0&fields1=f1&fields2=f51,f52,f53,f54,f55,f56,f57&"
        f"beg={tushare_date(start)}&end={tushare_date(end)}"
    )
    try:
        body = json.loads(fetch_text(url))
    except (json.JSONDecodeError, SyncWarning) as exc:
        raise SyncWarning(f"Eastmoney price fetch failed for {symbol}: {exc}") from exc
    klines = (body.get("data") or {}).get("klines") or []
    rows: list[dict[str, Any]] = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        close = float(parts[2])
        rows.append(
            {
                "symbol": symbol,
                "trade_date": parts[0],
                "open": float(parts[1]),
                "high": float(parts[3]),
                "low": float(parts[4]),
                "close": close,
                "adj_close": close,
                "volume": float(parts[5] or 0),
                "amount": float(parts[6] or 0),
                "currency": currency,
                "source": source_name,
            }
        )
    if not rows:
        raise SyncWarning(f"Eastmoney returned no price rows for {symbol}")
    return rows


def iso_date_text(value: Any) -> str:
    if hasattr(value, "date") and not isinstance(value, str):
        value = value.date()
    return parse_date(str(value)[:10]).isoformat()


def finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def price_row(
    symbol: str,
    trade_date: str,
    close: float,
    currency: str,
    source: str,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 0.0,
    amount: float = 0.0,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "open": open_ if open_ is not None else close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close,
        "adj_close": close,
        "volume": volume,
        "amount": amount,
        "currency": currency,
        "source": source,
    }


def parse_eastmoney_net_worth_trend(
    text: str,
    proxy_symbol: str,
    target_symbol: str,
    start: str,
    end: str,
    currency: str,
) -> list[dict[str, Any]]:
    match = re.search(r"var\s+Data_netWorthTrend\s*=\s*(\[.*?\]);", text, re.DOTALL)
    if not match:
        raise SyncWarning(f"Eastmoney fund NAV payload missing Data_netWorthTrend for {proxy_symbol}")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise SyncWarning(f"Eastmoney fund NAV payload invalid JSON for {proxy_symbol}") from exc
    start_date = parse_date(start)
    end_date = parse_date(end)
    rows: list[dict[str, Any]] = []
    for item in payload:
        timestamp_ms = item.get("x")
        nav = finite_float(item.get("y"))
        if timestamp_ms is None or nav is None or nav <= 0:
            continue
        trade_date = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=ZoneInfo("Asia/Shanghai")).date().isoformat()
        day = parse_date(trade_date)
        if not (start_date <= day <= end_date):
            continue
        rows.append(price_row(target_symbol, trade_date, nav, currency, f"eastmoney:fund_nav:{proxy_symbol}"))
    if not rows:
        raise SyncWarning(f"Eastmoney returned no fund NAV rows for {proxy_symbol}")
    return rows


def fetch_eastmoney_fund_nav_proxy_prices(proxy_symbol: str, target_symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    url = f"https://fund.eastmoney.com/pingzhongdata/{proxy_symbol}.js"
    try:
        text = fetch_text(url, timeout=20, referer=f"https://fund.eastmoney.com/{proxy_symbol}.html")
    except SyncWarning as exc:
        raise SyncWarning(f"Eastmoney fund NAV fetch failed for {proxy_symbol}: {exc}") from exc
    return parse_eastmoney_net_worth_trend(text, proxy_symbol, target_symbol, start, end, currency)


def fetch_akshare_fund_nav_proxy_prices(proxy_symbol: str, target_symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    try:
        import akshare as ak  # type: ignore
    except ImportError as exc:
        raise SyncWarning("akshare is not installed; open fund NAV fallback unavailable") from exc
    try:
        df = ak.fund_open_fund_info_em(symbol=proxy_symbol)
    except Exception as exc:
        raise SyncWarning(f"AKShare open fund NAV fetch failed for {proxy_symbol}: {exc}") from exc

    start_date = parse_date(start)
    end_date = parse_date(end)
    rows: list[dict[str, Any]] = []
    for item in df.itertuples(index=False, name=None):
        if len(item) < 2:
            continue
        trade_date = iso_date_text(item[0])
        day = parse_date(trade_date)
        if not (start_date <= day <= end_date):
            continue
        nav = finite_float(item[1])
        if nav is None or nav <= 0:
            continue
        rows.append(price_row(target_symbol, trade_date, nav, currency, f"akshare:fund_open_fund_info_em:{proxy_symbol}"))
    if not rows:
        raise SyncWarning(f"AKShare returned no open fund NAV rows for {proxy_symbol}")
    return rows


def fetch_fund_nav_proxy_prices(proxy_symbol: str, target_symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    warnings: list[str] = []
    for fetcher in (fetch_eastmoney_fund_nav_proxy_prices, fetch_akshare_fund_nav_proxy_prices):
        try:
            return fetcher(proxy_symbol, target_symbol, start, end, currency)
        except SyncWarning as exc:
            warnings.append(str(exc))
    raise SyncWarning("; ".join(warnings))


def fetch_sge_au9999_spot_prices(target_symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    try:
        import akshare as ak  # type: ignore
    except ImportError as exc:
        raise SyncWarning("akshare is not installed; SGE Au99.99 fallback unavailable") from exc
    try:
        df = ak.spot_hist_sge(symbol="Au99.99")
    except Exception as exc:
        raise SyncWarning(f"AKShare SGE Au99.99 spot fetch failed: {exc}") from exc

    start_date = parse_date(start)
    end_date = parse_date(end)
    rows: list[dict[str, Any]] = []
    for item in df.itertuples(index=False, name=None):
        if len(item) < 5:
            continue
        trade_date = iso_date_text(item[0])
        day = parse_date(trade_date)
        if not (start_date <= day <= end_date):
            continue
        open_, close, low, high = (finite_float(item[idx]) for idx in (1, 2, 3, 4))
        if close is None or close <= 0:
            continue
        rows.append(
            price_row(
                target_symbol,
                trade_date,
                close,
                currency,
                "akshare:spot_hist_sge:Au99.99",
                open_=open_,
                high=high,
                low=low,
            )
        )
    if not rows:
        raise SyncWarning("AKShare returned no SGE Au99.99 spot rows")
    return rows


def fetch_sge_au9999_report_prices(target_symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    try:
        import akshare as ak  # type: ignore
    except ImportError as exc:
        raise SyncWarning("akshare is not installed; SGE Au99.99 report fallback unavailable") from exc
    try:
        df = ak.macro_china_au_report()
    except Exception as exc:
        raise SyncWarning(f"AKShare SGE Au99.99 report fetch failed: {exc}") from exc

    start_date = parse_date(start)
    end_date = parse_date(end)
    rows: list[dict[str, Any]] = []
    for item in df.itertuples(index=False, name=None):
        if len(item) < 6 or str(item[1]) != "Au99.99":
            continue
        trade_date = iso_date_text(item[0])
        day = parse_date(trade_date)
        if not (start_date <= day <= end_date):
            continue
        open_, high, low, close = (finite_float(item[idx]) for idx in (2, 3, 4, 5))
        if close is None or close <= 0:
            continue
        rows.append(
            price_row(
                target_symbol,
                trade_date,
                close,
                currency,
                "akshare:macro_china_au_report:Au99.99",
                open_=open_,
                high=high,
                low=low,
                volume=finite_float(item[9]) or 0.0 if len(item) > 9 else 0.0,
                amount=finite_float(item[10]) or 0.0 if len(item) > 10 else 0.0,
            )
        )
    if not rows:
        raise SyncWarning("AKShare returned no SGE Au99.99 report rows")
    return rows


def fetch_au9999_proxy_prices(
    target_symbol: str,
    start: str,
    end: str,
    currency: str,
    token: str = "",
) -> list[dict[str, Any]]:
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    if token:
        try:
            rows = fetch_tushare_sge_au9999_prices(token, target_symbol, start, end, currency)
        except SyncWarning as exc:
            warnings.append(str(exc))
    if rows:
        return rows
    try:
        rows = fetch_sge_au9999_spot_prices(target_symbol, start, end, currency)
    except SyncWarning as exc:
        warnings.append(str(exc))
    expected_dates = {day.isoformat() for day in business_days(start, end)}
    if expected_dates - {row["trade_date"] for row in rows}:
        try:
            report_rows = fetch_sge_au9999_report_prices(target_symbol, start, end, currency)
            rows = merge_rows_by_trade_date(rows, report_rows)
        except SyncWarning as exc:
            warnings.append(str(exc))
    if expected_dates - {row["trade_date"] for row in rows}:
        try:
            public_rows = fetch_yahoo_gold_cny_proxy_prices(target_symbol, start, end, currency)
            rows = merge_rows_by_trade_date(rows, public_rows)
        except SyncWarning as exc:
            warnings.append(str(exc))
    if not rows:
        raise SyncWarning("; ".join(warnings) or "no Au99.99 fallback rows")
    return rows


def fetch_yahoo_gold_cny_proxy_prices(
    target_symbol: str,
    start: str,
    end: str,
    currency: str,
) -> list[dict[str, Any]]:
    """Build a keyless CNY/gram gold proxy from public USD gold and FX bars."""
    if currency != "CNY":
        raise SyncWarning(f"gold CNY proxy does not support {currency}")
    fx_start = (parse_date(start) - timedelta(days=10)).isoformat()
    gold_rows = fetch_yahoo_prices("GC=F", start, end, "USD")
    fx_rows = fetch_yahoo_fx_rates(fx_start, end, "USD/CNY")
    ordered_fx = sorted(
        (parse_date(row["trade_date"]), float(row["rate"]))
        for row in fx_rows
        if finite_float(row.get("rate")) is not None and float(row["rate"]) > 0
    )
    if not ordered_fx:
        raise SyncWarning("Yahoo returned no usable USD/CNY rows for gold conversion")

    result: list[dict[str, Any]] = []
    fx_index = 0
    latest_rate: float | None = None
    for gold_row in sorted(gold_rows, key=lambda row: row["trade_date"]):
        trade_day = parse_date(gold_row["trade_date"])
        while fx_index < len(ordered_fx) and ordered_fx[fx_index][0] <= trade_day:
            latest_rate = ordered_fx[fx_index][1]
            fx_index += 1
        close = finite_float(gold_row.get("close"))
        if latest_rate is None or close is None or close <= 0:
            continue

        def cny_per_gram(field: str) -> float:
            value = finite_float(gold_row.get(field)) or close
            return value * latest_rate / TROY_OUNCE_GRAMS

        result.append(
            price_row(
                target_symbol,
                trade_day.isoformat(),
                cny_per_gram("close"),
                currency,
                "yahoo:GC=F+CNY=X:synthetic_cny_per_gram",
                open_=cny_per_gram("open"),
                high=cny_per_gram("high"),
                low=cny_per_gram("low"),
                volume=finite_float(gold_row.get("volume")) or 0.0,
            )
        )
    if not result:
        raise SyncWarning("Yahoo gold and USD/CNY histories have no overlapping rows")
    return result


def price_scale_from_overlap(target_rows: list[dict[str, Any]], fallback_rows: list[dict[str, Any]]) -> float | None:
    target_by_date = {
        row["trade_date"]: float(row["close"])
        for row in target_rows
        if (
            row.get("close") is not None
            and not str(row.get("source", "")).startswith("generated:")
            and ":splice_scale_" not in str(row.get("source", ""))
            and str(row.get("source", "")) not in INDEX_PROXY_PRICE_SOURCES
        )
    }
    fallback_by_date = {
        row["trade_date"]: float(row["close"])
        for row in fallback_rows
        if row.get("close") is not None
    }
    for trade_date in sorted(set(target_by_date) & set(fallback_by_date)):
        fallback_close = fallback_by_date[trade_date]
        target_close = target_by_date[trade_date]
        if fallback_close > 0 and target_close > 0:
            return target_close / fallback_close
    return None


def scale_price_rows(rows: list[dict[str, Any]], scale: float, source_suffix: str) -> list[dict[str, Any]]:
    scaled_rows: list[dict[str, Any]] = []
    for row in rows:
        scaled = dict(row)
        for key in ("open", "high", "low", "close", "adj_close"):
            if scaled.get(key) is not None:
                scaled[key] = float(scaled[key]) * scale
        scaled["source"] = f"{scaled['source']}:{source_suffix}"
        scaled_rows.append(scaled)
    return scaled_rows


def fetch_price_fallback_rows(
    asset: dict[str, Any],
    range_start: str,
    range_end: str,
    target_rows: list[dict[str, Any]],
    token: str = "",
) -> list[dict[str, Any]]:
    fallback = asset.get("price_fallback")
    if not isinstance(fallback, dict):
        return []
    symbol = asset["symbol"]
    currency = asset.get("currency", "CNY")
    anchor_days = int(fallback.get("anchor_window_days", 370))
    fetch_start = (parse_date(range_start) - timedelta(days=anchor_days)).isoformat()
    fetch_end = (parse_date(range_end) + timedelta(days=anchor_days)).isoformat()
    kind = fallback.get("kind")
    if kind == "open_fund_nav":
        fallback_rows = fetch_fund_nav_proxy_prices(str(fallback["symbol"]), symbol, fetch_start, fetch_end, currency)
    elif kind == "sge_au9999":
        fallback_rows = fetch_au9999_proxy_prices(symbol, fetch_start, fetch_end, currency, token=token)
    elif kind == "chinabond_30y_yield_total_return":
        fallback_rows = fetch_chinabond_30y_modeled_prices(asset, symbol, fetch_start, fetch_end, currency)
    elif kind == "index":
        fallback_rows = fetch_index_proxy_prices(
            token,
            str(fallback["symbol"]),
            symbol,
            fetch_start,
            fetch_end,
            currency,
        )
    else:
        raise SyncWarning(f"unsupported price fallback kind for {symbol}: {kind}")

    if fallback.get("scale_mode") == "fixed":
        scale = float(fallback.get("price_scale", 1.0))
        fallback_rows = scale_price_rows(fallback_rows, scale, f"fixed_scale_{scale:g}")
    elif fallback.get("scale_mode") == "splice":
        scale = price_scale_from_overlap(target_rows, fallback_rows)
        if scale is None:
            raise SyncWarning(f"index proxy for {symbol} has no genuine ETF price anchor")
        fallback_rows = scale_price_rows(fallback_rows, scale, f"splice_scale_{scale:.8g}")
    start_date = parse_date(range_start)
    end_date = parse_date(range_end)
    return [
        row
        for row in fallback_rows
        if start_date <= parse_date(row["trade_date"]) <= end_date
    ]


def fetch_hk_yahoo_prices(symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    return restore_symbol(fetch_yahoo_prices(yahoo_hk_symbol(symbol), start, end, currency), symbol)


def fetch_cn_yahoo_prices(symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    return restore_symbol(fetch_yahoo_prices(yahoo_cn_symbol(symbol), start, end, currency), symbol)


def fetch_hk_yahoo_dividends(symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    return restore_symbol(fetch_yahoo_dividends(yahoo_hk_symbol(symbol), start, end, currency), symbol)


def tencent_hk_symbol(symbol: str) -> str:
    match = re.match(r"^0*(\d+)\.HK$", symbol, re.IGNORECASE)
    if not match:
        raise SyncWarning(f"Tencent HK source only supports .HK numeric symbols, got {symbol}")
    return f"hk{int(match.group(1)):05d}"


def fetch_tencent_hk_prices(symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    tencent_symbol = tencent_hk_symbol(symbol)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={tencent_symbol},day,{start},{end},800,qfq"
    try:
        body = json.loads(fetch_text(url, timeout=20, referer=f"https://gu.qq.com/{tencent_symbol}/gp"))
    except (json.JSONDecodeError, SyncWarning) as exc:
        raise SyncWarning(f"Tencent HK price fetch failed for {symbol}: {exc}") from exc
    rows = (((body.get("data") or {}).get(tencent_symbol) or {}).get("day") or [])
    result: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 6:
            continue
        trade_date, open_, close, high, low, volume = row[:6]
        result.append(
            {
                "symbol": symbol,
                "trade_date": trade_date,
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "adj_close": float(close),
                "volume": float(volume or 0),
                "amount": 0.0,
                "currency": currency,
                "source": "tencent:hk_qfq",
            }
        )
    if not result:
        raise SyncWarning(f"Tencent returned no HK price rows for {symbol}")
    return result


def sohu_code_and_referer(symbol: str) -> tuple[str, str]:
    code = symbol.split(".")[0]
    if symbol.upper() in {"000300.SH", "000852.SH", "000903.SH", "000905.SH"}:
        return f"zs_{code}", f"https://q.stock.sohu.com/zs/{code}/lshq.shtml"
    return f"cn_{code}", f"https://q.stock.sohu.com/cn/{code}/lshq.shtml"


def fetch_sohu_prices(symbol: str, start: str, end: str, currency: str, source_name: str) -> list[dict[str, Any]]:
    sohu_code, referer = sohu_code_and_referer(symbol)
    rows_by_date: dict[str, dict[str, Any]] = {}
    for chunk_start, chunk_end in chunk_date_ranges(start, end, 90):
        url = (
            "https://q.stock.sohu.com/hisHq?"
            f"code={sohu_code}&start={tushare_date(chunk_start)}&end={tushare_date(chunk_end)}"
            "&stat=1&order=D&period=d&callback=historySearchHandler&rt=jsonp"
        )
        try:
            data = fetch_sohu_jsonp_blocks(url, referer)
        except SyncWarning:
            continue
        for block in data:
            for item in block.get("hq") or []:
                if len(item) < 9:
                    continue
                trade_date = item[0]
                close = float(item[2])
                rows_by_date[trade_date] = {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "open": float(item[1]),
                    "high": float(item[6]),
                    "low": float(item[5]),
                    "close": close,
                    "adj_close": close,
                    "volume": float(item[7] or 0),
                    "amount": float(item[8] or 0) * 10000.0,
                    "currency": currency,
                    "source": source_name,
                }
    rows = [rows_by_date[key] for key in sorted(rows_by_date)]
    if not rows:
        raise SyncWarning(f"Sohu returned no price rows for {symbol}")
    return rows


def fetch_index_prices(token: str, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    rows_by_date: dict[str, dict[str, Any]] = {}
    # index_daily can silently cap a long response. Keep each request below
    # that cap so a historical proxy does not acquire artificial gaps.
    for chunk_start, chunk_end in chunk_date_ranges(start, end, 90):
        for row in tushare_call(
            token,
            "index_daily",
            {"ts_code": symbol, "start_date": tushare_date(chunk_start), "end_date": tushare_date(chunk_end)},
            "ts_code,trade_date,open,high,low,close,vol,amount",
        ):
            rows_by_date[str(row["trade_date"])] = row
    rows = list(rows_by_date.values())
    if not rows:
        raise SyncWarning(f"Tushare returned no index_daily rows for {symbol}")
    return [
        {
            "symbol": row["ts_code"],
            "trade_date": from_tushare_date(row["trade_date"]),
            "open": float(row.get("open") or row["close"]),
            "high": float(row.get("high") or row["close"]),
            "low": float(row.get("low") or row["close"]),
            "close": float(row["close"]),
            "adj_close": None,
            "volume": float(row.get("vol") or 0),
            "amount": float(row.get("amount") or 0),
            "currency": "CNY",
            "source": "tushare:index_daily",
        }
        for row in rows
    ]


def fetch_csindex_prices(
    proxy_symbol: str,
    target_symbol: str,
    start: str,
    end: str,
    currency: str,
) -> list[dict[str, Any]]:
    """Fetch an official CSI index performance series, including total-return indices."""
    index_code = str(proxy_symbol).split(".", 1)[0].upper()
    if not re.fullmatch(r"[A-Z]\d{5}", index_code):
        raise SyncWarning(f"CSI index source does not support {proxy_symbol}")

    rows_by_date: dict[str, dict[str, Any]] = {}
    referer = f"https://www.csindex.com.cn/#/indices/family/detail?indexCode={index_code}"
    for chunk_index, (chunk_start, chunk_end) in enumerate(chunk_date_ranges(start, end, 1825)):
        # The official endpoint occasionally omits the exact end-date row of a
        # long request. Overlap adjacent chunks so boundary trading days cannot
        # disappear; rows_by_date removes the duplicates.
        if chunk_index:
            chunk_start = (parse_date(chunk_start) - timedelta(days=7)).isoformat()
        url = (
            "https://www.csindex.com.cn/csindex-home/perf/index-perf?"
            f"indexCode={index_code}&startDate={tushare_date(chunk_start)}&endDate={tushare_date(chunk_end)}"
        )
        body: dict[str, Any] | None = None
        errors: list[str] = []
        for attempt in range(2):
            try:
                candidate = json.loads(
                    fetch_text(url, timeout=CSINDEX_TOTAL_RETURN_TIMEOUT_SECONDS, referer=referer)
                )
                if str(candidate.get("code")) != "200" or candidate.get("success") is False:
                    raise SyncWarning(
                        f"response failed: {candidate.get('msg') or candidate.get('code')}"
                    )
                body = candidate
                break
            except (json.JSONDecodeError, SyncWarning) as exc:
                errors.append(str(exc))
                if attempt == 0:
                    time.sleep(0.5)
        if body is None:
            raise SyncWarning(
                f"CSI official index fetch failed for {index_code}: {'; '.join(errors)}"
            )
        for item in body.get("data") or []:
            close = finite_float(item.get("close"))
            if close is None or close <= 0 or not item.get("tradeDate"):
                continue
            trade_date = from_tushare_date(str(item["tradeDate"]))
            rows_by_date[trade_date] = price_row(
                target_symbol,
                trade_date,
                close,
                currency,
                "csindex:index_perf",
                open_=finite_float(item.get("open")),
                high=finite_float(item.get("high")),
                low=finite_float(item.get("low")),
                volume=finite_float(item.get("tradingVol")) or 0.0,
                amount=finite_float(item.get("tradingValue")) or 0.0,
            )
    rows = [rows_by_date[key] for key in sorted(rows_by_date)]
    if not rows:
        raise SyncWarning(f"CSI official index source returned no rows for {index_code}")
    return rows


def fetch_index_proxy_prices(
    token: str,
    proxy_symbol: str,
    target_symbol: str,
    start: str,
    end: str,
    currency: str,
) -> list[dict[str, Any]]:
    """Fetch an index proxy from available sources, preferring its authoritative source per date."""
    attempts: list[str] = []
    fetchers = []
    is_official_csi_symbol = str(proxy_symbol).upper().endswith(".CSI")
    if is_official_csi_symbol:
        fetchers.append(
            (
                "csindex:index_perf",
                lambda: fetch_csindex_prices(proxy_symbol, target_symbol, start, end, currency),
            )
        )
    elif token:
        fetchers.append(("tushare:index_daily", lambda: fetch_index_prices(token, proxy_symbol, start, end)))
    if not is_official_csi_symbol:
        fetchers.extend(
            [
                ("datasrc:index", lambda: fetch_datasrc_market_prices(proxy_symbol, start, end, currency)),
                ("sohu:index_kline", lambda: fetch_sohu_prices(proxy_symbol, start, end, currency, "sohu:index_kline")),
                ("eastmoney:index_kline", lambda: fetch_eastmoney_prices(proxy_symbol, start, end, currency, "eastmoney:index_kline")),
            ]
        )
    selected_rows: list[dict[str, Any]] = []
    for source_name, fetcher in fetchers:
        try:
            rows = fetcher()
        except SyncWarning as exc:
            attempts.append(f"{source_name}: {exc}")
            continue
        if rows:
            # Fetchers are ordered by priority, so earlier rows stay selected
            # while later sources fill only the dates they do not provide.
            # Historical public endpoints often return only part of a long
            # requested range.
            selected_rows = merge_rows_by_trade_date(selected_rows, rows)
    if selected_rows:
        return restore_symbol(selected_rows, target_symbol)
    detail = "; ".join(attempts) or "no index price source configured"
    raise SyncWarning(f"index proxy fetch failed for {proxy_symbol}: {detail}")


def fetch_chinabond_index_prices(asset: dict[str, Any], start: str, end: str) -> list[dict[str, Any]]:
    """Fetch an official ChinaBond total-return index series for a configured asset."""
    index_id = str(asset.get("index_id") or "").strip()
    if not index_id:
        raise SyncWarning(f"ChinaBond index id is not configured for {asset['symbol']}")
    url = (
        "https://yield.chinabond.com.cn/cbweb-mn/indices/singleIndexQueryResult?"
        f"indexid={index_id}&qxlxt=00&ltcslx=&zslxt=CFZS,XQJSL&zslxt1=&lx=1&locale=zh_CN"
    )
    cmd = [
        curl_executable(), "-sS", "-L", "-X", "POST", "-A", "Mozilla/5.0",
        # The official endpoint returns the entire total-return history, even
        # for a small requested range.  The general eight-second market-data
        # timeout (and the former 30-second local override) cuts off valid
        # multi-thousand-row responses under concurrent automatic syncs.
        # Bypass an operator proxy as well: it is particularly prone to
        # truncating these large JSON responses.
        "--noproxy", "*",
        "--connect-timeout", str(CHINABOND_CONNECT_TIMEOUT_SECONDS),
        "--max-time", str(CHINABOND_TOTAL_RETURN_TIMEOUT_SECONDS),
        "--retry", str(CHINABOND_RETRY_COUNT),
        "--retry-all-errors",
        "--retry-delay", "1",
        url,
    ]
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=(CHINABOND_TOTAL_RETURN_TIMEOUT_SECONDS + 1) * (CHINABOND_RETRY_COUNT + 1) + 5,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise SyncWarning(f"exit {completed.returncode}: {detail}")
        payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, SyncWarning) as exc:
        raise SyncWarning(f"ChinaBond index fetch failed for {asset['symbol']}: {exc}") from exc
    values = payload.get("CFZS_00")
    if not isinstance(values, dict):
        raise SyncWarning(f"ChinaBond total-return series is invalid for {asset['symbol']}")
    start_date = parse_date(start)
    end_date = parse_date(end)
    rows: list[dict[str, Any]] = []
    for timestamp_ms, value in values.items():
        try:
            trade_date = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=ZoneInfo("Asia/Shanghai")).date()
            close = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if close > 0 and start_date <= trade_date <= end_date:
            rows.append(price_row(asset["symbol"], trade_date.isoformat(), close, "CNY", "chinabond:index_total_return"))
    if not rows:
        raise SyncWarning(f"ChinaBond returned no index rows for {asset['symbol']}")
    return rows


def fetch_chinabond_30y_yields(start: str, end: str) -> dict[str, float]:
    """Fetch the Ministry of Finance/ChinaBond 30-year CGB yield curve.

    The public history endpoint limits each query to less than one year, so a
    long pre-index backtest is split into bounded requests and merged by date.
    Returned yields are decimals (for example, 0.0482 for 4.82%).
    """
    yields: dict[str, float] = {}
    for chunk_start, chunk_end in chunk_date_ranges(start, end, 360):
        url = (
            "https://yield.chinabond.com.cn/cbweb-mn/pgxh/historyQuery?"
            f"startDate={chunk_start}&endDate={chunk_end}&gjqx=30&locale=zh_CN"
        )
        cmd = [
            curl_executable(),
            "-sS",
            "-L",
            "-X",
            "POST",
            "-A",
            "Mozilla/5.0",
            "--noproxy",
            "*",
            "--connect-timeout",
            str(CHINABOND_CONNECT_TIMEOUT_SECONDS),
            "--max-time",
            str(CHINABOND_YIELD_TIMEOUT_SECONDS),
            "--retry",
            str(CHINABOND_RETRY_COUNT),
            "--retry-all-errors",
            "--retry-delay",
            "1",
            url,
        ]
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=(CHINABOND_YIELD_TIMEOUT_SECONDS + 1) * (CHINABOND_RETRY_COUNT + 1) + 5,
            )
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace").strip()
                raise SyncWarning(f"exit {completed.returncode}: {detail}")
            payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, SyncWarning) as exc:
            raise SyncWarning(f"ChinaBond 30-year yield fetch failed: {exc}") from exc
        if not isinstance(payload, list):
            raise SyncWarning("ChinaBond 30-year yield series is invalid")
        for item in payload:
            if not isinstance(item, dict):
                continue
            trade_date = str(item.get("workTime") or "")
            yield_percent = finite_float(item.get("thirtyYear"))
            if not trade_date or yield_percent is None or yield_percent <= 0:
                continue
            yields[trade_date] = yield_percent / 100.0
    if not yields:
        raise SyncWarning("ChinaBond returned no 30-year yield rows")
    return yields


def thirty_year_par_bond_price(yield_rate: float, coupon_rate: float) -> float:
    """Price a 30-year semiannual par bond per CNY 100 face value."""
    periods = 60
    discount = 1.0 + yield_rate / 2.0
    coupon = 100.0 * coupon_rate / 2.0
    return sum(coupon / discount**period for period in range(1, periods)) + (100.0 + coupon) / discount**periods


def model_chinabond_30y_total_return_rows(
    target_symbol: str,
    yields: dict[str, float],
    currency: str = "CNY",
) -> list[dict[str, Any]]:
    """Build a constant-maturity 30-year total-return proxy from official yields.

    Each interval reprices the previous day's par 30-year bond at the current
    yield and adds the elapsed coupon carry.  The resulting daily return series
    closely tracks the later official 30-year total-return index while retaining
    a real 30-year duration exposure before that index begins.
    """
    ordered = sorted(
        (parse_date(trade_date), float(yield_rate))
        for trade_date, yield_rate in yields.items()
        if yield_rate and math.isfinite(float(yield_rate)) and float(yield_rate) > 0
    )
    if not ordered:
        return []
    level = 100.0
    rows = [
        price_row(
            target_symbol,
            ordered[0][0].isoformat(),
            level,
            currency,
            "chinabond:30y_yield_curve:modeled_total_return",
        )
    ]
    previous_day, previous_yield = ordered[0]
    for current_day, current_yield in ordered[1:]:
        elapsed_days = (current_day - previous_day).days
        if elapsed_days <= 0:
            continue
        repriced = thirty_year_par_bond_price(current_yield, previous_yield)
        holding_factor = repriced / 100.0 * (1.0 + previous_yield * elapsed_days / 365.0)
        if not math.isfinite(holding_factor) or holding_factor <= 0:
            continue
        level *= holding_factor
        rows.append(
            price_row(
                target_symbol,
                current_day.isoformat(),
                level,
                currency,
                "chinabond:30y_yield_curve:modeled_total_return",
            )
        )
        previous_day = current_day
        previous_yield = current_yield
    return rows


def fetch_chinabond_30y_modeled_prices(
    asset: dict[str, Any],
    target_symbol: str,
    start: str,
    end: str,
    currency: str,
) -> list[dict[str, Any]]:
    """Return an official-yield-based proxy spliced to the official 30-year index."""
    primary_start = parse_date(asset.get("inception_date") or "2011-01-04")
    anchor_end = primary_start + timedelta(days=370)
    curve_end = max(parse_date(end), anchor_end)
    yields = fetch_chinabond_30y_yields(start, curve_end.isoformat())
    modeled_rows = model_chinabond_30y_total_return_rows(target_symbol, yields, currency)
    official_rows = fetch_chinabond_index_prices(asset, primary_start.isoformat(), anchor_end.isoformat())
    scale = price_scale_from_overlap(official_rows, modeled_rows)
    if scale is None:
        raise SyncWarning("modeled 30-year Treasury history has no official index splice anchor")
    scaled_rows = scale_price_rows(modeled_rows, scale, f"splice_scale_{scale:.8g}")
    start_date = parse_date(start)
    end_date = parse_date(end)
    return [row for row in scaled_rows if start_date <= parse_date(row["trade_date"]) <= end_date]


def fetch_fund_dividends(token: str, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    rows = tushare_call(
        token,
        "fund_div",
        {"ts_code": symbol},
        "ts_code,ann_date,record_date,ex_date,pay_date,div_cash",
    )
    start_date = parse_date(start)
    end_date = parse_date(end)
    dividends: list[dict[str, Any]] = []
    unresolved_pay_dates = 0
    for row in rows:
        ex_date = from_tushare_date(row.get("ex_date"))
        if not ex_date:
            continue
        ex = parse_date(ex_date)
        if not (start_date <= ex <= end_date):
            continue
        pay_date = from_tushare_date(row.get("pay_date"))
        if not pay_date:
            unresolved_pay_dates += 1
            continue
        dividends.append(
            {
                "symbol": row["ts_code"],
                "ann_date": from_tushare_date(row.get("ann_date")),
                "record_date": from_tushare_date(row.get("record_date")),
                "ex_date": ex_date,
                "pay_date": pay_date,
                "div_cash": float(row.get("div_cash") or 0),
                "currency": "CNY",
                "source": "tushare:fund_div",
            }
        )
    if unresolved_pay_dates:
        raise SyncWarning(f"Tushare dividend data has no pay date for {symbol}")
    return dividends


def _eastmoney_cell_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(value))).strip()


def parse_eastmoney_fund_dividends(symbol: str, text: str, start: str, end: str) -> list[dict[str, Any]]:
    """Parse Eastmoney's distribution table without treating the ex-date as the pay date."""
    start_date = parse_date(start)
    end_date = parse_date(end)
    decoded = html.unescape(text).replace(r"\/", "/").replace(r'\"', '"')
    rows: list[dict[str, Any]] = []
    incomplete_rows = 0
    for table_row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", decoded, flags=re.IGNORECASE | re.DOTALL):
        cells = [_eastmoney_cell_text(cell) for cell in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", table_row, flags=re.IGNORECASE | re.DOTALL)]
        if not cells:
            continue
        row_text = " ".join(cells)
        amount_match = re.search(r"每\s*(?:10|十)\s*份.{0,40}?([0-9]+(?:\.[0-9]+)?)\s*元", row_text)
        if not amount_match:
            continue
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", row_text)
        if len(dates) < 3:
            incomplete_rows += 1
            continue
        record_date, ex_date, pay_date = dates[-3:]
        ex = parse_date(ex_date)
        if not (start_date <= ex <= end_date):
            continue
        rows.append(
            {
                "symbol": symbol,
                "ann_date": None,
                "record_date": record_date,
                "ex_date": ex_date,
                "pay_date": pay_date,
                "div_cash": float(amount_match.group(1)) / 10.0,
                "currency": "CNY",
                "source": "eastmoney:fund_dividend",
            }
        )
    if incomplete_rows:
        raise SyncWarning(f"Eastmoney dividend data has no pay date for {symbol}")
    return rows


def fetch_eastmoney_fund_dividends(symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    code = symbol.split(".")[0]
    url = f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjfh&code={code}&page=1&per=200"
    try:
        text = fetch_text(url, timeout=20, referer=f"https://fundf10.eastmoney.com/fhsp_{code}.html")
    except SyncWarning as exc:
        raise SyncWarning(f"Eastmoney dividend fetch failed for {symbol}: {exc}") from exc
    if text.strip() == "var apidata=":
        return []
    rows = parse_eastmoney_fund_dividends(symbol, text, start, end)
    if rows:
        return rows
    if "content" in text or "<table" in text:
        return []
    raise SyncWarning(f"Eastmoney returned unrecognized dividend payload for {symbol}")


def parse_sina_etf_cumulative_dividends(
    symbol: str,
    records: list[dict[str, Any]],
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    start_date = parse_date(start)
    end_date = parse_date(end)
    previous_cumulative = 0.0
    rows: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda item: str(item.get("日期") or "")):
        ex_date = str(record.get("日期") or "")[:10]
        if not ex_date:
            continue
        try:
            ex = parse_date(ex_date)
            cumulative = float(record.get("累计分红") or 0.0)
        except (TypeError, ValueError):
            continue
        dividend = cumulative - previous_cumulative
        previous_cumulative = cumulative
        if not (start_date <= ex <= end_date) or dividend <= 0:
            continue
        rows.append(
            {
                "symbol": symbol,
                "ann_date": ex_date,
                "record_date": ex_date,
                "ex_date": ex_date,
                "pay_date": None,
                "div_cash": dividend,
                "currency": "CNY",
                "source": "sina:etf_cumulative_dividend",
            }
        )
    return rows


def fetch_sina_etf_dividends(symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    try:
        import akshare as ak  # type: ignore
    except Exception as exc:
        raise SyncWarning(f"AKShare is unavailable for ETF dividends: {exc}") from exc
    code = symbol.split(".")[0]
    market = "sh" if symbol.upper().endswith(".SH") or code.startswith(("5", "6")) else "sz"
    try:
        frame = ak.fund_etf_dividend_sina(symbol=f"{market}{code}")
    except Exception as exc:
        raise SyncWarning(f"Sina ETF dividend fetch failed for {symbol}: {exc}") from exc
    if frame is None or frame.empty:
        return []
    required_columns = {"日期", "累计分红"}
    if not required_columns.issubset(frame.columns):
        raise SyncWarning(f"Sina returned unrecognized ETF dividend payload for {symbol}")
    rows = parse_sina_etf_cumulative_dividends(symbol, frame.to_dict("records"), start, end)
    if rows:
        raise SyncWarning(f"Sina cumulative dividend data has no pay date for {symbol}")
    return []


def dividend_source_priority(source: str) -> int:
    for prefix, priority in DIVIDEND_SOURCE_PRIORITY.items():
        if source.startswith(prefix):
            return priority
    return 5


def dividend_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row["symbol"]), str(row["ex_date"]), str(row["currency"]))


def prefer_dividend_row(current: dict[str, Any] | None, candidate: dict[str, Any]) -> dict[str, Any]:
    """Pick one distribution per asset/ex-date/currency to prevent cash being counted twice."""
    if current is None:
        return candidate
    if dividend_source_priority(candidate["source"]) <= dividend_source_priority(current["source"]):
        return candidate
    return current


def upsert_dividend_rows(conn, rows: list[dict[str, Any]]) -> int:
    """Replace lower-quality or stale variants of the same cash distribution."""
    preferred: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = dividend_identity(row)
        preferred[key] = prefer_dividend_row(preferred.get(key), row)

    inserted = 0
    for (symbol, ex_date, currency), candidate in preferred.items():
        existing_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT symbol, ann_date, record_date, ex_date, pay_date, div_cash, currency, source "
                "FROM fund_dividends WHERE symbol=? AND ex_date=? AND currency=?",
                (symbol, ex_date, currency),
            )
        ]
        winner: dict[str, Any] | None = None
        for existing in existing_rows:
            winner = prefer_dividend_row(winner, existing)
        winner = prefer_dividend_row(winner, candidate)
        if winner is not candidate:
            continue
        conn.execute(
            "DELETE FROM fund_dividends WHERE symbol=? AND ex_date=? AND currency=?",
            (symbol, ex_date, currency),
        )
        insert_many(conn, "fund_dividends", [candidate])
        inserted += 1
    return inserted


def fetch_adj_factors(token: str, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    rows = tushare_call(
        token,
        "fund_adj",
        {"ts_code": symbol, "start_date": tushare_date(start), "end_date": tushare_date(end)},
        "ts_code,trade_date,adj_factor",
    )
    return [
        {
            "symbol": row["ts_code"],
            "trade_date": from_tushare_date(row["trade_date"]),
            "adj_factor": float(row["adj_factor"]),
            "source": "tushare:fund_adj",
        }
        for row in rows
    ]


def fetch_stooq_prices(symbol: str, start: str, end: str, currency: str = "USD", stooq_symbol: str | None = None, source: str = "stooq") -> list[dict[str, Any]]:
    stooq_symbol = stooq_symbol or symbol.lower()
    if "." not in stooq_symbol:
        stooq_symbol += ".us"
    url = (
        f"https://stooq.com/q/d/l/?s={stooq_symbol}&d1={tushare_date(start)}"
        f"&d2={tushare_date(end)}&i=d"
    )
    try:
        text = fetch_text(url)
    except SyncWarning as exc:
        raise SyncWarning(f"Stooq fetch failed for {symbol}: {exc}") from exc
    rows = list(csv.DictReader(StringIO(text)))
    result: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("Close") or row["Close"].lower() == "null":
            continue
        close = float(row["Close"])
        result.append(
            {
                "symbol": symbol,
                "trade_date": row["Date"],
                "open": float(row.get("Open") or close),
                "high": float(row.get("High") or close),
                "low": float(row.get("Low") or close),
                "close": close,
                "adj_close": close,
                "volume": float(row.get("Volume") or 0),
                "amount": 0.0,
                "currency": currency,
                "source": source,
            }
        )
    if not result:
        raise SyncWarning(f"Stooq returned no rows for {symbol}")
    return result


def stooq_hk_symbol(symbol: str) -> str:
    match = re.match(r"^0*(\d+)\.HK$", symbol, re.IGNORECASE)
    if not match:
        return symbol.lower()
    return f"{int(match.group(1))}.hk"


def fetch_hk_stooq_prices(symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    return fetch_stooq_prices(symbol, start, end, currency, stooq_hk_symbol(symbol), "stooq:hk")


def fetch_hk_yahoo_spark_prices(symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    return restore_symbol(fetch_yahoo_spark_prices(yahoo_hk_symbol(symbol), start, end, currency), symbol)


def parse_market_number(value: Any) -> float:
    cleaned = re.sub(r"[^0-9.\-]", "", str(value or ""))
    if not cleaned:
        return 0.0
    return float(cleaned)


def fetch_nasdaq_prices(symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    start_date = parse_date(start)
    end_date = parse_date(end)
    from_date = previous_weekday(start_date).isoformat() if start_date == end_date else start
    url = (
        f"https://api.nasdaq.com/api/quote/{symbol}/historical?"
        f"assetclass=etf&fromdate={from_date}&todate={end}&limit=9999"
    )
    try:
        body = json.loads(fetch_text(url, timeout=20, referer=f"https://www.nasdaq.com/market-activity/etf/{symbol}/historical"))
    except (json.JSONDecodeError, SyncWarning) as exc:
        raise SyncWarning(f"Nasdaq price fetch failed for {symbol}: {exc}") from exc
    rows = (((body.get("data") or {}).get("tradesTable") or {}).get("rows") or [])
    result: list[dict[str, Any]] = []
    for row in rows:
        close = parse_market_number(row.get("close"))
        if close <= 0:
            continue
        trade_date = datetime.strptime(row["date"], "%m/%d/%Y").date().isoformat()
        if not (start <= trade_date <= end):
            continue
        result.append(
            {
                "symbol": symbol,
                "trade_date": trade_date,
                "open": parse_market_number(row.get("open")) or close,
                "high": parse_market_number(row.get("high")) or close,
                "low": parse_market_number(row.get("low")) or close,
                "close": close,
                "adj_close": close,
                "volume": parse_market_number(row.get("volume")),
                "amount": 0.0,
                "currency": currency,
                "source": "nasdaq:historical",
            }
        )
    result.sort(key=lambda item: item["trade_date"])
    if not result:
        raise SyncWarning(f"Nasdaq returned no price rows for {symbol}")
    return result


def yahoo_period(value: str) -> int:
    dt = datetime.combine(parse_date(value), datetime.min.time(), tzinfo=timezone.utc)
    return int(dt.timestamp())


def fetch_yahoo_chart_prices(symbol: str, start: str, end: str, currency: str, host: str, source: str) -> list[dict[str, Any]]:
    url = (
        f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?period1={yahoo_period(start)}&period2={yahoo_period(end) + 86400}&interval=1d&events=div,splits"
    )
    try:
        body = json.loads(fetch_text(url, timeout=30))
    except (json.JSONDecodeError, SyncWarning) as exc:
        raise SyncWarning(f"Yahoo {host} price fetch failed for {symbol}: {exc}") from exc
    result = ((body.get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise SyncWarning(f"Yahoo {host} returned no price rows for {symbol}")
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    adj = ((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
    rows: list[dict[str, Any]] = []
    for idx, ts in enumerate(timestamps):
        close = (quote.get("close") or [None])[idx]
        if close is None:
            continue
        trade_date = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        rows.append(
            {
                "symbol": symbol,
                "trade_date": trade_date,
                "open": (quote.get("open") or [close])[idx] or close,
                "high": (quote.get("high") or [close])[idx] or close,
                "low": (quote.get("low") or [close])[idx] or close,
                "close": close,
                "adj_close": adj[idx] if idx < len(adj) else close,
                "volume": (quote.get("volume") or [0])[idx] or 0,
                "amount": 0.0,
                "currency": currency,
                "source": source,
            }
        )
    if not rows:
        raise SyncWarning(f"Yahoo {host} returned no usable price rows for {symbol}")
    return rows


def fetch_yahoo_prices(symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    warnings: list[str] = []
    for host, source in (("query1", "yahoo:query1:chart"), ("query2", "yahoo:query2:chart")):
        try:
            return fetch_yahoo_chart_prices(symbol, start, end, currency, host, source)
        except SyncWarning as exc:
            warnings.append(str(exc))
    raise SyncWarning("; ".join(warnings))


def fetch_yahoo_spark_prices(symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    start_date = parse_date(start)
    end_date = parse_date(end)
    if (end_date - start_date).days > 35:
        raise SyncWarning(f"Yahoo spark fallback skipped for {symbol}: requested range is too long")
    warnings: list[str] = []
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/spark?symbols={symbol}&range=1mo&interval=1d"
        try:
            body = json.loads(fetch_text(url, timeout=20))
        except (json.JSONDecodeError, SyncWarning) as exc:
            warnings.append(f"Yahoo spark {host} fetch failed for {symbol}: {exc}")
            continue
        payload = body.get(symbol) or (((body.get("spark") or {}).get("result") or [{}])[0].get("response") or [{}])[0]
        timestamps = payload.get("timestamp") or []
        closes = payload.get("close") or (((payload.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
        rows: list[dict[str, Any]] = []
        for idx, ts in enumerate(timestamps):
            if idx >= len(closes) or closes[idx] is None:
                continue
            trade_date = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            if not (start <= trade_date <= end):
                continue
            close = float(closes[idx])
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "adj_close": close,
                    "volume": 0.0,
                    "amount": 0.0,
                    "currency": currency,
                    "source": f"yahoo:{host}:spark",
                }
            )
        if rows:
            return rows
        warnings.append(f"Yahoo spark {host} returned no usable price rows for {symbol}")
    raise SyncWarning("; ".join(warnings))


def fetch_yahoo_dividends(symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?period1={yahoo_period(start)}&period2={yahoo_period(end) + 86400}&interval=1d&events=div"
    )
    try:
        body = json.loads(fetch_text(url, timeout=30))
    except (json.JSONDecodeError, SyncWarning) as exc:
        raise SyncWarning(f"Yahoo dividend fetch failed for {symbol}: {exc}") from exc
    result = ((body.get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise SyncWarning(f"Yahoo returned no dividend payload for {symbol}")
    dividends = ((result.get("events") or {}).get("dividends") or {}).values()
    rows: list[dict[str, Any]] = []
    start_date = parse_date(start)
    end_date = parse_date(end)
    for event in dividends:
        event_date = event.get("date")
        amount = event.get("amount")
        if event_date is None or amount is None:
            continue
        ex_date = datetime.fromtimestamp(int(event_date), tz=timezone.utc).date()
        if not (start_date <= ex_date <= end_date):
            continue
        rows.append(
            {
                "symbol": symbol,
                "ann_date": ex_date.isoformat(),
                "record_date": ex_date.isoformat(),
                "ex_date": ex_date.isoformat(),
                "pay_date": ex_date.isoformat(),
                "div_cash": float(amount),
                "currency": currency,
                "source": "yahoo:chart:dividend",
            }
        )
    return sorted(rows, key=lambda row: (row["ex_date"], row["div_cash"]))


def fetch_digrin_dividends(symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    url = f"https://www.digrin.com/stocks/detail/{symbol}/"
    try:
        body = fetch_text(url, timeout=20)
    except SyncWarning as exc:
        raise SyncWarning(f"Digrin dividend fetch failed for {symbol}: {exc}") from exc
    start_date = parse_date(start)
    end_date = parse_date(end)
    rows: list[dict[str, Any]] = []
    pattern = re.compile(
        r"<tr>\s*<td>\s*(\d{4}-\d{2}-\d{2})\s*</td>\s*"
        r"<td>\s*(\d{4}-\d{2}-\d{2})\s*</td>\s*"
        r"<td>\s*([0-9][0-9.,]*)\s*([A-Z]{3})",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(body):
        ex_date = match.group(1)
        pay_date = match.group(2)
        ex = parse_date(ex_date)
        if not (start_date <= ex <= end_date):
            continue
        rows.append(
            {
                "symbol": symbol,
                "ann_date": ex_date,
                "record_date": ex_date,
                "ex_date": ex_date,
                "pay_date": pay_date,
                "div_cash": float(match.group(3).replace(",", "")),
                "currency": match.group(4) or currency,
                "source": "digrin:html:dividend",
            }
        )
    return sorted(rows, key=lambda row: (row["ex_date"], row["div_cash"]))


def fetch_stooq_fx_rates(start: str, end: str, pair: str = "USD/CNY") -> list[dict[str, Any]]:
    base, quote = fx_pair_parts(pair)
    stooq_pair = f"{base}{quote}".lower()
    url = f"https://stooq.com/q/d/l/?s={stooq_pair}&d1={tushare_date(start)}&d2={tushare_date(end)}&i=d"
    try:
        text = fetch_text(url)
    except SyncWarning as exc:
        raise SyncWarning(f"Stooq FX fetch failed for {stooq_pair}: {exc}") from exc
    rows = []
    for row in csv.DictReader(StringIO(text)):
        close = row.get("Close")
        if close and close.lower() != "null":
            rows.append({"pair": pair, "trade_date": row["Date"], "rate": float(close), "source": f"stooq:{stooq_pair}"})
    if not rows:
        raise SyncWarning(f"Stooq returned no rows for {stooq_pair}")
    return rows


def yahoo_fx_symbol(pair: str) -> str:
    base, quote = fx_pair_parts(pair)
    if base == "USD" and quote == "CNY":
        return "CNY=X"
    return f"{base}{quote}=X"


def fetch_yahoo_fx_rates(start: str, end: str, pair: str = "USD/CNY") -> list[dict[str, Any]]:
    symbol = yahoo_fx_symbol(pair)
    rows = fetch_yahoo_prices(symbol, start, end, "CNY")
    return [{"pair": pair, "trade_date": row["trade_date"], "rate": row["close"], "source": f"yahoo:{symbol}"} for row in rows]


def fetch_frankfurter_fx_rates(start: str, end: str, pair: str = "USD/CNY") -> list[dict[str, Any]]:
    base, quote = fx_pair_parts(pair)
    url = f"https://api.frankfurter.app/{start}..{end}?from={base}&to={quote}"
    try:
        body = json.loads(fetch_text(url, timeout=10))
    except (json.JSONDecodeError, SyncWarning) as exc:
        raise SyncWarning(f"Frankfurter FX fetch failed for {pair}: {exc}") from exc
    rates = body.get("rates") or {}
    rows = [
        {"pair": pair, "trade_date": trade_date, "rate": float(values[quote]), "source": f"frankfurter:{base}-{quote}"}
        for trade_date, values in sorted(rates.items())
        if values.get(quote) is not None
    ]
    if not rows:
        raise SyncWarning(f"Frankfurter returned no {pair} rows")
    return rows


def fetch_currency_api_fx_rates(start: str, end: str, pair: str = "USD/CNY") -> list[dict[str, Any]]:
    base, quote = fx_pair_parts(pair)
    base_lower = base.lower()
    quote_lower = quote.lower()
    days = business_days(start, end)
    if len(days) > 60:
        raise SyncWarning(f"Currency API daily fallback skipped for {len(days)} days")
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for day in days:
        trade_date = day.isoformat()
        url = f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{trade_date}/v1/currencies/{base_lower}.json"
        try:
            body = json.loads(fetch_text(url, timeout=10))
        except (json.JSONDecodeError, SyncWarning) as exc:
            errors.append(f"{trade_date}: {exc}")
            continue
        rate = (body.get(base_lower) or {}).get(quote_lower)
        if rate is None:
            errors.append(f"{trade_date}: no {quote_lower} rate")
            continue
        rows.append({"pair": pair, "trade_date": trade_date, "rate": float(rate), "source": "currency-api:jsdelivr"})
    if not rows:
        detail = "; ".join(errors[-3:]) if errors else "no requested business days"
        raise SyncWarning(f"Currency API returned no {pair} rows: {detail}")
    return rows


def fetch_open_er_latest_fx_rates(start: str, end: str, pair: str = "USD/CNY") -> list[dict[str, Any]]:
    base, quote = fx_pair_parts(pair)
    today = datetime.now(ZoneInfo("Asia/Singapore")).date()
    requested_today = [day for day in business_days(start, end) if day == today]
    if not requested_today:
        raise SyncWarning("Open ER latest FX fallback is only used for today's tail gap")
    url = f"https://open.er-api.com/v6/latest/{base}"
    try:
        body = json.loads(fetch_text(url, timeout=10))
    except (json.JSONDecodeError, SyncWarning) as exc:
        raise SyncWarning(f"Open ER FX fetch failed for {pair}: {exc}") from exc
    if body.get("result") != "success":
        raise SyncWarning(f"Open ER FX fetch returned {body.get('result') or 'unknown status'}")
    rate = (body.get("rates") or {}).get(quote)
    if rate is None:
        raise SyncWarning(f"Open ER returned no {pair} rate")
    return [
        {"pair": pair, "trade_date": day.isoformat(), "rate": float(rate), "source": "open-er-api:latest"}
        for day in requested_today
    ]


def fetch_akshare_repo_rates(symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    try:
        import akshare as ak  # type: ignore
    except ImportError as exc:
        raise SyncWarning("akshare is not installed; repo public source unavailable") from exc
    try:
        df = ak.bond_buy_back_hist_em(symbol=symbol)
    except Exception as exc:
        raise SyncWarning(f"AKShare repo fetch failed for {symbol}: {exc}") from exc
    start_date = parse_date(start)
    end_date = parse_date(end)
    rows: list[dict[str, Any]] = []
    for item in df.to_dict("records"):
        trade_date = parse_date(str(item["日期"]))
        if not (start_date <= trade_date <= end_date):
            continue
        rows.append(
            {
                "symbol": symbol,
                "trade_date": trade_date.isoformat(),
                "open_rate": float(item.get("开盘") or item.get("收盘")),
                "close_rate": float(item["收盘"]),
                "high_rate": float(item.get("最高") or item["收盘"]),
                "low_rate": float(item.get("最低") or item["收盘"]),
                "volume": float(item.get("成交量") or 0),
                "amount": float(item.get("成交额") or 0),
                "source": "akshare:bond_buy_back_hist_em",
            }
        )
    if not rows:
        raise SyncWarning(f"AKShare returned no repo rows for {symbol}")
    return rows


def fetch_eastmoney_repo_rates(symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    secid = f"1.{symbol}" if symbol.startswith("204") else f"0.{symbol}"
    url = (
        "http://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={secid}&klt=101&fqt=0&fields1=f1&fields2=f51,f52,f53,f54,f55,f56,f57&"
        f"beg={tushare_date(start)}&end={tushare_date(end)}"
    )
    try:
        body = json.loads(fetch_text(url))
    except (json.JSONDecodeError, SyncWarning) as exc:
        raise SyncWarning(f"Eastmoney repo fetch failed for {symbol}: {exc}") from exc
    klines = (body.get("data") or {}).get("klines") or []
    rows = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        rows.append(
            {
                "symbol": symbol,
                "trade_date": parts[0],
                "open_rate": float(parts[1]),
                "close_rate": float(parts[2]),
                "high_rate": float(parts[3]),
                "low_rate": float(parts[4]),
                "volume": float(parts[5] or 0),
                "amount": float(parts[6] or 0),
                "source": "eastmoney:repo_kline",
            }
        )
    if not rows:
        raise SyncWarning(f"Eastmoney returned no repo rows for {symbol}")
    return rows


def chunk_date_ranges(start: str, end: str, days: int) -> list[tuple[str, str]]:
    start_date = parse_date(start)
    end_date = parse_date(end)
    ranges: list[tuple[str, str]] = []
    current = start_date
    while current <= end_date:
        chunk_end = min(current + timedelta(days=days - 1), end_date)
        ranges.append((current.isoformat(), chunk_end.isoformat()))
        current = chunk_end + timedelta(days=1)
    return ranges


def parse_sohu_jsonp(text: str) -> list[dict[str, Any]]:
    start_idx = text.find("(")
    end_idx = text.rfind(")")
    if start_idx < 0 or end_idx <= start_idx:
        raise SyncWarning("Sohu repo response is not JSONP")
    try:
        payload = json.loads(text[start_idx + 1 : end_idx])
    except json.JSONDecodeError as exc:
        raise SyncWarning(f"Sohu repo response invalid JSONP: {text[:120]}") from exc
    return payload if isinstance(payload, list) else []


def fetch_sohu_jsonp_blocks(url: str, referer: str) -> list[dict[str, Any]]:
    errors: list[str] = []
    for attempt in range(3):
        try:
            return parse_sohu_jsonp(fetch_text(url, timeout=10, referer=referer))
        except SyncWarning as exc:
            errors.append(str(exc))
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    raise SyncWarning("; ".join(errors))


def fetch_sohu_repo_rates(symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    if not symbol.startswith("204"):
        raise SyncWarning(f"Sohu repo source only supports Shanghai repo symbols, got {symbol}")
    rows_by_date: dict[str, dict[str, Any]] = {}
    for chunk_start, chunk_end in chunk_date_ranges(start, end, 90):
        url = (
            "https://q.stock.sohu.com/hisHq?"
            f"code=cn_{symbol}&start={tushare_date(chunk_start)}&end={tushare_date(chunk_end)}"
            "&stat=1&order=D&period=d&callback=historySearchHandler&rt=jsonp"
        )
        try:
            data = fetch_sohu_jsonp_blocks(url, f"https://q.stock.sohu.com/cn/{symbol}/lshq.shtml")
        except SyncWarning:
            continue
        for block in data:
            for item in block.get("hq") or []:
                if len(item) < 9:
                    continue
                trade_date = item[0]
                rows_by_date[trade_date] = {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "open_rate": float(item[1]),
                    "close_rate": float(item[2]),
                    "high_rate": float(item[6]),
                    "low_rate": float(item[5]),
                    "volume": float(item[7] or 0),
                    "amount": float(item[8] or 0),
                    "source": "sohu:hisHq",
                }
    rows = [rows_by_date[key] for key in sorted(rows_by_date)]
    if not rows:
        raise SyncWarning(f"Sohu returned no repo rows for {symbol}")
    return rows


def sync_all(
    conn,
    token: str,
    start: str,
    end: str,
    assets: list[dict[str, Any]] | None = None,
    repo_symbol: str = "204001",
    allow_network: bool = True,
    missing_items: list[str] | None = None,
    should_cancel=None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    raise_if_cancelled(should_cancel)
    assets = assets or DEFAULT_ASSETS
    warnings: list[str] = []
    missing_data: list[str] = []
    plan = _sync_plan(missing_items, assets, repo_symbol)
    logger.info(
        "sync_all start range=%s..%s missing_items=%s plan_prices=%s plan_dividends=%s plan_adjustments=%s plan_index=%s plan_repo=%s plan_fx=%s",
        start,
        end,
        missing_items or ["all"],
        sorted(plan["asset_prices"]),
        sorted(plan["asset_dividends"]),
        sorted(plan["asset_adjustments"]),
        plan["index_prices"],
        sorted(plan["repo_symbols"]),
        sorted(plan["fx_pairs"]),
    )
    upsert_assets(conn, [{**asset, "source": "config"} for asset in assets])
    benchmark = {
        "symbol": "000300.SH",
        "name": "沪深300指数",
        "asset_type": "benchmark",
        "market": "CN",
        "currency": "CNY",
        "inception_date": None,
        "source": "config",
    }
    upsert_assets(conn, [benchmark])
    symbols = [asset["symbol"] for asset in assets] + ["000300.SH"]
    placeholders = ",".join("?" for _ in symbols)
    conn.execute(
        f"DELETE FROM prices WHERE symbol IN ({placeholders}) AND trade_date BETWEEN ? AND ? AND source LIKE 'generated:%'",
        (*symbols, start, end),
    )
    conn.execute(
        f"DELETE FROM fund_dividends WHERE symbol IN ({placeholders}) AND ex_date BETWEEN ? AND ? AND source LIKE 'generated:%'",
        (*symbols, start, end),
    )
    conn.execute(
        f"DELETE FROM adj_factors WHERE symbol IN ({placeholders}) AND trade_date BETWEEN ? AND ? AND source LIKE 'generated:%'",
        (*symbols, start, end),
    )
    fx_pairs = required_fx_pairs_for_assets(assets)
    if fx_pairs:
        fx_placeholders = ",".join("?" for _ in fx_pairs)
        conn.execute(
            f"DELETE FROM fx_rates WHERE pair IN ({fx_placeholders}) AND trade_date BETWEEN ? AND ? AND source LIKE 'generated:%'",
            (*fx_pairs, start, end),
        )
    conn.execute("DELETE FROM repo_rates WHERE symbol=? AND trade_date BETWEEN ? AND ? AND source LIKE 'generated:%'", (repo_symbol, start, end))
    if repo_symbol != "204001":
        conn.execute("DELETE FROM repo_rates WHERE symbol=? AND trade_date BETWEEN ? AND ? AND source LIKE 'generated:%'", ("204001", start, end))

    inserted = {"prices": 0, "dividends": 0, "adj_factors": 0, "repo_rates": 0, "fx_rates": 0}
    price_range_func = missing_date_ranges if plan["full"] else missing_tail_date_ranges
    rate_range_func = missing_date_ranges if plan["full"] else missing_edge_date_ranges
    cn_data_end = effective_price_end_for_market("CN", end)
    cn_data_end_text = cn_data_end.isoformat()
    asset_price_ranges: dict[str, list[tuple[str, str]]] = {}
    asset_dividend_ranges: dict[str, list[tuple[str, str]]] = {}
    asset_adjustment_ranges: dict[str, list[tuple[str, str]]] = {}
    for asset in assets:
        symbol = asset["symbol"]
        price_fetch_start_date = max(parse_date(start), parse_date(asset_price_start_date(asset, start)))
        dividend_fetch_start_date = max(parse_date(start), parse_date(asset_trade_start_date(asset, start)))
        price_end = effective_price_end_for_asset(asset, end)
        dividend_end = effective_asset_end(asset, end)
        price_fetch_start = price_fetch_start_date.isoformat()
        dividend_fetch_start = dividend_fetch_start_date.isoformat()
        range_function = chinabond_price_sync_ranges if asset.get("asset_type") == "cn_bond_index" else price_range_func
        asset_price_ranges[symbol] = (
            asset_price_sync_ranges(conn, asset, price_fetch_start, price_end.isoformat(), range_function)
            if symbol in plan["asset_prices"] and price_fetch_start_date <= price_end
            else []
        )
        if symbol in plan["asset_prices"] and price_fetch_start_date <= price_end:
            asset_price_ranges[symbol] = merge_date_ranges(
                asset_price_ranges[symbol]
                + price_anomaly_ranges(conn, symbol, price_fetch_start, price_end.isoformat())
                + (
                    legacy_cn_yahoo_price_ranges(conn, symbol, price_fetch_start, price_end.isoformat())
                    if asset.get("market") == "CN"
                    else []
                )
                + (
                    legacy_unscaled_index_proxy_price_ranges(conn, symbol, price_fetch_start, price_end.isoformat())
                    if asset.get("market") == "CN"
                    else []
                )
                + legacy_chinabond_modeled_overlap_ranges(
                    conn,
                    asset,
                    price_fetch_start,
                    price_end.isoformat(),
                )
            )
        asset_dividend_ranges[symbol] = (
            missing_coverage_ranges(conn, "dividends", symbol, dividend_fetch_start, dividend_end.isoformat())
            if symbol in plan["asset_dividends"] and asset.get("asset_type") != "money_fund" and dividend_fetch_start_date <= dividend_end
            else []
        )
        asset_adjustment_ranges[symbol] = (
            missing_adjustment_factor_ranges(
                conn,
                symbol,
                dividend_fetch_start,
                price_end.isoformat(),
                exclude_proxy_prices=isinstance(asset.get("price_fallback"), dict),
            )
            if symbol in plan["asset_adjustments"] and asset.get("market") == "CN" and asset.get("asset_type") not in {"cn_bond_index", "money_fund"} and asset.get("enabled", True) and dividend_fetch_start_date <= price_end
            else []
        )
        if symbol in plan["asset_prices"] and asset.get("market") == "CN" and asset.get("asset_type") not in {"cn_bond_index", "money_fund"} and asset.get("enabled", True):
            asset_adjustment_ranges[symbol] = merge_date_ranges(
                asset_adjustment_ranges[symbol]
                + [
                    (max(parse_date(range_start), dividend_fetch_start_date).isoformat(), range_end)
                    for range_start, range_end in asset_price_ranges[symbol]
                    if max(parse_date(range_start), dividend_fetch_start_date) <= parse_date(range_end)
                ]
            )

    price_anchor_start = (parse_date(start) - timedelta(days=370)).isoformat()
    price_anchor_end = (parse_date(end) + timedelta(days=370)).isoformat()
    planned_asset_symbols = (
        set(plan["asset_prices"])
        | set(plan["asset_dividends"])
        | set(plan["asset_adjustments"])
    )
    sync_assets = [asset for asset in assets if asset["symbol"] in planned_asset_symbols]
    existing_price_rows: dict[str, list[dict[str, Any]]] = {}
    for asset in sync_assets:
        rows = conn.execute(
            """
            SELECT trade_date, close, source FROM prices
            WHERE symbol=? AND trade_date BETWEEN ? AND ?
              AND source NOT LIKE 'generated:%'
            ORDER BY trade_date
            """,
            (asset["symbol"], price_anchor_start, price_anchor_end),
        ).fetchall()
        existing_price_rows[asset["symbol"]] = [dict(row) for row in rows]

    def fetch_asset_bundle(asset: dict[str, Any]) -> dict[str, Any]:
        raise_if_cancelled(should_cancel)
        asset_started_at = time.perf_counter()
        asset_warnings: list[str] = []
        asset_missing: list[str] = []
        symbol = asset["symbol"]
        fallback = asset.get("price_fallback") if isinstance(asset.get("price_fallback"), dict) else None
        fallback_kind = fallback.get("kind") if fallback else None
        authoritative_fallback = fallback_kind in {"sge_au9999", "chinabond_30y_yield_total_return"}
        primary_start = parse_date(asset_trade_start_date(asset, start))
        price_ranges = asset_price_ranges[symbol]
        dividend_ranges = asset_dividend_ranges[symbol]
        prices: list[dict[str, Any]] = []
        tushare_asset_available = True
        for range_start, range_end in price_ranges:
            raise_if_cancelled(should_cancel)
            range_prices: list[dict[str, Any]] = []
            if not allow_network:
                asset_warnings.append("network disabled for deterministic sync")
            elif asset.get("asset_type") == "cn_bond_index":
                # ChinaBond publishes on the bond-market calendar, which is not
                # the weekday calendar.  A valid returned series is authoritative
                # and must not be flagged as missing on mainland public holidays.
                expected_price_dates = set()
                try:
                    range_prices = fetch_chinabond_index_prices(asset, range_start, range_end)
                except SyncWarning as exc:
                    asset_warnings.append(str(exc))
            elif asset.get("market") == "CN":
                expected_price_dates = {day.isoformat() for day in business_days(range_start, range_end)}
                cn_price_sources = [
                    ("tushare", lambda: fetch_cn_fund_prices(token, symbol, range_start, range_end)),
                    ("datasrc", lambda: fetch_datasrc_market_prices(symbol, range_start, range_end, "CNY")),
                    ("sohu", lambda: fetch_sohu_prices(symbol, range_start, range_end, "CNY", "sohu:hisHq")),
                    ("eastmoney", lambda: fetch_eastmoney_prices(symbol, range_start, range_end, "CNY", "eastmoney:fund_kline")),
                ]
                source_rows: list[dict[str, Any]] = []
                for source_name, fetch_prices in cn_price_sources:
                    raise_if_cancelled(should_cancel)
                    try:
                        rows = [row for row in fetch_prices() if row["trade_date"] in expected_price_dates]
                    except SyncWarning as exc:
                        if source_name == "tushare":
                            tushare_asset_available = False
                        asset_warnings.append(str(exc))
                        continue
                    raise_if_cancelled(should_cancel)
                    if rows:
                        source_rows.extend(rows)
                        logger.info("sync price source complete symbol=%s source=%s range=%s..%s rows=%d", symbol, source_name, range_start, range_end, len(rows))
                range_prices, quality_warnings = select_price_rows_from_sources(
                    source_rows,
                    expected_dates=expected_price_dates,
                    existing_rows=existing_price_rows.get(symbol, []) + prices,
                    source_priorities=CN_PRICE_SOURCE_PRIORITY,
                    symbol=symbol,
                )
                asset_warnings.extend(quality_warnings)
            elif asset.get("market") == "HK":
                expected_price_dates = {day.isoformat() for day in business_days(range_start, range_end)}
                hk_price_sources = [
                    ("datasrc", lambda: fetch_datasrc_market_prices(symbol, range_start, range_end, asset.get("currency", "HKD"))),
                    ("eastmoney-hk", lambda: fetch_eastmoney_prices(symbol, range_start, range_end, asset.get("currency", "HKD"), "eastmoney:hk_kline")),
                    ("tencent-hk", lambda: fetch_tencent_hk_prices(symbol, range_start, range_end, asset.get("currency", "HKD"))),
                    ("yahoo-chart", lambda: fetch_hk_yahoo_prices(symbol, range_start, range_end, asset.get("currency", "HKD"))),
                    ("stooq-hk", lambda: fetch_hk_stooq_prices(symbol, range_start, range_end, asset.get("currency", "HKD"))),
                    ("yahoo-spark", lambda: fetch_hk_yahoo_spark_prices(symbol, range_start, range_end, asset.get("currency", "HKD"))),
                ]
                for source_name, fetch_prices in hk_price_sources:
                    raise_if_cancelled(should_cancel)
                    remaining_dates = expected_price_dates - {row["trade_date"] for row in range_prices}
                    if not remaining_dates:
                        break
                    try:
                        source_rows = fetch_prices()
                    except SyncWarning as exc:
                        asset_warnings.append(str(exc))
                        continue
                    raise_if_cancelled(should_cancel)
                    source_rows = [row for row in source_rows if row["trade_date"] in remaining_dates]
                    if source_rows:
                        range_prices = merge_rows_by_trade_date(range_prices, source_rows)
                        logger.info("sync price source complete symbol=%s source=%s range=%s..%s rows=%d", symbol, source_name, range_start, range_end, len(source_rows))
            else:
                expected_price_dates = {day.isoformat() for day in business_days(range_start, range_end)}
                us_price_sources = [
                    ("yahoo-chart", lambda: fetch_yahoo_prices(symbol, range_start, range_end, asset.get("currency", "USD"))),
                    ("nasdaq", lambda: fetch_nasdaq_prices(symbol, range_start, range_end, asset.get("currency", "USD"))),
                    ("stooq", lambda: fetch_stooq_prices(symbol, range_start, range_end)),
                    ("yahoo-spark", lambda: fetch_yahoo_spark_prices(symbol, range_start, range_end, asset.get("currency", "USD"))),
                ]
                for source_name, fetch_prices in us_price_sources:
                    raise_if_cancelled(should_cancel)
                    remaining_dates = expected_price_dates - {row["trade_date"] for row in range_prices}
                    if not remaining_dates:
                        break
                    try:
                        source_rows = fetch_prices()
                    except SyncWarning as exc:
                        asset_warnings.append(str(exc))
                        continue
                    raise_if_cancelled(should_cancel)
                    source_rows = [row for row in source_rows if row["trade_date"] in remaining_dates]
                    if source_rows:
                        range_prices = merge_rows_by_trade_date(range_prices, source_rows)
                        logger.info("sync price source complete symbol=%s source=%s range=%s..%s rows=%d", symbol, source_name, range_start, range_end, len(source_rows))
            if asset.get("asset_type") == "cn_bond_index":
                # ChinaBond's bond-market calendar is authoritative.  Its
                # reported dates must not be measured against the mainland
                # equity weekday calendar, or valid holiday gaps become false
                # "missing" warnings after a successful insert.
                expected_price_dates = {row["trade_date"] for row in range_prices}
                fallback_end = min(parse_date(range_end), primary_start - timedelta(days=1))
                if authoritative_fallback and parse_date(range_start) <= fallback_end:
                    # These dates only trigger the fallback fetch.  Afterward,
                    # the returned official curve calendar becomes authoritative.
                    expected_price_dates.update(day.isoformat() for day in business_days(range_start, fallback_end))
            else:
                expected_price_dates = {day.isoformat() for day in business_days(range_start, range_end)}
            missing_price_dates = expected_price_dates - {row["trade_date"] for row in range_prices}
            if missing_price_dates and allow_network and asset.get("price_fallback"):
                raise_if_cancelled(should_cancel)
                try:
                    fallback_range_end = range_end
                    if authoritative_fallback:
                        fallback_range_end = min(
                            parse_date(range_end), primary_start - timedelta(days=1)
                        ).isoformat()
                    fallback_rows = (
                        fetch_price_fallback_rows(
                            asset,
                            range_start,
                            fallback_range_end,
                            existing_price_rows.get(symbol, []) + range_prices,
                            token=token,
                        )
                        if parse_date(range_start) <= parse_date(fallback_range_end)
                        else []
                    )
                    fallback_rows = [
                        row
                        for row in fallback_rows
                        if row["trade_date"] in missing_price_dates
                        and (not authoritative_fallback or parse_date(row["trade_date"]) < primary_start)
                    ]
                    if fallback_rows:
                        range_prices = merge_rows_by_trade_date(range_prices, fallback_rows)
                        logger.info(
                            "sync price fallback complete symbol=%s range=%s..%s rows=%d",
                            symbol,
                            range_start,
                            range_end,
                            len(fallback_rows),
                        )
                except SyncWarning as exc:
                    asset_warnings.append(str(exc))
                raise_if_cancelled(should_cancel)
            if authoritative_fallback:
                wants_fallback = parse_date(range_start) < primary_start
                has_fallback = any(parse_date(row["trade_date"]) < primary_start for row in range_prices)
                wants_primary = parse_date(range_end) >= primary_start
                has_primary = any(parse_date(row["trade_date"]) >= primary_start for row in range_prices)
                if (wants_fallback and not has_fallback) or (wants_primary and not has_primary):
                    if f"prices:{symbol}" not in asset_missing:
                        asset_missing.append(f"prices:{symbol}")
                # SGE and ChinaBond publish on their own trading calendars.
                expected_price_dates = {row["trade_date"] for row in range_prices}
            missing_price_dates = expected_price_dates - {row["trade_date"] for row in range_prices}
            if missing_price_dates and f"prices:{symbol}" not in asset_missing:
                asset_missing.append(f"prices:{symbol}")
            prices = merge_rows_by_trade_date(prices, range_prices)
        dividends: list[dict[str, Any]] = []
        adj: list[dict[str, Any]] = []
        dividend_coverage: list[tuple[str, str, str, str]] = []
        for range_start, range_end in dividend_ranges:
            raise_if_cancelled(should_cancel)
            range_dividends: list[dict[str, Any]] = []
            dividend_source = ""
            dividend_fetch_succeeded = False
            try:
                if not allow_network:
                    raise SyncWarning("network disabled for deterministic sync")
                if asset.get("asset_type") == "cn_bond_index":
                    dividend_source = "chinabond:index_total_return"
                    dividend_fetch_succeeded = True
                elif asset.get("asset_type") == "money_fund":
                    # 511990's daily holding-period income is embedded in its
                    # exchange price.  Do not require an ordinary cash-dividend
                    # feed, which would otherwise make every sync look incomplete.
                    dividend_source = "market_price:money_fund_total_return"
                    dividend_fetch_succeeded = True
                elif asset.get("market") == "CN":
                    cn_dividend_sources = [
                        ("tushare:fund_div", lambda: fetch_fund_dividends(token, symbol, range_start, range_end)),
                        ("eastmoney:fund_dividend", lambda: fetch_eastmoney_fund_dividends(symbol, range_start, range_end)),
                        ("sina:etf_cumulative_dividend", lambda: fetch_sina_etf_dividends(symbol, range_start, range_end)),
                    ]
                    for source, fetch_dividends in cn_dividend_sources:
                        try:
                            range_dividends = fetch_dividends()
                            dividend_source = source
                            dividend_fetch_succeeded = True
                            break
                        except SyncWarning as exc:
                            asset_warnings.append(str(exc))
                    if not dividend_fetch_succeeded:
                        raise SyncWarning(f"all public dividend sources failed for {symbol}")
                elif asset.get("market") == "HK":
                    range_dividends = fetch_hk_yahoo_dividends(symbol, range_start, range_end, asset.get("currency", "HKD"))
                    dividend_source = "yahoo:chart:dividend"
                    dividend_fetch_succeeded = True
                else:
                    try:
                        range_dividends = fetch_yahoo_dividends(symbol, range_start, range_end, asset.get("currency", "USD"))
                        dividend_source = "yahoo:chart:dividend"
                        dividend_fetch_succeeded = True
                    except SyncWarning as yahoo_exc:
                        asset_warnings.append(str(yahoo_exc))
                        range_dividends = fetch_digrin_dividends(symbol, range_start, range_end, asset.get("currency", "USD"))
                        dividend_source = "digrin:html:dividend"
                        dividend_fetch_succeeded = True
                dividends.extend(range_dividends)
                dividend_coverage.append((symbol, range_start, range_end, dividend_source))
            except SyncWarning as exc:
                asset_warnings.append(str(exc))
                asset_missing.append(f"dividends:{symbol}")
        if asset.get("market") == "CN" and asset.get("asset_type") not in {"cn_bond_index", "money_fund"} and asset_adjustment_ranges[symbol]:
            if allow_network and tushare_asset_available:
                for range_start, range_end in asset_adjustment_ranges[symbol]:
                    raise_if_cancelled(should_cancel)
                    try:
                        adj = merge_rows_by_trade_date(adj, fetch_adj_factors(token, symbol, range_start, range_end))
                    except SyncWarning as exc:
                        asset_warnings.append(str(exc))
            else:
                asset_warnings.append(f"skip Tushare adj for {symbol} because primary Tushare price source is unavailable")
        return {
            "symbol": symbol,
            "prices": prices,
            "dividends": dividends,
            "dividend_coverage": dividend_coverage,
            "adj": adj,
            "warnings": asset_warnings,
            "missing": asset_missing,
            "seconds": time.perf_counter() - asset_started_at,
        }

    # ChinaBond's official endpoint serves a complete historical JSON series
    # for every request.  Serialise only these large transfers so they cannot
    # collectively exceed the upstream/proxy transfer window; ordinary asset
    # feeds keep the existing bounded parallelism.
    chinabond_assets = [asset for asset in sync_assets if asset.get("asset_type") == "cn_bond_index"]
    concurrent_assets = [asset for asset in sync_assets if asset.get("asset_type") != "cn_bond_index"]
    max_workers = min(max(len(concurrent_assets), 1), 8)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        raise_if_cancelled(should_cancel)
        futures = [executor.submit(fetch_asset_bundle, asset) for asset in concurrent_assets]
        for future in as_completed(futures):
            raise_if_cancelled(should_cancel)
            bundle = future.result()
            logger.info(
                "sync asset complete symbol=%s prices=%d dividends=%d adj=%d seconds=%.3f warnings=%d missing=%s",
                bundle["symbol"],
                len(bundle["prices"]),
                len(bundle["dividends"]),
                len(bundle["adj"]),
                bundle["seconds"],
                len(bundle["warnings"]),
                bundle["missing"],
            )
            warnings.extend(bundle["warnings"])
            missing_data.extend(bundle["missing"])
            if bundle["symbol"] in plan["asset_prices"]:
                for range_start, range_end in legacy_cn_yahoo_price_ranges(conn, bundle["symbol"], start, end):
                    conn.execute(
                        "DELETE FROM prices WHERE symbol=? AND trade_date BETWEEN ? AND ? AND source LIKE 'yahoo:%'",
                        (bundle["symbol"], range_start, range_end),
                    )
                for range_start, range_end in legacy_unscaled_index_proxy_price_ranges(conn, bundle["symbol"], start, end):
                    conn.execute(
                        """
                        DELETE FROM prices
                        WHERE symbol=? AND trade_date BETWEEN ? AND ? AND ABS(close) >= 100
                          AND (source LIKE '%:splice_scale_1' OR source IN (?, ?, ?, ?, ?))
                        """,
                        (bundle["symbol"], range_start, range_end, *sorted(INDEX_PROXY_PRICE_SOURCES)),
                    )
            inserted["prices"] += insert_many(conn, "prices", bundle["prices"])
            inserted["dividends"] += upsert_dividend_rows(conn, bundle["dividends"])
            for coverage_symbol, coverage_start, coverage_end, coverage_source in bundle["dividend_coverage"]:
                mark_sync_coverage(conn, "dividends", coverage_symbol, coverage_start, coverage_end, coverage_source)
            inserted["adj_factors"] += insert_many(conn, "adj_factors", bundle["adj"])

    for asset in chinabond_assets:
        raise_if_cancelled(should_cancel)
        bundle = fetch_asset_bundle(asset)
        logger.info(
            "sync ChinaBond asset complete symbol=%s prices=%d dividends=%d adj=%d seconds=%.3f warnings=%d missing=%s",
            bundle["symbol"],
            len(bundle["prices"]),
            len(bundle["dividends"]),
            len(bundle["adj"]),
            bundle["seconds"],
            len(bundle["warnings"]),
            bundle["missing"],
        )
        warnings.extend(bundle["warnings"])
        missing_data.extend(bundle["missing"])
        inserted["prices"] += insert_many(conn, "prices", bundle["prices"])
        inserted["dividends"] += upsert_dividend_rows(conn, bundle["dividends"])
        for coverage_symbol, coverage_start, coverage_end, coverage_source in bundle["dividend_coverage"]:
            mark_sync_coverage(conn, "dividends", coverage_symbol, coverage_start, coverage_end, coverage_source)
        inserted["adj_factors"] += insert_many(conn, "adj_factors", bundle["adj"])

    if allow_network:
        for asset in assets:
            symbol = asset["symbol"]
            if (
                symbol not in plan["asset_adjustments"]
                or not asset.get("allow_adj_factor_tail_carry_forward")
            ):
                continue
            carry_rows = carry_forward_adjustment_factor_rows(
                conn,
                symbol,
                asset_trade_start_date(asset, start),
                effective_price_end_for_asset(asset, end).isoformat(),
            )
            if carry_rows:
                inserted["adj_factors"] += insert_many(conn, "adj_factors", carry_rows)
                logger.info(
                    "sync adjustment factor tail carried symbol=%s rows=%d range=%s..%s",
                    symbol,
                    len(carry_rows),
                    carry_rows[0]["trade_date"],
                    carry_rows[-1]["trade_date"],
                )

    index_prices: list[dict[str, Any]] = []
    index_started_at = time.perf_counter()
    index_ranges = (
        price_range_func(conn, "prices", "symbol", "000300.SH", "trade_date", start, cn_data_end_text)
        if plan["index_prices"] and parse_date(start) <= cn_data_end
        else []
    )
    for range_start, range_end in index_ranges:
        raise_if_cancelled(should_cancel)
        range_prices: list[dict[str, Any]] = []
        try:
            if not allow_network:
                raise SyncWarning("network disabled for deterministic sync")
            range_prices = fetch_index_prices(token, "000300.SH", range_start, range_end)
        except SyncWarning as exc:
            warnings.append(str(exc))
            if allow_network:
                raise_if_cancelled(should_cancel)
                try:
                    range_prices = fetch_datasrc_market_prices("000300.SH", range_start, range_end, "CNY")
                except SyncWarning as datasrc_exc:
                    warnings.append(str(datasrc_exc))
            if allow_network:
                raise_if_cancelled(should_cancel)
                try:
                    range_prices = merge_rows_by_trade_date(range_prices, fetch_sohu_prices("000300.SH", range_start, range_end, "CNY", "sohu:hisHq"))
                except SyncWarning as public_exc:
                    warnings.append(str(public_exc))
            if allow_network and not range_prices:
                raise_if_cancelled(should_cancel)
                try:
                    range_prices = fetch_eastmoney_prices("000300.SH", range_start, range_end, "CNY", "eastmoney:index_kline")
                except SyncWarning as public_exc:
                    warnings.append(str(public_exc))
        if not range_prices:
            missing_data.append("prices:000300.SH")
        index_prices = merge_rows_by_trade_date(index_prices, range_prices)
    inserted["prices"] += insert_many(conn, "prices", index_prices)
    if index_ranges:
        logger.info("sync index complete ranges=%d rows=%d seconds=%.3f", len(index_ranges), len(index_prices), time.perf_counter() - index_started_at)

    def fetch_repo_bundle(symbol: str, range_start: str, range_end: str) -> list[dict[str, Any]]:
        raise_if_cancelled(should_cancel)
        rows: list[dict[str, Any]] = []
        try:
            rows = fetch_datasrc_repo_rates(symbol, range_start, range_end)
        except SyncWarning as exc:
            warnings.append(str(exc))
        if not rows:
            raise_if_cancelled(should_cancel)
            try:
                rows = fetch_sohu_repo_rates(symbol, range_start, range_end)
            except SyncWarning as exc:
                warnings.append(str(exc))
        if not rows:
            raise_if_cancelled(should_cancel)
            try:
                rows = fetch_akshare_repo_rates(symbol, range_start, range_end)
            except SyncWarning as exc:
                warnings.append(str(exc))
        if not rows:
            raise_if_cancelled(should_cancel)
            try:
                rows = fetch_eastmoney_repo_rates(symbol, range_start, range_end)
            except SyncWarning as eastmoney_exc:
                warnings.append(str(eastmoney_exc))
        if not rows:
            missing_data.append(f"repo_rates:{symbol}")
        return rows

    repo_rows: list[dict[str, Any]] = []
    repo_started_at = time.perf_counter()
    repo_range_map = {
        current_repo_symbol: (
            rate_range_func(conn, "repo_rates", "symbol", current_repo_symbol, "trade_date", start, cn_data_end_text)
            if parse_date(start) <= cn_data_end
            else []
        )
        for current_repo_symbol in sorted(plan["repo_symbols"])
    }
    if not allow_network:
        warnings.append("network disabled for deterministic sync")
        for current_repo_symbol, ranges in repo_range_map.items():
            if ranges:
                missing_data.append(f"repo_rates:{current_repo_symbol}")
    else:
        for current_repo_symbol, ranges in repo_range_map.items():
            for range_start, range_end in ranges:
                raise_if_cancelled(should_cancel)
                repo_rows = merge_rows_by_trade_date(repo_rows, fetch_repo_bundle(current_repo_symbol, range_start, range_end))
    inserted["repo_rates"] += insert_many(conn, "repo_rates", repo_rows)
    if repo_range_map:
        logger.info(
            "sync repo complete ranges=%d rows=%d seconds=%.3f",
            sum(len(ranges) for ranges in repo_range_map.values()),
            len(repo_rows),
            time.perf_counter() - repo_started_at,
        )

    fx_rows: list[dict[str, Any]] = []
    fx_started_at = time.perf_counter()
    fx_range_map = {
        pair: (
            rate_range_func(conn, "fx_rates", "pair", pair, "trade_date", start, cn_data_end_text)
            if parse_date(start) <= cn_data_end
            else []
        )
        for pair in sorted(plan["fx_pairs"])
    }
    if not allow_network:
        warnings.append("network disabled for deterministic sync")
        for pair, ranges in fx_range_map.items():
            if ranges:
                missing_data.append(f"fx_rates:{pair}")
    else:
        fx_sources = [
            ("datasrc", fetch_datasrc_fx_rates),
            ("yahoo", fetch_yahoo_fx_rates),
            ("frankfurter", fetch_frankfurter_fx_rates),
            ("stooq", fetch_stooq_fx_rates),
            ("currency-api", fetch_currency_api_fx_rates),
            ("open-er-api", fetch_open_er_latest_fx_rates),
        ]
        for pair, ranges in fx_range_map.items():
            pair_rows: list[dict[str, Any]] = []
            for range_start, range_end in ranges:
                raise_if_cancelled(should_cancel)
                range_fx_rows: list[dict[str, Any]] = []
                expected_fx_dates = {day.isoformat() for day in business_days(range_start, range_end)}
                for source_name, fetch_fx_rates in fx_sources:
                    raise_if_cancelled(should_cancel)
                    remaining_dates = expected_fx_dates - {row["trade_date"] for row in range_fx_rows}
                    if not remaining_dates:
                        break
                    try:
                        if pair == "USD/CNY":
                            source_rows = fetch_fx_rates(range_start, range_end)
                        else:
                            source_rows = fetch_fx_rates(range_start, range_end, pair)
                    except SyncWarning as exc:
                        warnings.append(str(exc))
                        continue
                    raise_if_cancelled(should_cancel)
                    source_rows = [row for row in source_rows if row["pair"] == pair and row["trade_date"] in remaining_dates]
                    if source_rows:
                        range_fx_rows = merge_rows_by_trade_date(range_fx_rows, source_rows)
                        logger.info("sync fx source complete pair=%s source=%s range=%s..%s rows=%d", pair, source_name, range_start, range_end, len(source_rows))
                missing_fx_dates = expected_fx_dates - {row["trade_date"] for row in range_fx_rows}
                if missing_fx_dates:
                    missing_data.append(f"fx_rates:{pair}")
                pair_rows.extend(range_fx_rows)
            if ranges and not pair_rows:
                missing_data.append(f"fx_rates:{pair}")
            fx_rows.extend(pair_rows)
    inserted["fx_rates"] += insert_many(conn, "fx_rates", fx_rows)
    if fx_range_map:
        logger.info("sync fx complete ranges=%d rows=%d seconds=%.3f", sum(len(ranges) for ranges in fx_range_map.values()), len(fx_rows), time.perf_counter() - fx_started_at)

    result = {"inserted": inserted, "warnings": sorted(set(warnings)), "missing_data": sorted(set(missing_data))}
    logger.info("sync_all complete seconds=%.3f inserted=%s missing=%s warnings=%d", time.perf_counter() - started_at, inserted, result["missing_data"], len(result["warnings"]))
    return result
