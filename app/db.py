from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;

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
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
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


def init_db(db_path: str | Path) -> None:
    with db_session(db_path) as conn:
        conn.executescript(SCHEMA)
        ensure_schema_migrations(conn)


def ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_coverage (
          kind TEXT NOT NULL,
          symbol TEXT NOT NULL,
          start_date TEXT NOT NULL,
          end_date TEXT NOT NULL,
          source TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sync_coverage_kind_symbol_dates
        ON sync_coverage(kind, symbol, start_date, end_date)
        """
    )
    backtest_run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(backtest_runs)").fetchall()}
    if "config_hash" not in backtest_run_cols:
        conn.execute("ALTER TABLE backtest_runs ADD COLUMN config_hash TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_backtest_runs_config_hash_created
        ON backtest_runs(config_hash, created_at DESC)
        """
    )


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
