from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import closing
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from urllib.request import Request, urlopen
import json
import threading

import pytest

from codex_quota_dashboard.collector import collect_once
from codex_quota_dashboard.cli import main as cli_main
from codex_quota_dashboard.config import (
    BootstrapReferenceConfig,
    LocalFittingConfig,
    MonitoringConfig,
    StateConfig,
    SystemConfig,
    WebConfig,
    load_config,
)
from codex_quota_dashboard.forecast_v2.live_m3 import _bootstrap
from codex_quota_dashboard.integration import (
    STATIC_ASSETS,
    copy_static_assets,
    static_asset_manifest,
    static_asset_root,
)
from codex_quota_dashboard.models import RateLimitSnapshot, iso_utc
from codex_quota_dashboard.server import create_server
from codex_quota_dashboard.store import add_rate_limits, connect
from codex_quota_dashboard import system as system_module
from codex_quota_dashboard.system import bootstrap_path, freeze_snapshot


UTC = timezone.utc


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_collection_and_fitting_are_disabled_by_default(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, 'timezone = "UTC"\n'))
    assert config.monitoring.enabled is False
    assert config.local_fitting.enabled is False
    assert config.bootstrap_reference.mode == "off"
    assert config.bootstrap_reference.capacity_multiplier == 1.0
    assert config.state.directory is None


def test_local_fitting_requires_explicit_monitoring_and_state(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="monitoring.enabled=true"):
        load_config(
            write_config(
                tmp_path,
                """
[state]
directory = "./state"
[local_fitting]
enabled = true
""",
            )
        )


def test_bundled_reference_matches_manifest_and_loader() -> None:
    config = SystemConfig(bootstrap_reference=BootstrapReferenceConfig(mode="bundled"))
    path = bootstrap_path(config)
    assert path is not None
    manifest_path = Path(str(files("codex_quota_dashboard").joinpath("data/bootstrap-reference-v1.manifest.json")))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert sha256(path.read_bytes()).hexdigest() == manifest["sha256"]
    loaded = _bootstrap(path)
    assert loaded["source"] == "bootstrap_reference"
    assert loaded["metadata"]["base_reference_id"] == manifest["reference_id"]
    assert loaded["metadata"]["reference_capacity_multiplier"] == 1.0
    assert loaded["metadata"]["target_capacity_multiplier"] == 1.0
    assert loaded["reference_id"].startswith(manifest["reference_id"] + ":capacity:1:")
    assert set(loaded["models"]) == {"gpt-5.6-luna", "gpt-5.6-sol", "gpt-6-astra"}


def test_public_integration_copies_identical_static_assets(tmp_path: Path) -> None:
    expected = static_asset_manifest()
    copied = copy_static_assets(tmp_path / "static")
    assert copied == expected
    for relative, digest in expected["files"].items():
        assert sha256((tmp_path / "static" / relative).read_bytes()).hexdigest() == digest


def test_distributed_resources_have_portable_lf_bytes() -> None:
    root = static_asset_root()
    resources = [root.joinpath(relative) for relative in STATIC_ASSETS]
    resources.extend(
        files("codex_quota_dashboard").joinpath("data", name)
        for name in (
            "bootstrap-reference-v1.json",
            "bootstrap-reference-v1.manifest.json",
        )
    )
    for resource in resources:
        assert b"\r\n" not in resource.read_bytes(), str(resource)


