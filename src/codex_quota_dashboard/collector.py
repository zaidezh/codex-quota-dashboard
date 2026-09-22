"""Explicit local evidence collection for M1.

Only configured JSONL sources are scanned. Conversation and tool payload rows are
discarded before JSON decoding; the local database stores token counters and the
minimum identifiers needed for deduplication. Authoritative quota observations are
read only when ``monitoring.collect_rate_limits`` is explicitly enabled.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any
import json
import sqlite3

from .appserver import AppServerClient
from .config import SystemConfig
from .models import iso_utc, parse_timestamp
from .store import add_rate_limits, connect, trim


TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)
MARKERS = (
    b'"session_meta"', b'"turn_context"', b'"token_usage_record"',
    b'"thread_settings_applied"',
)


def normalize_service_tier(value: Any) -> str:
    tier = str(value or "unknown").lower()
    if tier in {"fast", "priority"}:
        return "fast"
    if tier in {"standard", "default"}:
        return "standard"
    return "unknown"


def discover_sources(configured: list[Path]) -> list[Path]:
    files: set[Path] = set()
    for source in configured:
        if not source.exists():
            raise FileNotFoundError(f"configured monitoring source does not exist: {source}")
        if source.is_file():
            if source.suffix.lower() != ".jsonl":
                raise ValueError(f"configured monitoring file must use .jsonl: {source}")
            files.add(source.resolve())
        else:
            files.update(path.resolve() for path in source.rglob("*.jsonl") if path.is_file())
    return sorted(files)


def _saved_cursor(db: sqlite3.Connection, path: Path) -> tuple[int, dict[str, Any], int]:
    stat = path.stat()
    row = db.execute("SELECT * FROM collection_cursors WHERE path=?", (str(path),)).fetchone()
    if not row:
        return 0, {}, stat.st_mtime_ns
    offset = int(row["byte_offset"])
    state = json.loads(row["state_json"])
    if state.get("schema_version") != 1 or stat.st_size < offset:
        return 0, {}, stat.st_mtime_ns
    if stat.st_size == offset and int(row["mtime_ns"]) == stat.st_mtime_ns:
        return offset, {**state, "unchanged": True}, stat.st_mtime_ns
    return offset, state, stat.st_mtime_ns


def scan_file(db: sqlite3.Connection, path: Path) -> dict[str, int]:
    offset, state, mtime_ns = _saved_cursor(db, path)
    if state.pop("unchanged", False):
        return {"files": 0, "inserted": 0, "invalid": 0, "foreign": 0}
    inserted = invalid = foreign = 0
    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            start = handle.tell()
            line = handle.readline()
            if not line:
                offset = handle.tell()
                break
            if not line.endswith(b"\n"):
                offset = start
                break
            offset = handle.tell()
            if not any(marker in line for marker in MARKERS):
                continue
            try:
                row = json.loads(line.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                invalid += 1
                continue
            if not isinstance(row, dict) or not isinstance(row.get("payload") or {}, dict):
                invalid += 1
                continue
            payload = row.get("payload") or {}
            stamp = parse_timestamp(row.get("timestamp"))
            if stamp is None:
                continue
            occurred_at = iso_utc(stamp)
            kind = row.get("type")
            if kind == "session_meta":
                state.update(session_id=payload.get("id"), created_at=occurred_at)
                continue
            if not state.get("session_id") or occurred_at < state.get("created_at", ""):
                continue
            if kind == "turn_context":
                state["model"] = payload.get("model") or state.get("model", "unknown")
                state["tier"] = payload.get("service_tier") or state.get("tier", "unknown")
                state["effort"] = payload.get("effort") or payload.get("reasoning_effort")
                turn_id = payload.get("turn_id")
                if turn_id and turn_id != state.get("turn_id"):
                    state.update(turn_id=turn_id, turn_start=occurred_at)
                continue
            subtype = payload.get("type") if kind == "event_msg" else None
            if subtype == "thread_settings_applied":
                settings = payload.get("thread_settings") or {}
                state["model"] = settings.get("model") or state.get("model", "unknown")
                state["tier"] = settings.get("service_tier") or state.get("tier", "unknown")
                continue
            if kind != "token_usage_record":
                continue
            if payload.get("thread_id") != state["session_id"]:
                foreign += 1
                continue
            response_id = payload.get("response_id")
            usage = payload.get("usage") or {}
            if not response_id or any(type(usage.get(name)) is not int or usage[name] < 0 for name in TOKEN_FIELDS):
                invalid += 1
                continue
            if usage["cached_input_tokens"] > usage["input_tokens"] or usage["reasoning_output_tokens"] > usage["output_tokens"]:
                invalid += 1
                continue
            turn_id = payload.get("turn_id") or response_id
            coverage_start = state.get("turn_start", occurred_at) if turn_id == state.get("turn_id") else occurred_at
            coverage_start = min(coverage_start, occurred_at)
            old = db.execute("SELECT * FROM native_requests WHERE response_id=?", (response_id,)).fetchone()
            if old:
                if old["session_id"] != state["session_id"] or any(old[name] != usage[name] for name in TOKEN_FIELDS):
                    db.execute("UPDATE native_requests SET conflict=1 WHERE response_id=?", (response_id,))
                continue
            db.execute(
                """INSERT INTO native_requests
                (response_id,session_id,turn_id,occurred_at,coverage_start,model,service_tier,
                 reasoning_effort,input_tokens,cached_input_tokens,cache_write_input_tokens,
                 output_tokens,reasoning_output_tokens,total_tokens)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    response_id, state["session_id"], turn_id, occurred_at, coverage_start,
                    state.get("model", "unknown"), normalize_service_tier(state.get("tier")),
                    state.get("effort"), *(usage[name] for name in TOKEN_FIELDS),
                ),
            )
            inserted += 1
    state.update(schema_version=1, invalid_records=state.get("invalid_records", 0) + invalid,
                 foreign_records=state.get("foreign_records", 0) + foreign)
    with db:
        db.execute(
            "INSERT OR REPLACE INTO collection_cursors VALUES (?,?,?,?)",
            (str(path), offset, mtime_ns, json.dumps(state, ensure_ascii=False, separators=(",", ":"))),
        )
    return {"files": 1, "inserted": inserted, "invalid": invalid, "foreign": foreign}


def collect_once(config: SystemConfig) -> dict[str, Any]:
    if not config.monitoring.enabled:
        raise RuntimeError("monitoring is disabled; set monitoring.enabled=true explicitly")
    if config.database_path is None:
        raise RuntimeError("state.directory is required")
    totals: Counter[str] = Counter()
    with closing(connect(config.database_path)) as db:
        for path in discover_sources(config.monitoring.sources):
            totals.update(scan_file(db, path))
        quota_count = 0
        if config.monitoring.collect_rate_limits:
            with AppServerClient(config.monitoring.codex_command, config.monitoring.codex_home) as client:
                quota_count = add_rate_limits(db, client.read_rate_limits(config.monitoring.collector_id))
        removed = trim(db, config.state.retention_days)
    return {
        **dict(totals),
        "rate_limits_inserted": quota_count,
        "retention_removed": removed,
        "source_count": len(config.monitoring.sources),
    }
