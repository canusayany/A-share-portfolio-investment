from __future__ import annotations

from pathlib import Path
import math
import tempfile

from app.config import asset_price_start_date, backtest_assets, normalize_config, repo_rate_symbol
from app.db import db_session, init_db, insert_many, upsert_assets
from app.services.calendar import business_days
from app.services.data_sync import mark_sync_coverage


def temp_db_path() -> Path:
    return Path(tempfile.mkdtemp(prefix="portfolio_backtest_")) / "test.sqlite3"


def build_synced_db(start: str = "2020-01-01", end: str = "2020-03-31") -> tuple[Path, dict]:
    db_path = temp_db_path()
    init_db(db_path)
    config = normalize_config({"start_date": start, "end_date": end})
    with db_session(db_path) as conn:
        seed_fixture_data(conn, config, start, end)
    return db_path, config


def fixture_price_series(symbol: str, start: str, end: str, currency: str, seed: float) -> list[dict]:
    rows = []
    for idx, day in enumerate(business_days(start, end)):
        close = max(seed * (1 + 0.00012 * idx + 0.01 * math.sin(idx / 17 + seed)), 0.05)
        rows.append(
            {
                "symbol": symbol,
                "trade_date": day.isoformat(),
                "open": round(close * 0.998, 6),
                "high": round(close * 1.006, 6),
                "low": round(close * 0.994, 6),
                "close": round(close, 6),
                "adj_close": round(close, 6),
                "volume": 1000000.0,
                "amount": 0.0,
                "currency": currency,
                "source": "fixture:price",
            }
        )
    return rows


def fixture_repo_rates(start: str, end: str, symbol: str = "204001") -> list[dict]:
    rows = []
    for idx, day in enumerate(business_days(start, end)):
        rate = 1.7 + 0.5 * math.sin(idx / 23)
        rows.append(
            {
                "symbol": symbol,
                "trade_date": day.isoformat(),
                "open_rate": round(rate, 4),
                "close_rate": round(rate, 4),
                "high_rate": round(rate + 0.2, 4),
                "low_rate": round(max(rate - 0.2, 0.01), 4),
                "volume": 0.0,
                "amount": 0.0,
                "source": "fixture:repo",
            }
        )
    return rows


def fixture_fx_rates(start: str, end: str) -> list[dict]:
    return [
        {"pair": "USD/CNY", "trade_date": day.isoformat(), "rate": round(6.8 + 0.05 * math.sin(idx / 50), 6), "source": "fixture:fx"}
        for idx, day in enumerate(business_days(start, end))
    ]


def fixture_hkd_fx_rates(start: str, end: str) -> list[dict]:
    return [
        {"pair": "HKD/CNY", "trade_date": day.isoformat(), "rate": round(0.88 + 0.01 * math.sin(idx / 60), 6), "source": "fixture:fx"}
        for idx, day in enumerate(business_days(start, end))
    ]


def fixture_dividends(symbol: str, start: str, end: str, currency: str) -> list[dict]:
    rows = []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        ex_date = f"{year}-06-22"
        if start <= ex_date <= end:
            rows.append(
                {
                    "symbol": symbol,
                    "ann_date": ex_date,
                    "record_date": ex_date,
                    "ex_date": ex_date,
                    "pay_date": f"{year}-06-25",
                    "div_cash": 0.02 if currency == "CNY" else 1.2,
                    "currency": currency,
                    "source": "fixture:dividend",
                }
            )
    return rows


def seed_fixture_data(conn, config: dict, start: str, end: str) -> None:
    assets = backtest_assets(config)
    upsert_assets(conn, [{**asset, "source": "fixture"} for asset in assets])
    upsert_assets(
        conn,
        [
            {
                "symbol": "000300.SH",
                "name": "沪深300指数",
                "asset_type": "benchmark",
                "market": "CN",
                "currency": "CNY",
                "source": "fixture",
            }
        ],
    )
    seeds = {
        "VOO": 280.0,
        "03195.HK": 8.0,
        "513500.SH": 1.0,
        "512890.SH": 1.0,
        "510300.SH": 3.0,
        "159631.SZ": 1.5,
        "510500.SH": 2.0,
        "512100.SH": 1.8,
        "518880.SH": 2.5,
        "518850.SH": 4.0,
        "CBA03101": 100.0,
        "CBA06501": 100.0,
        "CBA21801": 100.0,
        "511990.SH": 100.0,
        "000300.SH": 3500.0,
    }
    for asset in assets:
        fetch_start = max(start, asset_price_start_date(asset, start))
        insert_many(conn, "prices", fixture_price_series(asset["symbol"], fetch_start, end, asset["currency"], seeds[asset["symbol"]]))
        if asset.get("asset_type") != "money_fund":
            dividend_start = max(start, asset.get("inception_date") or start)
            insert_many(conn, "fund_dividends", fixture_dividends(asset["symbol"], dividend_start, end, asset["currency"]))
            mark_sync_coverage(conn, "dividends", asset["symbol"], dividend_start, end, "fixture:dividend")
        if asset["market"] == "CN":
            insert_many(
                conn,
                "adj_factors",
                [
                    {"symbol": row["symbol"], "trade_date": row["trade_date"], "adj_factor": 1.0, "source": "fixture:adj"}
                    for row in fixture_price_series(asset["symbol"], fetch_start, end, asset["currency"], seeds[asset["symbol"]])
                ],
            )
    insert_many(conn, "prices", fixture_price_series("000300.SH", start, end, "CNY", seeds["000300.SH"]))
    insert_many(conn, "repo_rates", fixture_repo_rates(start, end, repo_rate_symbol(config)))
    insert_many(conn, "fx_rates", fixture_fx_rates(start, end))
    insert_many(conn, "fx_rates", fixture_hkd_fx_rates(start, end))
