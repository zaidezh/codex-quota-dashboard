"""Read-only data sources and privacy projection for the dashboard."""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from .demo import build_demo_snapshot


MAX_POINTER_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_API_BYTES = 16 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class SourceError(RuntimeError):
    """A source failed without exposing low-level or private details."""


def validate_upstream_url(value: str, *, allow_remote: bool = False) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("上游必须是完整的 http:// 或 https:// 地址。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("上游地址不能包含凭据、查询参数或片段。")
    if not allow_remote and parsed.hostname.lower() not in LOOPBACK_HOSTS:
        raise ValueError("默认只允许 loopback 上游；远程上游需显式使用 --allow-remote-upstream。")
    return value.rstrip("/") + "/"


def _read_limited(response: Any, limit: int) -> bytes:
    body = response.read(limit + 1)
    if len(body) > limit:
        raise SourceError("上游响应超过安全大小限制。")
    return body


def _request_bytes(url: str, *, method: str = "GET", body: bytes | None = None, limit: int) -> bytes:
    headers = {"Accept": "application/json", "User-Agent": "codex-quota-dashboard/0.1"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            return _read_limited(response, limit)
    except HTTPError as exc:
        raise SourceError(f"上游返回 HTTP {exc.code}。请确认本机监控台仍在运行。") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SourceError("无法连接上游。请确认本机监控台地址和运行状态。") from exc


def _request_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None, limit: int = MAX_API_BYTES) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    raw = _request_bytes(url, method=method, body=body, limit=limit)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError("上游返回的不是有效 UTF-8 JSON。") from exc
    if not isinstance(value, dict):
        raise SourceError("上游 JSON 顶层必须是对象。")
    return value


def _select(source: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {name: copy.deepcopy(source[name]) for name in names if name in source}


def project_snapshot(snapshot: dict[str, Any], *, show_local_titles: bool = False) -> dict[str, Any]:
    """Return only fields the public UI renders and redact local identities by default."""

    adaptive = snapshot.get("adaptive") if isinstance(snapshot.get("adaptive"), dict) else {}
    task = snapshot.get("task_forecast") if isinstance(snapshot.get("task_forecast"), dict) else {}
    consumption = snapshot.get("consumption_explanation") if isinstance(snapshot.get("consumption_explanation"), dict) else {}
    review = snapshot.get("forecast_review") if isinstance(snapshot.get("forecast_review"), dict) else {}
    calibration = snapshot.get("token_calibration") if isinstance(snapshot.get("token_calibration"), dict) else {}

    result: dict[str, Any] = _select(snapshot, ("schema_version", "generated_at", "timezone", "authority"))
    result["adaptive"] = _select(
        adaptive,
        (
            "status",
            "version",
            "latest",
            "actual",
            "boundaries",
            "forecast",
            "valid_history_hours",
            "matched_model_hours",
            "recent_rate_pp_hour",
            "pace_budget",
            "scenario",
            "activity",
            "model_rows",
        ),
    )

    task_view = _select(
        task,
        (
            "status",
            "version",
            "issued_at",
            "input_cutoff",
            "reset_at",
            "current_rate_pp_minute",
            "semantic_weight",
            "points",
            "minute_history",
            "validation",
            "warning",
            "observer",
            "semantic_tasks",
            "as_of",
        ),
    )
    distribution = task.get("thread_distribution") if isinstance(task.get("thread_distribution"), dict) else {}
    task_view["thread_distribution"] = _select(
        distribution,
        (
            "version",
            "as_of",
            "unit",
            "scope",
            "points",
            "range_kind",
            "coverage",
            "assumptions",
            "status",
            "interval_start",
            "interval_end",
        ),
    )
    task_fields = (
        "title",
        "nickname",
        "model",
        "effort",
        "tier",
        "status",
        "progress",
        "rate_pp_minute",
        "duration_basis",
        "goal_runtime_status",
        "remaining_minutes",
        "requests_15m",
        "activity_fraction",
        "continuation_deferred",
        "is_observer",
    )
    projected_tasks = []
    for index, item in enumerate(task.get("tasks") or [], start=1):
        if not isinstance(item, dict):
            continue
        projected = _select(item, task_fields)
        if not show_local_titles:
            projected["title"] = f"本机任务 {index}"
            projected.pop("nickname", None)
        projected_tasks.append(projected)
    task_view["tasks"] = projected_tasks

    continuing = task.get("continuing_work") if isinstance(task.get("continuing_work"), dict) else {}
    continuing_view = _select(continuing, ("source", "assumption", "goal_duration_unknown"))
    profiles = []
    for index, item in enumerate(continuing.get("profiles") or [], start=1):
        if not isinstance(item, dict):
            continue
        projected = _select(item, ("title", "status", "remaining_minutes", "request_count", "reason"))
        if not show_local_titles:
            projected["title"] = f"持续目标 {index}"
        profiles.append(projected)
    continuing_view["profiles"] = profiles
    task_view["continuing_work"] = continuing_view

    scheduled = task.get("scheduled") if isinstance(task.get("scheduled"), dict) else {}
    scheduled_view = _select(scheduled, ("issues", "horizon_end", "overlap_policy"))
    jobs = []
    for index, item in enumerate(scheduled.get("jobs") or [], start=1):
        if not isinstance(item, dict):
            continue
        projected = _select(
            item,
            ("name", "status", "basis", "history_runs", "forecast_runs", "next_runs", "cost_per_run", "duration_minutes"),
        )
        if not show_local_titles:
            projected["name"] = f"定时任务 {index}"
        jobs.append(projected)
    scheduled_view["jobs"] = jobs
    task_view["scheduled"] = scheduled_view
    result["task_forecast"] = task_view

    consumption_view = _select(
        consumption,
        ("status", "version", "computed_at", "purpose", "warning", "validation", "drift", "reset_comparison"),
    )
    consumption_view["rows"] = copy.deepcopy((consumption.get("rows") or [])[-50:])
    result["consumption_explanation"] = consumption_view

    review_view = _select(review, ("version", "automatic_application", "issues", "evaluations", "reason_counts"))
    review_view["candidates"] = copy.deepcopy((review.get("candidates") or [])[-12:])
    review_view["recent"] = copy.deepcopy((review.get("recent") or [])[-20:])
    result["forecast_review"] = review_view

    result["token_calibration"] = _select(
        calibration,
        (
            "schema_version",
            "version",
            "status",
            "calibration_id",
            "generated_at",
            "training_blocks",
            "feature_count",
            "matrix_rank",
            "training_start",
            "training_end",
            "unit",
            "official_tariff",
            "probability_calibrated",
            "models",
            "scope",
            "validation",
            "alignment_sensitivity",
            "notes",
        ),
    )
    return result


@dataclass(slots=True)
class DemoSource:
    """Synthetic source used by default."""

    show_local_titles: bool = False

    @property
    def mode(self) -> str:
        return "demo"

    def snapshot(self) -> dict[str, Any]:
        return project_snapshot(build_demo_snapshot(), show_local_titles=self.show_local_titles)

    def history(self, query: dict[str, list[str]]) -> dict[str, Any]:
        snapshot = self.snapshot()
        points = snapshot.get("adaptive", {}).get("actual", [])
        default_start = points[0]["time"] if points else snapshot["generated_at"]
        default_end = snapshot.get("task_forecast", {}).get("reset_at") or snapshot["generated_at"]
        start = (query.get("start") or [default_start])[-1]
        end = (query.get("end") or [default_end])[-1]
        composition_points = []
        for index, point in enumerate(points):
            point_end = points[index + 1]["time"] if index + 1 < len(points) else snapshot["generated_at"]
            concurrency = 1.0 + (1.0 if index % 9 in {2, 3, 4} else 0.0)
            sol = concurrency * (0.65 if index % 4 else 0.35)
            luna = concurrency - sol
            composition_points.append(
                {
                    "time": point["time"],
                    "end": point_end,
                    "seconds": 1800,
                    "observed_seconds": 1800,
                    "uncertain_seconds": 0,
                    "mean_concurrency": concurrency,
                    "peak_concurrency": int(concurrency),
                    "groups": {
                        "gpt-5.6-sol|high": {"seconds": sol * 1800, "mean_concurrency": sol, "share": sol / concurrency},
                        "gpt-5.6-luna|medium": {"seconds": luna * 1800, "mean_concurrency": luna, "share": luna / concurrency},
                    },
                }
            )
        return {
            "status": "ok",
            "source": "synthetic_demo",
            "start": start,
            "end": end,
            "as_of": snapshot["generated_at"],
            "points": points,
            "boundaries": [],
            "raw_samples": len(points),
            "reduction": None,
            "composition": {"status": "ok", "points": composition_points},
            "first_date": points[0]["time"][:10] if points else None,
            "last_date": points[-1]["time"][:10] if points else None,
        }

    def runtime(self, query: dict[str, list[str]]) -> dict[str, Any]:
        snapshot = self.snapshot()
        at = (query.get("at") or [snapshot["generated_at"]])[-1]
        threads = [
            {
                "host": "演示主机",
                "thread": "demo-thread-1",
                "title": "合成任务 1",
                "model": "gpt-5.6-sol",
                "effort": "high",
                "tier": "standard",
                "quality": "observed",
                "start": snapshot["adaptive"]["actual"][-4]["time"],
                "end": snapshot["generated_at"],
            }
        ]
        return {"status": "ok", "time": at, "count": len(threads), "covered": True, "threads": threads}

    def quote(self, payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = self.snapshot()
        calibration = snapshot.get("token_calibration", {})
        models = {item.get("model"): item for item in calibration.get("models", []) if isinstance(item, dict)}
        estimate = lower = upper = 0.0
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            raise SourceError("预算请求需要至少一个项目。")
        for item in items:
            if not isinstance(item, dict) or item.get("model") not in models:
                raise SourceError("预算请求中的模型不在当前校准表中。")
            calls = _positive_number(item.get("calls"), "调用次数")
            coefficients = models[item["model"]].get("coefficients", {})
            for channel in ("uncached_input", "cached_input", "output"):
                tokens = _nonnegative_number(item.get(channel), channel)
                coefficient = coefficients.get(channel) if isinstance(coefficients.get(channel), dict) else {}
                scale = calls * tokens / 1_000_000
                estimate += scale * float(coefficient.get("estimate", 0))
                lower += scale * float(coefficient.get("lower", 0))
                upper += scale * float(coefficient.get("upper", 0))
        return {
            "status": "ok",
            "source": "synthetic_demo",
            "calibration_id": calibration.get("calibration_id"),
            "estimate_pp": round(estimate, 4),
            "lower_pp": round(lower, 4),
            "upper_pp": round(upper, 4),
            "unit": "account_window_percentage_points",
        }


def _positive_number(value: Any, label: str) -> float:
    number = _nonnegative_number(value, label)
    if number <= 0:
        raise SourceError(f"{label}必须大于 0。")
    return number


def _nonnegative_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise SourceError(f"{label}必须是数字。")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SourceError(f"{label}必须是数字。") from exc
    if number < 0 or number != number or number == float("inf"):
        raise SourceError(f"{label}必须是有限的非负数。")
    return number


def project_runtime(result: dict[str, Any], *, show_local_titles: bool = False) -> dict[str, Any]:
    """Project runtime rows without exposing local identities by default."""

    projected = _select(result, ("status", "global_exact", "time", "count", "unknown_tails", "covered", "index_fresh"))
    threads = []
    for index, item in enumerate(result.get("threads") or [], start=1):
        if not isinstance(item, dict):
            continue
        thread = _select(item, ("thread", "title", "host", "model", "effort", "tier", "quality", "start", "end"))
        if not show_local_titles:
            thread["thread"] = f"local-task-{index}"
            thread["title"] = f"本机任务 {index}"
            thread["host"] = "本机"
        threads.append(thread)
    projected["threads"] = threads
    projected["count"] = len(threads)
    return projected


@dataclass(slots=True)
class LiveSource:
    """Read and verify a compatible local Codex quota monitor."""

    base_url: str
    show_local_titles: bool = False
    allow_remote: bool = False

    def __post_init__(self) -> None:
        self.base_url = validate_upstream_url(self.base_url, allow_remote=self.allow_remote)

    @property
    def mode(self) -> str:
        return "live"

    def snapshot(self) -> dict[str, Any]:
        pointer = _request_json(urljoin(self.base_url, "control/latest.json"), limit=MAX_POINTER_BYTES)
        path = pointer.get("path")
        if not isinstance(path, str) or not path:
            raise SourceError("上游快照指针缺少 path。")
        parsed_path = PurePosixPath(path)
        if parsed_path.is_absolute() or ".." in parsed_path.parts:
            raise SourceError("上游快照指针包含不安全路径。")
        snapshot_url = urljoin(self.base_url, path)
        if _origin(snapshot_url) != _origin(self.base_url):
            raise SourceError("上游快照指针试图跳转到其他来源。")
        compressed = _request_bytes(snapshot_url, limit=MAX_SNAPSHOT_BYTES)
        expected = pointer.get("sha256")
        if not isinstance(expected, str) or hashlib.sha256(compressed).hexdigest() != expected.lower():
            raise SourceError("上游快照 SHA-256 校验失败。")
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as stream:
                raw = stream.read(MAX_UNCOMPRESSED_BYTES + 1)
        except (gzip.BadGzipFile, OSError) as exc:
            raise SourceError("上游快照不是有效 gzip。") from exc
        if len(raw) > MAX_UNCOMPRESSED_BYTES:
            raise SourceError("上游解压后快照超过安全大小限制。")
        try:
            snapshot = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceError("上游快照不是有效 UTF-8 JSON。") from exc
        if not isinstance(snapshot, dict):
            raise SourceError("上游快照顶层必须是对象。")
        return project_snapshot(snapshot, show_local_titles=self.show_local_titles)

    def history(self, query: dict[str, list[str]]) -> dict[str, Any]:
        allowed = {
            key: values[-1]
            for key, values in query.items()
            if key in {"start", "end", "date", "days", "as_of", "max_points"} and values
        }
        suffix = "api/quota/history"
        if allowed:
            suffix += "?" + urlencode(allowed)
        return _request_json(urljoin(self.base_url, suffix))

    def runtime(self, query: dict[str, list[str]]) -> dict[str, Any]:
        allowed = {key: values[-1] for key, values in query.items() if key in {"at", "as_of"} and values}
        suffix = "api/quota/runtime"
        if allowed:
            suffix += "?" + urlencode(allowed)
        return project_runtime(_request_json(urljoin(self.base_url, suffix)), show_local_titles=self.show_local_titles)

    def quote(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _request_json(
            urljoin(self.base_url, "api/calibration/quote"),
            method="POST",
            payload=payload,
            limit=MAX_API_BYTES,
        )


def _origin(value: str) -> tuple[str, str, int | None]:
    parsed = urlparse(value)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port
