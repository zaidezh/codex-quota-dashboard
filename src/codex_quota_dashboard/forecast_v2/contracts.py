"""Strict, side-effect-free validation for the forecast-v2 data contracts."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Mapping, Sequence

from ..models import parse_timestamp

CHANNELS = ("uncached_input", "cached_input", "output")
ALIGNMENT_OFFSETS_SECONDS = (-120, 0, 120)
RELEASE_STATUSES = ("draft", "release_ready", "deployed", "failed", "rolled_back")
GOAL_STATUSES = (
    "not_evaluated",
    "goal_unmet",
    "observed_mean_target_met",
    "observed_fraction_target_met",
    "observed_all_within_target",
)


class ContractError(ValueError):
    """Raised when a v2 payload violates a structural or semantic contract."""


def _fail(path: str, message: str) -> None:
    raise ContractError(f"{path}: {message}")


def _finite(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "必须是有限数字")
    value = float(value)
    if not math.isfinite(value):
        _fail(path, "不能是 NaN 或 Infinity")
    return value


def _timestamp(value: Any, path: str) -> datetime:
    if not isinstance(value, str):
        _fail(path, "必须是 ISO-8601 字符串")
    parsed = parse_timestamp(value)
    if parsed is None:
        _fail(path, "不是有效时间")
    return parsed


def _nonempty(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "不能为空字符串")
    return value


def validate_token_vector(value: Mapping[str, Any], path: str = "tokens") -> dict[str, float]:
    if not isinstance(value, Mapping):
        _fail(path, "必须是对象")
    result: dict[str, float] = {}
    for channel in CHANNELS:
        if channel not in value:
            _fail(f"{path}.{channel}", "缺失值必须标为 unknown，不能静默按 0 处理")
        result[channel] = _finite(value[channel], f"{path}.{channel}")
        if result[channel] < 0:
            _fail(f"{path}.{channel}", "不能为负")
    return result


def validate_profile(profile: Mapping[str, Any], path: str = "profile") -> dict[str, Any]:
    if not isinstance(profile, Mapping):
        _fail(path, "必须是对象")
    result = deepcopy(dict(profile))
    _nonempty(result.get("profile_id", "profile"), f"{path}.profile_id")
    _nonempty(result.get("profile_version", ""), f"{path}.profile_version")
    basis = result.get("rate_basis")
    if basis not in ("per_active_thread_minute", "per_running_thread_minute"):
        _fail(f"{path}.rate_basis", "必须是受支持的速率单位")
    result["tokens_per_minute"] = validate_token_vector(
        result.get("tokens_per_minute", {}), f"{path}.tokens_per_minute"
    )
    return result


def validate_workload_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a JSON-safe copy of the workload-plan contract."""
    if not isinstance(plan, Mapping):
        _fail("plan", "必须是对象")
    result = deepcopy(dict(plan))
    if result.get("schema_version") != 1:
        _fail("schema_version", "必须为 1")
    _nonempty(result.get("plan_id"), "plan_id")
    if result.get("mode") not in ("declared_scenario", "observed_workload_projection"):
        _fail("mode", "不是受支持的场景模式")
    if result.get("scope_merge") not in (
        "replaces_declared_scope",
        "additional_to_explicit_disjoint_scope",
    ):
        _fail("scope_merge", "不是受支持的合并语义")
    input_cutoff = _timestamp(result.get("input_cutoff"), "input_cutoff")
    issued_at = _timestamp(result.get("issued_at"), "issued_at")
    expires_at = _timestamp(result.get("expires_at"), "expires_at")
    horizon_end = _timestamp(result.get("horizon_end"), "horizon_end")
    if issued_at < input_cutoff:
        _fail("issued_at", "不能早于 input_cutoff")
    if expires_at < issued_at:
        _fail("expires_at", "不能早于 issued_at")
    if horizon_end < issued_at:
        _fail("horizon_end", "不能早于 issued_at")
    slots = result.get("slots")
    if not isinstance(slots, list):
        _fail("slots", "必须是数组")
    seen: set[str] = set()
    for index, slot in enumerate(slots):
        path = f"slots[{index}]"
        if not isinstance(slot, Mapping):
            _fail(path, "必须是对象")
        slot = dict(slot)
        _nonempty(slot.get("slot_id"), f"{path}.slot_id")
        if slot["slot_id"] in seen:
            _fail(f"{path}.slot_id", "同一计划内不能重复")
        seen.add(slot["slot_id"])
        start = _timestamp(slot.get("start_at"), f"{path}.start_at")
        duration = _finite(slot.get("duration_minutes"), f"{path}.duration_minutes")
        if duration <= 0:
            _fail(f"{path}.duration_minutes", "必须大于 0")
        if start < input_cutoff:
            _fail(f"{path}.start_at", "不能早于 input_cutoff")
        if start > horizon_end or start + timedelta(minutes=duration) > horizon_end:
            _fail(f"{path}.start_at", "计划槽必须落在 horizon_end 内")
        count = slot.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            _fail(f"{path}.count", "必须是非负整数")
        for key in ("model", "effort", "service_tier"):
            _nonempty(slot.get(key), f"{path}.{key}")
        basis = slot.get("rate_basis")
        if basis not in ("per_active_thread_minute", "per_running_thread_minute"):
            _fail(f"{path}.rate_basis", "不是受支持的速率单位")
        activity = slot.get("activity_fraction")
        if activity is not None:
            activity = _finite(activity, f"{path}.activity_fraction")
            if not 0 <= activity <= 1:
                _fail(f"{path}.activity_fraction", "必须在 0..1")
        elif basis == "per_active_thread_minute":
            _fail(f"{path}.activity_fraction", "按活跃分钟计费时不能未知")
        if slot.get("profile_id") is not None:
            _nonempty(slot["profile_id"], f"{path}.profile_id")
        if slot.get("profile_version") is not None:
            _nonempty(slot["profile_version"], f"{path}.profile_version")
        if not isinstance(slot.get("includes_descendants"), bool):
            _fail(f"{path}.includes_descendants", "必须是布尔值")
    assumptions = result.get("assumptions")
    if not isinstance(assumptions, list) or not all(isinstance(x, str) for x in assumptions):
        _fail("assumptions", "必须是字符串数组")
    evidence_ids = result.get("evidence_ids")
    if not isinstance(evidence_ids, list) or not all(isinstance(x, str) and x for x in evidence_ids):
        _fail("evidence_ids", "必须是非空字符串数组")
    return result


