"""Cumulative displayed-quota design matrices with explicit provenance."""
from __future__ import annotations

from collections import defaultdict
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
import math
from typing import Any, Iterable, Mapping, Sequence

from ..models import iso_utc, parse_timestamp, stable_id
from .contracts import ALIGNMENT_OFFSETS_SECONDS, CHANNELS
from .evidence import normalize_request


PRIMARY_SOURCE = "appserver_account_rate_limits"
PRIMARY_AUTHORITY = "server_authoritative"
PRIMARY_LIMIT = "codex:primary"


def _time(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else parse_timestamp(value)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _scope(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("account_id") or row.get("account") or row.get("collector_id"),
        row.get("limit_id"),
        row.get("window_minutes"),
        row.get("plan_type"),
        row.get("authority"),
    )


def _same_period(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if _scope(left) != _scope(right):
        return False
    left_cycle = left.get("cycle_id") or left.get("period_id")
    right_cycle = right.get("cycle_id") or right.get("period_id")
    if left_cycle is not None or right_cycle is not None:
        return left_cycle == right_cycle
    lreset = _time(left.get("resets_at"))
    rreset = _time(right.get("resets_at"))
    if lreset is None or rreset is None:
        return lreset is None and rreset is None
    return abs((lreset - rreset).total_seconds()) <= 120


def _group_observations(observations: Iterable[Mapping[str, Any]]) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    ordered: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, raw in enumerate(observations):
        row = dict(raw)
        observed = _time(row.get("observed_at") or row.get("event_at"))
        used = _finite(row.get("used_percent"))
        if observed is None:
            rejected.append({"index": index, "reason": "invalid_observed_at"})
            continue
        if row.get("source") not in (None, PRIMARY_SOURCE):
            rejected.append({"index": index, "reason": "non_primary_source"})
            continue
        if row.get("authority") not in (None, PRIMARY_AUTHORITY):
            rejected.append({"index": index, "reason": "non_authoritative_snapshot"})
            continue
        if row.get("limit_id") not in (None, PRIMARY_LIMIT):
            rejected.append({"index": index, "reason": "non_primary_limit"})
            continue
        if used is None or used < 0 or used > 100:
            rejected.append({"index": index, "reason": "invalid_used_percent"})
            continue
        row["observed_at"] = iso_utc(observed)
        row["used_percent"] = used
        row["source"] = row.get("source") or PRIMARY_SOURCE
        row["authority"] = row.get("authority") or PRIMARY_AUTHORITY
        row["limit_id"] = row.get("limit_id") or PRIMARY_LIMIT
        row["observation_id"] = str(row.get("observation_id") or row.get("snapshot_id") or stable_id("forecast-v2-observation", [row["observed_at"], row["used_percent"], _scope(row)]))
        ordered.append(row)
    ordered.sort(key=lambda x: (x["observed_at"], x["observation_id"]))
    groups: list[list[dict[str, Any]]] = []
    for row in ordered:
        target = next((group for group in reversed(groups) if group and _same_period(group[-1], row)), None)
        if target is None:
            target = []
            groups.append(target)
        target.append(row)
    return groups, rejected


def compress_observations(observations: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Keep the first and last sample of each displayed-value plateau.

    With non-negative debit parameters, intermediate samples inside one
    unchanged display interval add no tighter lower/upper envelope than the
    two edges.  Keeping both edges preserves the plateau constraint while
    avoiding tens of thousands of duplicate LP rows in the live fitter.
    """
    raw = list(observations)
    groups, rejected = _group_observations(raw)
    kept: list[dict[str, Any]] = []
    plateau_count = 0
    for group in groups:
        run: list[dict[str, Any]] = []
        previous: tuple[float, bool] | None = None
        for row in group:
            key = (float(row["used_percent"]), bool(row.get("saturated")))
            if previous is None or key == previous:
                run.append(row)
                previous = key
                continue
            plateau_count += 1
            kept.extend(run if len(run) == 1 else (run[0], run[-1]))
            run = [row]
            previous = key
        if run:
            plateau_count += 1
            kept.extend(run if len(run) == 1 else (run[0], run[-1]))
    kept.sort(key=lambda row: (row["observed_at"], row["observation_id"]))
    return {
        "observations": kept,
        "raw_count": len(raw),
        "compressed_count": len(kept),
        "plateau_count": plateau_count,
        "period_count": len(groups),
        "rejected": rejected,
        "method": "first_and_last_per_display_plateau_v1",
    }


def _bounds(used: float, row: Mapping[str, Any]) -> tuple[float, float | None, str]:
    # A saturated 100 display has no finite upper state bound.  Other integer
    # displays use a conservative closed quantization envelope.
    if bool(row.get("saturated")) or used >= 100:
        return max(0.0, used - 0.5), None, "saturated_or_capped"
    return max(0.0, used - 0.5), min(100.5, used + 0.5), "quantized_closed_interval"


def _request_time(row: Mapping[str, Any]) -> datetime | None:
    return _time(row.get("occurred_at") or row.get("event_at"))


def _normalized_requests(
    requests: Iterable[Mapping[str, Any]],
    *,
    input_cutoff: datetime | None,
    evidence_mode: str,
    models: Sequence[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    result: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for raw in requests:
        row = normalize_request(raw)
        payload = row.to_dict()
        occurred = _time(payload["occurred_at"])
        first_seen = _time(payload.get("first_seen_at"))
        reason = None
        if occurred is None:
            reason = "invalid_occurred_at"
        elif input_cutoff is not None and occurred > input_cutoff:
            reason = "occurred_after_input_cutoff"
        elif payload["conflict"]:
            reason = "conflicting_duplicate"
        elif payload.get("token_unknown_fields"):
            reason = "token_evidence_unknown"
        elif evidence_mode == "strict_pre_event" and first_seen is None:
            reason = "arrival_clock_unknown"
        elif evidence_mode == "strict_pre_event" and input_cutoff is not None and first_seen > input_cutoff:
            reason = "first_seen_after_input_cutoff"
        elif payload["model"] not in models:
            reason = "model_not_in_declared_parameter_set"
        if reason:
            rejected.append({"event_id": payload["event_id"], "reason": reason})
            continue
        if evidence_mode == "reconstructed" and input_cutoff is not None and first_seen is not None and first_seen > input_cutoff:
            payload["reconstructed"] = True
            payload["arrival_clock_status"] = "late_reconstructed"
        result.append(payload)
    return result, rejected


def build_design(
    observations: Iterable[Mapping[str, Any]],
    requests: Iterable[Mapping[str, Any]],
    *,
    models: Sequence[str] | None = None,
    alignment_offset_seconds: int = 0,
    input_cutoff: str | datetime | None = None,
    evidence_mode: str = "reconstructed",
    source_snapshot_id: str | None = None,
    observation_contract_id: str = "display-quantized-v1",
    feature_schema_id: str = "request-token-channels-v1",
    algorithm_id: str = "cumulative-debit-v1",
    include_request_ids: bool = True,
) -> dict[str, Any]:
    """Build one LP design for one fixed alignment candidate.

    The returned matrix uses parameters ``theta(model, channel)`` in
    percentage points per million tokens and one bounded ``alpha`` per quota
    period.  No background-rate parameter is included.
    """
    if alignment_offset_seconds not in ALIGNMENT_OFFSETS_SECONDS:
        raise ValueError("alignment_offset_seconds must be one of -120, 0, 120")
    if evidence_mode not in ("strict_pre_event", "reconstructed"):
        raise ValueError("unsupported evidence_mode")
    requests = list(requests)
    cutoff = _time(input_cutoff)
    groups, rejected_observations = _group_observations(observations)
    if models is None:
        inferred: set[str] = set()
        for raw in requests:
            value = raw.get("model")
            if value and value != "unknown":
                inferred.add(str(value))
        models = sorted(inferred)
    models = tuple(dict.fromkeys(str(x) for x in models if str(x)))
    normalized_requests, rejected_requests = _normalized_requests(
        requests, input_cutoff=cutoff, evidence_mode=evidence_mode, models=models
    )
    theta_names = [f"theta.{model}.{channel}" for model in models for channel in CHANNELS]
    model_indexes = {model: index for index, model in enumerate(models)}
    effective_requests: list[tuple[datetime, dict[str, Any]]] = []
    for request in normalized_requests:
        request_at = _request_time(request)
        if request_at is not None:
            effective_requests.append(
                (request_at + timedelta(seconds=alignment_offset_seconds), request)
            )
    effective_requests.sort(key=lambda item: (item[0], item[1]["event_id"]))
    effective_times = [item[0] for item in effective_requests]
    prefix: list[list[float]] = [[0.0] * len(theta_names)]
    for _, request in effective_requests:
        values = list(prefix[-1])
        model_index = model_indexes[request["model"]]
        offset = model_index * len(CHANNELS)
        for channel_index, channel in enumerate(CHANNELS):
            values[offset + channel_index] += request["tokens"][channel] / 1_000_000.0
        prefix.append(values)
    alpha_names: list[str] = []
    rows: list[dict[str, Any]] = []
    quality_flags: list[str] = []
    for group in groups:
        if not group:
            continue
        cycle_id = str(group[0].get("cycle_id") or group[0].get("period_id") or "cycle-" + stable_id("forecast-v2-cycle", [_scope(group[0]), group[0]["observed_at"]])[:16])
        if cycle_id in alpha_names:
            cycle_id = f"{cycle_id}-{len(alpha_names)}"
        alpha_names.append(cycle_id)
        anchor = _time(group[0]["observed_at"])
        if anchor is None:
            continue
        for obs in group:
            at = _time(obs["observed_at"])
            if at is None:
                continue
            left = bisect_left(effective_times, anchor)
            right = bisect_right(effective_times, at)
            features = [prefix[right][index] - prefix[left][index] for index in range(len(theta_names))]
            request_ids = (
                [request["event_id"] for _, request in effective_requests[left:right]]
                if include_request_ids
                else []
            )
            request_count = max(0, right - left)
            lower, upper, bound_kind = _bounds(float(obs["used_percent"]), obs)
            row_id = str(obs["observation_id"])
            if obs.get("coverage_unknown"):
                quality_flags.append("observation_coverage_unknown")
            if obs.get("saturated") or float(obs["used_percent"]) >= 100:
                quality_flags.append("saturation_present")
            rows.append({
                "row_id": row_id,
                "cycle_id": cycle_id,
                "observed_at": obs["observed_at"],
                "used_percent": float(obs["used_percent"]),
                "lower": lower,
                "upper": upper,
                "bound_kind": bound_kind,
                "features": features,
                "alpha_index": len(alpha_names) - 1,
                "observation_id": row_id,
                "request_event_ids": request_ids if include_request_ids else [],
                "request_count": request_count,
                "source": obs.get("source"),
                "authority": obs.get("authority"),
                "resets_at": obs.get("resets_at"),
            })
    parameter_names = theta_names + [f"alpha.{name}" for name in alpha_names]
    a_ub: list[list[float]] = []
    b_ub: list[float] = []
    constraint_rows: list[dict[str, Any]] = []
    for row in rows:
        vector = row["features"] + [1.0 if index == row["alpha_index"] else 0.0 for index in range(len(alpha_names))]
        if row["upper"] is not None:
            a_ub.append(vector)
            b_ub.append(float(row["upper"]))
            constraint_rows.append({"row_id": row["row_id"], "side": "upper", "b": float(row["upper"])})
        a_ub.append([-value for value in vector])
        b_ub.append(-float(row["lower"]))
        constraint_rows.append({"row_id": row["row_id"], "side": "lower", "b": -float(row["lower"])})
    if not rows:
        quality_flags.append("no_usable_observations")
    if rejected_requests:
        quality_flags.extend(sorted({item["reason"] for item in rejected_requests}))
    if rejected_observations:
        quality_flags.extend(sorted({item["reason"] for item in rejected_observations}))
    payload = {
        "algorithm_id": algorithm_id,
        "observation_contract_id": observation_contract_id,
        "feature_schema_id": feature_schema_id,
        "alignment_offset_seconds": alignment_offset_seconds,
        "models": list(models),
        "channels": list(CHANNELS),
        "rows": rows,
        "rejected_requests": rejected_requests,
        "rejected_observations": rejected_observations,
        "source_snapshot_id": source_snapshot_id,
        "input_cutoff": iso_utc(cutoff) if cutoff else None,
    }
    input_hash = stable_id("forecast-v2-design", payload)
    return {
        "schema_version": 1,
        "algorithm_id": algorithm_id,
        "observation_contract_id": observation_contract_id,
        "feature_schema_id": feature_schema_id,
        "source_snapshot_id": source_snapshot_id,
        "input_cutoff": iso_utc(cutoff) if cutoff else None,
        "input_sha256": input_hash,
        "alignment_offset_seconds": alignment_offset_seconds,
        "models": list(models),
        "channels": list(CHANNELS),
        "theta_parameter_names": theta_names,
        "alpha_parameter_names": alpha_names,
        "parameter_names": parameter_names,
        "theta_count": len(theta_names),
        "alpha_count": len(alpha_names),
        "rows": rows,
        "constraints": constraint_rows,
        "A_ub": a_ub,
        "b_ub": b_ub,
        "bounds": [[0.0, None]] * len(theta_names) + [[0.0, 100.5]] * len(alpha_names),
        "quality_flags": sorted(set(quality_flags)),
        "rejected_requests": rejected_requests,
        "rejected_observations": rejected_observations,
        "status": "ready" if rows and models else "insufficient_evidence",
        "background_rate": {"status": "fixed_zero", "included": False},
    }


def cumulative_token_vector(row: Mapping[str, Any], *, theta_count: int | None = None) -> list[float]:
    values = [float(x) for x in row.get("features", [])]
    if theta_count is not None:
        if len(values) > theta_count:
            raise ValueError("row has more features than theta_count")
        values.extend([0.0] * (theta_count - len(values)))
    return values
