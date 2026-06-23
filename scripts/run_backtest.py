from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.db import db_session, init_db
from app.services.backtest_engine import run_backtest


def main() -> None:
    settings = get_settings()
    init_db(settings.db_path)
    with db_session(settings.db_path) as conn:
        result = run_backtest(conn)
    print(result)


if __name__ == "__main__":
    main()
