from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import gzip
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
import logging
import mimetypes
import os
from pathlib import Path
from threading import Event, Lock, Thread
import time
from urllib.parse import parse_qs, urlparse
import uuid

from app.config import backtest_assets, repo_rate_symbol, STATIC_DIR, default_config, get_settings, normalize_config
from app.db import data_status, db_session, init_db, json_loads, rows_to_dicts
from app.services.backtest_engine import BacktestCancelled, BacktestError, get_cached_backtest_run, run_backtest
from app.services.data_sync import required_data_missing, sync_all

logger = logging.getLogger(__name__)
MAX_LOG_BYTES = 5 * 1024 * 1024
DEFAULT_ABANDONED_JOB_SECONDS = 120.0
DEFAULT_JOB_RETENTION_SECONDS = 600.0
JOB_CLEANUP_INTERVAL_SECONDS = 5.0
CANCELLED_JOB_MESSAGE = "任务已取消：页面没有继续请求结果"


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


def columnar_chart_payload(rows: list[dict], max_points: int = 1000) -> dict:
    """Return chart data as compact arrays instead of repeating JSON keys per day."""
    source_points = len(rows)
    if source_points > max_points:
        indices = sorted({round(index * (source_points - 1) / (max_points - 1)) for index in range(max_points)})
        rows = [rows[index] for index in indices]
    chart = {
        "source_points": source_points,
        "display_points": len(rows),
        "dates": [],
        "total_assets": [],
        "daily_returns": [],
        "cumulative_returns": [],
        "drawdowns": [],
        "benchmark_returns": [],
        "comparison_total_assets": [],
        "weights": {},
    }
    parsed_payloads: list[dict] = []
    symbols: list[str] = []
    for row in rows:
        payload = json_loads(row.pop("payload_json"), {})
        parsed_payloads.append(payload)
        for symbol in payload.get("weights", {}):
            if symbol not in symbols:
                symbols.append(symbol)
    chart["weights"] = {symbol: [] for symbol in symbols}
    for row, payload in zip(rows, parsed_payloads):
        chart["dates"].append(row["trade_date"])
        chart["total_assets"].append(row["total_asset_cny"])
        chart["daily_returns"].append(row["daily_return"])
        chart["cumulative_returns"].append(row["cumulative_return"])
        chart["drawdowns"].append(row["drawdown"])
        chart["benchmark_returns"].append(row["benchmark_return"])
        chart["comparison_total_assets"].append(payload.get("comparison", {}).get("total_asset_cny"))
        weights = payload.get("weights", {})
        for symbol in symbols:
            chart["weights"][symbol].append(weights.get(symbol, 0.0))
    return chart


def rebalance_display_payload(payload: dict) -> dict:
    return {
        key: payload.get(key)
        for key in (
            "decision_date",
            "year_return",
            "year_max_drawdown",
            "year_fee_cny",
            "year_asset_performance",
            "asset_performance",
            "period_max_drawdown",
        )
        if key in payload
    }


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


