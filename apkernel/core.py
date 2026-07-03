"""Core contracts for an agent production system.

This module intentionally stays small and dependency-free. It is not a full
workflow engine; it is the contract layer that keeps agent work inspectable.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any, Callable


class ContractError(ValueError):
    """Raised when a manifest, schema, artifact, or checkpoint is invalid."""


class HumanApprovalRequired(ContractError):
    """Raised internally when a stage must stop for explicit human approval."""

    def __init__(self, stage_name: str, approver_role: str) -> None:
        super().__init__(f"{stage_name} requires explicit approval from {approver_role}")
        self.stage_name = stage_name
        self.approver_role = approver_role


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


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ContractError(f"{path} must contain a JSON object")
    return value


def _json_type_matches(expected: str, value: Any) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    return True


class SchemaRegistry:
    """Loads and validates compact JSON-schema-like artifact contracts."""

    def __init__(self, schema_dir: Path) -> None:
        self.schema_dir = schema_dir
        self.schemas: dict[str, dict[str, Any]] = {}

    def load(self) -> "SchemaRegistry":
        if not self.schema_dir.exists():
            raise ContractError(f"Schema directory not found: {self.schema_dir}")
        for path in sorted(self.schema_dir.glob("*.json")):
            schema = load_json(path)
            name = schema.get("artifact")
            if not isinstance(name, str) or not name:
                raise ContractError(f"{path} missing artifact name")
            self.schemas[name] = schema
        return self

    def validate(self, artifact_name: str, value: dict[str, Any]) -> None:
        schema = self.schemas.get(artifact_name)
        if schema is None:
            raise ContractError(f"No schema registered for artifact {artifact_name!r}")
        self._validate_object(artifact_name, value, schema, path=artifact_name)

    def _validate_object(
        self, artifact_name: str, value: Any, schema: dict[str, Any], *, path: str
    ) -> None:
        expected_type = schema.get("type", "object")
        if not _json_type_matches(expected_type, value):
            raise ContractError(f"{path} must be {expected_type}")

        if "const" in schema and value != schema["const"]:
            raise ContractError(f"{path} must equal {schema['const']!r}")
        if "enum" in schema and value not in schema["enum"]:
            raise ContractError(f"{path} must be one of {schema['enum']!r}")
        if expected_type in {"number", "integer"}:
            minimum = schema.get("minimum")
            exclusive_minimum = schema.get("exclusiveMinimum")
            maximum = schema.get("maximum")
            if isinstance(minimum, (int, float)) and value < minimum:
                raise ContractError(f"{path} must be >= {minimum}")
            if isinstance(exclusive_minimum, (int, float)) and value <= exclusive_minimum:
                raise ContractError(f"{path} must be > {exclusive_minimum}")
            if isinstance(maximum, (int, float)) and value > maximum:
                raise ContractError(f"{path} must be <= {maximum}")

        if expected_type == "object":
            assert isinstance(value, dict)
            required = schema.get("required", [])
            for key in required:
                if key not in value:
                    raise ContractError(f"{path}.{key} is required")
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                extra = sorted(set(value) - set(properties))
                if extra:
                    raise ContractError(f"{path} has unknown fields: {extra}")
            for key, child in properties.items():
                if key in value:
                    self._validate_object(
                        artifact_name, value[key], child, path=f"{path}.{key}"
                    )
            return

        if expected_type == "array":
            assert isinstance(value, list)
            min_items = schema.get("minItems")
            if isinstance(min_items, int) and len(value) < min_items:
                raise ContractError(f"{path} must have at least {min_items} items")
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    self._validate_object(
                        artifact_name, item, item_schema, path=f"{path}[{index}]"
                    )
            return

        min_length = schema.get("minLength")
        if isinstance(min_length, int) and isinstance(value, str) and len(value) < min_length:
            raise ContractError(f"{path} must be at least {min_length} chars")


@dataclasses.dataclass(frozen=True)
class PipelineManifest:
    """Validated declarative pipeline definition."""

    name: str
    version: str
    stages: tuple[dict[str, Any], ...]
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> "PipelineManifest":
        raw = load_json(path)
        return cls.from_dict(raw, source=path)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, source: Path | None = None) -> "PipelineManifest":
        name = raw.get("name")
        version = raw.get("version")
        stages = raw.get("stages")
        if not isinstance(name, str) or not name:
            raise ContractError(f"{source or '<manifest>'} missing name")
        if not isinstance(version, str) or not version:
            raise ContractError(f"{name} missing version")
        if not isinstance(stages, list) or not stages:
            raise ContractError(f"{name} must declare at least one stage")
        seen: set[str] = set()
        for stage in stages:
            if not isinstance(stage, dict):
                raise ContractError(f"{name} stage entries must be objects")
            stage_name = stage.get("name")
            if not isinstance(stage_name, str) or not stage_name:
                raise ContractError(f"{name} stage missing name")
            if stage_name in seen:
                raise ContractError(f"{name} duplicate stage {stage_name!r}")
            seen.add(stage_name)
            produces = stage.get("produces", [])
            if not isinstance(produces, list) or not all(isinstance(x, str) for x in produces):
                raise ContractError(f"{name}.{stage_name}.produces must be strings")
            for field in ("required_artifacts_in", "tools_available", "review_focus", "success_criteria"):
                value = stage.get(field, [])
                if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                    raise ContractError(f"{name}.{stage_name}.{field} must be strings")
        return cls(name=name, version=version, stages=tuple(stages), raw=raw)

    def stage_names(self) -> list[str]:
        return [stage["name"] for stage in self.stages]

    def get_stage(self, name: str) -> dict[str, Any]:
        for stage in self.stages:
            if stage["name"] == name:
                return stage
        raise ContractError(f"{self.name} has no stage {name!r}")


@dataclasses.dataclass(frozen=True)
class ApprovalRecord:
    role: str
    decision: str
    findings: tuple[dict[str, str], ...] = ()
    source: str = "runtime_review"

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "decision": self.decision,
            "source": self.source,
            "findings": list(self.findings),
        }


@dataclasses.dataclass(frozen=True)
class ReviewResult:
    stage: str
    decision: str
    findings: tuple[dict[str, str], ...]
    reviewer_role: str = "reviewer"
    approvals: tuple[ApprovalRecord, ...] = ()

    def approval_records(self) -> tuple[ApprovalRecord, ...]:
        if self.approvals:
            return self.approvals
        return (
            ApprovalRecord(
                role=self.reviewer_role,
                decision=self.decision,
                findings=self.findings,
            ),
        )

    def approved_roles(self) -> set[str]:
        return {
            approval.role
            for approval in self.approval_records()
            if approval.decision == "pass"
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "decision": self.decision,
            "reviewer_role": self.reviewer_role,
            "approvals": [approval.to_dict() for approval in self.approval_records()],
            "findings": list(self.findings),
        }


class Reviewer:
    """Schema-first reviewer with manifest-level focus and success criteria."""

    def __init__(self, schemas: SchemaRegistry, reviewer_role: str = "reviewer") -> None:
        self.schemas = schemas
        self.reviewer_role = reviewer_role

    def review(
        self,
        manifest: PipelineManifest,
        stage_name: str,
        artifacts: dict[str, dict[str, Any]],
        *,
        reviewer_role: str | None = None,
        reviewer_roles: tuple[str, ...] | list[str] | None = None,
    ) -> ReviewResult:
        stage = manifest.get_stage(stage_name)
        findings: list[dict[str, str]] = []
        for produced in stage.get("produces", []):
            if produced not in artifacts:
                findings.append({
                    "severity": "critical",
                    "title": "missing canonical artifact",
                    "detail": f"{stage_name} must produce {produced}",
                })
                continue
            try:
                self.schemas.validate(produced, artifacts[produced])
            except ContractError as exc:
                findings.append({
                    "severity": "critical",
                    "title": "schema validation failed",
                    "detail": str(exc),
                })
                continue
            for detail in _artifact_semantic_errors(produced, artifacts[produced]):
                findings.append({
                    "severity": "critical",
                    "title": "artifact semantic validation failed",
                    "detail": detail,
                })

        for criterion in stage.get("success_criteria", []):
            if criterion.startswith("requires:"):
                field = criterion.removeprefix("requires:").strip()
                if not _artifact_field_present(artifacts, field):
                    findings.append({
                        "severity": "critical",
                        "title": "success criterion unmet",
                        "detail": criterion,
                    })

        decision = "pass" if not any(f["severity"] == "critical" for f in findings) else "revise"
        if reviewer_roles is not None:
            roles = tuple(reviewer_roles)
        elif reviewer_role is not None:
            roles = (reviewer_role,)
        else:
            roles = (self.reviewer_role,)
        primary_reviewer = roles[0] if roles else (reviewer_role or self.reviewer_role)
        approvals = tuple(
            ApprovalRecord(
                role=role,
                decision=decision,
                findings=tuple(findings),
            )
            for role in roles
        )
        return ReviewResult(
            stage=stage_name,
            decision=decision,
            findings=tuple(findings),
            reviewer_role=primary_reviewer,
            approvals=approvals,
        )


def _artifact_field_present(artifacts: dict[str, dict[str, Any]], dotted_field: str) -> bool:
    for artifact in artifacts.values():
        current: Any = artifact
        for part in dotted_field.split("."):
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current[part]
        if current not in (None, "", [], {}):
            return True
    return False


def _artifact_semantic_errors(artifact_name: str, artifact: dict[str, Any]) -> list[str]:
    if artifact_name == "verification_report":
        return _verification_report_errors(artifact)
    if artifact_name == "release_report":
        return _release_report_errors(artifact)
    if artifact_name == "real_repo_bug_run":
        return _real_repo_bug_run_errors(artifact)
    if artifact_name == "checkpoint_branch_replay":
        return _checkpoint_branch_replay_errors(artifact)
    if artifact_name == "autonomy_run_report":
        return _autonomy_run_report_errors(artifact)
    if artifact_name == "real_repo_corpus_report":
        return _real_repo_corpus_report_errors(artifact)
    if artifact_name == "agent_battle_harness_report":
        return _agent_battle_harness_report_errors(artifact)
    if artifact_name == "approval_decision":
        return _approval_decision_errors(artifact)
    if artifact_name == "agent_judge_report":
        return _agent_judge_report_errors(artifact)
    return []


def validate_artifact_semantics(artifact_name: str, artifact: dict[str, Any]) -> None:
    errors = _artifact_semantic_errors(artifact_name, artifact)
    if errors:
        raise ContractError("; ".join(errors))


def _verification_report_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    commands = report.get("commands", [])
    for index, command in enumerate(commands):
        errors.extend(_command_evidence_errors(command, path=f"verification_report.commands[{index}]"))

    overall_status = report.get("overall_status")
    command_statuses = [command.get("status") for command in commands if isinstance(command, dict)]
    if overall_status == "pass" and any(status != "pass" for status in command_statuses):
        errors.append("verification_report.overall_status pass requires every command to pass")
    if overall_status == "fail" and "fail" not in command_statuses:
        errors.append("verification_report.overall_status fail requires at least one failed command")
    if overall_status == "blocked" and "blocked" not in command_statuses:
        errors.append("verification_report.overall_status blocked requires at least one blocked command")
    return errors


def _release_report_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    gates = report.get("gates", [])
    for index, gate in enumerate(gates):
        gate_path = f"release_report.gates[{index}]"
        evidence = gate.get("evidence") if isinstance(gate, dict) else None
        if isinstance(evidence, dict):
            errors.extend(_command_evidence_errors(evidence, path=f"{gate_path}.evidence"))
            if gate.get("status") == "pass" and evidence.get("status") != "pass":
                errors.append(f"{gate_path}.status pass requires passing evidence")
        else:
            errors.append(f"{gate_path}.evidence must be structured command evidence")

    report_status = report.get("status")
    gate_statuses = [gate.get("status") for gate in gates if isinstance(gate, dict)]
    if report_status in {"ready", "released"} and any(status != "pass" for status in gate_statuses):
        errors.append(f"release_report.status {report_status} requires every gate to pass")
    if report_status == "blocked" and all(status == "pass" for status in gate_statuses):
        errors.append("release_report.status blocked requires at least one non-passing gate")
    return errors


def _real_repo_bug_run_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    failing = report.get("failing_command", {})
    passing = report.get("passing_command", {})
    if isinstance(failing, dict):
        errors.extend(_command_evidence_errors(failing, path="real_repo_bug_run.failing_command"))
        if failing.get("commit_sha") != report.get("bug_commit"):
            errors.append("real_repo_bug_run.failing_command.commit_sha must match bug_commit")
    else:
        errors.append("real_repo_bug_run.failing_command must be an object")
    if isinstance(passing, dict):
        errors.extend(_command_evidence_errors(passing, path="real_repo_bug_run.passing_command"))
        if passing.get("commit_sha") != report.get("fix_commit"):
            errors.append("real_repo_bug_run.passing_command.commit_sha must match fix_commit")
    else:
        errors.append("real_repo_bug_run.passing_command must be an object")
    repo_url = report.get("repo_url")
    public_refs = report.get("public_refs", [])
    if isinstance(repo_url, str) and isinstance(public_refs, list) and repo_url not in public_refs:
        errors.append("real_repo_bug_run.public_refs must include repo_url")
    fix_branch = report.get("fix_branch")
    if isinstance(repo_url, str) and isinstance(fix_branch, str) and isinstance(public_refs, list):
        expected_fix_ref = f"{repo_url}/tree/{fix_branch}"
        if expected_fix_ref not in public_refs:
            errors.append("real_repo_bug_run.public_refs must include fix branch URL")
    return errors


def _checkpoint_branch_replay_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    checkpoints = report.get("checkpoints", {})
    if not isinstance(checkpoints, dict):
        return ["checkpoint_branch_replay.checkpoints must be an object"]
    seen_statuses: set[str] = set()
    for index, state in enumerate(report.get("states", [])):
        if not isinstance(state, dict):
            errors.append(f"checkpoint_branch_replay.states[{index}] must be an object")
            continue
        stage = state.get("stage")
        status = state.get("status")
        if isinstance(status, str):
            seen_statuses.add(status)
        checkpoint = checkpoints.get(stage) if isinstance(stage, str) else None
        if not isinstance(checkpoint, dict):
            errors.append(f"checkpoint_branch_replay.states[{index}] references missing checkpoint")
            continue
        if checkpoint.get("status") != status:
            errors.append(f"checkpoint_branch_replay checkpoint {stage!r} status must match states[{index}]")
    missing = {"awaiting_human", "failed", "blocked"} - seen_statuses
    if missing:
        errors.append(f"checkpoint_branch_replay.states missing statuses {sorted(missing)}")
    return errors


def _autonomy_run_report_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    decision = report.get("decision")
    commands = report.get("commands", [])
    if not isinstance(commands, list):
        return ["autonomy_run_report.commands must be an array"]
    for index, command in enumerate(commands):
        errors.extend(_command_evidence_errors(command, path=f"autonomy_run_report.commands[{index}]"))
    statuses = [command.get("status") for command in commands if isinstance(command, dict)]
    if decision == "executed" and (not statuses or any(status != "pass" for status in statuses)):
        errors.append("autonomy_run_report.decision executed requires passing commands")
    if decision == "failed" and "fail" not in statuses:
        errors.append("autonomy_run_report.decision failed requires a failed command")
    if decision == "blocked" and commands:
        errors.append("autonomy_run_report.decision blocked must not execute commands")
    if decision == "blocked" and not report.get("boundaries"):
        errors.append("autonomy_run_report.decision blocked requires boundaries")
    return errors


def _real_repo_corpus_report_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    entries = report.get("entries", [])
    if not isinstance(entries, list):
        return ["real_repo_corpus_report.entries must be an array"]
    repo_urls = {
        entry.get("repo_url")
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("repo_url"), str)
    }
    non_author_repo_urls = {
        entry.get("repo_url")
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("repo_url"), str)
        and entry.get("author_owned") is False
    }
    failure_families = {
        entry.get("failure_family")
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("failure_family"), str)
    }
    artifact_files = [
        entry.get("artifact_file")
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("artifact_file"), str)
    ]
    run_ids = [
        entry.get("run_id")
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("run_id"), str)
    ]
    command_timestamps: list[dt.datetime] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        for field in ("failing_command_timestamp", "passing_command_timestamp"):
            parsed = _parse_iso8601(entry.get(field))
            if parsed is None:
                errors.append(f"real_repo_corpus_report.entries[{index}].{field} must be ISO-8601")
            else:
                command_timestamps.append(parsed)
    if len(set(artifact_files)) != len(artifact_files):
        errors.append("real_repo_corpus_report.entries artifact_file values must be unique")
    if len(set(run_ids)) != len(run_ids):
        errors.append("real_repo_corpus_report.entries run_id values must be unique")
    summary = report.get("summary", {})
    if not isinstance(summary, dict):
        return ["real_repo_corpus_report.summary must be an object"]
    expected_target_met = (
        len(non_author_repo_urls) >= report.get("target_repo_count", 0)
        and len(failure_families) >= report.get("target_failure_family_count", 0)
    )
    checks = {
        "total_repo_count": len(repo_urls),
        "non_author_repo_count": len(non_author_repo_urls),
        "failure_family_count": len(failure_families),
        "missing_non_author_repos": max(0, report.get("target_repo_count", 0) - len(non_author_repo_urls)),
        "missing_failure_families": max(0, report.get("target_failure_family_count", 0) - len(failure_families)),
        "target_met": expected_target_met,
    }
    for field, expected in checks.items():
        if summary.get(field) != expected:
            errors.append(f"real_repo_corpus_report.summary.{field} must be {expected!r}")
    freshness = report.get("freshness", {})
    if not isinstance(freshness, dict):
        errors.append("real_repo_corpus_report.freshness must be an object")
    else:
        if freshness.get("fresh_artifact_count") != len(entries):
            errors.append("real_repo_corpus_report.freshness.fresh_artifact_count must match entries")
        if freshness.get("unique_artifact_count") != len(set(artifact_files)):
            errors.append("real_repo_corpus_report.freshness.unique_artifact_count must match unique artifact files")
        if expected_target_met and freshness.get("producer_roundtrip_status") != "pass":
            errors.append("real_repo_corpus_report.freshness.producer_roundtrip_status must be pass when target is met")
        if freshness.get("artifact_source") == "live_external_rerun" and freshness.get("external_execution") is not True:
            errors.append("real_repo_corpus_report.freshness.external_execution must be true for live_external_rerun")
        if freshness.get("artifact_source") != "live_external_rerun" and freshness.get("external_execution") is not False:
            errors.append("real_repo_corpus_report.freshness.external_execution must be false without live_external_rerun")
        live_count = freshness.get("live_artifact_count")
        fresh_live_count = freshness.get("fresh_live_artifact_count")
        stale_live_count = freshness.get("stale_live_artifact_count")
        if all(isinstance(value, int) and not isinstance(value, bool) for value in (live_count, fresh_live_count, stale_live_count)):
            if live_count != fresh_live_count + stale_live_count:
                errors.append("real_repo_corpus_report.freshness live and stale artifact counts must balance")
            if freshness.get("artifact_source") == "live_external_rerun":
                if live_count != len(entries) or fresh_live_count != len(entries) or stale_live_count != 0:
                    errors.append("real_repo_corpus_report.freshness.live_external_rerun requires fresh live artifacts for every entry")
        oldest_timestamp = _parse_iso8601(freshness.get("oldest_command_timestamp"))
        latest_timestamp = _parse_iso8601(freshness.get("latest_command_timestamp"))
        if oldest_timestamp is None:
            errors.append("real_repo_corpus_report.freshness.oldest_command_timestamp must be ISO-8601")
        if latest_timestamp is None:
            errors.append("real_repo_corpus_report.freshness.latest_command_timestamp must be ISO-8601")
        if oldest_timestamp is not None and latest_timestamp is not None:
            if oldest_timestamp > latest_timestamp:
                errors.append("real_repo_corpus_report.freshness.oldest_command_timestamp must not be after latest_command_timestamp")
            if command_timestamps:
                if oldest_timestamp != min(command_timestamps):
                    errors.append("real_repo_corpus_report.freshness.oldest_command_timestamp must match entries")
                if latest_timestamp != max(command_timestamps):
                    errors.append("real_repo_corpus_report.freshness.latest_command_timestamp must match entries")
    boundary = report.get("approval_boundary", {})
    if not isinstance(boundary, dict):
        errors.append("real_repo_corpus_report.approval_boundary must be an object")
        return errors
    if expected_target_met:
        if boundary.get("required") is not False:
            errors.append("real_repo_corpus_report.approval_boundary.required must be False when target is met")
        if boundary.get("status") != "not_required":
            errors.append("real_repo_corpus_report.approval_boundary.status must be not_required when target is met")
        if boundary.get("requested_actions") != []:
            errors.append("real_repo_corpus_report.approval_boundary.requested_actions must be empty when target is met")
        if boundary.get("prohibited_without_approval") != []:
            errors.append("real_repo_corpus_report.approval_boundary.prohibited_without_approval must be empty when target is met")
    else:
        if boundary.get("required") is not True:
            errors.append("real_repo_corpus_report.approval_boundary.required must be True until target is met")
        if boundary.get("status") != "awaiting_human":
            errors.append("real_repo_corpus_report.approval_boundary.status must be awaiting_human until target is met")
        requested_actions = boundary.get("requested_actions")
        if not isinstance(requested_actions, list) or not requested_actions:
            errors.append("real_repo_corpus_report.approval_boundary.requested_actions must name the approval request")
        prohibited = boundary.get("prohibited_without_approval")
        if not isinstance(prohibited, list) or "cloning third-party repositories" not in prohibited:
            errors.append("real_repo_corpus_report.approval_boundary.prohibited_without_approval must block third-party cloning")
    return errors


def _agent_battle_harness_report_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    protocol = report.get("protocol", {})
    judges = report.get("judges", [])
    veto = report.get("critic_veto", {})
    audit = report.get("judge_audit", [])
    outcome = report.get("outcome", {})
    if not isinstance(protocol, dict):
        return ["agent_battle_harness_report.protocol must be an object"]
    if not isinstance(judges, list):
        return ["agent_battle_harness_report.judges must be an array"]
    evidence_mode = protocol.get("evidence_mode")
    independent_contexts = protocol.get("independent_contexts")
    if evidence_mode == "derived_report" and independent_contexts is not False:
        errors.append("agent_battle_harness_report.protocol.independent_contexts must be false for derived_report")
    if evidence_mode == "independent_agent_reports" and independent_contexts is not True:
        errors.append("agent_battle_harness_report.protocol.independent_contexts must be true for independent_agent_reports")
    if evidence_mode == "independent_agent_reports" and isinstance(outcome, dict) and outcome.get("verdict") == "advance":
        errors.extend(_agent_battle_input_report_readiness_errors(report))
    judge_role_list = [judge.get("role") for judge in judges if isinstance(judge, dict)]
    judge_roles = set(judge_role_list)
    required_roles = set(protocol.get("required_judges", []))
    missing_roles = required_roles - judge_roles
    if missing_roles:
        errors.append(f"agent_battle_harness_report.judges missing required roles {sorted(missing_roles)}")
    judge_sources = [
        judge.get("source")
        for judge in judges
        if isinstance(judge, dict)
    ]
    if independent_contexts is True and any(source != "external_agent_report" for source in judge_sources):
        errors.append("agent_battle_harness_report.judges.source must be external_agent_report for independent contexts")
    if evidence_mode == "independent_agent_reports":
        run_id = report.get("run_id")
        duplicate_roles = sorted(
            {
                role
                for role in judge_role_list
                if isinstance(role, str) and judge_role_list.count(role) > 1
            }
        )
        unexpected_roles = sorted(role for role in judge_roles - required_roles if isinstance(role, str))
        if duplicate_roles:
            errors.append(f"agent_battle_harness_report.judges duplicate roles {duplicate_roles}")
        if unexpected_roles:
            errors.append(f"agent_battle_harness_report.judges unexpected roles {unexpected_roles}")
        if len(judges) != len(required_roles):
            errors.append("agent_battle_harness_report.judges must contain exactly one report per required role")
        mismatched_run_ids = [
            judge.get("role")
            for judge in judges
            if isinstance(judge, dict) and judge.get("input_run_id") != run_id
        ]
        if mismatched_run_ids:
            errors.append(f"agent_battle_harness_report.judges input_run_id mismatch for {mismatched_run_ids}")
        non_advance_judges = [
            judge.get("role")
            for judge in judges
            if isinstance(judge, dict) and judge.get("verdict") != "advance"
        ]
        if non_advance_judges and isinstance(outcome, dict) and outcome.get("verdict") == "advance":
            errors.append(f"agent_battle_harness_report.judges verdict blocks advance for {non_advance_judges}")
        minimum_score = float(protocol.get("minimum_score_to_advance", 95))
        low_score_judges = [
            judge.get("role")
            for judge in judges
            if isinstance(judge, dict) and float(judge.get("score", 0)) < minimum_score
        ]
        if low_score_judges and isinstance(outcome, dict) and outcome.get("verdict") == "advance":
            errors.append(f"agent_battle_harness_report.judges score below minimum for {low_score_judges}")
        source_reports = [
            judge.get("source_report")
            for judge in judges
            if isinstance(judge, dict) and isinstance(judge.get("source_report"), str)
        ]
        invalid_sources = [
            source
            for source in source_reports
            if not source.startswith("codex-subagent://")
        ]
        if invalid_sources:
            errors.append("agent_battle_harness_report.judges.source_report must reference codex-subagent sources")
        if len(set(source_reports)) != len(source_reports):
            errors.append("agent_battle_harness_report.judges.source_report values must be unique")
    if protocol.get("blind_review") is True:
        for index, judge in enumerate(judges):
            if isinstance(judge, dict) and judge.get("peer_scores_visible") is not False:
                errors.append(f"agent_battle_harness_report.judges[{index}].peer_scores_visible must be False under blind_review")
    active_judge_vetoes = [
        judge.get("role")
        for judge in judges
        if isinstance(judge, dict)
        and isinstance(judge.get("veto_vote"), dict)
        and judge["veto_vote"].get("active") is True
    ]
    veto_active = isinstance(veto, dict) and veto.get("active") is True
    if active_judge_vetoes and not veto_active:
        errors.append("agent_battle_harness_report.critic_veto.active must reflect active judge vetoes")
    if veto_active and isinstance(outcome, dict) and outcome.get("verdict") == "advance":
        errors.append("agent_battle_harness_report.outcome.verdict cannot advance while critic_veto is active")
    if evidence_mode == "derived_report" and isinstance(outcome, dict) and outcome.get("verdict") == "advance":
        errors.append("agent_battle_harness_report.outcome.verdict cannot advance from derived_report evidence")
    if isinstance(audit, list):
        failed_audits = [item for item in audit if isinstance(item, dict) and item.get("status") == "fail"]
        if failed_audits and isinstance(outcome, dict) and outcome.get("verdict") == "advance":
            errors.append("agent_battle_harness_report.outcome.verdict cannot advance with failed judge_audit checks")
    return errors


def _agent_battle_input_report_readiness_errors(report: dict[str, Any]) -> list[str]:
    input_reports = report.get("input_reports", {})
    if not isinstance(input_reports, dict):
        return ["agent_battle_harness_report.input_reports must be an object"]

    errors: list[str] = []
    loaded_reports: dict[str, dict[str, Any]] = {}
    bindings: tuple[tuple[str, str], ...] = (
        ("self_assessment_report", "self_assessment_run_id"),
        ("battle_report", "battle_report_run_id"),
    )
    for ref_key, run_id_key in bindings:
        ref = input_reports.get(ref_key)
        expected_run_id = input_reports.get(run_id_key)
        if not isinstance(ref, str) or not ref:
            errors.append(f"agent_battle_harness_report.input_reports.{ref_key} must be a file path")
            continue
        if ref.startswith("generated:"):
            errors.append(
                f"agent_battle_harness_report.input_reports.{ref_key} must be a real file for advance"
            )
            continue
        if not isinstance(expected_run_id, str) or not expected_run_id:
            errors.append(
                f"agent_battle_harness_report.input_reports.{run_id_key} must be recorded"
            )
            continue
        path = Path(ref)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent / path
        try:
            source_report = load_json(path)
        except OSError:
            errors.append(
                f"agent_battle_harness_report.input_reports.{ref_key} file is not readable"
            )
            continue
        except (json.JSONDecodeError, ContractError):
            errors.append(
                f"agent_battle_harness_report.input_reports.{ref_key} file must contain a JSON object"
            )
            continue
        actual_run_id = source_report.get("run_id")
        if actual_run_id != expected_run_id:
            errors.append(
                f"agent_battle_harness_report.input_reports.{run_id_key} "
                f"{expected_run_id!r} does not match {ref_key}.run_id {actual_run_id!r}"
            )
        loaded_reports[ref_key] = source_report
    if errors:
        return errors

    self_report = loaded_reports["self_assessment_report"]
    battle_report = loaded_reports["battle_report"]
    errors.extend(_self_assessment_pre_battle_readiness_errors(self_report))
    errors.extend(_battle_report_pre_battle_readiness_errors(battle_report))
    return errors


def _self_assessment_pre_battle_readiness_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    dimensions = report.get("dimensions", [])
    if not isinstance(dimensions, list):
        return [
            "agent_battle_harness_report.input_reports.self_assessment_report "
            "must include dimensions for pre-battle readiness"
        ]
    by_name: dict[str, float] = {}
    for dimension in dimensions:
        if not isinstance(dimension, dict):
            continue
        name = dimension.get("name")
        try:
            score = float(dimension.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        if isinstance(name, str):
            by_name[name] = score
    for name in PRE_BATTLE_DIMENSIONS:
        if name not in by_name:
            errors.append(
                "agent_battle_harness_report.input_reports.self_assessment_report "
                f"missing pre-battle dimension {name!r}"
            )
        elif by_name[name] < 95:
            errors.append(
                "agent_battle_harness_report.input_reports.self_assessment_report "
                f"dimension {name!r} must score at least 95 before independent battle advance"
            )
    next_actions = report.get("next_actions", [])
    if not isinstance(next_actions, list):
        errors.append(
            "agent_battle_harness_report.input_reports.self_assessment_report "
            "next_actions must be an array"
        )
    else:
        malformed_actions = [action for action in next_actions if not isinstance(action, dict)]
        unexpected_actions = [
            action.get("id")
            for action in next_actions
            if isinstance(action, dict)
            and action.get("id") not in ALLOWED_SELF_ASSESS_BATTLE_ACTIONS
        ]
        if malformed_actions:
            errors.append(
                "agent_battle_harness_report.input_reports.self_assessment_report "
                "next_actions must contain objects"
            )
        if unexpected_actions:
            errors.append(
                "agent_battle_harness_report.input_reports.self_assessment_report "
                f"has unresolved non-battle next_actions {unexpected_actions}"
            )
    return errors


def _battle_report_pre_battle_readiness_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if report.get("verdict") == "needs_human":
        errors.append(
            "agent_battle_harness_report.input_reports.battle_report "
            "needs human resolution before independent battle advance"
        )
    next_actions = report.get("next_actions", [])
    if not isinstance(next_actions, list):
        return [
            "agent_battle_harness_report.input_reports.battle_report "
            "next_actions must be an array"
        ]
    unexpected_actions = [
        action for action in next_actions if action not in ALLOWED_BATTLE_REPORT_ACTIONS
    ]
    if unexpected_actions:
        errors.append(
            "agent_battle_harness_report.input_reports.battle_report "
            f"has unresolved non-battle next_actions {unexpected_actions}"
        )
    return errors


def _approval_decision_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if report.get("decision") == "approved" and not report.get("evidence_refs"):
        errors.append("approval_decision.evidence_refs must be non-empty when approved")
    if report.get("decision") == "rejected" and "reject" not in str(report.get("reason", "")).lower():
        errors.append("approval_decision.reason should explain rejection when decision is rejected")
    return errors


def _agent_judge_report_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    source_report = report.get("source_report")
    if isinstance(source_report, str) and not source_report.startswith("codex-subagent://"):
        errors.append("agent_judge_report.source_report must start with codex-subagent://")
    if report.get("verdict") == "advance" and report.get("score", 0) < 95:
        errors.append("agent_judge_report.score must be at least 95 when verdict is advance")
    if report.get("verdict") != "advance" and report.get("score", 100) >= 95:
        errors.append("agent_judge_report.score must stay below 95 unless verdict is advance")
    if report.get("veto_active") is True and report.get("verdict") == "advance":
        errors.append("agent_judge_report.verdict cannot advance while veto_active is true")
    return errors


def _command_evidence_errors(command: dict[str, Any], *, path: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(command, dict):
        return [f"{path} must be an object"]

    status = command.get("status")
    exit_code = command.get("exit_code")
    if status == "pass" and exit_code != 0:
        errors.append(f"{path}.exit_code must be 0 when status is pass")
    if status in {"fail", "blocked"} and exit_code == 0:
        errors.append(f"{path}.exit_code must be non-zero when status is {status}")

    for digest_field in ("stdout_digest", "stderr_digest"):
        digest = command.get(digest_field)
        if isinstance(digest, str) and not digest.startswith("sha256:"):
            errors.append(f"{path}.{digest_field} must start with sha256:")

    timestamp = command.get("timestamp")
    if isinstance(timestamp, str) and _parse_iso8601(timestamp) is None:
        errors.append(f"{path}.timestamp must be ISO-8601")
    return errors


def _parse_iso8601(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


class CheckpointStore:
    """Writes resumable stage checkpoints and cumulative decision logs."""

    def __init__(
        self,
        root: Path,
        schemas: SchemaRegistry,
        role_policy: "RolePolicy | None" = None,
    ) -> None:
        self.root = root
        self.schemas = schemas
        self.role_policy = role_policy
        self._trusted_approval_sources: set[Path] = set()

    def _trust_runtime_approval_source(self, path: Path) -> None:
        self._trusted_approval_sources.add(path.resolve())

    def reset(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str, pipeline_name: str) -> Path:
        return self.root / run_id / pipeline_name

    def checkpoint_path(self, run_id: str, stage_name: str, pipeline_name: str | None = None) -> Path:
        if pipeline_name is None:
            return self.root / run_id / f"checkpoint_{stage_name}.json"
        return self.run_dir(run_id, pipeline_name) / f"checkpoint_{stage_name}.json"

    def read_checkpoint(self, run_id: str, stage_name: str, pipeline_name: str | None = None) -> dict[str, Any] | None:
        path = self.checkpoint_path(run_id, stage_name, pipeline_name)
        if not path.exists():
            if pipeline_name is not None:
                return None
            legacy_path = path
            candidates = sorted((self.root / run_id).glob(f"*/checkpoint_{stage_name}.json"))
            if not candidates:
                return None
            if len(candidates) > 1:
                raise ContractError(
                    f"checkpoint {stage_name!r} for run_id {run_id!r} is ambiguous; "
                    "provide pipeline_name"
                )
            path = candidates[0]
            if path == legacy_path:
                return None
        if not path.exists():
            return None
        return load_json(path)

    def write(
        self,
        manifest: PipelineManifest,
        run_id: str,
        stage_name: str,
        *,
        status: str,
        artifacts: dict[str, dict[str, Any]],
        review: ReviewResult | None = None,
        metadata: dict[str, Any] | None = None,
        actor_role: str | None = None,
    ) -> Path:
        if status not in {"completed", "failed", "awaiting_human", "blocked", "in_progress"}:
            raise ContractError(f"invalid checkpoint status {status!r}")
        stage = manifest.get_stage(stage_name)
        if self.role_policy and status in {"completed", "awaiting_human"}:
            actor_role = actor_role or self.role_policy.owner_for_stage(stage_name)
        merged_metadata = dict(metadata or {})
        if status in {"completed", "awaiting_human"}:
            merged_metadata.setdefault("tools_available", list(stage.get("tools_available", [])))
            merged_metadata.setdefault("tools_used", [])
        self._validate_stage_write(
            manifest,
            run_id,
            stage_name,
            status,
            artifacts,
            review,
            actor_role=actor_role,
            metadata=merged_metadata,
        )
        if status in {"completed", "awaiting_human"}:
            for produced in stage.get("produces", []):
                if produced not in artifacts:
                    raise ContractError(f"{stage_name} missing produced artifact {produced}")
        for name, artifact in artifacts.items():
            if name in self.schemas.schemas:
                self.schemas.validate(name, artifact)

        run_dir = self.run_dir(run_id, manifest.name)
        run_dir.mkdir(parents=True, exist_ok=True)
        if "decision_log" in artifacts:
            if artifacts["decision_log"].get("run_id") != run_id:
                raise ContractError(
                    f"decision_log.run_id {artifacts['decision_log'].get('run_id')!r} "
                    f"does not match checkpoint run_id {run_id!r}"
            )
            self._merge_decision_log(run_dir, run_id, artifacts["decision_log"])

        if actor_role:
            merged_metadata["actor_role"] = actor_role
        required_refs = self._required_artifact_refs(manifest, run_id, stage_name)
        if required_refs:
            merged_metadata["input_artifact_refs"] = required_refs

        checkpoint = {
            "version": "1.0",
            "run_id": run_id,
            "pipeline": manifest.name,
            "pipeline_version": manifest.version,
            "stage": stage_name,
            "status": status,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "artifacts": artifacts,
            "review": review.to_dict() if review else None,
            "metadata": merged_metadata,
        }
        path = self.checkpoint_path(run_id, stage_name, manifest.name)
        path.write_text(json.dumps(checkpoint, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def _validate_stage_write(
        self,
        manifest: PipelineManifest,
        run_id: str,
        stage_name: str,
        status: str,
        artifacts: dict[str, dict[str, Any]],
        review: ReviewResult | None,
        actor_role: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"completed", "awaiting_human"}:
            return

        stage = manifest.get_stage(stage_name)
        produced = set(stage.get("produces", []))
        special_artifacts = {"decision_log", "review", "approval_decision"}
        extra = sorted(set(artifacts) - produced - special_artifacts)
        if extra:
            raise ContractError(
                f"{stage_name} checkpoint includes artifacts not declared in produces: {extra}"
            )

        if review is None or review.decision != "pass":
            raise ContractError(f"{stage_name} requires a passing review before checkpoint")

        self._validate_tool_usage(stage_name, stage, metadata or {})

        stage_names = manifest.stage_names()
        current_index = stage_names.index(stage_name)
        for prior_stage in stage_names[:current_index]:
            prior = self.read_checkpoint(run_id, prior_stage, manifest.name)
            if prior is None or prior.get("status") != "completed":
                raise ContractError(
                    f"{stage_name} cannot complete before prior stage {prior_stage!r}"
                )

        prior_artifacts = self._prior_completed_artifacts(manifest, run_id, stage_name)
        missing_inputs = [
            name
            for name in stage.get("required_artifacts_in", [])
            if name not in prior_artifacts
        ]
        if missing_inputs:
            raise ContractError(
                f"{stage_name} missing required input artifacts from prior checkpoints: "
                f"{missing_inputs}"
            )
        prior_sources = self._prior_completed_artifact_sources(manifest, run_id, stage_name)
        if self.role_policy:
            if actor_role is None:
                raise ContractError(f"{stage_name} requires actor_role under role policy")
            self.role_policy.validate_stage_write(
                manifest,
                run_id,
                stage_name,
                actor_role,
                artifacts,
                review=review,
                prior_sources=prior_sources,
                require_approval=status == "completed",
            )
            if status == "completed":
                self._validate_approval_source_checkpoint(
                    manifest,
                    run_id,
                    stage_name,
                    actor_role,
                    artifacts,
                )
        if "decision_log" in artifacts:
            self._validate_decision_log_context(
                manifest,
                run_id,
                stage_name,
                artifacts["decision_log"],
                current_artifacts=artifacts,
                prior_artifacts=prior_artifacts,
            )
        self._validate_artifact_refs(
            stage_name,
            artifacts,
            known_artifacts=set(prior_artifacts) | set(artifacts),
        )

    def _validate_tool_usage(
        self,
        stage_name: str,
        stage: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        tools_available = metadata.get("tools_available")
        tools_used = metadata.get("tools_used")
        if not isinstance(tools_available, list) or not all(isinstance(item, str) for item in tools_available):
            raise ContractError(f"{stage_name} metadata.tools_available must be a list of strings")
        if not isinstance(tools_used, list) or not all(isinstance(item, str) for item in tools_used):
            raise ContractError(f"{stage_name} metadata.tools_used must be a list of strings")
        declared = set(stage.get("tools_available", []))
        if set(tools_available) != declared:
            raise ContractError(
                f"{stage_name} metadata.tools_available must match manifest tools_available"
            )
        undeclared = sorted(set(tools_used) - declared)
        if undeclared:
            raise ContractError(f"{stage_name} used tools not declared by manifest: {undeclared}")

    def _validate_decision_log_context(
        self,
        manifest: PipelineManifest,
        run_id: str,
        stage_name: str,
        decision_log: dict[str, Any],
        *,
        current_artifacts: dict[str, dict[str, Any]],
        prior_artifacts: dict[str, dict[str, Any]],
    ) -> None:
        if decision_log.get("run_id") != run_id:
            raise ContractError(
                f"decision_log.run_id {decision_log.get('run_id')!r} "
                f"does not match checkpoint run_id {run_id!r}"
            )
        known_artifacts = set(prior_artifacts) | set(current_artifacts)
        stage_names = manifest.stage_names()
        current_index = stage_names.index(stage_name)
        valid_checkpoint_refs = {
            str(self.checkpoint_path(run_id, prior_stage, manifest.name))
            for prior_stage in stage_names[:current_index]
        } | set(stage_names[:current_index])

        for decision in decision_log.get("decisions", []):
            if decision.get("stage") != stage_name:
                raise ContractError(
                    f"{stage_name} decision {decision.get('decision_id')!r} "
                    f"declares stage {decision.get('stage')!r}"
                )
            option_ids = {
                option.get("option_id")
                for option in decision.get("options_considered", [])
                if isinstance(option, dict)
            }
            if decision.get("selected") not in option_ids:
                raise ContractError(
                    f"{stage_name} decision {decision.get('decision_id')!r} "
                    f"selected unknown option {decision.get('selected')!r}"
                )
            for artifact_ref in decision.get("artifact_refs", []):
                if artifact_ref not in known_artifacts:
                    raise ContractError(
                        f"{stage_name} decision {decision.get('decision_id')!r} "
                        f"references unknown artifact {artifact_ref!r}"
                    )
            for checkpoint_ref in decision.get("checkpoint_refs", []):
                if checkpoint_ref not in valid_checkpoint_refs:
                    raise ContractError(
                        f"{stage_name} decision {decision.get('decision_id')!r} "
                        f"references unknown checkpoint {checkpoint_ref!r}"
                    )

    def _validate_artifact_refs(
        self,
        stage_name: str,
        artifacts: dict[str, dict[str, Any]],
        *,
        known_artifacts: set[str],
    ) -> None:
        if "verification_report" in artifacts:
            for command in artifacts["verification_report"].get("commands", []):
                for artifact_ref in command.get("artifact_refs", []):
                    if artifact_ref not in known_artifacts:
                        raise ContractError(
                            f"{stage_name} verification command references unknown "
                            f"artifact {artifact_ref!r}"
                        )
        if "release_report" in artifacts:
            for gate in artifacts["release_report"].get("gates", []):
                evidence = gate.get("evidence")
                if not isinstance(evidence, dict):
                    continue
                for artifact_ref in evidence.get("artifact_refs", []):
                    if artifact_ref not in known_artifacts:
                        raise ContractError(
                            f"{stage_name} release gate {gate.get('name')!r} "
                            f"references unknown artifact {artifact_ref!r}"
                        )

    def _validate_approval_source_checkpoint(
        self,
        manifest: PipelineManifest,
        run_id: str,
        stage_name: str,
        actor_role: str,
        artifacts: dict[str, dict[str, Any]],
    ) -> None:
        if not self.role_policy:
            return
        role = self.role_policy.roles.get(actor_role)
        if role is None or not role.approval_by:
            return

        approval = artifacts.get("approval_decision")
        if not isinstance(approval, dict):
            return
        source_ref = approval.get("source_checkpoint")
        if not isinstance(source_ref, str) or not source_ref:
            raise ContractError(
                f"approval_decision.source_checkpoint is required for {stage_name!r}"
            )

        source_path = Path(source_ref)
        try:
            resolved_source = source_path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ContractError(
                f"approval_decision.source_checkpoint does not exist: {source_ref!r}"
            ) from exc
        root = self.root.resolve()
        if root != resolved_source and root not in resolved_source.parents:
            raise ContractError(
                "approval_decision.source_checkpoint must be inside the checkpoint store root"
            )

        source_checkpoint = load_json(resolved_source)
        if source_checkpoint.get("status") != "completed":
            raise ContractError("approval source checkpoint must be completed")
        if source_checkpoint.get("run_id") != run_id:
            raise ContractError(
                f"approval source run_id {source_checkpoint.get('run_id')!r} "
                f"does not match {run_id!r}"
            )
        if source_checkpoint.get("pipeline") != "approval-decision":
            raise ContractError(
                "approval_decision.source_checkpoint must reference an approval-decision pipeline"
            )
        if source_checkpoint.get("version") != "1.0":
            raise ContractError("approval source checkpoint.version must be 1.0")
        if not isinstance(source_checkpoint.get("timestamp"), str) or not source_checkpoint.get("timestamp"):
            raise ContractError("approval source checkpoint.timestamp is required")

        metadata = source_checkpoint.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ContractError("approval source checkpoint metadata must be an object")
        approver_role = approval.get("approver_role")
        if not isinstance(approver_role, str) or not approver_role:
            raise ContractError("approval_decision.approver_role is required")
        expected_stage = _approval_stage_name(stage_name, approver_role)
        if source_checkpoint.get("stage") != expected_stage:
            raise ContractError(
                f"approval source checkpoint stage {source_checkpoint.get('stage')!r} "
                f"does not match expected approval stage {expected_stage!r}"
            )
        expected_path = self.checkpoint_path(run_id, expected_stage, "approval-decision").resolve()
        if resolved_source != expected_path:
            raise ContractError(
                "approval_decision.source_checkpoint must reference the expected approval checkpoint path"
            )
        review = source_checkpoint.get("review")
        if not isinstance(review, dict) or review.get("decision") != "pass":
            raise ContractError("approval source checkpoint review.decision must be pass")
        if review.get("stage") != expected_stage:
            raise ContractError("approval source checkpoint review.stage must match approval stage")
        if resolved_source not in self._trusted_approval_sources:
            raise ContractError(
                "approval source checkpoint was not produced by a trusted runtime approval gate"
            )
        if metadata.get("actor_role") != approver_role:
            raise ContractError(
                f"approval source actor_role {metadata.get('actor_role')!r} "
                f"does not match approval_decision.approver_role {approver_role!r}"
            )
        if metadata.get("approval_for_pipeline") != manifest.name:
            raise ContractError(
                "approval source checkpoint does not target this pipeline"
            )
        if metadata.get("approval_for_stage") != stage_name:
            raise ContractError(
                "approval source checkpoint does not target this stage"
            )

        source_artifacts = source_checkpoint.get("artifacts", {})
        if not isinstance(source_artifacts, dict):
            raise ContractError("approval source checkpoint artifacts must be an object")
        source_approval = source_artifacts.get("approval_decision")
        if not isinstance(source_approval, dict):
            raise ContractError("approval source checkpoint missing approval_decision")
        self.schemas.validate("approval_decision", source_approval)
        for detail in _approval_decision_errors(source_approval):
            raise ContractError(detail)
        if "source_checkpoint" in source_approval:
            raise ContractError("approval source checkpoint must not be recursively sourced")
        for field in ("version", "run_id", "stage", "approver_role", "decision", "reason", "evidence_refs"):
            if source_approval.get(field) != approval.get(field):
                raise ContractError(
                    f"approval source {field} {source_approval.get(field)!r} "
                    f"does not match target approval_decision {approval.get(field)!r}"
                )

    def _prior_completed_artifacts(
        self, manifest: PipelineManifest, run_id: str, stage_name: str
    ) -> dict[str, dict[str, Any]]:
        artifacts: dict[str, dict[str, Any]] = {}
        for prior_stage in manifest.stage_names()[: manifest.stage_names().index(stage_name)]:
            checkpoint = self.read_checkpoint(run_id, prior_stage, manifest.name)
            if checkpoint is None or checkpoint.get("status") != "completed":
                continue
            payload = checkpoint.get("artifacts", {})
            if isinstance(payload, dict):
                for name, artifact in payload.items():
                    if isinstance(artifact, dict):
                        artifacts[name] = artifact
        return artifacts

    def _prior_completed_artifact_sources(
        self, manifest: PipelineManifest, run_id: str, stage_name: str
    ) -> dict[str, dict[str, str]]:
        sources: dict[str, dict[str, str]] = {}
        for prior_stage in manifest.stage_names()[: manifest.stage_names().index(stage_name)]:
            checkpoint = self.read_checkpoint(run_id, prior_stage, manifest.name)
            if checkpoint is None or checkpoint.get("status") != "completed":
                continue
            metadata = checkpoint.get("metadata", {})
            role = metadata.get("actor_role") if isinstance(metadata, dict) else None
            review = checkpoint.get("review", {})
            review_decision = review.get("decision") if isinstance(review, dict) else None
            payload = checkpoint.get("artifacts", {})
            if not isinstance(payload, dict):
                continue
            for name, artifact in payload.items():
                if isinstance(artifact, dict):
                    sources[name] = {
                        "stage": prior_stage,
                        "role": role or "",
                        "review": review_decision or "",
                    }
        return sources

    def _required_artifact_refs(
        self, manifest: PipelineManifest, run_id: str, stage_name: str
    ) -> dict[str, str]:
        refs: dict[str, str] = {}
        stage = manifest.get_stage(stage_name)
        required = set(stage.get("required_artifacts_in", []))
        if not required:
            return refs
        for prior_stage in manifest.stage_names()[: manifest.stage_names().index(stage_name)]:
            checkpoint_path = self.checkpoint_path(run_id, prior_stage, manifest.name)
            checkpoint = self.read_checkpoint(run_id, prior_stage, manifest.name)
            if checkpoint is None or checkpoint.get("status") != "completed":
                continue
            payload = checkpoint.get("artifacts", {})
            if not isinstance(payload, dict):
                continue
            for artifact_name in required:
                if artifact_name in payload and artifact_name not in refs:
                    refs[artifact_name] = str(checkpoint_path)
        return refs

    def _merge_decision_log(self, run_dir: Path, run_id: str, new_log: dict[str, Any]) -> None:
        path = run_dir / "decision_log.json"
        if path.exists():
            existing = load_json(path)
            if existing.get("run_id") != run_id:
                raise ContractError(
                    f"existing decision log run_id {existing.get('run_id')!r} "
                    f"does not match checkpoint run_id {run_id!r}"
                )
        else:
            existing = {
                "version": "1.0",
                "run_id": run_id,
                "decisions": [],
            }
        if new_log.get("run_id") != run_id:
            raise ContractError(
                f"new decision log run_id {new_log.get('run_id')!r} "
                f"does not match checkpoint run_id {run_id!r}"
            )
        existing_by_id = {
            item["decision_id"]: item
            for item in existing.get("decisions", [])
            if isinstance(item, dict) and "decision_id" in item
        }
        for decision in new_log.get("decisions", []):
            decision_id = decision.get("decision_id")
            if decision_id in existing_by_id:
                if decision != existing_by_id[decision_id]:
                    raise ContractError(f"conflicting decision_id {decision_id!r}")
                continue
            if decision_id is not None:
                existing["decisions"].append(decision)
                existing_by_id[decision_id] = decision
        path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")


@dataclasses.dataclass(frozen=True)
class RoleContract:
    name: str
    owns_stages: tuple[str, ...]
    allowed_inputs: tuple[str, ...]
    required_outputs: tuple[str, ...]
    must_not: tuple[str, ...]
    review_by: tuple[str, ...]
    approval_by: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RoleContract":
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise ContractError("role missing name")
        fields: dict[str, tuple[str, ...]] = {}
        for field in ("owns_stages", "allowed_inputs", "required_outputs", "must_not", "review_by", "approval_by"):
            value = raw.get(field)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ContractError(f"role {name} {field} must be strings")
            fields[field] = tuple(value)
        return cls(name=name, **fields)

    def owns(self, stage_name: str) -> bool:
        return "all" in self.owns_stages or stage_name in self.owns_stages


class RolePolicy:
    """Runtime role ownership and handoff policy."""

    special_outputs = {"decision_log", "review", "approval_decision"}

    def __init__(self, roles: list[RoleContract], handoffs: list[dict[str, Any]]) -> None:
        self.roles = {role.name: role for role in roles}
        self.handoffs = tuple(handoffs)

    @classmethod
    def load(cls, path: Path) -> "RolePolicy":
        raw = load_json(path)
        roles = raw.get("roles")
        if not isinstance(roles, list) or not roles:
            raise ContractError(f"{path} missing roles[]")
        handoffs = raw.get("handoff_artifacts", [])
        if not isinstance(handoffs, list):
            raise ContractError(f"{path} handoff_artifacts must be a list")
        return cls([RoleContract.from_dict(role) for role in roles], handoffs)

    def owner_for_stage(self, stage_name: str) -> str:
        explicit = [
            role.name
            for role in self.roles.values()
            if stage_name in role.owns_stages
        ]
        if len(explicit) == 1:
            return explicit[0]
        if len(explicit) > 1:
            raise ContractError(f"stage {stage_name!r} has multiple role owners: {explicit}")
        wildcard = [role.name for role in self.roles.values() if "all" in role.owns_stages]
        if len(wildcard) == 1:
            return wildcard[0]
        raise ContractError(f"stage {stage_name!r} has no role owner")

    def reviewer_for_stage(self, stage_name: str) -> str | None:
        owner = self.roles[self.owner_for_stage(stage_name)]
        if not owner.review_by:
            return None
        return owner.review_by[0]

    def reviewers_for_stage(self, stage_name: str) -> tuple[str, ...]:
        owner = self.roles[self.owner_for_stage(stage_name)]
        return owner.review_by

    def approvers_for_stage(self, stage_name: str) -> tuple[str, ...]:
        owner = self.roles[self.owner_for_stage(stage_name)]
        return owner.approval_by

    def validate_stage_write(
        self,
        manifest: PipelineManifest,
        run_id: str,
        stage_name: str,
        actor_role: str,
        artifacts: dict[str, dict[str, Any]],
        *,
        review: ReviewResult,
        prior_sources: dict[str, dict[str, str]],
        require_approval: bool = True,
    ) -> None:
        role = self.roles.get(actor_role)
        if role is None:
            raise ContractError(f"unknown actor_role {actor_role!r}")
        if not role.owns(stage_name):
            raise ContractError(f"role {actor_role!r} cannot own stage {stage_name!r}")
        expected_owner = self.owner_for_stage(stage_name)
        if actor_role != expected_owner:
            raise ContractError(
                f"stage {stage_name!r} must be written by role {expected_owner!r}, "
                f"got {actor_role!r}"
            )
        if role.review_by:
            for approval in review.approval_records():
                if approval.role not in self.roles:
                    raise ContractError(f"unknown reviewer_role {approval.role!r}")
                if approval.role not in role.review_by:
                    raise ContractError(
                        f"stage {stage_name!r} written by {actor_role!r} must be reviewed by "
                        f"only {list(role.review_by)!r}, got {approval.role!r}"
                    )
            approved_roles = review.approved_roles()
            missing_reviewers = sorted(set(role.review_by) - approved_roles)
            if missing_reviewers:
                raise ContractError(
                    f"stage {stage_name!r} written by {actor_role!r} must be reviewed by "
                    f"all required reviewers {list(role.review_by)!r}; missing {missing_reviewers}"
                )
        for approver_role in role.approval_by:
            if approver_role not in self.roles:
                raise ContractError(f"unknown approval role {approver_role!r}")
        if role.approval_by and require_approval:
            approval = artifacts.get("approval_decision")
            if not isinstance(approval, dict):
                raise ContractError(
                    f"stage {stage_name!r} written by {actor_role!r} requires approval_decision "
                    f"from {list(role.approval_by)!r}"
                )
            if approval.get("stage") != stage_name:
                raise ContractError(
                    f"approval_decision.stage {approval.get('stage')!r} does not match {stage_name!r}"
                )
            if approval.get("run_id") != run_id:
                raise ContractError(
                    f"approval_decision.run_id {approval.get('run_id')!r} does not match {run_id!r}"
                )
            if approval.get("decision") != "approved":
                raise ContractError(f"approval_decision for {stage_name!r} must be approved")
            approver_role = approval.get("approver_role")
            if approver_role not in role.approval_by:
                raise ContractError(
                    f"stage {stage_name!r} written by {actor_role!r} must be approved by "
                    f"one of {list(role.approval_by)!r}, got {approver_role!r}"
                )
        elif role.approval_by and not require_approval and "approval_decision" in artifacts:
            raise ContractError(f"{stage_name} is awaiting approval and must not include approval_decision")
        elif not role.approval_by and "approval_decision" in artifacts:
            raise ContractError(f"{stage_name} does not declare approval_by but includes approval_decision")
        allowed_outputs = set(role.required_outputs) | self.special_outputs
        unexpected = sorted(set(artifacts) - allowed_outputs)
        if unexpected:
            raise ContractError(
                f"role {actor_role!r} cannot output artifacts for {stage_name!r}: {unexpected}"
            )
        stage = manifest.get_stage(stage_name)
        for artifact_name in stage.get("required_artifacts_in", []):
            source = prior_sources.get(artifact_name)
            if source is None:
                raise ContractError(f"{stage_name} missing source for {artifact_name!r}")
            source_role = source.get("role", "")
            if not source_role or source_role == actor_role:
                continue
            if not self._handoff_allowed(source_role, actor_role, artifact_name, source.get("review", "")):
                raise ContractError(
                    f"no valid handoff for {artifact_name!r} from {source_role!r} "
                    f"to {actor_role!r}"
                )

    def _handoff_allowed(
        self,
        source_role: str,
        target_role: str,
        artifact_name: str,
        review_decision: str,
    ) -> bool:
        for handoff in self.handoffs:
            if (
                handoff.get("from") == source_role
                and handoff.get("to") == target_role
                and handoff.get("artifact") == artifact_name
            ):
                required_review = handoff.get("required_review")
                return not required_review or required_review == review_decision
        return False


@dataclasses.dataclass(frozen=True)
class StageExecutionContext:
    """Inputs available to a stage executor."""

    manifest: PipelineManifest
    run_id: str
    stage_name: str
    scenario: dict[str, Any]
    prior_artifacts: dict[str, dict[str, Any]]
    allowed_tools: tuple[str, ...] = ()


StageHandler = Callable[[StageExecutionContext], dict[str, dict[str, Any]]]


class StageExecutor:
    """Interface for producing artifacts for one stage at a time."""

    def execute(self, context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        raise NotImplementedError

    def tools_used(
        self,
        context: StageExecutionContext,
        artifacts: dict[str, dict[str, Any]],
    ) -> tuple[str, ...]:
        return ()


class FunctionStageExecutor(StageExecutor):
    """Stage executor backed by explicit Python handlers."""

    def __init__(self, handlers: dict[str, StageHandler], tools_used: dict[str, tuple[str, ...]] | None = None) -> None:
        self.handlers = handlers
        self._tools_used = tools_used or {}

    def execute(self, context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        handler = self.handlers.get(context.stage_name)
        if handler is None:
            raise ContractError(f"no executor handler for stage {context.stage_name!r}")
        return handler(context)

    def tools_used(
        self,
        context: StageExecutionContext,
        artifacts: dict[str, dict[str, Any]],
    ) -> tuple[str, ...]:
        return self._tools_used.get(context.stage_name, ())


class RunEngine:
    """Executes manifest stages through review and checkpoint gates."""

    def __init__(self, store: CheckpointStore, reviewer: Reviewer) -> None:
        self.store = store
        self.reviewer = reviewer

    def run(
        self,
        manifest: PipelineManifest,
        run_id: str,
        stage_artifacts: dict[str, dict[str, dict[str, Any]]],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        checkpoints: dict[str, str] = {}
        approval_grants = _approval_grants_from(metadata)
        for stage_name in manifest.stage_names():
            if stage_name not in stage_artifacts:
                raise ContractError(f"missing stage payload for {stage_name}")
            artifacts = stage_artifacts[stage_name]
            stage = manifest.get_stage(stage_name)
            actor_role = None
            reviewer_roles: tuple[str, ...] | None = None
            if self.store.role_policy:
                actor_role = self.store.role_policy.owner_for_stage(stage_name)
                reviewer_roles = self.store.role_policy.reviewers_for_stage(stage_name)
            review = self.reviewer.review(
                manifest,
                stage_name,
                artifacts,
                reviewer_roles=reviewer_roles,
            )
            if review.decision != "pass":
                raise ContractError(f"{stage_name} review did not pass: {review.findings}")
            status = "completed"
            checkpoint_metadata = _stage_metadata(stage, metadata, ())
            try:
                artifacts = self._with_approval_source(
                    manifest,
                    run_id,
                    stage_name,
                    artifacts,
                    approval_grants=approval_grants,
                )
            except HumanApprovalRequired as exc:
                status = "awaiting_human"
                artifacts = _without_approval_decision(artifacts)
                checkpoint_metadata.update(
                    {
                        "awaiting_approval_by": exc.approver_role,
                        "approval_boundary": "explicit_human_required",
                    }
                )
            checkpoint = self.store.write(
                manifest,
                run_id,
                stage_name,
                status=status,
                artifacts=artifacts,
                review=review,
                metadata=checkpoint_metadata,
                actor_role=actor_role,
            )
            checkpoints[stage_name] = str(checkpoint)
            if status == "awaiting_human":
                break
        return checkpoints

    def run_with_executor(
        self,
        manifest: PipelineManifest,
        run_id: str,
        executor: StageExecutor,
        *,
        scenario: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        checkpoints: dict[str, str] = {}
        prior_artifacts: dict[str, dict[str, Any]] = {}
        approval_grants = _approval_grants_from(metadata, scenario)
        for stage_name in manifest.stage_names():
            stage = manifest.get_stage(stage_name)
            context = StageExecutionContext(
                manifest=manifest,
                run_id=run_id,
                stage_name=stage_name,
                scenario=dict(scenario or {}),
                prior_artifacts=dict(prior_artifacts),
                allowed_tools=tuple(stage.get("tools_available", [])),
            )
            artifacts = executor.execute(context)
            tools_used = executor.tools_used(context, artifacts)
            actor_role = None
            reviewer_roles: tuple[str, ...] | None = None
            if self.store.role_policy:
                actor_role = self.store.role_policy.owner_for_stage(stage_name)
                reviewer_roles = self.store.role_policy.reviewers_for_stage(stage_name)
            review = self.reviewer.review(
                manifest,
                stage_name,
                artifacts,
                reviewer_roles=reviewer_roles,
            )
            if review.decision != "pass":
                raise ContractError(f"{stage_name} review did not pass: {review.findings}")
            status = "completed"
            checkpoint_metadata = _stage_metadata(stage, metadata, tools_used)
            try:
                artifacts = self._with_approval_source(
                    manifest,
                    run_id,
                    stage_name,
                    artifacts,
                    approval_grants=approval_grants,
                )
            except HumanApprovalRequired as exc:
                status = "awaiting_human"
                artifacts = _without_approval_decision(artifacts)
                checkpoint_metadata.update(
                    {
                        "awaiting_approval_by": exc.approver_role,
                        "approval_boundary": "explicit_human_required",
                    }
                )
            checkpoint = self.store.write(
                manifest,
                run_id,
                stage_name,
                status=status,
                artifacts=artifacts,
                review=review,
                metadata=checkpoint_metadata,
                actor_role=actor_role,
            )
            checkpoints[stage_name] = str(checkpoint)
            if status == "awaiting_human":
                break
            prior_artifacts.update(artifacts)
        return checkpoints

    def _with_approval_source(
        self,
        manifest: PipelineManifest,
        run_id: str,
        stage_name: str,
        artifacts: dict[str, dict[str, Any]],
        *,
        approval_grants: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        if not self.store.role_policy:
            return artifacts
        approvers = self.store.role_policy.approvers_for_stage(stage_name)
        if not approvers:
            return artifacts
        if "approval_decision" in artifacts and isinstance(artifacts["approval_decision"], dict) and artifacts["approval_decision"].get("source_checkpoint"):
            return artifacts
        approver_role = approvers[0]
        approval, approval_source_metadata = self._approval_decision_for_stage(
            manifest,
            run_id,
            stage_name,
            approver_role,
            artifacts,
            approval_grants=approval_grants or {},
        )
        source_approval = dict(approval)
        source_path = self._write_approval_source_checkpoint(
            manifest,
            run_id,
            stage_name,
            approver_role,
            source_approval,
            approval_source_metadata=approval_source_metadata,
        )
        self.store._trust_runtime_approval_source(source_path)
        sourced_approval = dict(approval)
        sourced_approval["source_checkpoint"] = str(source_path.resolve())
        sourced_artifacts = dict(artifacts)
        sourced_artifacts["approval_decision"] = sourced_approval
        return sourced_artifacts

    def _approval_decision_for_stage(
        self,
        manifest: PipelineManifest,
        run_id: str,
        stage_name: str,
        approver_role: str,
        artifacts: dict[str, dict[str, Any]],
        *,
        approval_grants: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        if approver_role == "human_operator":
            return _human_approval_decision(
                approval_grants,
                run_id,
                stage_name,
                approver_role,
            )
        declared = set(manifest.get_stage(stage_name).get("produces", []))
        evidence_refs = [
            artifact_name
            for artifact_name in artifacts
            if artifact_name in declared and artifact_name != "approval_decision"
        ]
        if not evidence_refs:
            evidence_refs = sorted(declared - {"approval_decision"})
        return (
            {
                "version": "1.0",
                "run_id": run_id,
                "stage": stage_name,
                "approver_role": approver_role,
                "decision": "approved",
                "reason": (
                    f"{approver_role} approval generated by the runtime approval gate "
                    f"for {manifest.name}.{stage_name}."
                ),
                "evidence_refs": evidence_refs or [stage_name],
            },
            {"approval_source": "runtime_approval_gate"},
        )

    def _write_approval_source_checkpoint(
        self,
        manifest: PipelineManifest,
        run_id: str,
        stage_name: str,
        approver_role: str,
        approval_decision: dict[str, Any],
        *,
        approval_source_metadata: dict[str, str],
    ) -> Path:
        approval_manifest = _approval_manifest(stage_name, approver_role)
        approval_stage = approval_manifest.stage_names()[0]
        approval_artifacts = {"approval_decision": approval_decision}
        review = self.reviewer.review(approval_manifest, approval_stage, approval_artifacts)
        if review.decision != "pass":
            raise ContractError(
                f"{stage_name} approval source review did not pass: {review.findings}"
            )
        approval_store = CheckpointStore(self.store.root, self.store.schemas)
        return approval_store.write(
            approval_manifest,
            run_id,
            approval_stage,
            status="completed",
            artifacts=approval_artifacts,
            review=review,
            metadata={
                "approval_for_pipeline": manifest.name,
                "approval_for_stage": stage_name,
                "approval_requested_by": self.store.role_policy.owner_for_stage(stage_name) if self.store.role_policy else "",
                **approval_source_metadata,
            },
            actor_role=approver_role,
        )


def _stage_metadata(
    stage: dict[str, Any],
    base_metadata: dict[str, Any] | None,
    tools_used: tuple[str, ...],
) -> dict[str, Any]:
    metadata = dict(base_metadata or {})
    metadata["tools_available"] = list(stage.get("tools_available", []))
    metadata["tools_used"] = list(tools_used)
    return metadata


def _without_approval_decision(
    artifacts: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    cleaned = dict(artifacts)
    cleaned.pop("approval_decision", None)
    return cleaned


def _approval_grants_from(*sources: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    grants: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        if "human_approval_grants" in source:
            raw = source["human_approval_grants"]
        elif "approval_grants" in source:
            raw = source["approval_grants"]
        else:
            continue
        if not isinstance(raw, dict):
            raise ContractError("human_approval_grants must be an object")
        for stage_name, grant in raw.items():
            if not isinstance(stage_name, str):
                raise ContractError("human_approval_grants stage names must be strings")
            if not isinstance(grant, dict):
                raise ContractError(
                    f"human_approval_grants[{stage_name!r}] must be an object"
                )
            grants[stage_name] = grant
    return grants


def _human_approval_decision(
    approval_grants: dict[str, dict[str, Any]],
    run_id: str,
    stage_name: str,
    approver_role: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    grant = approval_grants.get(stage_name)
    if grant is None:
        raise HumanApprovalRequired(stage_name, approver_role)
    if grant.get("approver_role") != approver_role:
        raise ContractError(
            f"human approval grant for {stage_name!r} must come from {approver_role!r}"
        )
    grant_stage = grant.get("stage", stage_name)
    if grant_stage != stage_name:
        raise ContractError(
            f"human approval grant stage {grant_stage!r} does not match {stage_name!r}"
        )
    grant_run_id = grant.get("run_id", run_id)
    if grant_run_id != run_id:
        raise ContractError(
            f"human approval grant run_id {grant_run_id!r} does not match {run_id!r}"
        )
    if grant.get("decision") != "approved":
        raise ContractError(f"human approval grant for {stage_name!r} must be approved")
    evidence_refs = grant.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not evidence_refs or not all(isinstance(item, str) for item in evidence_refs):
        raise ContractError(f"human approval grant for {stage_name!r} must list evidence_refs")
    reason = grant.get("reason")
    if not isinstance(reason, str) or not reason:
        raise ContractError(f"human approval grant for {stage_name!r} must include reason")
    grant_id = grant.get("grant_id")
    if not isinstance(grant_id, str) or not grant_id:
        raise ContractError(f"human approval grant for {stage_name!r} must include grant_id")
    return (
        {
            "version": "1.0",
            "run_id": run_id,
            "stage": stage_name,
            "approver_role": approver_role,
            "decision": "approved",
            "reason": reason,
            "evidence_refs": evidence_refs,
        },
        {
            "approval_source": "explicit_human_grant",
            "human_grant_id": grant_id,
        },
    )


def _approval_stage_name(stage_name: str, approver_role: str) -> str:
    return f"approve_{stage_name}_by_{approver_role}"


def _approval_manifest(stage_name: str, approver_role: str) -> PipelineManifest:
    approval_stage = _approval_stage_name(stage_name, approver_role)
    return PipelineManifest.from_dict(
        {
            "name": "approval-decision",
            "version": "1.0",
            "stages": [
                {
                    "name": approval_stage,
                    "produces": ["approval_decision"],
                    "tools_available": [],
                    "review_focus": ["Approval decision is explicit and schema-valid."],
                    "success_criteria": [
                        "requires:decision",
                        "requires:approver_role",
                        "requires:reason",
                    ],
                }
            ],
        }
    )


@dataclasses.dataclass(frozen=True)
class ToolContract:
    name: str
    capability: str
    provider: str
    runtime: str
    dependencies: tuple[str, ...]
    input_artifacts: tuple[str, ...]
    output_artifacts: tuple[str, ...]
    fallback_tools: tuple[str, ...] = ()
    agent_skills: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ToolContract":
        for field in ("name", "capability", "provider", "runtime"):
            if not isinstance(raw.get(field), str) or not raw[field]:
                raise ContractError(f"tool missing {field}")
        return cls(
            name=raw["name"],
            capability=raw["capability"],
            provider=raw["provider"],
            runtime=raw["runtime"],
            dependencies=tuple(raw.get("dependencies", [])),
            input_artifacts=tuple(raw.get("input_artifacts", [])),
            output_artifacts=tuple(raw.get("output_artifacts", [])),
            fallback_tools=tuple(raw.get("fallback_tools", [])),
            agent_skills=tuple(raw.get("agent_skills", [])),
        )

    def status(self) -> str:
        for dep in self.dependencies:
            if dep.startswith("cmd:") and shutil.which(dep[4:]) is None:
                return "unavailable"
        return "available"

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["status"] = self.status()
        return data


class ToolRegistry:
    """Capability menu for agents."""

    def __init__(self, tools: list[ToolContract]) -> None:
        self.tools = {tool.name: tool for tool in tools}

    @classmethod
    def load(cls, path: Path) -> "ToolRegistry":
        raw = load_json(path)
        tools = raw.get("tools")
        if not isinstance(tools, list):
            raise ContractError(f"{path} missing tools[]")
        return cls([ToolContract.from_dict(item) for item in tools])

    def by_capability(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for tool in self.tools.values():
            grouped.setdefault(tool.capability, []).append(tool.to_dict())
        return {key: sorted(value, key=lambda item: item["name"]) for key, value in sorted(grouped.items())}

    def require_outputs_have_schemas(self, schemas: SchemaRegistry) -> None:
        for tool in self.tools.values():
            for artifact_name in tool.output_artifacts:
                if artifact_name not in schemas.schemas:
                    raise ContractError(f"tool {tool.name} outputs unknown artifact {artifact_name}")
