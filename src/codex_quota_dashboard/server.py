"""Loopback-first web service for frozen M1-M2-M3 snapshots."""

from __future__ import annotations

from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import ipaddress
import json
import mimetypes
import sys

from . import __version__
from .forecast_v2.live_m2 import quote_m2
from .models import parse_timestamp
from .system import load_snapshot


LOOPBACK_BINDS = {"127.0.0.1", "localhost", "::1"}
STATIC_FILES = {"index.html", "styles.css", "app.js", "vendor/echarts.min.js"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
}


class QuotaHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class QuotaHandler(BaseHTTPRequestHandler):
    server_version = f"CodexQuotaSystem/{__version__}"

    def __init__(self, *args: Any, snapshot_path: Path, **kwargs: Any) -> None:
        self.snapshot_path = snapshot_path
        self.static_root = files("codex_quota_dashboard").joinpath("static")
        super().__init__(*args, **kwargs)

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(head_only=True)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch(head_only=False)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/forecast-v2/m2/quote":
            self._json_error(HTTPStatus.NOT_FOUND, "接口不存在。")
            return
        try:
            address = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            address = None
        if address is None or not address.is_loopback:
            self._json_error(HTTPStatus.FORBIDDEN, "该计算接口仅允许本机访问。")
            return
        if self.headers.get_content_type() != "application/json":
            self._json_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "请求必须使用 application/json。")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            self._json_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求正文大小无效。")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求正文必须是对象")
            artifact = (self._snapshot().get("forecast_v2") or {}).get("m2") or {}
            result = quote_m2(artifact, payload.get("items"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            self._json_error(HTTPStatus.BAD_REQUEST, str(error) or "无法计算额度估算。")
            return
        self._send_json(HTTPStatus.OK, result)

    def _snapshot(self) -> dict[str, Any]:
        return load_snapshot(self.snapshot_path)

    def _dispatch(self, *, head_only: bool) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            try:
                snapshot = self._snapshot()
                payload = {
                    "status": "ok",
                    "forecast_system": "m1-m2-m3",
                    "system_state": snapshot.get("system_state"),
                    "snapshot_id": snapshot.get("snapshot_id"),
                    "generated_at": snapshot.get("generated_at"),
                }
                self._send_json(HTTPStatus.OK, payload, head_only=head_only)
            except (OSError, ValueError, json.JSONDecodeError):
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"status": "waiting_for_frozen_snapshot", "forecast_system": "m1-m2-m3"},
                    head_only=head_only,
                )
            return
        if parsed.path == "/api/dashboard":
            try:
                snapshot = self._snapshot()
            except (OSError, ValueError, json.JSONDecodeError):
                self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "尚无可读取的冻结预测快照。", head_only=head_only)
                return
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "mode": "local_system", "privacy": "local_state_not_exposed", "snapshot": snapshot},
                head_only=head_only,
            )
            return
        if parsed.path == "/api/quota/history":
            try:
                snapshot = self._snapshot()
                result = history_projection(snapshot, parse_qs(parsed.query, keep_blank_values=False))
            except (OSError, ValueError, json.JSONDecodeError):
                self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, "历史视图暂不可用。", head_only=head_only)
                return
            self._send_json(HTTPStatus.OK, result, head_only=head_only)
            return
        if parsed.path == "/api/quota/runtime":
            at = (parse_qs(parsed.query).get("at") or [None])[-1]
            self._send_json(
                HTTPStatus.OK,
                {"status": "aggregate_only", "time": at, "count": 0, "covered": False, "threads": []},
                head_only=head_only,
            )
            return
        self._serve_static(parsed.path, head_only=head_only)

    def _serve_static(self, route: str, *, head_only: bool) -> None:
        relative = "index.html" if route in {"", "/"} else route.lstrip("/")
        if relative not in STATIC_FILES:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        try:
            body = self.static_root.joinpath(relative).read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        mime = mimetypes.guess_type(relative)[0] or "application/octet-stream"
        if relative.endswith(".js"):
            mime = "text/javascript"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith(("text/", "application/javascript")) else mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400" if relative.startswith("vendor/") else "no-cache")
        self._security_headers()
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any], *, head_only: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _json_error(self, status: HTTPStatus, message: str, *, head_only: bool = False) -> None:
        self._send_json(status, {"status": "error", "message": message}, head_only=head_only)

    def _security_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)

    def log_message(self, format: str, *args: Any) -> None:
        route = urlparse(self.path).path
        sys.stderr.write(f"{self.client_address[0]} {self.command} {route} {args[1] if len(args) > 1 else ''}\n")


def history_projection(snapshot: dict[str, Any], query: dict[str, list[str]]) -> dict[str, Any]:
    actual = list((snapshot.get("adaptive") or {}).get("actual") or [])
    boundaries = list((snapshot.get("adaptive") or {}).get("boundaries") or [])
    start = parse_timestamp((query.get("start") or [None])[-1])
    end = parse_timestamp((query.get("end") or [None])[-1])
    if start:
        actual = [item for item in actual if (stamp := parse_timestamp(item.get("time"))) and stamp >= start]
        boundaries = [item for item in boundaries if (stamp := parse_timestamp(item.get("time"))) and stamp >= start]
    if end:
        actual = [item for item in actual if (stamp := parse_timestamp(item.get("time"))) and stamp <= end]
        boundaries = [item for item in boundaries if (stamp := parse_timestamp(item.get("time"))) and stamp <= end]
    return {
        "status": "ok" if actual else "empty",
        "start": start.isoformat().replace("+00:00", "Z") if start else (actual[0].get("time") if actual else None),
        "end": end.isoformat().replace("+00:00", "Z") if end else (actual[-1].get("time") if actual else None),
        "as_of": snapshot.get("generated_at"),
        "points": actual,
        "boundaries": boundaries,
        "raw_samples": len(actual),
        "reduction": None,
        "composition": {"status": "unavailable", "points": []},
    }


def create_server(host: str, port: int, snapshot_path: Path) -> QuotaHTTPServer:
    handler = partial(QuotaHandler, snapshot_path=snapshot_path)
    return QuotaHTTPServer((host, port), handler)


def serve(host: str, port: int, snapshot_path: Path) -> None:
    server = create_server(host, port, snapshot_path)
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    print(f"Codex quota system: http://{display_host}:{server.server_port}/#overview", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
