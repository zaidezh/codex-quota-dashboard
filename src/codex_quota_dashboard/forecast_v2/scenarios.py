"""Deterministic event-driven scenario generation for the M3 route."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Any, Mapping

from ..models import iso_utc, parse_timestamp, stable_id
from .contracts import validate_workload_plan
from .workload import normalize_profiles


def generate_scenario_paths(
    plan: Mapping[str, Any],
    profiles: Mapping[str, Mapping[str, Any]],
    *,
    path_count: int = 128,
    seed: int = 906,
    max_events: int = 100_000,
) -> dict[str, Any]:
    """Create reproducible paths from one plan.

    M3 deliberately uses fixed declared durations and profile rates.  It does
    not claim a calibrated probability model.  Future births/descendants must
    be supplied as separate slots or an observed-task plan; they are never
    silently invented here.
    """
    normalized_plan = validate_workload_plan(plan)
    if isinstance(path_count, bool) or not isinstance(path_count, int) or not 1 <= path_count <= 1024:
        raise ValueError("path_count must be between 1 and 1024")
    if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events < 1:
        raise ValueError("max_events must be positive")
    normalized_profiles = normalize_profiles(profiles)
    origin = parse_timestamp(normalized_plan["input_cutoff"])
    horizon = parse_timestamp(normalized_plan["horizon_end"])
    if origin is None or horizon is None:
        raise ValueError("plan timestamps are invalid")
    scenario_set_id = "scenario-set-" + stable_id(
        "forecast-v2-scenario-set",
        {
            "plan": normalized_plan,
            "profiles": normalized_profiles,
            "path_count": path_count,
            "seed": seed,
        },
    )[:24]
    paths: list[dict[str, Any]] = []
    for path_index in range(path_count):
        intervals: list[dict[str, Any]] = []
        unknowns: list[dict[str, Any]] = []
        for slot in normalized_plan["slots"]:
            profile = normalized_profiles.get(slot.get("profile_id"))
            if profile is None or profile.get("profile_version") != slot.get("profile_version"):
                unknowns.append({"slot_id": slot["slot_id"], "reason": "profile_missing_or_version_mismatch"})
            elif profile.get("rate_basis") != slot.get("rate_basis"):
                unknowns.append({"slot_id": slot["slot_id"], "reason": "rate_basis_mismatch"})
            for ordinal in range(int(slot["count"])):
                start = parse_timestamp(slot["start_at"])
                if start is None:
                    continue
                end = start + timedelta(minutes=float(slot["duration_minutes"]))
                thread_id = f"{slot['slot_id']}:{ordinal}"
                intervals.append({
                    "thread_id": thread_id,
                    "slot_id": slot["slot_id"],
                    "start_at": iso_utc(start),
                    "end_at": iso_utc(end),
                    "model": slot["model"],
                    "effort": slot["effort"],
                    "service_tier": slot["service_tier"],
                    "activity_fraction": slot.get("activity_fraction"),
                    "rate_basis": slot["rate_basis"],
                    "profile_id": slot.get("profile_id"),
                    "profile_version": slot.get("profile_version"),
                    "tokens_per_minute": deepcopy(profile.get("tokens_per_minute", {})) if profile else None,
                    "includes_descendants": bool(slot["includes_descendants"]),
                })
        truncation = "none"
        if len(intervals) > max_events:
            intervals = intervals[:max_events]
            truncation = "truncated_unknown"
            unknowns.append({"reason": "max_events_reached", "max_events": max_events})
        path_id = "path-" + stable_id("forecast-v2-scenario-path", [scenario_set_id, path_index])[:24]
        paths.append({
            "scenario_id": path_id,
            "scenario_set_id": scenario_set_id,
            "origin_at": iso_utc(origin),
            "horizon_end": iso_utc(horizon),
            "scenario_seed": int(seed) + path_index,
            "root_task_births": [dict(x) for x in intervals if x["slot_id"]],
            "child_task_births": [],
            "genealogy": [],
            "intervals": intervals,
            "state_intervals": [],
            "terminations": [{"thread_id": x["thread_id"], "at": x["end_at"], "kind": "declared_end"} for x in intervals],
            "request_events": [],
            "token_marks": [],
            "coverage_unknowns": unknowns,
            "truncation_status": truncation,
            "workload_model_id": "declared-plan-fixed-profile-v1",
            "input_snapshot_id": normalized_plan.get("evidence_ids", []),
        })
    return {
        "schema_version": 1,
        "scenario_set_id": scenario_set_id,
        "plan_id": normalized_plan["plan_id"],
        "path_count": path_count,
        "seed": seed,
        "probability_calibrated": False,
        "range_kind": "scenario_empirical_not_probability",
        "paths": paths,
    }
