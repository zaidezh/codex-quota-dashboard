"""Freeze and load the single M1 -> M2 -> M3 product snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import os

from .config import SystemConfig
from .forecast_v2.live_m2 import build_live_m2, compact_m2
from .forecast_v2.live_m3 import build_live_m3
from .forecast_v2.observation_view import build_observation_view
from .models import iso_utc, stable_id
from .store import connect


SNAPSHOT_SCHEMA = "codex-quota-system-snapshot-v1"
BUNDLED_REFERENCE = "data/bootstrap-reference-v1.json"


def bootstrap_path(config: SystemConfig) -> Path | None:
    mode = config.bootstrap_reference.mode
    if mode == "off":
        return None
    if mode == "path":
        return config.bootstrap_reference.path
    resource = files("codex_quota_dashboard").joinpath(BUNDLED_REFERENCE)
    path = Path(str(resource))
    if not path.is_file():
        raise FileNotFoundError("bundled bootstrap reference is unavailable")
    return path


def _disabled_m2(now: datetime) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "version": "quota-forecast-v2-m2-live-v2",
        "algorithm_id": "cumulative-debit-explanation-v1",
        "status": "disabled",
        "strict_status": "not_evaluated",
        "reason": "local_fitting_explicitly_disabled",
        "input_cutoff": iso_utc(now),
        "quote_ready": False,
        "model_parameters": [],
        "current_cycle": {"status": "disabled", "points": [], "current": None},
    }


def _latest_limit(db: Any) -> dict[str, Any] | None:
    row = db.execute(
        """SELECT * FROM rate_limit_snapshots
        WHERE limit_id='codex:primary'
          AND source='appserver_account_rate_limits'
          AND authority='server_authoritative'
        ORDER BY observed_at DESC LIMIT 1"""
    ).fetchone()
    return dict(row) if row else None


def build_snapshot(config: SystemConfig, now: datetime | None = None) -> dict[str, Any]:
    if config.database_path is None:
        raise RuntimeError("state.directory is required to build a snapshot")
    now = now or datetime.now(timezone.utc)
    try:
        zone = ZoneInfo(config.timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown timezone: {config.timezone}") from error
    with closing(connect(config.database_path)) as db:
        m1 = build_observation_view(db, now, zone, history_days=config.state.retention_days)
        m2 = (
            build_live_m2(
                db,
                now,
                history_days=config.local_fitting.history_days,
                timeout_seconds=config.local_fitting.solver_timeout_seconds,
            )
            if config.local_fitting.enabled
            else _disabled_m2(now)
        )
        m3 = build_live_m3(
            db,
            m2,
            _latest_limit(db),
            bootstrap_reference_path=bootstrap_path(config),
            bootstrap_capacity_multiplier=config.bootstrap_reference.capacity_multiplier,
            now=now,
            lookback_minutes=config.local_fitting.workload_lookback_minutes,
            path_count=config.local_fitting.path_count,
            seed=config.local_fitting.seed,
            bucket_seconds=config.local_fitting.bucket_seconds,
        )
    state = (
        "locally_validated" if m3.get("reference_source") == "local_m2_explanation"
        else "reference_only" if m3.get("reference_source") == "bootstrap_reference"
        else "collecting" if config.monitoring.enabled
        else "empty_history"
    )
    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "schema": SNAPSHOT_SCHEMA,
        "generated_at": iso_utc(now),
        "timezone": config.timezone,
        "system_state": state,
        "runtime": {
            "monitoring_enabled": config.monitoring.enabled,
            "local_fitting_enabled": config.local_fitting.enabled,
            "bootstrap_mode": config.bootstrap_reference.mode,
            "bootstrap_capacity_multiplier": config.bootstrap_reference.capacity_multiplier,
            "retention_days": config.state.retention_days,
        },
        "forecast_v2": {"m1": m1, "m2": compact_m2(m2), "m3": m3},
        "adaptive": m1,
    }
    snapshot["snapshot_id"] = stable_id(
        "codex-quota-system-snapshot",
        [snapshot["generated_at"], m2.get("m2_id"), m3.get("m3_id"), state],
    )
    return snapshot


def freeze_snapshot(config: SystemConfig, now: datetime | None = None) -> dict[str, Any]:
    if config.snapshot_path is None:
        raise RuntimeError("state.directory is required to freeze a snapshot")
    snapshot = build_snapshot(config, now)
    body = (json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    config.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config.snapshot_path.with_suffix(".json.tmp")
    temporary.write_bytes(body)
    os.replace(temporary, config.snapshot_path)
    return {**snapshot, "content_sha256": sha256(body).hexdigest()}


def load_snapshot(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("snapshot schema mismatch")
    return value
