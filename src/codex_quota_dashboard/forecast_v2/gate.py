"""Pure release checks plus an expected-pointer atomic switch."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Mapping

from ..models import iso_utc, utc_now
from .contracts import ContractError, validate_release


def read_pointer(pointer_path: str | Path) -> dict[str, Any] | None:
    path = Path(pointer_path)
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("release pointer must be a JSON object")
    return value


def check_release_gate(
    release: Mapping[str, Any],
    *,
    current_release_id: str | None,
    expected_previous_release_id: str | None,
    service_release_id: str | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    try:
        value = validate_release(release)
    except ContractError as error:
        return {"status": "rejected", "reasons": [str(error)]}
    if value.get("status") not in ("release_ready", "deployed"):
        reasons.append("release_not_ready")
    if value.get("expected_previous_release_id") != expected_previous_release_id:
        reasons.append("release_expected_pointer_mismatch")
    if current_release_id != expected_previous_release_id:
        reasons.append("current_pointer_changed")
    if value.get("status") == "deployed" and service_release_id is not None and value.get("actual_service_release_id") != service_release_id:
        reasons.append("service_release_identity_mismatch")
    return {"status": "accepted" if not reasons else "rejected", "reasons": reasons, "release_id": value["release_id"]}


def _write_json_no_overwrite(path: Path, value: Mapping[str, Any]) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
    temp.replace(path)


def promote(
    pointer_path: str | Path,
    release: Mapping[str, Any],
    *,
    expected_previous_release_id: str | None,
    actual_service_release_id: str | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Atomically promote a prepared pointer after an expected-value check.

    This function only updates the caller-provided derived pointer.  It does
    not call a service, change a database, or notify a user.
    """
    path = Path(pointer_path)
    current = read_pointer(path)
    current_id = current.get("release_id") if current else None
    decision = check_release_gate(
        release,
        current_release_id=current_id,
        expected_previous_release_id=expected_previous_release_id,
        service_release_id=actual_service_release_id,
    )
    if decision["status"] != "accepted":
        raise ContractError("发布门禁拒绝：" + ",".join(decision["reasons"]))
    value = dict(validate_release(release))
    value["status"] = "deployed"
    value["actual_service_release_id"] = actual_service_release_id or value.get("actual_service_release_id") or value["release_id"]
    value["promoted_at"] = iso_utc(at or utc_now())
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_no_overwrite(path, value)
    return {"status": "deployed", "release_id": value["release_id"], "pointer": str(path), "actual_service_release_id": value["actual_service_release_id"]}


def rollback(
    pointer_path: str | Path,
    *,
    target_release_id: str,
    incident_id: str,
    expected_previous_release_id: str | None,
    at: datetime | None = None,
) -> dict[str, Any]:
    if not target_release_id or not incident_id:
        raise ValueError("rollback needs target_release_id and incident_id")
    current = read_pointer(pointer_path)
    current_id = current.get("release_id") if current else None
    if current_id != expected_previous_release_id:
        raise ContractError("回退门禁拒绝：当前指针已变化")
    value = {
        "schema_version": 1,
        "release_id": target_release_id,
        "status": "rolled_back",
        "incident_id": incident_id,
        "rolled_back_from": current_id,
        "rolled_back_at": iso_utc(at or utc_now()),
    }
    path = Path(pointer_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_no_overwrite(path, value)
    return {"status": "rolled_back", "release_id": target_release_id, "incident_id": incident_id}
