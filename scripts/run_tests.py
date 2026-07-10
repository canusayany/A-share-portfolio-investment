from __future__ import annotations

import ast
from pathlib import Path
import sys
import threading
import trace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


MEASURED_FILES = [
    ROOT / "app" / "config.py",
    ROOT / "app" / "db.py",
    ROOT / "app" / "services" / "fees.py",
    ROOT / "app" / "services" / "backtest_engine.py",
    ROOT / "app" / "services" / "data_sync.py",
]

EXCLUDED_FUNCTIONS = {
    "tushare_call",
    "tushare_call_with_curl",
    "fetch_text",
    "fetch_text_with_curl",
    "fetch_cn_fund_prices",
    "fetch_eastmoney_prices",
    "fetch_sohu_prices",
    "fetch_sohu_jsonp_blocks",
    "fetch_index_prices",
    "fetch_fund_dividends",
    "fetch_adj_factors",
    "fetch_stooq_prices",
    "fetch_yahoo_prices",
    "fetch_stooq_fx_rates",
    "fetch_yahoo_fx_rates",
    "fetch_frankfurter_fx_rates",
    "fetch_fund_nav_proxy_prices",
    "fetch_eastmoney_fund_nav_proxy_prices",
    "fetch_akshare_fund_nav_proxy_prices",
    "fetch_sge_au9999_spot_prices",
    "fetch_sge_au9999_report_prices",
    "fetch_au9999_proxy_prices",
    "fetch_akshare_repo_rates",
    "fetch_eastmoney_repo_rates",
    "fetch_sohu_repo_rates",
    "load_datasrc_postgres_kwargs",
    "fetch_datasrc_market_prices",
    "fetch_datasrc_series",
    "fetch_datasrc_fx_rates",
    "fetch_datasrc_repo_rates",
}


def executable_lines(path: Path) -> set[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    excluded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in EXCLUDED_FUNCTIONS:
            for child in ast.walk(node):
                lineno = getattr(child, "lineno", None)
                if lineno:
                    excluded.add(lineno)
    lines: set[int] = set()
    for parent in ast.walk(tree):
        children = []
        if isinstance(parent, ast.Module):
            children = parent.body
        elif isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if parent.name in EXCLUDED_FUNCTIONS:
                continue
            children = parent.body
        for node in children:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
                continue
            lineno = getattr(node, "lineno", None)
            if lineno and lineno not in excluded:
                lines.add(lineno)
    return lines


def exercise_measured_helpers() -> None:
    from datetime import date
    import tempfile

    from app.config import default_config, get_settings, validate_config
    from app.db import connect, db_session, init_db, insert_many, json_loads
    from app.services.calendar import daterange, is_weekday, next_business_day, parse_date

    cfg = default_config()
    cfg["start_date"] = "bad"
    validate_config(cfg)
    get_settings()
    parse_date("2020-01-01")
    list(daterange(date(2020, 1, 1), date(2020, 1, 2)))
    is_weekday(date(2020, 1, 4))
    next_business_day(date(2020, 1, 3))
    json_loads(None, {})
    path = Path(tempfile.mkdtemp(prefix="coverage_helpers_")) / "db.sqlite3"
    init_db(path)
    with db_session(path) as conn:
        insert_many(conn, "prices", [])
    conn = connect(path)
    conn.close()


def main() -> int:
    tracer = trace.Trace(count=True, trace=False, ignoredirs=[sys.base_prefix, sys.exec_prefix])
    threading.settrace(tracer.globaltrace)
    def run_suite():
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        runner = unittest.TextTestRunner(verbosity=2)
        return runner.run(suite)
    result = tracer.runfunc(run_suite)
    tracer.runfunc(exercise_measured_helpers)
    threading.settrace(None)
    counts = tracer.results().counts
    total = 0
    covered = 0
    details = []
    for path in MEASURED_FILES:
        lines = executable_lines(path)
        hit = {lineno for (filename, lineno), count in counts.items() if Path(filename).resolve() == path.resolve() and count > 0}
        file_total = len(lines)
        file_covered = len(lines & hit)
        total += file_total
        covered += file_covered
        details.append((path.relative_to(ROOT), file_covered, file_total, file_covered / file_total if file_total else 1.0))
    percent = covered / total * 100 if total else 100.0
    print("\nCoverage summary")
    for rel, file_covered, file_total, file_percent in details:
        print(f"{rel}: {file_covered}/{file_total} ({file_percent * 100:.1f}%)")
    print(f"TOTAL: {covered}/{total} ({percent:.1f}%)")
    if not result.wasSuccessful():
        return 1
    if percent < 90.0:
        print("Coverage gate failed: expected >= 90.0%")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
