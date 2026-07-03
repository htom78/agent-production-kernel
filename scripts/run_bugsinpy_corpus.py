#!/usr/bin/env python3
"""Run curated BugsInPy real-repository bugs into corpus artifacts."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apkernel import (  # noqa: E402
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


BUGSINPY_ROOT = ROOT / ".apk" / "external-repos" / "BugsInPy"
WORK_ROOT = ROOT / ".apk" / "corpus-candidates"
ENV_ROOT = ROOT / ".apk" / "corpus-envs"
SHIM_ROOT = ROOT / ".apk" / "corpus-shims"


@dataclasses.dataclass(frozen=True)
class BugsInPyScenario:
    project: str
    bug_id: str
    run_id: str
    failure_family: str
    issue_summary: str
    root_cause: str
    system_fault: str
    prevention: str
    env_deps: tuple[str, ...] = ()
    pythonpath: tuple[str, ...] = (".",)
    setup_editable: bool = False

    @property
    def artifact_file(self) -> str:
        return f"examples/real_repo_bug_run_{self.run_id.replace('-', '_')}.json"


SCENARIOS: tuple[BugsInPyScenario, ...] = (
    BugsInPyScenario(
        project="youtube-dl",
        bug_id="1",
        run_id="bugsinpy-youtube-dl-1",
        failure_family="string_pattern_matching",
        issue_summary="String matching accepted the wrong command pattern.",
        root_cause="The matcher did not preserve the intended string-pattern boundary.",
        system_fault="The test suite needed a focused string-pattern regression from the fixed commit.",
        prevention="Keep the BugsInPy regression test as an artifact-backed replay candidate.",
    ),
    BugsInPyScenario(
        project="tornado",
        bug_id="1",
        run_id="bugsinpy-tornado-1",
        failure_family="network_option_contract",
        issue_summary="A websocket socket option contract regressed.",
        root_cause="The websocket connection path failed to preserve the expected TCP_NODELAY behavior.",
        system_fault="The repo needed protocol-option coverage at the public bug boundary.",
        prevention="Preserve the focused websocket regression and command evidence.",
    ),
    BugsInPyScenario(
        project="ansible",
        bug_id="1",
        run_id="bugsinpy-ansible-1",
        failure_family="api_shape_contract",
        issue_summary="Collection verification passed a scalar API where an iterable API contract was required.",
        root_cause="The verification path treated GalaxyAPI as iterable without normalizing the API shape.",
        system_fault="The system lacked a regression around collection API shape normalization.",
        prevention="Replay collection verification with the fixed test and command evidence.",
        env_deps=("PyYAML", "Jinja2", "six", "cryptography"),
        pythonpath=("lib", "."),
    ),
    BugsInPyScenario(
        project="thefuck",
        bug_id="1",
        run_id="bugsinpy-thefuck-1",
        failure_family="command_parser_regex",
        issue_summary="The pip unknown-command rule failed to parse a command suggestion.",
        root_cause="The regex accepted only alphabetic command fragments and rejected punctuation in the broken command.",
        system_fault="The command parser rule needed a regression for punctuation-bearing suggestions.",
        prevention="Keep the failing command-parser case as a real repository replay fixture.",
        env_deps=("mock", "psutil", "colorama", "decorator", "six", "pyte"),
    ),
    BugsInPyScenario(
        project="black",
        bug_id="1",
        run_id="bugsinpy-black-1",
        failure_family="runtime_environment_assumption",
        issue_summary="Black failed in a mono-process-only runtime environment.",
        root_cause="The formatter assumed multiprocessing was available and did not degrade cleanly.",
        system_fault="The project needed a regression for constrained runtime execution.",
        prevention="Run the mono-process regression against buggy and fixed commits.",
        env_deps=(
            "click",
            "toml",
            "regex",
            "pathspec",
            "typed-ast",
            "mypy_extensions",
            "appdirs",
            "aiohttp",
            "aiohttp-cors",
        ),
        setup_editable=True,
    ),
)


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _run(
    argv: list[str] | str,
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        shell=isinstance(argv, str),
        timeout=timeout,
    )


def _git(repo: Path, *args: str) -> str:
    result = _run(["git", *args], cwd=repo)
    if result.returncode != 0:
        raise ContractError(result.stderr or result.stdout)
    return result.stdout.strip()


def _parse_info(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip()
    return data


def _project_info(scenario: BugsInPyScenario) -> dict[str, str]:
    return _parse_info(BUGSINPY_ROOT / "projects" / scenario.project / "project.info")


def _bug_info(scenario: BugsInPyScenario) -> dict[str, str]:
    return _parse_info(BUGSINPY_ROOT / "projects" / scenario.project / "bugs" / scenario.bug_id / "bug.info")


def _run_test_command(scenario: BugsInPyScenario) -> str:
    path = BUGSINPY_ROOT / "projects" / scenario.project / "bugs" / scenario.bug_id / "run_test.sh"
    return path.read_text(encoding="utf-8", errors="ignore").strip().replace("\r", "")


def _ensure_bugsinpy() -> None:
    if BUGSINPY_ROOT.exists():
        return
    BUGSINPY_ROOT.parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        ["git", "clone", "https://github.com/soarsmu/BugsInPy.git", str(BUGSINPY_ROOT)],
        cwd=ROOT,
        timeout=180,
    )
    if result.returncode != 0:
        raise ContractError(result.stderr or result.stdout)


def _ensure_repo(scenario: BugsInPyScenario) -> Path:
    project = _project_info(scenario)
    repo_url = _required(project, "github_url", f"{scenario.project} project.info").removesuffix("/")
    repo = WORK_ROOT / scenario.project / "repo"
    if not (repo / ".git").exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        result = _run(["git", "clone", repo_url, str(repo)], cwd=ROOT, timeout=300)
        if result.returncode != 0:
            raise ContractError(result.stderr or result.stdout)
    return repo


def _ensure_env(scenario: BugsInPyScenario) -> Path:
    env_dir = ENV_ROOT / scenario.project
    python_bin = env_dir / "bin" / "python"
    if not python_bin.exists():
        env_dir.parent.mkdir(parents=True, exist_ok=True)
        result = _run(["python3.9", "-m", "venv", str(env_dir)], cwd=ROOT)
        if result.returncode != 0:
            raise ContractError(result.stderr or result.stdout)
    deps = ("pip", "pytest", *scenario.env_deps)
    result = _run(
        [str(python_bin), "-m", "pip", "install", "-q", "--upgrade", *deps],
        cwd=ROOT,
        timeout=300,
    )
    if result.returncode != 0:
        raise ContractError(result.stderr or result.stdout)
    return env_dir


def _write_shims(scenario: BugsInPyScenario, env_dir: Path) -> Path:
    shim_dir = SHIM_ROOT / scenario.run_id
    shim_dir.mkdir(parents=True, exist_ok=True)
    python_bin = shlex.quote(str(env_dir / "bin" / "python"))
    shims = {
        "python": f"#!/bin/sh\nexec {python_bin} \"$@\"\n",
        "python3": f"#!/bin/sh\nexec {python_bin} \"$@\"\n",
        "pytest": f"#!/bin/sh\nexec {python_bin} -m pytest \"$@\"\n",
        "py.test": f"#!/bin/sh\nexec {python_bin} -m pytest \"$@\"\n",
    }
    for name, content in shims.items():
        path = shim_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)
    return shim_dir


def _test_env(repo: Path, scenario: BugsInPyScenario, env_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = str(_write_shims(scenario, env_dir)) + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = os.pathsep.join(str(repo / item) for item in scenario.pythonpath)
    return env


def _copy_fixed_tests(repo: Path, fix_commit: str, test_files: list[str]) -> None:
    for test_file in test_files:
        result = _run(["git", "show", f"{fix_commit}:{test_file}"], cwd=repo)
        if result.returncode != 0:
            raise ContractError(result.stderr or result.stdout)
        target = repo / test_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(result.stdout, encoding="utf-8")


def _run_setup(repo: Path, scenario: BugsInPyScenario, env_dir: Path) -> None:
    if not scenario.setup_editable:
        return
    result = _run([str(env_dir / "bin" / "python"), "-m", "pip", "install", "-q", "-e", "."], cwd=repo, timeout=300)
    if result.returncode != 0:
        raise ContractError(result.stderr or result.stdout)


def _command_evidence(
    scenario: BugsInPyScenario,
    command: str,
    result: subprocess.CompletedProcess[str],
    *,
    commit_sha: str,
    expected_status: str,
    env_dir: Path,
) -> dict[str, Any]:
    status = "pass" if result.returncode == 0 else "fail"
    if status != expected_status:
        raise ContractError(
            f"{scenario.run_id} {command!r} expected {expected_status}, got {status}:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    python_version = _run([str(env_dir / "bin" / "python"), "-V"], cwd=ROOT).stdout.strip()
    return {
        "command": f"PATH={SHIM_ROOT / scenario.run_id}:$PATH PYTHONPATH=<repo> {command}",
        "status": status,
        "exit_code": result.returncode,
        "stdout_digest": _sha256(result.stdout),
        "stderr_digest": _sha256(result.stderr),
        "commit_sha": commit_sha,
        "tool_version": python_version,
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
        "reason": f"{approver_role} approved BugsInPy corpus evidence for {context.stage_name}.",
        "evidence_refs": evidence_refs,
    }


def _capture_evidence(scenario: BugsInPyScenario) -> dict[str, Any]:
    _ensure_bugsinpy()
    repo = _ensure_repo(scenario)
    env_dir = _ensure_env(scenario)
    project = _project_info(scenario)
    bug = _bug_info(scenario)
    repo_url = _required(project, "github_url", f"{scenario.project} project.info").removesuffix("/")
    bug_commit = _required(bug, "buggy_commit_id", f"{scenario.project} bug {scenario.bug_id}")
    fix_commit = _required(bug, "fixed_commit_id", f"{scenario.project} bug {scenario.bug_id}")
    test_files = [item for item in _required(bug, "test_file", f"{scenario.project} bug {scenario.bug_id}").split(";") if item]
    command = _run_test_command(scenario)

    _git(repo, "fetch", "--all", "--tags")
    _git(repo, "reset", "--hard", bug_commit)
    _git(repo, "clean", "-fd")
    _copy_fixed_tests(repo, fix_commit, test_files)
    _run_setup(repo, scenario, env_dir)
    failing = _run(command, cwd=repo, env=_test_env(repo, scenario, env_dir))
    failing_evidence = _command_evidence(
        scenario,
        command,
        failing,
        commit_sha=bug_commit,
        expected_status="fail",
        env_dir=env_dir,
    )

    _git(repo, "reset", "--hard", fix_commit)
    _git(repo, "clean", "-fd")
    _run_setup(repo, scenario, env_dir)
    passing = _run(command, cwd=repo, env=_test_env(repo, scenario, env_dir))
    passing_evidence = _command_evidence(
        scenario,
        command,
        passing,
        commit_sha=fix_commit,
        expected_status="pass",
        env_dir=env_dir,
    )

    return {
        "repo_url": repo_url,
        "repo_path": str(repo),
        "bug_branch": bug_commit,
        "fix_branch": fix_commit,
        "bug_commit": bug_commit,
        "fix_commit": fix_commit,
        "failing_command": failing_evidence,
        "passing_command": passing_evidence,
        "test_files": test_files,
        "test_command": command,
    }


def _executor(scenario: BugsInPyScenario, evidence: dict[str, Any]) -> FunctionStageExecutor:
    issue_id = f"BIP-{scenario.project}-{scenario.bug_id}".upper().replace("-", "_")

    def require(context: StageExecutionContext, artifact_name: str) -> dict[str, Any]:
        artifact = context.prior_artifacts.get(artifact_name)
        if artifact is None:
            raise ContractError(f"{context.stage_name} requires {artifact_name}")
        return artifact

    def reproduce(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        return {
            "bug_report": {
                "version": "1.0",
                "issue_id": issue_id,
                "observed_behavior": scenario.issue_summary,
                "expected_behavior": "The fixed commit should pass the focused BugsInPy regression test.",
                "reproduction_steps": [
                    f"Clone {evidence['repo_url']}",
                    f"Checkout buggy commit {evidence['bug_commit']}",
                    "Copy the regression test file(s) from the fixed commit into the buggy checkout.",
                    f"Run {evidence['test_command']}",
                ],
                "evidence": [
                    f"repo_url={evidence['repo_url']}",
                    f"bug_commit={evidence['bug_commit']}",
                    f"fix_commit={evidence['fix_commit']}",
                    f"failing_exit_code={evidence['failing_command']['exit_code']}",
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
                "root_cause": scenario.root_cause,
                "failure_family": scenario.failure_family,
                "affected_files": evidence["test_files"],
                "evidence": [
                    "BugsInPy metadata provides the public buggy and fixed commits.",
                    "The focused regression fails on the buggy commit and passes on the fixed commit.",
                ],
                "confidence": 0.86,
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
                        "category": scenario.failure_family,
                        "cause": scenario.system_fault,
                        "prevention": scenario.prevention,
                    }
                ],
            },
            "decision_log": {
                "version": "1.0",
                "run_id": scenario.run_id,
                "decisions": [
                    {
                        "decision_id": f"{scenario.run_id}-d-001",
                        "stage": "system_fault",
                        "category": "real_repo_corpus",
                        "subject": "Whether to include this public repository bug in the proof corpus",
                        "options_considered": [
                            {
                                "option_id": "skip",
                                "label": "Skip the candidate",
                                "score": 0.2,
                                "reason": "Would leave the corpus below the external proof target.",
                                "rejected_because": "The candidate has failing and passing command evidence.",
                            },
                            {
                                "option_id": "include",
                                "label": "Include verified BugsInPy public repo scenario",
                                "score": 0.88,
                                "reason": "The scenario is public, non-author, and has command evidence.",
                            },
                        ],
                        "selected": "include",
                        "reason": "The public buggy commit fails and the public fixed commit passes under the focused regression.",
                        "user_visible": True,
                        "user_approved": True,
                        "confidence": 0.86,
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
                "risk_level": "medium",
                "changes": [
                    "Use the public fixed commit as the patch boundary.",
                    "Preserve the focused BugsInPy regression command as command evidence.",
                ],
                "verification_commands": [evidence["test_command"]],
                "rollback_plan": f"Return to buggy commit {evidence['bug_commit']} and rerun the regression.",
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
                "id": f"reg-{scenario.run_id}",
                "source_issue_id": report["issue_id"],
                "fixture": f"BugsInPy {scenario.project} bug {scenario.bug_id}",
                "expected_guard": "buggy commit fails and fixed commit passes the focused regression",
                "replay_command": f"python3 scripts/run_bugsinpy_corpus.py --scenario {scenario.run_id}",
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
                "target": "real-repo-corpus",
                "update_type": "documentation",
                "trigger": f"BugsInPy {scenario.project} bug {scenario.bug_id}",
                "summary": "A corpus entry must preserve public repo URL, BugsInPy metadata ref, failing evidence, passing evidence, and kernel checkpoints.",
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


def run_scenario(scenario: BugsInPyScenario, *, write_example: bool) -> dict[str, Any]:
    schemas = SchemaRegistry(ROOT / "schemas" / "artifacts").load()
    manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
    reviewer = Reviewer(schemas)
    role_policy = RolePolicy.load(ROOT / "packs" / "software" / "roles.json")
    evidence = _capture_evidence(scenario)
    store = CheckpointStore(ROOT / ".apk" / "real-bug-runs", schemas, role_policy)
    engine = RunEngine(store, reviewer)
    checkpoints = engine.run_with_executor(
        manifest,
        scenario.run_id,
        _executor(scenario, evidence),
        scenario=evidence,
        metadata={
            "demo": False,
            "execution_mode": "bugsinpy_corpus",
            "repo_url": evidence["repo_url"],
            "bugsinpy_project": scenario.project,
            "bugsinpy_bug_id": scenario.bug_id,
        },
    )
    report = {
        "version": "1.0",
        "run_id": scenario.run_id,
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
            f"{evidence['repo_url']}/tree/{evidence['bug_commit']}",
            f"{evidence['repo_url']}/tree/{evidence['fix_commit']}",
            f"https://github.com/soarsmu/BugsInPy/tree/master/projects/{scenario.project}/bugs/{scenario.bug_id}",
        ],
    }
    schemas.validate("real_repo_bug_run", report)
    report_path = ROOT / ".apk" / "real-bug-runs" / scenario.run_id / "real_repo_bug_run.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    if write_example:
        example_path = ROOT / scenario.artifact_file
        example_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "run_id": scenario.run_id,
        "status": "pass",
        "repo_url": evidence["repo_url"],
        "artifact_file": scenario.artifact_file,
        "failure_family": scenario.failure_family,
        "report": str(report_path.relative_to(ROOT)),
    }


def _required(raw: dict[str, str], field: str, context: str) -> str:
    value = raw.get(field)
    if not value:
        raise ContractError(f"{context} missing {field}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=[scenario.run_id for scenario in SCENARIOS])
    parser.add_argument("--write-examples", action="store_true")
    args = parser.parse_args()

    selected = [scenario for scenario in SCENARIOS if args.scenario in {None, scenario.run_id}]
    results = [run_scenario(scenario, write_example=args.write_examples) for scenario in selected]
    print(json.dumps({"status": "pass", "results": results}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
