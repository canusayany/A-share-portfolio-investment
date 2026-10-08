from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
import gc
import gzip
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import inspect
import json
import logging
import math
import mimetypes
import os
from pathlib import Path
from threading import Event, Lock, Thread
import time
from urllib.parse import parse_qs, urlparse
import uuid

from app.config import backtest_assets, repo_rate_symbol, STATIC_DIR, default_config, get_settings, normalize_config, validate_config
from app.db import add_leaderboard_membership, data_status, db_session, get_run_metadata, init_db, json_dumps, json_loads, rows_to_dicts, update_run_metadata
from app.identity import (
    DEFAULT_LEADERBOARD_KEY_ID,
    IDENTITY_COOKIE_MAX_AGE_SECONDS,
    IDENTITY_COOKIE_NAME,
    leaderboard_key_id,
    valid_leaderboard_key_id,
)
from app.services.backtest_engine import (
    BACKTEST_ENGINE_VERSION,
    asset_comovement_statistics,
    BacktestCancelled,
    BacktestError,
    RANKING_VERSION,
    ranking_metrics,
    canonical_config_hash,
    get_cached_backtest_run,
    rolling_window_ranges,
    run_backtest,
)
from app.services.data_sync import SyncCancelled, required_data_missing, sync_all
from app.services.calendar import market_business_days, repo_actual_days, repo_year_basis
from app.services.strategy_diagnostics import build_backtest_csv, strategy_diagnostics
from app.services.research import run_research, validate_research_request

logger = logging.getLogger(__name__)
MAX_LOG_BYTES = 5 * 1024 * 1024
DEFAULT_ABANDONED_JOB_SECONDS = 120.0
DEFAULT_JOB_RETENTION_SECONDS = 600.0
JOB_CLEANUP_INTERVAL_SECONDS = 5.0
CANCELLED_JOB_MESSAGE = "任务已取消：页面没有继续请求结果"
TIME_RANKING_VERSION = 1
TIME_RANKING_MIN_OBSERVATIONS = 5
MAX_RESEARCH_PENDING_JOBS = 8


