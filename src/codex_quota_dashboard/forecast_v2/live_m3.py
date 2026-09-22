"""Primary M3 projection from direct native workload and a frozen M2 reference."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from ..models import iso_utc, parse_timestamp, stable_id
from .contracts import CHANNELS
from .pipeline import build_forecast


VERSION = "quota-forecast-v2-m3-live-v2"
BOOTSTRAP_SCHEMA = "quota-forecast-v2-bootstrap-reference-v1"


def _unavailable(status: str, reason: str, *, m2: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "version": VERSION,
        "status": status,
        "reason": reason,
        "m2_id": m2.get("m2_id"),
        "m2_status": m2.get("status"),
        "points": [],
    }


def _bootstrap(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("schema") != BOOTSTRAP_SCHEMA:
        raise ValueError("bootstrap_schema_invalid")
    models = payload.get("models")
    if not isinstance(models, dict) or not models:
        raise ValueError("bootstrap_models_missing")
    reference: dict[str, dict[str, float]] = {}
    bounds: list[list[float | None]] = []
    names: list[str] = []
    for model, raw_channels in models.items():
        if not isinstance(model, str) or not model or not isinstance(raw_channels, Mapping):
            raise ValueError("bootstrap_model_invalid")
        reference[model] = {}
        for channel in CHANNELS:
            item = raw_channels.get(channel)
            if not isinstance(item, Mapping):
                raise ValueError("bootstrap_channel_missing")
            value = float(item.get("reference"))
            lower = float(item.get("lower"))
            upper_raw = item.get("upper")
            upper = float(upper_raw) if upper_raw is not None else None
            if not math.isfinite(value) or not math.isfinite(lower) or value < 0 or lower < 0:
                raise ValueError("bootstrap_value_invalid")
            if upper is not None and (not math.isfinite(upper) or upper < lower):
                raise ValueError("bootstrap_bound_invalid")
            if value < lower or (upper is not None and value > upper):
                raise ValueError("bootstrap_reference_outside_bounds")
            reference[model][channel] = value
            names.append(f"theta.{model}.{channel}")
            bounds.append([lower, upper])
    models_order = list(models)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = sha256(canonical).hexdigest()
    design = {
        "schema_version": 1,
        "algorithm_id": "bootstrap-parameter-box-v1",
        "input_sha256": digest,
        "alignment_offset_seconds": None,
        "models": models_order,
        "channels": list(CHANNELS),
        "theta_parameter_names": names,
        "alpha_parameter_names": [],
        "parameter_names": names,
        "theta_count": len(names),
        "alpha_count": 0,
        "A_ub": [],
        "b_ub": [],
        "bounds": bounds,
    }
    return {
        "source": "bootstrap_reference",
        "reference_id": str(payload.get("reference_id") or "bootstrap-" + digest[:24]),
        "content_sha256": digest,
        "models": models_order,
        "reference_theta": reference,
        "compatibility_design": design,
        "metadata": {
            key: payload.get(key)
            for key in (
                "unit",
                "source_category",
                "privacy",
                "intended_use",
                "limitations",
            )
            if payload.get(key) is not None
        },
    }


def _local_m2_reference(m2: Mapping[str, Any]) -> dict[str, Any] | None:
    fit_status = str(m2.get("status") or "")
    if fit_status not in ("feasible", "approximate"):
        return None
    selected_offset = m2.get("selected_alignment_offset_seconds")
    candidates = [
        item for item in m2.get("_solver_candidates", [])
        if item.get("status") == fit_status
    ]
    selected = next(
        (item for item in candidates if item.get("alignment_offset_seconds") == selected_offset),
        candidates[0] if candidates else None,
    )
    if selected is None or not selected.get("reference_parameters") or not selected.get("design"):
        return None
    models = list(m2.get("models", []))
    values = list(selected["reference_parameters"])
    theta_count = int(selected["design"].get("theta_count", 0))
    theta = values[:theta_count]
    if len(theta) != len(models) * len(CHANNELS):
        return None
    compatibility_design = selected["design"]
    if fit_status == "approximate":
        compatibility_design = m2.get("_projection_design")
        if not isinstance(compatibility_design, Mapping):
            return None
    reference = {
        model: {
            channel: float(theta[index * len(CHANNELS) + channel_index])
            for channel_index, channel in enumerate(CHANNELS)
        }
        for index, model in enumerate(models)
    }
    return {
        "source": "local_m2_explanation",
        "reference_id": m2.get("m2_id"),
        "content_sha256": compatibility_design.get("input_sha256"),
        "models": models,
        "reference_theta": reference,
        "compatibility_design": compatibility_design,
        "metadata": {
            "alignment_offset_seconds": selected.get("alignment_offset_seconds"),
            "fit_status": fit_status,
            "strict_status": m2.get("strict_status"),
            "fit_kind": m2.get("fit_kind"),
            "fit_error": selected.get("fit_error") or m2.get("fit_error"),
            "range_kind": m2.get("range_kind"),
            "assumption_id": "observed_local_activity_principal_explanation_v1",
        },
    }


def _workload(
    db: sqlite3.Connection,
    now: datetime,
    reset_at: datetime,
    models: Sequence[str],
    *,
    lookback_minutes: int,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    start = now - timedelta(minutes=max(15, int(lookback_minutes)))
    rows = [
        dict(row)
        for row in db.execute(
            """
            SELECT response_id, occurred_at, model, service_tier, reasoning_effort,
                   input_tokens, cached_input_tokens, output_tokens
            FROM native_requests
            WHERE conflict=0 AND occurred_at>=? AND occurred_at<=?
            ORDER BY occurred_at, response_id
            """,
            (iso_utc(start), iso_utc(now)),
        )
    ]
    totals = {
        model: {channel: 0.0 for channel in CHANNELS}
        for model in models
    }
    tiers: dict[str, set[str]] = {model: set() for model in models}
    efforts: dict[str, set[str]] = {model: set() for model in models}
    included = 0
    unknown_models: Counter[str] = Counter()
    for row in rows:
        model = str(row.get("model") or "unknown")
        if model not in totals:
            unknown_models[model] += 1
            continue
        input_tokens = float(row.get("input_tokens") or 0)
        cached = float(row.get("cached_input_tokens") or 0)
        totals[model]["uncached_input"] += max(0.0, input_tokens - cached)
        totals[model]["cached_input"] += cached
        totals[model]["output"] += float(row.get("output_tokens") or 0)
        tiers[model].add(str(row.get("service_tier") or "unknown"))
        efforts[model].add(str(row.get("reasoning_effort") or "unknown"))
        included += 1
    minutes = max(1.0, (now - start).total_seconds() / 60.0)
    duration = max(0.0, (reset_at - now).total_seconds() / 60.0)
    profiles: dict[str, dict[str, Any]] = {}
    slots: list[dict[str, Any]] = []
    workload_rows: list[dict[str, Any]] = []
    for model in models:
        if sum(totals[model].values()) <= 0:
            continue
        profile_id = "recent-wall-clock:" + stable_id(
            "forecast-v2-m3-rate", [model, iso_utc(start), iso_utc(now), totals[model]]
        )[:20]
        profile_version = "recent-wall-clock-v1"
        rates = {channel: totals[model][channel] / minutes for channel in CHANNELS}
        profiles[profile_id] = {
            "profile_id": profile_id,
            "profile_version": profile_version,
            "rate_basis": "per_running_thread_minute",
            "tokens_per_minute": rates,
        }
        slots.append(
            {
                "slot_id": "continuation:" + model,
                "start_at": iso_utc(now),
                "duration_minutes": duration,
                "count": 1,
                "model": model,
                "effort": "/".join(sorted(efforts[model])) or "unknown",
                "service_tier": "/".join(sorted(tiers[model])) or "unknown",
                "rate_basis": "per_running_thread_minute",
                "activity_fraction": None,
                "profile_id": profile_id,
                "profile_version": profile_version,
                "includes_descendants": False,
            }
        )
        workload_rows.append(
            {
                "model": model,
                "tokens_per_wall_minute": rates,
                "service_tiers": sorted(tiers[model]),
                "reasoning_efforts": sorted(efforts[model]),
            }
        )
    plan = {
        "schema_version": 1,
        "plan_id": "live-plan-" + stable_id(
            "forecast-v2-m3-live-plan",
            [iso_utc(start), iso_utc(now), iso_utc(reset_at), slots],
        )[:24],
        "mode": "observed_task_forecast",
        "scope_merge": "replaces_declared_scope",
        "input_cutoff": iso_utc(now),
        "issued_at": iso_utc(now),
        "observed_at": iso_utc(now),
        "expires_at": iso_utc(now + timedelta(minutes=5)),
        "horizon_end": iso_utc(reset_at),
        "source_snapshot_id": "m1-live:" + stable_id(
            "forecast-v2-m3-evidence", [iso_utc(start), iso_utc(now), included]
        )[:24],
        "slots": slots,
        "assumptions": [
            f"最近 {int(minutes)} 分钟的每模型 wall-clock token 速率延续至本次重置",
            "这是条件情景，不是经概率校准的未来使用承诺",
        ],
        "evidence_ids": [f"native_requests:{included}", f"window_start:{iso_utc(start)}"],
    }
    summary = {
        "start": iso_utc(start),
        "end": iso_utc(now),
        "lookback_minutes": int(minutes),
        "request_count": included,
        "unknown_model_requests": dict(unknown_models),
        "models": workload_rows,
        "method": "recent_wall_clock_rate_continuation_v1",
    }
    return plan, profiles, summary


def build_live_m3(
    db: sqlite3.Connection,
    m2: Mapping[str, Any],
    latest_limit: Mapping[str, Any] | None,
    *,
    bootstrap_reference_path: Path | None,
    now: datetime | None = None,
    lookback_minutes: int = 120,
    path_count: int = 1,
    seed: int = 906,
    bucket_seconds: int = 900,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if not latest_limit:
        return _unavailable("insufficient_evidence", "latest_limit_missing", m2=m2)
    reset_at = parse_timestamp(latest_limit.get("resets_at"))
    used = latest_limit.get("used_percent")
    if reset_at is None or reset_at <= now or used is None:
        return _unavailable("insufficient_evidence", "current_quota_window_invalid", m2=m2)
    reference = _local_m2_reference(m2)
    if reference is None and bootstrap_reference_path is not None:
        try:
            reference = _bootstrap(bootstrap_reference_path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            return _unavailable("unavailable", f"bootstrap_reference_invalid:{type(error).__name__}", m2=m2)
    if reference is None:
        return _unavailable("insufficient_evidence", "m2_not_usable_and_bootstrap_not_enabled", m2=m2)
    try:
        plan, profiles, workload = _workload(
            db,
            now,
            reset_at,
            reference["models"],
            lookback_minutes=lookback_minutes,
        )
    except sqlite3.Error as error:
        return _unavailable("unavailable", f"workload_read_failed:{type(error).__name__}", m2=m2)
    if not plan["slots"]:
        result = _unavailable("idle", "recent_native_workload_empty", m2=m2)
        result.update(
            generated_at=iso_utc(now),
            input_cutoff=iso_utc(now),
            reference_source=reference["source"],
            reference_id=reference["reference_id"],
            workload=workload,
        )
        return result
    try:
        forecast = build_forecast(
            plan,
            profiles,
            models=reference["models"],
            reference_theta=reference["reference_theta"],
            start_used_pp=float(used),
            reset_at=reset_at,
            compatibility_design=reference["compatibility_design"],
            path_count=path_count,
            seed=seed,
            bucket_seconds=bucket_seconds,
        )
    except (ValueError, TypeError) as error:
        return _unavailable("unavailable", f"m3_build_failed:{type(error).__name__}", m2=m2)
    forecast.update(
        version=VERSION,
        status="conditional" if forecast.get("points") else "insufficient_evidence",
        generated_at=iso_utc(now),
        m2_id=m2.get("m2_id"),
        m2_status=m2.get("status"),
        reference_source=reference["source"],
        reference_id=reference["reference_id"],
        reference_sha256=reference.get("content_sha256"),
        reference_metadata=reference.get("metadata", {}),
        workload_window=workload,
        quality_flags=sorted(
            set(forecast.get("quality_flags", []))
            | ({"bootstrap_reference"} if reference["source"] == "bootstrap_reference" else set())
            | ({"approximate_m2_explanation"} if m2.get("status") == "approximate" and reference["source"] == "local_m2_explanation" else set())
        ),
    )
    for point in forecast.get("points", []):
        band = point.get("compatibility_used_pp")
        if isinstance(band, dict):
            # The UI and persisted contract need the continuous bounds, not a
            # potentially hundreds-item enumeration at every time bucket.
            band.pop("possible_display_values", None)
    forecast["m3_id"] = stable_id(
        "forecast-v2-m3-live",
        [forecast.get("source_snapshot_id"), reference["reference_id"], forecast.get("scenario_set_id")],
    )
    return forecast
