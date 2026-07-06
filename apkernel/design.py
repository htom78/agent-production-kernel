"""Design domain pack helpers."""

from __future__ import annotations

from typing import Any

from .core import ContractError, FunctionStageExecutor, StageExecutionContext


UPSTREAM_ATTRIBUTION = (
    "Adapted from Trystan-SA/claude-design-system-prompt "
    "(MIT), using APK schemas/checkpoints as the source of truth."
)


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
        return {
            "design_prototype_report": {
                "version": "1.0",
                "design_id": context_artifact["design_id"],
                "artifact_path": scenario["artifact_path"],
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
                "render_checks": [
                    {
                        "name": "static-html-render",
                        "status": "pass",
                        "evidence": "Prototype shell renders without missing canonical sections.",
                    },
                    {
                        "name": "responsive-breakpoints",
                        "status": "pass",
                        "evidence": "Desktop and mobile layout constraints are represented in the artifact plan.",
                    },
                ],
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
                "attribution_refs": [
                    "https://github.com/Trystan-SA/claude-design-system-prompt",
                    "https://raw.githubusercontent.com/Trystan-SA/claude-design-system-prompt/main/LICENSE",
                ],
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
