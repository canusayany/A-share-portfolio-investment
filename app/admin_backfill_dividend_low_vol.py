from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.config import default_config
from app.db import db_session, insert_many
from app.services.data_sync import fetch_price_fallback_rows


PROXY_START = "2005-12-30"
PROXY_END = "2019-01-17"
TARGET_SYMBOL = "512890.SH"


def dividend_low_vol_asset() -> dict[str, Any]:
    return next(asset for asset in default_config()["assets"] if asset["symbol"] == TARGET_SYMBOL)


def backfill(db_path: str | Path) -> dict[str, Any]:
    asset = dividend_low_vol_asset()
    with db_session(db_path) as conn:
        genuine_etf_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT trade_date, close, source FROM prices
                WHERE symbol=? AND trade_date>=? AND source NOT LIKE 'generated:%'
                  AND source NOT LIKE '%:splice_%'
                ORDER BY trade_date
                """,
                (TARGET_SYMBOL, asset["trade_start_date"]),
            )
        ]
    if not genuine_etf_rows:
        raise RuntimeError("cannot backfill H20269 without a genuine 512890 ETF price anchor")

    rows = fetch_price_fallback_rows(
        asset,
        PROXY_START,
        PROXY_END,
        genuine_etf_rows,
    )
    if len(rows) < 3_000:
        raise RuntimeError(f"official H20269 response is unexpectedly short: {len(rows)} rows")
    if rows[0]["trade_date"] != PROXY_START or rows[-1]["trade_date"] != PROXY_END:
        raise RuntimeError(
            "official H20269 boundaries are incomplete: "
            f"{rows[0]['trade_date']}..{rows[-1]['trade_date']}"
        )
    if any(not str(row["source"]).startswith("csindex:index_perf:splice_scale_") for row in rows):
        raise RuntimeError("backfill contains a non-official or unscaled index row")

    with db_session(db_path) as conn:
        inserted = insert_many(conn, "prices", rows)
        conn.execute("UPDATE backtest_runs SET config_hash=NULL")
        proxy_status = dict(
            conn.execute(
                """
                SELECT MIN(trade_date) AS start_date, MAX(trade_date) AS end_date,
                       COUNT(*) AS rows, GROUP_CONCAT(DISTINCT source) AS sources
                FROM prices
                WHERE symbol=? AND trade_date BETWEEN ? AND ?
                  AND source LIKE 'csindex:index_perf:splice_scale_%'
                """,
                (TARGET_SYMBOL, PROXY_START, PROXY_END),
            ).fetchone()
        )
    return {"symbol": TARGET_SYMBOL, "upserted": inserted, **proxy_status}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill the complete official H20269 total-return history used by 512890."
    )
    parser.add_argument("--db-path", default="data/backtest.sqlite3")
    args = parser.parse_args()
    print(json.dumps(backfill(args.db_path), ensure_ascii=False))


if __name__ == "__main__":
    main()
