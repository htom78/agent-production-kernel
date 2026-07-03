#!/usr/bin/env python3
"""Run a real public repository bug through the bug-fix kernel."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apkernel import (
    CheckpointStore,
    ContractError,
    FunctionStageExecutor,
    PipelineManifest,
    Reviewer,
    RolePolicy,
    RunEngine,
    SchemaRegistry,
    StageExecutionContext,
)


DEFAULT_REPO = ROOT.parent / "apk-real-bug-demo"
RUN_ID = "real-repo-privacy-cache"
TEST_COMMAND = "PYTHONPATH=src python3 -m unittest discover -s tests -v"


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)


def _git(repo: Path, *args: str) -> str:
    result = _run(["git", *args], repo)
    if result.returncode != 0:
        raise ContractError(result.stderr or result.stdout)
    return result.stdout.strip()


def _ensure_clean_worktree(repo: Path) -> None:
    status = _git(repo, "status", "--porcelain")
    if status:
        raise ContractError("real bug scenario requires a clean external repo worktree")


def _command_evidence(
    command: str,
    result: subprocess.CompletedProcess[str],
    *,
    commit_sha: str,
    expected_status: str,
) -> dict[str, Any]:
    status = "pass" if result.returncode == 0 else "fail"
    if status != expected_status:
        raise ContractError(
            f"{command!r} expected {expected_status}, got {status}: "
            f"{result.stdout}\n{result.stderr}"
        )
    return {
        "command": command,
        "status": status,
        "exit_code": result.returncode,
        "stdout_digest": _sha256(result.stdout),
        "stderr_digest": _sha256(result.stderr),
        "commit_sha": commit_sha,
        "tool_version": f"python {sys.version.split()[0]}",
        "timestamp": _timestamp(),
        "artifact_refs": ["bug_report" if status == "fail" else "patch_plan"],
    }


def _approval_decision(
    context: StageExecutionContext,
    approver_role: str,
    evidence_refs: list[str],
) -> dict[str, Any]:
    return {
        "version": "1.0",
        "run_id": context.run_id,
        "stage": context.stage_name,
        "approver_role": approver_role,
        "decision": "approved",
        "reason": f"{approver_role} approved real repository evidence for {context.stage_name}.",
        "evidence_refs": evidence_refs,
    }


def _checkout(repo: Path, ref: str) -> None:
    result = _run(["git", "checkout", ref], repo)
    if result.returncode != 0:
        raise ContractError(result.stderr or result.stdout)


def _capture_repo_evidence(repo: Path, bug_branch: str, fix_branch: str) -> dict[str, Any]:
    _ensure_clean_worktree(repo)
    original_branch = _git(repo, "branch", "--show-current")
    original_head = _git(repo, "rev-parse", "HEAD")
    repo_url = _git(repo, "remote", "get-url", "origin").removesuffix(".git")
    try:
        _checkout(repo, bug_branch)
        bug_commit = _git(repo, "rev-parse", "HEAD")
        failing_result = _run(["bash", "-lc", TEST_COMMAND], repo)
        failing_evidence = _command_evidence(
            TEST_COMMAND,
            failing_result,
            commit_sha=bug_commit,
            expected_status="fail",
        )

        _checkout(repo, fix_branch)
        fix_commit = _git(repo, "rev-parse", "HEAD")
        passing_result = _run(["bash", "-lc", TEST_COMMAND], repo)
        passing_evidence = _command_evidence(
            TEST_COMMAND,
            passing_result,
            commit_sha=fix_commit,
            expected_status="pass",
        )
    finally:
        if original_branch:
            _checkout(repo, original_branch)
        else:
            _checkout(repo, original_head)

    return {
        "repo_url": repo_url,
        "repo_path": str(repo),
        "bug_branch": bug_branch,
        "fix_branch": fix_branch,
        "bug_commit": bug_commit,
        "fix_commit": fix_commit,
        "failing_command": failing_evidence,
        "passing_command": passing_evidence,
    }


def _real_bug_executor(evidence: dict[str, Any]) -> FunctionStageExecutor:
    def require(context: StageExecutionContext, artifact_name: str) -> dict[str, Any]:
        artifact = context.prior_artifacts.get(artifact_name)
        if artifact is None:
            raise ContractError(f"{context.stage_name} requires {artifact_name}")
        return artifact

    def reproduce(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        return {
            "bug_report": {
                "version": "1.0",
                "issue_id": "REAL-BUG-001",
                "observed_behavior": "A public profile request reuses a private cached profile and exposes email.",
                "expected_behavior": "Public profile requests must not include private fields.",
                "reproduction_steps": [
                    f"Clone {evidence['repo_url']}",
                    f"Checkout {evidence['bug_branch']} at {evidence['bug_commit']}",
                    f"Run {TEST_COMMAND}",
                ],
                "evidence": [
                    f"repo_url={evidence['repo_url']}",
                    f"bug_commit={evidence['bug_commit']}",
                    f"failing_exit_code={evidence['failing_command']['exit_code']}",
                    f"stdout_digest={evidence['failing_command']['stdout_digest']}",
                    f"stderr_digest={evidence['failing_command']['stderr_digest']}",
                ],
            }
        }

    def root_cause(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        bug_report = require(context, "bug_report")
        return {
            "root_cause_report": {
                "version": "1.0",
                "issue_id": bug_report["issue_id"],
                "root_cause": "The profile cache key used only user_id and omitted include_private.",
                "failure_family": "state_key_under_specification",
                "affected_files": ["src/privacy_cache_lab/profile_cache.py"],
                "evidence": [
                    "main branch fails the privacy cache regression",
                    "fix branch passes after adding include_private to the key",
                ],
                "confidence": 0.95,
            }
        }

    def system_fault(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        root_report = require(context, "root_cause_report")
        return {
            "system_fault_report": {
                "version": "1.0",
                "issue_id": root_report["issue_id"],
                "fix_system_first": True,
                "system_faults": [
                    {
                        "category": "missing_privacy_scope_regression",
                        "cause": "The repo needed a regression that requests private and public variants sequentially.",
                        "prevention": "Keep a failing-then-passing privacy cache regression in the public bug fixture.",
                    }
                ],
            },
            "decision_log": {
                "version": "1.0",
                "run_id": RUN_ID,
                "decisions": [
                    {
                        "decision_id": "real-d-001",
                        "stage": "system_fault",
                        "category": "system_first_fix",
                        "subject": "Whether this counts as a real repo bug scenario",
                        "options_considered": [
                            {
                                "option_id": "synthetic_only",
                                "label": "Use only internal demo artifacts",
                                "score": 0.2,
                                "reason": "Would not prove the kernel against a public repository.",
                                "rejected_because": "Goal requires a real repo bug scenario.",
                            },
                            {
                                "option_id": "public_repo_fixture",
                                "label": "Use a public repo with failing and fixed branches",
                                "score": 0.88,
                                "reason": "Creates external, reproducible command evidence.",
                            },
                        ],
                        "selected": "public_repo_fixture",
                        "reason": "The bug is public, reproducible, and has command evidence before and after the fix.",
                        "user_visible": True,
                        "user_approved": True,
                        "confidence": 0.9,
                        "artifact_refs": ["root_cause_report", "system_fault_report"],
                        "checkpoint_refs": [],
                    }
                ],
            },
            "approval_decision": _approval_decision(
                context,
                "release_captain",
                ["system_fault_report", "decision_log"],
            ),
        }

    def product_patch(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        fault_report = require(context, "system_fault_report")
        return {
            "patch_plan": {
                "version": "1.0",
                "issue_id": fault_report["issue_id"],
                "risk_level": "low",
                "changes": [
                    "Change the profile cache key from user_id to (user_id, include_private).",
                    "Keep the privacy regression test as the guard.",
                ],
                "verification_commands": [TEST_COMMAND],
                "rollback_plan": "Revert the fix branch commit and restore the user_id-only cache key.",
            }
        }

    def verification(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        patch_plan = require(context, "patch_plan")
        return {
            "verification_report": {
                "version": "1.0",
                "issue_id": patch_plan["issue_id"],
                "overall_status": "pass",
                "commands": [evidence["passing_command"]],
            },
            "approval_decision": _approval_decision(
                context,
                "release_captain",
                ["verification_report"],
            ),
        }

    def regression(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        report = require(context, "verification_report")
        return {
            "regression_case": {
                "version": "1.0",
                "id": "reg-real-repo-privacy-cache",
                "source_issue_id": report["issue_id"],
                "fixture": "public repo requests private profile before public profile",
                "expected_guard": "public profile does not include email after private profile request",
                "replay_command": "python3 scripts/run_real_bug_scenario.py",
            },
            "approval_decision": _approval_decision(
                context,
                "release_captain",
                ["regression_case"],
            ),
        }

    def knowledge_update(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        _ = require(context, "regression_case")
        return {
            "knowledge_update": {
                "version": "1.0",
                "target": "software-pack/real-repo-dogfood",
                "update_type": "review_rule",
                "trigger": "public repo bug dogfood run",
                "summary": "A real bug run must preserve public repo URL, bug branch, fix branch, failing command evidence, passing command evidence, and kernel checkpoints.",
            },
            "approval_decision": _approval_decision(
                context,
                "release_captain",
                ["knowledge_update"],
            ),
        }

    return FunctionStageExecutor(
        {
            "reproduce": reproduce,
            "root_cause": root_cause,
            "system_fault": system_fault,
            "product_patch": product_patch,
            "verification": verification,
            "regression": regression,
            "knowledge_update": knowledge_update,
        }
    )


def run_real_bug(repo: Path, bug_branch: str, fix_branch: str) -> dict[str, Any]:
    schemas = SchemaRegistry(ROOT / "schemas" / "artifacts").load()
    manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
    reviewer = Reviewer(schemas)
    role_policy = RolePolicy.load(ROOT / "packs" / "software" / "roles.json")
    store = CheckpointStore(ROOT / ".apk" / "real-bug-runs", schemas, role_policy)
    evidence = _capture_repo_evidence(repo, bug_branch, fix_branch)
    engine = RunEngine(store, reviewer)
    checkpoints = engine.run_with_executor(
        manifest,
        RUN_ID,
        _real_bug_executor(evidence),
        scenario=evidence,
        metadata={
            "demo": False,
            "execution_mode": "stage_executor",
            "repo_url": evidence["repo_url"],
        },
    )
    report = {
        "version": "1.0",
        "run_id": RUN_ID,
        "repo_url": evidence["repo_url"],
        "repo_path": evidence["repo_path"],
        "bug_branch": evidence["bug_branch"],
        "fix_branch": evidence["fix_branch"],
        "bug_commit": evidence["bug_commit"],
        "fix_commit": evidence["fix_commit"],
        "failing_command": evidence["failing_command"],
        "passing_command": evidence["passing_command"],
        "checkpoint_refs": [str(Path(path).relative_to(ROOT)) for path in checkpoints.values()],
        "public_refs": [
            evidence["repo_url"],
            f"{evidence['repo_url']}/tree/{evidence['fix_branch']}",
        ],
    }
    schemas.validate("real_repo_bug_run", report)
    report_path = ROOT / ".apk" / "real-bug-runs" / RUN_ID / "real_repo_bug_run.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "status": "pass",
        "report": str(report_path.relative_to(ROOT)),
        "repo_url": evidence["repo_url"],
        "bug_commit": evidence["bug_commit"],
        "fix_commit": evidence["fix_commit"],
        "checkpoints": report["checkpoint_refs"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--bug-branch", default="main")
    parser.add_argument("--fix-branch", default="fix/privacy-cache-key")
    args = parser.parse_args()

    summary = run_real_bug(args.repo.resolve(), args.bug_branch, args.fix_branch)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
