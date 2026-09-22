"""Deterministic, synthetic data for the public demo mode."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import sin
from typing import Any


UTC = timezone.utc


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _coefficient(estimate: float, lower: float, upper: float) -> dict[str, float]:
    return {"estimate": estimate, "lower": lower, "upper": upper}


def build_demo_snapshot(now: datetime | None = None) -> dict[str, Any]:
    """Build a fresh synthetic snapshot without reading local user data."""

    now = (now or datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
    history_start = now - timedelta(hours=36)
    reset_at = now + timedelta(days=4, hours=9)

    actual: list[dict[str, Any]] = []
    for index in range(73):
        at = history_start + timedelta(minutes=30 * index)
        elapsed = index / 2
        used = 12.0 + elapsed * 0.47 + max(0.0, elapsed - 21) * 0.22 + sin(index / 4) * 0.45
        actual.append({"time": _iso(at), "used_percent": round(min(34.0, used), 2)})

    current_used = actual[-1]["used_percent"]
    points: list[dict[str, Any]] = []
    forecast_hours = int((reset_at - now).total_seconds() // 3600)
    for hour in range(0, forecast_hours + 1, 2):
        at = now + timedelta(hours=hour)
        center = current_used + hour * 0.38
        spread = 1.4 + hour * 0.035
        points.append(
            {
                "time": _iso(at),
                "lower": round(max(current_used, center - spread), 2),
                "median": round(min(96.0, center), 2),
                "upper": round(min(100.0, center + spread), 2),
            }
        )

    minute_history: list[dict[str, Any]] = []
    for index in range(72):
        at = now - timedelta(minutes=(71 - index) * 5)
        minute_history.append(
            {
                "time": _iso(at),
                "rate_pp_minute": round(0.004 + 0.003 * (1 + sin(index / 5)), 4),
                "active_tasks": 1 + (index % 11 in {3, 4, 5}) + (index % 17 == 0),
            }
        )

    thread_distribution = []
    for index, point in enumerate(points):
        sol = max(0.15, 1.25 - index * 0.035)
        luna = max(0.0, 0.75 - index * 0.025)
        thread_distribution.append(
            {
                "time": point["time"],
                "groups": {
                    "gpt-5.6-sol|high": {"expected": round(sol, 3), "lower": 0.0, "upper": round(sol + 0.6, 3)},
                    "gpt-5.6-luna|medium": {"expected": round(luna, 3), "lower": 0.0, "upper": round(luna + 0.4, 3)},
                },
                "expected_total": round(sol + luna, 3),
                "lower_total": 0.0,
                "upper_total": round(sol + luna + 1.0, 3),
            }
        )

    consumption_rows = []
    for index in range(8):
        start = now - timedelta(hours=(index + 1) * 2)
        observed = 0.8 + (index % 3) * 0.1
        estimate = observed + (-0.08 if index % 2 else 0.06)
        consumption_rows.append(
            {
                "start": _iso(start),
                "end": _iso(start + timedelta(hours=2)),
                "token_estimate_pp": round(estimate - 0.04, 3),
                "meter_estimate_pp": round(estimate, 2),
                "observed_pp": observed,
                "phase": {"status": "interval_consistent"},
            }
        )

    models = [
        {
            "model": "example-balanced",
            "active_blocks": 28,
            "exclusive_blocks": 13,
            "status": "budget_supported",
            "coefficients": {
                "uncached_input": _coefficient(0.11, 0.07, 0.16),
                "cached_input": _coefficient(0.03, 0.01, 0.06),
                "output": _coefficient(0.84, 0.58, 1.17),
            },
            "profiles": [
                {
                    "effort": "medium",
                    "tier": "standard",
                    "context": "example",
                    "requests": 184,
                    "average_tokens": {"uncached_input": 2600, "cached_input": 7200, "output": 950},
                }
            ],
        },
        {
            "model": "example-fast",
            "active_blocks": 19,
            "exclusive_blocks": 9,
            "status": "exploratory",
            "coefficients": {
                "uncached_input": _coefficient(0.07, 0.03, 0.12),
                "cached_input": _coefficient(0.02, 0.0, 0.05),
                "output": _coefficient(0.48, 0.24, 0.81),
            },
            "profiles": [],
        },
    ]

    latest = {
        "observed_at": _iso(now),
        "used_percent": current_used,
        "remaining_percent": round(100 - current_used, 2),
        "window_minutes": 10080,
        "plan_type": "demo",
        "source": "synthetic_demo",
        "authority": "demo_only",
        "limit_id": "example:primary",
        "resets_at": _iso(reset_at),
        "epoch_started_at": _iso(now - timedelta(days=3)),
        "history_started_at": _iso(history_start),
    }

    return {
        "schema_version": 1,
        "generated_at": _iso(now),
        "timezone": "UTC",
        "authority": {"kind": "synthetic_demo", "official": False},
        "adaptive": {
            "status": "model_matched",
            "version": "demo-forecast-v1",
            "latest": latest,
            "actual": actual,
            "boundaries": [],
            "forecast": points,
            "valid_history_hours": 31,
            "matched_model_hours": 18,
            "recent_rate_pp_hour": 0.61,
            "pace_budget": {
                "rate_pp_hour": 0.62,
                "hours_to_reset": round((reset_at - now).total_seconds() / 3600, 1),
                "interpretation": "synthetic_demo",
            },
            "scenario": {
                "horizon_end": _iso(reset_at),
                "horizon_limited": False,
                "exhaustion_median_at": None,
                "matched_reference_fraction": 0.72,
            },
            "activity": {
                "recent_active_tasks": 2,
                "open_turns": 3,
                "scope": "synthetic_demo",
            },
            "model_rows": [
                {"model": "example-balanced", "share": 0.68},
                {"model": "example-fast", "share": 0.32},
            ],
        },
        "task_forecast": {
            "status": "tracking",
            "version": "demo-task-forecast-v1",
            "issued_at": _iso(now),
            "input_cutoff": _iso(now - timedelta(minutes=2)),
            "reset_at": _iso(reset_at),
            "current_rate_pp_minute": 0.0087,
            "semantic_weight": 0.25,
            "points": points,
            "minute_history": minute_history,
            "thread_distribution": {
                "status": "ok",
                "coverage": "complete",
                "points": thread_distribution,
                "basis": "synthetic_demo",
            },
            "semantic_tasks": 2,
            "tasks": [
                {
                    "title": "示例任务 A",
                    "model": "example-balanced",
                    "effort": "high",
                    "status": "observed_open",
                    "progress": "实现与验证",
                    "rate_pp_minute": 0.0051,
                    "duration_basis": "observed",
                    "goal_runtime_status": "active",
                },
                {
                    "title": "示例任务 B",
                    "model": "example-fast",
                    "effort": "medium",
                    "status": "observed_open",
                    "progress": "等待外部结果",
                    "rate_pp_minute": 0.0026,
                    "duration_basis": "bounded_estimate",
                    "goal_runtime_status": "waiting_external",
                },
            ],
            "continuing_work": {
                "source": "synthetic_demo",
                "assumption": "Only supported active goals continue.",
                "profiles": [
                    {
                        "title": "示例持续目标",
                        "status": "active",
                        "remaining_minutes": 210,
                        "request_count": 9,
                        "reason": "recent_observed_workflow",
                    }
                ],
            },
            "scheduled": {
                "jobs": [
                    {
                        "name": "示例每日核查",
                        "status": "ACTIVE",
                        "basis": "history",
                        "history_runs": 12,
                        "forecast_runs": 4,
                        "next_runs": [_iso(now + timedelta(hours=6))],
                        "cost_per_run": {"estimate": 0.08, "lower": 0.04, "upper": 0.14},
                    }
                ],
                "issues": [],
            },
            "validation": {
                "windows": 17,
                "horizon_minutes": 30,
                "scores": {"blended": 0.31},
            },
            "warning": "Synthetic data. Not an account forecast.",
        },
        "consumption_explanation": {
            "status": "evaluated",
            "version": "demo-explanation-v1",
            "computed_at": _iso(now),
            "purpose": "synthetic_method_demonstration",
            "warning": "Synthetic validation values.",
            "validation": {
                "token_estimate": {"windows": 24, "mae_pp": 0.34},
                "meter_model": {
                    "windows": 24,
                    "mae_pp": 0.16,
                    "within_0_1_fraction": 0.71,
                    "target_0_1_mae_met": False,
                    "all_within_0_1": False,
                },
            },
            "drift": {
                "status": "monitoring",
                "reference_as_of": _iso(now - timedelta(days=8)),
                "reference_is_proven_normal": False,
                "changes": [],
                "automatic_sample_deletion": False,
            },
            "reset_comparison": {
                "status": "tracking",
                "boundary_after": _iso(now - timedelta(days=3)),
                "current": {
                    "reference_pp": 15.9,
                    "observed_pp": round(current_used - 12.0, 2),
                    "residual_pp": round((current_used - 12.0) - 15.9, 2),
                },
                "points": [
                    {
                        "time": item["time"],
                        "reference_pp": round(index * 0.3, 2),
                        "observed_pp": round(index * 0.31 + sin(index / 3) * 0.18, 2),
                    }
                    for index, item in enumerate(actual[-36:])
                ],
            },
            "rows": consumption_rows,
        },
        "forecast_review": {
            "version": "demo-review-v1",
            "automatic_application": False,
            "issues": 9,
            "evaluations": 7,
            "candidates": [
                {"status": "gated_not_started", "reason": "needs_more_independent_samples"}
            ],
            "recent": [
                {
                    "issued_at": _iso(now - timedelta(hours=10)),
                    "target_at": _iso(now - timedelta(hours=4)),
                    "prediction": {"median": 29.2, "lower": 27.9, "upper": 31.4},
                    "observed": 29.8,
                    "error_pp": -0.6,
                    "absolute_error_pp": 0.6,
                    "inside_envelope": True,
                    "comparable": True,
                    "reasons": [],
                },
                {
                    "issued_at": _iso(now - timedelta(days=1, hours=2)),
                    "target_at": _iso(now - timedelta(hours=20)),
                    "prediction": {"median": 23.7, "lower": 22.1, "upper": 25.9},
                    "observed": 24.5,
                    "error_pp": -0.8,
                    "absolute_error_pp": 0.8,
                    "inside_envelope": True,
                    "comparable": False,
                    "reasons": ["user_work_changed"],
                },
            ],
        },
        "token_calibration": {
            "schema_version": 1,
            "version": "demo-calibration-v1",
            "status": "exploratory",
            "calibration_id": "synthetic-demo-calibration",
            "generated_at": _iso(now),
            "training_blocks": 47,
            "feature_count": 7,
            "matrix_rank": 6,
            "training_start": _iso(now - timedelta(days=24)),
            "training_end": _iso(now - timedelta(hours=3)),
            "unit": "percentage_points_per_million_tokens",
            "official_tariff": False,
            "probability_calibrated": False,
            "models": models,
            "scope": {
                "plan_type": "demo",
                "limit_id": "example:primary",
                "window_minutes": 10080,
            },
            "validation": {
                "windows": 18,
                "scores_mae_pp": {"model_tokens": 0.27, "pooled_channels": 0.42, "request_count": 0.69},
            },
            "alignment_sensitivity": [
                {"alignment_seconds": 0, "tolerance_pp": 0.18},
                {"alignment_seconds": 60, "tolerance_pp": 0.23},
            ],
            "notes": ["All values in demo mode are synthetic."],
        },
    }
