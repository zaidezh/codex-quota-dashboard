"""Deterministic synthetic benchmark for the public M1-M2-M3 implementation."""

from __future__ import annotations

from argparse import ArgumentParser
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import ctypes
import json
import os
import platform
import statistics
import time

if os.name != "nt":
    import resource as unix_resource
else:
    unix_resource = None

from codex_quota_dashboard.config import (
    BootstrapReferenceConfig,
    LocalFittingConfig,
    MonitoringConfig,
    StateConfig,
    SystemConfig,
)
from codex_quota_dashboard.models import iso_utc
from codex_quota_dashboard.store import connect
from codex_quota_dashboard.system import freeze_snapshot


MODELS = ("gpt-5.6-luna", "gpt-5.6-sol", "gpt-6-astra")
COEFFICIENTS = {
    "gpt-5.6-luna": (0.10, 0.005, 0.50),
    "gpt-5.6-sol": (0.50, 0.020, 3.00),
    "gpt-6-astra": (0.55, 0.075, 4.00),
}


def peak_working_set_mib() -> float | None:
    if os.name == "nt":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        read_memory = kernel.K32GetProcessMemoryInfo
        read_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        read_memory.restype = wintypes.BOOL
        if read_memory(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters.PeakWorkingSetSize / 1024 / 1024
        return None
    assert unix_resource is not None
    usage = unix_resource.getrusage(unix_resource.RUSAGE_SELF).ru_maxrss
    return usage / 1024 if platform.system() != "Darwin" else usage / 1024 / 1024


def populate(config: SystemConfig, request_count: int, observation_count: int, now: datetime) -> None:
    start = now - timedelta(days=7)
    reset = now + timedelta(days=7)
    duration = (now - start).total_seconds()
    requests = []
    cumulative: list[tuple[datetime, float]] = []
    used = 5.0
    for index in range(request_count):
        stamp = start + timedelta(seconds=duration * (index + 1) / request_count)
        model = MODELS[(index // max(1, request_count // 12)) % len(MODELS)]
        uncached = 400 + index % 300
        cached = 1200 + (index * 7) % 2800
        output = 180 + (index * 3) % 420
        a, b, c = COEFFICIENTS[model]
        used += (a * uncached + b * cached + c * output) / 1_000_000
        requests.append(
            (
                f"response-{index}", f"session-{index % 97}", f"turn-{index}",
                iso_utc(stamp), iso_utc(stamp), model, "standard", "high",
                uncached + cached, cached, 0, output, 0, uncached + cached + output, 0,
            )
        )
        cumulative.append((stamp, used))
    observations = []
    for index in range(observation_count):
        position = round(index * (request_count - 1) / max(1, observation_count - 1))
        stamp, amount = cumulative[position]
        observations.append(
            (
                f"observation-{index}", iso_utc(stamp), "benchmark", "codex:primary", "primary",
                round(amount, 1), 10080, iso_utc(reset), "benchmark", None,
                "appserver_account_rate_limits", "server_authoritative", "{}",
            )
        )
    with closing(connect(config.database_path)) as db:
        db.executemany(
            """INSERT INTO native_requests
            (response_id,session_id,turn_id,occurred_at,coverage_start,model,service_tier,
             reasoning_effort,input_tokens,cached_input_tokens,cache_write_input_tokens,
             output_tokens,reasoning_output_tokens,total_tokens,conflict)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            requests,
        )
        db.executemany(
            """INSERT INTO rate_limit_snapshots
            (snapshot_id,observed_at,collector_id,limit_id,limit_name,used_percent,
             window_minutes,resets_at,plan_type,reached_type,source,authority,extra_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            observations,
        )
        db.commit()


def run(request_count: int, observation_count: int) -> dict[str, object]:
    now = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
    with TemporaryDirectory(prefix="codex-quota-benchmark-") as temporary:
        config = SystemConfig(
            timezone="UTC",
            state=StateConfig(directory=Path(temporary), retention_days=35),
            bootstrap_reference=BootstrapReferenceConfig(mode="bundled"),
            monitoring=MonitoringConfig(enabled=True, sources=[Path(__file__)]),
            local_fitting=LocalFittingConfig(enabled=True, history_days=7, solver_timeout_seconds=30),
        )
        setup_start = time.perf_counter()
        populate(config, request_count, observation_count, now)
        setup_seconds = time.perf_counter() - setup_start
        fit_start = time.perf_counter()
        snapshot = freeze_snapshot(config, now)
        fit_seconds = time.perf_counter() - fit_start
        m2 = snapshot["forecast_v2"]["m2"]
        m3 = snapshot["forecast_v2"]["m3"]
        return {
            "schema": "codex-quota-system-benchmark-v1",
            "dataset": "deterministic_synthetic",
            "request_count": request_count,
            "observation_count": observation_count,
            "setup_seconds": round(setup_seconds, 4),
            "fit_and_freeze_seconds": round(fit_seconds, 4),
            "peak_working_set_mib": round(peak_working_set_mib() or 0.0, 2),
            "m2_status": m2.get("status"),
            "m2_strict_status": m2.get("strict_status"),
            "m3_status": m3.get("status"),
            "m3_reference_source": m3.get("reference_source"),
            "python": platform.python_version(),
            "platform": platform.platform(),
        }


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--requests", type=int, default=50_000)
    parser.add_argument("--observations", type=int, default=132)
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    samples = [run(args.requests, args.observations) for _ in range(args.runs)]
    if len(samples) == 1:
        result: dict[str, object] = samples[0]
    else:
        fit_seconds = [float(item["fit_and_freeze_seconds"]) for item in samples]
        result = {
            "schema": "codex-quota-system-benchmark-series-v1",
            "dataset": "deterministic_synthetic",
            "request_count": args.requests,
            "observation_count": args.observations,
            "runs": len(samples),
            "fit_and_freeze_seconds": fit_seconds,
            "fit_and_freeze_p50_seconds": round(statistics.median(fit_seconds), 4),
            "fit_and_freeze_p95_seconds": round(
                statistics.quantiles(fit_seconds, n=100, method="inclusive")[94],
                4,
            ),
            "peak_working_set_mib_max": max(float(item["peak_working_set_mib"]) for item in samples),
            "m2_statuses": [item["m2_status"] for item in samples],
            "m3_statuses": [item["m3_status"] for item in samples],
            "python": samples[0]["python"],
            "platform": samples[0]["platform"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