def validate_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate, Mapping):
        _fail("candidate", "必须是对象")
    result = deepcopy(dict(candidate))
    if result.get("schema_version") != 1:
        _fail("schema_version", "必须为 1")
    for key in ("candidate_id", "incident_id", "author_role", "hypothesis", "falsification", "evaluation_spec_id", "policy_id"):
        _nonempty(result.get(key), key)
    if result.get("phase") not in {
        "proposed", "in_scope", "developing", "frozen", "evaluating", "awaiting_evidence",
        "reviewed", "release_ready", "deployed", "rejected", "rolled_back", "escalated", "suspended",
    }:
        _fail("phase", "不是受支持的候选阶段")
    if result.get("recipe_id") not in {"MP01", "MP02", "MP03", "MP04", "MP05", "MP06", "MP07", "M2", "M3", "M4", "M5"}:
        _fail("recipe_id", "不是登记的配方")
    if result.get("target_layer") not in {
        "data_quality", "observation_alignment", "consumption_explanation", "workload_generation",
        "quota_forecast", "thread_distribution", "presentation",
    }:
        _fail("target_layer", "不是受支持的目标层")
    for key in ("allowed_change", "forbidden_change", "source_snapshot_ids", "evidence_ids"):
        value = result.get(key)
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            _fail(key, "必须是字符串数组")
    variant_count = result.get("variant_count")
    if isinstance(variant_count, bool) or not isinstance(variant_count, int) or variant_count < 1:
        _fail("variant_count", "必须是正整数")
    if not isinstance(result.get("requires_route_decision"), bool):
        _fail("requires_route_decision", "必须是布尔值")
    return result


def validate_release(release: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(release, Mapping):
        _fail("release", "必须是对象")
    result = deepcopy(dict(release))
    if result.get("schema_version") != 1:
        _fail("schema_version", "必须为 1")
    for key in ("release_id", "candidate_id", "policy_id", "evaluation_spec_id"):
        _nonempty(result.get(key), key)
    if result.get("status") not in RELEASE_STATUSES:
        _fail("status", "不是受支持的发布状态")
    previous = result.get("expected_previous_release_id")
    if previous is not None:
        _nonempty(previous, "expected_previous_release_id")
    hashes = result.get("artifact_hashes")
    if not isinstance(hashes, Mapping):
        _fail("artifact_hashes", "必须是对象")
    for key, value in hashes.items():
        if not isinstance(key, str) or not isinstance(value, str) or len(value) != 64:
            _fail("artifact_hashes", "键和值必须是 SHA-256 字符串")
        try:
            int(value, 16)
        except ValueError:
            _fail("artifact_hashes", "包含非法 SHA-256")
    for key in ("author_execution_id", "reviewer_execution_id", "independent_review_receipt_id", "approval_receipt_id", "rollback_release_id", "actual_service_release_id"):
        if result.get(key) is not None:
            _nonempty(result[key], key)
    if result.get("goal_status") not in GOAL_STATUSES:
        _fail("goal_status", "不是受支持的目标状态")
    if result["status"] in ("release_ready", "deployed"):
        if len(hashes) < 5:
            _fail("artifact_hashes", "release_ready/deployed 至少需要五类工件哈希")
        for key in ("author_execution_id", "reviewer_execution_id", "independent_review_receipt_id", "approval_receipt_id", "rollback_release_id"):
            _nonempty(result.get(key), key)
        if result["author_execution_id"] == result["reviewer_execution_id"]:
            _fail("reviewer_execution_id", "不能与候选作者执行身份相同")
    if result["status"] == "deployed":
        _nonempty(result.get("actual_service_release_id"), "actual_service_release_id")
    return result


def validate_forecast_points(points: Sequence[Mapping[str, Any]], path: str = "points") -> None:
    previous: datetime | None = None
    for index, point in enumerate(points):
        item_path = f"{path}[{index}]"
        if not isinstance(point, Mapping):
            _fail(item_path, "必须是对象")
        at = _timestamp(point.get("time"), f"{item_path}.time")
        if previous is not None and at < previous:
            _fail(item_path, "时间不能倒退")
        previous = at
        for key in ("expected_used_pp", "remaining_pp"):
            if point.get(key) is not None:
                _finite(point[key], f"{item_path}.{key}")
