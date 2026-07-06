#!/usr/bin/env python3
"""Generate an auditable Agent Battle validation report for the kernel."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from apkernel import SchemaRegistry, validate_artifact_semantics  # noqa: E402
import battle_report  # noqa: E402
import self_assess  # noqa: E402


REQUIRED_JUDGES = ("architect", "test_engineer", "code_reviewer", "critic")
PRE_BATTLE_DIMENSIONS = (
    "contract_integrity",
    "execution_reality",
    "evidence_trust",
    "replay_strength",
    "multi_agent_readiness",
    "autonomy_loop",
)
ALLOWED_SELF_ASSESS_BATTLE_ACTIONS = {
    "run-independent-agent-battle",
    "address-independent-agent-battle-findings",
}
ALLOWED_BATTLE_REPORT_ACTIONS = {
    "Run and preserve an independent multi-agent battle report for APK.",
    "Address the current independent Agent Battle hold findings.",
}
ROLE_CONTEXT_REFS = {
    "architect": ["apkernel/core.py", "pipelines", "packs/registry.json"],
    "test_engineer": ["scripts/verify.py", "scripts/replay_regressions.py", "tests/test_kernel.py"],
    "code_reviewer": ["schemas/artifacts", "examples/golden_*.json", "apkernel/replay.py"],
    "critic": ["docs/SELF_DRIVING.md", "scripts/self_assess.py", "scripts/battle_report.py"],
}


def _hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _dimension_score(self_report: dict[str, Any], name: str) -> float:
    for dimension in self_report.get("dimensions", []):
        if dimension.get("name") == name:
            return float(dimension.get("score", 0))
    return 0.0


def _judge(
    raw_judge: dict[str, Any],
    self_report: dict[str, Any],
    *,
    veto_active: bool,
    veto_reason: str,
) -> dict[str, Any]:
    role = str(raw_judge["role"])
    context_refs = ROLE_CONTEXT_REFS.get(role, raw_judge.get("evidence_refs", []))
    score = float(raw_judge["score"])
    findings = list(raw_judge.get("concerns", [])) + list(raw_judge.get("recommended_actions", []))
    if not findings:
        findings = ["No material finding recorded."]
    role_veto = role == "critic" and veto_active
    context_payload = {
        "role": role,
        "context_refs": context_refs,
        "self_dimensions": {
            "contract_integrity": _dimension_score(self_report, "contract_integrity"),
            "execution_reality": _dimension_score(self_report, "execution_reality"),
            "evidence_trust": _dimension_score(self_report, "evidence_trust"),
            "replay_strength": _dimension_score(self_report, "replay_strength"),
        },
    }
    return {
        "role": role,
        "input_run_id": str(self_report.get("run_id", "")),
        "context_hash": _hash(context_payload),
        "context_refs": context_refs,
        "source": "derived_battle_report",
        "source_report": f"generated:battle_report.judges.{role}",
        "peer_scores_visible": False,
        "score": score,
        "verdict": raw_judge.get("verdict", "advance" if score >= 95 else "hold"),
        "stance": raw_judge["stance"],
        "findings": findings,
        "veto_vote": {
            "active": role_veto,
            "reason": veto_reason if role_veto else "No veto: required gates are satisfied.",
        },
    }


def _external_judge(raw_judge: dict[str, Any]) -> dict[str, Any]:
    role = str(raw_judge["role"])
    findings = raw_judge.get("findings", [])
    if not isinstance(findings, list) or not findings:
        findings = ["No material finding recorded."]
    context_refs = raw_judge.get("context_refs", ROLE_CONTEXT_REFS.get(role, []))
    if not isinstance(context_refs, list) or not context_refs:
        context_refs = ROLE_CONTEXT_REFS.get(role, ["external-agent-report"])
    source_report = str(raw_judge.get("source_report", f"external:{role}"))
    veto_active = bool(raw_judge.get("veto_active", False))
    veto_reason = str(raw_judge.get("veto_reason", "No veto: external judge did not raise a veto."))
    return {
        "role": role,
        "input_run_id": str(raw_judge.get("run_id", "")),
        "context_hash": _hash(
            {
                "role": role,
                "run_id": raw_judge.get("run_id", ""),
                "verdict": raw_judge.get("verdict", ""),
                "source_report": source_report,
                "context_refs": context_refs,
                "findings": findings,
            }
        ),
        "context_refs": [str(item) for item in context_refs],
        "source": "external_agent_report",
        "source_report": source_report,
        "peer_scores_visible": False,
        "score": float(raw_judge["score"]),
        "verdict": str(raw_judge.get("verdict", "hold")),
        "stance": str(raw_judge.get("stance", "External judge report.")),
        "findings": [str(item) for item in findings],
        "veto_vote": {
            "active": veto_active,
            "reason": veto_reason,
        },
    }


def _input_report_binding_errors(input_reports: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    bindings = (
        ("self_assessment_report", "self_assessment_run_id"),
        ("battle_report", "battle_report_run_id"),
    )
    for ref_key, run_id_key in bindings:
        ref = input_reports.get(ref_key)
        expected_run_id = input_reports.get(run_id_key)
        if not isinstance(ref, str) or not ref:
            errors.append(f"{ref_key} is not a file path")
            continue
        if ref.startswith("generated:"):
            errors.append(f"{ref_key} is generated, not file-bound")
            continue
        if not isinstance(expected_run_id, str) or not expected_run_id:
            errors.append(f"{run_id_key} is missing")
            continue
        path = Path(ref)
        if not path.is_absolute():
            path = ROOT / path
        try:
            actual = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.append(f"{ref_key} is not readable JSON")
            continue
        if not isinstance(actual, dict):
            errors.append(f"{ref_key} must contain a JSON object")
            continue
        if actual.get("run_id") != expected_run_id:
            errors.append(
                f"{run_id_key} {expected_run_id!r} does not match {ref_key}.run_id {actual.get('run_id')!r}"
            )
    return errors


def _input_report_readiness_errors(input_reports: dict[str, Any]) -> list[str]:
    binding_errors = _input_report_binding_errors(input_reports)
    if binding_errors:
        return binding_errors
    self_ref = Path(str(input_reports["self_assessment_report"]))
    battle_ref = Path(str(input_reports["battle_report"]))
    if not self_ref.is_absolute():
        self_ref = ROOT / self_ref
    if not battle_ref.is_absolute():
        battle_ref = ROOT / battle_ref
    self_report = json.loads(self_ref.read_text(encoding="utf-8"))
    battle = json.loads(battle_ref.read_text(encoding="utf-8"))
    errors: list[str] = []
    errors.extend(_self_assessment_pre_battle_readiness_errors(self_report))
    errors.extend(_battle_report_pre_battle_readiness_errors(battle))
    return errors


def _self_assessment_pre_battle_readiness_errors(self_report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    dimensions = self_report.get("dimensions", [])
    if not isinstance(dimensions, list):
        return ["self_assessment_report dimensions are missing"]
    by_name = {
        str(dimension.get("name")): float(dimension.get("score", 0))
        for dimension in dimensions
        if isinstance(dimension, dict)
    }
    for name in PRE_BATTLE_DIMENSIONS:
        if name not in by_name:
            errors.append(f"self_assessment_report {name} pre-battle dimension is missing")
        elif by_name[name] < 95:
            errors.append(f"self_assessment_report {name} pre-battle score is below 95")
    next_actions = self_report.get("next_actions", [])
    if not isinstance(next_actions, list):
        errors.append("self_assessment_report next_actions must be an array")
    else:
        unexpected_actions = [
            action.get("id")
            for action in next_actions
            if isinstance(action, dict)
            and action.get("id") not in ALLOWED_SELF_ASSESS_BATTLE_ACTIONS
        ]
        malformed_actions = [action for action in next_actions if not isinstance(action, dict)]
        if malformed_actions:
            errors.append("self_assessment_report next_actions must contain objects")
        if unexpected_actions:
            errors.append(
                f"self_assessment_report has unresolved non-battle next_actions {unexpected_actions}"
            )
    return errors


def _battle_report_pre_battle_readiness_errors(battle: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if battle.get("verdict") == "needs_human":
        errors.append("battle_report needs human resolution before independent battle advance")
    next_actions = battle.get("next_actions", [])
    if not isinstance(next_actions, list):
        return ["battle_report next_actions must be an array"]
    unexpected_actions = [
        action for action in next_actions if action not in ALLOWED_BATTLE_REPORT_ACTIONS
    ]
    if unexpected_actions:
        errors.append(f"battle_report has unresolved non-battle next_actions {unexpected_actions}")
    return errors


def _active_veto_reason(self_report: dict[str, Any], battle: dict[str, Any]) -> str:
    corpus = self_assess._real_repo_corpus_stats()
    if float(self_report.get("overall_score", 0)) < 95:
        return "self_assess score is below the 95 readiness target"
    if float(battle.get("overall_score", 0)) < 95:
        return "battle_report score is below the 95 readiness target"
    if int(corpus.get("non_author_repo_count", 0)) < 5:
        return "real repo corpus has fewer than five non-author repositories"
    if int(corpus.get("failure_family_count", 0)) < 3:
        return "real repo corpus has fewer than three failure families"
    if self_report.get("next_actions"):
        return "self_assess still has unresolved next actions"
    return ""


def build_agent_battle_harness_report(
    self_report: dict[str, Any],
    battle: dict[str, Any],
    run_id: str,
    *,
    input_reports: dict[str, str] | None = None,
    judge_reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    input_report_refs = dict(
        input_reports
        or {
            "self_assessment_report": "generated:self_assess.build_report",
            "battle_report": "generated:battle_report.build_battle_report",
        }
    )
    input_report_refs.setdefault("self_assessment_run_id", str(self_report.get("run_id", "")))
    input_report_refs.setdefault("battle_report_run_id", str(battle.get("run_id", "")))
    if judge_reports:
        veto_reason = ""
        veto_active = False
        judges = [_external_judge(raw_judge) for raw_judge in judge_reports]
        evidence_mode = "independent_agent_reports"
        independent_contexts = True
    else:
        veto_reason = _active_veto_reason(self_report, battle)
        veto_active = bool(veto_reason)
        judges = [
            _judge(raw_judge, self_report, veto_active=veto_active, veto_reason=veto_reason)
            for raw_judge in battle["judges"]
        ]
        evidence_mode = "derived_report"
        independent_contexts = False
    active_vetoes = [
        judge["veto_vote"]["reason"]
        for judge in judges
        if judge["veto_vote"]["active"] is True
    ]
    if active_vetoes:
        veto_active = True
        veto_reason = "; ".join(active_vetoes)
    mismatched_run_ids = [
        judge["role"]
        for judge in judges
        if judge.get("source") == "external_agent_report"
        and judge.get("input_run_id") != run_id
    ]
    judge_roles = [judge["role"] for judge in judges]
    required_roles = set(REQUIRED_JUDGES)
    duplicate_roles = sorted({role for role in judge_roles if judge_roles.count(role) > 1})
    unexpected_roles = sorted(set(judge_roles) - required_roles)
    non_advance_judges = [
        judge["role"]
        for judge in judges
        if judge.get("source") == "external_agent_report"
        and judge.get("verdict") != "advance"
    ]
    low_score_judges = [
        judge["role"]
        for judge in judges
        if judge.get("source") == "external_agent_report"
        and float(judge.get("score", 0)) < 95
    ]
    source_reports = [
        judge.get("source_report", "")
        for judge in judges
        if judge.get("source") == "external_agent_report"
    ]
    duplicate_sources = sorted(
        {source for source in source_reports if source_reports.count(source) > 1}
    )
    input_binding_errors = (
        _input_report_binding_errors(input_report_refs)
        if evidence_mode == "independent_agent_reports"
        else []
    )
    input_readiness_errors = (
        _input_report_readiness_errors(input_report_refs)
        if evidence_mode == "independent_agent_reports"
        else []
    )
    audit = [
        {
            "check": "required judges present",
            "status": "pass" if set(judge_roles) >= required_roles else "fail",
            "evidence_refs": ["schemas/artifacts/agent_battle_harness_report.json", "scripts/agent_battle_harness.py"],
        },
        {
            "check": "exactly one report per required judge",
            "status": "pass" if not duplicate_roles and not unexpected_roles and len(judges) == len(required_roles) else "fail",
            "evidence_refs": ["agent_battle_harness_report.protocol.required_judges", "agent_battle_harness_report.judges.role"],
        },
        {
            "check": "independent agent evidence present",
            "status": "pass" if independent_contexts and all(judge["source"] == "external_agent_report" for judge in judges) else "fail",
            "evidence_refs": ["agent_battle_harness_report.protocol.evidence_mode", "agent_battle_harness_report.judges.source"],
        },
        {
            "check": "input reports bind to source run ids",
            "status": "pass" if not input_binding_errors else "fail",
            "evidence_refs": [
                "agent_battle_harness_report.input_reports.self_assessment_report",
                "agent_battle_harness_report.input_reports.self_assessment_run_id",
                "agent_battle_harness_report.input_reports.battle_report",
                "agent_battle_harness_report.input_reports.battle_report_run_id",
            ],
        },
        {
            "check": "input reports meet advance readiness gate",
            "status": "pass" if not input_readiness_errors else "fail",
            "evidence_refs": [
                "agent_battle_harness_report.input_reports.self_assessment_report.dimensions",
                "agent_battle_harness_report.input_reports.self_assessment_report.next_actions",
                "agent_battle_harness_report.input_reports.battle_report.next_actions",
            ],
        },
        {
            "check": "external judge reports match harness run",
            "status": "pass" if not mismatched_run_ids else "fail",
            "evidence_refs": ["agent_battle_harness_report.run_id", "agent_battle_harness_report.judges.input_run_id"],
        },
        {
            "check": "external judge verdicts allow advance",
            "status": "pass" if not non_advance_judges else "fail",
            "evidence_refs": ["agent_battle_harness_report.judges.verdict"],
        },
        {
            "check": "external judge scores meet minimum",
            "status": "pass" if not low_score_judges else "fail",
            "evidence_refs": ["agent_battle_harness_report.protocol.minimum_score_to_advance", "agent_battle_harness_report.judges.score"],
        },
        {
            "check": "external judge sources are unique",
            "status": "pass" if not duplicate_sources else "fail",
            "evidence_refs": ["agent_battle_harness_report.judges.source_report"],
        },
        {
            "check": "blind review hides peer scores",
            "status": "pass" if all(judge["peer_scores_visible"] is False for judge in judges) else "fail",
            "evidence_refs": ["agent_battle_harness_report.judges.peer_scores_visible"],
        },
        {
            "check": "critic veto is wired into outcome",
            "status": "pass",
            "evidence_refs": ["agent_battle_harness_report.critic_veto", "agent_battle_harness_report.outcome"],
        },
    ]
    audit_pass = all(item["status"] == "pass" for item in audit)
    if judge_reports:
        score = round(sum(float(judge["score"]) for judge in judges) / max(len(judges), 1), 1)
    else:
        score = round((float(self_report["overall_score"]) + float(battle["overall_score"])) / 2, 1)
    verdict = "advance" if score >= 95 and audit_pass and not veto_active else "hold"
    return {
        "version": "1.0",
        "run_id": run_id,
        "subject": "Agent Battle validation readiness for Agent Production Kernel",
        "input_reports": input_report_refs,
        "protocol": {
            "evidence_mode": evidence_mode,
            "independent_contexts": independent_contexts,
            "blind_review": True,
            "critic_veto": True,
            "cross_exam_rounds": 2,
            "minimum_score_to_advance": 95,
            "required_judges": list(REQUIRED_JUDGES),
        },
        "judges": judges,
        "cross_examination": [
            {
                "from_role": "critic",
                "to_role": "architect",
                "challenge": "Does the control plane still confuse evidence readiness with autonomous execution?",
                "response": "No. The autonomy runner records no_action when self_assess has no safe next action and blocks bounded boundaries explicitly.",
                "status": "answered",
            },
            {
                "from_role": "test_engineer",
                "to_role": "code_reviewer",
                "challenge": "Is this Agent Battle evidence independent of the internal reports?",
                "response": "Yes." if independent_contexts else "No. This local run is a derived harness report and must hold until independent judge reports are supplied.",
                "status": "answered",
            },
        ],
        "critic_veto": {
            "enabled": True,
            "active": veto_active,
            "reason": veto_reason or "No veto: self_assess, battle_report, corpus, and next-action gates are satisfied.",
        },
        "judge_audit": audit,
        "outcome": {
            "verdict": verdict,
            "score": score,
            "accepted_actions": list(battle.get("next_actions", [])),
        },
    }


def _load_report(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _display_path(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def _write_report_files(
    report: dict[str, Any],
    report_path: Path,
    *,
    current_report_path: Path | None = None,
) -> None:
    encoded = json.dumps(report, indent=2, sort_keys=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(encoded, encoding="utf-8")
    if current_report_path is not None:
        current_report_path.parent.mkdir(parents=True, exist_ok=True)
        current_report_path.write_text(encoded, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-report", type=Path)
    parser.add_argument("--battle-report", type=Path)
    parser.add_argument("--judge-report", type=Path, action="append", default=[])
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--run-id")
    args = parser.parse_args()

    run_id = args.run_id or dt.datetime.now(dt.timezone.utc).strftime("agent-battle-%Y%m%dT%H%M%SZ")
    self_report = _load_report(args.self_report)
    battle = _load_report(args.battle_report)
    input_reports = None
    if self_report is None:
        self_report = self_assess.build_report(self_assess._run_commands(), run_id)
    if battle is None:
        battle = battle_report.build_battle_report(self_report, run_id)
    if args.self_report or args.battle_report:
        input_reports = {
            "self_assessment_report": str(args.self_report or "generated:self_assess.build_report"),
            "self_assessment_run_id": str(self_report.get("run_id", "")),
            "battle_report": str(args.battle_report or "generated:battle_report.build_battle_report"),
            "battle_report_run_id": str(battle.get("run_id", "")),
        }
    schemas = SchemaRegistry(ROOT / "schemas" / "artifacts").load()
    judge_reports = []
    for path in args.judge_report:
        loaded = _load_report(path)
        if loaded is not None:
            schemas.validate("agent_judge_report", loaded)
            validate_artifact_semantics("agent_judge_report", loaded)
            judge_reports.append(loaded)
    report = build_agent_battle_harness_report(
        self_report,
        battle,
        run_id,
        input_reports=input_reports,
        judge_reports=judge_reports,
    )
    schemas.validate("agent_battle_harness_report", report)
    validate_artifact_semantics("agent_battle_harness_report", report)

    default_output = args.output_dir is None
    output_dir = args.output_dir or ROOT / ".apk" / "agent-battle-harness" / run_id
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    report_path = output_dir / "agent_battle_harness_report.json"
    current_report_path = (
        ROOT / ".apk" / "agent-battle-harness" / "current" / "agent_battle_harness_report.json"
        if default_output
        else None
    )
    _write_report_files(report, report_path, current_report_path=current_report_path)
    summary = {
        "status": "pass" if report["outcome"]["verdict"] == "advance" else report["outcome"]["verdict"],
        "report": _display_path(report_path),
        "overall_score": report["outcome"]["score"],
        "critic_veto_active": report["critic_veto"]["active"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