class SingleFileSizeHandler(logging.FileHandler):
    def __init__(self, filename: str | Path, max_bytes: int = MAX_LOG_BYTES) -> None:
        self.max_bytes = max_bytes
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        super().__init__(filename, encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        self._shrink_if_needed()
        super().emit(record)
        self._shrink_if_needed()

    def _shrink_if_needed(self) -> None:
        try:
            path = Path(self.baseFilename)
            if not path.exists() or path.stat().st_size <= self.max_bytes:
                return
            keep_bytes = self.max_bytes // 2
            with path.open("rb") as source:
                source.seek(max(path.stat().st_size - keep_bytes, 0))
                tail = source.read()
            with path.open("wb") as target:
                target.write(b"... log truncated to keep file under 5MB ...\n")
                target.write(tail)
        except OSError:
            return


def response_bytes(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _chart_sample_indices(rows: list[dict], payloads: list[dict], max_points: int) -> list[int]:
    source_points = len(rows)
    if source_points <= max_points:
        return list(range(source_points))
    if max_points <= 2:
        return [0, source_points - 1][:max_points]

    mandatory = {0, source_points - 1}
    previous_symbols: frozenset[str] = frozenset()
    for index, payload in enumerate(payloads):
        symbols = frozenset(symbol for symbol, weight in payload.get("weights", {}).items() if abs(float(weight or 0.0)) > 1e-12)
        if symbols != previous_symbols:
            mandatory.update({max(index - 1, 0), index})
        previous_symbols = symbols
    if len(mandatory) >= max_points:
        ordered = sorted(mandatory)
        return sorted({ordered[round(index * (len(ordered) - 1) / (max_points - 1))] for index in range(max_points)})

    selected = set(mandatory)
    remaining = max_points - len(selected)
    bucket_count = max(remaining // 2, 1)
    for bucket in range(bucket_count):
        start = round(bucket * source_points / bucket_count)
        end = max(round((bucket + 1) * source_points / bucket_count), start + 1)
        candidates = range(start, min(end, source_points))
        if not candidates:
            continue
        selected.add(min(candidates, key=lambda index: float(rows[index].get("drawdown") or 0.0)))
        selected.add(max(candidates, key=lambda index: float(rows[index].get("cumulative_return") or 0.0)))
    if len(selected) < max_points:
        for index in (round(point * (source_points - 1) / (max_points - 1)) for point in range(max_points)):
            selected.add(index)
            if len(selected) >= max_points:
                break
    if len(selected) > max_points:
        non_mandatory = sorted(selected - mandatory)
        keep = max_points - len(mandatory)
        non_mandatory = [
            non_mandatory[round(index * (len(non_mandatory) - 1) / max(keep - 1, 1))]
            for index in range(keep)
        ] if keep > 0 else []
        selected = mandatory | set(non_mandatory)
    return sorted(selected)


def columnar_chart_payload(rows: list[dict], max_points: int = 1000, required_dates: set[str] | None = None) -> dict:
    """Return compact chart arrays while retaining route changes and extrema."""
    source_points = len(rows)
    all_payloads = [json_loads(row.get("payload_json"), {}) for row in rows]
    indices = _chart_sample_indices(rows, all_payloads, max_points)
    if required_dates:
        indices = sorted(set(indices) | {index for index, row in enumerate(rows) if row["trade_date"] in required_dates})
    sampled_rows = [rows[index] for index in indices]
    parsed_payloads = [all_payloads[index] for index in indices]
    chart = {
        "source_points": source_points,
        "display_points": len(sampled_rows),
        "dates": [],
        "total_assets": [],
        "daily_returns": [],
        "cumulative_returns": [],
        "drawdowns": [],
        "benchmark_returns": [],
        "comparison_total_assets": [],
        "values": {},
        "weights": {},
    }
    symbols: list[str] = []
    for payload in all_payloads:
        for symbol in dict.fromkeys((*payload.get("values", {}), *payload.get("weights", {}))):
            if symbol not in symbols:
                symbols.append(symbol)
    chart["values"] = {symbol: [] for symbol in symbols}
    chart["weights"] = {symbol: [] for symbol in symbols}
    for row, payload in zip(sampled_rows, parsed_payloads):
        chart["dates"].append(row["trade_date"])
        chart["total_assets"].append(row["total_asset_cny"])
        chart["daily_returns"].append(row["daily_return"])
        chart["cumulative_returns"].append(row["cumulative_return"])
        chart["drawdowns"].append(row["drawdown"])
        chart["benchmark_returns"].append(row["benchmark_return"])
        chart["comparison_total_assets"].append(payload.get("comparison", {}).get("total_asset_cny"))
        values = payload.get("values", {})
        weights = payload.get("weights", {})
        for symbol in symbols:
            chart["values"][symbol].append(values.get(symbol, 0.0))
            chart["weights"][symbol].append(weights.get(symbol, 0.0))
    return chart


def daily_pnl_chart_payload(rows: list[dict], config: dict) -> dict:
    """Return every selected sleeve's daily P&L plus comparable reference lines."""
    selected_assets = [
        asset
        for asset in config.get("assets", [])
        if asset.get("enabled", True) and float(asset.get("target_weight", 0.0) or 0.0) > 0
    ]
    groups: list[dict] = []
    for asset in selected_assets:
        aliases = [str(asset["symbol"])]
        fallback = asset.get("price_fallback")
        if isinstance(fallback, dict) and fallback.get("symbol"):
            aliases.append(str(fallback["symbol"]))
        aliases.extend(
            str(replacement["symbol"])
            for replacement in asset.get("replacement_assets", [])
            if isinstance(replacement, dict) and replacement.get("symbol")
        )
        groups.append(
            {
                "symbol": str(asset["symbol"]),
                "name": str(asset.get("choice_label") or asset.get("name") or asset["symbol"]),
                "aliases": list(dict.fromkeys(aliases)),
            }
        )

    payloads = [json_loads(row.get("payload_json"), {}) for row in rows]
    if not rows or not groups:
        return {
            "available": False,
            "reason": "当前回测没有已选择且权重大于零的标的",
            "source_points": len(rows),
            "dates": [row["trade_date"] for row in rows],
        }
    if any("asset_daily_profit_cny" not in payload for payload in payloads):
        return {
            "available": False,
            "reason": "此历史记录缺少逐标的每日盈亏，请使用当前版本重新回测",
            "source_points": len(rows),
            "dates": [row["trade_date"] for row in rows],
        }

    symbols = [group["symbol"] for group in groups]
    chart = {
        "available": True,
        "source_points": len(rows),
        "dates": [],
        "symbols": symbols,
        "names": {group["symbol"]: group["name"] for group in groups},
        "profits": {symbol: [] for symbol in symbols},
        "returns": {symbol: [] for symbol in symbols},
        "cumulative_returns": {symbol: [] for symbol in symbols},
        "drawdowns": {symbol: [] for symbol in symbols},
        "combined_profits": [],
        "combined_returns": [],
        "combined_cumulative_returns": [],
        "combined_drawdowns": [],
        "portfolio_profits": [],
        "portfolio_returns": [],
        "portfolio_cumulative_returns": [],
        "portfolio_drawdowns": [],
        "benchmark_profits": [],
        "benchmark_returns": [],
        "benchmark_cumulative_returns": [],
        "benchmark_drawdowns": [],
    }
    previous_values = {symbol: 0.0 for symbol in symbols}
    asset_navs = {symbol: 1.0 for symbol in symbols}
    asset_peaks = {symbol: 1.0 for symbol in symbols}
    combined_nav = 1.0
    combined_peak = 1.0
    benchmark_peak = 1.0
    previous_benchmark_cumulative = 0.0
    for row, payload in zip(rows, payloads):
        source_profits = payload.get("asset_daily_profit_cny", {})
        source_values = payload.get("values", {})
        current_values: dict[str, float] = {}
        daily_profits: dict[str, float] = {}
        chart["dates"].append(row["trade_date"])
        for group in groups:
            symbol = group["symbol"]
            current_value = sum(float(source_values.get(alias, 0.0) or 0.0) for alias in group["aliases"])
            profit = sum(float(source_profits.get(alias, 0.0) or 0.0) for alias in group["aliases"])
            capital_base = max(previous_values[symbol], current_value - profit, 0.0)
            current_values[symbol] = current_value
            daily_profits[symbol] = profit
            chart["profits"][symbol].append(profit)
            daily_return = profit / capital_base if capital_base > 1e-9 else None
            chart["returns"][symbol].append(daily_return)
            if daily_return is not None:
                asset_navs[symbol] = max(asset_navs[symbol] * (1.0 + daily_return), 0.0)
            asset_peaks[symbol] = max(asset_peaks[symbol], asset_navs[symbol])
            chart["cumulative_returns"][symbol].append(asset_navs[symbol] - 1.0)
            chart["drawdowns"][symbol].append(
                asset_navs[symbol] / asset_peaks[symbol] - 1.0
                if asset_peaks[symbol] > 1e-12
                else 0.0
            )

        combined_profit = sum(daily_profits.values())
        combined_previous_value = sum(previous_values.values())
        combined_current_value = sum(current_values.values())
        combined_base = max(
            combined_previous_value,
            combined_current_value - combined_profit,
            0.0,
        )
        combined_return = combined_profit / combined_base if combined_base > 1e-9 else None
        if combined_return is not None:
            combined_nav = max(combined_nav * (1.0 + combined_return), 0.0)
        combined_peak = max(combined_peak, combined_nav)
        benchmark_cumulative = float(row.get("benchmark_return") or 0.0)
        benchmark_denominator = 1.0 + previous_benchmark_cumulative
        benchmark_return = (
            (1.0 + benchmark_cumulative) / benchmark_denominator - 1.0
            if benchmark_denominator > 1e-12
            else 0.0
        )
        chart["combined_profits"].append(combined_profit)
        chart["combined_returns"].append(combined_return)
        chart["combined_cumulative_returns"].append(combined_nav - 1.0)
        chart["combined_drawdowns"].append(
            combined_nav / combined_peak - 1.0 if combined_peak > 1e-12 else 0.0
        )
        chart["portfolio_profits"].append(
            sum(float(value or 0.0) for value in source_profits.values())
        )
        chart["portfolio_returns"].append(float(row.get("daily_return") or 0.0))
        chart["portfolio_cumulative_returns"].append(float(row.get("cumulative_return") or 0.0))
        chart["portfolio_drawdowns"].append(float(row.get("drawdown") or 0.0))
        chart["benchmark_profits"].append(benchmark_return * combined_base)
        chart["benchmark_returns"].append(benchmark_return)
        chart["benchmark_cumulative_returns"].append(benchmark_cumulative)
        benchmark_nav = max(1.0 + benchmark_cumulative, 0.0)
        benchmark_peak = max(benchmark_peak, benchmark_nav)
        chart["benchmark_drawdowns"].append(
            benchmark_nav / benchmark_peak - 1.0 if benchmark_peak > 1e-12 else 0.0
        )
        previous_values = current_values
        previous_benchmark_cumulative = benchmark_cumulative
    return chart


def rebalance_display_payload(payload: dict) -> dict:
    return {
        key: payload.get(key)
        for key in (
            "event_type",
            "threshold_exceeded",
            "executed_trade_count",
            "rebalanced",
            "rebalance_reason",
            "rebalance_action",
            "rebalance_frequency",
            "rebalance_band",
            "rebalance_to_target",
            "current_weights",
            "before_weights",
            "after_weights",
            "weight_basis",
            "targets",
            "desired_weights",
            "decision_date",
            "year_label",
            "year_start_date",
            "year_return",
            "year_max_drawdown",
            "year_fee_cny",
            "year_start_total_cny",
            "year_external_flow_cny",
            "year_profit_cny",
            "year_profit_on_year_start",
            "year_profit_on_original_capital",
            "decision_total_asset_cny",
            "year_return_basis",
            "year_profit_basis",
            "year_asset_performance",
            "asset_performance",
            "asset_performance_version",
            "asset_profit_basis",
            "asset_return_basis",
            "period_max_drawdown",
        )
        if key in payload
    }


def archive_config_payload(config: dict) -> dict:
    return {
        **{key: config[key] for key in (
            "initial_capital_cny", "monthly_spend_cny", "monthly_spend_annual_growth",
            "repo_target_mode", "repo_fixed_target_cny", "repo_fixed_target_ratio", "repo_symbol",
            "rolling_window_years", "rebalance_month_analysis_enabled", "fees", "allow_fractional_us_shares",
            "dip_buy_enabled", "dip_buy_drawdown", "dip_buy_total_parts", "dip_buy_level_mode",
            "dip_buy_cost_basis_mode", "dip_buy_recovery_sell_enabled", "dip_buy_asset_cap_enabled",
            "dip_buy_asset_cap_ratio", "dip_buy_blackout_enabled", "dip_buy_blackout_months",
        ) if key in config},
        "start_date": config.get("start_date"),
        "end_date": config.get("end_date"),
        "rebalance_frequency": config.get("rebalance_frequency"),
        "annual_rebalance_month": config.get("annual_rebalance_month"),
        "rebalance_band": config.get("rebalance_band"),
        "rebalance_to_target": config.get("rebalance_to_target", False),
        "assets": [
            {
                key: asset.get(key)
                for key in ("key", "symbol", "name", "choice_label", "enabled", "target_weight", "market", "currency", "asset_type")
                if key in asset
            }
            for asset in config.get("assets", [])
        ],
    }


def archive_summary_payload(summary: dict) -> dict:
    fields = (
            "start_date",
            "engine_version",
            "end_date",
            "annualized_return",
            "max_drawdown",
            "annual_return_drawdown_ratio",
            "positive_year_count",
            "complete_year_count",
            "ranking_version",
            "repo_annualized_return",
            "excess_annualized_return",
            "adjusted_calmar",
            "positive_year_ratio",
            "ranking_eligible",
            "ranking_score",
            "analysis_status",
            "rebalance_check_count",
            "rebalance_trade_count",
            "rebalance_no_trade_count",
            "rebalance_within_band_count",
            "rebalance_constrained_count",
            "initial_allocation_count",
            "treasury_activation_count",
            "initial_capital_cny",
            "final_asset_cny",
            "total_return",
            "total_fees_cny",
            "total_spend_cny",
            "total_planned_spend_cny",
            "total_spend_shortfall_cny",
            "first_spend_shortfall_date",
            "spend_shortfall_count",
            "trade_count",
            "rebalance_count",
            "total_dividend_cny",
            "total_dividend_tax_cny",
            "withheld_tax_cny",
            "net_profit_cny",
            "original_capital_return",
            "original_capital_annualized_return",
            "final_unrealized_pnl_cny",
    )
    return {
        **{key: summary[key] for key in fields if key in summary},
        "requires_recalculation": int(summary.get("engine_version") or 0) < BACKTEST_ENGINE_VERSION,
    }


def yearly_return_counts_from_daily(conn, run_id: str) -> tuple[int, int]:
    yearly_nav: dict[str, float] = {}
    year_dates: dict[str, list[str]] = {}
    rows = conn.execute(
        "SELECT trade_date,daily_return FROM portfolio_daily WHERE run_id=? ORDER BY trade_date",
        (run_id,),
    ).fetchall()
    for row in rows:
        year = row["trade_date"][:4]
        yearly_nav[year] = yearly_nav.get(year, 1.0) * (1.0 + float(row["daily_return"] or 0.0))
        year_dates.setdefault(year, []).append(row["trade_date"])
    complete = [
        nav
        for year, nav in yearly_nav.items()
        if year_dates[year][0][5:7] == "01" and year_dates[year][-1][5:7] == "12"
    ]
    return sum(1 for nav in complete if nav > 1.0 + 1e-12), len(complete)


def repo_annualized_return_from_daily(conn, run_id: str) -> float:
    row = conn.execute(
        "SELECT MIN(trade_date) AS start_date, MAX(trade_date) AS end_date FROM portfolio_daily WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if not row or not row["start_date"] or not row["end_date"]:
        return 0.0
    config_row = conn.execute("SELECT config_json FROM backtest_runs WHERE run_id=?", (run_id,)).fetchone()
    config = json_loads(config_row["config_json"], {}) if config_row else {}
    repo_symbol = repo_rate_symbol(config)
    rates = conn.execute(
        "SELECT trade_date,close_rate FROM repo_rates WHERE symbol=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        (repo_symbol, row["start_date"], row["end_date"]),
    ).fetchall()
    if len(rates) < 2:
        return 0.0
    benchmark_nav = 1.0
    sessions = market_business_days(conn, "CN", row["start_date"], date.fromisoformat(row["end_date"]) + timedelta(days=33))
    for rate in rates:
        day = date.fromisoformat(rate["trade_date"])
        benchmark_nav *= 1.0 + float(rate["close_rate"] or 0.0) / 100.0 * repo_actual_days(day, 1, sessions) / repo_year_basis(day)
    years = max((datetime.fromisoformat(row["end_date"]) - datetime.fromisoformat(row["start_date"])).days / 365.25, 1 / 365.25)
    return benchmark_nav ** (1.0 / years) - 1.0


def refresh_ranking_summary(conn, run_id: str, summary: dict) -> dict:
    positive_year_count, complete_year_count = yearly_return_counts_from_daily(conn, run_id)
    ranking = ranking_metrics(
        float(summary.get("annualized_return") or 0.0),
        repo_annualized_return_from_daily(conn, run_id),
        float(summary.get("max_drawdown") or 0.0),
        positive_year_count,
        complete_year_count,
    )
    return {
        **summary,
        "positive_year_count": positive_year_count,
        "complete_year_count": complete_year_count,
        **ranking,
    }


def backtest_archive_entries(
    conn,
    limit: int,
    leaderboard: bool = False,
    leaderboard_key_id: str | None = None,
    favorites_key_id: str | None = None,
) -> list[dict]:
    if leaderboard:
        membership_join = ""
        query_params: tuple = (limit,)
        if leaderboard_key_id is not None:
            membership_join = "JOIN leaderboard_memberships lm ON lm.run_id=br.run_id AND lm.key_id=?"
            query_params = (leaderboard_key_id, limit)
        rows = rows_to_dicts(
            conn.execute(
                f"""
                SELECT br.run_id,br.created_at,br.config_json,br.summary_json
                FROM backtest_runs br
                {membership_join}
                WHERE COALESCE(json_extract(br.summary_json, '$.ranking_eligible'), 0) = 1
                  AND COALESCE(json_extract(br.summary_json, '$.engine_version'), 0) >= {BACKTEST_ENGINE_VERSION}
                ORDER BY
                  COALESCE(json_extract(br.summary_json, '$.ranking_score'), 0) DESC,
                  COALESCE(json_extract(br.summary_json, '$.excess_annualized_return'), 0) DESC,
                  COALESCE(json_extract(br.summary_json, '$.adjusted_calmar'), 0) DESC,
                  COALESCE(json_extract(br.summary_json, '$.positive_year_ratio'), 0) DESC,
                  br.created_at DESC,
                  br.run_id DESC
                LIMIT ?
                """,
                query_params,
            )
        )
    elif favorites_key_id is not None:
        rows = rows_to_dicts(conn.execute(
            """SELECT br.run_id,br.created_at,br.config_json,br.summary_json
               FROM backtest_runs br JOIN run_metadata rm ON rm.run_id=br.run_id
               WHERE rm.key_id=? AND rm.favorite=1
               ORDER BY rm.updated_at DESC,br.created_at DESC,br.run_id DESC""",
            (favorites_key_id,),
        ))
    else:
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT run_id,created_at,config_json,summary_json
                FROM backtest_runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        )
    entries = []
    for row in rows:
        summary = json_loads(row["summary_json"], {})
        compact_summary = archive_summary_payload(summary)
        entries.append(
            {
                "run_id": row["run_id"],
                "created_at": row["created_at"],
                "config": archive_config_payload(json_loads(row["config_json"], {})),
                "summary": compact_summary,
                "positive_year_count": int(summary.get("positive_year_count") or 0),
                "complete_year_count": int(summary.get("complete_year_count") or 0),
                "ranking_score": float(summary.get("ranking_score") or 0.0),
            }
        )
    if leaderboard:
        for rank, entry in enumerate(entries, start=1):
            entry["rank"] = rank
    return entries


def add_archive_metadata(conn, entries: list[dict], key_id: str) -> list[dict]:
    metadata = {row["run_id"]: {"name": row["name"], "note": row["note"], "favorite": bool(row["favorite"])}
                for row in conn.execute("SELECT run_id,name,note,favorite FROM run_metadata WHERE key_id=?", (key_id,))}
    for entry in entries:
        entry["metadata"] = metadata.get(entry["run_id"], {"name": "", "note": "", "favorite": False})
    return entries


def leaderboard_available_years(conn, leaderboard_key_id: str | None = None) -> list[int]:
    """Return calendar-year candidates without scanning the large daily table."""
    years: set[int] = set()
    membership_join = ""
    query_params: tuple = ()
    if leaderboard_key_id is not None:
        membership_join = "JOIN leaderboard_memberships lm ON lm.run_id=br.run_id AND lm.key_id=?"
        query_params = (leaderboard_key_id,)
    rows = conn.execute(
        f"""
        SELECT COALESCE(json_extract(br.summary_json, '$.start_date'), json_extract(br.config_json, '$.start_date')) AS start_date,
               COALESCE(json_extract(br.summary_json, '$.end_date'), json_extract(br.config_json, '$.end_date')) AS end_date
        FROM backtest_runs br
        {membership_join}
        WHERE COALESCE(json_extract(br.summary_json, '$.engine_version'), 0) >= {BACKTEST_ENGINE_VERSION}
        """
        ,
        query_params,
    ).fetchall()
    for row in rows:
        try:
            first_day = date.fromisoformat(str(row["start_date"] or ""))
            last_day = date.fromisoformat(str(row["end_date"] or ""))
        except ValueError:
            continue
        for year in range(first_day.year, last_day.year + 1):
            if first_day <= date(year, 1, 7) and last_day >= date(year, 12, 24):
                years.add(year)
    return sorted(years, reverse=True)


def _period_repo_annualized_return(conn, start_date: str, end_date: str) -> float:
    """Use one common one-day repo cash benchmark for every strategy in a cohort."""
    rows = conn.execute(
        """
        SELECT trade_date,close_rate
        FROM repo_rates
        WHERE symbol='204001' AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        (start_date, end_date),
    ).fetchall()
    if not rows:
        return 0.0
    period_end = date.fromisoformat(end_date)
    benchmark_nav = 1.0
    sessions = market_business_days(conn, "CN", start_date, period_end + timedelta(days=33))
    for row in rows:
        day = date.fromisoformat(row["trade_date"])
        actual_days = repo_actual_days(day, 1, sessions)
        benchmark_nav *= 1.0 + float(row["close_rate"] or 0.0) / 100.0 * actual_days / repo_year_basis(day)
    years = max(((period_end - date.fromisoformat(start_date)).days + 1) / 365.25, 1 / 365.25)
    return benchmark_nav ** (1.0 / years) - 1.0


def _period_performance_metrics(
    dates: list[str],
    daily_returns: list[float],
    repo_annualized_return: float,
) -> dict:
    growth = 1.0
    nav = 1.0
    peak = 1.0
    max_drawdown = 0.0
    monthly_growth: dict[str, float] = {}
    for trade_date, value in zip(dates, daily_returns):
        daily_return = float(value or 0.0)
        growth *= 1.0 + daily_return
        nav *= 1.0 + daily_return
        peak = max(peak, nav)
        max_drawdown = min(max_drawdown, nav / peak - 1.0 if peak else 0.0)
        month = trade_date[:7]
        monthly_growth[month] = monthly_growth.get(month, 1.0) * (1.0 + daily_return)
    calendar_days = max((date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days + 1, 1)
    sample_years = calendar_days / 365.25
    total_return = growth - 1.0
    annualized_return = -1.0 if growth <= 0 else growth ** (1.0 / max(sample_years, 1 / 365.25)) - 1.0
    excess_annualized_return = annualized_return - repo_annualized_return
    adjusted_calmar = excess_annualized_return / max(abs(max_drawdown), 0.08)
    positive_month_count = sum(1 for value in monthly_growth.values() if value > 1.0 + 1e-12)
    month_count = len(monthly_growth)
    return {
        "time_ranking_version": TIME_RANKING_VERSION,
        "start_date": dates[0],
        "end_date": dates[-1],
        "observation_count": len(dates),
        "sample_years": sample_years,
        "sample_confidence": min(len(dates) / 252.0, 1.0),
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown,
        "repo_annualized_return": repo_annualized_return,
        "excess_annualized_return": excess_annualized_return,
        "adjusted_calmar": adjusted_calmar,
        "positive_month_count": positive_month_count,
        "month_count": month_count,
        "positive_month_ratio": positive_month_count / month_count if month_count else 0.0,
        "cash_hurdle_passed": excess_annualized_return > 0.0,
    }


def _peer_percentiles(entries: list[dict], metric: str) -> dict[str, float]:
    """Return average-rank percentiles; one observation or all ties score 50%."""
    ordered = sorted((float(entry["period_metrics"].get(metric) or 0.0), entry["run_id"]) for entry in entries)
    count = len(ordered)
    if count <= 1:
        return {run_id: 0.5 for _value, run_id in ordered}
    result: dict[str, float] = {}
    index = 0
    while index < count:
        end = index + 1
        while end < count and abs(ordered[end][0] - ordered[index][0]) <= 1e-12:
            end += 1
        average_rank = (index + end - 1) / 2.0
        percentile = average_rank / (count - 1)
        for _value, run_id in ordered[index:end]:
            result[run_id] = percentile
        index = end
    return result


def time_aware_backtest_leaderboard(
    conn,
    start_date: str,
    end_date: str,
    limit: int = 100,
    leaderboard_key_id: str | None = None,
) -> list[dict]:
    """Rank strategies only after recomputing all metrics over one shared period."""
    membership_join = ""
    query_params: tuple = (start_date, end_date)
    if leaderboard_key_id is not None:
        membership_join = "JOIN leaderboard_memberships lm ON lm.run_id=pd.run_id AND lm.key_id=?"
        query_params = (leaderboard_key_id, start_date, end_date)
    daily_rows = conn.execute(
        f"""
        SELECT pd.run_id,pd.trade_date,pd.daily_return
        FROM portfolio_daily pd
        JOIN backtest_runs br ON br.run_id=pd.run_id
        {membership_join}
        WHERE pd.trade_date BETWEEN ? AND ?
          AND COALESCE(json_extract(br.summary_json, '$.engine_version'), 0) >= {BACKTEST_ENGINE_VERSION}
        ORDER BY pd.run_id,pd.trade_date
        """,
        query_params,
    ).fetchall()
    grouped: dict[str, list] = {}
    for row in daily_rows:
        grouped.setdefault(row["run_id"], []).append(row)
    if not grouped:
        return []

    max_observations = max(len(rows) for rows in grouped.values())
    if max_observations < TIME_RANKING_MIN_OBSERVATIONS:
        return []
    minimum_observations = min(20, max(TIME_RANKING_MIN_OBSERVATIONS, math.ceil(max_observations * 0.8)))
    span_days = max((date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1, 1)
    edge_tolerance_days = min(7, max(2, span_days // 50))
    latest_allowed_start = (date.fromisoformat(start_date) + timedelta(days=edge_tolerance_days)).isoformat()
    earliest_allowed_end = (date.fromisoformat(end_date) - timedelta(days=edge_tolerance_days)).isoformat()
    run_rows = {
        row["run_id"]: row
        for row in conn.execute("SELECT run_id,created_at,config_json,summary_json FROM backtest_runs").fetchall()
    }
    repo_annualized_return = _period_repo_annualized_return(conn, start_date, end_date)
    entries: list[dict] = []
    for run_id, rows in grouped.items():
        run = run_rows.get(run_id)
        coverage_ratio = len(rows) / max_observations
        if (
            not run
            or len(rows) < minimum_observations
            or coverage_ratio < 0.95
            or rows[0]["trade_date"] > latest_allowed_start
            or rows[-1]["trade_date"] < earliest_allowed_end
        ):
            continue
        summary = json_loads(run["summary_json"], {})
        metrics = _period_performance_metrics(
            [row["trade_date"] for row in rows],
            [float(row["daily_return"] or 0.0) for row in rows],
            repo_annualized_return,
        )
        metrics["coverage_ratio"] = coverage_ratio
        entries.append(
            {
                "run_id": run_id,
                "created_at": run["created_at"],
                "config": archive_config_payload(json_loads(run["config_json"], {})),
                "summary": archive_summary_payload(summary),
                "positive_year_count": int(summary.get("positive_year_count") or 0),
                "complete_year_count": int(summary.get("complete_year_count") or 0),
                "period_metrics": metrics,
            }
        )
    if not entries:
        return []

    percentile_metrics = {
        "excess_return": _peer_percentiles(entries, "excess_annualized_return"),
        "risk_efficiency": _peer_percentiles(entries, "adjusted_calmar"),
        # max_drawdown is negative, so a larger value means a shallower loss.
        "drawdown": _peer_percentiles(entries, "max_drawdown"),
        "stability": _peer_percentiles(entries, "positive_month_ratio"),
    }
    peer_count = len(entries)
    for entry in entries:
        run_id = entry["run_id"]
        metrics = entry["period_metrics"]
        components = {key: values[run_id] for key, values in percentile_metrics.items()}
        raw_score = (
            45.0 * components["excess_return"]
            + 25.0 * components["risk_efficiency"]
            + 15.0 * components["drawdown"]
            + 15.0 * components["stability"]
        )
        confidence = min(float(metrics["sample_confidence"]), float(metrics["coverage_ratio"]))
        ranking_score = 50.0 + (raw_score - 50.0) * confidence
        metrics["peer_count"] = peer_count
        metrics["peer_percentiles"] = components
        metrics["ranking_score"] = ranking_score
        entry["ranking_score"] = ranking_score
        entry["time_ranking_score"] = ranking_score
    entries.sort(
        key=lambda entry: (
            float(entry["ranking_score"]),
            float(entry["period_metrics"]["excess_annualized_return"]),
            float(entry["period_metrics"]["adjusted_calmar"]),
            entry["created_at"],
            entry["run_id"],
        ),
        reverse=True,
    )
    entries = entries[:limit]
    for rank, entry in enumerate(entries, start=1):
        entry["rank"] = rank
    return entries


def backtest_leaderboard_payload(
    conn,
    query: dict[str, list[str]] | None = None,
    leaderboard_key_id: str | None = None,
) -> dict:
    query = query or {}
    available_years = leaderboard_available_years(conn, leaderboard_key_id)
    mode = (query.get("period") or [""])[0]
    requested_year = (query.get("year") or [""])[0]
    requested_start = (query.get("start_date") or [""])[0]
    requested_end = (query.get("end_date") or [""])[0]

    if mode == "all" or (not available_years and not requested_year and not requested_start and not requested_end):
        records = backtest_archive_entries(
            conn,
            limit=100,
            leaderboard=True,
            leaderboard_key_id=leaderboard_key_id,
        )
        period = {
            "mode": "all",
            "label": "各自完整回测期",
            "comparable": False,
            "description": "起止时间不同，仅供查看；请选择同一年或自定义区间进行公平横向比较。",
        }
    else:
        if requested_year:
            try:
                year = int(requested_year)
                start_date = date(year, 1, 1)
                end_date = date(year, 12, 31)
            except (TypeError, ValueError) as exc:
                raise ValueError("year must use YYYY") from exc
            period_mode = "year"
            label = f"{year}年"
        elif requested_start or requested_end:
            if not requested_start or not requested_end:
                raise ValueError("start_date and end_date must be provided together")
            try:
                start_date = date.fromisoformat(requested_start)
                end_date = date.fromisoformat(requested_end)
            except ValueError as exc:
                raise ValueError("start_date and end_date must use YYYY-MM-DD") from exc
            if start_date > end_date:
                raise ValueError("start_date must be before or equal to end_date")
            period_mode = "custom"
            label = f"{start_date.isoformat()} 至 {end_date.isoformat()}"
        else:
            year = available_years[0]
            start_date = date(year, 1, 1)
            end_date = date(year, 12, 31)
            period_mode = "year"
            label = f"{year}年"
        records = time_aware_backtest_leaderboard(
            conn,
            start_date.isoformat(),
            end_date.isoformat(),
            limit=100,
            leaderboard_key_id=leaderboard_key_id,
        )
        period = {
            "mode": period_mode,
            "label": label,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "comparable": True,
            "peer_count": len(records),
            "scoring_version": TIME_RANKING_VERSION,
            "description": "同一区间重算收益、回撤、同期现金超额和正收益月份，再按同期百分位评分。",
        }
    return {
        "records": records,
        "period": period,
        "available_years": available_years,
        "default_year": available_years[0] if available_years else None,
    }


def extended_analysis_required(config: dict) -> bool:
    if rolling_window_ranges(
        config["start_date"],
        config["end_date"],
        int(config["rolling_window_years"]),
    ):
        return True
    return bool(
        config["rebalance_frequency"] == "yearly"
        and config.get("rebalance_month_analysis_enabled", False)
    )


def set_persisted_analysis_status(conn, result: dict, status: str, error: str | None = None) -> None:
    summary = result["summary"]
    summary["analysis_status"] = status
    summary["analysis_error"] = error
    conn.execute(
        "UPDATE backtest_runs SET summary_json=? WHERE run_id=?",
        (json_dumps(summary), result["run_id"]),
    )


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def configure_logging() -> None:
    level = getattr(logging, os.getenv("PORTFOLIO_LOG_LEVEL", "INFO").upper(), logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_file = os.getenv("PORTFOLIO_LOG_FILE", "logs/portfolio.log")
    if log_file:
        handlers.append(SingleFileSizeHandler(log_file))
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=handlers, force=True)


def raise_if_cancelled(should_cancel=None) -> None:
    if should_cancel and should_cancel():
        raise BacktestCancelled(CANCELLED_JOB_MESSAGE)


def execute_backtest_request(
    settings,
    write_lock: Lock,
    config: dict,
    should_cancel=None,
    *,
    defer_extended_analysis: bool = False,
    leaderboard_key_id: str = DEFAULT_LEADERBOARD_KEY_ID,
) -> dict:
    request_id = str(uuid.uuid4())[:8]
    started_at = time.perf_counter()
    errors = validate_config(config)
    if errors:
        raise BacktestError("; ".join(errors))
    data_assets = backtest_assets(config)
    rate_symbol = repo_rate_symbol(config)
    raise_if_cancelled(should_cancel)
    logger.info(
        "backtest request start id=%s range=%s..%s repo=%s assets=%s",
        request_id,
        config["start_date"],
        config["end_date"],
        config["repo_symbol"],
        [asset["symbol"] for asset in config["assets"] if asset.get("enabled", True)],
    )
    with write_lock:
        raise_if_cancelled(should_cancel)
        lock_acquired_at = time.perf_counter()
        logger.info("backtest lock acquired id=%s wait_seconds=%.3f", request_id, lock_acquired_at - started_at)
        with db_session(settings.db_path) as conn:
            missing_started_at = time.perf_counter()
            missing_before = required_data_missing(conn, config["start_date"], config["end_date"], data_assets, rate_symbol)
            logger.info("backtest data check id=%s seconds=%.3f missing=%s", request_id, time.perf_counter() - missing_started_at, missing_before)
            raise_if_cancelled(should_cancel)
            if not missing_before:
                cache_started_at = time.perf_counter()
                cached = get_cached_backtest_run(conn, config)
                if cached:
                    add_leaderboard_membership(conn, leaderboard_key_id, cached["run_id"])
                    cached_status = cached["summary"].get("analysis_status", "completed")
                    if (
                        not defer_extended_analysis
                        and extended_analysis_required(config)
                        and cached_status not in {"completed", "not_required"}
                    ):
                        # A synchronous caller must not receive the partial
                        # result left by an earlier asynchronous request.
                        analysis = run_backtest(
                            conn,
                            config,
                            persist=False,
                            should_cancel=should_cancel,
                            include_comparison=False,
                            include_month_analysis=True,
                            include_rolling_analysis=True,
                        )["summary"]
                        cached["summary"]["rolling_periods"] = analysis.get("rolling_periods", [])
                        cached["summary"]["rebalance_month_scenarios"] = analysis.get("rebalance_month_scenarios", [])
                        set_persisted_analysis_status(conn, cached, "completed")
                        cached_status = "completed"
                    cached["analysis_pending"] = bool(
                        defer_extended_analysis
                        and extended_analysis_required(config)
                        and cached_status not in {"completed", "not_required"}
                    )
                    cached["data_sync"] = {"triggered": False, "missing_before": [], "result": None}
                    logger.info("backtest cache hit id=%s seconds=%.3f total_seconds=%.3f", request_id, time.perf_counter() - cache_started_at, time.perf_counter() - started_at)
                    return cached
                logger.info("backtest cache miss id=%s seconds=%.3f", request_id, time.perf_counter() - cache_started_at)

        cache_invalidated = False
        sync_result = None
        if missing_before:
            raise_if_cancelled(should_cancel)
            with db_session(settings.db_path) as conn:
                sync_started_at = time.perf_counter()
                sync_result = sync_all(
                    conn,
                    settings.tushare_token,
                    config["start_date"],
                    config["end_date"],
                    data_assets,
                    rate_symbol,
                    missing_items=missing_before,
                    should_cancel=should_cancel,
                )
                logger.info("backtest sync complete id=%s seconds=%.3f result=%s", request_id, time.perf_counter() - sync_started_at, sync_result)
                inserted = (sync_result or {}).get("inserted", {})
                cache_invalidated = any(item.startswith("generated:") for item in missing_before) or any(int(count or 0) > 0 for count in inserted.values())
                if cache_invalidated:
                    # Commit invalidation with the changed source data, even if
                    # another source is still missing or the request is cancelled.
                    conn.execute("UPDATE backtest_runs SET config_hash=NULL")
            raise_if_cancelled(should_cancel)
            with db_session(settings.db_path) as conn:
                missing_after_started_at = time.perf_counter()
                missing_after = required_data_missing(conn, config["start_date"], config["end_date"], data_assets, rate_symbol)
                logger.info("backtest post-sync data check id=%s seconds=%.3f missing=%s", request_id, time.perf_counter() - missing_after_started_at, missing_after)
            if missing_after:
                raise BacktestError("自动补足数据后仍缺少：" + "、".join(missing_after))

        with db_session(settings.db_path) as conn:
            raise_if_cancelled(should_cancel)
            run_started_at = time.perf_counter()
            needs_extended_analysis = defer_extended_analysis and extended_analysis_required(config)
            result = run_backtest(
                conn,
                config,
                should_cancel=should_cancel,
                include_month_analysis=not needs_extended_analysis,
                include_rolling_analysis=not needs_extended_analysis,
            )
            set_persisted_analysis_status(
                conn,
                result,
                "pending" if needs_extended_analysis else "completed",
            )
            add_leaderboard_membership(conn, leaderboard_key_id, result["run_id"])
            result["analysis_pending"] = needs_extended_analysis
            logger.info("backtest engine complete id=%s seconds=%.3f cache=%s", request_id, time.perf_counter() - run_started_at, result.get("cache"))
            result["data_sync"] = {
                "triggered": bool(missing_before),
                "missing_before": missing_before,
                "result": sync_result,
            }
            if sync_result and any(int(count or 0) > 0 for count in sync_result.get("inserted", {}).values()):
                status_started_at = time.perf_counter()
                result["status"] = data_status(conn)
                logger.info("backtest status complete id=%s seconds=%.3f rows=%d", request_id, time.perf_counter() - status_started_at, len(result["status"]))
            logger.info("backtest request complete id=%s total_seconds=%.3f", request_id, time.perf_counter() - started_at)
            return result


class PortfolioServer(ThreadingHTTPServer):
    request_queue_size = 64
    daemon_threads = True
    allow_reuse_address = True

    def server_close(self) -> None:
        stop_event = getattr(self, "job_monitor_stop", None)
        if stop_event is not None:
            stop_event.set()
        monitor = getattr(self, "job_monitor_thread", None)
        if monitor is not None:
            monitor.join(timeout=2)
        if hasattr(self, "research_jobs_lock"):
            with self.research_jobs_lock:
                self.research_closing = True
                research_ids = list(self.research_jobs)
            for job_id in research_ids:
                cancel_research_job(self, job_id, "研究已取消：服务正在关闭")
            self.research_executor.shutdown(wait=False, cancel_futures=True)
        executor = getattr(self, "job_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        analysis_executor = getattr(self, "analysis_executor", None)
        if analysis_executor is not None:
            analysis_executor.shutdown(wait=False, cancel_futures=True)
        super().server_close()


def set_job(server, job_id: str, **updates) -> None:
    with server.jobs_lock:  # type: ignore[attr-defined]
        job = dict(server.jobs.get(job_id, {}))  # type: ignore[attr-defined]
        job.update(updates)
        job["updated_at"] = iso_now()
        server.jobs[job_id] = job  # type: ignore[attr-defined]


def record_job_request(server, job_id: str) -> None:
    with server.jobs_lock:  # type: ignore[attr-defined]
        if job_id in server.jobs:  # type: ignore[attr-defined]
            server.job_activity[job_id] = time.monotonic()  # type: ignore[attr-defined]
            server.jobs[job_id]["last_requested_at"] = iso_now()  # type: ignore[attr-defined]


def job_cancel_requested(server, job_id: str) -> bool:
    event = server.job_cancel_events.get(job_id)  # type: ignore[attr-defined]
    return bool(event and event.is_set())


def cancel_job(server, job_id: str, message: str = CANCELLED_JOB_MESSAGE) -> None:
    event = server.job_cancel_events.get(job_id)  # type: ignore[attr-defined]
    if event:
        event.set()
    future = server.job_futures.get(job_id)  # type: ignore[attr-defined]
    if future:
        future.cancel()
    with server.jobs_lock:  # type: ignore[attr-defined]
        job = server.jobs.get(job_id)  # type: ignore[attr-defined]
        if not job or job.get("status") in {"completed", "failed", "cancelled"}:
            return
        job.update(
            {
                "status": "cancelled",
                "message": message,
                "error": message,
                "completed_at": iso_now(),
                "updated_at": iso_now(),
            }
        )
        server.jobs[job_id] = job  # type: ignore[attr-defined]
    logger.info("backtest job cancelled id=%s reason=%s", job_id, message)


def cleanup_jobs(server) -> None:
    now = time.monotonic()
    abandoned_seconds = float(getattr(server, "job_abandoned_seconds", DEFAULT_ABANDONED_JOB_SECONDS))
    retention_seconds = float(getattr(server, "job_retention_seconds", DEFAULT_JOB_RETENTION_SECONDS))
    to_cancel: list[str] = []
    to_delete: list[str] = []
    with server.jobs_lock:  # type: ignore[attr-defined]
        for job_id, job in list(server.jobs.items()):  # type: ignore[attr-defined]
            activity = server.job_activity.get(job_id, now)  # type: ignore[attr-defined]
            status = job.get("status")
            if status in {"queued", "running"} and now - activity > abandoned_seconds:
                to_cancel.append(job_id)
            elif status in {"completed", "failed", "cancelled"} and now - activity > retention_seconds:
                to_delete.append(job_id)
        for job_id in to_delete:
            job = server.jobs.get(job_id)  # type: ignore[attr-defined]
            server.jobs.pop(job_id, None)  # type: ignore[attr-defined]
            server.job_activity.pop(job_id, None)  # type: ignore[attr-defined]
            server.job_cancel_events.pop(job_id, None)  # type: ignore[attr-defined]
            server.job_futures.pop(job_id, None)  # type: ignore[attr-defined]
            request_index_key = job.get("request_index_key") if job else None
            if request_index_key:
                server.job_request_index.pop(request_index_key, None)  # type: ignore[attr-defined]
    for job_id in to_cancel:
        cancel_job(server, job_id)
    if to_delete:
        logger.info("backtest jobs cleaned count=%d", len(to_delete))


def monitor_jobs(server) -> None:
    stop_event = server.job_monitor_stop  # type: ignore[attr-defined]
    while not stop_event.wait(JOB_CLEANUP_INTERVAL_SECONDS):
        cleanup_jobs(server)
        cleanup_research_jobs(server)


def research_job_payload(job: dict) -> dict:
    return deepcopy({key: value for key, value in job.items() if not key.startswith("_")})


def cancel_research_job(server, job_id: str, reason: str = "研究已取消") -> None:
    with server.research_jobs_lock:
        job = server.research_jobs.get(job_id)
        if not job or job["status"] not in {"queued", "running"}:
            return
        server.research_cancel_events[job_id].set()
        job.update(status="cancelled", error=reason, updated_at=iso_now())
        future = server.research_futures.get(job_id)
        if future is not None and future.cancel():
            job["_active"] = False
        server.research_activity[job_id] = time.monotonic()


def cleanup_research_jobs(server) -> None:
    if not hasattr(server, "research_jobs_lock"):
        return
    now = time.monotonic()
    to_cancel = []
    with server.research_jobs_lock:
        for job_id, job in list(server.research_jobs.items()):
            elapsed = now - server.research_activity.get(job_id, now)
            if job["status"] in {"queued", "running"} and elapsed > server.job_abandoned_seconds:
                to_cancel.append(job_id)
            elif not job["_active"] and elapsed > server.job_retention_seconds:
                server.research_jobs.pop(job_id, None)
                server.research_activity.pop(job_id, None)
                server.research_cancel_events.pop(job_id, None)
                server.research_futures.pop(job_id, None)
    for job_id in to_cancel:
        cancel_research_job(server, job_id, "研究已取消：页面没有继续请求进度")


def run_research_job(server, job_id: str, base_config: dict, payload: dict) -> None:
    with server.research_jobs_lock:
        job = server.research_jobs[job_id]
        cancelled = server.research_cancel_events[job_id]
        if job["status"] == "cancelled":
            job["_active"] = False
            return
        job.update(status="running", updated_at=iso_now())

    def on_progress(completed, total, rows):
        with server.research_jobs_lock:
            if not cancelled.is_set():
                server.research_jobs[job_id].update(
                    completed=completed, total=total, rows=deepcopy(rows), updated_at=iso_now(),
                )

    try:
        with db_session(server.settings.db_path) as conn:
            # Research consumes saved market data. Enforce its non-persistent
            # contract even if a future engine change accidentally writes.
            conn.execute("PRAGMA query_only=ON")
            # WAL keeps every scenario on the same market-data snapshot even
            # when another request syncs prices while this research is running.
            conn.execute("BEGIN")
            result = run_research(conn, base_config, payload, should_cancel=cancelled.is_set, on_progress=on_progress)
        with server.research_jobs_lock:
            if not cancelled.is_set():
                server.research_jobs[job_id].update(
                    status="completed", completed=result["completed"], total=result["total"],
                    rows=deepcopy(result["rows"]), methodology=result.get("methodology", {}), updated_at=iso_now(),
                )
    except BacktestCancelled:
        with server.research_jobs_lock:
            server.research_jobs[job_id].update(status="cancelled", error="研究已取消", updated_at=iso_now())
    except Exception as exc:
        logger.exception("research failed job_id=%s", job_id)
        with server.research_jobs_lock:
            if not cancelled.is_set():
                server.research_jobs[job_id].update(status="failed", error=str(exc), updated_at=iso_now())
    finally:
        with server.research_jobs_lock:
            server.research_jobs[job_id]["_active"] = False
            server.research_activity[job_id] = time.monotonic()


def update_deferred_analysis_status(server, run_id: str, status: str, error: str | None = None) -> None:
    with server.write_lock:  # type: ignore[attr-defined]
        with db_session(server.settings.db_path) as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT summary_json FROM backtest_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if not row:
                return
            summary = json_loads(row["summary_json"], {})
            summary["analysis_status"] = status
            summary["analysis_error"] = error
            conn.execute(
                "UPDATE backtest_runs SET summary_json=? WHERE run_id=?",
                (json_dumps(summary), run_id),
            )


def run_deferred_backtest_analysis(server, run_id: str, config: dict) -> None:
    started_at = time.perf_counter()
    try:
        update_deferred_analysis_status(server, run_id, "running")
        with db_session(server.settings.db_path) as conn:  # type: ignore[attr-defined]
            analysis = run_backtest(
                conn,
                config,
                persist=False,
                include_comparison=False,
                include_month_analysis=True,
                include_rolling_analysis=True,
            )["summary"]
        with server.write_lock:  # type: ignore[attr-defined]
            with db_session(server.settings.db_path) as conn:  # type: ignore[attr-defined]
                row = conn.execute(
                    "SELECT summary_json FROM backtest_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if not row:
                    return
                summary = json_loads(row["summary_json"], {})
                summary["rolling_periods"] = analysis.get("rolling_periods", [])
                summary["rebalance_month_scenarios"] = analysis.get("rebalance_month_scenarios", [])
                summary["analysis_status"] = "completed"
                summary["analysis_error"] = None
                conn.execute(
                    "UPDATE backtest_runs SET summary_json=? WHERE run_id=?",
                    (json_dumps(summary), run_id),
                )
        logger.info(
            "backtest deferred analysis complete run_id=%s seconds=%.3f rolling=%d months=%d",
            run_id,
            time.perf_counter() - started_at,
            len(analysis.get("rolling_periods", [])),
            len(analysis.get("rebalance_month_scenarios", [])),
        )
    except Exception as exc:
        logger.exception("backtest deferred analysis failed run_id=%s", run_id)
        try:
            update_deferred_analysis_status(server, run_id, "failed", str(exc))
        except Exception:
            logger.exception("backtest deferred analysis status update failed run_id=%s", run_id)
    finally:
        with server.analysis_lock:  # type: ignore[attr-defined]
            server.analysis_futures.pop(run_id, None)  # type: ignore[attr-defined]
        gc.collect()


def schedule_deferred_backtest_analysis(server, run_id: str, config: dict) -> None:
    with server.analysis_lock:  # type: ignore[attr-defined]
        existing = server.analysis_futures.get(run_id)  # type: ignore[attr-defined]
        if existing and not existing.done():
            return
        with db_session(server.settings.db_path) as conn:  # type: ignore[attr-defined]
            row = conn.execute("SELECT summary_json FROM backtest_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or json_loads(row["summary_json"], {}).get("analysis_status") in {"completed", "not_required"}:
                return
        future = server.analysis_executor.submit(  # type: ignore[attr-defined]
            run_deferred_backtest_analysis,
            server,
            run_id,
            config,
        )
        server.analysis_futures[run_id] = future  # type: ignore[attr-defined]


def run_backtest_job(
    server,
    job_id: str,
    config: dict,
    leaderboard_key_id: str = DEFAULT_LEADERBOARD_KEY_ID,
) -> None:
    set_job(server, job_id, status="running", message="正在检查数据并运行回测", started_at=iso_now())
    try:
        execute_parameters = inspect.signature(execute_backtest_request).parameters
        execute_kwargs = {
            "should_cancel": lambda: job_cancel_requested(server, job_id),
        }
        if "defer_extended_analysis" in execute_parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in execute_parameters.values()
        ):
            execute_kwargs["defer_extended_analysis"] = True
        if "leaderboard_key_id" in execute_parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in execute_parameters.values()
        ):
            execute_kwargs["leaderboard_key_id"] = leaderboard_key_id
        result = execute_backtest_request(
            server.settings,  # type: ignore[attr-defined]
            server.write_lock,  # type: ignore[attr-defined]
            config,
            **execute_kwargs,
        )
    except (BacktestCancelled, SyncCancelled) as exc:
        logger.info("backtest job cancelled id=%s", job_id)
        set_job(server, job_id, status="cancelled", message=str(exc), error=str(exc), completed_at=iso_now())
    except BacktestError as exc:
        logger.warning("backtest job failed id=%s error=%s", job_id, exc)
        set_job(server, job_id, status="failed", message=str(exc), error=str(exc), completed_at=iso_now())
    except Exception as exc:
        logger.exception("backtest job crashed id=%s", job_id)
        set_job(server, job_id, status="failed", message=str(exc), error=str(exc), completed_at=iso_now())
    else:
        if job_cancel_requested(server, job_id):
            set_job(server, job_id, status="cancelled", message=CANCELLED_JOB_MESSAGE, error=CANCELLED_JOB_MESSAGE, completed_at=iso_now())
        else:
            try:
                with db_session(server.settings.db_path) as conn:  # type: ignore[attr-defined]
                    chart_rows = rows_to_dicts(
                        conn.execute(
                            """
                            SELECT trade_date,total_asset_cny,flow_cny,
                                   daily_return,cumulative_return,drawdown,benchmark_return,payload_json
                            FROM portfolio_daily WHERE run_id=? ORDER BY trade_date
                            """,
                            (result.get("run_id"),),
                        )
                    )
                result["chart"] = columnar_chart_payload(chart_rows, max_points=800)
            except Exception:
                logger.exception("failed to attach initial chart payload job_id=%s", job_id)
            set_job(server, job_id, status="completed", message="回测完成", result=result, completed_at=iso_now())
            if result.get("analysis_pending"):
                schedule_deferred_backtest_analysis(server, result["run_id"], config)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "PortfolioBacktest/1.0"

    def log_message(self, fmt: str, *args) -> None:
        return

    def handle_one_request(self) -> None:
        self._request_started_at = time.perf_counter()
        self._response_status = None
        self._response_bytes = 0
        try:
            super().handle_one_request()
        except Exception:
            logger.exception("http request unhandled method=%s path=%s client=%s", getattr(self, "command", ""), getattr(self, "path", ""), self.client_address[0])
            raise
        finally:
            command = getattr(self, "command", "")
            path = getattr(self, "path", "")
            if command and path:
                logger.info(
                    "http request method=%s path=%s status=%s bytes=%s seconds=%.3f client=%s ua=%s",
                    command,
                    path,
                    getattr(self, "_response_status", None),
                    getattr(self, "_response_bytes", 0),
                    time.perf_counter() - getattr(self, "_request_started_at", time.perf_counter()),
                    self.client_address[0],
                    self.headers.get("User-Agent", ""),
                )

    def send_response(self, code: int, message: str | None = None) -> None:
        self._response_status = code
        super().send_response(code, message)

    @property
    def settings(self):
        return self.server.settings  # type: ignore[attr-defined]

    @property
    def write_lock(self):
        return self.server.write_lock  # type: ignore[attr-defined]

    @property
    def jobs_lock(self):
        return self.server.jobs_lock  # type: ignore[attr-defined]

    @property
    def jobs(self):
        return self.server.jobs  # type: ignore[attr-defined]

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        if length < 0:
            raise BacktestError("Content-Length must be non-negative")
        body = self.rfile.read(length).decode("utf-8")
        payload = json.loads(body) if body else {}
        if not isinstance(payload, dict):
            raise BacktestError("request body must be a JSON object")
        if "config" in payload and not isinstance(payload["config"], dict):
            raise BacktestError("config must be an object")
        return payload

    def identity_cookie_key_id(self) -> str | None:
        raw_cookie = self.headers.get("Cookie") or ""
        if not raw_cookie:
            return None
        try:
            cookies = SimpleCookie()
            cookies.load(raw_cookie)
            morsel = cookies.get(IDENTITY_COOKIE_NAME)
            key_id = morsel.value if morsel else None
        except Exception:
            return None
        return key_id if valid_leaderboard_key_id(key_id) else None

    def current_leaderboard_key_id(self) -> str:
        return self.identity_cookie_key_id() or DEFAULT_LEADERBOARD_KEY_ID

    def identity_cookie_header(self, key_id: str) -> str:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=IDENTITY_COOKIE_MAX_AGE_SECONDS)
        attributes = [
            f"{IDENTITY_COOKIE_NAME}={key_id}",
            "Path=/",
            f"Max-Age={IDENTITY_COOKIE_MAX_AGE_SECONDS}",
            f"Expires={format_datetime(expires_at, usegmt=True)}",
            "HttpOnly",
            "SameSite=Lax",
        ]
        forwarded_proto = (self.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().lower()
        host = (self.headers.get("Host") or "").split(":", 1)[0].strip().lower()
        if forwarded_proto == "https" or host not in {"", "127.0.0.1", "localhost", "::1"}:
            attributes.append("Secure")
        return "; ".join(attributes)

    def send_json(self, status: int, data: object, headers: dict[str, str] | None = None) -> None:
        body = response_bytes(data)
        use_gzip = len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        if use_gzip:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self._response_bytes = len(body)
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            logger.warning("client disconnected while sending json path=%s status=%s bytes=%d", self.path, status, len(body))
            raise

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self._response_bytes = len(body)
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            logger.warning("client disconnected while sending bytes path=%s status=%s bytes=%d", self.path, status, len(body))
            raise

    def normalize_path(self, path: str) -> str:
        if path == "/portfolio":
            return "/"
        if path.startswith("/portfolio/"):
            return path[len("/portfolio") :]
        for detail_path in ("/backtest/permanent-investment", "/backtest/cross-market"):
            if path == detail_path:
                return "/"
            if path.startswith(f"{detail_path}/"):
                return path[len(detail_path) :]
        return path

    def serve_static(self, path: str, head_only: bool = False) -> None:
        is_index = path in {"", "/"}
        if path in {"/backtest", "/backtest/"}:
            file_path = STATIC_DIR / "backtest-index.html"
            is_index = True
        elif path in {"", "/"}:
            file_path = STATIC_DIR / "index.html"
        else:
            relative = path.lstrip("/")
            if relative.startswith("static/"):
                relative = relative[len("static/") :]
            file_path = STATIC_DIR / relative
        file_path = file_path.resolve()
        try:
            file_path.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        use_gzip = (
            file_path.stat().st_size > 1024
            and "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
            and (content_type.startswith("text/") or content_type in {"application/javascript", "application/json"})
        )
        stat = file_path.stat()
        cache_key = (str(file_path), stat.st_mtime_ns, stat.st_size, use_gzip)
        with self.server.static_cache_lock:  # type: ignore[attr-defined]
            body = self.server.static_cache.get(cache_key)  # type: ignore[attr-defined]
        if body is None:
            body = file_path.read_bytes()
            if use_gzip:
                body = gzip.compress(body, compresslevel=5)
            with self.server.static_cache_lock:  # type: ignore[attr-defined]
                if len(self.server.static_cache) >= 32:  # type: ignore[attr-defined]
                    self.server.static_cache.clear()  # type: ignore[attr-defined]
                self.server.static_cache[cache_key] = body  # type: ignore[attr-defined]
        is_versioned = bool(parse_qs(urlparse(self.path).query).get("v"))
        if is_index:
            cache_control = "public, max-age=60, s-maxage=300, stale-while-revalidate=60"
        elif is_versioned:
            cache_control = "public, max-age=31536000, immutable"
        else:
            cache_control = "public, max-age=3600"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("CDN-Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self._response_bytes = len(body)
        if not head_only:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                logger.warning("client disconnected while sending static path=%s bytes=%d", self.path, len(body))
                raise

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        path = self.normalize_path(parsed.path)
        if path.startswith("/api/"):
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
            return
        self.serve_static(path, head_only=True)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = self.normalize_path(parsed.path)
        try:
            if path == "/api/health":
                self.send_json(HTTPStatus.OK, {"ok": True, "service": "portfolio-backtest", "time": iso_now(), "engine_version": BACKTEST_ENGINE_VERSION})
            elif path == "/api/identity":
                key_id = self.identity_cookie_key_id()
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "configured": key_id is not None,
                        "key_hint": key_id[:8] if key_id else None,
                    },
                )
            elif path == "/api/default-config":
                self.send_json(HTTPStatus.OK, default_config())
            elif path == "/api/data/status":
                with db_session(self.settings.db_path) as conn:
                    self.send_json(HTTPStatus.OK, {"status": data_status(conn)})
            elif path == "/api/backtest/history":
                with db_session(self.settings.db_path) as conn:
                    key_id = self.current_leaderboard_key_id()
                    favorites = (parse_qs(parsed.query).get("favorites") or ["0"])[0] == "1"
                    records = backtest_archive_entries(conn, limit=20, favorites_key_id=key_id if favorites else None)
                    self.send_json(HTTPStatus.OK, {"records": add_archive_metadata(conn, records, key_id)})
            elif path == "/api/backtest/leaderboard":
                with db_session(self.settings.db_path) as conn:
                    try:
                        payload = backtest_leaderboard_payload(
                            conn,
                            parse_qs(parsed.query),
                            leaderboard_key_id=self.current_leaderboard_key_id(),
                        )
                        add_archive_metadata(conn, payload["records"], self.current_leaderboard_key_id())
                    except ValueError as exc:
                        self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                        return
                    self.send_json(HTTPStatus.OK, payload)
            elif path.startswith("/api/research/jobs/"):
                self.handle_research_job(path)
            elif path.startswith("/api/backtest/jobs/"):
                self.handle_backtest_job(path)
            elif path.startswith("/api/backtest/"):
                self.handle_backtest_get(path, parse_qs(parsed.query))
            elif path.startswith("/api/"):
                self.send_error_json(HTTPStatus.NOT_FOUND, "unknown API endpoint")
            else:
                self.serve_static(path)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except (BacktestError, ValueError) as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            logger.exception("http get failed path=%s", path)
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = self.normalize_path(parsed.path)
        try:
            payload = self.read_json()
            if path == "/api/identity":
                try:
                    key_id = leaderboard_key_id(payload.get("key"))
                except ValueError as exc:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                self.send_json(
                    HTTPStatus.OK,
                    {"configured": True, "key_hint": key_id[:8]},
                    headers={"Set-Cookie": self.identity_cookie_header(key_id)},
                )
            elif path == "/api/research/start":
                self.handle_research_start(payload)
            elif path.startswith("/api/research/jobs/") and path.endswith("/cancel"):
                self.handle_research_job(path, cancel=True)
            elif path.startswith("/api/backtest/") and path.endswith("/metadata"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    self.send_error_json(HTTPStatus.NOT_FOUND, "unknown API endpoint")
                    return
                run_id = parts[2]
                with self.write_lock:
                    with db_session(self.settings.db_path) as conn:
                        if not conn.execute("SELECT 1 FROM backtest_runs WHERE run_id=?", (run_id,)).fetchone():
                            self.send_error_json(HTTPStatus.NOT_FOUND, "run not found")
                            return
                        metadata = update_run_metadata(conn, self.current_leaderboard_key_id(), run_id, payload)
                self.send_json(HTTPStatus.OK, {"run_id": run_id, "metadata": metadata})
            elif path == "/api/data/sync":
                config = normalize_config(payload.get("config") or payload)
                errors = validate_config(config)
                if errors:
                    raise BacktestError("; ".join(errors))
                data_assets = backtest_assets(config)
                with self.write_lock:
                    with db_session(self.settings.db_path) as conn:
                        result = sync_all(
                            conn,
                            self.settings.tushare_token,
                            config["start_date"],
                            config["end_date"],
                            data_assets,
                            repo_rate_symbol(config),
                        )
                        cache_invalidated = any(
                            int(count or 0) > 0 for count in result.get("inserted", {}).values()
                        )
                        if cache_invalidated:
                            conn.execute("UPDATE backtest_runs SET config_hash=NULL")
                        result["cache_invalidated"] = cache_invalidated
                        result["status"] = data_status(conn)
                self.send_json(HTTPStatus.OK, result)
            elif path == "/api/backtest/run":
                config = normalize_config(payload.get("config") or payload)
                result = execute_backtest_request(
                    self.settings,
                    self.write_lock,
                    config,
                    leaderboard_key_id=self.current_leaderboard_key_id(),
                )
                self.send_json(HTTPStatus.OK, result)
            elif path == "/api/backtest/start":
                config = normalize_config(payload.get("config") or payload)
                errors = validate_config(config)
                if errors:
                    raise BacktestError("; ".join(errors))
                client_request_id = str(payload.get("client_request_id") or "").strip()
                active_key_id = self.current_leaderboard_key_id()
                request_index_key = f"{active_key_id}:{client_request_id}" if client_request_id else ""
                request_config_hash = canonical_config_hash(config)
                job_id = str(uuid.uuid4())
                now_mono = time.monotonic()
                job = {
                    "job_id": job_id,
                    "status": "queued",
                    "message": "回测任务已进入队列",
                    "created_at": iso_now(),
                    "updated_at": iso_now(),
                    "client_request_id": client_request_id or None,
                    "request_index_key": request_index_key or None,
                    "request_config_hash": request_config_hash,
                    "cancel_if_unrequested_seconds": self.server.job_abandoned_seconds,  # type: ignore[attr-defined]
                }
                existing_job = None
                with self.jobs_lock:
                    if client_request_id:
                        existing_job_id = self.server.job_request_index.get(request_index_key)  # type: ignore[attr-defined]
                        existing_job = self.jobs.get(existing_job_id) if existing_job_id else None
                    if existing_job:
                        existing_job = dict(existing_job)
                        if existing_job.get("request_config_hash") == request_config_hash:
                            self.server.job_activity[existing_job_id] = now_mono  # type: ignore[index,attr-defined]
                            existing_job["last_requested_at"] = iso_now()
                            existing_job["updated_at"] = iso_now()
                            self.jobs[existing_job_id] = existing_job  # type: ignore[index]
                    else:
                        # Lookup and registration must share one critical
                        # section: network retries can arrive concurrently.
                        self.jobs[job_id] = job
                        self.server.job_activity[job_id] = now_mono  # type: ignore[attr-defined]
                        self.server.job_cancel_events[job_id] = Event()  # type: ignore[attr-defined]
                        if client_request_id:
                            self.server.job_request_index[request_index_key] = job_id  # type: ignore[attr-defined]
                if existing_job:
                    if existing_job.get("request_config_hash") != request_config_hash:
                        self.send_error_json(HTTPStatus.CONFLICT, "同一次请求的参数已改变，请重新提交回测")
                    else:
                        self.send_json(HTTPStatus.ACCEPTED, existing_job)
                    return
                future = self.server.job_executor.submit(  # type: ignore[attr-defined]
                    run_backtest_job,
                    self.server,
                    job_id,
                    config,
                    active_key_id,
                )
                with self.jobs_lock:
                    self.server.job_futures[job_id] = future  # type: ignore[attr-defined]
                self.send_json(HTTPStatus.ACCEPTED, job)
            elif path.startswith("/api/backtest/") and path.endswith("/analysis"):
                parts = [part for part in path.split("/") if part]
                if len(parts) != 4:
                    self.send_error_json(HTTPStatus.NOT_FOUND, "unknown API endpoint")
                    return
                run_id = parts[2]
                with self.write_lock:
                    with db_session(self.settings.db_path) as conn:
                        row = conn.execute("SELECT config_json,summary_json FROM backtest_runs WHERE run_id=?", (run_id,)).fetchone()
                        if not row:
                            self.send_error_json(HTTPStatus.NOT_FOUND, "run not found")
                            return
                        config = json_loads(row["config_json"], {})
                        summary = json_loads(row["summary_json"], {})
                        if int(summary.get("engine_version") or 0) != BACKTEST_ENGINE_VERSION:
                            self.send_error_json(HTTPStatus.CONFLICT, "此历史记录使用旧版计算逻辑，请重新运行回测后分析")
                            return
                        status = summary.get("analysis_status", "completed")
                        if status not in {"completed", "not_required"}:
                            status = "pending" if extended_analysis_required(config) else "not_required"
                            set_persisted_analysis_status(conn, {"run_id": run_id, "summary": summary}, status)
                if status == "pending":
                    schedule_deferred_backtest_analysis(self.server, run_id, config)
                self.send_json(HTTPStatus.ACCEPTED, {"run_id": run_id, "analysis_status": status})
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "unknown API endpoint")
        except (BrokenPipeError, ConnectionResetError):
            raise
        except (BacktestError, ValueError, UnicodeDecodeError) as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            logger.exception("http post failed path=%s", path)
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = self.normalize_path(parsed.path)
        parts = [part for part in path.split("/") if part]
        if len(parts) != 3 or parts[:2] != ["api", "backtest"]:
            self.send_error_json(HTTPStatus.NOT_FOUND, "unknown API endpoint")
            return
        run_id = parts[2]
        try:
            with self.write_lock:
                with db_session(self.settings.db_path) as conn:
                    existing = conn.execute("SELECT 1 FROM backtest_runs WHERE run_id=?", (run_id,)).fetchone()
                    conn.execute("DELETE FROM portfolio_daily WHERE run_id=?", (run_id,))
                    conn.execute("DELETE FROM trades WHERE run_id=?", (run_id,))
                    conn.execute("DELETE FROM rebalance_events WHERE run_id=?", (run_id,))
                    conn.execute("DELETE FROM leaderboard_memberships WHERE run_id=?", (run_id,))
                    conn.execute("DELETE FROM run_metadata WHERE run_id=?", (run_id,))
                    conn.execute("DELETE FROM backtest_runs WHERE run_id=?", (run_id,))
            with self.server.diagnostics_cache_lock:  # type: ignore[attr-defined]
                for key in list(self.server.diagnostics_cache):  # type: ignore[attr-defined]
                    if key[0] == run_id:
                        self.server.diagnostics_cache.pop(key, None)  # type: ignore[attr-defined]
            # The browser retries after a lost response. A successful deletion
            # remains successful when that same request arrives again.
            self.send_json(HTTPStatus.OK, {"deleted": run_id, "already_deleted": not bool(existing)})
        except Exception as exc:
            logger.exception("http delete failed path=%s", path)
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def handle_backtest_get(self, path: str, query: dict[str, list[str]]) -> None:
        parts = [part for part in path.split("/") if part]
        if len(parts) < 3 or len(parts) > 4:
            self.send_error_json(HTTPStatus.NOT_FOUND, "missing run_id")
            return
        run_id = parts[2]
        section = parts[3] if len(parts) > 3 else ""
        with db_session(self.settings.db_path) as conn:
            run = conn.execute("SELECT * FROM backtest_runs WHERE run_id=?", (run_id,)).fetchone()
            if not run:
                self.send_error_json(HTTPStatus.NOT_FOUND, "run not found")
                return
            if not section:
                saved_config = json_loads(run["config_json"], {})
                saved_summary = json_loads(run["summary_json"], {})
                if (
                    saved_summary.get("analysis_status") in {"pending", "running"}
                    and int(saved_summary.get("engine_version") or 0) == BACKTEST_ENGINE_VERSION
                    and extended_analysis_required(saved_config)
                ):
                    # Resume interrupted background analysis when a saved run
                    # is reopened after a process restart.
                    schedule_deferred_backtest_analysis(self.server, run_id, saved_config)
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "run_id": run_id,
                        "created_at": run["created_at"],
                        "config": saved_config,
                        "metadata": get_run_metadata(conn, self.current_leaderboard_key_id(), run_id),
                        "summary": {
                            **saved_summary,
                            "requires_recalculation": int(saved_summary.get("engine_version") or 0) < BACKTEST_ENGINE_VERSION,
                        },
                    },
                )
                return
            if section == "series":
                rows = rows_to_dicts(
                    conn.execute(
                        """
                        SELECT trade_date,total_asset_cny,flow_cny,
                               daily_return,cumulative_return,drawdown,benchmark_return,payload_json
                        FROM portfolio_daily WHERE run_id=? ORDER BY trade_date
                        """,
                        (run_id,),
                    )
                )
                for row in rows:
                    row["payload"] = json_loads(row.pop("payload_json"), {})
                self.send_json(HTTPStatus.OK, {"series": rows})
            elif section == "chart-series":
                focus_dates = parse_qs(urlparse(self.path).query, keep_blank_values=True).get("focus_date")
                if focus_dates is not None:
                    if len(focus_dates) != 1 or len(focus_dates[0]) != 10:
                        raise ValueError("focus_date must be a single date in YYYY-MM-DD format")
                    try:
                        valid_date = date.fromisoformat(focus_dates[0]).isoformat() == focus_dates[0]
                    except ValueError:
                        valid_date = False
                    if not valid_date:
                        raise ValueError("focus_date must be a valid date in YYYY-MM-DD format")
                rows = rows_to_dicts(
                    conn.execute(
                        """
                        SELECT trade_date,total_asset_cny,flow_cny,
                               daily_return,cumulative_return,drawdown,benchmark_return,payload_json
                        FROM portfolio_daily WHERE run_id=? ORDER BY trade_date
                        """,
                        (run_id,),
                    )
                )
                if focus_dates and not any(row["trade_date"] == focus_dates[0] for row in rows):
                    self.send_error_json(HTTPStatus.NOT_FOUND, "focus_date is not a trading date in this backtest")
                    return
                self.send_json(HTTPStatus.OK, {"chart": columnar_chart_payload(rows, required_dates=set(focus_dates or []))})
            elif section == "daily-pnl":
                rows = rows_to_dicts(
                    conn.execute(
                        """
                        SELECT trade_date,total_asset_cny,flow_cny,daily_return,
                               cumulative_return,drawdown,benchmark_return,payload_json
                        FROM portfolio_daily WHERE run_id=? ORDER BY trade_date
                        """,
                        (run_id,),
                    )
                )
                self.send_json(
                    HTTPStatus.OK,
                    {"daily_pnl": daily_pnl_chart_payload(rows, json_loads(run["config_json"], {}))},
                )
            elif section == "asset-comovement":
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "asset_comovement": asset_comovement_statistics(
                            conn,
                            json_loads(run["config_json"], {}),
                        )
                    },
                )
            elif section == "strategy-diagnostics":
                saved_summary = json_loads(run["summary_json"], {})
                if int(saved_summary.get("engine_version") or 0) != BACKTEST_ENGINE_VERSION:
                    self.send_error_json(HTTPStatus.CONFLICT, "此历史记录使用旧版计算逻辑，请重新运行回测后分析")
                    return
                window_key = str((query.get("window") or ["all"])[0] or "all")
                cache_key = (run_id, window_key)
                with self.server.diagnostics_cache_lock:  # type: ignore[attr-defined]
                    diagnostics = self.server.diagnostics_cache.get(cache_key)  # type: ignore[attr-defined]
                    if diagnostics is None:
                        diagnostics = strategy_diagnostics(
                            conn,
                            json_loads(run["config_json"], {}),
                            saved_summary,
                            window_key,
                        )
                        if len(self.server.diagnostics_cache) >= 32:  # type: ignore[attr-defined]
                            self.server.diagnostics_cache.clear()  # type: ignore[attr-defined]
                        self.server.diagnostics_cache[cache_key] = diagnostics  # type: ignore[index,attr-defined]
                self.send_json(HTTPStatus.OK, {"strategy_diagnostics": diagnostics})
            elif section == "export.csv":
                selected_symbols = [
                    symbol.strip()
                    for symbol in str((query.get("symbols") or [""])[0]).split(",")
                    if symbol.strip()
                ]
                body, filename = build_backtest_csv(
                    conn,
                    json_loads(run["config_json"], {}),
                    json_loads(run["summary_json"], {}),
                    run_id=run_id,
                    start_date=(query.get("start_date") or [None])[0],
                    end_date=(query.get("end_date") or [None])[0],
                    symbols=selected_symbols or None,
                )
                self.send_bytes(
                    HTTPStatus.OK,
                    body,
                    "text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'},
                )
            elif section == "rebalance":
                rows = rows_to_dicts(
                    conn.execute(
                        """
                        SELECT rebalance_date,period_return,total_asset_before,total_asset_after,turnover_cny,fee_cny,payload_json
                        FROM rebalance_events WHERE run_id=? ORDER BY rebalance_date
                        """,
                        (run_id,),
                    )
                )
                for row in rows:
                    row["payload"] = rebalance_display_payload(json_loads(row.pop("payload_json"), {}))
                self.send_json(HTTPStatus.OK, {"rebalance": rows})
            elif section == "trades":
                rows = rows_to_dicts(
                    conn.execute(
                        """
                        SELECT trade_date,symbol,side,quantity,price,gross_amount,fee,currency,reason
                        FROM trades WHERE run_id=? ORDER BY trade_date
                        """,
                        (run_id,),
                    )
                )
                self.send_json(HTTPStatus.OK, {"trades": rows})
            elif section == "positions":
                limit = int((query.get("limit") or ["30"])[0])
                rows = rows_to_dicts(
                    conn.execute(
                        """
                        SELECT trade_date,total_asset_cny,payload_json FROM portfolio_daily
                        WHERE run_id=? ORDER BY trade_date DESC LIMIT ?
                        """,
                        (run_id, limit),
                    )
                )
                for row in rows:
                    row["payload"] = json_loads(row.pop("payload_json"), {})
                self.send_json(HTTPStatus.OK, {"positions": rows})
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "unknown backtest section")

    def handle_research_start(self, payload: dict) -> None:
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must identify a saved backtest")
        with db_session(self.settings.db_path) as conn:
            row = conn.execute("SELECT config_json,summary_json FROM backtest_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                self.send_error_json(HTTPStatus.NOT_FOUND, "run not found")
                return
            base_config = json_loads(row["config_json"], {})
            summary = json_loads(row["summary_json"], {})
        if int(summary.get("engine_version") or 0) != BACKTEST_ENGINE_VERSION:
            self.send_error_json(HTTPStatus.CONFLICT, "此历史记录使用旧版计算逻辑，请重新运行回测后研究")
            return
        normalized = validate_research_request(base_config, payload)
        key_id = self.current_leaderboard_key_id()
        cleanup_research_jobs(self.server)
        with self.server.research_jobs_lock:
            if self.server.research_closing:
                self.send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, "研究服务正在关闭，请稍后重试")
                return
            active = [job for job in self.server.research_jobs.values() if job["_active"]]
            owned = next((job for job in active if job["_owner_key_id"] == key_id), None)
            if owned:
                self.send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "当前 Key 已有研究任务，请等待结束或取消", "job_id": owned["job_id"]})
                return
            if len(active) >= self.server.research_max_pending:
                self.send_error_json(HTTPStatus.TOO_MANY_REQUESTS, "研究队列已满，请稍后重试")
                return
            job_id = str(uuid.uuid4())
            job = {
                "job_id": job_id, "run_id": run_id, "kind": normalized["kind"], "status": "queued",
                "completed": 0, "total": normalized["total"], "rows": [], "created_at": iso_now(), "updated_at": iso_now(),
                "_owner_key_id": key_id, "_active": True,
            }
            self.server.research_jobs[job_id] = job
            self.server.research_activity[job_id] = time.monotonic()
            self.server.research_cancel_events[job_id] = Event()
            try:
                # Register the future while holding the lock so cancellation
                # cannot miss a submitted-but-unregistered queued job.
                self.server.research_futures[job_id] = self.server.research_executor.submit(
                    run_research_job, self.server, job_id, deepcopy(base_config), deepcopy(normalized),
                )
            except RuntimeError:
                self.server.research_jobs.pop(job_id, None)
                self.server.research_activity.pop(job_id, None)
                self.server.research_cancel_events.pop(job_id, None)
                self.send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, "研究服务正在关闭，请稍后重试")
                return
            response = research_job_payload(job)
        self.send_json(HTTPStatus.ACCEPTED, response)

    def handle_research_job(self, path: str, cancel: bool = False) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != (5 if cancel else 4):
            self.send_error_json(HTTPStatus.NOT_FOUND, "unknown research endpoint")
            return
        job_id = parts[3]
        cleanup_research_jobs(self.server)
        with self.server.research_jobs_lock:
            job = self.server.research_jobs.get(job_id)
            if not job or job["_owner_key_id"] != self.current_leaderboard_key_id():
                self.send_error_json(HTTPStatus.NOT_FOUND, "research job not found")
                return
            self.server.research_activity[job_id] = time.monotonic()
        if cancel:
            cancel_research_job(self.server, job_id)
        with self.server.research_jobs_lock:
            response = research_job_payload(self.server.research_jobs[job_id])
        self.send_json(HTTPStatus.OK, response)

    def handle_backtest_job(self, path: str) -> None:
        parts = [part for part in path.split("/") if part]
        if len(parts) != 4:
            self.send_error_json(HTTPStatus.NOT_FOUND, "missing job_id")
            return
        job_id = parts[3]
        cleanup_jobs(self.server)
        with self.jobs_lock:
            job = self.jobs.get(job_id)
        if not job:
            self.send_error_json(HTTPStatus.NOT_FOUND, "job not found")
            return
        if job.get("status") in {"queued", "running"}:
            record_job_request(self.server, job_id)
            with self.jobs_lock:
                job = self.jobs.get(job_id, job)
        self.send_json(HTTPStatus.OK, job)


def create_server(host: str = "127.0.0.1", port: int = 8000, db_path: str | Path | None = None) -> ThreadingHTTPServer:
    settings = get_settings(db_path)
    init_db(settings.db_path)
    server = PortfolioServer((host, port), ApiHandler)
    server.settings = settings  # type: ignore[attr-defined]
    server.write_lock = Lock()  # type: ignore[attr-defined]
    server.jobs_lock = Lock()  # type: ignore[attr-defined]
    server.jobs = {}  # type: ignore[attr-defined]
    server.job_activity = {}  # type: ignore[attr-defined]
    server.job_cancel_events = {}  # type: ignore[attr-defined]
    server.job_futures = {}  # type: ignore[attr-defined]
    server.job_request_index = {}  # type: ignore[attr-defined]
    server.analysis_lock = Lock()  # type: ignore[attr-defined]
    server.analysis_futures = {}  # type: ignore[attr-defined]
    server.research_jobs_lock = Lock()  # type: ignore[attr-defined]
    server.research_closing = False  # type: ignore[attr-defined]
    server.research_jobs = {}  # type: ignore[attr-defined]
    server.research_activity = {}  # type: ignore[attr-defined]
    server.research_cancel_events = {}  # type: ignore[attr-defined]
    server.research_futures = {}  # type: ignore[attr-defined]
    server.research_max_pending = MAX_RESEARCH_PENDING_JOBS  # type: ignore[attr-defined]
    server.research_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="research")  # type: ignore[attr-defined]
    server.static_cache_lock = Lock()  # type: ignore[attr-defined]
    server.static_cache = {}  # type: ignore[attr-defined]
    server.diagnostics_cache_lock = Lock()  # type: ignore[attr-defined]
    server.diagnostics_cache = {}  # type: ignore[attr-defined]
    server.job_abandoned_seconds = float(os.getenv("PORTFOLIO_JOB_ABANDONED_SECONDS", DEFAULT_ABANDONED_JOB_SECONDS))  # type: ignore[attr-defined]
    server.job_retention_seconds = float(os.getenv("PORTFOLIO_JOB_RETENTION_SECONDS", DEFAULT_JOB_RETENTION_SECONDS))  # type: ignore[attr-defined]
    server.job_executor = ThreadPoolExecutor(max_workers=1)  # type: ignore[attr-defined]
    server.analysis_executor = ThreadPoolExecutor(max_workers=1)  # type: ignore[attr-defined]
    server.job_monitor_stop = Event()  # type: ignore[attr-defined]
    server.job_monitor_thread = Thread(target=monitor_jobs, args=(server,), daemon=True)  # type: ignore[attr-defined]
    server.job_monitor_thread.start()  # type: ignore[attr-defined]
    return server


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db-path", default=None)
    args = parser.parse_args()
    server = create_server(args.host, args.port, args.db_path)
    print(f"Serving on http://{server.server_address[0]}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
