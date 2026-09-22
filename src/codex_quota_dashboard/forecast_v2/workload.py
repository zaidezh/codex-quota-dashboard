"""Small, explicit workload-plan projection primitives."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .contracts import CHANNELS, ContractError, validate_profile, validate_workload_plan


def normalize_profiles(profiles: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for profile_id, raw in profiles.items():
        value = deepcopy(dict(raw))
        value.setdefault("profile_id", profile_id)
        value.setdefault("profile_version", "unversioned")
        result[str(profile_id)] = validate_profile(value, f"profiles[{profile_id}]")
    return result


def project_slot(slot: Mapping[str, Any], profiles: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    profile_id = slot.get("profile_id")
    profile = profiles.get(profile_id) if profile_id is not None else None
    base = {
        "slot_id": slot.get("slot_id"),
        "model": slot.get("model"),
        "effort": slot.get("effort"),
        "service_tier": slot.get("service_tier"),
        "profile_id": profile_id,
        "profile_version": slot.get("profile_version"),
        "rate_basis": slot.get("rate_basis"),
        "status": "projected",
        "unknowns": [],
    }
    if profile is None:
        base.update(status="unknown", token_totals=None, running_thread_minutes=None, active_thread_minutes=None)
        base["unknowns"].append("profile_missing")
        return base
    if profile.get("profile_version") != slot.get("profile_version"):
        base.update(status="unknown", token_totals=None, running_thread_minutes=None, active_thread_minutes=None)
        base["unknowns"].append("profile_version_mismatch")
        return base
    if profile.get("rate_basis") != slot.get("rate_basis"):
        base.update(status="unknown", token_totals=None, running_thread_minutes=None, active_thread_minutes=None)
        base["unknowns"].append("rate_basis_mismatch")
        return base
    duration = float(slot["duration_minutes"])
    count = int(slot["count"])
    activity = slot.get("activity_fraction")
    running = duration * count
    active = running * float(activity) if activity is not None else None
    effective_minutes = active if profile["rate_basis"] == "per_active_thread_minute" else running
    token_totals = {
        channel: float(profile["tokens_per_minute"].get(channel, 0.0)) * effective_minutes
        for channel in CHANNELS
    }
    return {
        **base,
        "running_thread_minutes": running,
        "active_thread_minutes": active,
        "request_active_thread_minutes": active,
        "effective_rate_minutes": effective_minutes,
        "token_totals": token_totals,
        "profile_id": profile["profile_id"],
        "profile_version": profile["profile_version"],
    }

def project_plan(plan: Mapping[str, Any], profiles: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Project one declared plan without generating a second hidden workload."""
    normalized_plan = validate_workload_plan(plan)
    normalized_profiles = normalize_profiles(profiles)
    slots = [project_slot(slot, normalized_profiles) for slot in normalized_plan["slots"]]
    totals = {channel: 0.0 for channel in CHANNELS}
    running = 0.0
    active = 0.0
    active_known = True
    unknowns: list[dict[str, Any]] = []
    for slot in slots:
        if slot["status"] != "projected":
            unknowns.append({"slot_id": slot["slot_id"], "reasons": slot["unknowns"]})
            continue
        running += float(slot["running_thread_minutes"])
        if slot["active_thread_minutes"] is None:
            active_known = False
        else:
            active += float(slot["active_thread_minutes"])
        for channel in CHANNELS:
            totals[channel] += float(slot["token_totals"][channel])
    return {
        "schema_version": 1,
        "plan_id": normalized_plan["plan_id"],
        "mode": normalized_plan["mode"],
        "scope_merge": normalized_plan["scope_merge"],
        "slots": slots,
        "totals": {
            "running_thread_minutes": running,
            "active_thread_minutes": active if active_known else None,
            "request_active_thread_minutes": active if active_known else None,
            "tokens": totals if not unknowns else None,
        },
        "unknowns": unknowns,
        "quality_flags": ["profile_coverage_incomplete"] if unknowns else [],
        "activity_multiplier_applied_once": True,
    }
