#!/usr/bin/env python3
"""End-to-end contract verification for Agent Production Kernel."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apkernel import CheckpointStore, ContractError, PipelineManifest, ReplayHarness, Reviewer, RunEngine, SchemaRegistry, ToolContract, artifacts_from_checkpoints, build_real_repo_corpus_report, checkpoints_from_paths, load_domain_packs, load_json, validate_artifact_semantics
from apkernel.packs import DomainPack


def _add_static_replay_artifacts(
    root: Path,
    schemas: SchemaRegistry,
    packs: list[DomainPack],
    actual_by_pipeline: dict[str, dict[str, dict[str, object]]],
    checkpoints_by_pipeline: dict[str, dict[str, dict[str, object]]],
) -> None:
    for pack in packs:
        for spec in pack.static_replays:
            actual, checkpoints = spec.load_fixture(root)
            for artifact_name, artifact in actual.items():
                schemas.validate(artifact_name, artifact)
                validate_artifact_semantics(artifact_name, artifact)
            actual_by_pipeline[spec.pipeline] = actual
            checkpoints_by_pipeline[spec.pipeline] = checkpoints


def _load_pack_tools(root: Path, schemas: SchemaRegistry, packs: list[DomainPack]) -> dict[str, ToolContract]:
    tools: dict[str, ToolContract] = {}
    for pack in packs:
        registry = pack.tool_registry(root)
        registry.require_outputs_have_schemas(schemas)
        for name, tool in registry.tools.items():
            if name in tools:
                raise ContractError(f"duplicate tool {name!r} across packs")
            tools[name] = tool
    return tools


def _verify_pipeline_manifests(root: Path, schemas: SchemaRegistry, tools: dict[str, ToolContract]) -> list[PipelineManifest]:
    manifests = []
    for path in sorted((root / "pipelines").glob("*.json")):
        manifest = PipelineManifest.load(path)
        for stage in manifest.stages:
            skill = stage.get("skill")
            if isinstance(skill, str) and not (root / f"{skill}.md").exists():
                raise ContractError(
                    f"{path.name}:{stage['name']} references missing skill {skill}.md"
                )
            for artifact_name in stage.get("produces", []) + stage.get("required_artifacts_in", []):
                if artifact_name not in schemas.schemas:
                    raise ContractError(f"{path.name}:{stage['name']} references unknown artifact {artifact_name}")
            for tool_name in stage.get("tools_available", []):
                if tool_name not in tools:
                    raise ContractError(f"{path.name}:{stage['name']} references unknown tool {tool_name}")
            required_inputs = set(stage.get("required_artifacts_in", []))
            if required_inputs:
                declared_tool_inputs: set[str] = set()
                for tool_name in stage.get("tools_available", []):
                    declared_tool_inputs.update(tools[tool_name].input_artifacts)
                missing_inputs = sorted(required_inputs - declared_tool_inputs)
                if missing_inputs:
                    raise ContractError(
                        f"{path.name}:{stage['name']} tools_available do not declare input artifacts {missing_inputs}"
                    )
        manifests.append(manifest)
    if not manifests:
        raise ContractError("no pipeline manifests found")
    return manifests


def _verify_external_adapters(root: Path) -> int:
    raw = load_json(root / "integrations" / "external_adapters.json")
    adapters = raw.get("adapters")
    if not isinstance(adapters, list) or not adapters:
        raise ContractError("external_adapters.json must define adapters[]")
    required = {"name", "phase", "integration_mode", "provides", "kernel_mapping", "trust_boundary", "not_core_reason"}
    for adapter in adapters:
        missing = required - set(adapter)
        if missing:
            raise ContractError(f"adapter missing fields: {sorted(missing)}")
        if adapter["phase"] != 3:
            raise ContractError(f"{adapter['name']} must be phase 3")
    return len(adapters)


def _verify_roles(root: Path, schemas: SchemaRegistry, packs: list[DomainPack]) -> int:
    special_outputs = {"review", "approval_decision"}
    role_count = 0
    for pack in packs:
        raw = load_json(root / pack.role_policy_file)
        roles = raw.get("roles")
        if not isinstance(roles, list) or not roles:
            raise ContractError(f"{pack.role_policy_file} must define roles[]")
        role_count += len(roles)
        for role in roles:
            for field in ("name", "owns_stages", "allowed_inputs", "required_outputs", "must_not", "review_by", "approval_by"):
                if field not in role:
                    raise ContractError(f"role missing {field}: {role}")
            for artifact_name in role.get("required_outputs", []):
                if artifact_name not in schemas.schemas and artifact_name not in special_outputs:
                    raise ContractError(f"role {role['name']} outputs unknown artifact {artifact_name}")
        for handoff in raw.get("handoff_artifacts", []):
            artifact_name = handoff.get("artifact")
            if artifact_name not in schemas.schemas:
                raise ContractError(f"handoff references unknown artifact {artifact_name}")
    return role_count


def _run_all_pipeline_scenarios(
    root: Path, schemas: SchemaRegistry, manifests: list[PipelineManifest], packs: list[DomainPack]
) -> tuple[
    dict[str, dict[str, str]],
    dict[str, dict[str, dict[str, object]]],
    dict[str, dict[str, dict[str, object]]],
]:
    reviewer = Reviewer(schemas)
    store_root = root / ".apk" / f"verify-runs-{os.getpid()}"
    reset_store = CheckpointStore(store_root, schemas)
    reset_store.reset()
    by_name = {manifest.name: manifest for manifest in manifests}
    all_checkpoints: dict[str, dict[str, str]] = {}
    actual_by_pipeline: dict[str, dict[str, dict[str, object]]] = {}
    checkpoints_by_pipeline: dict[str, dict[str, dict[str, object]]] = {}
    for pack in packs:
        store = CheckpointStore(store_root, schemas, pack.role_policy(root))
        engine = RunEngine(store, reviewer)
        for spec in pack.scenarios:
            manifest = by_name[spec.pipeline]
            scenario = load_json(root / "examples" / spec.scenario_file)
            checkpoints = engine.run_with_executor(
                manifest,
                scenario["run_id"],
                spec.executor_factory()(scenario),
                scenario=scenario,
                metadata={"demo": True, "execution_mode": "stage_executor", "domain_pack": pack.name},
            )
            all_checkpoints[spec.pipeline] = {
                stage: str(Path(path).relative_to(root)) for stage, path in checkpoints.items()
            }
            ordered_paths = tuple(checkpoints[stage] for stage in manifest.stage_names())
            actual_by_pipeline[spec.pipeline] = artifacts_from_checkpoints(ordered_paths)
            checkpoints_by_pipeline[spec.pipeline] = checkpoints_from_paths(ordered_paths)

    _add_static_replay_artifacts(root, schemas, packs, actual_by_pipeline, checkpoints_by_pipeline)

    decision_log = store.run_dir("demo-bug-fix", "software-bug-fix") / "decision_log.json"
    if not decision_log.exists():
        raise ContractError("decision log was not merged")
    merged = load_json(decision_log)
    if merged.get("run_id") != "demo-bug-fix" or len(merged.get("decisions", [])) != 1:
        raise ContractError("expected one merged decision for demo-bug-fix")
    return all_checkpoints, actual_by_pipeline, checkpoints_by_pipeline


def _verify_replay(
    root: Path,
    actual_by_pipeline: dict[str, dict[str, dict[str, object]]],
    checkpoints_by_pipeline: dict[str, dict[str, dict[str, object]]],
) -> int:
    harness = ReplayHarness(root / "examples")
    scenarios = harness.load_scenarios("regression")
    if not scenarios:
        raise ContractError("no regression replay scenarios found")
    for scenario in scenarios:
        actual = actual_by_pipeline.get(scenario.pipeline)
        if actual is None:
            raise ContractError(f"no actual artifacts for replay pipeline {scenario.pipeline}")
        result = harness.evaluate(
            scenario,
            actual,
            checkpoints_by_pipeline.get(scenario.pipeline, {}),
        )
        if not result.passed:
            raise ContractError(f"replay failed: {result.to_dict()}")
    return len(scenarios)


def main() -> int:
    schemas = SchemaRegistry(ROOT / "schemas" / "artifacts").load()
    packs = load_domain_packs(ROOT)
    tools = _load_pack_tools(ROOT, schemas, packs)
    manifests = _verify_pipeline_manifests(ROOT, schemas, tools)
    adapter_count = _verify_external_adapters(ROOT)
    role_count = _verify_roles(ROOT, schemas, packs)
    checkpoints, actual_by_pipeline, checkpoints_by_pipeline = _run_all_pipeline_scenarios(ROOT, schemas, manifests, packs)
    replay_scenarios = _verify_replay(ROOT, actual_by_pipeline, checkpoints_by_pipeline)
    corpus_report = build_real_repo_corpus_report(
        ROOT,
        schemas,
        producer_command="python3 scripts/verify.py",
    )

    summary = {
        "status": "pass",
        "schemas": len(schemas.schemas),
        "pipelines": [manifest.name for manifest in manifests],
        "domain_packs": [pack.name for pack in packs],
        "tools": len(tools),
        "external_adapters": adapter_count,
        "roles": role_count,
        "replay_scenarios": replay_scenarios,
        "real_repo_corpus": {
            "summary": corpus_report["summary"],
            "freshness": corpus_report["freshness"],
            "approval_boundary": corpus_report["approval_boundary"],
        },
        "scenario_checkpoints": checkpoints,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
