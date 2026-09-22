from datetime import datetime, timedelta, timezone
import json
import sqlite3

from codex_quota_dashboard.forecast_v2.live_m3 import build_live_m3
from codex_quota_dashboard.models import iso_utc


NOW = datetime(2026, 9, 22, 6, tzinfo=timezone.utc)


def database() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        """
        CREATE TABLE native_requests (
          response_id TEXT PRIMARY KEY, occurred_at TEXT, model TEXT,
          service_tier TEXT, reasoning_effort TEXT, input_tokens INTEGER,
          cached_input_tokens INTEGER, output_tokens INTEGER, conflict INTEGER
        )
        """
    )
    db.execute(
        "INSERT INTO native_requests VALUES (?,?,?,?,?,?,?,?,0)",
        (
            "r1",
            iso_utc(NOW - timedelta(minutes=15)),
            "gpt-test",
            "standard",
            "high",
            12_000,
            2_000,
            1_000,
        ),
    )
    return db


def bootstrap(tmp_path):
    path = tmp_path / "bootstrap.json"
    path.write_text(
        json.dumps(
            {
                "schema": "quota-forecast-v2-bootstrap-reference-v1",
                "reference_id": "safe-start-v1",
                "unit": "percentage_points_per_million_tokens",
                "source_category": "deidentified_fitted_aggregate",
                "privacy": "no_raw_observations_or_identifiers",
                "models": {
                    "gpt-test": {
                        "uncached_input": {"reference": 1.0, "lower": 0.5, "upper": 2.0},
                        "cached_input": {"reference": 0.2, "lower": 0.0, "upper": 0.5},
                        "output": {"reference": 3.0, "lower": 1.0, "upper": 5.0},
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def latest():
    return {"used_percent": 20.0, "resets_at": iso_utc(NOW + timedelta(hours=3))}


def test_bootstrap_is_explicit_primary_reference(tmp_path):
    db = database()
    result = build_live_m3(
        db,
        {"status": "infeasible", "m2_id": "m2-local"},
        latest(),
        bootstrap_reference_path=bootstrap(tmp_path),
        now=NOW,
        lookback_minutes=120,
        path_count=1,
        bucket_seconds=900,
    )
    assert result["status"] == "conditional"
    assert result["reference_source"] == "bootstrap_reference"
    assert result["reference_id"] == "safe-start-v1"
    assert result["m2_status"] == "infeasible"
    assert result["points"]
    assert result["points"][0]["compatibility_used_pp"]["lower_used_pp"] is not None


def test_missing_bootstrap_does_not_fall_back_to_legacy():
    result = build_live_m3(
        database(),
        {"status": "infeasible", "m2_id": "m2-local"},
        latest(),
        bootstrap_reference_path=None,
        now=NOW,
    )
    assert result["status"] == "insufficient_evidence"
    assert result["reason"] == "m2_not_usable_and_bootstrap_not_enabled"


def test_feasible_local_m2_takes_precedence_over_bootstrap(tmp_path):
    names = [
        "theta.gpt-test.uncached_input",
        "theta.gpt-test.cached_input",
        "theta.gpt-test.output",
    ]
    design = {
        "algorithm_id": "cumulative-debit-v1",
        "input_sha256": "a" * 64,
        "alignment_offset_seconds": 0,
        "models": ["gpt-test"],
        "channels": ["uncached_input", "cached_input", "output"],
        "theta_parameter_names": names,
        "alpha_parameter_names": [],
        "parameter_names": names,
        "theta_count": 3,
        "alpha_count": 0,
        "A_ub": [],
        "b_ub": [],
        "bounds": [[0.5, 2.0], [0.0, 0.5], [1.0, 5.0]],
    }
    m2 = {
        "status": "feasible",
        "m2_id": "m2-feasible",
        "models": ["gpt-test"],
        "selected_alignment_offset_seconds": 0,
        "_solver_candidates": [
            {
                "status": "feasible",
                "alignment_offset_seconds": 0,
                "reference_parameters": [1.0, 0.2, 3.0],
                "design": design,
            }
        ],
    }
    result = build_live_m3(
        database(),
        m2,
        latest(),
        bootstrap_reference_path=bootstrap(tmp_path),
        now=NOW,
        bucket_seconds=900,
    )
    assert result["status"] == "conditional"
    assert result["reference_source"] == "local_m2_explanation"
    assert result["reference_id"] == "m2-feasible"
    assert "bootstrap_reference" not in result["quality_flags"]


def test_approximate_local_m2_drives_m3_without_bootstrap(tmp_path):
    names = [
        "theta.gpt-test.uncached_input",
        "theta.gpt-test.cached_input",
        "theta.gpt-test.output",
    ]
    source_design = {
        "algorithm_id": "cumulative-debit-explanation-v1",
        "input_sha256": "b" * 64,
        "alignment_offset_seconds": 0,
        "models": ["gpt-test"],
        "channels": ["uncached_input", "cached_input", "output"],
        "theta_parameter_names": names,
        "alpha_parameter_names": ["cycle"],
        "parameter_names": names + ["alpha.cycle"],
        "theta_count": 3,
        "alpha_count": 1,
        "A_ub": [],
        "b_ub": [],
        "bounds": [[0.0, None], [0.0, None], [0.0, None], [0.0, 100.5]],
    }
    projection_design = {
        "algorithm_id": "m2-alignment-sensitivity-box-v1",
        "input_sha256": "c" * 64,
        "alignment_offset_seconds": None,
        "models": ["gpt-test"],
        "channels": ["uncached_input", "cached_input", "output"],
        "theta_parameter_names": names,
        "alpha_parameter_names": [],
        "parameter_names": names,
        "theta_count": 3,
        "alpha_count": 0,
        "A_ub": [],
        "b_ub": [],
        "bounds": [[0.8, 1.2], [0.1, 0.3], [2.0, 4.0]],
        "range_kind": "alignment_sensitivity_box",
    }
    m2 = {
        "status": "approximate",
        "strict_status": "infeasible",
        "fit_kind": "minimum_residual_explanation",
        "range_kind": "alignment_sensitivity_box",
        "m2_id": "m2-approximate",
        "models": ["gpt-test"],
        "selected_alignment_offset_seconds": 0,
        "fit_error": {"constraint_slack_sum_pp": 0.2},
        "_projection_design": projection_design,
        "_solver_candidates": [
            {
                "status": "approximate",
                "strict_status": "infeasible",
                "alignment_offset_seconds": 0,
                "reference_parameters": [1.0, 0.2, 3.0, 10.0],
                "fit_error": {"constraint_slack_sum_pp": 0.2},
                "design": source_design,
            }
        ],
    }

    result = build_live_m3(
        database(),
        m2,
        latest(),
        bootstrap_reference_path=bootstrap(tmp_path),
        now=NOW,
        bucket_seconds=900,
    )

    assert result["status"] == "conditional"
    assert result["reference_source"] == "local_m2_explanation"
    assert result["reference_id"] == "m2-approximate"
    assert result["reference_metadata"]["fit_status"] == "approximate"
    assert result["reference_metadata"]["strict_status"] == "infeasible"
    assert "approximate_m2_explanation" in result["quality_flags"]
    assert "bootstrap_reference" not in result["quality_flags"]
    assert result["points"][0]["compatibility_used_pp"]["range_kind"] == "alignment_sensitivity_box"
