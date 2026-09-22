from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from codex_quota_dashboard.forecast_v2 import live_m2
from codex_quota_dashboard.forecast_v2.live_m2 import build_live_m2, compact_m2, quote_m2
from codex_quota_dashboard.models import iso_utc


UTC = timezone.utc


def _database(now: datetime) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE native_requests (
          response_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL,
          model TEXT NOT NULL, service_tier TEXT NOT NULL,
          reasoning_effort TEXT, input_tokens INTEGER NOT NULL,
          cached_input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
          conflict INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE rate_limit_snapshots (
          snapshot_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL,
          collector_id TEXT NOT NULL, limit_id TEXT NOT NULL,
          used_percent REAL, window_minutes INTEGER, resets_at TEXT,
          plan_type TEXT, source TEXT NOT NULL, authority TEXT NOT NULL
        );
        """
    )
    reset = now + timedelta(days=7)
    db.executemany(
        "INSERT INTO rate_limit_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("o0", iso_utc(now - timedelta(minutes=2)), "test", "codex:primary", 10.0, 10080, iso_utc(reset), "pro", "appserver_account_rate_limits", "server_authoritative"),
            ("o0-repeat", iso_utc(now - timedelta(minutes=1, seconds=30)), "test", "codex:primary", 10.0, 10080, iso_utc(reset), "pro", "appserver_account_rate_limits", "server_authoritative"),
            ("o1", iso_utc(now), "test", "codex:primary", 11.0, 10080, iso_utc(reset), "pro", "appserver_account_rate_limits", "server_authoritative"),
        ],
    )
    db.execute(
        "INSERT INTO native_requests VALUES (?,?,?,?,?,?,?,?,?)",
        ("r1", iso_utc(now - timedelta(minutes=1)), "m", "standard", "high", 1_000_000, 0, 0, 0),
    )
    return db


def _approximate_database(now: datetime) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE native_requests (
          response_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL,
          model TEXT NOT NULL, service_tier TEXT NOT NULL,
          reasoning_effort TEXT, input_tokens INTEGER NOT NULL,
          cached_input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
          conflict INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE rate_limit_snapshots (
          snapshot_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL,
          collector_id TEXT NOT NULL, limit_id TEXT NOT NULL,
          used_percent REAL, window_minutes INTEGER, resets_at TEXT,
          plan_type TEXT, source TEXT NOT NULL, authority TEXT NOT NULL
        );
        """
    )
    historical_start = now - timedelta(days=2)
    current_start = now - timedelta(minutes=10)
    db.executemany(
        "INSERT INTO rate_limit_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("h0", iso_utc(historical_start), "test", "codex:primary", 10.0, 10080, iso_utc(now - timedelta(days=1)), "pro", "appserver_account_rate_limits", "server_authoritative"),
            ("h1", iso_utc(historical_start + timedelta(minutes=10)), "test", "codex:primary", 12.0, 10080, iso_utc(now - timedelta(days=1)), "pro", "appserver_account_rate_limits", "server_authoritative"),
            ("c0", iso_utc(current_start), "test", "codex:primary", 20.0, 10080, iso_utc(now + timedelta(days=7)), "pro", "appserver_account_rate_limits", "server_authoritative"),
            ("c1", iso_utc(now), "test", "codex:primary", 23.0, 10080, iso_utc(now + timedelta(days=7)), "pro", "appserver_account_rate_limits", "server_authoritative"),
        ],
    )
    db.execute(
        "INSERT INTO native_requests VALUES (?,?,?,?,?,?,?,?,?)",
        ("r-history", iso_utc(historical_start + timedelta(minutes=5)), "m", "standard", "high", 1_000_000, 0, 0, 0),
    )
    return db


