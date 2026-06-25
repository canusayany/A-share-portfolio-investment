from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
import logging
import mimetypes
import os
from pathlib import Path
from threading import Lock
import time
from urllib.parse import parse_qs, urlparse
import uuid

from app.config import STATIC_DIR, default_config, get_settings, normalize_config
from app.db import data_status, db_session, init_db, json_loads, rows_to_dicts
from app.services.backtest_engine import BacktestError, get_cached_backtest_run, run_backtest
from app.services.data_sync import required_data_missing, sync_all

logger = logging.getLogger(__name__)


def response_bytes(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def execute_backtest_request(settings, write_lock: Lock, config: dict) -> dict:
    request_id = str(uuid.uuid4())[:8]
    started_at = time.perf_counter()
    logger.info(
        "backtest request start id=%s range=%s..%s repo=%s assets=%s",
        request_id,
        config["start_date"],
        config["end_date"],
        config["repo_symbol"],
        [asset["symbol"] for asset in config["assets"] if asset.get("enabled", True)],
    )
    with write_lock:
        lock_acquired_at = time.perf_counter()
        logger.info("backtest lock acquired id=%s wait_seconds=%.3f", request_id, lock_acquired_at - started_at)
        with db_session(settings.db_path) as conn:
            missing_started_at = time.perf_counter()
            missing_before = required_data_missing(conn, config["start_date"], config["end_date"], config["assets"], config["repo_symbol"])
            logger.info("backtest data check id=%s seconds=%.3f missing=%s", request_id, time.perf_counter() - missing_started_at, missing_before)
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
            with db_session(settings.db_path) as conn:
                sync_started_at = time.perf_counter()
                sync_result = sync_all(
                    conn,
                    settings.tushare_token,
                    config["start_date"],
                    config["end_date"],
                    config["assets"],
                    config["repo_symbol"],
                    missing_items=missing_before,
                )
                logger.info("backtest sync complete id=%s seconds=%.3f result=%s", request_id, time.perf_counter() - sync_started_at, sync_result)
            with db_session(settings.db_path) as conn:
                missing_after_started_at = time.perf_counter()
                missing_after = required_data_missing(conn, config["start_date"], config["end_date"], config["assets"], config["repo_symbol"])
                logger.info("backtest post-sync data check id=%s seconds=%.3f missing=%s", request_id, time.perf_counter() - missing_after_started_at, missing_after)
            if missing_after:
                raise BacktestError("自动补足数据后仍缺少：" + "、".join(missing_after))
            inserted = (sync_result or {}).get("inserted", {})
            cache_invalidated = any(item.startswith("generated:") for item in missing_before) or any(int(count or 0) > 0 for count in inserted.values())

        with db_session(settings.db_path) as conn:
            if cache_invalidated:
                invalidate_started_at = time.perf_counter()
                conn.execute("DELETE FROM portfolio_daily")
                conn.execute("DELETE FROM trades")
                conn.execute("DELETE FROM rebalance_events")
                conn.execute("DELETE FROM backtest_runs")
                logger.info("backtest cache invalidated id=%s seconds=%.3f", request_id, time.perf_counter() - invalidate_started_at)
            run_started_at = time.perf_counter()
            result = run_backtest(conn, config)
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


def run_backtest_job(server, job_id: str, config: dict) -> None:
    set_job(server, job_id, status="running", message="正在检查数据并运行回测", started_at=iso_now())
    try:
        result = execute_backtest_request(server.settings, server.write_lock, config)  # type: ignore[attr-defined]
    except BacktestError as exc:
        set_job(server, job_id, status="failed", message=str(exc), error=str(exc), completed_at=iso_now())
    except Exception as exc:
        set_job(server, job_id, status="failed", message=str(exc), error=str(exc), completed_at=iso_now())
    else:
        set_job(server, job_id, status="completed", message="回测完成", result=result, completed_at=iso_now())


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "PortfolioBacktest/1.0"

    def log_message(self, fmt: str, *args) -> None:
        return

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
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def normalize_path(self, path: str) -> str:
        if path == "/portfolio":
            return "/"
        if path.startswith("/portfolio/"):
            return path[len("/portfolio") :]
        return path

    def serve_static(self, path: str, head_only: bool = False) -> None:
        if path in {"", "/"}:
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
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

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
        except Exception as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = self.normalize_path(parsed.path)
        try:
            payload = self.read_json()
            if path == "/api/data/sync":
                config = normalize_config(payload.get("config") or payload)
                with self.write_lock:
                    with db_session(self.settings.db_path) as conn:
                        result = sync_all(
                            conn,
                            self.settings.tushare_token,
                            config["start_date"],
                            config["end_date"],
                            config["assets"],
                            config["repo_symbol"],
                        )
                        result["status"] = data_status(conn)
                self.send_json(HTTPStatus.OK, result)
            elif path == "/api/backtest/run":
                config = normalize_config(payload.get("config") or payload)
                result = execute_backtest_request(self.settings, self.write_lock, config)
                self.send_json(HTTPStatus.OK, result)
            elif path == "/api/backtest/start":
                config = normalize_config(payload.get("config") or payload)
                job_id = str(uuid.uuid4())
                job = {
                    "job_id": job_id,
                    "status": "queued",
                    "message": "回测任务已进入队列",
                    "created_at": iso_now(),
                    "updated_at": iso_now(),
                }
                with self.jobs_lock:
                    self.jobs[job_id] = job
                self.server.job_executor.submit(run_backtest_job, self.server, job_id, config)  # type: ignore[attr-defined]
                self.send_json(HTTPStatus.ACCEPTED, job)
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "unknown API endpoint")
        except BacktestError as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
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
                        SELECT trade_date,total_asset_cny,flow_cny,payload_json
                        FROM portfolio_daily WHERE run_id=? ORDER BY trade_date
                        """,
                        (run_id,),
                    )
                )
                for row in rows:
                    row["payload"] = json_loads(row.pop("payload_json"), {})
                self.send_json(HTTPStatus.OK, {"series": rows})
            elif section == "rebalance":
                rows = rows_to_dicts(conn.execute("SELECT * FROM rebalance_events WHERE run_id=? ORDER BY rebalance_date", (run_id,)))
                for row in rows:
                    row["payload"] = json_loads(row.pop("payload_json"), {})
                self.send_json(HTTPStatus.OK, {"rebalance": rows})
            elif section == "trades":
                rows = rows_to_dicts(conn.execute("SELECT * FROM trades WHERE run_id=? ORDER BY trade_date", (run_id,)))
                for row in rows:
                    row["payload"] = json_loads(row.pop("payload_json"), {})
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
        with self.jobs_lock:
            job = self.jobs.get(job_id)
        if not job:
            self.send_error_json(HTTPStatus.NOT_FOUND, "job not found")
            return
        self.send_json(HTTPStatus.OK, job)


def create_server(host: str = "127.0.0.1", port: int = 8000, db_path: str | Path | None = None) -> ThreadingHTTPServer:
    settings = get_settings(db_path)
    init_db(settings.db_path)
    server = PortfolioServer((host, port), ApiHandler)
    server.settings = settings  # type: ignore[attr-defined]
    server.write_lock = Lock()  # type: ignore[attr-defined]
    server.jobs_lock = Lock()  # type: ignore[attr-defined]
    server.jobs = {}  # type: ignore[attr-defined]
    server.job_executor = ThreadPoolExecutor(max_workers=1)  # type: ignore[attr-defined]
    return server


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("PORTFOLIO_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
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
