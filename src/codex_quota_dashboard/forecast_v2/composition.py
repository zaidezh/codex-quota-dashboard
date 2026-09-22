"""One-scenario composition: occupancy, request activity and token channels."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
import math
from typing import Any, Iterable, Mapping

from ..models import iso_utc, parse_timestamp
from .contracts import CHANNELS


def _time(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else parse_timestamp(value)


def _group_key(item: Mapping[str, Any]) -> str:
    return f"{item.get('model', 'unknown')}|{item.get('effort') or 'unknown'}"


def _valid_activity(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    value = float(value)
    return math.isfinite(value) and 0 <= value <= 1


def _unknown_rate_fields(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return list(CHANNELS)
    unknown: list[str] = []
    for channel in CHANNELS:
        if channel not in value:
            unknown.append(channel)
            continue
        item = value[channel]
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) or float(item) < 0:
            unknown.append(channel)
    return unknown


def _choose_one_per_thread(active: Iterable[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], int]:
    by_thread: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in active:
        by_thread[str(item.get("thread_id"))].append(item)
    selected: list[Mapping[str, Any]] = []
    overlaps = 0
    for items in by_thread.values():
        ordered = sorted(items, key=lambda x: (str(x.get("start_at")), str(x.get("slot_id"))))
        selected.append(ordered[-1])
        overlaps += max(0, len(items) - 1)
    return selected, overlaps


def compose_path(
    path: Mapping[str, Any],
    *,
    start_at: str | datetime | None = None,
    end_at: str | datetime | None = None,
    bucket_seconds: int = 60,
) -> dict[str, Any]:
    if bucket_seconds <= 0:
        raise ValueError("bucket_seconds must be positive")
    intervals = list(path.get("intervals", []))
    if not intervals:
        start = _time(start_at or path.get("origin_at"))
        end = _time(end_at or path.get("horizon_end"))
        if start is None or end is None or end <= start:
            return {"status": "insufficient_evidence", "buckets": [], "unknowns": ["missing_horizon"]}
    else:
        start = _time(start_at or min(x.get("start_at") for x in intervals))
        end = _time(end_at or path.get("horizon_end") or max(x.get("end_at") for x in intervals))
    if start is None or end is None or end <= start:
        raise ValueError("invalid composition horizon")
    normalized: list[dict[str, Any]] = []
    unknowns = list(path.get("coverage_unknowns", []))
    for raw in intervals:
        item = dict(raw)
        item["_start"] = _time(item.get("start_at"))
        item["_end"] = _time(item.get("end_at"))
        if item["_start"] is None or item["_end"] is None or item["_end"] <= item["_start"]:
            unknowns.append({"thread_id": item.get("thread_id"), "reason": "invalid_interval"})
            continue
        rate_fields = _unknown_rate_fields(item.get("tokens_per_minute"))
        item["_token_rate_unknown"] = bool(rate_fields)
        if rate_fields:
            unknowns.append({"thread_id": item.get("thread_id"), "reason": "token_profile_unknown", "fields": rate_fields})
        item["_activity_unknown"] = False
        if item.get("rate_basis") == "per_active_thread_minute":
            if not _valid_activity(item.get("activity_fraction")):
                item["_activity_unknown"] = True
                unknowns.append({"thread_id": item.get("thread_id"), "reason": "activity_fraction_unknown"})
        elif item.get("rate_basis") not in ("per_running_thread_minute",):
            unknowns.append({"thread_id": item.get("thread_id"), "reason": "unknown_rate_basis"})
        normalized.append(item)
    buckets: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        bucket_end = min(end, cursor + timedelta(seconds=bucket_seconds))
        boundaries = {cursor, bucket_end}
        for item in normalized:
            if item["_end"] <= cursor or item["_start"] >= bucket_end:
                continue
            boundaries.add(max(cursor, item["_start"]))
            boundaries.add(min(bucket_end, item["_end"]))
        ordered = sorted(boundaries)
        groups: dict[str, dict[str, Any]] = {}
        total_running_seconds = 0.0
        total_request_active_seconds = 0.0
        request_active_known = True
        overlap_count = 0
        for left, right in zip(ordered, ordered[1:]):
            if right <= left:
                continue
            active = [item for item in normalized if item["_start"] < right and item["_end"] > left]
            selected, overlaps = _choose_one_per_thread(active)
            overlap_count += overlaps
            seconds = (right - left).total_seconds()
            total_running_seconds += seconds * len(selected)
            for item in selected:
                activity = item.get("activity_fraction")
                if not _valid_activity(activity):
                    request_active_known = False
                else:
                    total_request_active_seconds += seconds * float(activity)
                key = _group_key(item)
                group = groups.setdefault(key, {
                    "model": item.get("model", "unknown"),
                    "effort": item.get("effort") or "unknown",
                    "thread_seconds": 0.0,
                    "request_active_thread_seconds": 0.0,
                    "tokens": {channel: 0.0 for channel in CHANNELS},
                    "rate_basis": item.get("rate_basis"),
                })
                group["thread_seconds"] += seconds
                if _valid_activity(activity):
                    group["request_active_thread_seconds"] += seconds * float(activity)
                rates = item.get("tokens_per_minute") if not item.get("_token_rate_unknown") else {}
                factor_minutes = seconds / 60.0
                if item.get("rate_basis") == "per_active_thread_minute":
                    factor_minutes = factor_minutes * float(activity) if not item.get("_activity_unknown") else 0.0
                elif item.get("rate_basis") != "per_running_thread_minute":
                    factor_minutes = 0.0
                for channel in CHANNELS:
                    group["tokens"][channel] += float(rates.get(channel, 0.0)) * factor_minutes
        width = (bucket_end - cursor).total_seconds()
        by_model: dict[str, dict[str, float]] = defaultdict(lambda: {channel: 0.0 for channel in CHANNELS})
        for group in groups.values():
            model = str(group["model"])
            for channel in CHANNELS:
                by_model[model][channel] += group["tokens"][channel]
        buckets.append({
            "time": iso_utc(cursor),
            "end_time": iso_utc(bucket_end),
            "width_seconds": width,
            "running_thread_seconds": total_running_seconds,
            "running_threads": total_running_seconds / width,
            "request_active_thread_seconds": total_request_active_seconds if request_active_known else None,
            "request_active_threads": total_request_active_seconds / width if request_active_known else None,
            "groups": groups,
            "token_totals_by_model": dict(by_model),
            "overlap_deduplicated": overlap_count,
        })
        cursor = bucket_end
    return {
        "schema_version": 1,
        "scenario_id": path.get("scenario_id"),
        "scenario_set_id": path.get("scenario_set_id"),
        "status": "truncated" if path.get("truncation_status") not in (None, "none") else "projected",
        "range_kind": "scenario_empirical_not_probability",
        "probability_calibrated": False,
        "interval_start": iso_utc(start),
        "interval_end": iso_utc(end),
        "buckets": buckets,
        "unknowns": unknowns,
        "coverage": "partial" if unknowns else "complete_for_declared_intervals",
        "request_active_definition": "activity_fraction_proxy_not_gpu_concurrency",
    }

def aggregate_compositions(compositions: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values = [dict(x) for x in compositions]
    if not values:
        return {"status": "insufficient_evidence", "buckets": []}
    count = len(values)
    buckets: list[dict[str, Any]] = []
    for index, first in enumerate(values[0].get("buckets", [])):
        aligned = [item["buckets"][index] for item in values if index < len(item.get("buckets", [])) and item["buckets"][index].get("time") == first.get("time")]
        if not aligned:
            continue
        group_keys = sorted({key for item in aligned for key in item.get("groups", {})})
        groups: dict[str, Any] = {}
        for key in group_keys:
            source = [item["groups"][key] for item in aligned if key in item.get("groups", {})]
            groups[key] = {
                "model": source[0].get("model"),
                "effort": source[0].get("effort"),
                "thread_seconds": sum(float(x.get("thread_seconds", 0)) for x in source) / len(source),
                "request_active_thread_seconds": (
                    sum(float(x.get("request_active_thread_seconds", 0)) for x in source) / len(source)
                    if all(x.get("request_active_thread_seconds") is not None for x in source) else None
                ),
                "tokens": {channel: sum(float(x.get("tokens", {}).get(channel, 0)) for x in source) / len(source) for channel in CHANNELS},
                "rate_basis": source[0].get("rate_basis"),
            }
        buckets.append({
            "time": first["time"],
            "end_time": first["end_time"],
            "width_seconds": first["width_seconds"],
            "running_thread_seconds": sum(float(x["running_thread_seconds"]) for x in aligned) / len(aligned),
            "running_threads": sum(float(x["running_threads"]) for x in aligned) / len(aligned),
            "request_active_thread_seconds": (
                sum(float(x["request_active_thread_seconds"]) for x in aligned) / len(aligned)
                if all(x.get("request_active_thread_seconds") is not None for x in aligned) else None
            ),
            "request_active_threads": (
                sum(float(x["request_active_threads"]) for x in aligned) / len(aligned)
                if all(x.get("request_active_threads") is not None for x in aligned) else None
            ),
            "groups": groups,
            "token_totals_by_model": {
                model: {channel: sum(float(item.get("token_totals_by_model", {}).get(model, {}).get(channel, 0)) for item in aligned) / len(aligned) for channel in CHANNELS}
                for model in sorted({model for item in aligned for model in item.get("token_totals_by_model", {})})
            },
            "overlap_deduplicated": sum(int(x.get("overlap_deduplicated", 0)) for x in aligned),
        })
    unknowns = [unknown for item in values for unknown in item.get("unknowns", [])]
    return {
        "schema_version": 1,
        "status": "partial" if unknowns else "projected",
        "scenario_count": count,
        "buckets": buckets,
        "unknowns": unknowns,
        "coverage": "partial" if unknowns else "complete_for_declared_intervals",
    }