def test_live_m2_reads_native_evidence_and_quotes_without_legacy_calibration() -> None:
    now = datetime(2026, 9, 22, 12, tzinfo=UTC)
    artifact = build_live_m2(_database(now), now, history_days=7, timeout_seconds=2)
    assert artifact["status"] == "feasible"
    assert artifact["quote_ready"] is True
    assert artifact["evidence"]["raw_observation_count"] == 3
    assert artifact["evidence"]["compressed_observation_count"] == 3
    assert artifact["current_cycle"]["current"]["actual_used_pp"] == pytest.approx(1.0)
    quote = quote_m2(artifact, [{"model": "m", "calls": 1, "uncached_input": 1_000_000, "cached_input": 0, "output": 0}])
    assert quote["m2_id"] == artifact["m2_id"]
    assert quote["lower_pp"] <= 1.0
    assert quote["upper_pp"] is None or quote["upper_pp"] >= 1.0
    assert "_solver_candidates" not in compact_m2(artifact)


def test_live_m2_does_not_fall_back_when_evidence_is_missing() -> None:
    now = datetime(2026, 9, 22, 12, tzinfo=UTC)
    db = _database(now)
    db.execute("DELETE FROM native_requests")
    artifact = build_live_m2(db, now)
    assert artifact["status"] == "insufficient_evidence"
    with pytest.raises(ValueError, match="M2 当前没有可用解释参数"):
        quote_m2(artifact, [{"model": "m", "calls": 1}])


def test_live_m2_promotes_finite_minimum_residual_explanation() -> None:
    now = datetime(2026, 9, 22, 12, tzinfo=UTC)
    artifact = build_live_m2(_approximate_database(now), now, history_days=7, timeout_seconds=2)

    assert artifact["status"] == "approximate"
    assert artifact["strict_status"] == "infeasible"
    assert artifact["fit_kind"] == "minimum_residual_explanation"
    assert artifact["range_kind"] == "alignment_sensitivity_box"
    assert artifact["quote_ready"] is True
    assert artifact["fit_error"]["constraint_slack_sum_pp"] > 0
    assert all(candidate["status"] == "approximate" for candidate in artifact["candidates"])
    parameter = artifact["model_parameters"][0]["channels"]["uncached_input"]
    assert parameter["reference"] is not None
    assert parameter["lower"] <= parameter["reference"] <= parameter["upper"]
    current = artifact["current_cycle"]["current"]
    assert current["status"] == "approximate_explanation"
    assert current["explained_used_pp"] == pytest.approx(0.0)
    assert current["fit_residual_pp"] == pytest.approx(3.0)

    quote = quote_m2(
        artifact,
        [{"model": "m", "calls": 1, "uncached_input": 1_000_000, "cached_input": 0, "output": 0}],
    )
    assert quote["fit_status"] == "approximate"
    assert quote["strict_status"] == "infeasible"
    assert quote["range_kind"] == "alignment_sensitivity_box"
    assert quote["lower_pp"] <= quote["estimate_pp"] <= quote["upper_pp"]
    assert "_projection_design" not in compact_m2(artifact)


def test_live_m2_strict_status_describes_selected_explanation_when_other_alignment_times_out(monkeypatch) -> None:
    now = datetime(2026, 9, 22, 12, tzinfo=UTC)

    def solve(design, **_kwargs):
        dimension = int(design["theta_count"]) + int(design["alpha_count"])
        if design["alignment_offset_seconds"] == -120:
            reference = [0.0] * dimension
            return {
                "status": "infeasible",
                "diagnostic": {
                    "status": "feasible",
                    "method": "minimum_l1_constraint_residual",
                    "reference_parameters": reference,
                    "slack_sum": 1.0,
                    "max_slack": 0.5,
                    "mean_slack": 0.25,
                    "affected_constraint_count": 2,
                    "constraint_count": 4,
                },
            }
        return {"status": "timeout"}

    monkeypatch.setattr(live_m2, "solve_debit_set", solve)
    artifact = build_live_m2(_database(now), now, history_days=7, timeout_seconds=2)

    assert artifact["status"] == "approximate"
    assert artifact["selected_alignment_offset_seconds"] == -120
    assert artifact["strict_status"] == "infeasible"
