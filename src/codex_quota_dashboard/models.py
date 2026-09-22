from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Mapping


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str | int | float | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def stable_id(kind: str, value: Mapping[str, Any] | list[Any]) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(f"{kind}\0{body}".encode("utf-8")).hexdigest()


@dataclass(slots=True)
class RateLimitSnapshot:
    observed_at: datetime
    collector_id: str
    limit_id: str
    limit_name: str | None
    used_percent: float | None
    window_minutes: int | None
    resets_at: datetime | None
    plan_type: str | None
    reached_type: str | None
    source: str
    authority: str = "server_authoritative"
    snapshot_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.snapshot_id:
            self.snapshot_id = stable_id(
                "rate-limit",
                [
                    self.collector_id,
                    iso_utc(self.observed_at),
                    self.limit_id,
                    self.used_percent,
                    self.window_minutes,
                    iso_utc(self.resets_at) if self.resets_at else None,
                    self.source,
                ],
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "observed_at": iso_utc(self.observed_at),
            "collector_id": self.collector_id,
            "limit_id": self.limit_id,
            "limit_name": self.limit_name,
            "used_percent": self.used_percent,
            "window_minutes": self.window_minutes,
            "resets_at": iso_utc(self.resets_at) if self.resets_at else None,
            "plan_type": self.plan_type,
            "reached_type": self.reached_type,
            "source": self.source,
            "authority": self.authority,
            "extra": self.extra,
        }
