"""Live M1/M2 fitting directly from authoritative local observations."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from ..models import iso_utc, parse_timestamp, stable_id
from .contracts import ALIGNMENT_OFFSETS_SECONDS, CHANNELS
from .cumulative_design import build_design, compress_observations
from .debit_set import objective_from_model_channels, solve_debit_set
from .evidence import build_snapshot


VERSION = "quota-forecast-v2-m2-live-v2"
ALGORITHM_ID = "cumulative-debit-explanation-v1"


def _rows(db: sqlite3.Connection, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(sql, params)]


def _load_requests(db: sqlite3.Connection, start: datetime, cutoff: datetime) -> list[dict[str, Any]]:
    values = _rows(
        db,
        """
        SELECT response_id, occurred_at, model, service_tier, reasoning_effort,
               input_tokens, cached_input_tokens, output_tokens
        FROM native_requests
        WHERE conflict=0 AND occurred_at>=? AND occurred_at<=?
        ORDER BY occurred_at, response_id
        """,
        (iso_utc(start), iso_utc(cutoff)),
    )
    result: list[dict[str, Any]] = []
    for row in values:
        response_id = row.pop("response_id")
        total_input = int(row.pop("input_tokens"))
        cached = int(row.pop("cached_input_tokens"))
        output = int(row.pop("output_tokens"))
        result.append({
            **row,
            "event_id": response_id,
            "uncached_input": max(0, total_input - cached),
            "cached_input": cached,
            "output": output,
            "source": "native_requests",
        })
    return result


def _load_observations(db: sqlite3.Connection, start: datetime, cutoff: datetime) -> list[dict[str, Any]]:
    return _rows(
        db,
        """
        SELECT snapshot_id AS observation_id, observed_at, collector_id,
               limit_id, used_percent, window_minutes, resets_at, plan_type,
               source, authority
        FROM rate_limit_snapshots
        WHERE source='appserver_account_rate_limits'
          AND authority='server_authoritative'
          AND limit_id='codex:primary'
          AND observed_at>=? AND observed_at<=?
        ORDER BY observed_at, snapshot_id
        """,
        (iso_utc(start), iso_utc(cutoff)),
    )


def _has_informative_quota_change(observations: Sequence[Mapping[str, Any]]) -> bool:
    """Return whether one reset period contains an observed display change.

    A single authoritative point, or an unchanged display plateau, permits a
    degenerate all-zero parameter vector but does not identify local debit
    parameters. Such evidence must remain in cold-start state so M3 can use an
    explicitly enabled bootstrap reference instead of promoting a false local
    fit.
    """
    for index, left in enumerate(observations):
        left_used = left.get("used_percent")
        left_reset = parse_timestamp(left.get("resets_at"))
        if left_used is None:
            continue
        for right in observations[index + 1:]:
            right_used = right.get("used_percent")
            right_reset = parse_timestamp(right.get("resets_at"))
            if right_used is None or math.isclose(float(left_used), float(right_used), abs_tol=1e-12):
                continue
            same_period = (
                left_reset is None and right_reset is None
                or left_reset is not None
                and right_reset is not None
                and abs((left_reset - right_reset).total_seconds()) <= 120
            )
            if same_period:
                return True
    return False


def _solver_design(design: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "algorithm_id", "input_sha256", "alignment_offset_seconds", "models",
        "channels", "theta_parameter_names", "alpha_parameter_names",
        "parameter_names", "theta_count", "alpha_count", "A_ub", "b_ub",
        "bounds",
    )
    return {key: design.get(key) for key in keys}


def _finite_reference(values: Any, expected: int) -> list[float] | None:
    if not isinstance(values, (list, tuple)) or len(values) < expected:
        return None
    reference = [float(value) for value in values[:expected]]
    if any(not math.isfinite(value) or value < 0 for value in reference):
        return None
    return reference


def _fit_error(diagnostic: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "method": diagnostic.get("method") or "minimum_l1_constraint_residual",
        "constraint_slack_sum_pp": diagnostic.get("slack_sum"),
        "max_constraint_slack_pp": diagnostic.get("max_slack"),
        "mean_constraint_slack_pp": diagnostic.get("mean_slack"),
        "affected_constraint_count": diagnostic.get("affected_constraint_count"),
        "constraint_count": diagnostic.get("constraint_count"),
    }


def _explanation_candidate(design: Mapping[str, Any], solved: Mapping[str, Any]) -> dict[str, Any]:
    """Promote a finite minimum-residual witness without changing strict status."""
    candidate = dict(solved)
    strict_status = str(solved.get("status") or "numeric_failure")
    candidate["strict_status"] = strict_status
    if strict_status == "feasible":
        candidate["fit_kind"] = "exact_compatibility"
        candidate["fit_error"] = {
            "method": "exact_constraints",
            "constraint_slack_sum_pp": 0.0,
            "max_constraint_slack_pp": 0.0,
            "mean_constraint_slack_pp": 0.0,
            "affected_constraint_count": 0,
            "constraint_count": len(design.get("b_ub", [])),
        }
        return candidate
    if strict_status != "infeasible":
        return candidate
    diagnostic = solved.get("diagnostic")
    if not isinstance(diagnostic, Mapping) or diagnostic.get("status") != "feasible":
        return candidate
    dimension = int(design.get("theta_count", 0)) + int(design.get("alpha_count", 0))
    reference = _finite_reference(diagnostic.get("reference_parameters"), dimension)
    if reference is None:
        return candidate
    candidate.update(
        status="approximate",
        fit_kind="minimum_residual_explanation",
        reference_parameters=reference,
        reference_basis="minimum_l1_constraint_residual",
        max_reference_violation=diagnostic.get("max_slack"),
        fit_error=_fit_error(diagnostic),
    )
    return candidate


def _compact_candidate(design: Mapping[str, Any], solved: Mapping[str, Any]) -> dict[str, Any]:
    diagnostic = solved.get("diagnostic") or {}
    return {
        "alignment_offset_seconds": design.get("alignment_offset_seconds"),
        "status": solved.get("status"),
        "strict_status": solved.get("strict_status", solved.get("status")),
        "fit_kind": solved.get("fit_kind"),
        "row_count": len(design.get("rows", [])),
        "period_count": int(design.get("alpha_count", 0)),
        "quality_flags": list(design.get("quality_flags", [])),
        "reference_basis": solved.get("reference_basis"),
        "max_reference_violation": solved.get("max_reference_violation"),
        "fit_error": solved.get("fit_error"),
        "diagnostic": {
            "kind": diagnostic.get("kind"),
            "method": diagnostic.get("method"),
            "status": diagnostic.get("status"),
            "slack_sum": diagnostic.get("slack_sum"),
            "max_slack": diagnostic.get("max_slack"),
            "mean_slack": diagnostic.get("mean_slack"),
            "affected_constraint_count": diagnostic.get("affected_constraint_count"),
            "constraint_count": diagnostic.get("constraint_count"),
        } if diagnostic else None,
    }


def _aggregate_parameters(
    models: Sequence[str],
    candidates: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    selected: tuple[Mapping[str, Any], Mapping[str, Any]] | None,
    *,
    range_kind: str,
) -> list[dict[str, Any]]:
    selected_reference = list(selected[1].get("reference_parameters", [])) if selected else []
    values: list[dict[str, Any]] = []
    for model_index, model in enumerate(models):
        channels: dict[str, Any] = {}
        for channel_index, channel in enumerate(CHANNELS):
            index = model_index * len(CHANNELS) + channel_index
            name = f"theta.{model}.{channel}"
            if range_kind == "compatibility_set_union":
                ranges = [solved.get("parameter_bounds", {}).get(name) for _, solved in candidates]
                ranges = [item for item in ranges if isinstance(item, Mapping)]
                lowers = [float(item["lower"]) for item in ranges if item.get("lower") is not None]
                finite_uppers = [float(item["upper"]) for item in ranges if item.get("upper") is not None]
                has_unbounded = any(
                    item.get("upper") is None or item.get("upper_status") == "unbounded"
                    for item in ranges
                )
                lower = min(lowers) if lowers else None
                upper = None if has_unbounded else (max(finite_uppers) if finite_uppers else None)
            else:
                points = []
                for _, solved in candidates:
                    reference = list(solved.get("reference_parameters", []))
                    if index < len(reference) and math.isfinite(float(reference[index])):
                        points.append(float(reference[index]))
                lower = min(points) if points else None
                upper = max(points) if points else None
            channels[channel] = {
                "reference": float(selected_reference[index]) if index < len(selected_reference) else None,
                "lower": lower,
                "upper": upper,
                "range_kind": range_kind,
                "estimate_kind": (
                    "exact_compatible_reference"
                    if range_kind == "compatibility_set_union"
                    else "minimum_residual_explanation"
                ),
            }
        values.append({"model": model, "channels": channels})
    return values


def _projection_design(
    models: Sequence[str],
    candidates: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> dict[str, Any] | None:
    theta_count = len(models) * len(CHANNELS)
    vectors = []
    for _, solved in candidates:
        reference = _finite_reference(solved.get("reference_parameters"), theta_count)
        if reference is not None:
            vectors.append(reference)
    if not vectors:
        return None
    names = [f"theta.{model}.{channel}" for model in models for channel in CHANNELS]
    bounds = [
        [min(vector[index] for vector in vectors), max(vector[index] for vector in vectors)]
        for index in range(theta_count)
    ]
    input_sha256 = stable_id(
        "forecast-v2-m2-alignment-sensitivity",
        [list(models), list(CHANNELS), vectors],
    )
    return {
        "schema_version": 1,
        "algorithm_id": "m2-alignment-sensitivity-box-v1",
        "input_sha256": input_sha256,
        "alignment_offset_seconds": None,
        "models": list(models),
        "channels": list(CHANNELS),
        "theta_parameter_names": names,
        "alpha_parameter_names": [],
        "parameter_names": names,
        "theta_count": theta_count,
        "alpha_count": 0,
        "A_ub": [],
        "b_ub": [],
        "bounds": bounds,
        "range_kind": "alignment_sensitivity_box",
    }


def _point_explanation(
    design: Mapping[str, Any],
    solved: Mapping[str, Any],
    observation_id: Any,
) -> float | None:
    theta_count = int(design.get("theta_count", 0))
    reference = _finite_reference(solved.get("reference_parameters"), theta_count)
    if reference is None:
        return None
    row = next(
        (item for item in design.get("rows", []) if item.get("observation_id") == observation_id),
        None,
    )
    if row is None:
        return None
    features = [float(value) for value in row.get("features", [])]
    if len(features) != theta_count:
        return None
    value = sum(coefficient * feature for coefficient, feature in zip(reference, features))
    return float(value) if math.isfinite(value) else None


def _comparison_points(
    candidate_pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    selected: tuple[Mapping[str, Any], Mapping[str, Any]] | None,
    *,
    range_kind: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    usable = [
        pair for pair in candidate_pairs
        if pair[1].get("status") in ("feasible", "approximate")
    ]
    source_design = selected[0] if selected else (candidate_pairs[0][0] if candidate_pairs else None)
    if not source_design or not source_design.get("rows"):
        return {"status": "insufficient_evidence", "points": [], "current": None}
    latest_cycle = source_design["rows"][-1]["cycle_id"]
    cycle_rows = [row for row in source_design["rows"] if row.get("cycle_id") == latest_cycle]
    collapsed: list[dict[str, Any]] = []
    for row in cycle_rows:
        if collapsed and collapsed[-1]["used_percent"] == row["used_percent"]:
            collapsed[-1] = row
        else:
            collapsed.append(row)
    collapsed = collapsed[-32:]
    anchor = cycle_rows[0]
    anchor_used = float(anchor["used_percent"])

    def cycle_value(change: float | None) -> float | None:
        if change is None:
            return None
        value = anchor_used + float(change)
        return max(0.0, min(100.0, value)) if math.isfinite(value) else None

    points: list[dict[str, Any]] = []
    for row in collapsed:
        bounds: list[Mapping[str, Any]] = []
        explanations = [
            value
            for design, solved in usable
            if (value := _point_explanation(design, solved, row.get("observation_id"))) is not None
        ]
        if range_kind == "compatibility_set_union":
            for design, solved in usable:
                if solved.get("status") != "feasible":
                    continue
                matching = next(
                    (item for item in design.get("rows", []) if item.get("observation_id") == row.get("observation_id")),
                    None,
                )
                if matching is None:
                    continue
                objective = list(matching.get("features", [])) + [0.0] * int(design.get("alpha_count", 0))
                budget = solve_debit_set(
                    design,
                    target=objective,
                    timeout_seconds=timeout_seconds,
                    parameter_bound_names=(),
                ).get("target_budget", {})
                if budget:
                    bounds.append(budget)
            lowers = [float(item["lower"]) for item in bounds if item.get("lower") is not None]
            uppers = [float(item["upper"]) for item in bounds if item.get("upper") is not None]
            unbounded = any(item.get("upper") is None for item in bounds)
            lower = min(lowers) if lowers else None
            upper = None if unbounded else (max(uppers) if uppers else None)
        else:
            lower = min(explanations) if explanations else None
            upper = max(explanations) if explanations else None
        explained = (
            _point_explanation(selected[0], selected[1], row.get("observation_id"))
            if selected
            else None
        )
        actual = float(row["used_percent"]) - float(anchor["used_percent"])
        if range_kind == "compatibility_set_union":
            compatible = lower is not None and lower <= actual + 1.0 and (upper is None or upper >= actual - 1.0)
            point_status = "interval_consistent" if compatible else ("incompatible" if bounds else "not_solved")
        else:
            point_status = "approximate_explanation" if explained is not None else "not_solved"
        points.append({
            "time": row["observed_at"],
            "actual_used_pp": actual,
            "explained_used_pp": explained,
            "fit_residual_pp": actual - explained if explained is not None else None,
            "explained_lower_pp": lower,
            "explained_upper_pp": upper,
            "compatible_lower_pp": lower,
            "compatible_upper_pp": upper,
            "anchor_observed_used_pp": anchor_used,
            "observed_cycle_used_pp": float(row["used_percent"]),
            "explained_cycle_used_pp": cycle_value(explained),
            "explained_cycle_lower_pp": cycle_value(lower),
            "explained_cycle_upper_pp": cycle_value(upper),
            "status": point_status,
        })
    residuals = [abs(float(point["fit_residual_pp"])) for point in points if point.get("fit_residual_pp") is not None]
    aggregate_status = str(selected[1].get("status")) if selected else "insufficient_evidence"
    return {
        "status": aggregate_status,
        "period_start": anchor.get("observed_at"),
        "through": cycle_rows[-1].get("observed_at"),
        "observation_count": len(cycle_rows),
        "range_kind": range_kind,
        "mean_absolute_residual_pp": sum(residuals) / len(residuals) if residuals else None,
        "max_absolute_residual_pp": max(residuals) if residuals else None,
        "points": points,
        "current": points[-1] if points else None,
    }


def build_live_m2(
    db: sqlite3.Connection,
    now: datetime | None = None,
    *,
    history_days: int = 7,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=max(1, int(history_days)))
    try:
        requests = _load_requests(db, start, now)
        raw_observations = _load_observations(db, start, now)
    except sqlite3.Error as error:
        return {
            "schema_version": 1,
            "version": VERSION,
            "status": "unavailable",
            "reason": f"evidence_read_failed:{type(error).__name__}",
            "algorithm_id": ALGORITHM_ID,
        }
    compressed = compress_observations(raw_observations)
    models = sorted({str(row.get("model")) for row in requests if row.get("model") not in (None, "", "unknown")})
    if not requests or not compressed["observations"] or not models:
        return {
            "schema_version": 1,
            "version": VERSION,
            "status": "insufficient_evidence",
            "reason": "requests_observations_or_models_missing",
            "algorithm_id": ALGORITHM_ID,
            "models": models,
            "evidence": {
                "request_count": len(requests),
                "raw_observation_count": len(raw_observations),
                "compressed_observation_count": len(compressed["observations"]),
            },
        }
    if not _has_informative_quota_change(compressed["observations"]):
        return {
            "schema_version": 1,
            "version": VERSION,
            "status": "insufficient_evidence",
            "strict_status": "insufficient_evidence",
            "reason": "same_period_displayed_quota_change_missing",
            "algorithm_id": ALGORITHM_ID,
            "models": models,
            "evidence": {
                "request_count": len(requests),
                "raw_observation_count": compressed["raw_count"],
                "compressed_observation_count": compressed["compressed_count"],
                "period_count": compressed["period_count"],
            },
        }
    snapshot = build_snapshot(requests, compressed["observations"], now, mode="reconstructed")
    candidate_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for offset in ALIGNMENT_OFFSETS_SECONDS:
        design = build_design(
            snapshot.observations,
            snapshot.requests,
            models=models,
            alignment_offset_seconds=offset,
            input_cutoff=snapshot.input_cutoff,
            evidence_mode=snapshot.mode,
            source_snapshot_id=snapshot.source_snapshot_id,
            algorithm_id=ALGORITHM_ID,
            include_request_ids=False,
        )
        strict_solved = solve_debit_set(
            design,
            timeout_seconds=timeout_seconds,
            parameter_bound_names=design.get("theta_parameter_names", []),
        )
        solved = _explanation_candidate(design, strict_solved)
        candidate_pairs.append((design, solved))
    feasible = [pair for pair in candidate_pairs if pair[1].get("status") == "feasible"]
    approximate = [pair for pair in candidate_pairs if pair[1].get("status") == "approximate"]
    if feasible:
        selected = next(
            (pair for pair in feasible if int(pair[0].get("alignment_offset_seconds", 999)) == 0),
            feasible[0],
        )
        usable = feasible
        status = "feasible"
        range_kind = "compatibility_set_union"
    elif approximate:
        selected = min(
            approximate,
            key=lambda pair: (
                float(pair[1].get("fit_error", {}).get("constraint_slack_sum_pp"))
                if pair[1].get("fit_error", {}).get("constraint_slack_sum_pp") is not None
                else math.inf,
                abs(int(pair[0].get("alignment_offset_seconds", 0))),
                int(pair[0].get("alignment_offset_seconds", 0)),
            ),
        )
        usable = approximate
        status = "approximate"
        range_kind = "alignment_sensitivity_box"
    else:
        selected = None
        usable = []
        range_kind = "unavailable"
    strict_statuses = [str(solved.get("strict_status", solved.get("status"))) for _, solved in candidate_pairs]
    statuses = [str(solved.get("status")) for _, solved in candidate_pairs]
    if selected:
        strict_status = str(selected[1].get("strict_status", selected[1].get("status")))
    elif strict_statuses and all(item == "infeasible" for item in strict_statuses):
        strict_status = "infeasible"
    elif "timeout" in strict_statuses:
        strict_status = "timeout"
    elif "numeric_failure" in strict_statuses:
        strict_status = "numeric_failure"
    else:
        strict_status = "insufficient_evidence"
    if feasible:
        status = "feasible"
    elif approximate:
        status = "approximate"
    elif statuses and all(item == "infeasible" for item in statuses):
        status = "infeasible"
    elif "timeout" in statuses:
        status = "timeout"
    elif "numeric_failure" in statuses:
        status = "numeric_failure"
    else:
        status = "insufficient_evidence"
    projection_design = _projection_design(models, usable) if status == "approximate" else None
    m2_id = stable_id(
        "forecast-v2-m2-live",
        [snapshot.snapshot_sha256, statuses, strict_statuses, history_days, VERSION],
    )
    artifact = {
        "schema_version": 1,
        "version": VERSION,
        "m2_id": m2_id,
        "status": status,
        "strict_status": strict_status,
        "fit_kind": (
            "exact_compatibility"
            if status == "feasible"
            else "minimum_residual_explanation" if status == "approximate" else None
        ),
        "algorithm_id": ALGORITHM_ID,
        "generated_at": iso_utc(now),
        "input_cutoff": snapshot.input_cutoff,
        "source_snapshot_id": snapshot.source_snapshot_id,
        "evidence_mode": snapshot.mode,
        "history_days": int(history_days),
        "models": models,
        "channels": list(CHANNELS),
        "evidence": {
            "request_count": len(snapshot.requests),
            "excluded_count": len(snapshot.excluded),
            "raw_observation_count": compressed["raw_count"],
            "compressed_observation_count": compressed["compressed_count"],
            "plateau_count": compressed["plateau_count"],
            "period_count": compressed["period_count"],
            "compression_method": compressed["method"],
        },
        "candidates": [_compact_candidate(design, solved) for design, solved in candidate_pairs],
        "selected_alignment_offset_seconds": int(selected[0]["alignment_offset_seconds"]) if selected else None,
        "model_parameters": _aggregate_parameters(
            models,
            usable,
            selected,
            range_kind=range_kind,
        ),
        "current_cycle": _comparison_points(
            candidate_pairs,
            selected,
            range_kind=range_kind,
            timeout_seconds=timeout_seconds,
        ),
        "fit_error": selected[1].get("fit_error") if selected else None,
        "quote_ready": bool(selected),
        "range_kind": range_kind,
        "not_a_probability_interval": True,
        "internal_billing_verified": False,
        "official_tariff": False,
        "assumptions": [
            {
                "id": "observed_local_activity_principal_explanation_v1",
                "text": "以目标机器记录到的原生请求作为额度变化的主要可观测解释；其他设备活动、采样时序与显示取整进入残差。",
                "unobserved_activity_handling": "fit_residual",
            }
        ],
        "_solver_candidates": [
            {
                "status": solved.get("status"),
                "strict_status": solved.get("strict_status", solved.get("status")),
                "fit_kind": solved.get("fit_kind"),
                "alignment_offset_seconds": design.get("alignment_offset_seconds"),
                "reference_parameters": solved.get("reference_parameters"),
                "fit_error": solved.get("fit_error"),
                "design": _solver_design(design),
            }
            for design, solved in candidate_pairs
        ],
        "_projection_design": projection_design,
    }
    return artifact


def compact_m2(artifact: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in artifact.items() if not str(key).startswith("_")}


def quote_m2(artifact: Mapping[str, Any], items: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    if artifact.get("status") not in ("feasible", "approximate"):
        raise ValueError("M2 当前没有可用解释参数")
    values = list(items or [])
    if not values:
        raise ValueError("items 不能为空")
    models = list(artifact.get("models", []))
    totals = {model: {channel: 0.0 for channel in CHANNELS} for model in models}
    for index, raw in enumerate(values):
        if not isinstance(raw, Mapping):
            raise ValueError(f"items[{index}] 必须是对象")
        model = str(raw.get("model") or "")
        if model not in totals:
            raise ValueError(f"items[{index}].model 不在 M2 参数集合中")
        calls = float(raw.get("calls", 0))
        if not math.isfinite(calls) or calls < 0:
            raise ValueError(f"items[{index}].calls 必须是有限非负数")
        for channel in CHANNELS:
            amount = float(raw.get(channel, 0))
            if not math.isfinite(amount) or amount < 0:
                raise ValueError(f"items[{index}].{channel} 必须是有限非负数")
            totals[model][channel] += calls * amount
    candidates = list(artifact.get("_solver_candidates", []))
    if not candidates:
        parameters = {
            str(item.get("model")): item.get("channels") or {}
            for item in artifact.get("model_parameters", [])
            if isinstance(item, Mapping) and item.get("model")
        }
        estimate = 0.0
        lower = 0.0
        upper = 0.0
        upper_unbounded = False
        for model, channels in totals.items():
            model_parameters = parameters.get(model)
            if not isinstance(model_parameters, Mapping):
                raise ValueError("M2 当前没有可用解释参数")
            for channel, token_count in channels.items():
                if token_count <= 0:
                    continue
                item = model_parameters.get(channel)
                if not isinstance(item, Mapping):
                    raise ValueError("M2 当前没有可用解释参数")
                scale = token_count / 1_000_000.0
                reference = item.get("reference")
                lower_value = item.get("lower")
                if reference is None or lower_value is None:
                    raise ValueError("M2 当前没有可用解释参数")
                estimate += scale * float(reference)
                lower += scale * float(lower_value)
                if item.get("upper") is None:
                    upper_unbounded = True
                else:
                    upper += scale * float(item["upper"])
        return {
            "schema_version": 1,
            "m2_id": artifact.get("m2_id"),
            "estimate_pp": estimate,
            "lower_pp": lower,
            "upper_pp": None if upper_unbounded else upper,
            "fit_status": artifact.get("status"),
            "strict_status": artifact.get("strict_status"),
            "fit_error": artifact.get("fit_error"),
            "range_kind": artifact.get("range_kind"),
            "not_a_probability_interval": True,
            "domain_warnings": [],
        }
    budgets: list[Mapping[str, Any]] = []
    estimate = None
    selected_offset = artifact.get("selected_alignment_offset_seconds")
    if artifact.get("status") == "approximate":
        design = artifact.get("_projection_design") or {}
        if not design:
            raise ValueError("M2 当前没有可用解释参数")
        target = objective_from_model_channels(design, totals)
        solved = solve_debit_set(design, target=target, parameter_bound_names=())
        budget = solved.get("target_budget")
        if isinstance(budget, Mapping):
            budgets.append(budget)
        selected = next(
            (
                candidate for candidate in candidates
                if candidate.get("status") == "approximate"
                and candidate.get("alignment_offset_seconds") == selected_offset
            ),
            None,
        )
        if selected and selected.get("reference_parameters"):
            theta_count = int(design.get("theta_count", 0))
            theta = list(selected["reference_parameters"])[:theta_count]
            estimate = sum(a * b for a, b in zip(theta, target[:theta_count]))
    else:
        feasible_candidates = [candidate for candidate in candidates if candidate.get("status") == "feasible"]
        selected = next(
            (
                candidate for candidate in feasible_candidates
                if candidate.get("alignment_offset_seconds") == selected_offset
            ),
            feasible_candidates[0] if feasible_candidates else None,
        )
        for candidate in feasible_candidates:
            design = candidate.get("design") or {}
            target = objective_from_model_channels(design, totals)
            solved = solve_debit_set(design, target=target, parameter_bound_names=())
            budget = solved.get("target_budget")
            if isinstance(budget, Mapping):
                budgets.append(budget)
            if candidate is selected and candidate.get("reference_parameters"):
                theta = list(candidate["reference_parameters"])[: int(design.get("theta_count", 0))]
                estimate = sum(a * b for a, b in zip(theta, target[: len(theta)]))
    lowers = [float(item["lower"]) for item in budgets if item.get("lower") is not None]
    uppers = [float(item["upper"]) for item in budgets if item.get("upper") is not None]
    if not lowers:
        raise ValueError("M2 无法计算当前预算范围")
    return {
        "schema_version": 1,
        "m2_id": artifact.get("m2_id"),
        "estimate_pp": float(estimate) if estimate is not None else min(lowers),
        "lower_pp": min(lowers),
        "upper_pp": None if any(item.get("upper") is None for item in budgets) else max(uppers),
        "fit_status": artifact.get("status"),
        "strict_status": artifact.get("strict_status"),
        "fit_error": artifact.get("fit_error"),
        "range_kind": artifact.get("range_kind"),
        "not_a_probability_interval": True,
        "domain_warnings": [],
    }
