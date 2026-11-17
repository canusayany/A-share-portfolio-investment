from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from app.db import backup_db, check_db, init_db
from app.identity import DEFAULT_LEADERBOARD_KEY_ID


INIT_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "init_db.py"


class DatabaseInstallTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="portfolio-db-install-")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.db_path = self.folder / "组合 #1.sqlite3"

    def cli(self, *args, database_path=None):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "DATABASE_PATH": str(database_path or self.db_path)}
        return subprocess.run(
            [sys.executable, str(INIT_SCRIPT), *map(str, args)],
            cwd=self.folder,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=12,
        )

    def seed_legacy(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "CREATE TABLE backtest_runs (run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
                "config_json TEXT NOT NULL, summary_json TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO backtest_runs VALUES ('legacy', '2020-01-01', '{}', '{}')")

    def test_cli_explicit_path_overrides_environment_and_is_repeatable(self):
        other_path = self.folder / "untouched.sqlite3"
        for _ in range(2):
            result = self.cli("--db-path", self.db_path, database_path=other_path)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(other_path.exists())
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        before = self.db_path.stat().st_mtime_ns
        result = self.cli("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.db_path.stat().st_mtime_ns, before)

    def test_check_fails_without_creating_or_upgrading_the_database(self):
        missing = self.cli("--check")
        self.assertNotEqual(missing.returncode, 0)
        self.assertFalse(self.db_path.exists())
        self.seed_legacy()
        incomplete = self.cli("--check")
        self.assertNotEqual(incomplete.returncode, 0)
        self.assertIn("Missing tables:", incomplete.stderr)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("backtest_runs",)])

    def test_check_detects_sqlite_inconsistency_with_complete_schema(self):
        init_db(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn:
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            root_page = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='prices'").fetchone()[0]
        # Damage a data B-tree page without changing its sqlite_master entry.
        with self.db_path.open("r+b") as database:
            database.seek((root_page - 1) * page_size)
            database.write(b"\x00")
        result = self.cli("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SQLite quick_check failed:", result.stderr)

    def test_interrupted_migration_rolls_back_ddl_and_backfill(self):
        self.seed_legacy()
        from app import db
        original = db.ensure_schema_migrations

        def interrupt(conn):
            original(conn)
            raise RuntimeError("interrupted")

        with patch("app.db.ensure_schema_migrations", side_effect=interrupt), self.assertRaisesRegex(RuntimeError, "interrupted"):
            init_db(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertNotIn("config_hash", {row[1] for row in conn.execute("PRAGMA table_info(backtest_runs)")})
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='leaderboard_memberships'").fetchone())
            self.assertEqual(conn.execute("SELECT run_id FROM backtest_runs").fetchall(), [("legacy",)])
        init_db(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT key_id, run_id FROM leaderboard_memberships").fetchall(),
                             [(DEFAULT_LEADERBOARD_KEY_ID, "legacy")])

    def test_concurrent_initializers_share_one_legacy_upgrade(self):
        self.seed_legacy()
        gate = Barrier(8, timeout=5)

        def initialize():
            gate.wait()
            init_db(self.db_path)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(initialize) for _ in range(8)]
            for future in futures:
                future.result(timeout=10)
        check_db(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT key_id, run_id FROM leaderboard_memberships").fetchall(),
                             [(DEFAULT_LEADERBOARD_KEY_ID, "legacy")])

    def test_backup_precedes_upgrade_and_refuses_existing_target(self):
        self.seed_legacy()
        target = self.folder / "before upgrade.sqlite3"
        result = self.cli("--backup", target)
        self.assertEqual(result.returncode, 0, result.stderr)
        check_db(self.db_path)
        with closing(sqlite3.connect(target)) as conn:
            self.assertEqual(conn.execute("SELECT run_id FROM backtest_runs").fetchall(), [("legacy",)])
            self.assertNotIn("config_hash", {row[1] for row in conn.execute("PRAGMA table_info(backtest_runs)")})
        previous = target.read_bytes()
        repeated = self.cli("--backup", target)
        self.assertNotEqual(repeated.returncode, 0)
        self.assertEqual(target.read_bytes(), previous)

    def test_backup_includes_committed_wal_data(self):
        init_db(self.db_path)
        target = self.folder / "backup.sqlite3"
        with closing(sqlite3.connect(self.db_path)) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "INSERT INTO backtest_runs(run_id,created_at,config_json,summary_json) "
                "VALUES ('from-wal', '2020-01-01', '{}', '{}')"
            )
            writer.commit()
            self.assertGreater(Path(str(self.db_path) + "-wal").stat().st_size, 0)
            backup_db(self.db_path, target)
        with closing(sqlite3.connect(target)) as conn:
            self.assertEqual(conn.execute("SELECT run_id FROM backtest_runs").fetchall(), [("from-wal",)])

    def test_backup_failure_cleans_destination(self):
        target = self.folder / "backup.sqlite3"
        with patch("app.db._open_database_read_only") as source:
            source.return_value.backup.side_effect = sqlite3.OperationalError("copy failed")
            with self.assertRaisesRegex(sqlite3.OperationalError, "copy failed"):
                backup_db(self.db_path, target)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_backup_times_out_only_when_waiting_for_locks(self):
        target = self.folder / "backup.sqlite3"

        def blocked(_backup, *, pages, progress, sleep):
            progress(sqlite3.SQLITE_OK, 2, 3)
            progress(sqlite3.SQLITE_BUSY, 2, 3)

        with patch("app.db._open_database_read_only") as source, patch("app.db.time.monotonic", side_effect=[0, 100, 131]):
            source.return_value.backup.side_effect = blocked
            with self.assertRaisesRegex(sqlite3.OperationalError, "Timed out waiting"):
                backup_db(self.db_path, target)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_backup_publish_preserves_target_created_during_copy(self):
        target = self.folder / "backup.sqlite3"
        with patch("app.db._open_database_read_only") as source:
            source.return_value.backup.side_effect = lambda *_args, **_kwargs: target.write_bytes(b"other backup")
            with self.assertRaises(FileExistsError):
                backup_db(self.db_path, target)
        self.assertEqual(list(self.folder.iterdir()), [target])
        self.assertEqual(target.read_bytes(), b"other backup")

    def test_backup_refuses_dangling_symlink(self):
        self.seed_legacy()
        missing = self.folder / "missing.sqlite3"
        target = self.folder / "backup-link.sqlite3"
        try:
            target.symlink_to(missing)
        except OSError:
            self.skipTest("Creating symbolic links is unavailable")
        with self.assertRaises(FileExistsError):
            backup_db(self.db_path, target)
        self.assertTrue(target.is_symlink())
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
