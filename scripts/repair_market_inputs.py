"""Prepare verified public market inputs locally, then apply them in one transaction."""

import argparse
from datetime import date
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DEFAULT_ASSETS
from app.db import db_session, init_db, insert_many
from app.services.data_sync import (
    fetch_money_fund_income, fetch_price_fallback_rows, fetch_tencent_hk_prices,
    fetch_verified_voo_dividends, mark_sync_coverage, upsert_dividend_rows, valid_hk_ohlc,
)
from app.services.fund_income import money_income_expected


def prepare(conn, end):
    asset = next(a for a in DEFAULT_ASSETS if a["symbol"] == "510300.SH")
    listing = asset["trade_start_date"]
    anchor = conn.execute("SELECT trade_date, close, source FROM prices WHERE symbol=? AND trade_date=?",
                          (asset["symbol"], listing)).fetchall()
    dates = [r[0] for r in conn.execute(
        "SELECT trade_date FROM prices WHERE symbol=? AND trade_date<? ORDER BY trade_date",
        (asset["symbol"], listing),
    )]
    if not dates or not anchor:
        raise ValueError("510300 上市前历史或上市日真实锚点缺失")
    nav = fetch_price_fallback_rows(asset, dates[0], dates[-1], [dict(r) for r in anchor])
    if not set(dates).issubset({r["trade_date"] for r in nav}):
        raise ValueError("160706 核对序列不能覆盖生产原有代理净值")
    original_dates = set(dates)
    nav = [r for r in nav if r["trade_date"] in original_dates]
    bad = conn.execute(
        """SELECT trade_date, open, close FROM prices WHERE symbol='03195.HK'
           AND (open<low OR open>high OR close<low OR close>high OR low>high)"""
    ).fetchall()
    hong_kong = []
    for cached in bad:
        day = cached["trade_date"]
        replacement = fetch_tencent_hk_prices("03195.HK", day, day, "HKD")
        candidates = [r for r in replacement if r["trade_date"] == day and valid_hk_ohlc(r)
                      and abs(r["close"] - cached["close"]) < 0.00001 and abs(r["open"] - cached["open"]) < 0.00001]
        if len(candidates) != 1:
            raise ValueError(f"03195 {day} 开盘与收盘不能跨来源确认")
        hong_kong.extend(candidates)
    income = fetch_money_fund_income("2013-01-28", end)
    missing = money_income_expected(conn, "2013-01-28", end) - {r["income_date"] for r in income}
    if missing:
        raise ValueError(f"511990 收益缺少 {sorted(missing)[:10]}")
    dividends = fetch_verified_voo_dividends("2010-09-07", end)
    return {"end": end, "proxy": nav, "hong_kong": hong_kong, "income": income, "dividends": dividends}


def apply(conn, bundle):
    for row in bundle["proxy"]:
        if row["symbol"] != "510300.SH" or row["trade_date"] >= "2012-05-28" or "fund_total_return:160706:" not in row["source"]:
            raise ValueError("invalid 160706 row")
    for row in bundle["hong_kong"]:
        if row["symbol"] != "03195.HK" or not valid_hk_ohlc(row):
            raise ValueError("invalid HK repaired quote")
    for row in bundle["income"]:
        if row["symbol"] != "511990.SH":
            raise ValueError("invalid money fund")
    for row in bundle["dividends"]:
        if row["symbol"] != "VOO" or row["pay_date"] < row["ex_date"]:
            raise ValueError("invalid VOO distribution")
    insert_many(conn, "prices", bundle["proxy"])
    insert_many(conn, "prices", bundle["hong_kong"])
    insert_many(conn, "money_fund_income", bundle["income"])
    upsert_dividend_rows(conn, bundle["dividends"])
    mark_sync_coverage(conn, "dividends", "VOO", "2010-09-07", bundle["end"], "vanguard:digrin_paid_history")
    for symbol, day in (("512100.SH", "2022-09-02"), ("512890.SH", "2021-10-22")):
        conn.execute("DELETE FROM prices WHERE symbol=? AND trade_date=? AND source LIKE '%:splice_scale_%'", (symbol, day))
    conn.execute("UPDATE backtest_runs SET config_hash=NULL WHERE config_hash IS NOT NULL")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True, type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prepare", type=Path, metavar="BUNDLE_JSON")
    group.add_argument("--apply", type=Path, metavar="BUNDLE_JSON")
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args()
    if args.prepare:
        if not args.end:
            parser.error("--prepare requires --end YYYY-MM-DD")
        with sqlite3.connect(args.db_path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            bundle = prepare(conn, args.end.isoformat())
        args.prepare.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    else:
        bundle = json.loads(args.apply.read_text(encoding="utf-8"))
        init_db(args.db_path)
        with db_session(args.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            apply(conn, bundle)
    print(json.dumps({k: len(v) if isinstance(v, list) else v for k, v in bundle.items()}))


if __name__ == "__main__":
    main()
