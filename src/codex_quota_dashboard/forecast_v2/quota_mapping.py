"""Map one unified scenario composition to conditional quota curves."""
from __future__ import annotations

from datetime import datetime, timedelta
import math
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from ..models import iso_utc, parse_timestamp
from .contracts import CHANNELS
from .debit_set import objective_from_model_channels, possible_display_values, solve_debit_set


def flatten_theta(models: Sequence[str], theta: Mapping[str, Mapping[str, float]] | Sequence[float]) -> list[float]:
    if isinstance(theta, Mapping):
        return [float(theta.get(model, {}).get(channel, 0.0)) for model in models for channel in CHANNELS]
    result = [float(x) for x in theta]
    expected = len(models) * len(CHANNELS)
    if len(result) != expected:
        raise ValueError("theta dimension mismatch")
    return result


def _token_vector(bucket: Mapping[str, Any], models: Sequence[str]) -> list[float]:
    totals = bucket.get("token_totals_by_model", {})
    return [float(totals.get(model, {}).get(channel, 0.0)) for model in models for channel in CHANNELS]


def map_composition(
    composition: Mapping[str, Any],
    *,
    models: Sequence[str],
    reference_theta: Mapping[str, Mapping[str, float]] | Sequence[float],
    start_used_pp: float,
    reset_at: str | datetime | None = None,
) -> dict[str, Any]:
    theta = np.asarray(flatten_theta(models, reference_theta), dtype=float)
    if not np.all(np.isfinite(theta)) or np.any(theta < 0):
        raise ValueError("reference theta must be finite and non-negative")
    current = float(start_used_pp)
    if not math.isfinite(current) or not 0 <= current <= 100:
        raise ValueError("start_used_pp must be in 0..100")
    reset = reset_at if isinstance(reset_at, datetime) else parse_timestamp(reset_at)
    unknowns = list(composition.get("unknowns", []))
    critical_unknowns = [
        item for item in unknowns
        if (isinstance(item, Mapping) and item.get("reason") in {
            "token_profile_unknown",
            "unknown_rate_basis",
            "profile_missing_or_version_mismatch",
            "rate_basis_mismatch",
            "activity_fraction_unknown",
        })
    ]
    if critical_unknowns:
        return {
            "status": "insufficient_evidence",
            "points": [],
            "unknowns": unknowns,
            "exhaustion": {"status": "unknown", "at": None},
            "probability_calibrated": False,
        }
    points: list[dict[str, Any]] = []
    exhaustion_at: str | None = iso_utc(parse_timestamp(composition.get("interval_start"))) if start_used_pp >= 100 and parse_timestamp(composition.get("interval_start")) else None
    for bucket in composition.get("buckets", []):
        at = parse_timestamp(bucket.get("time"))
        end = parse_timestamp(bucket.get("end_time"))
        if at is None or end is None or end <= at:
            continue
        if reset is not None and at >= reset:
            break
        width = (end - at).total_seconds()
        if reset is not None and end > reset:
            end = reset
            width = (end - at).total_seconds()
        if width <= 0:
            continue
        delta = float(np.dot(np.asarray(_token_vector(bucket, models), dtype=float) / 1_000_000.0, theta))
        before = current
        raw_after = current + max(0.0, delta)
        if exhaustion_at is None and before < 100 <= raw_after:
            fraction = (100.0 - before) / max(delta, 1e-12)
            exhaustion_at = iso_utc(at + timedelta(seconds=width * fraction))
        current = min(100.0, raw_after)
        points.append({
            "time": iso_utc(at),
            "end_time": iso_utc(end),
            "expected_used_pp": current,
            "remaining_pp": max(0.0, 100.0 - current),
            "delta_used_pp": max(0.0, delta),
            "token_totals_by_model": bucket.get("token_totals_by_model", {}),
        })
    if not points:
        return {
            "status": "insufficient_evidence",
            "points": [],
            "exhaustion": {"status": "unknown", "at": None},
            "probability_calibrated": False,
        }
    return {
        "status": "conditional",
        "points": points,
        "horizon_end": points[-1]["end_time"],
        "reference_parameters": {model: {channel: float(theta[i * 3 + j]) for j, channel in enumerate(CHANNELS)} for i, model in enumerate(models)},
        "reference_parameters_kind": "one_complete_feasible_reference_or_declared_parameter",
        "exhaustion": {
            "status": "exhausted" if exhaustion_at else "not_exhausted_before_reset",
            "at": exhaustion_at,
        },
        "range_kind": "reference_parameter_scenario_curve",
        "probability_calibrated": False,
        "internal_billing_verified": False,
        "unknowns": unknowns,
    }

