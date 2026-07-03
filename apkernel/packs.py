"""Domain pack registry and executor resolution."""

from __future__ import annotations

import dataclasses
import importlib
from pathlib import Path
from typing import Any, Callable

from .core import ContractError, RolePolicy, StageExecutor, ToolRegistry, load_json


ExecutorFactory = Callable[[dict[str, Any]], StageExecutor]


@dataclasses.dataclass(frozen=True)
class ScenarioSpec:
    pipeline: str
    scenario_file: str
    executor: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ScenarioSpec":
        for field in ("pipeline", "scenario_file", "executor"):
            if not isinstance(raw.get(field), str) or not raw[field]:
                raise ContractError(f"scenario missing {field}")
        return cls(
            pipeline=raw["pipeline"],
            scenario_file=raw["scenario_file"],
            executor=raw["executor"],
        )

    def executor_factory(self) -> ExecutorFactory:
        module_name, sep, attr = self.executor.partition(":")
        if not sep or not module_name or not attr:
            raise ContractError(f"invalid executor reference {self.executor!r}")
        module = importlib.import_module(module_name)
        factory = getattr(module, attr, None)
        if factory is None or not callable(factory):
            raise ContractError(f"executor {self.executor!r} is not callable")
        return factory


@dataclasses.dataclass(frozen=True)
class StaticReplaySpec:
    pipeline: str
    fixture_file: str
    artifact: str
    artifact_field: str | None = None
    checkpoints_field: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StaticReplaySpec":
        for field in ("pipeline", "fixture_file", "artifact"):
            if not isinstance(raw.get(field), str) or not raw[field]:
                raise ContractError(f"static replay missing {field}")
        artifact_field = raw.get("artifact_field")
        checkpoints_field = raw.get("checkpoints_field")
        if artifact_field is not None and not isinstance(artifact_field, str):
            raise ContractError("static replay artifact_field must be a string")
        if checkpoints_field is not None and not isinstance(checkpoints_field, str):
            raise ContractError("static replay checkpoints_field must be a string")
        return cls(
            pipeline=raw["pipeline"],
            fixture_file=raw["fixture_file"],
            artifact=raw["artifact"],
            artifact_field=artifact_field,
            checkpoints_field=checkpoints_field,
        )

    def load_fixture(self, root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        fixture = load_json(root / "examples" / self.fixture_file)
        artifact_payload = fixture
        if self.artifact_field:
            artifact_payload = fixture.get(self.artifact_field)
            if not isinstance(artifact_payload, dict):
                raise ContractError(f"{self.fixture_file} missing {self.artifact_field}")
        checkpoints: dict[str, dict[str, Any]] = {}
        if self.checkpoints_field:
            raw_checkpoints = fixture.get(self.checkpoints_field)
            if not isinstance(raw_checkpoints, dict):
                raise ContractError(f"{self.fixture_file} missing {self.checkpoints_field}")
            checkpoints = raw_checkpoints
        return {self.artifact: artifact_payload}, checkpoints


@dataclasses.dataclass(frozen=True)
class DomainPack:
    name: str
    role_policy_file: str
    tool_registry_file: str
    pipelines: tuple[str, ...]
    scenarios: tuple[ScenarioSpec, ...]
    static_replays: tuple[StaticReplaySpec, ...]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DomainPack":
        for field in ("name", "role_policy_file", "tool_registry_file", "pipelines"):
            if field not in raw:
                raise ContractError(f"pack missing {field}")
        if not isinstance(raw["name"], str) or not raw["name"]:
            raise ContractError("pack name must be a string")
        if not isinstance(raw["role_policy_file"], str) or not raw["role_policy_file"]:
            raise ContractError(f"pack {raw['name']} role_policy_file must be a string")
        if not isinstance(raw["tool_registry_file"], str) or not raw["tool_registry_file"]:
            raise ContractError(f"pack {raw['name']} tool_registry_file must be a string")
        pipelines = raw["pipelines"]
        if not isinstance(pipelines, list) or not all(isinstance(item, str) and item for item in pipelines):
            raise ContractError(f"pack {raw['name']} pipelines must be strings")
        scenarios = raw.get("scenarios", [])
        static_replays = raw.get("static_replays", [])
        if not isinstance(scenarios, list) or not isinstance(static_replays, list):
            raise ContractError(f"pack {raw['name']} scenarios/static_replays must be lists")
        return cls(
            name=raw["name"],
            role_policy_file=raw["role_policy_file"],
            tool_registry_file=raw["tool_registry_file"],
            pipelines=tuple(pipelines),
            scenarios=tuple(ScenarioSpec.from_dict(item) for item in scenarios),
            static_replays=tuple(StaticReplaySpec.from_dict(item) for item in static_replays),
        )

    def role_policy(self, root: Path) -> RolePolicy:
        return RolePolicy.load(root / self.role_policy_file)

    def tool_registry(self, root: Path) -> ToolRegistry:
        return ToolRegistry.load(root / self.tool_registry_file)


def load_domain_packs(root: Path) -> list[DomainPack]:
    raw = load_json(root / "packs" / "registry.json")
    packs = raw.get("packs")
    if not isinstance(packs, list) or not packs:
        raise ContractError("packs/registry.json must define packs[]")
    loaded = [DomainPack.from_dict(item) for item in packs]
    names = [pack.name for pack in loaded]
    if len(set(names)) != len(names):
        raise ContractError(f"duplicate domain pack names: {names}")
    return loaded
