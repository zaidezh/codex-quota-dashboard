"""Explicit, privacy-first configuration for the M1-M2-M3 runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import math
import os
import tomllib


@dataclass(slots=True)
class StateConfig:
    directory: Path | None = None
    retention_days: int = 35


@dataclass(slots=True)
class BootstrapReferenceConfig:
    mode: str = "off"
    path: Path | None = None
    capacity_multiplier: float = 1.0


@dataclass(slots=True)
class MonitoringConfig:
    enabled: bool = False
    sources: list[Path] = field(default_factory=list)
    collect_rate_limits: bool = False
    collector_id: str = "local-machine"
    codex_command: str = "codex"
    codex_home: Path | None = None
    poll_seconds: int = 60


@dataclass(slots=True)
class LocalFittingConfig:
    enabled: bool = False
    history_days: int = 7
    solver_timeout_seconds: float = 15.0
    workload_lookback_minutes: int = 120
    path_count: int = 1
    seed: int = 906
    bucket_seconds: int = 900
    refresh_seconds: int = 300


@dataclass(slots=True)
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 18766


@dataclass(slots=True)
class SystemConfig:
    timezone: str = "UTC"
    state: StateConfig = field(default_factory=StateConfig)
    bootstrap_reference: BootstrapReferenceConfig = field(default_factory=BootstrapReferenceConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    local_fitting: LocalFittingConfig = field(default_factory=LocalFittingConfig)
    web: WebConfig = field(default_factory=WebConfig)
    config_path: Path | None = None

    @property
    def database_path(self) -> Path | None:
        return self.state.directory / "evidence.sqlite" if self.state.directory else None

    @property
    def snapshot_path(self) -> Path | None:
        return self.state.directory / "snapshot.json" if self.state.directory else None


def _path(value: object, base: Path) -> Path | None:
    if value in (None, ""):
        return None
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    candidate = Path(expanded)
    return (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()


def _bounded_int(value: object, name: str, default: int, minimum: int, maximum: int) -> int:
    number = int(default if value is None else value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def _bounded_float(value: object, name: str, default: float, minimum: float, maximum: float) -> float:
    number = float(default if value is None else value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def _positive_float(value: object, name: str, default: float) -> float:
    number = float(default if value is None else value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return number


def load_config(path: str | os.PathLike[str]) -> SystemConfig:
    config_path = Path(path).resolve()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    base = config_path.parent
    state_raw = raw.get("state") or {}
    bootstrap_raw = raw.get("bootstrap_reference") or {}
    monitor_raw = raw.get("monitoring") or {}
    fit_raw = raw.get("local_fitting") or {}
    web_raw = raw.get("web") or {}

    state = StateConfig(
        directory=_path(state_raw.get("directory"), base),
        retention_days=_bounded_int(state_raw.get("retention_days"), "state.retention_days", 35, 1, 3650),
    )
    mode = str(bootstrap_raw.get("mode") or "off").strip().lower()
    if mode not in {"off", "bundled", "path"}:
        raise ValueError("bootstrap_reference.mode must be off, bundled, or path")
    bootstrap = BootstrapReferenceConfig(
        mode=mode,
        path=_path(bootstrap_raw.get("path"), base),
        capacity_multiplier=_positive_float(
            bootstrap_raw.get("capacity_multiplier"),
            "bootstrap_reference.capacity_multiplier",
            1.0,
        ),
    )
    if mode == "path" and bootstrap.path is None:
        raise ValueError("bootstrap_reference.path is required when mode=path")

    source_values = monitor_raw.get("sources") or []
    if not isinstance(source_values, list):
        raise ValueError("monitoring.sources must be a list")
    monitoring = MonitoringConfig(
        enabled=bool(monitor_raw.get("enabled", False)),
        sources=[item for value in source_values if (item := _path(value, base)) is not None],
        collect_rate_limits=bool(monitor_raw.get("collect_rate_limits", False)),
        collector_id=str(monitor_raw.get("collector_id") or "local-machine").strip(),
        codex_command=str(monitor_raw.get("codex_command") or "codex").strip(),
        codex_home=_path(monitor_raw.get("codex_home"), base),
        poll_seconds=_bounded_int(monitor_raw.get("poll_seconds"), "monitoring.poll_seconds", 60, 10, 86400),
    )
    fitting = LocalFittingConfig(
        enabled=bool(fit_raw.get("enabled", False)),
        history_days=_bounded_int(fit_raw.get("history_days"), "local_fitting.history_days", 7, 1, 90),
        solver_timeout_seconds=_bounded_float(fit_raw.get("solver_timeout_seconds"), "local_fitting.solver_timeout_seconds", 15.0, 0.1, 120.0),
        workload_lookback_minutes=_bounded_int(fit_raw.get("workload_lookback_minutes"), "local_fitting.workload_lookback_minutes", 120, 15, 1440),
        path_count=_bounded_int(fit_raw.get("path_count"), "local_fitting.path_count", 1, 1, 128),
        seed=int(fit_raw.get("seed", 906)),
        bucket_seconds=_bounded_int(fit_raw.get("bucket_seconds"), "local_fitting.bucket_seconds", 900, 60, 3600),
        refresh_seconds=_bounded_int(fit_raw.get("refresh_seconds"), "local_fitting.refresh_seconds", 300, 30, 86400),
    )
    web = WebConfig(
        host=str(web_raw.get("host") or "127.0.0.1").strip(),
        port=_bounded_int(web_raw.get("port"), "web.port", 18766, 0, 65535),
    )
    config = SystemConfig(
        timezone=str(raw.get("timezone") or "UTC").strip(),
        state=state,
        bootstrap_reference=bootstrap,
        monitoring=monitoring,
        local_fitting=fitting,
        web=web,
        config_path=config_path,
    )
    _validate(config)
    return config


def _validate(config: SystemConfig) -> None:
    if config.monitoring.enabled:
        if config.state.directory is None:
            raise ValueError("state.directory is required when monitoring is enabled")
        if not config.monitoring.sources and not config.monitoring.collect_rate_limits:
            raise ValueError("monitoring requires at least one explicit source or collect_rate_limits=true")
    if config.local_fitting.enabled:
        if not config.monitoring.enabled:
            raise ValueError("local fitting requires monitoring.enabled=true")
        if config.state.directory is None:
            raise ValueError("state.directory is required when local fitting is enabled")
    if config.bootstrap_reference.mode == "path" and not config.bootstrap_reference.path.is_file():
        raise ValueError("bootstrap_reference.path must point to a readable file")
