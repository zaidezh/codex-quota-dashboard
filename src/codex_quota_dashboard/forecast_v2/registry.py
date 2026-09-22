"""Append-only local artifact registry and freeze identities."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..models import iso_utc, stable_id, utc_now
from .contracts import validate_candidate, validate_release
from .evidence import canonical_bytes


def artifact_hash(path: str | Path) -> str:
    return sha256(Path(path).read_bytes()).hexdigest()


def implementation_hash(package_dir: str | Path | None = None) -> str:
    root = Path(package_dir) if package_dir else Path(__file__).parent
    chunks: list[bytes] = []
    for path in sorted(root.glob("*.py")):
        chunks.append(path.name.encode("utf-8") + b"\0" + path.read_bytes())
    return sha256(b"".join(chunks)).hexdigest()


def freeze_forecast(
    forecast: Mapping[str, Any],
    *,
    source_snapshot_id: str,
    input_cutoff: str,
    algorithm_id: str,
    evaluation_spec_id: str,
    issued_at: datetime | None = None,
) -> dict[str, Any]:
    """Return a frozen issuance without changing the original mapping."""
    issued = issued_at or utc_now()
    body = json.loads(json.dumps(dict(forecast), ensure_ascii=False, allow_nan=False))
    frozen = {
        "source_snapshot_id": source_snapshot_id,
        "input_cutoff": input_cutoff,
        "algorithm_id": algorithm_id,
        "evaluation_spec_id": evaluation_spec_id,
        "forecast": body,
    }
    input_hash = sha256(canonical_bytes(frozen)).hexdigest()
    issuance_id = "issuance-" + stable_id("forecast-v2-issuance", [input_hash, iso_utc(issued)])[:24]
    return {
        "issuance_id": issuance_id,
        "issued_at": iso_utc(issued),
        "input_cutoff": input_cutoff,
        "source_snapshot_id": source_snapshot_id,
        "algorithm_id": algorithm_id,
        "evaluation_spec_id": evaluation_spec_id,
        "input_sha256": input_hash,
        "implementation_sha256": implementation_hash(),
        "forecast": body,
        "immutability": "append_only_as_issued",
    }


@dataclass
class AppendOnlyRegistry:
    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        result = []
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid registry line {line_number}") from error
            if not isinstance(value, dict):
                raise ValueError(f"registry line {line_number} is not an object")
            result.append(value)
        return result

    def events(self) -> list[dict[str, Any]]:
        return self._read()

    def append(self, kind: str, payload: Mapping[str, Any], *, event_id: str | None = None, at: datetime | None = None) -> str:
        if not kind or not kind.strip():
            raise ValueError("registry event kind is required")
        value = json.loads(json.dumps(dict(payload), ensure_ascii=False, allow_nan=False))
        identity = event_id or stable_id("forecast-v2-event", [kind, value])
        for existing in self._read():
            if existing.get("event_id") != identity:
                continue
            if existing.get("kind") == kind and existing.get("payload") == value:
                return identity
            raise ValueError("append-only registry identity collision")
        event = {"event_id": identity, "kind": kind, "at": iso_utc(at or utc_now()), "payload": value}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        return identity

    def append_candidate(self, candidate: Mapping[str, Any]) -> str:
        value = validate_candidate(candidate)
        return self.append("candidate", value, event_id=value["candidate_id"])

    def append_release(self, release: Mapping[str, Any]) -> str:
        value = validate_release(release)
        return self.append("release", value, event_id=value["release_id"])


def hashes_for(paths: Iterable[str | Path]) -> dict[str, str]:
    return {str(Path(path)): artifact_hash(path) for path in paths}
