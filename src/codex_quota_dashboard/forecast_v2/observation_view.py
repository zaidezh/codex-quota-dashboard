"""Primary M1 observation view built from authoritative quota and token inputs."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone, tzinfo
import sqlite3
from typing import Any, Iterable, Mapping

from ..models import iso_utc, parse_timestamp


VERSION = "quota-forecast-v2-m1-observation-view-v1"
MAX_GAP_SECONDS = 300
RESET_TOLERANCE_SECONDS = 120


def normalize_quota_samples(
    rows: Iterable[Mapping[str, Any]], now: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    parsed: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        stamp = parse_timestamp(row.get("observed_at"))
        reset = parse_timestamp(row.get("resets_at"))
        value = row.get("used_percent")
        if stamp is None or reset is None or stamp > now or value is None or not row.get("window_minutes"):
            continue
        row.update(stamp=stamp, reset=reset, used_percent=float(value))
        parsed.append(row)
    parsed.sort(key=lambda item: item["stamp"])
    samples = list({item["stamp"]: item for item in parsed}.values())
    epochs: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for row in samples:
        reason = None
        if previous is None:
            reason = "first_observation"
        elif (row.get("plan_type"), row["window_minutes"]) != (
            previous.get("plan_type"), previous["window_minutes"]
        ):
            reason = "plan_or_window_change"
        elif abs((row["reset"] - previous["reset"]).total_seconds()) > RESET_TOLERANCE_SECONDS:
            reason = "reset_time_changed"
        elif row["used_percent"] < previous["used_percent"] - 0.5:
            reason = "quota_rebound"
        if reason:
            row["known_window_return"] = reason == "reset_time_changed" and any(
                (old.get("plan_type"), old["window_minutes"])
                == (row.get("plan_type"), row["window_minutes"])
                and abs((old["reset"] - row["reset"]).total_seconds())
                <= RESET_TOLERANCE_SECONDS
                for epoch in epochs
                for old in (epoch["rows"][0], epoch["rows"][-1])
            )
            epochs.append(
                {"id": len(epochs), "start": row["stamp"], "reason": reason, "rows": []}
            )
        row["epoch"] = epochs[-1]["id"]
        epochs[-1]["rows"].append(row)
        previous = row
    for epoch in epochs:
        stamps = sorted(item["reset"].timestamp() for item in epoch["rows"])
        epoch["reset"] = datetime.fromtimestamp(
            stamps[len(stamps) // 2], timezone.utc
        )
    return samples, epochs


def classify_quota_boundary(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    delta = float(after["used_percent"]) - float(before["used_percent"])
    reset_shift = (after["reset"] - before["reset"]).total_seconds()
    if (before.get("plan_type"), before["window_minutes"]) != (
        after.get("plan_type"), after["window_minutes"]
    ):
        kind = "scope_change"
    elif after.get("known_window_return"):
        kind = "quota_window_change"
    elif reset_shift > RESET_TOLERANCE_SECONDS and delta < -0.5 and float(after["used_percent"]) <= 1:
        kind = "quota_reset"
    elif abs(reset_shift) > RESET_TOLERANCE_SECONDS:
        kind = "quota_window_change"
    else:
        kind = "quota_recovery_unclassified"
    return {
        "kind": kind,
        "before_time": iso_utc(before["stamp"]),
        "time": iso_utc(after["stamp"]),
        "before_used_percent": float(before["used_percent"]),
        "after_used_percent": float(after["used_percent"]),
        "delta_pp": delta,
        "before_resets_at": iso_utc(before["reset"]),
        "after_resets_at": iso_utc(after["reset"]),
        "assigned_to_request_consumption": False,
    }


def display_history(epochs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not epochs:
        return [], []
    begin = 0
    boundaries: dict[int, dict[str, Any]] = {}
    for index in range(1, len(epochs)):
        event = classify_quota_boundary(epochs[index - 1]["rows"][-1], epochs[index]["rows"][0])
        boundaries[index] = event
        if event["kind"] in ("quota_reset", "scope_change"):
            begin = index
    actual: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for index in range(begin, len(epochs)):
        for row in epochs[index]["rows"]:
            if previous and (
                previous["epoch"] != row["epoch"]
                or (row["stamp"] - previous["stamp"]).total_seconds() > MAX_GAP_SECONDS
            ):
                actual.append(
                    {
                        "time": iso_utc(previous["stamp"] + timedelta(milliseconds=1)),
                        "used_percent": None,
                    }
                )
            actual.append({"time": iso_utc(row["stamp"]), "used_percent": row["used_percent"]})
            previous = row
    return actual, [event for index, event in boundaries.items() if index > begin]


def _request_rows(db: sqlite3.Connection, start: datetime, now: datetime) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.execute(
            """
            SELECT response_id, session_id, occurred_at, model, service_tier,
                   input_tokens, cached_input_tokens, output_tokens
            FROM native_requests
            WHERE conflict=0 AND occurred_at>=? AND occurred_at<=?
            ORDER BY occurred_at, response_id
            """,
            (iso_utc(start), iso_utc(now)),
        )
    ]


def _usage_views(
    rows: list[dict[str, Any]], now: datetime, zone: tzinfo
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    hourly: dict[tuple[str, str, str], dict[str, Any]] = {}
    daily: dict[str, dict[str, Any]] = {}
    model_rows: dict[str, dict[str, Any]] = {}
    recent_mix: Counter[str] = Counter()
    recent_input = recent_cached = 0
    recent_cutoff = now - timedelta(hours=6)
    day_cutoff = now - timedelta(hours=24)
    for row in rows:
        stamp = parse_timestamp(row.get("occurred_at"))
        if stamp is None:
            continue
        model = str(row.get("model") or "unknown")
        tier = str(row.get("service_tier") or "unknown")
        input_tokens = int(row.get("input_tokens") or 0)
        cached = int(row.get("cached_input_tokens") or 0)
        output = int(row.get("output_tokens") or 0)
        hour = stamp.replace(minute=0, second=0, microsecond=0)
        hkey = (iso_utc(hour), model, tier)
        hitem = hourly.setdefault(
            hkey,
            {
                "hour": hkey[0],
                "model": model,
                "service_tier": tier,
                "requests": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "active_tasks": set(),
                "active_task_mean": None,
                "native_requests": 0,
            },
        )
        hitem["requests"] += 1
        hitem["native_requests"] += 1
        hitem["input_tokens"] += input_tokens
        hitem["cached_input_tokens"] += cached
        hitem["output_tokens"] += output
        hitem["total_tokens"] += input_tokens + output
        hitem["active_tasks"].add(str(row.get("session_id") or ""))
        day = stamp.astimezone(zone).date().isoformat()
        ditem = daily.setdefault(
            day,
            {"day": day, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
        )
        ditem["input_tokens"] += input_tokens
        ditem["cached_input_tokens"] += cached
        ditem["output_tokens"] += output
        if stamp >= day_cutoff:
            mitem = model_rows.setdefault(
                model,
                {
                    "model": model,
                    "requests": 0,
                    "native_requests": 0,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "tiers": set(),
                },
            )
            mitem["requests"] += 1
            mitem["native_requests"] += 1
            mitem["input_tokens"] += input_tokens
            mitem["cached_input_tokens"] += cached
            mitem["output_tokens"] += output
            mitem["total_tokens"] += input_tokens + output
            mitem["tiers"].add(tier)
        if stamp >= recent_cutoff:
            recent_mix[f"{model}|{tier}"] += 1
            recent_input += input_tokens
            recent_cached += cached
    hourly_values: list[dict[str, Any]] = []
    for item in sorted(hourly.values(), key=lambda value: (value["hour"], value["model"], value["service_tier"])):
        item = dict(item)
        item["active_tasks"] = len(item["active_tasks"])
        hourly_values.append(item)
    models: list[dict[str, Any]] = []
    for item in sorted(model_rows.values(), key=lambda value: value["requests"], reverse=True):
        value = dict(item)
        value["tiers"] = sorted(value["tiers"])
        value["cache_ratio"] = value["cached_input_tokens"] / value["input_tokens"] if value["input_tokens"] else None
        models.append(value)
    day_summary = {
        key: sum(int(item.get(key) or 0) for item in models)
        for key in (
            "requests",
            "native_requests",
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
        )
    }
    day_summary["cache_rate"] = (
        day_summary["cached_input_tokens"] / day_summary["input_tokens"]
        if day_summary["input_tokens"]
        else None
    )
    mix_total = sum(recent_mix.values())
    target = {
        "mix": {key: value / mix_total for key, value in recent_mix.items()} if mix_total else {},
        "cache_ratio": recent_cached / recent_input if recent_input else None,
    }
    return hourly_values, models, day_summary, [daily[key] for key in sorted(daily)], target


def build_observation_view(
    db: sqlite3.Connection,
    now: datetime,
    zone: tzinfo,
    *,
    history_days: int = 35,
) -> dict[str, Any]:
    start = now - timedelta(days=max(1, int(history_days)))
    rows = [
        dict(row)
        for row in db.execute(
            """
            SELECT observed_at, used_percent, resets_at, window_minutes,
                   plan_type, source, authority
            FROM rate_limit_snapshots
            WHERE limit_id='codex:primary'
              AND source='appserver_account_rate_limits'
              AND authority='server_authoritative'
              AND observed_at>=? AND observed_at<=?
            ORDER BY observed_at
            """,
            (iso_utc(start), iso_utc(now)),
        )
    ]
    samples, epochs = normalize_quota_samples(rows, now)
    requests = _request_rows(db, start, now)
    hourly, models, day_summary, daily, target = _usage_views(requests, now, zone)
    result: dict[str, Any] = {
        "version": VERSION,
        "system_id": "forecast_v2",
        "source": "server_authoritative",
        "status": "insufficient_evidence",
        "status_label": "等待权威额度观测",
        "latest": None,
        "actual": [],
        "forecast": [],
        "boundaries": [],
        "windows": [],
        "hourly_usage": hourly,
        "model_rows": models,
        "day_summary": day_summary,
        "daily_usage": daily,
        "target": target,
        "activity": {
            "native_records": len(requests),
            "conflicting_responses": int(
                db.execute(
                    "SELECT COUNT(*) FROM native_requests WHERE conflict<>0 AND occurred_at>=? AND occurred_at<=?",
                    (iso_utc(start), iso_utc(now)),
                ).fetchone()[0]
            ),
        },
        "backtest": {},
        "notes": [
            "当前页面使用 M1 权威额度观测与原生请求记录。",
            "未来走势由同一 forecast_v2 快照中的 M3 生成。",
        ],
    }
    if not samples:
        return result
    current = epochs[-1]
    latest = samples[-1]
    actual, boundaries = display_history(epochs)
    result["actual"] = actual
    result["boundaries"] = boundaries
    result["latest"] = {
        **{key: latest.get(key) for key in ("observed_at", "used_percent", "window_minutes", "plan_type", "source", "authority")},
        "limit_id": "codex:primary",
        "resets_at": iso_utc(current["reset"]),
        "epoch_started_at": iso_utc(current["start"]),
        "remaining_percent": max(0.0, 100.0 - float(latest["used_percent"])),
        "history_started_at": actual[0]["time"] if actual else None,
    }
    for index, epoch in enumerate(epochs[-10:]):
        first, last = epoch["rows"][0], epoch["rows"][-1]
        absolute_index = len(epochs) - len(epochs[-10:]) + index
        previous = epochs[absolute_index - 1]["rows"][-1] if absolute_index else None
        result["windows"].append(
            {
                "start": iso_utc(first["stamp"]),
                "last_observation": iso_utc(last["stamp"]),
                "resets_at": iso_utc(epoch["reset"]),
                "last_used_percent": float(last["used_percent"]),
                "change_kind": classify_quota_boundary(previous, first)["kind"] if previous else "first_observation",
                "reason": epoch["reason"],
                "current": epoch is current,
                "partial_start": float(first["used_percent"]) > 1,
            }
        )
    age = (now - latest["stamp"]).total_seconds()
    result["latest_age_seconds"] = age
    if age > 180 or current["reset"] <= now:
        result.update(status="stale", status_label="服务器观测已过期")
    else:
        result.update(status="observed", status_label="M1 权威观测正常")
    return result
