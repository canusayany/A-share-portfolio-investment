from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.db import init_db


def main() -> None:
    settings = get_settings()
    init_db(settings.db_path)
    print(f"Initialized {settings.db_path}")


if __name__ == "__main__":
    main()
