"""Design domain pack helpers."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .core import (
    ContractError,
    FunctionStageExecutor,
    StageExecutionContext,
    load_json,
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
ACTIVE_DESIGN_GATES = {
    "accessibility-aa",
    "browser-render-probe",
    "component-inventory",
    "interaction-state-probe",
    "source-context-present",
    "token-traceability",
    "visual-quality",
}
EXPECTED_DESIGN_SKILL_MAPPINGS = {
    "accessibility-audit": {
        "category": "review",
        "mode": "quality_gate",
        "stage": "accessibility_audit",
        "artifacts": ("accessibility_audit",),
        "gates": ("accessibility-aa",),
    },
    "ai-slop-check": {
        "category": "review",
        "mode": "quality_gate",
        "stage": "visual_quality_gate",
        "artifacts": ("visual_quality_report",),
        "gates": ("visual-quality",),
    },
    "component-extract": {
        "category": "system",
        "mode": "pipeline_stage",
        "stage": "extract_design_context",
        "artifacts": ("design_context",),
        "gates": ("component-inventory",),
    },
    "design-system-extract": {
        "category": "system",
        "mode": "pipeline_stage",
        "stage": "extract_design_context",
        "artifacts": ("design_context",),
        "gates": ("token-traceability",),
    },
    "discovery-questions": {
        "category": "production",
        "mode": "pipeline_stage",
        "stage": "scope_design",
        "artifacts": ("design_brief",),
        "gates": ("source-context-present",),
    },
    "frontend-aesthetic-direction": {
        "category": "production",
        "mode": "pipeline_stage",
        "stage": "extract_design_context",
        "artifacts": ("design_context",),
        "gates": ("token-traceability",),
    },
    "generate-variations": {
        "category": "production",
        "mode": "corpus_only",
        "stage": "future_variation_generation",
        "artifacts": ("design_brief", "design_prototype_report"),
        "gates": ("future:variation-axis-capture",),
    },
    "hierarchy-rhythm-review": {
        "category": "review",
        "mode": "quality_gate",
        "stage": "visual_quality_gate",
        "artifacts": ("visual_quality_report",),
        "gates": ("visual-quality",),
    },
    "interaction-states-pass": {
        "category": "review",
        "mode": "quality_gate",
        "stage": "visual_quality_gate",
        "artifacts": ("design_prototype_report", "visual_quality_report"),
        "gates": ("interaction-state-probe", "visual-quality"),
    },
    "make-a-deck": {
        "category": "production",
        "mode": "corpus_only",
        "stage": "future_deck_production",
        "artifacts": ("design_prototype_report", "design_release_report"),
        "gates": ("future:render-evidence",),
    },
    "make-a-prototype": {
        "category": "production",
        "mode": "pipeline_stage",
        "stage": "prototype_surface",
        "artifacts": ("design_prototype_report",),
        "gates": ("browser-render-probe", "interaction-state-probe"),
    },
    "make-tweakable": {
        "category": "production",
        "mode": "corpus_only",
        "stage": "future_tweakable_prototype",
        "artifacts": ("design_prototype_report",),
        "gates": ("future:tweak-control-persistence",),
    },
    "polish-pass": {
        "category": "review",
        "mode": "quality_gate",
        "stage": "design_release_gate",
        "artifacts": ("accessibility_audit", "visual_quality_report", "design_release_report"),
        "gates": ("accessibility-aa", "visual-quality"),
    },
    "wireframe": {
        "category": "production",
        "mode": "corpus_only",
        "stage": "future_wireframe_exploration",
        "artifacts": ("design_brief", "design_prototype_report"),
        "gates": ("future:decision-capture",),
    },
}
EXPECTED_DESIGN_SKILL_DETAILS = {
    "discovery-questions": {
        "principles": (
            "Ask only when product, audience, fidelity, or variation scope is unclear",
            "Read attached design context before asking",
        ),
        "verification_hooks": (
            "design_brief.source_refs",
            "design_brief.primary_goal",
        ),
    },
    "frontend-aesthetic-direction": {
        "principles": (
            "Commit to a concrete visual system when no brand exists",
            "Declare typography, color, density, components, imagery, and motion",
        ),
        "verification_hooks": (
            "design_context.tokens",
            "design_context.gaps",
        ),
    },
    "wireframe": {
        "principles": (
            "Explore structure before polishing surface style",
            "Annotate decisions and tradeoffs",
        ),
        "verification_hooks": (
            "future:wireframe_options",
            "future:decision_log",
        ),
    },
    "make-a-deck": {
        "principles": (
            "Build slide systems with intentional layout and rhythm",
            "Verify deck rendering before delivery",
        ),
        "verification_hooks": (
            "design_prototype_report.probe",
            "design_release_report.gates",
        ),
    },
    "make-a-prototype": {
        "principles": (
            "Map screens and state before coding",
            "Wire interactions and verify behavior",
        ),
        "verification_hooks": (
            "design_prototype_report.render_checks",
            "design_prototype_report.states_covered",
        ),
    },
    "make-tweakable": {
        "principles": (
            "Expose meaningful design controls without cluttering the artifact",
            "Persist default values when tweak controls change output",
        ),
        "verification_hooks": (
            "future:tweak_controls",
            "design_prototype_report.render_checks",
        ),
    },
    "generate-variations": {
        "principles": (
            "Vary design direction substantively, not only cosmetics",
            "Recommend a direction with rationale",
        ),
        "verification_hooks": (
            "future:variation_axes",
            "future:recommendation_evidence",
        ),
    },
    "design-system-extract": {
        "principles": (
            "Extract tokens from sources before inventing styles",
            "Record gaps instead of silently filling missing design-system data",
        ),
        "verification_hooks": (
            "design_context.sources",
            "design_context.tokens",
        ),
    },
    "component-extract": {
        "principles": (
            "Inventory reusable components and interaction patterns",
            "Identify component gaps before production",
        ),
        "verification_hooks": (
            "design_context.components",
            "design_context.gaps",
        ),
    },
    "accessibility-audit": {
        "principles": (
            "Review contrast, semantics, keyboard focus, motion, and forms",
            "Do not pass WCAG blockers",
        ),
        "verification_hooks": (
            "accessibility_audit.checks",
            "accessibility_audit.blockers",
        ),
    },
    "ai-slop-check": {
        "principles": (
            "Detect generic AI aesthetics such as decorative gradients, filler icons, and unearned cards",
            "Fix or explicitly mark findings",
        ),
        "verification_hooks": (
            "visual_quality_report.ai_slop_findings",
            "visual_quality_report.fixes_applied",
        ),
    },
    "hierarchy-rhythm-review": {
        "principles": (
            "Review hierarchy signals and spacing rhythm",
            "Keep density and scale intentional",
        ),
        "verification_hooks": (
            "visual_quality_report.hierarchy_findings",
            "design_context.tokens.spacing",
        ),
    },
    "interaction-states-pass": {
        "principles": (
            "Inventory every interactive element",
            "Verify default, hover, active, focus-visible, disabled, loading, and feedback states",
        ),
        "verification_hooks": (
            "design_prototype_report.states_covered",
            "visual_quality_report.interaction_state_findings",
        ),
    },
    "polish-pass": {
        "principles": (
            "Aggregate final design review findings before release",
            "Release only when blocker and quality findings are closed",
        ),
        "verification_hooks": (
            "design_release_report.gates",
            "design_release_report.open_decisions",
        ),
    },
}
REQUIRED_DESIGN_SKILLS = {
    name: str(spec["category"])
    for name, spec in EXPECTED_DESIGN_SKILL_MAPPINGS.items()
}
DESIGN_SKILL_ORDER = (
    "discovery-questions",
    "frontend-aesthetic-direction",
    "wireframe",
    "make-a-deck",
    "make-a-prototype",
    "make-tweakable",
    "generate-variations",
    "design-system-extract",
    "component-extract",
    "accessibility-audit",
    "ai-slop-check",
    "hierarchy-rhythm-review",
    "interaction-states-pass",
    "polish-pass",
)
DEFAULT_SOURCE_ROOT = "examples/design_sources/upstream/claude-design-system-prompt-3c3ddb0"
DEFAULT_SOURCE_INDEX = "examples/design_sources/claude_design_skill_index.json"


def _require_prior(context: StageExecutionContext, artifact_name: str) -> dict[str, Any]:
    artifact = context.prior_artifacts.get(artifact_name)
    if artifact is None:
        raise ContractError(f"{context.stage_name} requires prior artifact {artifact_name!r}")
    return artifact


def _load_design_skill_corpus_from_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    source_root = scenario.get("source_root", DEFAULT_SOURCE_ROOT)
    source_index_ref = scenario.get("source_index", DEFAULT_SOURCE_INDEX)
    if not isinstance(source_root, str) or not source_root:
        raise ContractError("design skill corpus scenario requires source_root")
    if not isinstance(source_index_ref, str) or not source_index_ref:
        raise ContractError("design skill corpus scenario requires source_index")
    source_refs = scenario.get("source_refs")
    if not isinstance(source_refs, list) or not all(isinstance(ref, str) and ref for ref in source_refs):
        raise ContractError("design skill corpus scenario requires source_refs")
    expected_refs = _expected_design_source_refs(source_root, source_index_ref)
    if source_refs != expected_refs:
        raise ContractError("design skill corpus scenario source_refs must match expected upstream source refs")
    for ref in source_refs:
        if not _source_ref_exists(ref):
            raise ContractError(f"design skill corpus scenario source_ref is missing: {ref}")
    return _build_design_skill_corpus(source_root, source_index_ref, source_refs)


def _build_design_skill_corpus(
    source_root: str,
    source_index_ref: str,
    source_refs: list[str],
) -> dict[str, Any]:
    source_index = load_json(_local_path(source_index_ref))
    if source_index.get("source_root") != source_root:
        raise ContractError("design skill corpus source_index source_root mismatch")
    if source_index.get("source") != "https://github.com/Trystan-SA/claude-design-system-prompt":
        raise ContractError("design skill corpus source_index source mismatch")
    if source_index.get("commit") != "3c3ddb0":
        raise ContractError("design skill corpus source_index commit mismatch")
    if source_index.get("license") != "MIT":
        raise ContractError("design skill corpus source_index license mismatch")
    if source_index.get("variant") != "codex":
        raise ContractError("design skill corpus source_index variant mismatch")
    index_by_name = {
        str(skill.get("name", "")): skill
        for skill in source_index.get("skills", [])
        if isinstance(skill, dict)
    }
    skills: list[dict[str, Any]] = []
    for name in DESIGN_SKILL_ORDER:
        mapping = EXPECTED_DESIGN_SKILL_MAPPINGS[name]
        source_path = f"codex/skills/{name}.md"
        source_file = _local_path(source_root) / source_path
        if not source_file.exists():
            raise ContractError(f"design skill corpus missing upstream source file: {source_path}")
        source_sha256 = _sha256_file(source_file)
        index_skill = index_by_name.get(name)
        if index_skill is None:
            raise ContractError(f"design skill corpus source_index missing skill {name}")
        if index_skill.get("source_path") != source_path or index_skill.get("source_sha256") != source_sha256:
            raise ContractError(f"design skill corpus source_index digest mismatch for {name}")
        artifacts = list(mapping["artifacts"])
        gates = list(mapping["gates"])
        details = EXPECTED_DESIGN_SKILL_DETAILS[name]
        skills.append(
            {
                "name": name,
                "category": mapping["category"],
                "source_path": source_path,
                "source_sha256": source_sha256,
                "apk_mapping": {
                    "mode": mapping["mode"],
                    "stage": mapping["stage"],
                    "artifacts": artifacts,
                    "gates": gates,
                },
                "principles": list(details["principles"]),
                "verification_hooks": list(details["verification_hooks"]),
            }
        )
    return {
        "version": "1.0",
        "corpus_id": "claude-design-system-prompt-codex-skills",
        "upstream": {
            "repo_url": "https://github.com/Trystan-SA/claude-design-system-prompt",
            "commit": "3c3ddb0",
            "license": "MIT",
            "source_variant": "codex",
            "source_root": source_root,
            "source_index": source_index_ref,
            "attribution": UPSTREAM_ATTRIBUTION,
        },
        "source_refs": source_refs,
        "integration_policy": {
            "control_plane_boundary": (
                "The upstream prompt library is domain knowledge. APK owns the control plane "
                "through manifests, schemas, checkpoints, replay, and verification gates."
            ),
            "allowed_uses": [
                "Seed design pack stage skills and review criteria",
                "Map design skills to APK artifacts and gates",
                "Preserve upstream provenance for design-quality evidence",
            ],
            "disallowed_uses": [
                "Install the upstream system prompt as APK's global operating instruction",
                "Treat prompt compliance as release evidence without schema and replay gates",
                "Let upstream skill wording override APK control-plane contracts",
            ],
        },
        "skills": skills,
        "coverage": {
            "total": len(DESIGN_SKILL_ORDER),
            "production": sum(1 for skill in skills if skill["category"] == "production"),
            "system": sum(1 for skill in skills if skill["category"] == "system"),
            "review": sum(1 for skill in skills if skill["category"] == "review"),
            "skill_names": sorted(DESIGN_SKILL_ORDER),
        },
    }


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


def _source_ref_exists(ref: str) -> bool:
    return _is_url(ref) or _local_path(ref).exists()


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _expected_design_source_refs(source_root: str, source_index_ref: str = DEFAULT_SOURCE_INDEX) -> list[str]:
    return [
        "https://github.com/Trystan-SA/claude-design-system-prompt",
        f"{source_root}/LICENSE",
        source_index_ref,
        "examples/design_sources/claude_design_system_prompt_summary.md",
        "examples/design_sources/accessibility_audit_summary.md",
        "examples/design_sources/ai_slop_check_summary.md",
        "examples/design_sources/polish_pass_summary.md",
    ]


def _design_pipeline_stages() -> dict[str, dict[str, Any]]:
    stages_by_name: dict[str, dict[str, Any]] = {}
    for pipeline_name in ("design-review", "design-skill-corpus"):
        manifest = load_json(ROOT / "pipelines" / f"{pipeline_name}.json")
        for stage in manifest.get("stages", []):
            if isinstance(stage, dict) and isinstance(stage.get("name"), str):
                stages_by_name[stage["name"]] = stage
    return stages_by_name


def _design_tool_contracts() -> dict[str, dict[str, Any]]:
    registry = load_json(ROOT / "packs" / "design" / "tool_registry.json")
    return {
        str(tool.get("name", "")): tool
        for tool in registry.get("tools", [])
        if isinstance(tool, dict)
    }


def _design_role_contracts() -> dict[str, Any]:
    raw = load_json(ROOT / "packs" / "design" / "roles.json")
    return {
        "roles": [
            role for role in raw.get("roles", [])
            if isinstance(role, dict)
        ],
        "handoffs": [
            handoff for handoff in raw.get("handoff_artifacts", [])
            if isinstance(handoff, dict)
        ],
    }


def _artifact_schema_names() -> set[str]:
    return {
        path.stem
        for path in (ROOT / "schemas" / "artifacts").glob("*.json")
    }


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


def _design_skill_corpus_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    upstream = report.get("upstream", {})
    source_index_by_name: dict[str, dict[str, Any]] = {}
    if isinstance(upstream, dict):
        if upstream.get("repo_url") != "https://github.com/Trystan-SA/claude-design-system-prompt":
            errors.append("design_skill_corpus.upstream.repo_url must preserve upstream repository")
        if upstream.get("license") != "MIT":
            errors.append("design_skill_corpus.upstream.license must be MIT")
        if upstream.get("source_variant") != "codex":
            errors.append("design_skill_corpus.upstream.source_variant must be codex")
        source_root = upstream.get("source_root")
        if not isinstance(source_root, str) or not source_root:
            errors.append("design_skill_corpus.upstream.source_root is required")
        elif not _local_path(source_root).exists():
            errors.append("design_skill_corpus.upstream.source_root must exist locally")
        if "Trystan-SA/claude-design-system-prompt" not in str(upstream.get("attribution", "")):
            errors.append("design_skill_corpus.upstream.attribution must preserve upstream project attribution")
        source_index_ref = upstream.get("source_index")
        if isinstance(source_index_ref, str) and source_index_ref:
            try:
                source_index = load_json(_local_path(source_index_ref))
            except (OSError, json.JSONDecodeError, ContractError) as exc:
                errors.append(f"design_skill_corpus.upstream.source_index is not readable JSON: {exc}")
            else:
                if source_index.get("source") != upstream.get("repo_url"):
                    errors.append("design_skill_corpus.upstream.source_index source must match repo_url")
                if source_index.get("commit") != upstream.get("commit"):
                    errors.append("design_skill_corpus.upstream.source_index commit must match upstream commit")
                if source_index.get("license") != upstream.get("license"):
                    errors.append("design_skill_corpus.upstream.source_index license must match upstream license")
                if source_index.get("variant") != upstream.get("source_variant"):
                    errors.append("design_skill_corpus.upstream.source_index variant must match upstream source_variant")
                if source_index.get("source_root") != upstream.get("source_root"):
                    errors.append("design_skill_corpus.upstream.source_index source_root must match upstream source_root")
                source_skills = source_index.get("skills", [])
                if not isinstance(source_skills, list):
                    errors.append("design_skill_corpus.upstream.source_index skills must be an array")
                else:
                    source_index_by_name = {
                        str(skill.get("name", "")): skill
                        for skill in source_skills
                        if isinstance(skill, dict)
                    }
        else:
            errors.append("design_skill_corpus.upstream.source_index is required")
    source_refs = report.get("source_refs", [])
    if isinstance(source_refs, list):
        for ref in source_refs:
            if isinstance(ref, str) and not _source_ref_exists(ref):
                errors.append(f"design_skill_corpus.source_refs contains missing local source {ref!r}")
        if isinstance(upstream, dict):
            repo_url = upstream.get("repo_url")
            source_index = upstream.get("source_index")
            source_root = upstream.get("source_root")
            if repo_url not in source_refs:
                errors.append("design_skill_corpus.source_refs must include upstream repo_url")
            if source_index not in source_refs:
                errors.append("design_skill_corpus.source_refs must include upstream source_index")
            if (
                isinstance(source_root, str)
                and isinstance(source_index, str)
                and source_refs != _expected_design_source_refs(source_root, source_index)
            ):
                errors.append("design_skill_corpus.source_refs must exactly match expected upstream source refs")
    policy = report.get("integration_policy", {})
    if isinstance(policy, dict):
        boundary = str(policy.get("control_plane_boundary", ""))
        if "domain knowledge" not in boundary or "APK owns the control plane" not in boundary:
            errors.append("design_skill_corpus.integration_policy must keep upstream prompt outside the control plane")
        disallowed = " ".join(str(item) for item in policy.get("disallowed_uses", []))
        if "global operating instruction" not in disallowed:
            errors.append("design_skill_corpus.integration_policy must disallow global prompt installation")

    skills = [skill for skill in report.get("skills", []) if isinstance(skill, dict)]
    names = [str(skill.get("name", "")) for skill in skills]
    expected_names = set(REQUIRED_DESIGN_SKILLS)
    actual_names = set(names)
    missing = sorted(expected_names - actual_names)
    extra = sorted(actual_names - expected_names)
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if missing:
        errors.append(f"design_skill_corpus.skills missing required skills {missing}")
    if extra:
        errors.append(f"design_skill_corpus.skills contains unknown skills {extra}")
    if duplicates:
        errors.append(f"design_skill_corpus.skills contains duplicate skills {duplicates}")

    stages_by_name = _design_pipeline_stages()
    tools_by_name = _design_tool_contracts()
    role_contracts = _design_role_contracts()
    roles = role_contracts["roles"]
    handoffs = role_contracts["handoffs"]
    artifact_names = _artifact_schema_names()
    for skill in skills:
        name = str(skill.get("name", ""))
        expected_mapping = EXPECTED_DESIGN_SKILL_MAPPINGS.get(name, {})
        expected_category = str(expected_mapping.get("category", ""))
        if expected_category and skill.get("category") != expected_category:
            errors.append(f"design_skill_corpus.skills.{name}.category must be {expected_category}")
        expected_path = f"codex/skills/{name}.md"
        if name and skill.get("source_path") != expected_path:
            errors.append(f"design_skill_corpus.skills.{name}.source_path must be {expected_path}")
        source_index_skill = source_index_by_name.get(name)
        if source_index_skill is None:
            errors.append(f"design_skill_corpus.skills.{name} must appear in upstream source_index")
        else:
            for field in ("category", "source_path", "source_sha256"):
                if skill.get(field) != source_index_skill.get(field):
                    errors.append(f"design_skill_corpus.skills.{name}.{field} must match upstream source_index")
            if isinstance(upstream, dict) and isinstance(upstream.get("source_root"), str):
                source_file = _local_path(str(upstream["source_root"])) / expected_path
                if not source_file.exists():
                    errors.append(f"design_skill_corpus.skills.{name}.source_path must exist under upstream source_root")
                elif skill.get("source_sha256") != _sha256_file(source_file):
                    errors.append(f"design_skill_corpus.skills.{name}.source_sha256 must match upstream source file")
        mapping = skill.get("apk_mapping", {})
        if not isinstance(mapping, dict):
            errors.append(f"design_skill_corpus.skills.{name}.apk_mapping must be an object")
            continue
        expected_mode = expected_mapping.get("mode")
        expected_stage = expected_mapping.get("stage")
        expected_artifacts = list(expected_mapping.get("artifacts", ()))
        expected_gates = list(expected_mapping.get("gates", ()))
        expected_details = EXPECTED_DESIGN_SKILL_DETAILS.get(name, {})
        expected_principles = list(expected_details.get("principles", ()))
        expected_hooks = list(expected_details.get("verification_hooks", ()))
        if expected_principles and skill.get("principles") != expected_principles:
            errors.append(f"design_skill_corpus.skills.{name}.principles must match APK skill mapping")
        if expected_hooks and skill.get("verification_hooks") != expected_hooks:
            errors.append(f"design_skill_corpus.skills.{name}.verification_hooks must match APK skill mapping")
        if expected_mode and mapping.get("mode") != expected_mode:
            errors.append(f"design_skill_corpus.skills.{name}.apk_mapping.mode must be {expected_mode}")
        if expected_stage and mapping.get("stage") != expected_stage:
            errors.append(f"design_skill_corpus.skills.{name}.apk_mapping.stage must be {expected_stage}")
        artifacts = mapping.get("artifacts", [])
        if not isinstance(artifacts, list) or not artifacts:
            errors.append(f"design_skill_corpus.skills.{name}.apk_mapping.artifacts must not be empty")
        elif artifacts != expected_artifacts:
            errors.append(f"design_skill_corpus.skills.{name}.apk_mapping.artifacts must be {expected_artifacts}")
        else:
            for artifact in artifacts:
                if artifact not in artifact_names:
                    errors.append(f"design_skill_corpus.skills.{name}.apk_mapping.artifacts references unknown artifact {artifact!r}")
        gates = mapping.get("gates", [])
        if not isinstance(gates, list) or not gates:
            errors.append(f"design_skill_corpus.skills.{name}.apk_mapping.gates must not be empty")
        elif gates != expected_gates:
            errors.append(f"design_skill_corpus.skills.{name}.apk_mapping.gates must be {expected_gates}")
        mode = mapping.get("mode")
        stage = mapping.get("stage")
        if mode == "corpus_only":
            if not isinstance(stage, str) or not stage.startswith("future_"):
                errors.append(f"design_skill_corpus.skills.{name}.apk_mapping.stage must be explicitly future-only")
            for gate in gates if isinstance(gates, list) else []:
                if gate not in ACTIVE_DESIGN_GATES and not str(gate).startswith("future:"):
                    errors.append(f"design_skill_corpus.skills.{name}.apk_mapping.gates future-only gate must use future: prefix")
            continue

        live_stage = stages_by_name.get(str(stage))
        if not isinstance(live_stage, dict):
            errors.append(f"design_skill_corpus.skills.{name}.apk_mapping.stage must reference a design pipeline stage")
            continue

        stage_produces = set(live_stage.get("produces", []))
        stage_inputs = set(live_stage.get("required_artifacts_in", []))
        stage_artifacts = stage_produces | stage_inputs
        for artifact in artifacts if isinstance(artifacts, list) else []:
            if artifact not in stage_artifacts:
                errors.append(
                    f"design_skill_corpus.skills.{name}.apk_mapping.artifacts references artifact {artifact!r} "
                    f"outside stage {stage!r} inputs/outputs"
                )

        tool_inputs: set[str] = set()
        tool_outputs: set[str] = set()
        for tool_name in live_stage.get("tools_available", []):
            tool = tools_by_name.get(str(tool_name))
            if not isinstance(tool, dict):
                errors.append(f"design_skill_corpus.skills.{name}.apk_mapping.stage uses unknown tool {tool_name!r}")
                continue
            tool_inputs.update(str(item) for item in tool.get("input_artifacts", []) if isinstance(item, str))
            tool_outputs.update(str(item) for item in tool.get("output_artifacts", []) if isinstance(item, str))
        for artifact in artifacts if isinstance(artifacts, list) else []:
            if artifact in stage_produces and artifact not in tool_outputs:
                errors.append(
                    f"design_skill_corpus.skills.{name}.apk_mapping.artifacts produced artifact {artifact!r} "
                    "must be declared by a stage tool output"
                )
            if artifact in stage_inputs and artifact not in tool_inputs:
                errors.append(
                    f"design_skill_corpus.skills.{name}.apk_mapping.artifacts input artifact {artifact!r} "
                    "must be declared by a stage tool input"
                )

        owners = [
            role for role in roles
            if str(stage) in role.get("owns_stages", [])
        ]
        if len(owners) != 1:
            errors.append(f"design_skill_corpus.skills.{name}.apk_mapping.stage must have exactly one explicit role owner")
        else:
            owner = owners[0]
            owner_outputs = set(owner.get("required_outputs", []))
            missing_outputs = sorted(stage_produces - owner_outputs)
            if missing_outputs:
                errors.append(
                    f"design_skill_corpus.skills.{name}.apk_mapping.stage owner must require outputs {missing_outputs}"
                )
            owner_name = str(owner.get("name", ""))
            for artifact in stage_inputs:
                if not any(
                    handoff.get("to") == owner_name
                    and handoff.get("artifact") == artifact
                    and handoff.get("required_review") == "pass"
                    for handoff in handoffs
                ):
                    errors.append(
                        f"design_skill_corpus.skills.{name}.apk_mapping.stage input {artifact!r} "
                        f"must have a passing handoff to {owner_name!r}"
                    )

        if mode != "corpus_only":
            for gate in gates if isinstance(gates, list) else []:
                if gate not in ACTIVE_DESIGN_GATES:
                    errors.append(f"design_skill_corpus.skills.{name}.apk_mapping.gates references unknown active gate {gate!r}")

    coverage = report.get("coverage", {})
    if isinstance(coverage, dict):
        skill_names = coverage.get("skill_names", [])
        if sorted(skill_names) != sorted(REQUIRED_DESIGN_SKILLS):
            errors.append("design_skill_corpus.coverage.skill_names must exactly match required upstream skills")
        counts = {
            "production": sum(1 for category in REQUIRED_DESIGN_SKILLS.values() if category == "production"),
            "system": sum(1 for category in REQUIRED_DESIGN_SKILLS.values() if category == "system"),
            "review": sum(1 for category in REQUIRED_DESIGN_SKILLS.values() if category == "review"),
        }
        if coverage.get("total") != len(REQUIRED_DESIGN_SKILLS):
            errors.append("design_skill_corpus.coverage.total must match required skill count")
        for category, expected_count in counts.items():
            if coverage.get(category) != expected_count:
                errors.append(f"design_skill_corpus.coverage.{category} must be {expected_count}")
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
        "design_skill_corpus": _design_skill_corpus_errors,
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

    def catalog_design_skills(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        return {"design_skill_corpus": _load_design_skill_corpus_from_scenario(scenario)}

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
        corpus = _require_prior(context, "design_skill_corpus")
        future_only_skills = sorted(
            str(skill["name"])
            for skill in corpus.get("skills", [])
            if isinstance(skill, dict)
            and isinstance(skill.get("apk_mapping"), dict)
            and skill["apk_mapping"].get("mode") == "corpus_only"
        )
        gaps = list(scenario["gaps"])
        if future_only_skills:
            gaps.append(
                "Future-only design skills not executed in this pipeline: "
                + ", ".join(future_only_skills)
            )
        return {
            "design_context": {
                "version": "1.0",
                "design_id": brief["design_id"],
                "sources": list(dict.fromkeys([
                    *brief["source_refs"],
                    corpus["upstream"]["source_index"],
                ])),
                "tokens": scenario["tokens"],
                "components": scenario["components"],
                "gaps": gaps,
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
            "catalog_design_skills": catalog_design_skills,
            "scope_design": scope_design,
            "extract_design_context": extract_design_context,
            "prototype_surface": prototype_surface,
            "accessibility_audit": accessibility_audit,
            "visual_quality_gate": visual_quality_gate,
            "design_release_gate": design_release_gate,
        },
        tools_used={
            "catalog_design_skills": ("design_skill_catalog_reader",),
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


def build_design_skill_corpus_executor(scenario: dict[str, Any]) -> FunctionStageExecutor:
    """Create a deterministic executor for the design skill corpus pipeline."""

    def catalog_design_skills(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        return {"design_skill_corpus": _load_design_skill_corpus_from_scenario(scenario)}

    return FunctionStageExecutor(
        {"catalog_design_skills": catalog_design_skills},
        tools_used={"catalog_design_skills": ("design_skill_catalog_reader",)},
    )


_register_design_semantics()
