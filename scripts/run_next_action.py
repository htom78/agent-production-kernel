#!/usr/bin/env python3
"""Run the next self-assessed action inside explicit safety bounds."""

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
sys.path.insert(0, str(ROOT / "scripts"))

from apkernel import CheckpointStore, ContractError, PipelineManifest, Reviewer, RolePolicy, SchemaRegistry, load_json
import self_assess


SAFE_ACTIONS = {"verify-current-state"}
BOUNDARY_ACTIONS = {
    "run-real-bug-scenario": (
        "real external repository boundary requires user confirmation",
        ["external_repo", "requires_user_confirmation"],
    ),
    "expand-real-repo-corpus": (
        "additional real repository boundary requires user confirmation",
        ["external_repo", "requires_user_confirmation"],
    ),
}
ALLOWED_COMMANDS = {
    "python3 scripts/verify.py": [sys.executable, "scripts/verify.py"],
    "python3 scripts/replay_regressions.py": [sys.executable, "scripts/replay_regressions.py"],
    "python3 -m unittest discover -s tests -v": [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-v",
    ],
    "python3 -m compileall apkernel scripts tests": [
        sys.executable,
        "-m",
        "compileall",
        "apkernel",
        "scripts",
        "tests",
    ],
}
EMPTY_ACTION = {
    "id": "none",
    "title": "No next action",
    "priority": "P2",
    "target_files": [],
    "verification_commands": [],
}


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _commit_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "workspace-uncommitted"
    if result.returncode != 0:
        return "workspace-uncommitted"
    return result.stdout.strip() or "workspace-uncommitted"


def _command_evidence(command: str, result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "command": command,
        "status": "pass" if result.returncode == 0 else "fail",
        "exit_code": result.returncode,
        "stdout_digest": _sha256(result.stdout),
        "stderr_digest": _sha256(result.stderr),
        "commit_sha": _commit_sha(),
        "tool_version": f"python {sys.version.split()[0]}",
        "timestamp": _timestamp(),
        "artifact_refs": ["autonomy_run_report"],
    }


def _load_action(path: Path | None, run_id: str) -> dict[str, Any]:
    if path is not None:
        raw = load_json(path)
        action = raw.get("action", raw)
        if not isinstance(action, dict):
            raise ContractError(f"{path} must contain an action object")
        _validate_action(action, source=str(path))
        return action
    report = self_assess.build_report(self_assess._run_commands(), run_id)
    actions = report.get("next_actions", [])
    if not actions:
        return dict(EMPTY_ACTION)
    action = _select_action(actions)
    if not isinstance(action, dict):
        raise ContractError("self_assess next_actions must contain objects")
    _validate_action(action, source="self_assess.next_actions")
    return action


def _validate_action(action: dict[str, Any], *, source: str) -> None:
    for field in ("id", "title", "priority"):
        if not isinstance(action.get(field), str) or not action[field]:
            raise ContractError(f"{source} action.{field} must be a non-empty string")
    if action["priority"] not in {"P0", "P1", "P2"}:
        raise ContractError(f"{source} action.priority must be P0, P1, or P2")
    for field in ("target_files", "verification_commands"):
        value = action.get(field)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ContractError(f"{source} action.{field} must be a list of strings")


def _select_action(actions: list[Any]) -> dict[str, Any]:
    boundary_candidate: dict[str, Any] | None = None
    for action in actions:
        if not isinstance(action, dict):
            raise ContractError("self_assess next_actions must contain objects")
        reason, _ = _boundary_for(action)
        if reason is None:
            return action
        if boundary_candidate is None:
            boundary_candidate = action
    return boundary_candidate or dict(EMPTY_ACTION)


def _boundary_for(action: dict[str, Any]) -> tuple[str | None, list[str]]:
    action_id = str(action.get("id", ""))
    if action_id in BOUNDARY_ACTIONS:
        return BOUNDARY_ACTIONS[action_id]
    target_text = " ".join(str(item) for item in action.get("target_files", []))
    if "integrations" in target_text:
        return "external integration boundary requires human confirmation", ["external_integration"]
    if action_id not in SAFE_ACTIONS:
        return f"no bounded handler registered for action {action_id!r}", ["unknown_action_handler"]
    return None, []


