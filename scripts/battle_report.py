#!/usr/bin/env python3
"""Generate a structured multi-judge battle report."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from apkernel import SchemaRegistry
import self_assess


def _dimension_score(self_report: dict[str, Any], name: str) -> float:
    for dimension in self_report["dimensions"]:
        if dimension["name"] == name:
            return float(dimension["score"])
    return 0.0


def _average(values: list[float]) -> float:
    return round(sum(values) / len(values), 1)


def _has_git_metadata() -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def build_battle_report(self_report: dict[str, Any], run_id: str) -> dict[str, Any]:
    contract = _dimension_score(self_report, "contract_integrity")
    execution = _dimension_score(self_report, "execution_reality")
    evidence = _dimension_score(self_report, "evidence_trust")
    replay = _dimension_score(self_report, "replay_strength")
    multi_agent = _dimension_score(self_report, "multi_agent_readiness")
    autonomy = _dimension_score(self_report, "autonomy_loop")
    evaluation = _dimension_score(self_report, "evaluation_independence")
    real_bug_run = self_assess._has_real_bug_run_evidence()
    branch_replay = self_assess._has_branch_level_replay()
    bounded_runner = self_assess._has_bounded_autonomy_runner()
    real_repo_corpus = self_assess._real_repo_corpus_count()
    corpus_stats = self_assess._real_repo_corpus_stats()
    failure_family_count = int(corpus_stats.get("failure_family_count", 0))
    corpus_target_met = real_repo_corpus >= 5 and failure_family_count >= 3
    second_pack = self_assess._has_second_domain_pack()
    semantic_fake_green_tests = self_assess._has_semantic_fake_green_tests()
    independent_battle = self_assess._has_independent_agent_battle_evidence()
    independent_battle_attempt = self_assess._has_current_independent_agent_battle_attempt()
    has_git_metadata = _has_git_metadata()
    critic_stance = "The system has a public real-repo proof corpus; the remaining risk is freshness and release discipline."
    critic_concerns = ["Demo executors are deterministic and do not yet call external tools or models."]
    critic_actions = ["Maintain the verified corpus and rerun gates before release."]
    if not independent_battle and independent_battle_attempt:
        critic_stance = "The system has current independent Agent Battle evidence, but that battle held or vetoed advance."
        critic_concerns = ["The latest independent Agent Battle found findings that must be repaired before readiness can advance."]
        critic_actions = ["Address the current independent Agent Battle hold findings."]
    elif not independent_battle:
        critic_stance = "The system has a valid derived battle report, but not an independent multi-agent battle artifact."
        critic_concerns = ["The local Agent Battle harness must hold until external judge reports are supplied."]
        critic_actions = ["Run and preserve an independent multi-agent battle report for APK."]
    if not corpus_target_met:
        critic_stance = "The system has a public real-repo bug proof, but needs broader branch and autonomy coverage."
        critic_actions = ["Expand to five non-author real repository bug scenarios after human approval."]
    if not semantic_fake_green_tests:
        critic_actions = ["Add schema-valid semantic false-green tests."]
    if not second_pack:
        critic_actions = ["Add one non-software domain pack to prove pack generality."]
    if not branch_replay:
        critic_actions = ["Add branch-level replay for awaiting_human, failed, and blocked states."]
    if not bounded_runner and branch_replay:
        critic_actions = ["Add a bounded safe-action runner for self-assessed next_actions."]
    if not real_bug_run:
        critic_stance = "The system has improved from contract skeleton to dogfooded kernel, but still needs real-world task runs."
        critic_concerns = ["Demo executors are deterministic and do not yet call external tools or models."]
        critic_actions = ["Run the kernel on one real repository bug and capture the resulting artifacts."]

    judges = [
        {
            "role": "architect",
            "score": _average([contract, execution, multi_agent]),
            "stance": "The kernel is now a coherent control-plane skeleton with real executor and role-policy paths.",
            "evidence_refs": ["apkernel/core.py", "apkernel/software.py", "packs/software/roles.json"],
            "concerns": ["Autonomy is still proposal-driven rather than fully executing queued work."],
            "recommended_actions": ["Add an explicit approval/runtime boundary for high-risk operations."],
        },
        {
            "role": "test_engineer",
            "score": _average([evidence, replay, evaluation]),
            "stance": "The verifier and replay suite now cover positive checkpoints and negative replay mutations.",
            "evidence_refs": ["scripts/verify.py", "scripts/replay_regressions.py", "tests/test_kernel.py"],
            "concerns": ["Golden coverage is still scenario-level and does not yet measure broad branch coverage."],
            "recommended_actions": ["Add branch-level replay cases for awaiting_human, failed, and blocked statuses."],
        },
        {
            "role": "code_reviewer",
            "score": _average([contract, evidence, replay]),
            "stance": "The strongest prior false-green paths are guarded by schema, semantic checks, and clean-state tests.",
            "evidence_refs": ["schemas/artifacts", "apkernel/replay.py", "tests/test_kernel.py"],
            "concerns": (
                ["Git metadata is available; release evidence must still be bound to the current independent battle cycle."]
                if has_git_metadata
                else ["There is no git metadata in this checkout, so diff hygiene cannot be verified locally."]
            ),
            "recommended_actions": (
                ["Preserve a current independent battle harness before claiming advance."]
                if has_git_metadata
                else ["Run the same gates inside a real repository checkout before release."]
            ),
        },
        {
            "role": "critic",
            "score": _average([execution, replay, autonomy, evaluation]),
            "stance": critic_stance,
            "evidence_refs": ["docs/SELF_DRIVING.md", "scripts/self_assess.py", "scripts/run_real_bug_scenario.py", "examples/golden_*.json"],
            "concerns": critic_concerns,
            "recommended_actions": critic_actions,
        },
    ]
    overall = round(sum(judge["score"] for judge in judges) / len(judges), 1)
    verdict = "advance" if overall >= 95 and independent_battle else "hold"
    if overall < 50:
        verdict = "needs_human"
    dissent = [
        "Autonomy still proposes and verifies work; it does not yet safely schedule bounded work itself.",
    ]
    if real_bug_run and corpus_target_met:
        dissent.append(f"The real-repo proof corpus meets the count target with {real_repo_corpus} non-author repo(s) and {failure_family_count} failure families; freshness still needs periodic reruns.")
    elif real_bug_run:
        dissent.append(f"The real-repo proof corpus has {real_repo_corpus} repo(s); generality needs at least five non-author repos and three failure families.")
    else:
        dissent.insert(0, "The system is not yet proven on a real external repository bug.")
    if not branch_replay:
        dissent.append("Replay does not yet cover awaiting_human, failed, and blocked checkpoint branches.")
    if not bounded_runner:
        dissent.append("The system does not yet checkpoint bounded autonomy decisions.")
    if not independent_battle and independent_battle_attempt:
        dissent.append("A current independent Agent Battle exists, but it held or vetoed advance.")
    elif not independent_battle:
        dissent.append("The Agent Battle harness is derived from internal reports and cannot advance without external judge reports.")

    next_actions = []
    if not independent_battle and independent_battle_attempt:
        next_actions.append("Address the current independent Agent Battle hold findings.")
    elif not independent_battle:
        next_actions.append("Run and preserve an independent multi-agent battle report for APK.")
    elif not branch_replay:
        next_actions.append("Add branch-level replay for awaiting_human, failed, and blocked states.")
    if not next_actions:
        if not real_bug_run:
            next_actions.append("Run one real bug-fix scenario through the kernel and preserve the artifacts.")
        elif not second_pack:
            next_actions.append("Add one non-software domain pack to prove pack generality.")
        elif not semantic_fake_green_tests:
            next_actions.append("Add schema-valid semantic false-green tests.")
        elif not bounded_runner:
            next_actions.append("Add a bounded safe-action runner for self-assessed next_actions.")
        elif not corpus_target_met:
            next_actions.append("Expand to five non-author real repository bug scenarios after human approval.")
        else:
            next_actions.append("Maintain the verified corpus and rerun gates before release.")

    return {
        "version": "1.0",
        "run_id": run_id,
        "evidence_fingerprint": str(self_report.get("evidence_fingerprint", self_assess._current_evidence_fingerprint())),
        "subject": "Agent Production Kernel self-driving readiness",
        "overall_score": overall,
        "verdict": verdict,
        "judges": judges,
        "consensus": [
            "The kernel should keep advancing through evidence-backed self-assessment.",
            "Checkpoint replay and role-policy enforcement are now first-class verification surfaces.",
        ],
        "dissent": dissent,
        "next_actions": next_actions,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    run_id = dt.datetime.now(dt.timezone.utc).strftime("battle-%Y%m%dT%H%M%SZ")
    self_report = self_assess.build_report(self_assess._run_commands(), run_id)
    report = build_battle_report(self_report, run_id)
    schemas = SchemaRegistry(ROOT / "schemas" / "artifacts").load()
    schemas.validate("battle_report", report)

    output_dir = args.output_dir or ROOT / ".apk" / "battle-reports" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "battle_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    summary = {
        "status": "pass" if report["verdict"] == "advance" else report["verdict"],
        "report": str(report_path.relative_to(ROOT)),
        "overall_score": report["overall_score"],
        "next_action": report["next_actions"][0],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if report["verdict"] == "advance" else 1


if __name__ == "__main__":
    raise SystemExit(main())
