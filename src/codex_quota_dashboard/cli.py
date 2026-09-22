"""Command line entry point for the environment-adapted quota system."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import threading
import time

from .collector import collect_once
from .config import SystemConfig, load_config
from .server import LOOPBACK_BINDS, create_server
from .system import freeze_snapshot, load_snapshot


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _require_state(config: SystemConfig) -> Path:
    if config.state.directory is None:
        raise SystemExit("state.directory must be configured for this command")
    return config.state.directory


def _serve(config: SystemConfig, *, allow_network_bind: bool, supervisor: bool) -> int:
    _require_state(config)
    if config.snapshot_path is None:
        raise SystemExit("state.directory must be configured")
    if config.web.host.lower() not in LOOPBACK_BINDS and not allow_network_bind:
        raise SystemExit("network listening requires --allow-network-bind")
    stop = threading.Event()
    worker: threading.Thread | None = None

    if supervisor:
        if config.monitoring.enabled:
            collect_once(config)
            freeze_snapshot(config)
        elif not config.snapshot_path.is_file():
            raise SystemExit("run requires an existing snapshot when monitoring is disabled")

        def loop() -> None:
            started_at = time.monotonic()
            next_collect = started_at + config.monitoring.poll_seconds
            next_refresh = started_at + config.local_fitting.refresh_seconds
            while not stop.wait(0.5):
                now = time.monotonic()
                try:
                    if config.monitoring.enabled and now >= next_collect:
                        collect_once(config)
                        next_collect = now + config.monitoring.poll_seconds
                    if config.monitoring.enabled and now >= next_refresh:
                        freeze_snapshot(config)
                        next_refresh = now + config.local_fitting.refresh_seconds
                except Exception as error:  # keep the last frozen snapshot available
                    print(f"background cycle failed: {type(error).__name__}: {error}", flush=True)

        worker = threading.Thread(target=loop, name="quota-system-supervisor", daemon=True)
        worker.start()

    server = create_server(config.web.host, config.web.port, config.snapshot_path)
    display_host = "127.0.0.1" if config.web.host in {"0.0.0.0", "::"} else config.web.host
    print(f"Codex quota system: http://{display_host}:{server.server_port}/#overview", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        if worker:
            worker.join(timeout=5)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the explicit M1-M2-M3 Codex quota system.")
    parser.add_argument("--config", default="config.toml", help="TOML configuration file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-config", help="validate configuration without collecting or writing state")
    sub.add_parser("collect", help="run one explicitly enabled M1 collection cycle")
    sub.add_parser("refresh", help="freeze one M1-M2-M3 snapshot from current local state")
    sub.add_parser("status", help="read the current frozen snapshot status")
    serve_parser = sub.add_parser("serve", help="serve an existing frozen snapshot")
    serve_parser.add_argument("--allow-network-bind", action="store_true")
    run_parser = sub.add_parser("run", help="run configured monitoring, fitting and the web service")
    run_parser.add_argument("--allow-network-bind", action="store_true")
    purge = sub.add_parser("purge-local-state", help="delete the configured local state directory")
    purge.add_argument("--yes", action="store_true", help="confirm deletion of the exact configured directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    if args.command == "validate-config":
        _json({
            "status": "valid",
            "monitoring_enabled": config.monitoring.enabled,
            "local_fitting_enabled": config.local_fitting.enabled,
            "bootstrap_mode": config.bootstrap_reference.mode,
            "state_configured": config.state.directory is not None,
        })
        return 0
    if args.command == "collect":
        _json(collect_once(config))
        return 0
    if args.command == "refresh":
        snapshot = freeze_snapshot(config)
        _json({
            "status": "frozen",
            "snapshot_id": snapshot.get("snapshot_id"),
            "system_state": snapshot.get("system_state"),
            "m2_status": snapshot.get("forecast_v2", {}).get("m2", {}).get("status"),
            "m3_status": snapshot.get("forecast_v2", {}).get("m3", {}).get("status"),
            "content_sha256": snapshot.get("content_sha256"),
        })
        return 0
    if args.command == "status":
        if config.snapshot_path is None or not config.snapshot_path.is_file():
            _json({"status": "no_frozen_snapshot"})
            return 1
        snapshot = load_snapshot(config.snapshot_path)
        _json({
            "status": "ok",
            "snapshot_id": snapshot.get("snapshot_id"),
            "generated_at": snapshot.get("generated_at"),
            "system_state": snapshot.get("system_state"),
            "runtime": snapshot.get("runtime"),
        })
        return 0
    if args.command == "serve":
        return _serve(config, allow_network_bind=args.allow_network_bind, supervisor=False)
    if args.command == "run":
        return _serve(config, allow_network_bind=args.allow_network_bind, supervisor=True)
    if args.command == "purge-local-state":
        target = _require_state(config).resolve()
        forbidden = {Path(target.anchor).resolve(), Path.home().resolve(), config.config_path.parent.resolve()}
        if target in forbidden or len(target.parts) < 3:
            raise SystemExit("refusing to delete an unsafe state.directory")
        if not args.yes:
            raise SystemExit("purge-local-state requires --yes")
        if target.exists():
            shutil.rmtree(target)
        _json({"status": "deleted", "path": str(target)})
        return 0
    raise AssertionError(args.command)
