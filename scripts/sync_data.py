from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import default_config, get_settings, normalize_config
from app.db import data_status, db_session, init_db
from app.services.data_sync import sync_all


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=default_config()["start_date"])
    parser.add_argument("--end", default=default_config()["end_date"])
    args = parser.parse_args()
    settings = get_settings()
    init_db(settings.db_path)
    config = normalize_config({"start_date": args.start, "end_date": args.end})
    with db_session(settings.db_path) as conn:
        result = sync_all(conn, settings.tushare_token, config["start_date"], config["end_date"], config["assets"], config["repo_symbol"])
        status = data_status(conn)
    print(result)
    print(status)


if __name__ == "__main__":
    main()
