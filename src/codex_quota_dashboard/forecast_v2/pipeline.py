"""Backend-only M3 orchestration for a conditional quota forecast."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..models import iso_utc, parse_timestamp, stable_id
from .composition import aggregate_compositions, compose_path
from .quota_mapping import aggregate_quota_paths
from .scenarios import generate_scenario_paths
from .workload import project_plan


def build_forecast(
    plan: Mapping[str, Any],
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    models: Sequence[str],
    reference_theta: Mapping[str, Mapping[str, float]] | Sequence[float],
    start_used_pp: float,
    reset_at: str | datetime | None = None,
    compatibility_design: Mapping[str, Any] | None = None,
    path_count: int = 128,
    seed: int = 906,
    bucket_seconds: int = 60,
) -> dict[str, Any]:
    """Generate one internally consistent scenario/concurrency/token/quota result."""
    normalized_plan = dict(plan)
    issued_at = parse_timestamp(normalized_plan.get("issued_at")) or datetime.now(timezone.utc)
    scenarios = generate_scenario_paths(normalized_plan, profiles, path_count=path_count, seed=seed)
    # Keep the composition clock anchored to the issued plan even when the
    # first declared/scheduled slot starts later than the input cutoff.  A
    # future-only plan must expose its idle prefix instead of shifting the
    # forecast origin forward and making the prefix unevaluable.
    compositions = [
        compose_path(
            path,
            start_at=normalized_plan["input_cutoff"],
            end_at=normalized_plan["horizon_end"],
            bucket_seconds=bucket_seconds,
        )
        for path in scenarios["paths"]
    ]
    workload = project_plan(normalized_plan, profiles)
    composition = aggregate_compositions(compositions)
    quota = aggregate_quota_paths(
        compositions,
        models=models,
        reference_theta=reference_theta,
        start_used_pp=start_used_pp,
        reset_at=reset_at,
        compatibility_design=compatibility_design,
    )
    unknowns = list(workload.get("unknowns", []))
    unknowns.extend(composition.get("unknowns", []))
    unknowns.extend(quota.get("unknowns", []))
    status = "conditional" if quota.get("points") else "insufficient_evidence"
    reference_parameters_id = "parameters-" + stable_id("forecast-v2-reference-parameters", reference_theta)[:24]
    return {
        "schema_version": 1,
        "version": "quota-forecast-v2-m3-core-v1",
        "algorithm_ids": ["workload-plan-v1", "scenario-fixed-profile-v1", "composition-v1", "quota-reference-map-v1"],
        "input_cutoff": normalized_plan.get("input_cutoff"),
        "issued_at": iso_utc(issued_at),
        "observed_at": normalized_plan.get("observed_at"),
        "observed_used_pp": float(start_used_pp),
        "source_snapshot_id": normalized_plan.get("source_snapshot_id"),
        "observation_contract_id": "display-quantized-v1",
        "reference_parameters_id": reference_parameters_id,
        "horizon_end": normalized_plan.get("horizon_end"),
        "scope": normalized_plan.get("mode"),
        "mode": normalized_plan.get("mode"),
        "scenario_set_id": scenarios["scenario_set_id"],
        "scenario": scenarios,
        "workload": workload,
        "composition": composition,
        "quota": quota,
        "points": quota.get("points", []),
        "unknowns": unknowns,
        "quality_flags": sorted({"simulation_uncalibrated"} | ({"partial_coverage"} if unknowns else set())),
        "status": status,
    }
