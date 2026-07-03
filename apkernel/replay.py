"""Replay harness for regression and golden-scenario checks."""

from __future__ import annotations

import dataclasses
import copy
import json
import re
from pathlib import Path
from typing import Any, Callable

from .core import ContractError, load_json


@dataclasses.dataclass(frozen=True)
class ReplayScenario:
    name: str
    pipeline: str
    inputs: dict[str, Any]
    expected_artifacts: dict[str, dict[str, Any]]
    expected_checkpoints: dict[str, dict[str, Any]]
    expected_result: str
    mutations: tuple[dict[str, Any], ...]
    expected_errors_contain: tuple[str, ...]
    tags: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: Path) -> "ReplayScenario":
        raw = load_json(path)
        expected = raw.get("expected_artifacts")
        if not isinstance(expected, dict):
            raise ContractError(f"{path} missing expected_artifacts")
        expected_checkpoints = raw.get("expected_checkpoints", {})
        if not isinstance(expected_checkpoints, dict):
            raise ContractError(f"{path} expected_checkpoints must be an object")
        expected_result = raw.get("expected_result", "pass")
        if expected_result not in {"pass", "fail"}:
            raise ContractError(f"{path} expected_result must be pass or fail")
        mutations = raw.get("mutations", [])
        if not isinstance(mutations, list):
            raise ContractError(f"{path} mutations must be a list")
        expected_errors = raw.get("expected_errors_contain", [])
        if not isinstance(expected_errors, list) or not all(isinstance(item, str) for item in expected_errors):
            raise ContractError(f"{path} expected_errors_contain must be strings")
        return cls(
            name=raw["name"],
            pipeline=raw["pipeline"],
            inputs=raw.get("inputs", {}),
            expected_artifacts=expected,
            expected_checkpoints=expected_checkpoints,
            expected_result=expected_result,
            mutations=tuple(mutations),
            expected_errors_contain=tuple(expected_errors),
            tags=tuple(raw.get("tags", [])),
        )


@dataclasses.dataclass(frozen=True)
class ReplayResult:
    scenario: str
    passed: bool
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class ReplayHarness:
    """Compares actual artifacts against expected partial artifact shapes."""

    def __init__(self, scenarios_dir: Path) -> None:
        self.scenarios_dir = scenarios_dir

    def load_scenarios(self, tag: str | None = None) -> list[ReplayScenario]:
        scenarios = [
            ReplayScenario.load(path)
            for path in sorted(self.scenarios_dir.glob("golden_*.json"))
        ]
        if tag is None:
            return scenarios
        return [scenario for scenario in scenarios if tag in scenario.tags]

    def evaluate(
        self,
        scenario: ReplayScenario,
        actual_artifacts: dict[str, dict[str, Any]],
        actual_checkpoints: dict[str, dict[str, Any]] | None = None,
    ) -> ReplayResult:
        errors: list[str] = []
        bundle = {
            "artifacts": copy.deepcopy(actual_artifacts),
            "checkpoints": copy.deepcopy(actual_checkpoints or {}),
        }
        for mutation in scenario.mutations:
            _apply_mutation(bundle, mutation)

        for artifact_name, expected_partial in scenario.expected_artifacts.items():
            actual = bundle["artifacts"].get(artifact_name)
            if actual is None:
                errors.append(f"missing artifact {artifact_name}")
                continue
            errors.extend(_find_missing(expected_partial, actual, path=artifact_name))
        for stage_name, expected_partial in scenario.expected_checkpoints.items():
            actual = bundle["checkpoints"].get(stage_name)
            if actual is None:
                errors.append(f"missing checkpoint {stage_name}")
                continue
            errors.extend(_find_missing(expected_partial, actual, path=f"checkpoint:{stage_name}"))

        if scenario.expected_result == "pass":
            passed = not errors
        else:
            joined = "\n".join(errors)
            expected_error_matches = all(fragment in joined for fragment in scenario.expected_errors_contain)
            passed = bool(errors) and expected_error_matches
        return ReplayResult(scenario=scenario.name, passed=passed, errors=tuple(errors))

    def run_all(
        self,
        runner: Callable[[ReplayScenario], dict[str, dict[str, Any]]],
        *,
        tag: str | None = None,
    ) -> list[ReplayResult]:
        return [self.evaluate(scenario, runner(scenario)) for scenario in self.load_scenarios(tag)]


