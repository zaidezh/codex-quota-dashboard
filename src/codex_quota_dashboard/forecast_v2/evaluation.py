"""As-issued, leakage-aware evaluation helpers for the v2 back end."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from statistics import median
from typing import Any, Iterable, Mapping

from ..models import iso_utc, parse_timestamp


STREAMS = (
    "E1_consumption_explanation",
    "E2_future_workload",
    "E3_future_quota",
    "E4_counterfactual",
)


def _timestamp(value: Any) -> datetime | None:
    return parse_timestamp(value) if value is not None else None


def is_matured(issue: Mapping[str, Any], now: datetime, maturity_delay_minutes: int = 5) -> bool:
    target = _timestamp(issue.get("target_at"))
    if target is None:
        return False
    return target <= now - timedelta(minutes=max(0, maturity_delay_minutes))


def non_overlapping(records: Iterable[Mapping[str, Any]], *, key: str = "target_at", horizon_minutes_key: str = "horizon_minutes") -> list[dict[str, Any]]:
    """Thin origins within a horizon without claiming IID independence."""
    selected: list[dict[str, Any]] = []
    last_by_horizon: dict[Any, datetime] = {}
    ordered = sorted((dict(x) for x in records), key=lambda x: (_timestamp(x.get(key)) or datetime.max.replace(tzinfo=timezone.utc)))
    for item in ordered:
        at = _timestamp(item.get(key))
        if at is None:
            continue
        horizon = item.get(horizon_minutes_key)
        spacing = max(0.0, float(horizon or 0)) * 60
        previous = last_by_horizon.get(horizon)
        if previous is not None and (at - previous).total_seconds() < spacing:
            continue
        last_by_horizon[horizon] = at
        selected.append(item)
    return selected


def _quantile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def score_records(records: Iterable[Mapping[str, Any]], *, error_key: str = "error_pp") -> dict[str, Any]:
    items = [dict(x) for x in records]
    comparable = [x for x in items if bool(x.get("comparable", True)) and x.get(error_key) is not None]
    errors = [float(x[error_key]) for x in comparable]
    absolute = [abs(x) for x in errors]
    inside = [x for x in comparable if x.get("compatibility_lower_pp") is not None and x.get("compatibility_upper_pp") is not None]
    covered = [
        float(x.get("observed_used_pp")) >= float(x["compatibility_lower_pp"]) - 1e-9
        and float(x.get("observed_used_pp")) <= float(x["compatibility_upper_pp"]) + 1e-9
        for x in inside
    ]
    exact_integer = [
        x for x in comparable
        if x.get("predicted_display") is not None
        and x.get("observed_display") is not None
        and x["predicted_display"] == x["observed_display"]
    ]
    return {
        "sample_count": len(comparable),
        "rejected_count": sum(1 for x in items if not bool(x.get("comparable", True))),
        "bias_pp": sum(errors) / len(errors) if errors else None,
        "mae_pp": sum(absolute) / len(absolute) if absolute else None,
        "mdae_pp": median(absolute) if absolute else None,
        "p50_abs_pp": _quantile(absolute, 0.50),
        "p90_abs_pp": _quantile(absolute, 0.90),
        "p95_abs_pp": _quantile(absolute, 0.95),
        "max_abs_pp": max(absolute) if absolute else None,
        "fraction_abs_le_0_1": sum(x <= 0.1 + 1e-12 for x in absolute) / len(absolute) if absolute else None,
        "integer_exact_hit_fraction": len(exact_integer) / len(comparable) if comparable else None,
        "compatibility_sample_count": len(inside),
        "compatibility_coverage": sum(covered) / len(covered) if covered else None,
        "compatibility_width_mean_pp": (
            sum(float(x["compatibility_upper_pp"]) - float(x["compatibility_lower_pp"]) for x in inside) / len(inside)
            if inside else None
        ),
        "not_a_probability_calibration": True,
    }

def make_score_record(
    *,
    issue_id: str,
    input_cutoff: str,
    issued_at: str,
    target_at: str,
    predicted_used_pp: float | None,
    observed_used_pp: float | None,
    comparable: bool,
    reasons: list[str] | None = None,
    compatibility: tuple[float | None, float | None] | None = None,
    predicted_display: int | None = None,
    observed_display: int | None = None,
    stream: str = "E3_future_quota",
) -> dict[str, Any]:
    if stream not in STREAMS:
        raise ValueError(f"unknown evaluation stream: {stream}")
    error = None
    if predicted_used_pp is not None and observed_used_pp is not None:
        error = float(observed_used_pp) - float(predicted_used_pp)
    lower, upper = compatibility or (None, None)
    return {
        "issue_id": issue_id,
        "input_cutoff": input_cutoff,
        "issued_at": issued_at,
        "target_at": target_at,
        "predicted_used_pp": predicted_used_pp,
        "observed_used_pp": observed_used_pp,
        "error_pp": error,
        "comparable": bool(comparable and error is not None),
        "reasons": list(reasons or []),
        "compatibility_lower_pp": lower,
        "compatibility_upper_pp": upper,
        "predicted_display": predicted_display,
        "observed_display": observed_display,
        "stream": stream,
    }


def evaluate_four_streams(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Score each stream separately; no stream is silently pooled with another."""
    groups = {name: [] for name in STREAMS}
    for record in records:
        stream = record.get("stream")
        if stream in groups:
            groups[stream].append(dict(record))
    return {name: score_records(items) for name, items in groups.items()}


def temporal_split(records: Iterable[Mapping[str, Any]], cutoff: str | datetime, *, gap_minutes: int = 0) -> dict[str, list[dict[str, Any]]]:
    boundary = _timestamp(cutoff)
    if boundary is None:
        raise ValueError("cutoff must be a timestamp")
    gap = timedelta(minutes=max(0, gap_minutes))
    train: list[dict[str, Any]] = []
    holdout: list[dict[str, Any]] = []
    buffered: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        at = _timestamp(item.get("target_at") or item.get("issued_at") or item.get("input_cutoff"))
        if at is None:
            item.setdefault("rejection", "missing_time")
            continue
        if at < boundary - gap:
            train.append(item)
        elif at > boundary + gap:
            holdout.append(item)
        else:
            item.setdefault("rejection", "temporal_buffer")
            buffered.append(item)
    return {"train": train, "holdout": holdout, "buffered": buffered}


def audit_baseline(*, reported_value_pp: float | None, original_output: Any = None, reproducible_inputs: bool = False) -> dict[str, Any]:
    """Keep the 0.227 claim honest unless its original output and inputs exist."""
    if original_output is not None and reproducible_inputs:
        return {"status": "reproduced", "reported_value_pp": reported_value_pp, "original_output": original_output}
    return {
        "status": "unreproduced",
        "reported_value_pp": reported_value_pp,
        "original_output": original_output,
        "reason": "缺少可核验的原始输出或完整复算输入；用户报告值不是本次运行结果",
    }
