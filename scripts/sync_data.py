from __future__ import annotations

import argparse
from datetime import date
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import backtest_assets, default_config, get_settings, normalize_config, repo_rate_symbol
from app.db import data_status, db_session, init_db
from app.services.data_sync import required_data_missing, sync_all


def main(argv: list[str] | None = None) -> int:
    defaults = default_config()
    parser = argparse.ArgumentParser(description="Fill market-data gaps for the default portfolio configuration.")
    parser.add_argument("--db-path", type=Path, help="SQLite database path")
    parser.add_argument("--start", type=date.fromisoformat, default=defaults["start_date"])
    parser.add_argument("--end", type=date.fromisoformat, default=defaults["end_date"])
    args = parser.parse_args(argv)
    if args.end < args.start:
        parser.error("--end must be on or after --start")
    start, end = args.start.isoformat(), args.end.isoformat()
    settings = get_settings(args.db_path)
    init_db(settings.db_path)
    config = normalize_config({"start_date": start, "end_date": end})
    assets = backtest_assets(config)
    repo_symbol = repo_rate_symbol(config)
    result = {"inserted": {}, "warnings": []}
    with db_session(settings.db_path) as conn:
        missing_before = required_data_missing(conn, start, end, assets, repo_symbol)
        if missing_before:
            result = sync_all(
                conn, settings.tushare_token, start, end, assets, repo_symbol, missing_items=missing_before,
            )
            if any(result["inserted"].values()) or any(item.startswith("generated:") for item in missing_before):
                conn.execute("UPDATE backtest_runs SET config_hash=NULL WHERE config_hash IS NOT NULL")
    with db_session(settings.db_path) as conn:
        missing_after = required_data_missing(conn, start, end, assets, repo_symbol)
        status = data_status(conn)
    print(json.dumps({
        "database": str(settings.db_path),
        "start_date": start,
        "end_date": end,
        "inserted": result["inserted"],
        "warnings": result["warnings"],
        "missing_before": missing_before,
        "missing_data": missing_after,
        "status": status,
    }, ensure_ascii=False))
    return 1 if missing_after else 0


if __name__ == "__main__":
    sys.exit(main())