def artifacts_from_checkpoints(checkpoint_paths: list[str | Path] | tuple[str | Path, ...]) -> dict[str, dict[str, Any]]:
    """Load cumulative artifacts from persisted completed checkpoints."""

    artifacts: dict[str, dict[str, Any]] = {}
    for raw_path in checkpoint_paths:
        checkpoint = load_json(Path(raw_path))
        if checkpoint.get("status") != "completed":
            raise ContractError(f"{raw_path} is not a completed checkpoint")
        payload = checkpoint.get("artifacts")
        if not isinstance(payload, dict):
            raise ContractError(f"{raw_path} missing artifacts")
        for artifact_name, artifact in payload.items():
            if not isinstance(artifact, dict):
                raise ContractError(f"{raw_path} artifact {artifact_name} must be object")
            artifacts[artifact_name] = artifact
    return artifacts


def checkpoints_from_paths(checkpoint_paths: list[str | Path] | tuple[str | Path, ...]) -> dict[str, dict[str, Any]]:
    """Load checkpoints by stage name."""

    checkpoints: dict[str, dict[str, Any]] = {}
    for raw_path in checkpoint_paths:
        checkpoint = load_json(Path(raw_path))
        stage = checkpoint.get("stage")
        if not isinstance(stage, str) or not stage:
            raise ContractError(f"{raw_path} missing checkpoint stage")
        checkpoints[stage] = checkpoint
    return checkpoints


def _find_missing(expected: Any, actual: Any, *, path: str) -> list[str]:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path} expected object"]
        errors: list[str] = []
        for key, value in expected.items():
            if key not in actual:
                errors.append(f"{path}.{key} missing")
            else:
                errors.extend(_find_missing(value, actual[key], path=f"{path}.{key}"))
        return errors
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path} expected array"]
        errors = []
        for expected_item in expected:
            if isinstance(expected_item, dict):
                matched = any(
                    isinstance(actual_item, dict)
                    and not _find_missing(expected_item, actual_item, path=path)
                    for actual_item in actual
                )
                if not matched:
                    encoded = json.dumps(expected_item, sort_keys=True)
                    errors.append(f"{path} missing expected partial item {encoded}")
                continue
            encoded_actual = [json.dumps(item, sort_keys=True) for item in actual]
            encoded = json.dumps(expected_item, sort_keys=True)
            if encoded not in encoded_actual:
                errors.append(f"{path} missing expected item {encoded}")
        return errors
    if expected != actual:
        return [f"{path} expected {expected!r}, got {actual!r}"]
    return []


def _apply_mutation(bundle: dict[str, Any], mutation: dict[str, Any]) -> None:
    if not isinstance(mutation, dict):
        raise ContractError("replay mutation must be an object")
    path = mutation.get("path")
    if not isinstance(path, str) or not path:
        raise ContractError("replay mutation missing path")
    op = mutation.get("op", "set")
    if op not in {"set", "delete"}:
        raise ContractError(f"unsupported replay mutation op {op!r}")
    parent, key = _resolve_parent(bundle, path)
    if op == "set":
        if "value" not in mutation:
            raise ContractError("set mutation missing value")
        if isinstance(parent, list):
            parent[key] = mutation["value"]
        else:
            parent[key] = mutation["value"]
        return
    if isinstance(parent, list):
        del parent[key]
    else:
        parent.pop(key, None)


def _resolve_parent(root: Any, path: str) -> tuple[Any, Any]:
    parts = path.split(".")
    current = root
    for raw_part in parts[:-1]:
        current = _resolve_part(current, raw_part)
    final = _parse_part(parts[-1])
    parent = _resolve_part(current, final[0]) if final[1] is not None else current
    return parent, final[1] if final[1] is not None else final[0]


def _resolve_part(current: Any, raw_part: str) -> Any:
    key, index = _parse_part(raw_part)
    if not isinstance(current, dict) or key not in current:
        raise ContractError(f"mutation path missing key {key!r}")
    value = current[key]
    if index is None:
        return value
    if not isinstance(value, list):
        raise ContractError(f"mutation path {key!r} is not a list")
    if index >= len(value):
        raise ContractError(f"mutation path {key!r}[{index}] out of range")
    return value[index]


def _parse_part(raw_part: str) -> tuple[str, int | None]:
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_:-]*)(?:\[(\d+)\])?", raw_part)
    if not match:
        raise ContractError(f"invalid mutation path part {raw_part!r}")
    index = int(match.group(2)) if match.group(2) is not None else None
    return match.group(1), index
