from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from typing import Any

from .models import RateLimitSnapshot, parse_timestamp, utc_now


class AppServerError(RuntimeError):
    pass


def _resolve_command(command: str) -> list[str]:
    candidate = shutil.which(command) or command
    if os.name == "nt" and str(candidate).lower().endswith(".ps1"):
        return ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(candidate)]
    return [str(candidate)]


class AppServerClient:
    def __init__(self, command: str = "codex", codex_home: Path | None = None):
        self.command = command
        self.codex_home = codex_home
        self.process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._next_id = 1
        self._reader: threading.Thread | None = None

    def start(self, timeout: float = 20.0) -> None:
        if self.process and self.process.poll() is None:
            return
        env = os.environ.copy()
        if self.codex_home:
            env["CODEX_HOME"] = str(self.codex_home)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [*_resolve_command(self.command), "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=flags,
        )
        self._reader = threading.Thread(target=self._read_messages, daemon=True)
        self._reader.start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_quota_system",
                    "title": "Codex Quota System",
                    "version": "0.2.0",
                },
                "capabilities": {
                    "optOutNotificationMethods": [
                        "thread/started",
                        "turn/started",
                        "item/started",
                        "item/completed",
                    ]
                },
            },
            timeout=timeout,
        )
        self.notify("initialized", {})

    def _read_messages(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                self._messages.put(value)

    def _write(self, value: dict[str, Any]) -> None:
        if not self.process or self.process.poll() is not None or not self.process.stdin:
            raise AppServerError("Codex App Server is not running")
        try:
            self.process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except OSError as error:
            raise AppServerError(f"failed to write to Codex App Server: {error}") from error

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"method": method, "params": params})

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 20.0) -> Any:
        request_id = self._next_id
        self._next_id += 1
        self._write({"method": method, "id": request_id, "params": params or {}})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError(f"timeout waiting for {method}")
            try:
                message = self._messages.get(timeout=remaining)
            except queue.Empty as error:
                raise AppServerError(f"timeout waiting for {method}") from error
            # This client issues one request at a time. Notifications have no id
            # and can be discarded; a different id is a stale response.
            if message.get("id") != request_id:
                continue
            if message.get("error") is not None:
                raise AppServerError(f"{method}: {message['error']}")
            return message.get("result")

    def read_rate_limits(self, collector_id: str) -> list[RateLimitSnapshot]:
        result = self.request("account/rateLimits/read") or {}
        observed_at = utc_now()
        reset_credits = result.get("rateLimitResetCredits")
        buckets = result.get("rateLimitsByLimitId") or {}
        if not buckets and result.get("rateLimits"):
            one = result["rateLimits"]
            buckets = {str(one.get("limitId") or "codex"): one}
        snapshots: list[RateLimitSnapshot] = []
        for map_id, bucket in buckets.items():
            limit_id = str(bucket.get("limitId") or map_id)
            for slot in ("primary", "secondary"):
                window = bucket.get(slot)
                if not window:
                    continue
                snapshots.append(
                    RateLimitSnapshot(
                        observed_at=observed_at,
                        collector_id=collector_id,
                        limit_id=f"{limit_id}:{slot}",
                        limit_name=bucket.get("limitName") or slot,
                        used_percent=(
                            float(window["usedPercent"])
                            if window.get("usedPercent") is not None
                            else None
                        ),
                        window_minutes=(
                            int(window["windowDurationMins"])
                            if window.get("windowDurationMins") is not None
                            else None
                        ),
                        resets_at=parse_timestamp(window.get("resetsAt")),
                        plan_type=bucket.get("planType"),
                        reached_type=bucket.get("rateLimitReachedType"),
                        source="appserver_account_rate_limits",
                        authority="server_authoritative",
                        extra={
                            "normal_model_slug": bucket.get("normalModelSlug"),
                            "spend_control_reached": bucket.get("spendControlReached"),
                            "individual_limit": bucket.get("individualLimit"),
                            "credits": bucket.get("credits"),
                            # Reset credits are response-level metadata, not part of
                            # an individual rate-limit bucket.
                            "rate_limit_reset_credits": reset_credits,
                        },
                    )
                )
        return snapshots

    def close(self) -> None:
        process = self.process
        self.process = None
        if not process:
            return
        try:
            if process.stdin:
                process.stdin.close()
            process.terminate()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()

    def __enter__(self) -> "AppServerClient":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