def test_explicit_jsonl_collection_is_incremental(tmp_path: Path) -> None:
    source = tmp_path / "sessions"
    source.mkdir()
    rollout = source / "rollout.jsonl"
    rows = [
        {"timestamp": "2026-09-22T00:00:00Z", "type": "session_meta", "payload": {"id": "session-test"}},
        {"timestamp": "2026-09-22T00:00:01Z", "type": "turn_context", "payload": {"turn_id": "turn-test", "model": "gpt-test", "service_tier": "standard", "effort": "high"}},
        {"timestamp": "2026-09-22T00:00:02Z", "type": "token_usage_record", "payload": {"thread_id": "session-test", "turn_id": "turn-test", "response_id": "response-test", "usage": {"input_tokens": 1000, "cached_input_tokens": 500, "cache_write_input_tokens": 0, "output_tokens": 100, "reasoning_output_tokens": 25, "total_tokens": 1100}}},
    ]
    rollout.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    config = SystemConfig(
        state=StateConfig(directory=tmp_path / "state", retention_days=35),
        monitoring=MonitoringConfig(enabled=True, sources=[source], collect_rate_limits=False),
    )
    first = collect_once(config)
    second = collect_once(config)
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    with closing(connect(config.database_path, create=False)) as db:
        assert db.execute("SELECT COUNT(*) FROM native_requests").fetchone()[0] == 1


def test_rate_limit_write_normalizes_reset_datetime_and_is_idempotent(tmp_path: Path) -> None:
    observed_at = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    resets_at = observed_at + timedelta(days=7)
    snapshot = RateLimitSnapshot(
        observed_at=observed_at,
        collector_id="test-collector",
        limit_id="codex:primary",
        limit_name="primary",
        used_percent=34.0,
        window_minutes=10080,
        resets_at=resets_at,
        plan_type="pro",
        reached_type=None,
        source="appserver_account_rate_limits",
    )
    with closing(connect(tmp_path / "state" / "evidence.sqlite")) as db:
        assert add_rate_limits(db, [snapshot]) == 1
        assert add_rate_limits(db, [snapshot]) == 0
        row = db.execute(
            "SELECT snapshot_id,resets_at FROM rate_limit_snapshots"
        ).fetchone()
        assert row["snapshot_id"] == snapshot.snapshot_id
        assert row["resets_at"] == iso_utc(resets_at)