def execute_backtest_request(settings, write_lock: Lock, config: dict, should_cancel=None) -> dict:
    request_id = str(uuid.uuid4())[:8]
    started_at = time.perf_counter()
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
                )
                logger.info("backtest sync complete id=%s seconds=%.3f result=%s", request_id, time.perf_counter() - sync_started_at, sync_result)
            raise_if_cancelled(should_cancel)
            with db_session(settings.db_path) as conn:
                missing_after_started_at = time.perf_counter()
                missing_after = required_data_missing(conn, config["start_date"], config["end_date"], data_assets, rate_symbol)
                logger.info("backtest post-sync data check id=%s seconds=%.3f missing=%s", request_id, time.perf_counter() - missing_after_started_at, missing_after)
            if missing_after:
                raise BacktestError("自动补足数据后仍缺少：" + "、".join(missing_after))
            inserted = (sync_result or {}).get("inserted", {})
            cache_invalidated = any(item.startswith("generated:") for item in missing_before) or any(int(count or 0) > 0 for count in inserted.values())

        with db_session(settings.db_path) as conn:
            raise_if_cancelled(should_cancel)
            if cache_invalidated:
                invalidate_started_at = time.perf_counter()
                conn.execute("DELETE FROM portfolio_daily")
                conn.execute("DELETE FROM trades")
                conn.execute("DELETE FROM rebalance_events")
                conn.execute("DELETE FROM backtest_runs")
                logger.info("backtest cache invalidated id=%s seconds=%.3f", request_id, time.perf_counter() - invalidate_started_at)
            run_started_at = time.perf_counter()
            result = run_backtest(conn, config, should_cancel=should_cancel)
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
    def server_close(self) -> None:
        stop_event = getattr(self, "job_monitor_stop", None)
        if stop_event is not None:
            stop_event.set()
        monitor = getattr(self, "job_monitor_thread", None)
        if monitor is not None:
            monitor.join(timeout=2)
        executor = getattr(self, "job_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
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
            client_request_id = job.get("client_request_id") if job else None
            if client_request_id:
                server.job_request_index.pop(client_request_id, None)  # type: ignore[attr-defined]
    for job_id in to_cancel:
        cancel_job(server, job_id)
    if to_delete:
        logger.info("backtest jobs cleaned count=%d", len(to_delete))


def monitor_jobs(server) -> None:
    stop_event = server.job_monitor_stop  # type: ignore[attr-defined]
    while not stop_event.wait(JOB_CLEANUP_INTERVAL_SECONDS):
        cleanup_jobs(server)


def run_backtest_job(server, job_id: str, config: dict) -> None:
    set_job(server, job_id, status="running", message="正在检查数据并运行回测", started_at=iso_now())
    try:
        result = execute_backtest_request(
            server.settings,  # type: ignore[attr-defined]
            server.write_lock,  # type: ignore[attr-defined]
            config,
            should_cancel=lambda: job_cancel_requested(server, job_id),
        )
    except BacktestCancelled as exc:
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
            set_job(server, job_id, status="completed", message="回测完成", result=result, completed_at=iso_now())


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
        body = self.rfile.read(length).decode("utf-8")
        return json.loads(body) if body else {}

    def send_json(self, status: int, data: object) -> None:
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
        self.end_headers()
        self._response_bytes = len(body)
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            logger.warning("client disconnected while sending json path=%s status=%s bytes=%d", self.path, status, len(body))
            raise

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

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
        if not str(file_path).startswith(str(STATIC_DIR.resolve())) or not file_path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        body = file_path.read_bytes()
        use_gzip = (
            len(body) > 1024
            and "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
            and (content_type.startswith("text/") or content_type in {"application/javascript", "application/json"})
        )
        if use_gzip:
            body = gzip.compress(body, compresslevel=5)
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
            if path == "/api/default-config":
                self.send_json(HTTPStatus.OK, default_config())
            elif path == "/api/data/status":
                with db_session(self.settings.db_path) as conn:
                    self.send_json(HTTPStatus.OK, {"status": data_status(conn)})
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
        except Exception as exc:
            logger.exception("http get failed path=%s", path)
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = self.normalize_path(parsed.path)
        try:
            payload = self.read_json()
            if path == "/api/data/sync":
                config = normalize_config(payload.get("config") or payload)
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
                        result["status"] = data_status(conn)
                self.send_json(HTTPStatus.OK, result)
            elif path == "/api/backtest/run":
                config = normalize_config(payload.get("config") or payload)
                result = execute_backtest_request(self.settings, self.write_lock, config)
                self.send_json(HTTPStatus.OK, result)
            elif path == "/api/backtest/start":
                config = normalize_config(payload.get("config") or payload)
                client_request_id = str(payload.get("client_request_id") or "").strip()
                job_id = str(uuid.uuid4())
                now_mono = time.monotonic()
                if client_request_id:
                    with self.jobs_lock:
                        existing_job_id = self.server.job_request_index.get(client_request_id)  # type: ignore[attr-defined]
                        existing_job = self.jobs.get(existing_job_id) if existing_job_id else None
                        if existing_job:
                            self.server.job_activity[existing_job_id] = now_mono  # type: ignore[index,attr-defined]
                            existing_job["last_requested_at"] = iso_now()
                            existing_job["updated_at"] = iso_now()
                            self.jobs[existing_job_id] = existing_job  # type: ignore[index]
                            self.send_json(HTTPStatus.ACCEPTED, existing_job)
                            return
                job = {
                    "job_id": job_id,
                    "status": "queued",
                    "message": "回测任务已进入队列",
                    "created_at": iso_now(),
                    "updated_at": iso_now(),
                    "client_request_id": client_request_id or None,
                    "cancel_if_unrequested_seconds": self.server.job_abandoned_seconds,  # type: ignore[attr-defined]
                }
                with self.jobs_lock:
                    self.jobs[job_id] = job
                    self.server.job_activity[job_id] = now_mono  # type: ignore[attr-defined]
                    self.server.job_cancel_events[job_id] = Event()  # type: ignore[attr-defined]
                    if client_request_id:
                        self.server.job_request_index[client_request_id] = job_id  # type: ignore[attr-defined]
                future = self.server.job_executor.submit(run_backtest_job, self.server, job_id, config)  # type: ignore[attr-defined]
                with self.jobs_lock:
                    self.server.job_futures[job_id] = future  # type: ignore[attr-defined]
                self.send_json(HTTPStatus.ACCEPTED, job)
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "unknown API endpoint")
        except (BrokenPipeError, ConnectionResetError):
            raise
        except BacktestError as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            logger.exception("http post failed path=%s", path)
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def handle_backtest_get(self, path: str, query: dict[str, list[str]]) -> None:
        parts = [part for part in path.split("/") if part]
        if len(parts) < 3:
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
                self.send_json(
                    HTTPStatus.OK,
                    {
                        "run_id": run_id,
                        "created_at": run["created_at"],
                        "config": json_loads(run["config_json"]),
                        "summary": json_loads(run["summary_json"]),
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
                self.send_json(HTTPStatus.OK, {"chart": columnar_chart_payload(rows)})
            elif section == "rebalance":
                rows = rows_to_dicts(
                    conn.execute(
                        """
                        SELECT rebalance_date,period_return,fee_cny,payload_json
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
    server.job_abandoned_seconds = float(os.getenv("PORTFOLIO_JOB_ABANDONED_SECONDS", DEFAULT_ABANDONED_JOB_SECONDS))  # type: ignore[attr-defined]
    server.job_retention_seconds = float(os.getenv("PORTFOLIO_JOB_RETENTION_SECONDS", DEFAULT_JOB_RETENTION_SECONDS))  # type: ignore[attr-defined]
    server.job_executor = ThreadPoolExecutor(max_workers=1)  # type: ignore[attr-defined]
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