def compatibility_band(
    design: Mapping[str, Any],
    *,
    token_totals_by_model: Mapping[str, Mapping[str, float]],
    start_used_pp: float,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    objective = objective_from_model_channels(design, token_totals_by_model)
    solved = solve_debit_set(design, target=objective, timeout_seconds=timeout_seconds)
    budget = solved.get("target_budget", {})
    lower = float(start_used_pp) + float(budget["lower"]) if budget.get("lower") is not None else None
    upper = float(start_used_pp) + float(budget["upper"]) if budget.get("upper") is not None else None
    return {
        "lower_used_pp": lower,
        "upper_used_pp": upper,
        "possible_display_values": possible_display_values(lower, upper),
        "range_kind": str(design.get("range_kind") or "compatibility_set"),
        "solver_status": solved.get("status"),
        "internal_billing_verified": False,
        "probability_calibrated": False,
    }


def aggregate_quota_paths(
    compositions: Iterable[Mapping[str, Any]],
    *,
    models: Sequence[str],
    reference_theta: Mapping[str, Mapping[str, float]] | Sequence[float],
    start_used_pp: float,
    reset_at: str | datetime | None = None,
    compatibility_design: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    compositions = list(compositions)
    mapped = [
        map_composition(
            composition,
            models=models,
            reference_theta=reference_theta,
            start_used_pp=start_used_pp,
            reset_at=reset_at,
        )
        for composition in compositions
    ]
    usable = [item for item in mapped if item.get("points")]
    if not usable:
        return {"status": "insufficient_evidence", "paths": len(mapped), "points": [], "probability_calibrated": False}
    point_rows: list[dict[str, Any]] = []
    cumulative_tokens: dict[str, dict[str, float]] = {model: {channel: 0.0 for channel in CHANNELS} for model in models}
    for index, first in enumerate(usable[0]["points"]):
        values = [item["points"][index]["expected_used_pp"] for item in usable if index < len(item["points"]) and item["points"][index]["time"] == first["time"]]
        if not values:
            continue
        aligned_compositions = [composition for composition in compositions if index < len(composition.get("buckets", [])) and composition["buckets"][index].get("time") == first["time"]]
        for model in models:
            for channel in CHANNELS:
                cumulative_tokens[model][channel] += (
                    sum(float(item["buckets"][index].get("token_totals_by_model", {}).get(model, {}).get(channel, 0.0)) for item in aligned_compositions) / len(aligned_compositions)
                    if aligned_compositions else 0.0
                )
        point = {
            "time": first["time"],
            "expected_used_pp": float(np.mean(values)),
            "quantiles_used_pp": {"p10": float(np.quantile(values, 0.1)), "p50": float(np.quantile(values, 0.5)), "p90": float(np.quantile(values, 0.9))},
            "remaining_pp": max(0.0, 100.0 - float(np.mean(values))),
        }
        if compatibility_design is not None:
            point["compatibility_used_pp"] = compatibility_band(
                compatibility_design,
                token_totals_by_model=cumulative_tokens,
                start_used_pp=start_used_pp,
            )
        point_rows.append(point)
    exhaustion = [item["exhaustion"] for item in usable]
    finite = [item for item in exhaustion if item.get("at")]
    return {
        "status": "conditional",
        "paths": len(mapped),
        "usable_paths": len(usable),
        "points": point_rows,
        "exhaustion": {
            "simulated_exhaustion_fraction": len(finite) / len(usable),
            "exhausted_paths": len(finite),
            "not_exhausted_before_reset": len(usable) - len(finite),
            "path_statuses": exhaustion,
        },
        "range_kind": "scenario_empirical_not_probability",
        "probability_calibrated": False,
        "internal_billing_verified": False,
        "unknowns": [item.get("unknowns", []) for item in mapped if item.get("unknowns")],
    }
