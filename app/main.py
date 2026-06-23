from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.config import STATIC_DIR, default_config, get_settings, normalize_config
from app.db import data_status, db_session, init_db, json_loads, rows_to_dicts
from app.services.backtest_engine import BacktestError, run_backtest
from app.services.data_sync import required_data_missing, sync_all


def response_bytes(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "PortfolioBacktest/1.0"

    def log_message(self, fmt: str, *args) -> None:
        return

    @property
    def settings(self):
        return self.server.settings  # type: ignore[attr-defined]

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
                with db_session(self.settings.db_path) as conn:
                    missing_before = required_data_missing(conn, config["start_date"], config["end_date"], config["assets"], config["repo_symbol"])
                    sync_result = None
                    if missing_before:
                        sync_result = sync_all(
                            conn,
                            self.settings.tushare_token,
                            config["start_date"],
                            config["end_date"],
                            config["assets"],
                            config["repo_symbol"],
                        )
                        missing_after = required_data_missing(conn, config["start_date"], config["end_date"], config["assets"], config["repo_symbol"])
                        if missing_after:
                            message = "自动补足数据后仍缺少：" + "、".join(missing_after)
                            self.send_error_json(HTTPStatus.BAD_REQUEST, message)
                            return
                    result = run_backtest(conn, config)
                    result["data_sync"] = {
                        "triggered": bool(missing_before),
                        "missing_before": missing_before,
                        "result": sync_result,
                    }
                    result["status"] = data_status(conn)
                self.send_json(HTTPStatus.OK, result)
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
                        SELECT trade_date,total_asset_cny,flow_cny,daily_return,cumulative_return,drawdown,benchmark_return,payload_json
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


def create_server(host: str = "127.0.0.1", port: int = 8000, db_path: str | Path | None = None) -> ThreadingHTTPServer:
    settings = get_settings(db_path)
    init_db(settings.db_path)
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.settings = settings  # type: ignore[attr-defined]
    return server


def main() -> None:
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
