"""Software engineering domain helpers."""

from __future__ import annotations

from typing import Any

from .core import ContractError, FunctionStageExecutor, StageExecutionContext


def build_demo_bug_fix_artifacts(scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build deterministic artifacts for the bug-fix pipeline demo.

    This is deliberately simple: it proves the contracts and checkpoints work
    without pretending to repair an arbitrary repository.
    """

    issue_id = scenario["issue_id"]
    run_id = scenario.get("run_id", "demo-bug-fix")
    return {
        "bug_report": {
            "version": "1.0",
            "issue_id": issue_id,
            "observed_behavior": scenario["observed_behavior"],
            "expected_behavior": scenario["expected_behavior"],
            "reproduction_steps": scenario["reproduction_steps"],
            "evidence": scenario["evidence"],
        },
        "root_cause_report": {
            "version": "1.0",
            "issue_id": issue_id,
            "root_cause": "Cache key omits privacy-affecting request fields.",
            "failure_family": "state_key_under_specification",
            "affected_files": ["src/cache.py"],
            "evidence": ["same key reused across include_private variants"],
            "confidence": 0.92,
        },
        "system_fault_report": {
            "version": "1.0",
            "issue_id": issue_id,
            "fix_system_first": True,
            "system_faults": [
                {
                    "category": "missing_regression_guard",
                    "cause": "No fixture forces cache keys to include data-scope flags.",
                    "prevention": "Add regression case before product patch.",
                },
                {
                    "category": "review_rule_gap",
                    "cause": "Reviewer did not inspect state keys for safety-sensitive inputs.",
                    "prevention": "Add a root-cause checklist item for cache/idempotency keys.",
                },
            ],
        },
        "patch_plan": {
            "version": "1.0",
            "issue_id": issue_id,
            "risk_level": "medium",
            "changes": [
                "Include include_private and requested fields in cache key.",
                "Add regression fixture for private/non-private request variants.",
            ],
            "verification_commands": [
                "python3 -m unittest discover",
                "python3 scripts/replay_regressions.py",
            ],
            "rollback_plan": "Revert cache-key change and disable affected cache path.",
        },
        "verification_report": {
            "version": "1.0",
            "issue_id": issue_id,
            "overall_status": "pass",
            "commands": [
                _command_evidence(
                    "python3 -m unittest discover",
                    "pass",
                    ["patch_plan"],
                )
            ],
        },
        "regression_case": {
            "version": "1.0",
            "id": "reg-cache-key-privacy-scope",
            "source_issue_id": issue_id,
            "fixture": "two requests differ only by include_private",
            "expected_guard": "cache keys must differ",
            "replay_command": "python3 scripts/replay_regressions.py --case reg-cache-key-privacy-scope",
        },
        "knowledge_update": {
            "version": "1.0",
            "target": "software-pack/root-cause-review",
            "update_type": "review_rule",
            "trigger": "state key omitted privacy field",
            "summary": "Whenever a bug involves cached or idempotent state, inspect whether all safety- and scope-affecting inputs participate in the key.",
        },
        "decision_log": {
            "version": "1.0",
            "run_id": run_id,
            "decisions": [
                {
                    "decision_id": "d-001",
                    "stage": "system_fault",
                    "category": "system_first_fix",
                    "subject": "Whether to patch harness rules before product code",
                    "options_considered": [
                        {
                            "option_id": "product_only",
                            "label": "Patch product code only",
                            "score": 0.35,
                            "reason": "Would fix symptom but not prevent similar key omissions.",
                            "rejected_because": "Leaves system fault unaddressed.",
                        },
                        {
                            "option_id": "system_then_product",
                            "label": "Add regression/review guard before product patch",
                            "score": 0.91,
                            "reason": "Turns the bug into a reusable guardrail.",
                        },
                    ],
                    "selected": "system_then_product",
                    "reason": "The observed failure family is reusable across cache/idempotency bugs.",
                    "user_visible": True,
                    "user_approved": False,
                    "confidence": 0.87,
                    "artifact_refs": ["root_cause_report", "system_fault_report"],
                    "checkpoint_refs": [],
                }
            ],
        },
    }


def _command_evidence(
    command: str,
    status: str,
    artifact_refs: list[str],
    *,
    exit_code: int = 0,
) -> dict[str, Any]:
    """Create a deterministic command evidence object for examples/tests."""

    return {
        "command": command,
        "status": status,
        "exit_code": exit_code,
        "stdout_digest": "sha256:demo-stdout",
        "stderr_digest": "sha256:demo-stderr",
        "commit_sha": "workspace-uncommitted",
        "tool_version": "python3",
        "timestamp": "2026-06-30T00:00:00Z",
        "artifact_refs": artifact_refs,
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
        "reason": f"{approver_role} approved {context.stage_name} for the deterministic demo scenario.",
        "evidence_refs": evidence_refs,
    }


def _require_prior(context: StageExecutionContext, artifact_name: str) -> dict[str, Any]:
    artifact = context.prior_artifacts.get(artifact_name)
    if artifact is None:
        raise ContractError(
            f"{context.stage_name} requires prior artifact {artifact_name!r}"
        )
    return artifact


def build_demo_feature_artifacts(scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    feature_id = scenario["feature_id"]
    return {
        "feature_spec": {
            "version": "1.0",
            "feature_id": feature_id,
            "user_value": scenario["user_value"],
            "requirements": scenario["requirements"],
            "acceptance_checks": scenario["acceptance_checks"],
        },
        "patch_plan": {
            "version": "1.0",
            "issue_id": feature_id,
            "risk_level": "medium",
            "changes": ["Implement feature behind a bounded interface."],
            "verification_commands": ["python3 -m unittest discover"],
            "rollback_plan": "Disable the feature flag and revert the patch.",
        },
        "verification_report": {
            "version": "1.0",
            "issue_id": feature_id,
            "overall_status": "pass",
            "commands": [
                _command_evidence("python3 -m unittest discover", "pass", ["feature_spec", "patch_plan"])
            ],
        },
        "release_report": {
            "version": "1.0",
            "release_id": feature_id,
            "status": "ready",
            "gates": [
                {
                    "name": "contract-tests",
                    "status": "pass",
                    "evidence": _command_evidence(
                        "python3 -m unittest discover",
                        "pass",
                        ["verification_report"],
                    ),
                }
            ],
            "rollback_plan": "Disable feature flag.",
        },
    }


def build_demo_refactor_artifacts(scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    target = scenario["target"]
    return {
        "refactor_plan": {
            "version": "1.0",
            "target": target,
            "invariants": scenario["invariants"],
            "safe_steps": scenario["safe_steps"],
            "verification_commands": ["python3 -m unittest discover"],
        },
        "patch_plan": {
            "version": "1.0",
            "issue_id": f"refactor:{target}",
            "risk_level": "low",
            "changes": scenario["safe_steps"],
            "verification_commands": ["python3 -m unittest discover"],
            "rollback_plan": "Revert the refactor commit.",
        },
        "verification_report": {
            "version": "1.0",
            "issue_id": f"refactor:{target}",
            "overall_status": "pass",
            "commands": [
                _command_evidence("python3 -m unittest discover", "pass", ["refactor_plan", "patch_plan"])
            ],
        },
        "regression_case": {
            "version": "1.0",
            "id": "reg-refactor-invariant",
            "source_issue_id": f"refactor:{target}",
            "fixture": "public API behavior before and after refactor",
            "expected_guard": "all declared invariants hold",
            "replay_command": "python3 scripts/replay_regressions.py --case reg-refactor-invariant",
        },
    }


def build_demo_incident_artifacts(scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    incident_id = scenario["incident_id"]
    return {
        "incident_report": {
            "version": "1.0",
            "incident_id": incident_id,
            "impact": scenario["impact"],
            "timeline": scenario["timeline"],
            "mitigation": scenario["mitigation"],
            "followups": scenario["followups"],
        },
        "root_cause_report": {
            "version": "1.0",
            "issue_id": incident_id,
            "root_cause": "Release gate accepted unstructured evidence.",
            "failure_family": "weak_release_evidence",
            "affected_files": ["release/gates"],
            "evidence": ["incident timeline shows release gate missed blocked check"],
            "confidence": 0.84,
        },
        "system_fault_report": {
            "version": "1.0",
            "issue_id": incident_id,
            "fix_system_first": True,
            "system_faults": [
                {
                    "category": "weak_release_gate",
                    "cause": "Gate accepted text evidence instead of command evidence.",
                    "prevention": "Require structured command evidence in release reports.",
                }
            ],
        },
        "knowledge_update": {
            "version": "1.0",
            "target": "software-pack/release-review",
            "update_type": "review_rule",
            "trigger": "release incident with weak evidence",
            "summary": "Release gates require structured command evidence and rollback readiness.",
        },
    }


def build_demo_release_artifacts(scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    release_id = scenario["release_id"]
    return {
        "release_report": {
            "version": "1.0",
            "release_id": release_id,
            "status": "ready",
            "gates": [
                {
                    "name": "unit-tests",
                    "status": "pass",
                    "evidence": _command_evidence(
                        "python3 -m unittest discover",
                        "pass",
                        ["release_report"],
                    ),
                },
                {
                    "name": "rollback-plan",
                    "status": "pass",
                    "evidence": _command_evidence(
                        "python3 - <<'PY'\nassert rollback_plan\nPY",
                        "pass",
                        ["release_report"],
                    ),
                },
            ],
            "rollback_plan": scenario["rollback_plan"],
        }
    }


def build_demo_bug_fix_executor(scenario: dict[str, Any]) -> FunctionStageExecutor:
    """Create a stage-by-stage executor for the bug-fix demo pipeline."""

    issue_id = scenario["issue_id"]

    def reproduce(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        return {
            "bug_report": {
                "version": "1.0",
                "issue_id": issue_id,
                "observed_behavior": scenario["observed_behavior"],
                "expected_behavior": scenario["expected_behavior"],
                "reproduction_steps": scenario["reproduction_steps"],
                "evidence": scenario["evidence"],
            }
        }

    def root_cause(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        bug_report = _require_prior(context, "bug_report")
        return {
            "root_cause_report": {
                "version": "1.0",
                "issue_id": bug_report["issue_id"],
                "root_cause": "Cache key omits privacy-affecting request fields.",
                "failure_family": "state_key_under_specification",
                "affected_files": ["src/cache.py"],
                "evidence": ["same key reused across include_private variants"],
                "confidence": 0.92,
            }
        }

    def system_fault(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        root_report = _require_prior(context, "root_cause_report")
        return {
            "system_fault_report": {
                "version": "1.0",
                "issue_id": root_report["issue_id"],
                "fix_system_first": True,
                "system_faults": [
                    {
                        "category": "missing_regression_guard",
                        "cause": "No fixture forces cache keys to include data-scope flags.",
                        "prevention": "Add regression case before product patch.",
                    },
                    {
                        "category": "review_rule_gap",
                        "cause": "Reviewer did not inspect state keys for safety-sensitive inputs.",
                        "prevention": "Add a root-cause checklist item for cache/idempotency keys.",
                    },
                ],
            },
            "decision_log": {
                "version": "1.0",
                "run_id": context.run_id,
                "decisions": [
                    {
                        "decision_id": "d-001",
                        "stage": context.stage_name,
                        "category": "system_first_fix",
                        "subject": "Whether to patch harness rules before product code",
                        "options_considered": [
                            {
                                "option_id": "product_only",
                                "label": "Patch product code only",
                                "score": 0.35,
                                "reason": "Would fix symptom but not prevent similar key omissions.",
                                "rejected_because": "Leaves system fault unaddressed.",
                            },
                            {
                                "option_id": "system_then_product",
                                "label": "Add regression/review guard before product patch",
                                "score": 0.91,
                                "reason": "Turns the bug into a reusable guardrail.",
                            },
                        ],
                        "selected": "system_then_product",
                        "reason": "The observed failure family is reusable across cache/idempotency bugs.",
                        "user_visible": True,
                        "user_approved": False,
                        "confidence": 0.87,
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
        fault_report = _require_prior(context, "system_fault_report")
        return {
            "patch_plan": {
                "version": "1.0",
                "issue_id": fault_report["issue_id"],
                "risk_level": "medium",
                "changes": [
                    "Include include_private and requested fields in cache key.",
                    "Add regression fixture for private/non-private request variants.",
                ],
                "verification_commands": [
                    "python3 -m unittest discover",
                    "python3 scripts/replay_regressions.py",
                ],
                "rollback_plan": "Revert cache-key change and disable affected cache path.",
            }
        }

    def verification(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        patch_plan = _require_prior(context, "patch_plan")
        return {
            "verification_report": {
                "version": "1.0",
                "issue_id": patch_plan["issue_id"],
                "overall_status": "pass",
                "commands": [
                    _command_evidence(
                        "python3 -m unittest discover",
                        "pass",
                        ["patch_plan"],
                    )
                ],
            },
            "approval_decision": _approval_decision(
                context,
                "release_captain",
                ["verification_report"],
            ),
        }

    def regression(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        report = _require_prior(context, "verification_report")
        return {
            "regression_case": {
                "version": "1.0",
                "id": "reg-cache-key-privacy-scope",
                "source_issue_id": report["issue_id"],
                "fixture": "two requests differ only by include_private",
                "expected_guard": "cache keys must differ",
                "replay_command": "python3 scripts/replay_regressions.py --case reg-cache-key-privacy-scope",
            },
            "approval_decision": _approval_decision(
                context,
                "release_captain",
                ["regression_case"],
            ),
        }

    def knowledge_update(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        _require_prior(context, "regression_case")
        return {
            "knowledge_update": {
                "version": "1.0",
                "target": "software-pack/root-cause-review",
                "update_type": "review_rule",
                "trigger": "state key omitted privacy field",
                "summary": (
                    "Whenever a bug involves cached or idempotent state, inspect "
                    "whether all safety- and scope-affecting inputs participate "
                    "in the key."
                ),
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


def build_demo_feature_executor(scenario: dict[str, Any]) -> FunctionStageExecutor:
    """Create a stage-by-stage executor for the feature-build demo pipeline."""

    feature_id = scenario["feature_id"]

    def spec(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        return {
            "feature_spec": {
                "version": "1.0",
                "feature_id": feature_id,
                "user_value": scenario["user_value"],
                "requirements": scenario["requirements"],
                "acceptance_checks": scenario["acceptance_checks"],
            }
        }

    def implementation_plan(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        feature_spec = _require_prior(context, "feature_spec")
        return {
            "patch_plan": {
                "version": "1.0",
                "issue_id": feature_spec["feature_id"],
                "risk_level": "medium",
                "changes": ["Implement feature behind a bounded interface."],
                "verification_commands": ["python3 -m unittest discover"],
                "rollback_plan": "Disable the feature flag and revert the patch.",
            }
        }

    def verification(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        patch_plan = _require_prior(context, "patch_plan")
        return {
            "verification_report": {
                "version": "1.0",
                "issue_id": patch_plan["issue_id"],
                "overall_status": "pass",
                "commands": [
                    _command_evidence(
                        "python3 -m unittest discover",
                        "pass",
                        ["feature_spec", "patch_plan"],
                    )
                ],
            },
            "approval_decision": _approval_decision(
                context,
                "release_captain",
                ["verification_report"],
            ),
        }

    def release_readiness(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        report = _require_prior(context, "verification_report")
        return {
            "release_report": {
                "version": "1.0",
                "release_id": report["issue_id"],
                "status": "ready",
                "gates": [
                    {
                        "name": "contract-tests",
                        "status": "pass",
                        "evidence": _command_evidence(
                            "python3 -m unittest discover",
                            "pass",
                            ["verification_report"],
                        ),
                    }
                ],
                "rollback_plan": "Disable feature flag.",
            },
            "approval_decision": _approval_decision(
                context,
                "human_operator",
                ["release_report"],
            ),
        }

    return FunctionStageExecutor(
        {
            "spec": spec,
            "implementation_plan": implementation_plan,
            "verification": verification,
            "release_readiness": release_readiness,
        }
    )


def build_demo_refactor_executor(scenario: dict[str, Any]) -> FunctionStageExecutor:
    """Create a stage-by-stage executor for the refactor demo pipeline."""

    target = scenario["target"]

    def invariants(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        return {
            "refactor_plan": {
                "version": "1.0",
                "target": target,
                "invariants": scenario["invariants"],
                "safe_steps": scenario["safe_steps"],
                "verification_commands": ["python3 -m unittest discover"],
            }
        }

    def patch_plan(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        plan = _require_prior(context, "refactor_plan")
        return {
            "patch_plan": {
                "version": "1.0",
                "issue_id": f"refactor:{plan['target']}",
                "risk_level": "low",
                "changes": list(plan["safe_steps"]),
                "verification_commands": ["python3 -m unittest discover"],
                "rollback_plan": "Revert the refactor commit.",
            }
        }

    def verification(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        patch = _require_prior(context, "patch_plan")
        return {
            "verification_report": {
                "version": "1.0",
                "issue_id": patch["issue_id"],
                "overall_status": "pass",
                "commands": [
                    _command_evidence(
                        "python3 -m unittest discover",
                        "pass",
                        ["refactor_plan", "patch_plan"],
                    )
                ],
            },
            "approval_decision": _approval_decision(
                context,
                "release_captain",
                ["verification_report"],
            ),
        }

    def regression(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        report = _require_prior(context, "verification_report")
        return {
            "regression_case": {
                "version": "1.0",
                "id": "reg-refactor-invariant",
                "source_issue_id": report["issue_id"],
                "fixture": "public API behavior before and after refactor",
                "expected_guard": "all declared invariants hold",
                "replay_command": "python3 scripts/replay_regressions.py --case reg-refactor-invariant",
            },
            "approval_decision": _approval_decision(
                context,
                "release_captain",
                ["regression_case"],
            ),
        }

    return FunctionStageExecutor(
        {
            "invariants": invariants,
            "patch_plan": patch_plan,
            "verification": verification,
            "regression": regression,
        }
    )


def build_demo_incident_executor(scenario: dict[str, Any]) -> FunctionStageExecutor:
    """Create a stage-by-stage executor for the incident-postmortem demo pipeline."""

    incident_id = scenario["incident_id"]

    def incident(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        return {
            "incident_report": {
                "version": "1.0",
                "incident_id": incident_id,
                "impact": scenario["impact"],
                "timeline": scenario["timeline"],
                "mitigation": scenario["mitigation"],
                "followups": scenario["followups"],
            }
        }

    def root_cause(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        incident_report = _require_prior(context, "incident_report")
        return {
            "root_cause_report": {
                "version": "1.0",
                "issue_id": incident_report["incident_id"],
                "root_cause": "Release gate accepted unstructured evidence.",
                "failure_family": "weak_release_evidence",
                "affected_files": ["release/gates"],
                "evidence": ["incident timeline shows release gate missed blocked check"],
                "confidence": 0.84,
            }
        }

    def system_fault(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        root_report = _require_prior(context, "root_cause_report")
        return {
            "system_fault_report": {
                "version": "1.0",
                "issue_id": root_report["issue_id"],
                "fix_system_first": True,
                "system_faults": [
                    {
                        "category": "weak_release_gate",
                        "cause": "Gate accepted text evidence instead of command evidence.",
                        "prevention": "Require structured command evidence in release reports.",
                    }
                ],
            },
            "approval_decision": _approval_decision(
                context,
                "release_captain",
                ["system_fault_report"],
            ),
        }

    def knowledge_update(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        _require_prior(context, "system_fault_report")
        return {
            "knowledge_update": {
                "version": "1.0",
                "target": "software-pack/release-review",
                "update_type": "review_rule",
                "trigger": "release incident with weak evidence",
                "summary": "Release gates require structured command evidence and rollback readiness.",
            },
            "approval_decision": _approval_decision(
                context,
                "release_captain",
                ["knowledge_update"],
            ),
        }

    return FunctionStageExecutor(
        {
            "incident": incident,
            "root_cause": root_cause,
            "system_fault": system_fault,
            "knowledge_update": knowledge_update,
        }
    )


def build_demo_release_executor(scenario: dict[str, Any]) -> FunctionStageExecutor:
    """Create a stage-by-stage executor for the release demo pipeline."""

    release_id = scenario["release_id"]

    def release_gate(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        return {
            "release_report": {
                "version": "1.0",
                "release_id": release_id,
                "status": "ready",
                "gates": [
                    {
                        "name": "unit-tests",
                        "status": "pass",
                        "evidence": _command_evidence(
                            "python3 -m unittest discover",
                            "pass",
                            ["release_report"],
                        ),
                    },
                    {
                        "name": "rollback-plan",
                        "status": "pass",
                        "evidence": _command_evidence(
                            "python3 - <<'PY'\nassert rollback_plan\nPY",
                            "pass",
                            ["release_report"],
                        ),
                    },
                ],
                "rollback_plan": scenario["rollback_plan"],
            },
            "approval_decision": _approval_decision(
                context,
                "human_operator",
                ["release_report"],
            ),
        }

    return FunctionStageExecutor({"release_gate": release_gate})
