"""SciPy/HiGHS solving and conservative diagnostics for cumulative designs."""
from __future__ import annotations

from datetime import datetime
from copy import deepcopy
import math
from typing import Any, Mapping, Sequence

import numpy as np
from scipy.optimize import linprog


def _arrays(design: Mapping[str, Any]) -> tuple[np.ndarray, np.ndarray, list[tuple[float | None, float | None]]]:
    a = np.asarray(design.get("A_ub", []), dtype=float)
    b = np.asarray(design.get("b_ub", []), dtype=float)
    if a.size == 0:
        a = np.empty((0, int(design.get("theta_count", 0)) + int(design.get("alpha_count", 0))))
    if a.ndim != 2 or b.ndim != 1 or a.shape[0] != len(b):
        raise ValueError("invalid design matrix")
    bounds = []
    for item in design.get("bounds", []):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("invalid parameter bounds")
        bounds.append((item[0], item[1]))
    if len(bounds) != a.shape[1]:
        raise ValueError("parameter bounds dimension mismatch")
    return a, b, bounds


def _status(result: Any) -> str:
    if result.success:
        return "feasible"
    if result.status == 1:
        return "timeout"
    if result.status == 2:
        return "infeasible"
    if result.status == 3:
        return "unbounded"
    return "numeric_failure"


