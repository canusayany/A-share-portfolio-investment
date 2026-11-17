from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.config import BASE_DIR, get_settings
from app.db import db_session, init_db
from scripts import sync_data


class DbSyncCliTests(unittest.TestCase):
    def test_sync_commits_partial_updates_and_invalidates_cache(self) -> None:
        with TemporaryDirectory() as temp:
            db_path = Path(temp) / "market history.sqlite3"
            init_db(db_path)
            with db_session(db_path) as conn:
                conn.execute(
                    "INSERT INTO backtest_runs(run_id,created_at,config_hash,config_json,summary_json) VALUES(?,?,?,?,?)",
                    ("old-run", "2020-01-03", "cached", "{}", "{}"),
                )

            def fetch(conn, token, start, end, assets, repo_symbol, *, missing_items):
                self.assertEqual((start, end, repo_symbol), ("2020-01-02", "2020-01-03", "204001"))
                self.assertIn("511090.SH", {asset["symbol"] for asset in assets})
                self.assertEqual(missing_items, ["repo_rates:204001", "prices:512890.SH"])
                conn.execute(
                    "INSERT INTO repo_rates(symbol,trade_date,close_rate,source) VALUES(?,?,?,?)",
                    ("204001", "2020-01-02", 2.54, "test:quote"),
                )
                return {"inserted": {"repo_rates": 1}, "warnings": ["price history unavailable"]}

            with (
                patch.object(sync_data, "required_data_missing", side_effect=[
                    ["repo_rates:204001", "prices:512890.SH"], ["prices:512890.SH"],
                ]),
                patch.object(sync_data, "sync_all", side_effect=fetch),
                redirect_stdout(StringIO()) as output,
            ):
                self.assertEqual(sync_data.main([
                    "--db-path", str(db_path), "--start", "2020-01-02", "--end", "2020-01-03",
                ]), 1)

            self.assertEqual(json.loads(output.getvalue())["missing_data"], ["prices:512890.SH"])
            with db_session(db_path) as conn:
                self.assertIsNone(conn.execute("SELECT config_hash FROM backtest_runs").fetchone()[0])
                self.assertEqual(conn.execute("SELECT close_rate FROM repo_rates").fetchone()[0], 2.54)

    def test_invalid_date_range_fails_before_opening_a_database(self) -> None:
        with patch.object(sync_data, "get_settings") as settings, redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as caught:
                sync_data.main(["--start", "2020-01-03", "--end", "2020-01-02"])
        self.assertEqual(caught.exception.code, 2)
        settings.assert_not_called()

    def test_environment_database_path_is_relative_to_repository(self) -> None:
        with (
            patch.dict(os.environ, {"DATABASE_PATH": "other data/market.sqlite3"}),
            patch("app.config.load_dotenv_if_present"),
        ):
            self.assertEqual(get_settings().db_path, (BASE_DIR / "other data/market.sqlite3").resolve())
            self.assertEqual(get_settings("cli.sqlite3").db_path, (Path.cwd() / "cli.sqlite3").resolve())
