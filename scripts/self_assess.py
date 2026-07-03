#!/usr/bin/env python3
"""Self-assess the kernel and propose the next iteration.

This is the project's dogfood loop: the kernel should not wait for a human to
notice the next weakness. It gathers command evidence, scores itself, emits a
schema-validated report, and names the next concrete actions.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apkernel import ContractError, SchemaRegistry, build_real_repo_corpus_report, load_domain_packs, load_json, validate_artifact_semantics


COMMANDS = (
    ("python3 scripts/verify.py", [sys.executable, "scripts/verify.py"]),
    ("python3 scripts/verify_real_repo_corpus.py", [sys.executable, "scripts/verify_real_repo_corpus.py"]),
    ("python3 -m unittest discover -s tests -v", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]),
    ("python3 scripts/replay_regressions.py", [sys.executable, "scripts/replay_regressions.py"]),
    ("python3 -m compileall apkernel scripts tests", [sys.executable, "-m", "compileall", "apkernel", "scripts", "tests"]),
)

EVIDENCE_FRESHNESS_ROOTS = (
    "apkernel",
    "scripts",
    "schemas",
    "pipelines",
    "packs",
    "tests",
    "docs",
    "README.md",
)
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


@dataclasses.dataclass(frozen=True)
class CommandRun:
    command: str
    status: str
    exit_code: int
    stdout: str
    stderr: str


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


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


def _run_commands() -> list[CommandRun]:
    runs: list[CommandRun] = []
    for display, argv in COMMANDS:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        runs.append(
            CommandRun(
                command=display,
                status="pass" if result.returncode == 0 else "fail",
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        )
    return runs


def _command_evidence(runs: list[CommandRun]) -> list[dict[str, Any]]:
    commit_sha = _commit_sha()
    tool_version = f"python {sys.version.split()[0]}"
    evidence = []
    for run in runs:
        evidence.append(
            {
                "command": run.command,
                "status": run.status,
                "exit_code": run.exit_code,
                "stdout_digest": _sha256(run.stdout),
                "stderr_digest": _sha256(run.stderr),
                "commit_sha": commit_sha,
                "tool_version": tool_version,
                "timestamp": _timestamp(),
                "artifact_refs": ["self_assessment_report"],
            }
        )
    return evidence


def _pipeline_count() -> int:
    return len(sorted((ROOT / "pipelines").glob("*.json")))


def _executor_count() -> int:
    try:
        packs = load_domain_packs(ROOT)
    except ContractError:
        return 0
    executable_pipelines = {scenario.pipeline for pack in packs for scenario in pack.scenarios}
    if (ROOT / "scripts" / "run_next_action.py").exists() and (ROOT / "pipelines" / "kernel-autonomy.json").exists():
        executable_pipelines.add("kernel-autonomy")
    return len(executable_pipelines)


def _pack_registry_stats() -> dict[str, Any]:
    try:
        packs = load_domain_packs(ROOT)
    except ContractError:
        return {"pack_count": 0, "pack_names": [], "scenario_pipelines": []}
    return {
        "pack_count": len(packs),
        "pack_names": [pack.name for pack in packs],
        "scenario_pipelines": sorted({scenario.pipeline for pack in packs for scenario in pack.scenarios}),
    }


def _has_second_domain_pack() -> bool:
    stats = _pack_registry_stats()
    return stats["pack_count"] >= 2 and any(name != "software" for name in stats["pack_names"])


def _has_role_runtime_enforcement() -> bool:
    source = (ROOT / "apkernel" / "core.py").read_text(encoding="utf-8")
    return "class RolePolicy" in source and "validate_stage_write" in source


def _replay_contract_stats() -> dict[str, int]:
    stats = {"total": 0, "with_checkpoints": 0, "negative": 0}
    for path in sorted((ROOT / "examples").glob("golden_*.json")):
        raw = load_json(path)
        stats["total"] += 1
        if raw.get("expected_checkpoints"):
            stats["with_checkpoints"] += 1
        if raw.get("expected_result") == "fail":
            stats["negative"] += 1
    return stats


def _real_bug_run_report() -> dict[str, Any] | None:
    report_path = ROOT / ".apk" / "real-bug-runs" / "real-repo-privacy-cache" / "real_repo_bug_run.json"
    if not report_path.exists():
        return None
    try:
        report = load_json(report_path)
        SchemaRegistry(ROOT / "schemas" / "artifacts").load().validate("real_repo_bug_run", report)
    except (OSError, json.JSONDecodeError, ContractError):
        return None
    if report.get("failing_command", {}).get("status") != "fail":
        return None
    if report.get("passing_command", {}).get("status") != "pass":
        return None
    if not report.get("checkpoint_refs") or not report.get("public_refs"):
        return None
    return report


def _has_real_bug_run_evidence() -> bool:
    return _real_bug_run_report() is not None


def _real_repo_corpus_stats() -> dict[str, Any]:
    report = _real_repo_corpus_report()
    if report is None:
        return {
            "non_author_repo_count": 0,
            "failure_family_count": 0,
            "target_met": False,
            "missing_non_author_repos": 5,
            "missing_failure_families": 3,
        }
    return dict(report["summary"])


def _real_repo_corpus_freshness() -> dict[str, Any]:
    report = _real_repo_corpus_report()
    if report is None:
        return {
            "artifact_source": "unknown",
            "external_execution": False,
            "stale_live_artifact_count": 0,
        }
    return dict(report["freshness"])


def _real_repo_corpus_report() -> dict[str, Any] | None:
    try:
        return build_real_repo_corpus_report(
            ROOT,
            SchemaRegistry(ROOT / "schemas" / "artifacts").load(),
        )
    except (OSError, json.JSONDecodeError, ContractError):
        return None


def _real_repo_corpus_count() -> int:
    return int(_real_repo_corpus_stats().get("non_author_repo_count", 0))


def _branch_replay_statuses() -> set[str]:
    statuses: set[str] = set()
    for path in sorted((ROOT / "examples").glob("golden_*.json")):
        raw = load_json(path)
        for expected in raw.get("expected_checkpoints", {}).values():
            if isinstance(expected, dict) and isinstance(expected.get("status"), str):
                statuses.add(expected["status"])
    return statuses


def _has_branch_level_replay() -> bool:
    return {"awaiting_human", "failed", "blocked"}.issubset(_branch_replay_statuses())


def _has_bounded_autonomy_runner() -> bool:
    required_paths = [
        ROOT / "scripts" / "run_next_action.py",
        ROOT / "pipelines" / "kernel-autonomy.json",
        ROOT / "schemas" / "artifacts" / "autonomy_run_report.json",
        ROOT / "examples" / "autonomy_run_replay_fixture.json",
    ]
    if not all(path.exists() for path in required_paths):
        return False
    try:
        fixture = load_json(ROOT / "examples" / "autonomy_run_replay_fixture.json")
        report = fixture.get("autonomy_run_report")
        if not isinstance(report, dict):
            return False
        SchemaRegistry(ROOT / "schemas" / "artifacts").load().validate("autonomy_run_report", report)
    except (OSError, json.JSONDecodeError, ContractError):
        return False
    return True


def _has_semantic_fake_green_tests() -> bool:
    tests = (ROOT / "tests" / "test_kernel.py").read_text(encoding="utf-8")
    return (
        "validate_artifact_semantics" in tests
        and "commit_sha_mismatch" in tests
        and "checkpoint_status_mismatch" in tests
    )


def _has_battle_report_surface() -> bool:
    return (
        (ROOT / "schemas" / "artifacts" / "battle_report.json").exists()
        and (ROOT / "scripts" / "battle_report.py").exists()
        and "battle_report" in (ROOT / "tests" / "test_kernel.py").read_text(encoding="utf-8")
    )


def _has_independent_agent_battle_evidence() -> bool:
    return _has_current_independent_agent_battle(require_advance=True)


def _has_current_independent_agent_battle_attempt() -> bool:
    return _has_current_independent_agent_battle(require_advance=False)


def _has_current_independent_agent_battle(*, require_advance: bool) -> bool:
    report_root = ROOT / ".apk" / "agent-battle-harness"
    if not report_root.exists():
        return False
    schemas = SchemaRegistry(ROOT / "schemas" / "artifacts").load()
    candidates = sorted(
        report_root.glob("*/agent_battle_harness_report.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for report_path in candidates:
        try:
            report = load_json(report_path)
            schemas.validate("agent_battle_harness_report", report)
            validate_artifact_semantics("agent_battle_harness_report", report)
        except (OSError, json.JSONDecodeError, ContractError):
            continue
        if _is_current_independent_agent_battle_report(
            report,
            report_path,
            require_advance=require_advance,
        ):
            return True
    return False


def _is_independent_agent_battle_attempt(report: dict[str, Any]) -> bool:
    try:
        SchemaRegistry(ROOT / "schemas" / "artifacts").load().validate("agent_battle_harness_report", report)
        validate_artifact_semantics("agent_battle_harness_report", report)
    except ContractError:
        return False
    return (
        report.get("protocol", {}).get("evidence_mode") == "independent_agent_reports"
        and report.get("protocol", {}).get("independent_contexts") is True
    )


def _is_independent_agent_battle_report(report: dict[str, Any]) -> bool:
    return (
        _is_independent_agent_battle_attempt(report)
        and report.get("outcome", {}).get("verdict") == "advance"
    )


def _is_current_independent_agent_battle_report(
    report: dict[str, Any],
    report_path: Path,
    *,
    current_evidence_mtime: float | None = None,
    require_advance: bool = True,
) -> bool:
    if require_advance:
        if not _is_independent_agent_battle_report(report):
            return False
    elif not _is_independent_agent_battle_attempt(report):
        return False
    try:
        report_mtime = report_path.stat().st_mtime
    except OSError:
        return False
    input_reports = report.get("input_reports", {})
    if not isinstance(input_reports, dict):
        return False
    input_paths: list[Path] = []
    for key in ("self_assessment_report", "battle_report"):
        raw_ref = input_reports.get(key)
        if not isinstance(raw_ref, str) or raw_ref.startswith("generated:"):
            return False
        ref_path = Path(raw_ref)
        if not ref_path.is_absolute():
            ref_path = ROOT / ref_path
        if not ref_path.exists():
            return False
        input_paths.append(ref_path)
    schemas = SchemaRegistry(ROOT / "schemas" / "artifacts").load()
    try:
        self_report = load_json(input_paths[0])
        battle_report = load_json(input_paths[1])
        schemas.validate("self_assessment_report", self_report)
        schemas.validate("battle_report", battle_report)
    except (OSError, json.JSONDecodeError, ContractError):
        return False
    if not self_report.get("run_id") or not battle_report.get("run_id"):
        return False
    if input_reports.get("self_assessment_run_id") != self_report.get("run_id"):
        return False
    if input_reports.get("battle_report_run_id") != battle_report.get("run_id"):
        return False
    current_fingerprint = _current_evidence_fingerprint()
    if self_report.get("evidence_fingerprint") != current_fingerprint:
        return False
    if battle_report.get("evidence_fingerprint") != current_fingerprint:
        return False
    if _has_newer_open_battle_cycle_report(
        "self_assessment_report",
        input_paths[0],
        self_report,
    ):
        return False
    if _has_newer_open_battle_cycle_report(
        "battle_report",
        input_paths[1],
        battle_report,
    ):
        return False
    freshness_mtime = (
        _current_evidence_source_mtime()
        if current_evidence_mtime is None
        else current_evidence_mtime
    )
    try:
        input_mtimes = [path.stat().st_mtime for path in input_paths]
    except OSError:
        return False
    if any(report_mtime < input_mtime for input_mtime in input_mtimes):
        return False
    if any(input_mtime < freshness_mtime for input_mtime in input_mtimes):
        return False
    return report_mtime >= freshness_mtime


def _has_newer_open_battle_cycle_report(
    report_name: str,
    bound_path: Path,
    bound_report: dict[str, Any],
) -> bool:
    fingerprint = bound_report.get("evidence_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        return True
    try:
        bound_resolved = bound_path.resolve()
        bound_mtime = bound_path.stat().st_mtime
    except OSError:
        return True
    report_root, filename = (
        (ROOT / ".apk" / "self-assessments", "self_assessment_report.json")
        if report_name == "self_assessment_report"
        else (ROOT / ".apk" / "battle-reports", "battle_report.json")
    )
    if not report_root.exists():
        return False
    schemas = SchemaRegistry(ROOT / "schemas" / "artifacts").load()
    for candidate_path in sorted(report_root.glob(f"*/{filename}")):
        try:
            if candidate_path.resolve() == bound_resolved:
                continue
            if candidate_path.stat().st_mtime <= bound_mtime:
                continue
            candidate = load_json(candidate_path)
            schemas.validate(report_name, candidate)
        except (OSError, json.JSONDecodeError, ContractError):
            continue
        if candidate.get("evidence_fingerprint") != fingerprint:
            continue
        if report_name == "self_assessment_report":
            if _self_report_has_open_battle_cycle(candidate):
                return True
        elif _battle_report_has_open_battle_cycle(candidate):
            return True
    return False


def _self_report_has_open_battle_cycle(report: dict[str, Any]) -> bool:
    next_actions = report.get("next_actions", [])
    if isinstance(next_actions, list):
        for action in next_actions:
            if isinstance(action, dict) and action.get("id") in ALLOWED_SELF_ASSESS_BATTLE_ACTIONS:
                return True
    return _dimension_score(report, "evaluation_independence") < 95 and all(
        _dimension_score(report, name) >= 95 for name in PRE_BATTLE_DIMENSIONS
    )


def _battle_report_has_open_battle_cycle(report: dict[str, Any]) -> bool:
    next_actions = report.get("next_actions", [])
    if isinstance(next_actions, list) and any(action in ALLOWED_BATTLE_REPORT_ACTIONS for action in next_actions):
        return True
    return report.get("verdict") != "advance"


def _dimension_score(report: dict[str, Any], name: str) -> float:
    for dimension in report.get("dimensions", []):
        if isinstance(dimension, dict) and dimension.get("name") == name:
            try:
                return float(dimension.get("score", 0))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _current_evidence_source_mtime() -> float:
    newest = 0.0
    for path in _iter_evidence_files():
        newest = max(newest, path.stat().st_mtime)
    return newest


def _current_evidence_fingerprint() -> str:
    payload: list[dict[str, str]] = []
    for path in _iter_evidence_files():
        relative = path.relative_to(ROOT).as_posix()
        payload.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _iter_evidence_files() -> list[Path]:
    files: list[Path] = []
    for relative in EVIDENCE_FRESHNESS_ROOTS:
        path = ROOT / relative
        if not path.exists():
            continue
        if path.is_file():
            if path.suffix in {".py", ".json", ".md"}:
                files.append(path)
            continue
        for child in path.rglob("*"):
            if child.is_file() and child.suffix in {".py", ".json", ".md"}:
                files.append(child)
    return sorted(files)


def _gate_passed(runs: list[CommandRun], command_prefix: str) -> bool:
    return any(run.command.startswith(command_prefix) and run.status == "pass" for run in runs)


def _dimensions(runs: list[CommandRun]) -> list[dict[str, Any]]:
    pipelines = _pipeline_count()
    executors = _executor_count()
    all_gates_pass = all(run.status == "pass" for run in runs)
    verify_pass = _gate_passed(runs, "python3 scripts/verify.py")
    corpus_pass = _gate_passed(runs, "python3 scripts/verify_real_repo_corpus.py")
    replay_pass = _gate_passed(runs, "python3 scripts/replay_regressions.py")
    unit_pass = _gate_passed(runs, "python3 -m unittest")
    replay_stats = _replay_contract_stats()
    real_bug_run = _has_real_bug_run_evidence()
    corpus_stats = _real_repo_corpus_stats()
    corpus_freshness = _real_repo_corpus_freshness()
    real_repo_corpus = int(corpus_stats.get("non_author_repo_count", 0))
    failure_family_count = int(corpus_stats.get("failure_family_count", 0))
    branch_replay = _has_branch_level_replay()
    bounded_runner = _has_bounded_autonomy_runner()
    second_pack = _has_second_domain_pack()
    semantic_fake_green_tests = _has_semantic_fake_green_tests()
    pack_stats = _pack_registry_stats()
    independent_battle = _has_independent_agent_battle_evidence()
    independent_battle_attempt = _has_current_independent_agent_battle_attempt()

    execution_score = 35 + round(35 * executors / max(pipelines, 1))
    if second_pack:
        execution_score += 10
    if real_bug_run:
        execution_score += 5
    if real_repo_corpus >= 5:
        execution_score += 10
    execution_score = min(95, execution_score)

    role_score = 68
    if _has_role_runtime_enforcement():
        role_score = 78
    if _has_role_runtime_enforcement() and branch_replay:
        role_score = 84
    if _has_role_runtime_enforcement() and branch_replay and bounded_runner and second_pack:
        role_score = 88
    if (
        _has_role_runtime_enforcement()
        and branch_replay
        and bounded_runner
        and second_pack
        and real_repo_corpus >= 5
        and failure_family_count >= 3
    ):
        role_score = 96

    contract_score = 45
    if verify_pass and corpus_pass:
        contract_score = 84
    if verify_pass and corpus_pass and second_pack:
        contract_score = 88
    if verify_pass and corpus_pass and second_pack and semantic_fake_green_tests:
        contract_score = 91
    if verify_pass and corpus_pass and second_pack and semantic_fake_green_tests and real_repo_corpus >= 5 and failure_family_count >= 3:
        contract_score = 96

    replay_score = 35
    if replay_pass:
        replay_score = 72
        if replay_stats["with_checkpoints"] >= pipelines and replay_stats["negative"] >= 2:
            replay_score = 82
        if branch_replay and bounded_runner:
            replay_score = 86
        if second_pack and semantic_fake_green_tests:
            replay_score = 90
        if second_pack and semantic_fake_green_tests and real_repo_corpus >= 5 and failure_family_count >= 3:
            replay_score = 96
    evidence_score = 40
    if all_gates_pass and unit_pass:
        evidence_score = 74
        if real_bug_run:
            evidence_score += 6
        if semantic_fake_green_tests:
            evidence_score += 6
        if second_pack:
            evidence_score += 4
        if real_repo_corpus >= 5 and failure_family_count >= 3:
            evidence_score += 10
        if (
            corpus_freshness.get("external_execution") is False
            and int(corpus_freshness.get("stale_live_artifact_count", 0)) > 0
        ):
            evidence_score = min(evidence_score, 96)
    autonomy_score = 72
    if bounded_runner:
        autonomy_score = 82
    if bounded_runner and branch_replay:
        autonomy_score = 86
    if bounded_runner and branch_replay and real_repo_corpus >= 5 and failure_family_count >= 3:
        autonomy_score = 96

    evaluation_score = 72
    evaluation_rationale = (
        "The local Agent Battle harness is schema-valid, but current checked evidence is derived from internal reports and must hold."
    )
    if independent_battle:
        evaluation_score = 96
        evaluation_rationale = "An independent Agent Battle harness report with external judge sources is checked and semantically valid."
    elif independent_battle_attempt:
        evaluation_score = 84
        evaluation_rationale = (
            "A current independent Agent Battle harness report exists, but it held or vetoed advance; "
            "the next action is to address those findings, not rerun the same battle."
        )

    evidence_rationale = "Verification gates pass with semantic checks, replay, and a broad real-repo corpus."
    if real_repo_corpus < 5 or failure_family_count < 3:
        evidence_rationale = "Verification gates pass, but trust is capped until live producer roundtrips and a broader real repo corpus exist."
    elif (
        corpus_freshness.get("external_execution") is False
        and int(corpus_freshness.get("stale_live_artifact_count", 0)) > 0
    ):
        evidence_rationale = (
            "Verification gates pass with semantic checks, replay, and a broad checked-in real-repo corpus; "
            "trust is capped below perfect while live external rerun artifacts are stale."
        )

    return [
        {
            "name": "contract_integrity",
            "score": contract_score,
            "rationale": "Pipeline manifests, schemas, tools, roles, and scenarios are checked by the verifier.",
            "evidence_refs": ["scripts/verify.py", "schemas/artifacts", "pipelines"],
        },
        {
            "name": "execution_reality",
            "score": execution_score,
            "rationale": f"{executors} of {pipelines} pipelines have an executor path across packs {pack_stats['pack_names']}; non-author repo corpus size is {real_repo_corpus}, failure families {failure_family_count}.",
            "evidence_refs": ["apkernel/packs.py", "packs/registry.json", "scripts/verify.py"],
        },
        {
            "name": "evidence_trust",
            "score": evidence_score,
            "rationale": evidence_rationale,
            "evidence_refs": ["scripts/self_assess.py", "scripts/verify_real_repo_corpus.py", "tests/test_kernel.py", "examples/real_repo_corpus_manifest.json"],
        },
        {
            "name": "replay_strength",
            "score": replay_score,
            "rationale": (
                "Replay evaluates persisted checkpoint artifacts, "
                f"{replay_stats['with_checkpoints']} golden scenarios assert checkpoints, "
                f"{replay_stats['negative']} negative scenarios prove failure detection, "
                f"branch statuses covered are {sorted(_branch_replay_statuses())}, "
                f"and semantic fake-green tests are {semantic_fake_green_tests}."
            ),
            "evidence_refs": ["apkernel/replay.py", "scripts/replay_regressions.py", "examples/golden_*.json"],
        },
        {
            "name": "multi_agent_readiness",
            "score": role_score,
            "rationale": "Roles, handoff artifacts, runtime stage ownership policy, and non-happy-path checkpoint branches are enforced or replayed across registered packs.",
            "evidence_refs": ["packs/registry.json", "packs/software/roles.json", "packs/research/roles.json"],
        },
        {
            "name": "autonomy_loop",
            "score": autonomy_score,
            "rationale": "The project can score itself, emit next actions, execute allowlisted local actions, and checkpoint blocked boundaries; broad autonomous scheduling remains out of scope.",
            "evidence_refs": ["scripts/self_assess.py", "scripts/run_next_action.py", "scripts/run_real_bug_scenario.py", "schemas/artifacts/self_assessment_report.json"],
        },
        {
            "name": "evaluation_independence",
            "score": evaluation_score,
            "rationale": evaluation_rationale,
            "evidence_refs": ["scripts/agent_battle_harness.py", "schemas/artifacts/agent_battle_harness_report.json", "examples/agent_battle_harness_report_fixture.json"],
        },
    ]


def _next_actions(dimensions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {dimension["name"]: dimension for dimension in dimensions}
    actions: list[dict[str, Any]] = []
    corpus_stats = _real_repo_corpus_stats()
    real_repo_corpus = int(corpus_stats.get("non_author_repo_count", 0))
    failure_family_count = int(corpus_stats.get("failure_family_count", 0))

    if not _pack_registry_stats()["pack_count"]:
        actions.append(
            {
                "id": "add-domain-pack-registry",
                "title": "Add a domain pack registry so verification is pack-agnostic",
                "priority": "P0",
                "rationale": "The kernel should discover packs instead of hardcoding software pipelines.",
                "target_files": ["apkernel/packs.py", "packs/registry.json", "scripts/verify.py", "scripts/replay_regressions.py"],
                "verification_commands": ["python3 scripts/verify.py", "python3 scripts/replay_regressions.py"],
            }
        )
    if not _has_second_domain_pack():
        actions.append(
            {
                "id": "add-second-domain-pack",
                "title": "Add one non-software domain pack to prove generality",
                "priority": "P0",
                "rationale": "A generic kernel needs at least one working domain outside software engineering.",
                "target_files": ["packs", "pipelines", "schemas/artifacts", "examples", "apkernel"],
                "verification_commands": ["python3 scripts/verify.py", "python3 scripts/replay_regressions.py", "python3 -m unittest discover -s tests -v"],
            }
        )
    if not _has_semantic_fake_green_tests():
        actions.append(
            {
                "id": "add-semantic-fake-green-tests",
                "title": "Add schema-valid semantic false-green tests",
                "priority": "P0",
                "rationale": "Schema-valid artifacts can still lie; verification needs semantic adversarial cases.",
                "target_files": ["apkernel/core.py", "tests/test_kernel.py", "scripts/verify.py", "scripts/replay_regressions.py"],
                "verification_commands": ["python3 -m unittest discover -s tests -v", "python3 scripts/verify.py"],
            }
        )
    if by_name["execution_reality"]["score"] < 80:
        actions.append(
            {
                "id": "expand-stage-executors",
                "title": "Add stage executors for feature, refactor, incident, and release pipelines",
                "priority": "P0",
                "rationale": "The system should produce artifacts stage by stage instead of relying on prebuilt demo payloads.",
                "target_files": ["apkernel/software.py", "scripts/verify.py", "scripts/replay_regressions.py", "tests/test_kernel.py"],
                "verification_commands": ["python3 scripts/self_assess.py", "python3 scripts/verify.py", "python3 scripts/replay_regressions.py"],
            }
        )
    if by_name["multi_agent_readiness"]["score"] < 80:
        actions.append(
            {
                "id": "enforce-role-handoffs",
                "title": "Enforce role ownership, handoffs, and approval-sensitive stages at runtime",
                "priority": "P0",
                "rationale": "Roles are currently a contract surface; they need runtime policy before role-based collaboration is trustworthy.",
                "target_files": ["apkernel/core.py", "packs/software/roles.json", "tests/test_kernel.py"],
                "verification_commands": ["python3 -m unittest discover -s tests -v", "python3 scripts/verify.py"],
            }
        )
    if by_name["replay_strength"]["score"] < 90:
        actions.append(
            {
                "id": "strengthen-golden-replay",
                "title": "Expand golden scenarios to cover command evidence, role handoffs, and failure branches",
                "priority": "P1",
                "rationale": "Replay should protect the contract-critical fields that would otherwise regress silently.",
                "target_files": ["examples/golden_*.json", "apkernel/replay.py", "tests/test_kernel.py"],
                "verification_commands": ["python3 scripts/replay_regressions.py", "python3 -m unittest discover -s tests -v"],
            }
        )
    if 90 <= by_name["replay_strength"]["score"] < 95 and not _has_branch_level_replay():
        actions.append(
            {
                "id": "add-branch-level-replay",
                "title": "Add branch-level replay for awaiting_human, failed, and blocked checkpoints",
                "priority": "P1",
                "rationale": "Replay should prove recovery and approval-sensitive checkpoint branches, not only completed paths.",
                "target_files": ["examples/golden_*.json", "apkernel/replay.py", "scripts/replay_regressions.py", "tests/test_kernel.py"],
                "verification_commands": ["python3 scripts/replay_regressions.py", "python3 scripts/verify.py", "python3 -m unittest discover -s tests -v"],
            }
        )
    if not _has_battle_report_surface():
        actions.append(
            {
                "id": "add-battle-report",
                "title": "Add a structured battle report artifact for architecture, test, critic, and reviewer scores",
                "priority": "P1",
                "rationale": "The project should preserve multi-perspective disagreement as first-class evidence, not just chat history.",
                "target_files": ["schemas/artifacts", "scripts/self_assess.py", "tests/test_kernel.py"],
                "verification_commands": ["python3 scripts/battle_report.py", "python3 scripts/self_assess.py", "python3 scripts/verify.py"],
            }
        )
    independent_battle_attempt = _has_current_independent_agent_battle_attempt()
    independent_battle_advance = _has_independent_agent_battle_evidence()
    if not independent_battle_attempt:
        actions.append(
            {
                "id": "run-independent-agent-battle",
                "title": "Run and preserve an independent multi-agent battle report for APK",
                "priority": "P0",
                "rationale": "The derived local battle harness is auditable but cannot prove independent evaluation or advance readiness.",
                "target_files": ["scripts/agent_battle_harness.py", "schemas/artifacts/agent_battle_harness_report.json", "examples/agent_battle_harness_report_fixture.json", "docs"],
                "verification_commands": ["python3 scripts/agent_battle_harness.py", "python3 scripts/verify.py", "python3 -m unittest discover -s tests -v"],
            }
        )
    elif not independent_battle_advance:
        actions.append(
            {
                "id": "address-independent-agent-battle-findings",
                "title": "Address the current independent Agent Battle hold findings",
                "priority": "P0",
                "rationale": "An independent battle has run and held; the system should repair the findings it produced instead of rerunning the same evaluation loop.",
                "target_files": ["apkernel", "scripts", "schemas/artifacts", "tests/test_kernel.py", "docs", ".apk/agent-battle-harness/current/agent_battle_harness_report.json"],
                "verification_commands": ["python3 -m unittest discover -s tests -v", "python3 scripts/verify.py", "python3 scripts/replay_regressions.py", "python3 scripts/self_assess.py", "python3 scripts/battle_report.py"],
            }
        )
    if not _has_real_bug_run_evidence():
        actions.append(
            {
                "id": "run-real-bug-scenario",
                "title": "Run one real repository bug through the kernel and preserve artifacts",
                "priority": "P2",
                "rationale": "The current system is dogfooded on deterministic demos; a real bug run is the next proof of generality.",
                "target_files": ["examples", "scripts", "docs"],
                "verification_commands": ["python3 scripts/verify.py", "python3 scripts/replay_regressions.py", "python3 scripts/battle_report.py"],
            }
        )
    if by_name["autonomy_loop"]["score"] < 95 and not _has_bounded_autonomy_runner():
        actions.append(
            {
                "id": "add-bounded-autonomy-runner",
                "title": "Add a bounded runner that executes safe next_actions with explicit stop conditions",
                "priority": "P2",
                "rationale": "The kernel should advance routine safe work without turning external or destructive actions into implicit autonomy.",
                "target_files": ["scripts", "docs/SELF_DRIVING.md", "tests/test_kernel.py"],
                "verification_commands": ["python3 scripts/self_assess.py", "python3 scripts/battle_report.py", "python3 scripts/verify.py"],
            }
        )
    if real_repo_corpus < 5 or failure_family_count < 3:
        actions.append(
            {
                "id": "expand-real-repo-corpus",
                "title": "Expand to five non-author real repository bug scenarios after human approval",
                "priority": "P1",
                "rationale": f"The current non-author real repo corpus size is {real_repo_corpus}; production confidence needs at least five non-author repos and three failure families.",
                "target_files": ["examples", "scripts", "docs"],
                "verification_commands": ["python3 scripts/verify.py", "python3 scripts/replay_regressions.py", "python3 scripts/battle_report.py"],
            }
        )
    return actions


def build_report(runs: list[CommandRun], run_id: str) -> dict[str, Any]:
    dimensions = _dimensions(runs)
    overall = round(sum(item["score"] for item in dimensions) / len(dimensions), 1)
    decision = "continue"
    if any(run.status == "fail" for run in runs):
        decision = "hold"
    if overall < 50:
        decision = "needs_human"
    return {
        "version": "1.0",
        "run_id": run_id,
        "evidence_fingerprint": _current_evidence_fingerprint(),
        "overall_score": overall,
        "decision": decision,
        "dimensions": dimensions,
        "commands": _command_evidence(runs),
        "next_actions": _next_actions(dimensions),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    run_id = dt.datetime.now(dt.timezone.utc).strftime("self-assess-%Y%m%dT%H%M%SZ")
    runs = _run_commands()
    report = build_report(runs, run_id)
    schemas = SchemaRegistry(ROOT / "schemas" / "artifacts").load()
    schemas.validate("self_assessment_report", report)

    output_dir = args.output_dir or ROOT / ".apk" / "self-assessments" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "self_assessment_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    summary = {
        "status": "pass" if report["decision"] == "continue" else report["decision"],
        "report": str(report_path.relative_to(ROOT)),
        "overall_score": report["overall_score"],
        "next_action": report["next_actions"][0]["id"] if report["next_actions"] else "none",
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if report["decision"] == "continue" else 1


if __name__ == "__main__":
    raise SystemExit(main())
