"""Minimal local evidence store used by the public M1-M2-M3 runtime."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
from typing import Any, Iterable

from .models import RateLimitSnapshot, iso_utc, stable_id


SCHEMA = """
CREATE TABLE IF NOT EXISTS native_requests (
 response_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL,
 occurred_at TEXT NOT NULL, coverage_start TEXT NOT NULL, model TEXT NOT NULL,
 service_tier TEXT NOT NULL, reasoning_effort TEXT,
 input_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL,
 cache_write_input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
 reasoning_output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
 conflict INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS native_time ON native_requests(occurred_at);
CREATE TABLE IF NOT EXISTS rate_limit_snapshots (
 snapshot_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, collector_id TEXT NOT NULL,
 limit_id TEXT NOT NULL, limit_name TEXT, used_percent REAL, window_minutes INTEGER,
 resets_at TEXT, plan_type TEXT, reached_type TEXT, source TEXT NOT NULL,
 authority TEXT NOT NULL, extra_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_limit_time ON rate_limit_snapshots(observed_at);
CREATE TABLE IF NOT EXISTS collection_cursors (
 path TEXT PRIMARY KEY, byte_offset INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
 state_json TEXT NOT NULL
);
"""


def connect(path: Path, *, create: bool = True) -> sqlite3.Connection:
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise FileNotFoundError(path)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    if create:
        db.executescript(SCHEMA)
    return db


def add_rate_limits(db: sqlite3.Connection, snapshots: Iterable[RateLimitSnapshot]) -> int:
    inserted = 0
    with db:
        for item in snapshots:
            snapshot_id = stable_id(
                "rate-limit",
                [item.collector_id, item.limit_id, iso_utc(item.observed_at), item.used_percent, item.resets_at],
            )
            cursor = db.execute(
                """INSERT OR IGNORE INTO rate_limit_snapshots
                (snapshot_id,observed_at,collector_id,limit_id,limit_name,used_percent,
                 window_minutes,resets_at,plan_type,reached_type,source,authority,extra_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id, iso_utc(item.observed_at), item.collector_id, item.limit_id,
                    item.limit_name, item.used_percent, item.window_minutes,
                    iso_utc(item.resets_at) if item.resets_at else None,
                    item.plan_type, item.reached_type, item.source, item.authority,
                    json.dumps(item.extra, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            inserted += int(cursor.rowcount > 0)
    return inserted


def trim(db: sqlite3.Connection, retention_days: int, now: datetime | None = None) -> dict[str, int]:
    cutoff = iso_utc((now or datetime.now(timezone.utc)) - timedelta(days=retention_days))
    with db:
        requests = db.execute("DELETE FROM native_requests WHERE occurred_at<?", (cutoff,)).rowcount
        limits = db.execute("DELETE FROM rate_limit_snapshots WHERE observed_at<?", (cutoff,)).rowcount
    return {"native_requests": requests, "rate_limit_snapshots": limits}
