from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
import time
from typing import Any, Iterable, Iterator

from app.identity import DEFAULT_LEADERBOARD_KEY_ID


DB_LOCK_TIMEOUT_SECONDS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
  symbol TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  asset_type TEXT NOT NULL,
  market TEXT NOT NULL,
  currency TEXT NOT NULL,
  inception_date TEXT,
  expense_ratio REAL,
  management_fee REAL,
  custodian_fee REAL,
  source TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prices (
  symbol TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  open REAL,
  high REAL,
  low REAL,
  close REAL NOT NULL,
  adj_close REAL,
  volume REAL,
  amount REAL,
  currency TEXT NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS fund_dividends (
  symbol TEXT NOT NULL,
  ann_date TEXT,
  record_date TEXT,
  ex_date TEXT,
  pay_date TEXT,
  div_cash REAL NOT NULL,
  currency TEXT NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY (symbol, ex_date, pay_date, div_cash)
);

CREATE TABLE IF NOT EXISTS adj_factors (
  symbol TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  adj_factor REAL NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS fx_rates (
  pair TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  rate REAL NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY (pair, trade_date)
);

CREATE TABLE IF NOT EXISTS repo_rates (
  symbol TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  open_rate REAL,
  close_rate REAL NOT NULL,
  high_rate REAL,
  low_rate REAL,
  volume REAL,
  amount REAL,
  source TEXT NOT NULL,
  PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS trading_calendar (
  market TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  is_open INTEGER NOT NULL,
  prev_trade_date TEXT,
  next_trade_date TEXT,
  PRIMARY KEY (market, trade_date)
);

CREATE TABLE IF NOT EXISTS sync_coverage (
  kind TEXT NOT NULL,
  symbol TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  source TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sync_coverage_kind_symbol_dates
ON sync_coverage(kind, symbol, start_date, end_date);

CREATE TABLE IF NOT EXISTS backtest_runs (
  run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  config_hash TEXT,
  config_json TEXT NOT NULL,
  summary_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_daily (
  run_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  total_asset_cny REAL NOT NULL,
  flow_cny REAL NOT NULL,
  daily_return REAL,
  cumulative_return REAL,
  drawdown REAL,
  benchmark_return REAL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (run_id, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_portfolio_daily_trade_date_run_id
ON portfolio_daily(trade_date, run_id);

CREATE TABLE IF NOT EXISTS trades (
  run_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  quantity REAL NOT NULL,
  price REAL NOT NULL,
  gross_amount REAL NOT NULL,
  fee REAL NOT NULL,
  currency TEXT NOT NULL,
  reason TEXT NOT NULL,
  payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_run_id_date
ON trades(run_id, trade_date);

CREATE TABLE IF NOT EXISTS rebalance_events (
  run_id TEXT NOT NULL,
  rebalance_date TEXT NOT NULL,
  period_return REAL,
  total_asset_before REAL,
  total_asset_after REAL,
  turnover_cny REAL,
  fee_cny REAL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (run_id, rebalance_date)
);

CREATE INDEX IF NOT EXISTS idx_rebalance_events_run_id_date
ON rebalance_events(run_id, rebalance_date);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=DB_LOCK_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def db_session(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _enable_wal(conn: sqlite3.Connection) -> None:
    deadline = time.monotonic() + DB_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL").fetchone()
            return
        except sqlite3.OperationalError as exc:
            error_code = getattr(exc, "sqlite_errorcode", 0) & 0xff
            if error_code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                raise
            # WAL promotion can return SQLITE_BUSY before busy_timeout when
            # several processes initialize a database in rollback journal mode.
            time.sleep(0.05)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
    ensure_schema_migrations(conn)


def init_db(db_path: str | Path) -> None:
    with db_session(db_path) as conn:
        _enable_wal(conn)
        _initialize_schema(conn)


def _open_database_read_only(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve(strict=True)
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=DB_LOCK_TIMEOUT_SECONDS)


def _schema_objects(conn: sqlite3.Connection, kind: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type=? AND substr(name, 1, 7) <> 'sqlite_'",
            (kind,),
        )
    }


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))}


def check_db(db_path: str | Path) -> None:
    with closing(_open_database_read_only(db_path)) as conn, closing(sqlite3.connect(":memory:")) as expected:
        expected.row_factory = sqlite3.Row
        with expected:
            _initialize_schema(expected)
        expected_tables = _schema_objects(expected, "table")
        missing_tables = expected_tables - _schema_objects(conn, "table")
        if missing_tables:
            raise sqlite3.DatabaseError(f"Missing tables: {', '.join(sorted(missing_tables))}")
        for table in sorted(expected_tables):
            missing_columns = _table_columns(expected, table) - _table_columns(conn, table)
            if missing_columns:
                raise sqlite3.DatabaseError(f"Missing columns in {table}: {', '.join(sorted(missing_columns))}")
        missing_indexes = _schema_objects(expected, "index") - _schema_objects(conn, "index")
        if missing_indexes:
            raise sqlite3.DatabaseError(f"Missing indexes: {', '.join(sorted(missing_indexes))}")
        integrity = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"SQLite quick_check failed: {integrity}")


def backup_db(db_path: str | Path, backup_path: str | Path) -> None:
    destination = Path(backup_path).expanduser()
    if os.path.lexists(destination):
        raise FileExistsError(f"Backup destination already exists: {destination}")
    with closing(_open_database_read_only(db_path)) as source:
        with NamedTemporaryFile(dir=destination.parent, prefix=".portfolio-db-backup-", suffix=".sqlite3", delete=False) as temp:
            staged = Path(temp.name)
        try:
            with closing(sqlite3.connect(staged, timeout=DB_LOCK_TIMEOUT_SECONDS)) as backup:
                last_progress = time.monotonic()

                def check_backup_progress(status: int, _remaining: int, _total: int) -> None:
                    nonlocal last_progress
                    now = time.monotonic()
                    if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        if now - last_progress >= DB_LOCK_TIMEOUT_SECONDS:
                            raise sqlite3.OperationalError("Timed out waiting for SQLite backup locks")
                    else:
                        last_progress = now

                source.backup(backup, pages=1024, progress=check_backup_progress, sleep=0.05)
            # Publish only the complete snapshot and never replace an existing
            # path, even if it was created while SQLite was copying the backup.
            if os.name == "nt":
                staged.rename(destination)
            else:
                os.link(staged, destination)
        finally:
            staged.unlink(missing_ok=True)


def ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_metadata (
          key_id TEXT NOT NULL,
          run_id TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
          name TEXT NOT NULL DEFAULT '' CHECK(length(name) <= 80),
          note TEXT NOT NULL DEFAULT '' CHECK(length(note) <= 1000),
          favorite INTEGER NOT NULL DEFAULT 0 CHECK(favorite IN (0, 1)),
          updated_at TEXT NOT NULL,
          PRIMARY KEY (key_id, run_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_run_metadata_favorites ON run_metadata(key_id, favorite, updated_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_run_metadata_run_id ON run_metadata(run_id)")
    backtest_run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(backtest_runs)").fetchall()}
    if "config_hash" not in backtest_run_cols:
        conn.execute("ALTER TABLE backtest_runs ADD COLUMN config_hash TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_backtest_runs_config_hash_created
        ON backtest_runs(config_hash, created_at DESC)
        """
    )
    leaderboard_memberships_existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='leaderboard_memberships'"
    ).fetchone()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leaderboard_memberships (
          key_id TEXT NOT NULL,
          run_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY (key_id, run_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_leaderboard_memberships_run_id
        ON leaderboard_memberships(run_id)
        """
    )
    if not leaderboard_memberships_existed:
        conn.execute(
            """
            INSERT OR IGNORE INTO leaderboard_memberships(key_id, run_id, created_at)
            SELECT ?, run_id, created_at FROM backtest_runs
            """,
            (DEFAULT_LEADERBOARD_KEY_ID,),
        )


def add_leaderboard_membership(conn: sqlite3.Connection, key_id: str, run_id: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO leaderboard_memberships(key_id, run_id, created_at)
        VALUES(?,?,?)
        """,
        (key_id, run_id, utc_now()),
    )


def get_run_metadata(conn: sqlite3.Connection, key_id: str, run_id: str) -> dict:
    row = conn.execute(
        "SELECT name,note,favorite FROM run_metadata WHERE key_id=? AND run_id=?", (key_id, run_id)
    ).fetchone()
    return {"name": row["name"] if row else "", "note": row["note"] if row else "", "favorite": bool(row["favorite"]) if row else False}


def update_run_metadata(conn: sqlite3.Connection, key_id: str, run_id: str, patch: dict) -> dict:
    if not patch or set(patch) - {"name", "note", "favorite"}:
        raise ValueError("metadata must contain name, note or favorite only")
    for field, limit in (("name", 80), ("note", 1000)):
        if field in patch and (not isinstance(patch[field], str) or len(patch[field]) > limit):
            raise ValueError(f"{field} must be a string of at most {limit} characters")
    if "favorite" in patch and not isinstance(patch["favorite"], bool):
        raise ValueError("favorite must be a boolean")
    metadata = {**get_run_metadata(conn, key_id, run_id), **patch}
    metadata["name"] = metadata["name"].strip()
    conn.execute(
        """INSERT INTO run_metadata(key_id,run_id,name,note,favorite,updated_at) VALUES(?,?,?,?,?,?)
           ON CONFLICT(key_id,run_id) DO UPDATE SET name=excluded.name,note=excluded.note,
             favorite=excluded.favorite,updated_at=excluded.updated_at""",
        (key_id, run_id, metadata["name"], metadata["note"], int(metadata["favorite"]), utc_now()),
    )
    return metadata


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def json_loads(data: str | None, default: Any = None) -> Any:
    if data is None:
        return default
    return json.loads(data)


def upsert_assets(conn: sqlite3.Connection, assets: list[dict[str, Any]]) -> None:
    now = utc_now()
    conn.executemany(
        """
        INSERT INTO assets(symbol, name, asset_type, market, currency, inception_date,
                           expense_ratio, management_fee, custodian_fee, source, updated_at)
        VALUES(:symbol, :name, :asset_type, :market, :currency, :inception_date,
               :expense_ratio, :management_fee, :custodian_fee, :source, :updated_at)
        ON CONFLICT(symbol) DO UPDATE SET
          name=excluded.name,
          asset_type=excluded.asset_type,
          market=excluded.market,
          currency=excluded.currency,
          inception_date=excluded.inception_date,
          expense_ratio=excluded.expense_ratio,
          management_fee=excluded.management_fee,
          custodian_fee=excluded.custodian_fee,
          source=excluded.source,
          updated_at=excluded.updated_at
        """,
        [
            {
                "symbol": item["symbol"],
                "name": item.get("name", item["symbol"]),
                "asset_type": item.get("asset_type", ""),
                "market": item.get("market", ""),
                "currency": item.get("currency", "CNY"),
                "inception_date": item.get("inception_date"),
                "expense_ratio": item.get("expense_ratio"),
                "management_fee": item.get("management_fee"),
                "custodian_fee": item.get("custodian_fee"),
                "source": item.get("source", "config"),
                "updated_at": now,
            }
            for item in assets
        ],
    )


def insert_many(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    keys = list(rows[0].keys())
    placeholders = ",".join(":" + key for key in keys)
    columns = ",".join(keys)
    conflict_target = {
        "prices": "(symbol, trade_date)",
        "fund_dividends": "(symbol, ex_date, pay_date, div_cash)",
        "adj_factors": "(symbol, trade_date)",
        "fx_rates": "(pair, trade_date)",
        "repo_rates": "(symbol, trade_date)",
        "trading_calendar": "(market, trade_date)",
    }.get(table)
    if conflict_target:
        updates = ",".join(f"{key}=excluded.{key}" for key in keys if key not in conflict_target)
        sql = (
            f"INSERT INTO {table}({columns}) VALUES({placeholders}) "
            f"ON CONFLICT{conflict_target} DO UPDATE SET {updates}"
        )
    else:
        sql = f"INSERT INTO {table}({columns}) VALUES({placeholders})"
    conn.executemany(sql, rows)
    return len(rows)


def data_status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    queries = [
        ("price", "prices", "symbol", "trade_date", "source"),
        ("dividend", "fund_dividends", "symbol", "ex_date", "source"),
        ("adj_factor", "adj_factors", "symbol", "trade_date", "source"),
        ("fx", "fx_rates", "pair", "trade_date", "source"),
        ("repo", "repo_rates", "symbol", "trade_date", "source"),
    ]
    status: list[dict[str, Any]] = []
    for kind, table, code_col, date_col, source_col in queries:
        rows = conn.execute(
            f"""
            SELECT '{kind}' AS kind, {code_col} AS symbol, MIN({date_col}) AS start_date,
                   MAX({date_col}) AS end_date, COUNT(*) AS rows,
                   GROUP_CONCAT(DISTINCT {source_col}) AS sources
            FROM {table}
            WHERE {source_col} NOT LIKE 'generated:%'
            GROUP BY {code_col}
            ORDER BY {code_col}
            """
        ).fetchall()
        status.extend(rows_to_dicts(rows))
    return status
