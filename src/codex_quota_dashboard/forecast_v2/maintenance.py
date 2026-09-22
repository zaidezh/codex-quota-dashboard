"""Bounded, non-automatic maintenance decisions for forecast-v2."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping

from .contracts import validate_candidate
from .evidence import canonical_bytes


@dataclass(frozen=True, slots=True)
class MaintenancePolicy:
    policy_id: str = "maintenance-policy-v1"
    max_new_candidates_per_run: int = 1
    max_attempts_per_incident: int = 2
    max_variants_per_candidate: int = 3
    production_auto_promotion: bool = False
    notification_mode: str = "notification_unconfigured"


def incident_id(kind: str, scope: Mapping[str, Any]) -> str:
    return "incident-" + sha256(canonical_bytes({"kind": kind, "scope": dict(scope)})).hexdigest()[:24]


def deduplicated_incident_events(
    events: Iterable[Mapping[str, Any]],
    *,
    current_evidence: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Return one event per stable incident/evidence combination.

    This produces a decision record only.  It deliberately does not send a
    notification or write the production outbox.
    """
    evidence = sorted(set(str(x) for x in current_evidence))
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for raw in events:
        item = dict(raw)
        ident = str(item.get("incident_id") or incident_id(str(item.get("kind", "unknown")), item.get("scope", {})))
        severity = str(item.get("severity", "P2"))
        fingerprint = sha256(canonical_bytes({"incident_id": ident, "severity": severity, "evidence": evidence or item.get("evidence", [])})).hexdigest()
        key = (ident, severity, fingerprint)
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "incident_id": ident,
            "severity": severity,
            "fingerprint": fingerprint,
            "evidence": evidence or list(item.get("evidence", [])),
            "notification_status": "notification_unconfigured",
            "delivery_attempted": False,
        })
    return result


def choose_daily_action(
    *,
    new_evidence: bool,
    active_candidate: Mapping[str, Any] | None = None,
    attempts_for_incident: int = 0,
    proposed_candidate: Mapping[str, Any] | None = None,
    policy: MaintenancePolicy | None = None,
) -> dict[str, Any]:
    """Choose no_action/awaiting/propose/escalate without changing code."""
    policy = policy or MaintenancePolicy()
    if active_candidate is not None:
        return {"action": "advance_existing_candidate", "candidate_id": active_candidate.get("candidate_id"), "policy_id": policy.policy_id}
    if not new_evidence:
        return {"action": "awaiting_evidence", "policy_id": policy.policy_id, "reason": "no_new_evidence"}
    if attempts_for_incident >= policy.max_attempts_per_incident:
        return {"action": "escalate", "policy_id": policy.policy_id, "reason": "bounded_attempt_budget_exhausted"}
    if proposed_candidate is None:
        return {"action": "no_action", "policy_id": policy.policy_id, "reason": "no_falsifiable_candidate"}
    candidate = validate_candidate(proposed_candidate)
    if candidate["variant_count"] > policy.max_variants_per_candidate:
        return {"action": "escalate", "policy_id": policy.policy_id, "reason": "candidate_variant_budget_exceeded"}
    return {
        "action": "propose_candidate",
        "policy_id": policy.policy_id,
        "candidate_id": candidate["candidate_id"],
        "production_auto_promotion": policy.production_auto_promotion,
        "notification_status": policy.notification_mode,
    }

def build_escalation(
    *,
    incident: Mapping[str, Any],
    current_stage: str,
    attempts: Iterable[Mapping[str, Any]],
    safe_state: str,
    decision_question: str,
    resume_condition: str,
) -> dict[str, Any]:
    """Create a bounded user decision package, without dispatching it."""
    return {
        "schema_version": 1,
        "kind": "forecast_v2_escalation",
        "incident_id": incident.get("incident_id"),
        "current_stage": current_stage,
        "incident": dict(incident),
        "attempts": [dict(x) for x in attempts][:2],
        "safe_state": safe_state,
        "decision_question": decision_question,
        "resume_condition": resume_condition,
        "notification_status": "notification_unconfigured",
        "delivery_attempted": False,
    }
