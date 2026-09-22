"""Finite alignment candidates and union-of-compatible-ranges handling."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from .contracts import ALIGNMENT_OFFSETS_SECONDS
from .cumulative_design import build_design
from .debit_set import solve_debit_set
from ..models import parse_timestamp


def temporal_split(
    observations: Iterable[Mapping[str, Any]],
    *,
    cutoff: str | datetime,
    buffer_seconds: int = 0,
) -> dict[str, list[dict[str, Any]]]:
    boundary = cutoff if isinstance(cutoff, datetime) else parse_timestamp(cutoff)
    if boundary is None:
        raise ValueError("cutoff must be a valid timestamp")
    buffer = timedelta(seconds=max(0, int(buffer_seconds)))
    train: list[dict[str, Any]] = []
    holdout: list[dict[str, Any]] = []
    buffered: list[dict[str, Any]] = []
    for raw in observations:
        row = dict(raw)
        at = parse_timestamp(row.get("observed_at") or row.get("event_at"))
        if at is None:
            row["rejection"] = "missing_observed_at"
        elif at < boundary - buffer:
            train.append(row)
        elif at > boundary + buffer:
            holdout.append(row)
        else:
            row["rejection"] = "temporal_buffer"
            buffered.append(row)
    return {"train": train, "holdout": holdout, "buffered": buffered}


def union_ranges(candidates: Sequence[Mapping[str, Any]], *, range_key: str = "target_budget") -> dict[str, Any]:
    ranges = [candidate.get(range_key) for candidate in candidates if candidate.get("status") == "feasible" and isinstance(candidate.get(range_key), Mapping)]
    lowers = [float(item["lower"]) for item in ranges if item.get("lower") is not None]
    uppers = [float(item["upper"]) for item in ranges]
    return {
        "lower": min(lowers) if lowers else None,
        "upper": max(uppers) if uppers and all(item.get("upper") is not None for item in ranges) else None,
        "candidate_count": len(ranges),
        "range_kind": "compatibility_set_union",
        "not_a_probability_interval": True,
    }

def evaluate_candidates(
    observations: Iterable[Mapping[str, Any]],
    requests: Iterable[Mapping[str, Any]],
    *,
    models: Sequence[str],
    input_cutoff: str | datetime | None = None,
    evidence_mode: str = "reconstructed",
    offsets: Sequence[int] = ALIGNMENT_OFFSETS_SECONDS,
    target: Sequence[float] | None = None,
    timeout_seconds: float = 15.0,
    source_snapshot_id: str | None = None,
) -> dict[str, Any]:
    observations = list(observations)
    requests = list(requests)
    offsets = tuple(int(x) for x in offsets)
    invalid = sorted(set(offsets) - set(ALIGNMENT_OFFSETS_SECONDS))
    if invalid:
        raise ValueError(f"alignment offsets outside approved set: {invalid}")
    candidate_results: list[dict[str, Any]] = []
    designs: list[dict[str, Any]] = []
    for offset in offsets:
        design = build_design(
            observations,
            requests,
            models=models,
            alignment_offset_seconds=offset,
            input_cutoff=input_cutoff,
            evidence_mode=evidence_mode,
            source_snapshot_id=source_snapshot_id,
        )
        solved = solve_debit_set(design, target=target, timeout_seconds=timeout_seconds)
        candidate_results.append({"alignment_offset_seconds": offset, "result": solved})
        designs.append(design)
    union = union_ranges([item["result"] for item in candidate_results])
    return {
        "schema_version": 1,
        "status": "ready" if candidate_results else "insufficient_evidence",
        "approved_offsets_seconds": list(ALIGNMENT_OFFSETS_SECONDS),
        "candidates": candidate_results,
        "designs": designs,
        "union": union,
        "selection_policy": "retain_all_feasible_candidates; reference_point_does_not_remove_union_members",
        "expanded_offsets": False,
    }
