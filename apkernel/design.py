"""Design domain pack helpers."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .core import (
    ContractError,
    FunctionStageExecutor,
    StageExecutionContext,
    register_artifact_semantic_validator,
)


ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_ATTRIBUTION = (
    "Adapted from Trystan-SA/claude-design-system-prompt "
    "(MIT), using APK schemas/checkpoints as the source of truth."
)
REQUIRED_INTERACTION_STATES = {
    "default",
    "hover",
    "active",
    "focus-visible",
    "disabled",
    "loading",
}
REQUIRED_RELEASE_GATES = {"accessibility-aa", "visual-quality"}


def _require_prior(context: StageExecutionContext, artifact_name: str) -> dict[str, Any]:
    artifact = context.prior_artifacts.get(artifact_name)
    if artifact is None:
        raise ContractError(f"{context.stage_name} requires prior artifact {artifact_name!r}")
    return artifact


def _finding(rule: str, evidence: str, *, status: str = "fixed", severity: str = "quality") -> dict[str, str]:
    return {
        "rule": rule,
        "severity": severity,
        "status": status,
        "evidence": evidence,
    }


def _is_url(value: str) -> bool:
    return value.startswith("https://") or value.startswith("http://")


def _local_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _probe_design_prototype(artifact_path: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "scripts/probe_design_prototype.py", artifact_path],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(
            f"prototype render probe did not return JSON: {exc}; stderr={result.stderr.strip()}"
        ) from exc
    if result.returncode != 0 or report.get("status") != "pass":
        raise ContractError(f"prototype render probe failed: {json.dumps(report, sort_keys=True)}")
    return report


def _design_brief_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for ref in report.get("source_refs", []):
        if not isinstance(ref, str):
            continue
        if _is_url(ref):
            continue
        if not _local_path(ref).exists():
            errors.append(f"design_brief.source_refs contains missing local source {ref!r}")
    return errors


def _design_context_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for source in report.get("sources", []):
        if not isinstance(source, str):
            continue
        if _is_url(source):
            continue
        if not _local_path(source).exists():
            errors.append(f"design_context.sources contains missing local source {source!r}")
    tokens = report.get("tokens", {})
    if isinstance(tokens, dict):
        for category in ("colors", "typography", "spacing", "radii"):
            if not tokens.get(category):
                errors.append(f"design_context.tokens.{category} must preserve source-traced values")
    if "Trystan-SA/claude-design-system-prompt" not in str(report.get("upstream_attribution", "")):
        errors.append("design_context.upstream_attribution must preserve upstream project attribution")
    return errors


def _design_prototype_report_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    artifact_path = report.get("artifact_path")
    artifact_text = ""
    if isinstance(artifact_path, str):
        path = _local_path(artifact_path)
        if not path.exists():
            errors.append(f"design_prototype_report.artifact_path does not exist: {artifact_path!r}")
        elif path.suffix.lower() not in {".html", ".htm"}:
            errors.append("design_prototype_report.artifact_path must point to an HTML artifact")
        else:
            try:
                artifact_text = path.read_text(encoding="utf-8").lower()
            except OSError as exc:
                errors.append(f"design_prototype_report.artifact_path is not readable: {exc}")
    states = set(report.get("states_covered", []))
    missing_states = sorted(REQUIRED_INTERACTION_STATES - states)
    if missing_states:
        errors.append(f"design_prototype_report.states_covered missing {missing_states}")
    if artifact_text:
        state_markers = {
            "default": ("<button", "<a "),
            "hover": (":hover",),
            "active": (":active",),
            "focus-visible": (":focus-visible",),
            "disabled": (":disabled", " disabled", "disabled>"),
            "loading": ("aria-busy=\"true\"", "data-state=\"loading\"", "data-loading"),
        }
        for state, markers in state_markers.items():
            if state in states and not any(marker in artifact_text for marker in markers):
                errors.append(
                    f"design_prototype_report.states_covered claims {state!r} without an HTML state marker"
                )
    probe = report.get("probe", {})
    if isinstance(probe, dict):
        if probe.get("tool") != "scripts/probe_design_prototype.py":
            errors.append("design_prototype_report.probe.tool must name the browser probe script")
        if probe.get("status") != "pass":
            errors.append("design_prototype_report.probe.status must be pass")
        screenshot_path = probe.get("screenshot_path")
        if not isinstance(screenshot_path, str) or not screenshot_path:
            errors.append("design_prototype_report.probe.screenshot_path is required")
        elif not _local_path(screenshot_path).exists():
            errors.append(
                f"design_prototype_report.probe.screenshot_path does not exist: {screenshot_path!r}"
            )
    render_checks = [
        check for check in report.get("render_checks", [])
        if isinstance(check, dict)
    ]
    if not render_checks:
        errors.append("design_prototype_report.render_checks must include at least one probe")
    if any(check.get("status") != "pass" for check in render_checks):
        errors.append("design_prototype_report pass path requires every render check to pass")
    check_names = {str(check.get("name", "")) for check in render_checks}
    for required_check in ("browser-render-probe", "interaction-state-probe"):
        if required_check not in check_names:
            errors.append(f"design_prototype_report.render_checks missing {required_check!r}")
    for check in render_checks:
        evidence = str(check.get("evidence", "")).lower()
        if "artifact" not in evidence and "render" not in evidence and "probe" not in evidence:
            errors.append(f"design_prototype_report.render_checks.{check.get('name')} evidence must name a concrete render/probe")
    return errors


def _accessibility_audit_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    checks = report.get("checks", [])
    blockers = report.get("blockers", [])
    verdict = report.get("verdict")
    failed_checks = [
        check for check in checks
        if isinstance(check, dict) and check.get("status") == "fail"
    ]
    if verdict == "pass" and blockers:
        errors.append("accessibility_audit.verdict pass requires no blockers")
    if verdict == "pass" and failed_checks:
        errors.append("accessibility_audit.verdict pass requires no failed checks")
    if verdict == "fail" and not blockers and not failed_checks:
        errors.append("accessibility_audit.verdict fail requires blockers or failed checks")
    return errors


def _visual_quality_report_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    verdict = report.get("verdict")
    open_serious_findings: list[dict[str, Any]] = []
    for field in ("ai_slop_findings", "hierarchy_findings", "interaction_state_findings"):
        for finding in report.get(field, []):
            if not isinstance(finding, dict):
                continue
            if finding.get("status") == "open" and finding.get("severity") in {"blocker", "quality"}:
                open_serious_findings.append(finding)
    if verdict == "pass" and open_serious_findings:
        errors.append("visual_quality_report.verdict pass requires no open blocker or quality findings")
    if verdict == "fail" and not open_serious_findings:
        errors.append("visual_quality_report.verdict fail requires an open blocker or quality finding")
    return errors


def _design_release_report_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    status = report.get("status")
    gates = [
        gate for gate in report.get("gates", [])
        if isinstance(gate, dict)
    ]
    gate_statuses = [gate.get("status") for gate in gates]
    gate_names = {str(gate.get("name", "")) for gate in gates}
    missing_gates = sorted(REQUIRED_RELEASE_GATES - gate_names)
    if missing_gates:
        errors.append(f"design_release_report.gates missing required gates {missing_gates}")
    if status == "ready" and any(gate_status != "pass" for gate_status in gate_statuses):
        errors.append("design_release_report.status ready requires every gate to pass")
    if status == "hold" and gate_statuses and all(gate_status == "pass" for gate_status in gate_statuses):
        errors.append("design_release_report.status hold requires at least one non-passing gate")
    attribution_refs = report.get("attribution_refs", [])
    if not any(
        isinstance(ref, str) and "Trystan-SA/claude-design-system-prompt" in ref
        for ref in attribution_refs
    ):
        errors.append("design_release_report.attribution_refs must include upstream project provenance")
    return errors


def _register_design_semantics() -> None:
    validators = {
        "design_brief": _design_brief_errors,
        "design_context": _design_context_errors,
        "design_prototype_report": _design_prototype_report_errors,
        "accessibility_audit": _accessibility_audit_errors,
        "visual_quality_report": _visual_quality_report_errors,
        "design_release_report": _design_release_report_errors,
    }
    for artifact_name, validator in validators.items():
        try:
            register_artifact_semantic_validator(artifact_name, validator, source=__name__)
        except ContractError as exc:
            if "already registered" not in str(exc):
                raise


def build_demo_design_review_executor(scenario: dict[str, Any]) -> FunctionStageExecutor:
    """Create a deterministic executor for the design-review demo pipeline."""

    design_id = scenario["design_id"]

    def scope_design(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        return {
            "design_brief": {
                "version": "1.0",
                "design_id": design_id,
                "surface": scenario["surface"],
                "audience": scenario["audience"],
                "primary_goal": scenario["primary_goal"],
                "constraints": scenario["constraints"],
                "source_refs": scenario["source_refs"],
            }
        }

    def extract_design_context(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        brief = _require_prior(context, "design_brief")
        return {
            "design_context": {
                "version": "1.0",
                "design_id": brief["design_id"],
                "sources": brief["source_refs"],
                "tokens": scenario["tokens"],
                "components": scenario["components"],
                "gaps": scenario["gaps"],
                "upstream_attribution": UPSTREAM_ATTRIBUTION,
            }
        }

    def prototype_surface(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        context_artifact = _require_prior(context, "design_context")
        artifact_path = str(scenario["artifact_path"])
        artifact = _local_path(artifact_path)
        if not artifact.exists():
            raise ContractError(f"prototype artifact does not exist: {artifact_path}")
        probe_report = _probe_design_prototype(artifact_path)
        return {
            "design_prototype_report": {
                "version": "1.0",
                "design_id": context_artifact["design_id"],
                "artifact_path": artifact_path,
                "medium": scenario["medium"],
                "interaction_model": "Clickable HTML prototype with local state and explicit UI feedback.",
                "states_covered": [
                    "default",
                    "hover",
                    "active",
                    "focus-visible",
                    "disabled",
                    "loading",
                ],
                "probe": {
                    "tool": "scripts/probe_design_prototype.py",
                    "status": probe_report["status"],
                    "screenshot_path": probe_report["screenshot_path"],
                },
                "render_checks": probe_report["checks"],
            }
        }

    def accessibility_audit(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        prototype = _require_prior(context, "design_prototype_report")
        return {
            "accessibility_audit": {
                "version": "1.0",
                "design_id": prototype["design_id"],
                "wcag_level": "AA",
                "verdict": "pass",
                "checks": [
                    {
                        "category": "contrast",
                        "status": "pass",
                        "evidence": "Normal text target is >= 4.5:1; UI/focus targets are >= 3:1.",
                    },
                    {
                        "category": "semantic_html",
                        "status": "pass",
                        "evidence": "Landmarks, headings, buttons, links, labels, and image alt rules are explicit.",
                    },
                    {
                        "category": "keyboard_focus",
                        "status": "pass",
                        "evidence": "Tab order, keyboard activation, Escape behavior, and visible focus are covered.",
                    },
                    {
                        "category": "motion_forms",
                        "status": "pass",
                        "evidence": "Reduced-motion handling, field error linkage, and 44px hit targets are covered.",
                    },
                ],
                "blockers": [],
                "fixes_applied": [
                    "Preserved visible focus treatment.",
                    "Tied form errors to fields rather than color alone.",
                ],
            }
        }

    def visual_quality_gate(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        prototype = _require_prior(context, "design_prototype_report")
        _require_prior(context, "accessibility_audit")
        return {
            "visual_quality_report": {
                "version": "1.0",
                "design_id": prototype["design_id"],
                "verdict": "pass",
                "ai_slop_findings": [
                    _finding(
                        "ai-slop-check.gradients",
                        "Large surfaces use flat tokenized colors; no rainbow or saturated hero gradients remain.",
                    ),
                    _finding(
                        "ai-slop-check.spacing",
                        "Spacing is constrained to a 4px/8px scale.",
                    ),
                ],
                "hierarchy_findings": [
                    _finding(
                        "hierarchy-rhythm-review.primary-action",
                        "Primary action is visually distinct through size, placement, and tokenized color.",
                    )
                ],
                "interaction_state_findings": [
                    _finding(
                        "interaction-states-pass.complete-states",
                        "Interactive controls cover default, hover, active, focus, disabled, and loading states.",
                    )
                ],
                "fixes_applied": [
                    "Removed decorative emoji and generic gradient styling from the design direction.",
                    "Snapped off-scale spacing to the declared token scale.",
                ],
            }
        }

    def design_release_gate(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        audit = _require_prior(context, "accessibility_audit")
        quality = _require_prior(context, "visual_quality_report")
        design_context = _require_prior(context, "design_context")
        attribution_refs = list(dict.fromkeys(
            [
                *[str(source) for source in design_context.get("sources", [])],
                str(design_context["upstream_attribution"]),
            ]
        ))
        return {
            "design_release_report": {
                "version": "1.0",
                "design_id": audit["design_id"],
                "status": "ready",
                "gates": [
                    {
                        "name": "accessibility-aa",
                        "status": "pass" if audit["verdict"] == "pass" else "fail",
                        "evidence_artifact": "accessibility_audit",
                    },
                    {
                        "name": "visual-quality",
                        "status": "pass" if quality["verdict"] == "pass" else "fail",
                        "evidence_artifact": "visual_quality_report",
                    },
                ],
                "open_decisions": [],
                "attribution_refs": attribution_refs,
            }
        }

    return FunctionStageExecutor(
        {
            "scope_design": scope_design,
            "extract_design_context": extract_design_context,
            "prototype_surface": prototype_surface,
            "accessibility_audit": accessibility_audit,
            "visual_quality_gate": visual_quality_gate,
            "design_release_gate": design_release_gate,
        },
        tools_used={
            "scope_design": ("design_context_reader",),
            "extract_design_context": (
                "design_source_reader",
                "design_token_extractor",
                "component_inventory",
            ),
            "prototype_surface": ("html_renderer", "interaction_probe"),
            "accessibility_audit": (
                "contrast_checker",
                "dom_semantics_probe",
                "keyboard_focus_probe",
                "motion_forms_probe",
            ),
            "visual_quality_gate": (
                "ai_slop_detector",
                "hierarchy_rhythm_probe",
                "interaction_state_probe",
            ),
            "design_release_gate": ("design_release_gate_runner",),
        },
    )


_register_design_semantics()
