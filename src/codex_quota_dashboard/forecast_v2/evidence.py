"""Immutable, arrival-aware evidence normalization for forecast-v2."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping

from ..models import iso_utc, parse_timestamp, stable_id
from .contracts import CHANNELS


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value: Any) -> str:
    return sha256(canonical_bytes(value)).hexdigest()


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    return parse_timestamp(value)


def _optional_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _token_values(row: Mapping[str, Any]) -> tuple[dict[str, int | None], tuple[str, ...]]:
    """Normalize token channels without turning missing/invalid data into zero."""
    unknown: set[str] = set()
    nested = row.get("tokens")
    if isinstance(nested, Mapping):
        values: dict[str, int | None] = {}
        for channel in CHANNELS:
            values[channel] = _optional_nonnegative_int(nested.get(channel))
            if values[channel] is None:
                unknown.add(channel)
        return values, tuple(sorted(unknown))
    if "uncached_input" in row or "cached_input" in row or "output" in row:
        values: dict[str, int | None] = {}
        for channel in CHANNELS:
            if channel not in row:
                values[channel] = None
                unknown.add(channel)
                continue
            values[channel] = _optional_nonnegative_int(row[channel])
            if values[channel] is None:
                unknown.add(channel)
        return values, tuple(sorted(unknown))

    total_input = _optional_nonnegative_int(row["input_tokens"]) if "input_tokens" in row else None
    cached = _optional_nonnegative_int(row["cached_input_tokens"]) if "cached_input_tokens" in row else None
    output = _optional_nonnegative_int(row["output_tokens"]) if "output_tokens" in row else None
    if total_input is None or cached is None or cached > total_input:
        unknown.add("uncached_input")
    if cached is None:
        unknown.add("cached_input")
    if output is None:
        unknown.add("output")
    return {
        "uncached_input": total_input - cached if total_input is not None and cached is not None and cached <= total_input else None,
        "cached_input": cached,
        "output": output,
    }, tuple(sorted(unknown))


@dataclass(frozen=True, slots=True)
class RequestEvidence:
    event_id: str
    occurred_at: str
    first_seen_at: str | None
    ingestion_sequence: int | None
    model: str
    effort: str | None
    service_tier: str | None
    tokens: dict[str, int | None]
    conflict: bool = False
    duplicate_count: int = 1
    arrival_clock_status: str = "known"
    reconstructed: bool = False
    source: str = "unknown"
    token_unknown_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
            "first_seen_at": self.first_seen_at,
            "ingestion_sequence": self.ingestion_sequence,
            "model": self.model,
            "effort": self.effort,
            "service_tier": self.service_tier,
            "tokens": dict(self.tokens),
            "tokens_complete": not self.token_unknown_fields,
            "token_unknown_fields": list(self.token_unknown_fields),
            "conflict": self.conflict,
            "duplicate_count": self.duplicate_count,
            "arrival_clock_status": self.arrival_clock_status,
            "reconstructed": self.reconstructed,
            "source": self.source,
        }


def normalize_request(row: Mapping[str, Any]) -> RequestEvidence:
    occurred = _time(row.get("occurred_at") or row.get("event_at"))
    if occurred is None:
        raise ValueError("request evidence needs occurred_at/event_at")
    first_seen = _time(row.get("first_seen_at") or row.get("arrival_at"))
    event_id = str(row.get("event_id") or row.get("response_id") or "")
    if not event_id:
        event_id = stable_id("forecast-v2-request", [iso_utc(occurred), row.get("model", "unknown"), _token_values(row)])
    sequence = row.get("ingestion_sequence")
    if sequence is not None:
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("ingestion_sequence must be a non-negative integer")
    tokens, token_unknown_fields = _token_values(row)
    return RequestEvidence(
        event_id=event_id,
        occurred_at=iso_utc(occurred),
        first_seen_at=iso_utc(first_seen) if first_seen else None,
        ingestion_sequence=sequence,
        model=str(row.get("model") or "unknown"),
        effort=str(row["effort"]) if row.get("effort") is not None else (
            str(row["reasoning_effort"]) if row.get("reasoning_effort") is not None else None
        ),
        service_tier=str(row["service_tier"]) if row.get("service_tier") is not None else None,
        tokens=tokens,
        conflict=bool(row.get("conflict", False)),
        duplicate_count=max(1, int(row.get("duplicate_count") or 1)),
        arrival_clock_status="known" if first_seen is not None else "unknown",
        reconstructed=bool(row.get("reconstructed", False)),
        source=str(row.get("source") or "unknown"),
        token_unknown_fields=token_unknown_fields,
    )

def deduplicate_requests(rows: Iterable[Mapping[str, Any] | RequestEvidence]) -> list[dict[str, Any]]:
    """Deduplicate response identities while retaining conflicts as unusable evidence."""
    grouped: dict[str, list[RequestEvidence]] = {}
    for row in rows:
        item = row if isinstance(row, RequestEvidence) else normalize_request(row)
        grouped.setdefault(item.event_id, []).append(item)
    result: list[dict[str, Any]] = []
    for event_id, items in grouped.items():
        ordered = sorted(items, key=lambda x: (x.first_seen_at or "9999", x.occurred_at, x.source))
        first = ordered[0]
        variants = {tuple(first.tokens[channel] for channel in CHANNELS)}
        variants.update(tuple(item.tokens[channel] for channel in CHANNELS) for item in ordered[1:])
        payload = first.to_dict()
        payload["duplicate_count"] = len(items)
        payload["conflict"] = first.conflict or len(variants) > 1 or any(item.conflict for item in items)
        unknown_fields = sorted({field for item in items for field in item.token_unknown_fields})
        payload["token_unknown_fields"] = unknown_fields
        payload["tokens_complete"] = not unknown_fields
        if payload["conflict"]:
            payload["conflict_reason"] = "same_response_id_has_conflicting_usage"
        result.append(payload)
    return sorted(result, key=lambda x: (x["occurred_at"], x["event_id"]))


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    source_snapshot_id: str
    input_cutoff: str
    mode: str
    requests: tuple[dict[str, Any], ...]
    observations: tuple[dict[str, Any], ...]
    excluded: tuple[dict[str, Any], ...]
    snapshot_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_snapshot_id": self.source_snapshot_id,
            "input_cutoff": self.input_cutoff,
            "mode": self.mode,
            "requests": [dict(x) for x in self.requests],
            "observations": [dict(x) for x in self.observations],
            "excluded": [dict(x) for x in self.excluded],
            "snapshot_sha256": self.snapshot_sha256,
        }


def build_snapshot(
    requests: Iterable[Mapping[str, Any] | RequestEvidence],
    observations: Iterable[Mapping[str, Any]],
    input_cutoff: str | datetime,
    *,
    mode: str = "strict_pre_event",
) -> EvidenceSnapshot:
    """Build a frozen, JSON-safe snapshot without reading or writing a database."""
    cutoff = _time(input_cutoff)
    if cutoff is None:
        raise ValueError("input_cutoff must be a valid timestamp")
    if mode not in ("strict_pre_event", "reconstructed"):
        raise ValueError("unsupported evidence mode")
    normalized = deduplicate_requests(requests)
    accepted: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for item in normalized:
        occurred = parse_timestamp(item["occurred_at"])
        first_seen = parse_timestamp(item.get("first_seen_at"))
        reason = None
        if occurred is None or occurred > cutoff:
            reason = "occurred_after_input_cutoff"
        elif item.get("conflict"):
            reason = "conflicting_duplicate"
        elif item.get("token_unknown_fields"):
            reason = "token_evidence_unknown"
        elif mode == "strict_pre_event" and first_seen is None:
            reason = "arrival_clock_unknown"
        elif mode == "strict_pre_event" and first_seen > cutoff:
            reason = "first_seen_after_input_cutoff"
        if reason:
            excluded.append({"event_id": item["event_id"], "reason": reason})
            continue
        item = dict(item)
        if mode == "reconstructed" and first_seen is None:
            item["reconstructed"] = True
            item["arrival_clock_status"] = "unknown"
        elif mode == "reconstructed" and first_seen is not None and first_seen > cutoff:
            item["reconstructed"] = True
            item["arrival_clock_status"] = "late_reconstructed"
        accepted.append(item)
    accepted_observations: list[dict[str, Any]] = []
    for index, raw in enumerate(observations):
        item = dict(raw)
        observed = _time(item.get("observed_at") or item.get("event_at"))
        if observed is None:
            excluded.append({"observation_index": index, "reason": "invalid_observed_at"})
            continue
        if observed > cutoff:
            excluded.append({"observation_index": index, "reason": "observed_after_input_cutoff"})
            continue
        item["observed_at"] = iso_utc(observed)
        accepted_observations.append(item)
    identity = {
        "input_cutoff": iso_utc(cutoff),
        "mode": mode,
        "requests": accepted,
        "observations": accepted_observations,
        "excluded": excluded,
    }
    snapshot_sha256 = digest(identity)
    source_snapshot_id = "snapshot-" + snapshot_sha256[:24]
    return EvidenceSnapshot(
        source_snapshot_id=source_snapshot_id,
        input_cutoff=iso_utc(cutoff),
        mode=mode,
        requests=tuple(accepted),
        observations=tuple(accepted_observations),
        excluded=tuple(excluded),
        snapshot_sha256=snapshot_sha256,
    )
