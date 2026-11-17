"""Evidence for historical exchange-repo sessions with no reported quote."""
from __future__ import annotations

from datetime import datetime, timedelta
import math
import re
from zoneinfo import ZoneInfo

from app.services.calendar import daterange, parse_date


REPO_ABSENCE_KIND = "repo_no_quote"
REPO_ABSENCE_SOURCE = "tushare:repo_daily:market_snapshot"
REPO_SNAPSHOT_ROW_LIMIT = 2000
REPO_ABSENCE_MIN_AGE_DAYS = 7


def _repo_ts_code(symbol: str) -> str | None:
    if not isinstance(symbol, str) or not re.fullmatch(r"(?:204|131)\d{3}", symbol):
        return None
    return f"{symbol}.{'SH' if symbol.startswith('204') else 'SZ'}"


def _historical_cutoff():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=REPO_ABSENCE_MIN_AGE_DAYS)


def fetch_verified_repo_absences(
    token: str, symbol: str, start: str, end: str, expected_dates: set[str],
) -> set[str]:
    """Prove old missing quotes using complete, unfiltered market snapshots.

    An absent selected contract is meaningful only on a date where the same
    response contains a valid 204001.SH quote. Nothing is returned if any
    requested snapshot is unavailable, malformed or possibly truncated.
    """
    # data_sync imports these helpers, so its transport/error types are loaded
    # only when a request is made, after module initialization has completed.
    from app.services.data_sync import RepoSyncTimedOut, SyncCancelled, SyncWarning, tushare_call

    if symbol == "204001":
        return set()
    selected_code = _repo_ts_code(symbol)
    if selected_code is None:
        raise SyncWarning(f"unsupported repo history symbol {symbol}")
    try:
        first, last = parse_date(start), min(parse_date(end), _historical_cutoff())
        expected = {parse_date(day).isoformat() for day in expected_dates}
    except (TypeError, ValueError) as exc:
        raise SyncWarning("invalid repo absence request dates") from exc
    eligible = {day for day in expected if first.isoformat() <= day <= last.isoformat()}
    if first > last or not eligible:
        return set()

    absences: set[str] = set()
    cursor = first
    while cursor <= last:
        chunk_end = min(cursor + timedelta(days=29), last)
        chunk_dates = {day for day in eligible if cursor.isoformat() <= day <= chunk_end.isoformat()}
        if chunk_dates:
            try:
                rows = tushare_call(
                    token, "repo_daily",
                    {"start_date": cursor.strftime("%Y%m%d"), "end_date": chunk_end.strftime("%Y%m%d")},
                    "ts_code,trade_date,close",
                )
            except (RepoSyncTimedOut, SyncCancelled):
                raise
            except Exception as exc:
                raise SyncWarning(f"repo market snapshot unavailable for {cursor}..{chunk_end}: {type(exc).__name__}") from exc
            if not isinstance(rows, list) or not rows or len(rows) >= REPO_SNAPSHOT_ROW_LIMIT:
                raise SyncWarning(f"empty, invalid or truncated repo market snapshot for {cursor}..{chunk_end}")
            anchors: set[str] = set()
            selected: set[str] = set()
            observed: dict[tuple[str, str], float] = {}
            for row in rows:
                try:
                    if not isinstance(row, dict):
                        raise ValueError("row is not an object")
                    code, raw_day, raw_close = row["ts_code"], row["trade_date"], row["close"]
                    if not isinstance(code, str) or not code.strip() or code != code.strip():
                        raise ValueError("missing contract identity")
                    if not isinstance(raw_day, str) or not re.fullmatch(r"(?:\d{8}|\d{4}-\d{2}-\d{2})", raw_day):
                        raise ValueError("invalid trade date")
                    day = parse_date(
                        f"{raw_day[:4]}-{raw_day[4:6]}-{raw_day[6:]}" if len(raw_day) == 8 else raw_day
                    )
                    if not cursor <= day <= chunk_end:
                        raise ValueError("quote outside snapshot interval")
                    if raw_close is None or isinstance(raw_close, bool):
                        raise ValueError("missing rate")
                    close = float(raw_close)
                    if not math.isfinite(close):
                        raise ValueError("non-finite rate")
                    day_text = day.isoformat()
                    key = (code, day_text)
                    if key in observed and observed[key] != close:
                        raise ValueError("conflicting duplicate quotes")
                    observed[key] = close
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    raise SyncWarning(f"invalid repo market snapshot for {cursor}..{chunk_end}: {exc}") from exc
                if code == "204001.SH":
                    anchors.add(day_text)
                if code == selected_code:
                    selected.add(day_text)
            absences.update(chunk_dates & anchors - selected)
        cursor = chunk_end + timedelta(days=1)
    return absences


def verified_repo_absence_dates(conn, symbol: str, start: str, end: str) -> set[str]:
    """Read only trusted historical proofs; real quotes always take priority."""
    if symbol == "204001" or _repo_ts_code(symbol) is None:
        return set()
    first, last = parse_date(start), min(parse_date(end), _historical_cutoff())
    if first > last:
        return set()
    rows = conn.execute(
        "SELECT start_date,end_date FROM sync_coverage WHERE kind=? AND symbol=? AND source=? "
        "AND end_date>=? AND start_date<=?",
        (REPO_ABSENCE_KIND, symbol, REPO_ABSENCE_SOURCE, first.isoformat(), last.isoformat()),
    ).fetchall()
    verified: set[str] = set()
    for row in rows:
        try:
            range_start = max(first, parse_date(row["start_date"]))
            range_end = min(last, parse_date(row["end_date"]))
        except (TypeError, ValueError):
            continue
        verified.update(day.isoformat() for day in daterange(range_start, range_end))
    quotes = conn.execute(
        "SELECT trade_date FROM repo_rates WHERE symbol=? AND trade_date BETWEEN ? AND ? "
        "AND source NOT LIKE 'generated:%'",
        (symbol, first.isoformat(), last.isoformat()),
    ).fetchall()
    return verified - {row["trade_date"] for row in quotes}
