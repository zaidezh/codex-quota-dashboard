"""Loopback-only HTTP server for the independent quota dashboard."""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlparse

from .source import DemoSource, LiveSource, SourceError


API_BODY_LIMIT = 64 * 1024
LOOPBACK_BINDS = {"127.0.0.1", "localhost", "::1"}
STATIC_FILES = {
    "index.html",
    "styles.css",
    "app.js",
    "vendor/echarts.min.js",
}
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


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "CodexQuotaDashboard/0.1"

    def __init__(self, *args: Any, source: DemoSource | LiveSource, **kwargs: Any) -> None:
        self.source = source
        self.static_root = files("codex_quota_dashboard").joinpath("static")
        super().__init__(*args, **kwargs)

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(head_only=True)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch(head_only=False)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route != "/api/calibration/quote":
            self._json_error(HTTPStatus.NOT_FOUND, "未找到该接口。")
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._json_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "请求必须使用 application/json。")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json_error(HTTPStatus.BAD_REQUEST, "Content-Length 无效。")
            return
        if length <= 0 or length > API_BODY_LIMIT:
            self._json_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求体为空或超过 64 KiB。")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json_error(HTTPStatus.BAD_REQUEST, "请求体不是有效 UTF-8 JSON。")
            return
        if not isinstance(payload, dict):
            self._json_error(HTTPStatus.BAD_REQUEST, "请求体顶层必须是对象。")
            return
        try:
            result = self.source.quote(payload)
        except SourceError as exc:
            self._json_error(HTTPStatus.BAD_GATEWAY if self.source.mode == "live" else HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._send_json(HTTPStatus.OK, result)

    def _dispatch(self, *, head_only: bool) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        if route == "/healthz":
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "mode": self.source.mode, "scope": "loopback-first"},
                head_only=head_only,
            )
            return
        if route == "/api/dashboard":
            try:
                snapshot = self.source.snapshot()
            except SourceError as exc:
                self._json_error(
                    HTTPStatus.BAD_GATEWAY,
                    str(exc),
                    recovery="确认上游健康后再刷新；实时模式不会自动降级成演示数据。",
                    head_only=head_only,
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "mode": self.source.mode,
                    "privacy": "local_titles_visible" if self.source.show_local_titles else "local_titles_redacted",
                    "snapshot": snapshot,
                },
                head_only=head_only,
            )
            return
        if route == "/api/quota/history":
            try:
                result = self.source.history(parse_qs(parsed.query, keep_blank_values=False))
            except SourceError as exc:
                self._json_error(HTTPStatus.BAD_GATEWAY, str(exc), head_only=head_only)
                return
            self._send_json(HTTPStatus.OK, result, head_only=head_only)
            return
        if route == "/api/quota/runtime":
            try:
                result = self.source.runtime(parse_qs(parsed.query, keep_blank_values=False))
            except SourceError as exc:
                self._json_error(HTTPStatus.BAD_GATEWAY, str(exc), head_only=head_only)
                return
            self._send_json(HTTPStatus.OK, result, head_only=head_only)
            return
        self._serve_static(route, head_only=head_only)

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

    def _json_error(
        self,
        status: HTTPStatus,
        message: str,
        *,
        recovery: str = "检查输入或服务状态后重试。",
        head_only: bool = False,
    ) -> None:
        self._send_json(
            status,
            {"status": "error", "message": message, "recovery": recovery},
            head_only=head_only,
        )

    def _security_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)

    def log_message(self, format: str, *args: Any) -> None:
        # Do not log query strings, request bodies, task titles, or upstream paths.
        route = urlparse(self.path).path
        sys.stderr.write(f"{self.client_address[0]} {self.command} {route} {args[1] if len(args) > 1 else ''}\n")


def create_server(
    host: str,
    port: int,
    source: DemoSource | LiveSource,
) -> DashboardHTTPServer:
    handler = partial(DashboardHandler, source=source)
    return DashboardHTTPServer((host, port), handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the Codex quota dashboard locally.")
    parser.add_argument("--mode", choices=("demo", "live"), default="demo", help="demo is synthetic; live reads a compatible upstream")
    parser.add_argument("--upstream", default="http://127.0.0.1:18765", help="compatible quota monitor root URL")
    parser.add_argument("--host", default="127.0.0.1", help="listen address (loopback by default)")
    parser.add_argument("--port", type=int, default=18766, help="listen port")
    parser.add_argument("--show-local-titles", action="store_true", help="show task and schedule titles from a live upstream")
    parser.add_argument("--allow-remote-upstream", action="store_true", help="allow a non-loopback upstream URL")
    parser.add_argument("--allow-network-bind", action="store_true", help="allow listening beyond loopback")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.port < 0 or args.port > 65535:
        raise SystemExit("--port 必须在 0–65535 之间；0 表示由系统分配临时端口。")
    if args.host.lower() not in LOOPBACK_BINDS and not args.allow_network_bind:
        raise SystemExit("默认只监听 loopback；如确需局域网暴露，请显式使用 --allow-network-bind。")
    try:
        source: DemoSource | LiveSource
        if args.mode == "live":
            source = LiveSource(
                args.upstream,
                show_local_titles=args.show_local_titles,
                allow_remote=args.allow_remote_upstream,
            )
        else:
            source = DemoSource(show_local_titles=args.show_local_titles)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    server = create_server(args.host, args.port, source)
    display_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    print(f"Codex quota dashboard: http://{display_host}:{server.server_port}/ ({source.mode})", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
