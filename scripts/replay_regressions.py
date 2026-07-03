#!/usr/bin/env python3
"""Replay golden regression scenarios."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from apkernel import CheckpointStore, ContractError, PipelineManifest, ReplayHarness, Reviewer, RunEngine, SchemaRegistry, artifacts_from_checkpoints, checkpoints_from_paths, load_domain_packs, load_json, validate_artifact_semantics
from apkernel.packs import DomainPack


def _add_static_replay_artifacts(
    schemas: SchemaRegistry,
    packs: list[DomainPack],
    actual_by_pipeline: dict[str, dict[str, dict[str, object]]],
    checkpoints_by_pipeline: dict[str, dict[str, dict[str, object]]],
) -> None:
    for pack in packs:
        for spec in pack.static_replays:
            actual, checkpoints = spec.load_fixture(ROOT)
            for artifact_name, artifact in actual.items():
                schemas.validate(artifact_name, artifact)
                validate_artifact_semantics(artifact_name, artifact)
            actual_by_pipeline[spec.pipeline] = actual
            checkpoints_by_pipeline[spec.pipeline] = checkpoints


def _actual_from_checkpoints() -> tuple[
    dict[str, dict[str, dict[str, object]]],
    dict[str, dict[str, dict[str, object]]],
]:
    schemas = SchemaRegistry(ROOT / "schemas" / "artifacts").load()
    reviewer = Reviewer(schemas)
    packs = load_domain_packs(ROOT)
    store_root = ROOT / ".apk" / f"replay-runs-{os.getpid()}"
    reset_store = CheckpointStore(store_root, schemas)
    reset_store.reset()
    actual_by_pipeline: dict[str, dict[str, dict[str, object]]] = {}
    checkpoints_by_pipeline: dict[str, dict[str, dict[str, object]]] = {}
    for pack in packs:
        store = CheckpointStore(store_root, schemas, pack.role_policy(ROOT))
        engine = RunEngine(store, reviewer)
        for spec in pack.scenarios:
            manifest = PipelineManifest.load(ROOT / "pipelines" / f"{spec.pipeline}.json")
            scenario = load_json(ROOT / "examples" / spec.scenario_file)
            checkpoints = engine.run_with_executor(
                manifest,
                scenario["run_id"],
                spec.executor_factory()(scenario),
                scenario=scenario,
                metadata={"demo": True, "execution_mode": "stage_executor", "domain_pack": pack.name},
            )
            ordered_paths = tuple(checkpoints[stage] for stage in manifest.stage_names())
            actual_by_pipeline[spec.pipeline] = artifacts_from_checkpoints(ordered_paths)
            checkpoints_by_pipeline[spec.pipeline] = checkpoints_from_paths(ordered_paths)
    _add_static_replay_artifacts(schemas, packs, actual_by_pipeline, checkpoints_by_pipeline)
    return actual_by_pipeline, checkpoints_by_pipeline


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", dest="case_id")
    args = parser.parse_args()

    actual_by_pipeline, checkpoints_by_pipeline = _actual_from_checkpoints()
    harness = ReplayHarness(ROOT / "examples")
    results = []
    for replay_scenario in harness.load_scenarios("regression"):
        searchable = json.dumps(
            {
                "name": replay_scenario.name,
                "inputs": replay_scenario.inputs,
                "expected_artifacts": replay_scenario.expected_artifacts,
                "expected_checkpoints": replay_scenario.expected_checkpoints,
            },
            sort_keys=True,
        )
        if args.case_id and args.case_id not in searchable:
            continue
        actual = actual_by_pipeline.get(replay_scenario.pipeline)
        if actual is None:
            raise ContractError(f"no actual artifacts for {replay_scenario.pipeline}")
        results.append(
            harness.evaluate(
                replay_scenario,
                actual,
                checkpoints_by_pipeline.get(replay_scenario.pipeline, {}),
            )
        )

    if args.case_id and not results:
        print(json.dumps({"error": f"no replay case matched {args.case_id!r}"}, indent=2))
        return 1

    failed = [result for result in results if not result.passed]
    print(json.dumps([result.to_dict() for result in results], indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