def _run(
    design: Mapping[str, Any],
    objective: Sequence[float],
    *,
    maximize: bool = False,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    a, b, bounds = _arrays(design)
    c = np.asarray(objective, dtype=float)
    if len(c) != a.shape[1] or not np.all(np.isfinite(c)):
        raise ValueError("objective dimension or values are invalid")
    if maximize:
        c = -c
    try:
        options = {"time_limit": max(0.001, float(timeout_seconds))}
        result = linprog(c, A_ub=a, b_ub=b, bounds=bounds, method="highs", options=options)
    except Exception as error:  # SciPy can raise for malformed/numerically bad inputs.
        return {"status": "numeric_failure", "message": str(error), "solver": "scipy-highs"}
    payload: dict[str, Any] = {
        "status": _status(result),
        "solver": "scipy-highs",
        "solver_status": int(result.status),
        "message": str(result.message),
        "fun": float(result.fun) if result.fun is not None and math.isfinite(float(result.fun)) else None,
        "nit": int(result.nit) if result.nit is not None else None,
    }
    if result.x is not None:
        payload["x"] = [float(x) for x in result.x]
        payload["max_violation"] = max_constraint_violation(design, result.x)
    if maximize and payload.get("status") == "feasible":
        payload["value"] = float(-result.fun)
    elif not maximize and payload.get("status") == "feasible":
        payload["value"] = float(result.fun)
    return payload


def max_constraint_violation(design: Mapping[str, Any], values: Sequence[float]) -> float:
    a, b, bounds = _arrays(design)
    vector = np.asarray(values, dtype=float)
    if len(vector) != a.shape[1] or not np.all(np.isfinite(vector)):
        return float("inf")
    violation = float(np.max(a @ vector - b)) if len(b) else 0.0
    for value, (lower, upper) in zip(vector, bounds):
        if lower is not None:
            violation = max(violation, float(lower) - float(value))
        if upper is not None:
            violation = max(violation, float(value) - float(upper))
    return max(0.0, violation)


def target_vector(design: Mapping[str, Any], theta_values: Sequence[float]) -> list[float]:
    theta_count = int(design.get("theta_count", 0))
    alpha_count = int(design.get("alpha_count", 0))
    values = [float(x) for x in theta_values]
    if len(values) != theta_count:
        raise ValueError("theta target dimension mismatch")
    return values + [0.0] * alpha_count


def objective_from_model_channels(
    design: Mapping[str, Any],
    channel_totals: Mapping[str, Mapping[str, float]] | Sequence[float],
) -> list[float]:
    """Convert future token totals to one joint theta objective."""
    theta_count = int(design.get("theta_count", 0))
    if isinstance(channel_totals, Mapping):
        result: list[float] = []
        channels = list(design.get("channels", ("uncached_input", "cached_input", "output")))
        for model in design.get("models", []):
            values = channel_totals.get(model, {})
            result.extend(float(values.get(channel, 0.0)) / 1_000_000.0 for channel in channels)
    else:
        result = [float(x) for x in channel_totals]
    if len(result) != theta_count:
        raise ValueError("future token objective dimension mismatch")
    return result + [0.0] * int(design.get("alpha_count", 0))


def possible_display_values(lower: float | None, upper: float | None, *, tolerance: float = 1e-7, maximum: int = 1000) -> list[int] | None:
    if lower is None or upper is None or not math.isfinite(float(lower)):
        return None
    lower = float(lower)
    if upper is None or not math.isfinite(float(upper)):
        return None
    if lower > upper + tolerance:
        return []
    start = max(-maximum, math.floor(lower - 0.5 - tolerance) - 1)
    end = min(maximum, math.ceil(upper + 0.5 + tolerance) + 1)
    values = [
        integer for integer in range(start, end + 1)
        if upper >= integer - 0.5 - tolerance and lower <= integer + 0.5 + tolerance
    ]
    return values


def _reference_is_feasible(design: Mapping[str, Any], reference: Sequence[float] | None) -> bool:
    return reference is not None and max_constraint_violation(design, reference) <= 1e-7


def solve_debit_set(
    design: Mapping[str, Any],
    *,
    target: Sequence[float] | None = None,
    previous_reference: Sequence[float] | None = None,
    fixed_theta: Sequence[float] | None = None,
    timeout_seconds: float = 15.0,
    parameter_bound_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Solve feasibility, parameter ranges, and an optional future budget.

    The five public solver states are kept distinct: feasible, infeasible,
    unbounded, timeout and numeric_failure.
    """
    working_design = deepcopy(dict(design))
    if fixed_theta is not None:
        theta_count = int(working_design.get("theta_count", 0))
        values = [float(x) for x in fixed_theta]
        if len(values) != theta_count or not np.all(np.isfinite(values)) or any(x < 0 for x in values):
            raise ValueError("fixed_theta dimension or values are invalid")
        bounds = [list(x) for x in working_design.get("bounds", [])]
        for index, value in enumerate(values):
            bounds[index] = [value, value]
        working_design["bounds"] = bounds
    a, _, _ = _arrays(working_design)
    dimension = a.shape[1]
    feasibility = _run(working_design, [0.0] * dimension, timeout_seconds=timeout_seconds)
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": feasibility["status"],
        "solver": feasibility,
        "algorithm_id": design.get("algorithm_id"),
        "input_sha256": design.get("input_sha256"),
        "alignment_offset_seconds": design.get("alignment_offset_seconds"),
        "models": design.get("models", []),
        "channels": design.get("channels", []),
        "range_kind": "compatibility_set",
        "internal_billing_verified": False,
    }
    if fixed_theta is not None:
        result["fixed_theta"] = [float(x) for x in fixed_theta]
    if feasibility["status"] != "feasible":
        if feasibility["status"] == "infeasible":
            # Slack witnesses are explanatory only.  Keep them bounded so an
            # infeasible live fit cannot consume the entire publish interval.
            result["diagnostic"] = diagnostic_relaxation(
                working_design, timeout_seconds=min(float(timeout_seconds), 2.0)
            )
        return result
    reference = list(previous_reference) if _reference_is_feasible(working_design, previous_reference) else list(feasibility.get("x", []))
    result["reference_parameters"] = reference
    result["reference_basis"] = "previous_validated" if _reference_is_feasible(working_design, previous_reference) else "feasible_solver_point"
    result["max_reference_violation"] = max_constraint_violation(working_design, reference)
    parameter_bounds: dict[str, dict[str, Any]] = {}
    names = list(design.get("parameter_names", []))
    selected = set(str(name) for name in parameter_bound_names) if parameter_bound_names is not None else None
    unknown_names = sorted(selected - set(names)) if selected is not None else []
    if unknown_names:
        raise ValueError(f"unknown parameter bound names: {unknown_names}")
    for index, name in enumerate(names):
        if selected is not None and name not in selected:
            continue
        objective = [0.0] * dimension
        objective[index] = 1.0
        low = _run(working_design, objective, timeout_seconds=timeout_seconds)
        high = _run(working_design, objective, maximize=True, timeout_seconds=timeout_seconds)
        parameter_bounds[name] = {
            "lower": low.get("value") if low["status"] == "feasible" else None,
            "upper": high.get("value") if high["status"] == "feasible" else None,
            "lower_status": low["status"],
            "upper_status": high["status"],
        }
    result["parameter_bounds"] = parameter_bounds
    result["parameter_bounds_scope"] = "all" if selected is None else "selected"
    if target is not None:
        objective = [float(x) for x in target]
        if len(objective) == int(design.get("theta_count", 0)):
            objective = target_vector(design, objective)
        if len(objective) != dimension:
            raise ValueError("target dimension mismatch")
        low = _run(working_design, objective, timeout_seconds=timeout_seconds)
        high = _run(working_design, objective, maximize=True, timeout_seconds=timeout_seconds)
        result["target_budget"] = {
            "lower": low.get("value") if low["status"] == "feasible" else None,
            "upper": high.get("value") if high["status"] == "feasible" else None,
            "lower_status": low["status"],
            "upper_status": high["status"],
            "objective": objective,
        }
    return result


def budget_bounds(solution: Mapping[str, Any], objective: Sequence[float]) -> dict[str, Any]:
    """Solve a new objective against a serialized design/solution pair."""
    design = solution.get("design")
    if not isinstance(design, Mapping):
        raise ValueError("solution does not contain a design")
    target = list(objective)
    if len(target) == int(design.get("theta_count", 0)):
        target = target_vector(design, target)
    if len(target) != len(design.get("parameter_names", [])):
        raise ValueError("objective dimension mismatch")
    return solve_debit_set(design, target=target).get("target_budget", {})


def diagnostic_relaxation(design: Mapping[str, Any], *, timeout_seconds: float = 15.0) -> dict[str, Any]:
    """Find a bounded slack witness; never return it as a certified fit."""
    a, b, bounds = _arrays(design)
    rows = len(b)
    if rows == 0:
        return {"status": "not_needed", "kind": "diagnostic_only"}
    slack_a = np.hstack([a, -np.eye(rows)])
    slack_bounds = bounds + [(0.0, None)] * rows
    diagnostic_design = {
        "A_ub": slack_a.tolist(),
        "b_ub": b.tolist(),
        "bounds": [list(x) for x in slack_bounds],
        "theta_count": a.shape[1],
        "alpha_count": 0,
        "parameter_names": list(design.get("parameter_names", [])) + [f"slack.{i}" for i in range(rows)],
    }
    objective = [0.0] * a.shape[1] + [1.0] * rows
    solved = _run(diagnostic_design, objective, timeout_seconds=timeout_seconds)
    solution = list(solved.get("x", []))
    reference = solution[: a.shape[1]] if len(solution) >= a.shape[1] else None
    slacks = solution[a.shape[1] :] if len(solution) >= a.shape[1] + rows else None
    finite_reference = bool(
        reference is not None
        and len(reference) == a.shape[1]
        and np.all(np.isfinite(reference))
    )
    finite_slacks = [float(value) for value in (slacks or []) if math.isfinite(float(value))]
    affected = [value for value in finite_slacks if value > 1e-7]
    return {
        "kind": "diagnostic_only",
        "method": "minimum_l1_constraint_residual",
        "status": solved["status"],
        "solver": solved,
        "slack_sum": solved.get("value"),
        "slacks": slacks,
        "reference_parameters": reference if finite_reference else None,
        "theta_reference_parameters": (
            reference[: int(design.get("theta_count", 0))]
            if finite_reference
            else None
        ),
        "constraint_count": rows,
        "affected_constraint_count": len(affected),
        "max_slack": max(finite_slacks) if finite_slacks else None,
        "mean_slack": (sum(finite_slacks) / len(finite_slacks)) if finite_slacks else None,
    }
