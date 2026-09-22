"""Offline CLI for validating and evaluating M1-M2-M3 artifacts."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Callable

from ..models import iso_utc, parse_timestamp
from .alignment import evaluate_candidates
from .contracts import ContractError, validate_release
from .evaluation import audit_baseline, evaluate_four_streams, is_matured, score_records
from .evidence import build_snapshot
from .gate import promote, rollback
from .pipeline import build_forecast
from .scenarios import generate_scenario_paths
from .workload import project_plan


def _read_json(path: str | Path) -> Any:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return value


def _write_json(path: str | Path | None, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if path is None:
        sys.stdout.write(encoded)
        return
    target = Path(path)
    if target.exists():
        raise FileExistsError(f"拒绝覆盖已有工件: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(encoded, encoding="utf-8", newline="\n")


def _now(value: str | None) -> datetime:
    if value:
        parsed = parse_timestamp(value)
        if parsed is None:
            raise ValueError("--now 不是有效时间")
        return parsed
    return datetime.now(timezone.utc)


def _records(source: Any) -> list[dict[str, Any]]:
    if isinstance(source, list):
        records = source
    elif isinstance(source, dict) and isinstance(source.get("records"), list):
        records = source["records"]
    else:
        raise ValueError("输入必须是记录数组或包含 records 数组的对象")
    if not all(isinstance(item, dict) for item in records):
        raise ValueError("records 必须全部是对象")
    return [dict(item) for item in records]


def cmd_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    source = _read_json(args.input)
    snapshot = build_snapshot(
        source.get("requests", []),
        source.get("observations", []),
        source["input_cutoff"],
        mode=args.mode,
    )
    return snapshot.to_dict()


def cmd_audit_baseline(args: argparse.Namespace) -> dict[str, Any]:
    source = _read_json(args.input)
    return audit_baseline(
        reported_value_pp=source.get("reported_value_pp", 0.227 if args.reported is None else args.reported),
        original_output=source.get("original_output"),
        reproducible_inputs=bool(source.get("reproducible_inputs", False)),
    )


def cmd_fit_debit(args: argparse.Namespace) -> dict[str, Any]:
    source = _read_json(args.input)
    models = source.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError("fit-debit 输入必须包含非空 models")
    return evaluate_candidates(
        source.get("observations", []),
        source.get("requests", []),
        models=models,
        input_cutoff=source.get("input_cutoff"),
        evidence_mode=source.get("evidence_mode", "reconstructed"),
        offsets=source.get("offsets", (-120, 0, 120)),
        target=source.get("target"),
        timeout_seconds=args.timeout,
        source_snapshot_id=source.get("source_snapshot_id"),
    )


def cmd_generate_scenarios(args: argparse.Namespace) -> dict[str, Any]:
    plan = _read_json(args.plan)
    profiles = _read_json(args.profiles)
    if not isinstance(profiles, dict):
        raise ValueError("profiles 必须是对象")
    return generate_scenario_paths(plan, profiles, path_count=args.paths, seed=args.seed, max_events=args.max_events)


def cmd_forecast(args: argparse.Namespace) -> dict[str, Any]:
    source = _read_json(args.input)
    if not isinstance(source, dict):
        raise ValueError("forecast 输入必须是对象")
    return build_forecast(
        source["plan"],
        source["profiles"],
        models=source["models"],
        reference_theta=source["reference_theta"],
        start_used_pp=source["start_used_pp"],
        reset_at=source.get("reset_at"),
        compatibility_design=source.get("compatibility_design"),
        path_count=args.paths if args.paths is not None else source.get("path_count", 128),
        seed=args.seed if args.seed is not None else source.get("seed", 906),
    )


def cmd_project_plan(args: argparse.Namespace) -> dict[str, Any]:
    return project_plan(_read_json(args.plan), _read_json(args.profiles))


def cmd_evaluate(args: argparse.Namespace) -> dict[str, Any]:
    source = _read_json(args.input)
    records = _records(source)
    return {"overall": score_records(records), "streams": evaluate_four_streams(records)}


def cmd_prepare_review(args: argparse.Namespace) -> dict[str, Any]:
    source = _read_json(args.input)
    records = _records(source)
    now = _now(args.now)
    matured = [dict(item) for item in records if is_matured(item, now, args.maturity_delay)]
    matured.sort(key=lambda item: str(item.get("target_at", "")), reverse=True)
    selected = matured[: args.maximum]
    while len(json.dumps({"records": selected}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > args.byte_limit and len(selected) > args.minimum:
        selected.pop()
    status = "ready" if len(selected) >= args.minimum else "no_new_mature_evidence"
    return {
        "status": status,
        "as_of": iso_utc(now),
        "records": selected,
        "samples": len(selected),
        "minimum": args.minimum,
        "maximum": args.maximum,
        "byte_limit": args.byte_limit,
        "automatic_error_feedback": False,
        "scope": "offline_matured_as_issued_records",
    }


def cmd_verify_release(args: argparse.Namespace) -> dict[str, Any]:
    release = validate_release(_read_json(args.manifest))
    return {"status": "valid_contract", "release_id": release["release_id"], "release": release}


def cmd_promote(args: argparse.Namespace) -> dict[str, Any]:
    return promote(
        args.pointer,
        _read_json(args.manifest),
        expected_previous_release_id=args.expected_release,
        actual_service_release_id=args.actual_service_release_id,
    )


def cmd_rollback(args: argparse.Namespace) -> dict[str, Any]:
    return rollback(
        args.pointer,
        target_release_id=args.to_release,
        incident_id=args.incident,
        expected_previous_release_id=args.expected_release,
    )


def _common_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", help="输出 JSON 路径；省略则写 stdout")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m codex_quota_dashboard.forecast_v2.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    snapshot = sub.add_parser("snapshot", help="构建到达时钟感知的只读证据快照")
    snapshot.add_argument("--input", required=True)
    snapshot.add_argument("--mode", choices=("strict_pre_event", "reconstructed"), default="strict_pre_event")
    _common_output(snapshot)
    snapshot.set_defaults(handler=cmd_snapshot)

    baseline = sub.add_parser("audit-baseline", help="登记 0.227 基线是否可复算")
    baseline.add_argument("--input", required=True)
    baseline.add_argument("--reported", type=float)
    _common_output(baseline)
    baseline.set_defaults(handler=cmd_audit_baseline)

    fit = sub.add_parser("fit-debit", help="运行固定对齐候选的累计约束求解")
    fit.add_argument("--input", required=True)
    fit.add_argument("--timeout", type=float, default=15.0)
    _common_output(fit)
    fit.set_defaults(handler=cmd_fit_debit)

    plan = sub.add_parser("project-plan", help="投影确定性工作量计划")
    plan.add_argument("--plan", required=True)
    plan.add_argument("--profiles", required=True)
    _common_output(plan)
    plan.set_defaults(handler=cmd_project_plan)

    scenarios = sub.add_parser("generate-scenarios", help="从计划生成可重现 M3 场景路径")
    scenarios.add_argument("--plan", required=True)
    scenarios.add_argument("--profiles", required=True)
    scenarios.add_argument("--paths", type=int, default=128)
    scenarios.add_argument("--seed", type=int, default=906)
    scenarios.add_argument("--max-events", type=int, default=100_000)
    _common_output(scenarios)
    scenarios.set_defaults(handler=cmd_generate_scenarios)

    forecast = sub.add_parser("forecast", help="生成后端条件额度预测与统一场景结果")
    forecast.add_argument("--input", required=True)
    forecast.add_argument("--paths", type=int)
    forecast.add_argument("--seed", type=int)
    _common_output(forecast)
    forecast.set_defaults(handler=cmd_forecast)

    evaluate = sub.add_parser("evaluate", help="按四条评价流分别计算指标")
    evaluate.add_argument("--input", required=True)
    _common_output(evaluate)
    evaluate.set_defaults(handler=cmd_evaluate)

    review = sub.add_parser("prepare-review", help="离线构造有界成熟证据包")
    review.add_argument("--input", required=True)
    review.add_argument("--now")
    review.add_argument("--maturity-delay", type=int, default=5)
    review.add_argument("--minimum", type=int, default=3)
    review.add_argument("--maximum", type=int, default=48)
    review.add_argument("--byte-limit", type=int, default=48_000)
    _common_output(review)
    review.set_defaults(handler=cmd_prepare_review)

    verify = sub.add_parser("verify-release", help="验证发布工件合同")
    verify.add_argument("--manifest", required=True)
    _common_output(verify)
    verify.set_defaults(handler=cmd_verify_release)

    promote_parser = sub.add_parser("promote", help="按 expected_previous 原子更新派生指针")
    promote_parser.add_argument("--manifest", required=True)
    promote_parser.add_argument("--pointer", required=True)
    promote_parser.add_argument("--expected-release")
    promote_parser.add_argument("--actual-service-release-id")
    _common_output(promote_parser)
    promote_parser.set_defaults(handler=cmd_promote)

    rollback_parser = sub.add_parser("rollback", help="按 incident 原子回退派生指针")
    rollback_parser.add_argument("--pointer", required=True)
    rollback_parser.add_argument("--to-release", required=True)
    rollback_parser.add_argument("--incident", required=True)
    rollback_parser.add_argument("--expected-release")
    _common_output(rollback_parser)
    rollback_parser.set_defaults(handler=cmd_rollback)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        value = args.handler(args)
        _write_json(getattr(args, "output", None), value)
        return 0
    except (ContractError, FileExistsError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        parser.error(str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
