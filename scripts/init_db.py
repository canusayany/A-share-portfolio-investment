from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.db import backup_db, check_db, init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize or upgrade the portfolio SQLite database.")
    parser.add_argument("--db-path", type=Path, help="Database file (defaults to DATABASE_PATH or data/backtest.sqlite3).")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--check", action="store_true", help="Verify the existing schema and SQLite integrity without upgrading it.")
    operation.add_argument("--backup", type=Path, metavar="PATH", help="Back up an existing database to a new file before upgrading it.")
    args = parser.parse_args()
    db_path = get_settings(args.db_path).db_path.expanduser().absolute()
    try:
        if args.check:
            check_db(db_path)
            print(f"Verified {db_path}")
            return
        if args.backup is not None:
            backup_path = args.backup.expanduser().absolute()
            backup_db(db_path, backup_path)
            print(f"Backed up {db_path} to {backup_path}")
        init_db(db_path)
    except (OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Database operation failed for {db_path}: {exc}\n")
    print(f"Initialized {db_path}")


if __name__ == "__main__":
    main()