def test_single_quota_observation_stays_on_explicit_bootstrap_reference(tmp_path: Path) -> None:
    now = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    config = SystemConfig(
        timezone="UTC",
        state=StateConfig(directory=tmp_path / "state", retention_days=35),
        bootstrap_reference=BootstrapReferenceConfig(mode="bundled"),
        monitoring=MonitoringConfig(enabled=True, sources=[]),
        local_fitting=LocalFittingConfig(enabled=True, history_days=7, solver_timeout_seconds=2),
    )
    quota = RateLimitSnapshot(
        observed_at=now,
        collector_id="test-collector",
        limit_id="codex:primary",
        limit_name="primary",
        used_percent=34.0,
        window_minutes=10080,
        resets_at=now + timedelta(days=7),
        plan_type="pro",
        reached_type=None,
        source="appserver_account_rate_limits",
    )
    with closing(connect(config.database_path)) as db:
        assert add_rate_limits(db, [quota]) == 1
        db.execute(
            """INSERT INTO native_requests
            (response_id,session_id,turn_id,occurred_at,coverage_start,model,service_tier,
             reasoning_effort,input_tokens,cached_input_tokens,cache_write_input_tokens,
             output_tokens,reasoning_output_tokens,total_tokens,conflict)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (
                "response-cold-start", "session-cold-start", "turn-cold-start",
                iso_utc(now - timedelta(minutes=1)), iso_utc(now - timedelta(minutes=1)),
                "gpt-5.6-sol", "standard", "high", 1_000_000, 0, 0, 0, 0, 1_000_000,
            ),
        )
        db.commit()
    snapshot = freeze_snapshot(config, now)
    assert snapshot["forecast_v2"]["m2"]["status"] == "insufficient_evidence"
    assert snapshot["forecast_v2"]["m2"]["reason"] == "same_period_displayed_quota_change_missing"
    assert snapshot["forecast_v2"]["m3"]["reference_source"] == "bootstrap_reference"
    assert snapshot["system_state"] == "reference_only"


def test_full_snapshot_prefers_finite_local_m2(tmp_path: Path) -> None:
    now = datetime(2026, 9, 22, 12, tzinfo=UTC)
    source = tmp_path / "selected.jsonl"
    source.write_text("", encoding="utf-8")
    config = SystemConfig(
        timezone="UTC",
        state=StateConfig(directory=tmp_path / "state", retention_days=35),
        bootstrap_reference=BootstrapReferenceConfig(mode="bundled"),
        monitoring=MonitoringConfig(enabled=True, sources=[source]),
        local_fitting=LocalFittingConfig(enabled=True, history_days=7, solver_timeout_seconds=2),
        web=WebConfig(port=0),
    )
    with closing(connect(config.database_path)) as db:
        reset = now + timedelta(days=7)
        observations = [
            ("o0", now - timedelta(minutes=2), 10.0),
            ("o0-repeat", now - timedelta(minutes=1, seconds=30), 10.0),
            ("o1", now, 11.0),
        ]
        db.executemany(
            """INSERT INTO rate_limit_snapshots
            (snapshot_id,observed_at,collector_id,limit_id,limit_name,used_percent,
             window_minutes,resets_at,plan_type,reached_type,source,authority,extra_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (key, iso_utc(stamp), "test", "codex:primary", "primary", used, 10080,
                 iso_utc(reset), "pro", None, "appserver_account_rate_limits",
                 "server_authoritative", "{}")
                for key, stamp, used in observations
            ],
        )
        db.execute(
            """INSERT INTO native_requests
            (response_id,session_id,turn_id,occurred_at,coverage_start,model,service_tier,
             reasoning_effort,input_tokens,cached_input_tokens,cache_write_input_tokens,
             output_tokens,reasoning_output_tokens,total_tokens,conflict)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            ("private-response-sentinel", "private-session-sentinel", "private-turn-sentinel", iso_utc(now - timedelta(minutes=1)),
             iso_utc(now - timedelta(minutes=1)), "gpt-5.6-sol", "standard", "high",
             1_000_000, 0, 0, 0, 0, 1_000_000),
        )
        db.commit()
    snapshot = freeze_snapshot(config, now)
    assert snapshot["forecast_v2"]["m2"]["status"] == "feasible"
    assert snapshot["forecast_v2"]["m3"]["reference_source"] == "local_m2_explanation"
    assert snapshot["system_state"] == "locally_validated"
    current = snapshot["forecast_v2"]["m2"]["current_cycle"]["current"]
    assert current["anchor_observed_used_pp"] == pytest.approx(10.0)
    assert current["observed_cycle_used_pp"] == pytest.approx(11.0)
    assert current["explained_cycle_used_pp"] == pytest.approx(
        10.0 + current["explained_used_pp"]
    )
    assert set(snapshot["forecast_v2"]) == {"m1", "m2", "m3"}
    assert "task_forecast" not in snapshot
    assert "_solver_candidates" not in snapshot["forecast_v2"]["m2"]
    serialized = json.dumps(snapshot)
    assert "private-response-sentinel" not in serialized
    assert "private-session-sentinel" not in serialized
    assert "private-turn-sentinel" not in serialized


def test_http_server_exposes_only_frozen_projection(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot = {
        "schema": "codex-quota-system-snapshot-v1",
        "schema_version": 1,
        "snapshot_id": "snapshot-test",
        "generated_at": "2026-09-22T00:00:00Z",
        "system_state": "reference_only",
        "adaptive": {"actual": [], "boundaries": []},
        "forecast_v2": {
            "m1": {},
            "m2": {
                "status": "approximate",
                "strict_status": "infeasible",
                "m2_id": "m2-http-test",
                "quote_ready": True,
                "models": ["gpt-test"],
                "model_parameters": [{
                    "model": "gpt-test",
                    "channels": {
                        "uncached_input": {"reference": 1.0, "lower": 0.5, "upper": 2.0},
                        "cached_input": {"reference": 0.2, "lower": 0.0, "upper": 0.5},
                        "output": {"reference": 3.0, "lower": 1.0, "upper": 5.0},
                    },
                }],
            },
            "m3": {},
        },
    }
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    server = create_server("127.0.0.1", 0, snapshot_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        root = f"http://127.0.0.1:{server.server_port}"
        health = json.loads(urlopen(root + "/healthz").read())
        dashboard = json.loads(urlopen(root + "/api/dashboard").read())
        assert health["forecast_system"] == "m1-m2-m3"
        assert dashboard["mode"] == "local_system"
        assert dashboard["snapshot"]["snapshot_id"] == "snapshot-test"
        request = Request(
            root + "/api/forecast-v2/m2/quote",
            data=json.dumps({"items": [{"model": "gpt-test", "calls": 1, "uncached_input": 1_000_000, "cached_input": 0, "output": 0}]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        quote = json.loads(urlopen(request).read())
        assert quote["m2_id"] == "m2-http-test"
        assert quote["estimate_pp"] == pytest.approx(1.0)
        assert quote["lower_pp"] == pytest.approx(0.5)
        assert quote["upper_pp"] == pytest.approx(2.0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_explicit_lifecycle_reference_fit_restart_disable_and_purge(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    config_dir = tmp_path / "config"
    input_dir = tmp_path / "input"
    config_dir.mkdir()
    input_dir.mkdir()
    source = input_dir / "rollout.jsonl"
    rows = [
        {"timestamp": iso_utc(now - timedelta(minutes=1, seconds=2)), "type": "session_meta", "payload": {"id": "session-lifecycle"}},
        {"timestamp": iso_utc(now - timedelta(minutes=1, seconds=1)), "type": "turn_context", "payload": {"turn_id": "turn-lifecycle", "model": "gpt-5.6-sol", "service_tier": "standard", "effort": "high"}},
        {"timestamp": iso_utc(now - timedelta(minutes=1)), "type": "token_usage_record", "payload": {"thread_id": "session-lifecycle", "turn_id": "turn-lifecycle", "response_id": "response-lifecycle", "usage": {"input_tokens": 1_000_000, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 1_000_000}}},
    ]
    source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    config_path = config_dir / "config.toml"

    def write_runtime(*, monitoring: bool, fitting: bool, bootstrap: str) -> None:
        config_path.write_text(
            f"""
timezone = "UTC"
[state]
directory = "../state"
retention_days = 35
[bootstrap_reference]
mode = "{bootstrap}"
[monitoring]
enabled = {str(monitoring).lower()}
sources = ["{source.as_posix()}"]
collect_rate_limits = false
poll_seconds = 60
[local_fitting]
enabled = {str(fitting).lower()}
history_days = 7
solver_timeout_seconds = 2
refresh_seconds = 300
[web]
host = "127.0.0.1"
port = 0
""",
            encoding="utf-8",
        )

    write_runtime(monitoring=False, fitting=False, bootstrap="off")
    empty_snapshot = freeze_snapshot(load_config(config_path), now - timedelta(minutes=3))
    assert empty_snapshot["system_state"] == "empty_history"
    assert empty_snapshot["forecast_v2"]["m3"]["status"] == "insufficient_evidence"

    write_runtime(monitoring=True, fitting=False, bootstrap="bundled")
    collecting = load_config(config_path)
    assert collect_once(collecting)["inserted"] == 1
    with closing(connect(collecting.database_path)) as db:
        reset = now + timedelta(days=7)
        observations = [
            ("lifecycle-o0", now - timedelta(minutes=2), 10.0),
            ("lifecycle-o0-repeat", now - timedelta(minutes=1, seconds=30), 10.0),
            ("lifecycle-o1", now, 11.0),
        ]
        db.executemany(
            """INSERT INTO rate_limit_snapshots
            (snapshot_id,observed_at,collector_id,limit_id,limit_name,used_percent,
             window_minutes,resets_at,plan_type,reached_type,source,authority,extra_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    key,
                    iso_utc(stamp),
                    "lifecycle",
                    "codex:primary",
                    "primary",
                    used,
                    10080,
                    iso_utc(reset),
                    "test",
                    None,
                    "appserver_account_rate_limits",
                    "server_authoritative",
                    "{}",
                )
                for key, stamp, used in observations
            ],
        )
        db.commit()

    write_runtime(monitoring=False, fitting=False, bootstrap="bundled")
    reference_snapshot = freeze_snapshot(load_config(config_path), now)
    assert reference_snapshot["system_state"] == "reference_only"
    assert reference_snapshot["forecast_v2"]["m2"]["status"] == "disabled"
    assert reference_snapshot["forecast_v2"]["m3"]["reference_source"] == "bootstrap_reference"

    write_runtime(monitoring=True, fitting=True, bootstrap="bundled")
    fitted_config = load_config(config_path)
    assert collect_once(fitted_config)["inserted"] == 0
    first_fit = freeze_snapshot(fitted_config, now)
    assert first_fit["system_state"] == "locally_validated"
    assert first_fit["forecast_v2"]["m3"]["reference_source"] == "local_m2_explanation"

    extra_rows = [
        {"timestamp": iso_utc(now + timedelta(seconds=28)), "type": "turn_context", "payload": {"turn_id": "turn-incremental", "model": "gpt-5.6-sol", "service_tier": "standard", "effort": "high"}},
        {"timestamp": iso_utc(now + timedelta(seconds=30)), "type": "token_usage_record", "payload": {"thread_id": "session-lifecycle", "turn_id": "turn-incremental", "response_id": "response-incremental", "usage": {"input_tokens": 500_000, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 500_000}}},
    ]
    with source.open("a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(row) + "\n" for row in extra_rows))
    assert collect_once(fitted_config)["inserted"] == 1
    with closing(connect(fitted_config.database_path)) as db:
        db.execute(
            """INSERT INTO rate_limit_snapshots
            (snapshot_id,observed_at,collector_id,limit_id,limit_name,used_percent,
             window_minutes,resets_at,plan_type,reached_type,source,authority,extra_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "lifecycle-o2",
                iso_utc(now + timedelta(minutes=1)),
                "lifecycle",
                "codex:primary",
                "primary",
                11.5,
                10080,
                iso_utc(now + timedelta(days=7)),
                "test",
                None,
                "appserver_account_rate_limits",
                "server_authoritative",
                "{}",
            ),
        )
        db.commit()
    incremental = freeze_snapshot(fitted_config, now + timedelta(minutes=1))
    assert incremental["system_state"] == "locally_validated"
    assert incremental["forecast_v2"]["m2"]["status"] in {"feasible", "approximate"}

    restarted = freeze_snapshot(load_config(config_path), now + timedelta(minutes=1, seconds=1))
    assert restarted["system_state"] == "locally_validated"
    assert restarted["forecast_v2"]["m2"]["status"] in {"feasible", "approximate"}

    write_runtime(monitoring=False, fitting=False, bootstrap="off")
    disabled = freeze_snapshot(load_config(config_path), now + timedelta(seconds=2))
    assert disabled["runtime"]["monitoring_enabled"] is False
    assert disabled["runtime"]["local_fitting_enabled"] is False
    assert disabled["forecast_v2"]["m2"]["status"] == "disabled"

    assert cli_main(["--config", str(config_path), "purge-local-state", "--yes"]) == 0
    assert not (tmp_path / "state").exists()
    assert '"status": "deleted"' in capsys.readouterr().out


def test_failed_refresh_preserves_last_frozen_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SystemConfig(state=StateConfig(directory=tmp_path / "state"))
    freeze_snapshot(config, datetime(2026, 9, 22, 12, tzinfo=UTC))
    assert config.snapshot_path is not None
    frozen = config.snapshot_path.read_bytes()

    def fail_build(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("synthetic refresh failure")

    monkeypatch.setattr(system_module, "build_snapshot", fail_build)
    with pytest.raises(RuntimeError, match="synthetic refresh failure"):
        freeze_snapshot(config, datetime(2026, 9, 22, 13, tzinfo=UTC))
    assert config.snapshot_path.read_bytes() == frozen
