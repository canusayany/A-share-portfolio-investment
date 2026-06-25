from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any
from zoneinfo import ZoneInfo
from app.config import DEFAULT_ASSETS
from app.db import insert_many, upsert_assets, utc_now
from app.services.calendar import business_days, daterange, parse_date


TUSHARE_URL = "http://api.tushare.pro"
HTTP_TIMEOUT_SECONDS = 2
CURL_TIMEOUT_SECONDS = 8
DATASRC_MARKET_APPSETTINGS = Path.home() / "Documents" / "code" / "DataSrc" / "market-data-platform" / "src" / "Market.Api" / "appsettings.json"
DATASRC_SOURCE_PRIORITY = {"tushare": 0, "akshare": 1, "amazingdata": 2, "tdx": 3}
logger = logging.getLogger(__name__)


class SyncWarning(RuntimeError):
    pass


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
    required_start = parse_date(start)
    requested_end = parse_date(end)
    check_end = min(requested_end, datetime.now(timezone.utc).date())
    if require_start and first_date > required_start + timedelta(days=start_tolerance_days):
        return True
    if last_date < check_end - timedelta(days=end_tolerance_days):
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
    asset_symbols = {asset["symbol"] for asset in assets}
    if missing_items is None or any(item.startswith("generated:") for item in missing_items):
        return {
            "asset_prices": set(asset_symbols),
            "asset_dividends": set(asset_symbols),
            "index_prices": True,
            "repo_symbols": set(sorted({"204001", repo_symbol})),
            "fx_rates": True,
            "full": True,
        }

    plan = {
        "asset_prices": set(),
        "asset_dividends": set(),
        "index_prices": False,
        "repo_symbols": set(),
        "fx_rates": False,
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
        elif kind == "repo_rates":
            plan["repo_symbols"].add(symbol)
        elif kind == "fx_rates":
            plan["fx_rates"] = True
    if plan["repo_symbols"]:
        plan["repo_symbols"].add("204001")
        plan["repo_symbols"].add(repo_symbol)
    return plan


def previous_weekday(day):
    current = day - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def latest_completed_market_day(market: str):
    now_utc = datetime.now(timezone.utc)
    if market == "US":
        now_market = now_utc.astimezone(ZoneInfo("America/New_York"))
        close_hour = 17
    else:
        now_market = now_utc.astimezone(ZoneInfo("Asia/Shanghai"))
        close_hour = 18
    today = now_market.date()
    if today.weekday() >= 5 or now_market.hour < close_hour:
        return previous_weekday(today)
    return today


def effective_price_end_for_market(market: str, end: str):
    requested_end = parse_date(end)
    latest_completed = latest_completed_market_day(market)
    return min(requested_end, latest_completed)


def effective_price_end_for_asset(asset: dict[str, Any], end: str):
    return effective_price_end_for_market(asset.get("market", "CN"), end)


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
    price_symbols = [asset["symbol"] for asset in assets] + ["000300.SH"]

    for asset in assets:
        if not asset.get("enabled", True):
            continue
        fetch_start = max(parse_date(start), parse_date(asset.get("inception_date") or start))
        if fetch_start > requested_end:
            continue
        price_end = effective_price_end_for_asset(asset, end)
        if fetch_start <= price_end and _coverage_gap(conn, "prices", "symbol", asset["symbol"], "trade_date", fetch_start.isoformat(), price_end.isoformat(), end_tolerance_days=0):
            missing.add(f"prices:{asset['symbol']}")
        if missing_coverage_ranges(conn, "dividends", asset["symbol"], fetch_start.isoformat(), end):
            missing.add(f"dividends:{asset['symbol']}")

    cn_data_end = effective_price_end_for_market("CN", end)
    cn_data_end_text = cn_data_end.isoformat()
    if parse_date(start) <= cn_data_end and _coverage_gap(conn, "prices", "symbol", "000300.SH", "trade_date", start, cn_data_end_text, require_start=True, end_tolerance_days=0):
        missing.add("prices:000300.SH")
    if parse_date(start) <= cn_data_end and _coverage_gap(conn, "fx_rates", "pair", "USD/CNY", "trade_date", start, cn_data_end_text, require_start=True, end_tolerance_days=0):
        missing.add("fx_rates:USD/CNY")
    repo_symbols = sorted({"204001", repo_symbol})
    for current_repo_symbol in repo_symbols:
        if parse_date(start) <= cn_data_end and _coverage_gap(conn, "repo_rates", "symbol", current_repo_symbol, "trade_date", start, cn_data_end_text, end_tolerance_days=0):
            missing.add(f"repo_rates:{current_repo_symbol}")

    generated_checks = [
        ("prices", "symbol", price_symbols, "trade_date"),
        ("fund_dividends", "symbol", price_symbols, "ex_date"),
        ("adj_factors", "symbol", price_symbols, "trade_date"),
        ("fx_rates", "pair", ["USD/CNY"], "trade_date"),
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


def fetch_datasrc_fx_rates(start: str, end: str) -> list[dict[str, Any]]:
    rows = fetch_datasrc_series("USDCNY", "fx", start, end)
    result = [
        {"pair": "USD/CNY", "trade_date": row["point_date"], "rate": row["value"], "source": f"datasrc:{row['source']}:series_point"}
        for row in rows
    ]
    if not result:
        raise SyncWarning("DataSrc Postgres returned no USD/CNY rows")
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


def eastmoney_secid(symbol: str) -> str:
    code = symbol.split(".")[0]
    suffix = symbol.split(".")[-1].upper() if "." in symbol else ""
    market = "1" if suffix == "SH" or code.startswith(("5", "6")) else "0"
    return f"{market}.{code}"


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


def sohu_code_and_referer(symbol: str) -> tuple[str, str]:
    code = symbol.split(".")[0]
    if symbol.upper() == "000300.SH":
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
    rows = tushare_call(
        token,
        "index_daily",
        {"ts_code": symbol, "start_date": tushare_date(start), "end_date": tushare_date(end)},
        "ts_code,trade_date,open,high,low,close,vol,amount",
    )
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
    for row in rows:
        ex_date = from_tushare_date(row.get("ex_date"))
        if not ex_date:
            continue
        ex = parse_date(ex_date)
        if not (start_date <= ex <= end_date):
            continue
        dividends.append(
            {
                "symbol": row["ts_code"],
                "ann_date": from_tushare_date(row.get("ann_date")),
                "record_date": from_tushare_date(row.get("record_date")),
                "ex_date": ex_date,
                "pay_date": from_tushare_date(row.get("pay_date")) or ex_date,
                "div_cash": float(row.get("div_cash") or 0),
                "currency": "CNY",
                "source": "tushare:fund_div",
            }
        )
    return dividends


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


def fetch_stooq_prices(symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    stooq_symbol = symbol.lower()
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
                "currency": "USD",
                "source": "stooq",
            }
        )
    if not result:
        raise SyncWarning(f"Stooq returned no rows for {symbol}")
    return result


def parse_market_number(value: Any) -> float:
    cleaned = re.sub(r"[^0-9.\-]", "", str(value or ""))
    if not cleaned:
        return 0.0
    return float(cleaned)


def fetch_nasdaq_prices(symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    url = (
        f"https://api.nasdaq.com/api/quote/{symbol}/historical?"
        f"assetclass=etf&fromdate={start}&todate={end}&limit=9999"
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


def fetch_yahoo_prices(symbol: str, start: str, end: str, currency: str) -> list[dict[str, Any]]:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?period1={yahoo_period(start)}&period2={yahoo_period(end) + 86400}&interval=1d&events=div,splits"
    )
    try:
        body = json.loads(fetch_text(url, timeout=30))
    except (json.JSONDecodeError, SyncWarning) as exc:
        raise SyncWarning(f"Yahoo price fetch failed for {symbol}: {exc}") from exc
    result = ((body.get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise SyncWarning(f"Yahoo returned no price rows for {symbol}")
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
                "source": "yahoo:chart",
            }
        )
    if not rows:
        raise SyncWarning(f"Yahoo returned no usable price rows for {symbol}")
    return rows


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


def fetch_stooq_fx_rates(start: str, end: str) -> list[dict[str, Any]]:
    url = f"https://stooq.com/q/d/l/?s=usdcny&d1={tushare_date(start)}&d2={tushare_date(end)}&i=d"
    try:
        text = fetch_text(url)
    except SyncWarning as exc:
        raise SyncWarning(f"Stooq FX fetch failed for USDCNY: {exc}") from exc
    rows = []
    for row in csv.DictReader(StringIO(text)):
        close = row.get("Close")
        if close and close.lower() != "null":
            rows.append({"pair": "USD/CNY", "trade_date": row["Date"], "rate": float(close), "source": "stooq:usdcny"})
    if not rows:
        raise SyncWarning("Stooq returned no rows for USDCNY")
    return rows


def fetch_yahoo_fx_rates(start: str, end: str) -> list[dict[str, Any]]:
    rows = fetch_yahoo_prices("CNY=X", start, end, "CNY")
    return [{"pair": "USD/CNY", "trade_date": row["trade_date"], "rate": row["close"], "source": "yahoo:CNY=X"} for row in rows]


def fetch_frankfurter_fx_rates(start: str, end: str) -> list[dict[str, Any]]:
    url = f"https://api.frankfurter.app/{start}..{end}?from=USD&to=CNY"
    try:
        body = json.loads(fetch_text(url, timeout=10))
    except (json.JSONDecodeError, SyncWarning) as exc:
        raise SyncWarning(f"Frankfurter FX fetch failed for USD/CNY: {exc}") from exc
    rates = body.get("rates") or {}
    rows = [
        {"pair": "USD/CNY", "trade_date": trade_date, "rate": float(values["CNY"]), "source": "frankfurter:USD-CNY"}
        for trade_date, values in sorted(rates.items())
        if values.get("CNY") is not None
    ]
    if not rows:
        raise SyncWarning("Frankfurter returned no USD/CNY rows")
    return rows


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
) -> dict[str, Any]:
    started_at = time.perf_counter()
    assets = assets or DEFAULT_ASSETS
    warnings: list[str] = []
    missing_data: list[str] = []
    plan = _sync_plan(missing_items, assets, repo_symbol)
    logger.info(
        "sync_all start range=%s..%s missing_items=%s plan_prices=%s plan_dividends=%s plan_index=%s plan_repo=%s plan_fx=%s",
        start,
        end,
        missing_items or ["all"],
        sorted(plan["asset_prices"]),
        sorted(plan["asset_dividends"]),
        plan["index_prices"],
        sorted(plan["repo_symbols"]),
        plan["fx_rates"],
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
    conn.execute("DELETE FROM fx_rates WHERE pair='USD/CNY' AND trade_date BETWEEN ? AND ? AND source LIKE 'generated:%'", (start, end))
    conn.execute("DELETE FROM repo_rates WHERE symbol=? AND trade_date BETWEEN ? AND ? AND source LIKE 'generated:%'", (repo_symbol, start, end))
    if repo_symbol != "204001":
        conn.execute("DELETE FROM repo_rates WHERE symbol=? AND trade_date BETWEEN ? AND ? AND source LIKE 'generated:%'", ("204001", start, end))

    inserted = {"prices": 0, "dividends": 0, "adj_factors": 0, "repo_rates": 0, "fx_rates": 0}
    price_range_func = missing_date_ranges if plan["full"] else missing_tail_date_ranges
    cn_data_end = effective_price_end_for_market("CN", end)
    cn_data_end_text = cn_data_end.isoformat()
    asset_price_ranges: dict[str, list[tuple[str, str]]] = {}
    asset_dividend_ranges: dict[str, list[tuple[str, str]]] = {}
    for asset in assets:
        symbol = asset["symbol"]
        inception = asset.get("inception_date") or start
        fetch_start_date = max(parse_date(start), parse_date(inception))
        price_end = effective_price_end_for_asset(asset, end)
        fetch_start = fetch_start_date.isoformat()
        asset_price_ranges[symbol] = (
            price_range_func(conn, "prices", "symbol", symbol, "trade_date", fetch_start, price_end.isoformat())
            if symbol in plan["asset_prices"] and fetch_start_date <= price_end
            else []
        )
        asset_dividend_ranges[symbol] = (
            missing_coverage_ranges(conn, "dividends", symbol, fetch_start, end)
            if symbol in plan["asset_dividends"]
            else []
        )

    def fetch_asset_bundle(asset: dict[str, Any]) -> dict[str, Any]:
        asset_started_at = time.perf_counter()
        asset_warnings: list[str] = []
        asset_missing: list[str] = []
        symbol = asset["symbol"]
        price_ranges = asset_price_ranges[symbol]
        dividend_ranges = asset_dividend_ranges[symbol]
        prices: list[dict[str, Any]] = []
        tushare_asset_available = True
        for range_start, range_end in price_ranges:
            range_prices: list[dict[str, Any]] = []
            try:
                if not allow_network:
                    raise SyncWarning("network disabled for deterministic sync")
                if asset.get("market") == "CN":
                    range_prices = fetch_cn_fund_prices(token, symbol, range_start, range_end)
                else:
                    range_prices = fetch_yahoo_prices(symbol, range_start, range_end, asset.get("currency", "USD"))
            except SyncWarning as exc:
                asset_warnings.append(str(exc))
                if asset.get("market") == "CN":
                    tushare_asset_available = False
                if allow_network and asset.get("market") == "CN":
                    try:
                        range_prices = merge_rows_by_trade_date(fetch_datasrc_market_prices(symbol, range_start, range_end, "CNY"), range_prices)
                    except SyncWarning as datasrc_exc:
                        asset_warnings.append(str(datasrc_exc))
                if allow_network and asset.get("market") == "CN":
                    try:
                        range_prices = merge_rows_by_trade_date(range_prices, fetch_sohu_prices(symbol, range_start, range_end, "CNY", "sohu:hisHq"))
                    except SyncWarning as public_exc:
                        asset_warnings.append(str(public_exc))
                if allow_network and asset.get("market") == "CN" and not range_prices:
                    try:
                        range_prices = fetch_eastmoney_prices(symbol, range_start, range_end, "CNY", "eastmoney:fund_kline")
                    except SyncWarning as public_exc:
                        asset_warnings.append(str(public_exc))
                elif allow_network and not range_prices:
                    try:
                        range_prices = fetch_nasdaq_prices(symbol, range_start, range_end, asset.get("currency", "USD"))
                    except SyncWarning as public_exc:
                        asset_warnings.append(str(public_exc))
                if allow_network and asset.get("market") != "CN" and not range_prices:
                    try:
                        range_prices = fetch_stooq_prices(symbol, range_start, range_end)
                    except SyncWarning as public_exc:
                        asset_warnings.append(str(public_exc))
            if not range_prices:
                asset_missing.append(f"prices:{symbol}")
            prices = merge_rows_by_trade_date(prices, range_prices)
        dividends: list[dict[str, Any]] = []
        adj: list[dict[str, Any]] = []
        dividend_coverage: list[tuple[str, str, str, str]] = []
        for range_start, range_end in dividend_ranges:
            try:
                if not allow_network:
                    raise SyncWarning("network disabled for deterministic sync")
                if asset.get("market") == "CN":
                    range_dividends = fetch_fund_dividends(token, symbol, range_start, range_end)
                    dividend_source = "tushare:fund_div"
                else:
                    try:
                        range_dividends = fetch_yahoo_dividends(symbol, range_start, range_end, asset.get("currency", "USD"))
                        dividend_source = "yahoo:chart:dividend"
                    except SyncWarning as yahoo_exc:
                        asset_warnings.append(str(yahoo_exc))
                        range_dividends = fetch_digrin_dividends(symbol, range_start, range_end, asset.get("currency", "USD"))
                        dividend_source = "digrin:html:dividend"
                dividends.extend(range_dividends)
                dividend_coverage.append((symbol, range_start, range_end, dividend_source))
            except SyncWarning as exc:
                asset_warnings.append(str(exc))
                asset_missing.append(f"dividends:{symbol}")
        if asset.get("market") == "CN" and price_ranges:
            if allow_network and tushare_asset_available:
                for range_start, range_end in price_ranges:
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

    max_workers = min(max(len(assets), 1), 8)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_asset_bundle, asset) for asset in assets]
        for future in as_completed(futures):
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
            inserted["prices"] += insert_many(conn, "prices", bundle["prices"])
            inserted["dividends"] += insert_many(conn, "fund_dividends", bundle["dividends"])
            for coverage_symbol, coverage_start, coverage_end, coverage_source in bundle["dividend_coverage"]:
                mark_sync_coverage(conn, "dividends", coverage_symbol, coverage_start, coverage_end, coverage_source)
            inserted["adj_factors"] += insert_many(conn, "adj_factors", bundle["adj"])

    index_prices: list[dict[str, Any]] = []
    index_started_at = time.perf_counter()
    index_ranges = (
        price_range_func(conn, "prices", "symbol", "000300.SH", "trade_date", start, cn_data_end_text)
        if plan["index_prices"] and parse_date(start) <= cn_data_end
        else []
    )
    for range_start, range_end in index_ranges:
        range_prices: list[dict[str, Any]] = []
        try:
            if not allow_network:
                raise SyncWarning("network disabled for deterministic sync")
            range_prices = fetch_index_prices(token, "000300.SH", range_start, range_end)
        except SyncWarning as exc:
            warnings.append(str(exc))
            if allow_network:
                try:
                    range_prices = fetch_datasrc_market_prices("000300.SH", range_start, range_end, "CNY")
                except SyncWarning as datasrc_exc:
                    warnings.append(str(datasrc_exc))
            if allow_network:
                try:
                    range_prices = merge_rows_by_trade_date(range_prices, fetch_sohu_prices("000300.SH", range_start, range_end, "CNY", "sohu:hisHq"))
                except SyncWarning as public_exc:
                    warnings.append(str(public_exc))
            if allow_network and not range_prices:
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
        rows: list[dict[str, Any]] = []
        try:
            rows = fetch_datasrc_repo_rates(symbol, range_start, range_end)
        except SyncWarning as exc:
            warnings.append(str(exc))
        if not rows:
            try:
                rows = fetch_sohu_repo_rates(symbol, range_start, range_end)
            except SyncWarning as exc:
                warnings.append(str(exc))
        if not rows:
            try:
                rows = fetch_akshare_repo_rates(symbol, range_start, range_end)
            except SyncWarning as exc:
                warnings.append(str(exc))
        if not rows:
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
            price_range_func(conn, "repo_rates", "symbol", current_repo_symbol, "trade_date", start, cn_data_end_text)
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
    fx_missing_ranges = (
        price_range_func(conn, "fx_rates", "pair", "USD/CNY", "trade_date", start, cn_data_end_text)
        if plan["fx_rates"] and parse_date(start) <= cn_data_end
        else []
    )
    if not allow_network:
        warnings.append("network disabled for deterministic sync")
        if fx_missing_ranges:
            missing_data.append("fx_rates:USD/CNY")
    else:
        for range_start, range_end in fx_missing_ranges:
            range_fx_rows: list[dict[str, Any]] = []
            try:
                range_fx_rows = fetch_datasrc_fx_rates(range_start, range_end)
            except SyncWarning as exc:
                warnings.append(str(exc))
            if not range_fx_rows:
                try:
                    range_fx_rows = fetch_yahoo_fx_rates(range_start, range_end)
                except SyncWarning as exc:
                    warnings.append(str(exc))
            if not range_fx_rows:
                try:
                    range_fx_rows = fetch_frankfurter_fx_rates(range_start, range_end)
                except SyncWarning as public_exc:
                    warnings.append(str(public_exc))
            if not range_fx_rows:
                try:
                    range_fx_rows = fetch_stooq_fx_rates(range_start, range_end)
                except SyncWarning as public_exc:
                    warnings.append(str(public_exc))
            if not range_fx_rows:
                missing_data.append("fx_rates:USD/CNY")
            fx_rows = merge_rows_by_trade_date(fx_rows, range_fx_rows)
    if allow_network and fx_missing_ranges and not fx_rows:
        missing_data.append("fx_rates:USD/CNY")
    inserted["fx_rates"] += insert_many(conn, "fx_rates", fx_rows)
    if fx_missing_ranges:
        logger.info("sync fx complete ranges=%d rows=%d seconds=%.3f", len(fx_missing_ranges), len(fx_rows), time.perf_counter() - fx_started_at)

    result = {"inserted": inserted, "warnings": sorted(set(warnings)), "missing_data": sorted(set(missing_data))}
    logger.info("sync_all complete seconds=%.3f inserted=%s missing=%s warnings=%d", time.perf_counter() - started_at, inserted, result["missing_data"], len(result["warnings"]))
    return result