def _run_allowed_commands(action: dict[str, Any]) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    for command in action.get("verification_commands", []):
        if command not in ALLOWED_COMMANDS:
            raise ContractError(f"command is not allowlisted: {command}")
        result = subprocess.run(
            ALLOWED_COMMANDS[command],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        commands.append(_command_evidence(command, result))
    return commands


def build_autonomy_report(action: dict[str, Any], run_id: str) -> dict[str, Any]:
    reason, boundaries = _boundary_for(action)
    commands: list[dict[str, Any]] = []
    if action.get("id") == "none":
        decision = "no_action"
        reason = "self_assess returned no next action"
    elif reason is not None:
        decision = "blocked"
    else:
        commands = _run_allowed_commands(action)
        decision = "executed" if all(command["status"] == "pass" for command in commands) else "failed"
        reason = "completed allowlisted local commands" if decision == "executed" else "one or more allowlisted commands failed"

    report = {
        "version": "1.0",
        "run_id": run_id,
        "selected_action": {
            "id": str(action.get("id", "")),
            "title": str(action.get("title", "")),
            "priority": str(action.get("priority", "P2")),
            "target_files": list(action.get("target_files", [])),
            "verification_commands": list(action.get("verification_commands", [])),
        },
        "decision": decision,
        "stop_reason": reason or "completed",
        "boundaries": boundaries,
        "commands": commands,
    }
    SchemaRegistry(ROOT / "schemas" / "artifacts").load().validate("autonomy_run_report", report)
    return report


def write_autonomy_checkpoint(report: dict[str, Any], output_root: Path) -> Path:
    schemas = SchemaRegistry(ROOT / "schemas" / "artifacts").load()
    manifest = PipelineManifest.load(ROOT / "pipelines" / "kernel-autonomy.json")
    reviewer = Reviewer(schemas)
    role_policy = RolePolicy.load(ROOT / "packs" / "software" / "roles.json")
    review = reviewer.review(
        manifest,
        "select_next_action",
        {"autonomy_run_report": report},
        reviewer_roles=role_policy.reviewers_for_stage("select_next_action"),
    )
    store = CheckpointStore(output_root, schemas, role_policy)
    checkpoint_status = {
        "executed": "completed",
        "failed": "failed",
        "blocked": "blocked",
        "no_action": "completed",
    }[report["decision"]]
    return store.write(
        manifest,
        report["run_id"],
        "select_next_action",
        status=checkpoint_status,
        artifacts={"autonomy_run_report": report},
        review=review,
        metadata={"execution_mode": "bounded_runner"},
        actor_role="autonomy_runner",
    )


def run_next_action(
    *,
    action_file: Path | None,
    run_id: str,
    output_root: Path,
) -> dict[str, Any]:
    action = _load_action(action_file, run_id)
    report = build_autonomy_report(action, run_id)
    checkpoint_path = write_autonomy_checkpoint(report, output_root)
    report_path = output_root / run_id / "autonomy_run_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "status": report["decision"],
        "report": str(report_path.relative_to(ROOT)) if report_path.is_relative_to(ROOT) else str(report_path),
        "checkpoint": str(checkpoint_path.relative_to(ROOT)) if checkpoint_path.is_relative_to(ROOT) else str(checkpoint_path),
        "selected_action": report["selected_action"]["id"],
        "stop_reason": report["stop_reason"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action-file", type=Path)
    parser.add_argument("--output-root", type=Path, default=ROOT / ".apk" / "autonomy-runs")
    parser.add_argument("--run-id", default=dt.datetime.now(dt.timezone.utc).strftime("autonomy-%Y%m%dT%H%M%SZ"))
    args = parser.parse_args()

    summary = run_next_action(
        action_file=args.action_file,
        run_id=args.run_id,
        output_root=args.output_root,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] in {"executed", "no_action"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
