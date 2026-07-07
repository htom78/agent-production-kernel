from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import sys
import tempfile
import unittest
import copy
import importlib.util
import json
import os
import threading
import time
from pathlib import Path
from unittest import mock

from apkernel import CheckpointStore, ContractError, PipelineManifest, ReplayHarness, Reviewer, RolePolicy, RunEngine, SchemaRegistry, ToolRegistry, artifacts_from_checkpoints, build_real_repo_corpus_report, load_domain_packs, load_json, validate_artifact_semantics
from apkernel.software import (
    build_demo_bug_fix_artifacts,
    build_demo_bug_fix_executor,
    build_demo_feature_executor,
    build_demo_incident_executor,
    build_demo_refactor_executor,
    build_demo_release_executor,
)
from apkernel.research import build_demo_research_brief_executor
from apkernel.design import build_demo_design_review_executor


ROOT = Path(__file__).resolve().parent.parent


class KernelContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schemas = SchemaRegistry(ROOT / "schemas" / "artifacts").load()

    def bug_scenario(self, run_id: str = "unit-demo") -> dict[str, object]:
        return {
            "run_id": run_id,
            "issue_id": "BUG-UNIT",
            "observed_behavior": "Private cache variant leaks.",
            "expected_behavior": "Cache variants are scoped.",
            "reproduction_steps": ["run private request", "run non-private request"],
            "evidence": ["same cache key"],
        }

    def write_bug_stages(
        self,
        store: CheckpointStore,
        manifest: PipelineManifest,
        artifacts: dict[str, dict[str, object]],
        run_id: str,
        stage_names: list[str],
    ) -> None:
        reviewer = Reviewer(self.schemas)
        for stage_name in stage_names:
            produced = {
                artifact_name: artifacts[artifact_name]
                for artifact_name in manifest.get_stage(stage_name).get("produces", [])
            }
            review = reviewer.review(manifest, stage_name, produced)
            self.assertEqual(review.decision, "pass")
            store.write(
                manifest,
                run_id,
                stage_name,
                status="completed",
                artifacts=produced,
                review=review,
            )

    def mark_reports_ready_for_agent_battle(
        self,
        self_report: dict[str, object],
        battle_report: dict[str, object],
    ) -> None:
        self_report["overall_score"] = 96
        self_report["decision"] = "continue"
        self_report["next_actions"] = []
        for dimension in self_report.get("dimensions", []):  # type: ignore[union-attr]
            if isinstance(dimension, dict):
                dimension["score"] = max(float(dimension.get("score", 0)), 95)
        battle_report["overall_score"] = 96
        battle_report["verdict"] = "advance"
        battle_report["next_actions"] = ["Run and preserve an independent multi-agent battle report for APK."]
        for judge in battle_report.get("judges", []):  # type: ignore[union-attr]
            if isinstance(judge, dict):
                judge["score"] = max(float(judge.get("score", 0)), 95)

    def test_schema_rejects_missing_required_field(self) -> None:
        with self.assertRaises(ContractError):
            self.schemas.validate("bug_report", {"version": "1.0"})

    def test_schema_rejects_out_of_range_calibration(self) -> None:
        artifact = build_demo_bug_fix_artifacts(self.bug_scenario())["decision_log"]
        bad = dict(artifact)
        bad["decisions"] = [dict(artifact["decisions"][0], confidence=42)]
        with self.assertRaises(ContractError):
            self.schemas.validate("decision_log", bad)

    def test_schema_rejects_exclusive_minimum_boundary(self) -> None:
        report = build_real_repo_corpus_report(ROOT, self.schemas)
        report["freshness"]["freshness_window_hours"] = 0
        with self.assertRaisesRegex(ContractError, "must be > 0"):
            self.schemas.validate("real_repo_corpus_report", report)

    def test_real_repo_bug_run_fixture_validates(self) -> None:
        fixture = load_json(ROOT / "examples" / "real_repo_bug_run_fixture.json")
        self.schemas.validate("real_repo_bug_run", fixture)
        self.assertEqual(fixture["repo_url"], "https://github.com/htom78/apk-real-bug-demo")
        self.assertEqual(fixture["failing_command"]["status"], "fail")
        self.assertEqual(fixture["passing_command"]["status"], "pass")
        self.assertTrue(fixture["checkpoint_refs"])

    def test_real_repo_bug_run_schema_rejects_false_passing_evidence(self) -> None:
        fixture = copy.deepcopy(load_json(ROOT / "examples" / "real_repo_bug_run_fixture.json"))
        fixture["passing_command"]["status"] = "fail"
        with self.assertRaises(ContractError):
            self.schemas.validate("real_repo_bug_run", fixture)

    def test_real_repo_bug_run_semantics_reject_commit_sha_mismatch(self) -> None:
        fixture = copy.deepcopy(load_json(ROOT / "examples" / "real_repo_bug_run_fixture.json"))
        fixture["passing_command"]["commit_sha"] = fixture["bug_commit"]
        self.schemas.validate("real_repo_bug_run", fixture)
        with self.assertRaisesRegex(ContractError, "passing_command.commit_sha"):
            validate_artifact_semantics("real_repo_bug_run", fixture)

    def test_real_repo_corpus_report_tracks_current_gap(self) -> None:
        report = build_real_repo_corpus_report(ROOT, self.schemas)
        self.schemas.validate("real_repo_corpus_report", report)
        self.assertTrue(report["summary"]["target_met"])
        self.assertEqual(report["summary"]["non_author_repo_count"], 5)
        self.assertEqual(report["summary"]["missing_non_author_repos"], 0)
        self.assertGreaterEqual(report["summary"]["failure_family_count"], 3)
        self.assertEqual(report["freshness"]["producer_roundtrip_status"], "pass")
        self.assertIn(
            report["freshness"]["artifact_source"],
            {"checked_in_manifest_artifacts", "live_external_rerun"},
        )
        self.assertEqual(
            report["freshness"]["external_execution"],
            report["freshness"]["artifact_source"] == "live_external_rerun",
        )
        self.assertEqual(
            report["freshness"]["live_artifact_count"],
            report["freshness"]["fresh_live_artifact_count"] + report["freshness"]["stale_live_artifact_count"],
        )
        self.assertGreater(report["freshness"]["freshness_window_hours"], 0)
        self.assertIn("T", report["freshness"]["latest_command_timestamp"])
        self.assertFalse(report["approval_boundary"]["required"])
        self.assertEqual(report["approval_boundary"]["status"], "not_required")
        self.assertEqual(report["approval_boundary"]["prohibited_without_approval"], [])

    def test_real_repo_corpus_report_prefers_live_artifacts_when_available(self) -> None:
        fixture = load_json(ROOT / "examples" / "real_repo_bug_run_fixture.json")
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            (temp_root / "examples").mkdir()
            (temp_root / ".apk" / "real-bug-runs" / fixture["run_id"]).mkdir(parents=True)
            manifest = {
                "version": "1.0",
                "corpus_id": "unit-live-corpus",
                "target_repo_count": 1,
                "target_failure_family_count": 1,
                "entries": [
                    {
                        "artifact_file": "examples/real_repo_bug_run_fixture.json",
                        "author_owned": False,
                        "failure_family": "state_key_under_specification",
                        "repo_url": fixture["repo_url"],
                        "run_id": fixture["run_id"],
                    }
                ],
            }
            (temp_root / "examples" / "real_repo_bug_run_fixture.json").write_text(
                json.dumps(fixture, indent=2),
                encoding="utf-8",
            )
            (temp_root / ".apk" / "real-bug-runs" / fixture["run_id"] / "real_repo_bug_run.json").write_text(
                json.dumps(fixture, indent=2),
                encoding="utf-8",
            )
            manifest_path = temp_root / "examples" / "real_repo_corpus_manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

            report = build_real_repo_corpus_report(
                temp_root,
                self.schemas,
                manifest_path,
                now=dt.datetime(2026, 6, 30, 10, 0, tzinfo=dt.timezone.utc),
            )
            self.schemas.validate("real_repo_corpus_report", report)
            validate_artifact_semantics("real_repo_corpus_report", report)
            self.assertEqual(report["freshness"]["artifact_source"], "live_external_rerun")
            self.assertTrue(report["freshness"]["external_execution"])
            self.assertEqual(report["freshness"]["live_artifact_count"], 1)
            self.assertEqual(report["freshness"]["fresh_live_artifact_count"], 1)
            self.assertEqual(report["freshness"]["stale_live_artifact_count"], 0)

    def test_real_repo_corpus_report_rejects_stale_live_artifact_as_external_rerun(self) -> None:
        fixture = load_json(ROOT / "examples" / "real_repo_bug_run_fixture.json")
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            (temp_root / "examples").mkdir()
            (temp_root / ".apk" / "real-bug-runs" / fixture["run_id"]).mkdir(parents=True)
            manifest = {
                "version": "1.0",
                "corpus_id": "unit-stale-live-corpus",
                "target_repo_count": 1,
                "target_failure_family_count": 1,
                "entries": [
                    {
                        "artifact_file": "examples/real_repo_bug_run_fixture.json",
                        "author_owned": False,
                        "failure_family": "state_key_under_specification",
                        "repo_url": fixture["repo_url"],
                        "run_id": fixture["run_id"],
                    }
                ],
            }
            (temp_root / "examples" / "real_repo_bug_run_fixture.json").write_text(
                json.dumps(fixture, indent=2),
                encoding="utf-8",
            )
            (temp_root / ".apk" / "real-bug-runs" / fixture["run_id"] / "real_repo_bug_run.json").write_text(
                json.dumps(fixture, indent=2),
                encoding="utf-8",
            )
            manifest_path = temp_root / "examples" / "real_repo_corpus_manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

            report = build_real_repo_corpus_report(
                temp_root,
                self.schemas,
                manifest_path,
                now=dt.datetime(2026, 7, 3, 10, 0, tzinfo=dt.timezone.utc),
            )
            self.schemas.validate("real_repo_corpus_report", report)
            validate_artifact_semantics("real_repo_corpus_report", report)
            self.assertEqual(report["freshness"]["artifact_source"], "checked_in_manifest_artifacts")
            self.assertFalse(report["freshness"]["external_execution"])
            self.assertEqual(report["freshness"]["live_artifact_count"], 1)
            self.assertEqual(report["freshness"]["fresh_live_artifact_count"], 0)
            self.assertEqual(report["freshness"]["stale_live_artifact_count"], 1)

    def test_real_repo_corpus_report_semantics_reject_target_met_mismatch(self) -> None:
        report = build_real_repo_corpus_report(ROOT, self.schemas)
        report["summary"]["target_met"] = False
        self.schemas.validate("real_repo_corpus_report", report)
        with self.assertRaisesRegex(ContractError, "target_met"):
            validate_artifact_semantics("real_repo_corpus_report", report)

    def test_real_repo_corpus_report_semantics_reject_missing_approval_boundary(self) -> None:
        report = build_real_repo_corpus_report(ROOT, self.schemas)
        report["approval_boundary"]["required"] = True
        report["approval_boundary"]["status"] = "awaiting_human"
        self.schemas.validate("real_repo_corpus_report", report)
        with self.assertRaisesRegex(ContractError, "approval_boundary"):
            validate_artifact_semantics("real_repo_corpus_report", report)

    def test_real_repo_corpus_report_semantics_reject_stale_roundtrip(self) -> None:
        report = build_real_repo_corpus_report(ROOT, self.schemas)
        report["freshness"]["producer_roundtrip_status"] = "not_checked"
        self.schemas.validate("real_repo_corpus_report", report)
        with self.assertRaisesRegex(ContractError, "producer_roundtrip_status"):
            validate_artifact_semantics("real_repo_corpus_report", report)

    def test_real_repo_corpus_report_semantics_reject_timestamp_lie(self) -> None:
        report = build_real_repo_corpus_report(ROOT, self.schemas)
        report["freshness"]["latest_command_timestamp"] = report["freshness"]["oldest_command_timestamp"]
        self.schemas.validate("real_repo_corpus_report", report)
        with self.assertRaisesRegex(ContractError, "latest_command_timestamp must match entries"):
            validate_artifact_semantics("real_repo_corpus_report", report)

    def test_real_repo_corpus_report_rejects_duplicate_artifact_and_run_id(self) -> None:
        manifest = load_json(ROOT / "examples" / "real_repo_corpus_manifest.json")
        manifest["entries"] = [manifest["entries"][0], dict(manifest["entries"][0])]
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "duplicate-corpus.json"
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "duplicates artifact_file"):
                build_real_repo_corpus_report(ROOT, self.schemas, manifest_path)

    def test_bugsinpy_corpus_runner_defines_five_distinct_external_repos(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "run_bugsinpy_corpus", ROOT / "scripts" / "run_bugsinpy_corpus.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["run_bugsinpy_corpus"] = module
        spec.loader.exec_module(module)
        projects = {scenario.project for scenario in module.SCENARIOS}
        families = {scenario.failure_family for scenario in module.SCENARIOS}
        self.assertEqual(len(projects), 5)
        self.assertGreaterEqual(len(families), 3)
        for scenario in module.SCENARIOS:
            self.assertTrue(scenario.artifact_file.startswith("examples/real_repo_bug_run_"))

    def test_checkpoint_branch_replay_fixture_validates(self) -> None:
        fixture = load_json(ROOT / "examples" / "checkpoint_branch_replay_fixture.json")
        self.schemas.validate("checkpoint_branch_replay", fixture)
        statuses = {state["status"] for state in fixture["states"]}
        self.assertEqual(statuses, {"awaiting_human", "failed", "blocked"})

    def test_checkpoint_branch_replay_schema_rejects_unknown_state(self) -> None:
        fixture = copy.deepcopy(load_json(ROOT / "examples" / "checkpoint_branch_replay_fixture.json"))
        fixture["states"][0]["status"] = "paused"
        with self.assertRaises(ContractError):
            self.schemas.validate("checkpoint_branch_replay", fixture)

    def test_checkpoint_branch_replay_semantics_reject_checkpoint_status_mismatch(self) -> None:
        fixture = copy.deepcopy(load_json(ROOT / "examples" / "checkpoint_branch_replay_fixture.json"))
        fixture["checkpoints"]["external_dependency"]["status"] = "completed"
        self.schemas.validate("checkpoint_branch_replay", fixture)
        with self.assertRaisesRegex(ContractError, "status must match"):
            validate_artifact_semantics("checkpoint_branch_replay", fixture)

    def test_autonomy_run_report_fixture_validates(self) -> None:
        fixture = load_json(ROOT / "examples" / "autonomy_run_replay_fixture.json")
        report = fixture["autonomy_run_report"]
        self.schemas.validate("autonomy_run_report", report)
        self.assertEqual(report["decision"], "blocked")
        self.assertIn("external_repo", report["boundaries"])

    def test_autonomy_run_report_semantics_reject_false_executed_without_commands(self) -> None:
        fixture = copy.deepcopy(load_json(ROOT / "examples" / "autonomy_run_replay_fixture.json"))
        report = fixture["autonomy_run_report"]
        report["decision"] = "executed"
        self.schemas.validate("autonomy_run_report", report)
        with self.assertRaisesRegex(ContractError, "executed requires passing commands"):
            validate_artifact_semantics("autonomy_run_report", report)

    def test_all_pipeline_outputs_have_schemas(self) -> None:
        for path in sorted((ROOT / "pipelines").glob("*.json")):
            manifest = PipelineManifest.load(path)
            self.assertTrue(manifest.stage_names(), path.name)
            for stage in manifest.stages:
                for artifact_name in stage.get("produces", []):
                    self.assertIn(artifact_name, self.schemas.schemas, f"{path.name}:{stage['name']}")

    def test_stage_tools_declare_required_input_artifacts(self) -> None:
        tools = {}
        for pack in load_domain_packs(ROOT):
            tools.update(pack.tool_registry(ROOT).tools)
        for path in sorted((ROOT / "pipelines").glob("*.json")):
            manifest = PipelineManifest.load(path)
            for stage in manifest.stages:
                required_inputs = set(stage.get("required_artifacts_in", []))
                if not required_inputs:
                    continue
                declared_tool_inputs = set()
                for tool_name in stage.get("tools_available", []):
                    declared_tool_inputs.update(tools[tool_name].input_artifacts)
                self.assertLessEqual(
                    required_inputs,
                    declared_tool_inputs,
                    f"{path.name}:{stage['name']} missing tool inputs {sorted(required_inputs - declared_tool_inputs)}",
                )

    def test_tool_registry_groups_capabilities(self) -> None:
        registry = ToolRegistry.load(ROOT / "packs" / "software" / "tool_registry.json")
        grouped = registry.by_capability()
        self.assertIn("verification", grouped)
        self.assertIn("regression_harness", grouped)
        registry.require_outputs_have_schemas(self.schemas)

    def test_domain_pack_registry_discovers_research_pack(self) -> None:
        packs = load_domain_packs(ROOT)
        by_name = {pack.name: pack for pack in packs}
        self.assertIn("software", by_name)
        self.assertIn("research", by_name)
        self.assertIn("design", by_name)
        self.assertIn("research-brief", by_name["research"].pipelines)
        self.assertEqual(by_name["research"].scenarios[0].pipeline, "research-brief")
        self.assertIn("design-review", by_name["design"].pipelines)
        self.assertEqual(by_name["design"].scenarios[0].pipeline, "design-review")

    def test_self_assessment_report_validates_and_names_next_action(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "self_assess", ROOT / "scripts" / "self_assess.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["self_assess"] = module
        spec.loader.exec_module(module)
        runs = [
            module.CommandRun("python3 scripts/verify.py", "pass", 0, "ok", ""),
            module.CommandRun("python3 -m unittest discover -s tests -v", "pass", 0, "ok", ""),
            module.CommandRun("python3 scripts/replay_regressions.py", "pass", 0, "ok", ""),
            module.CommandRun("python3 -m compileall apkernel scripts tests", "pass", 0, "ok", ""),
        ]
        report = module.build_report(runs, "unit-self-assess")
        self.schemas.validate("self_assessment_report", report)
        self.assertEqual(report["decision"], "continue")
        self.assertGreater(report["overall_score"], 0)
        self.assertIsInstance(report["next_actions"], list)
        if report["next_actions"]:
            self.assertIn(
                report["next_actions"][0]["id"],
                {
                    "run-independent-agent-battle",
                    "address-independent-agent-battle-findings",
                },
            )

    def test_self_assessment_prices_stale_live_corpus_below_perfect_trust(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "self_assess_stale_corpus_pricing", ROOT / "scripts" / "self_assess.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["self_assess_stale_corpus_pricing"] = module
        spec.loader.exec_module(module)

        runs = [
            module.CommandRun("python3 scripts/verify.py", "pass", 0, "ok", ""),
            module.CommandRun("python3 scripts/verify_real_repo_corpus.py", "pass", 0, "ok", ""),
            module.CommandRun("python3 -m unittest discover -s tests -v", "pass", 0, "ok", ""),
            module.CommandRun("python3 scripts/replay_regressions.py", "pass", 0, "ok", ""),
            module.CommandRun("python3 -m compileall apkernel scripts tests", "pass", 0, "ok", ""),
        ]
        report = module.build_report(runs, "unit-stale-corpus-pricing")
        evidence = next(item for item in report["dimensions"] if item["name"] == "evidence_trust")
        self.assertEqual(evidence["score"], 96)
        self.assertIn("stale", evidence["rationale"])

    def test_self_assess_distinguishes_derived_and_independent_battle_evidence(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "self_assess_battle_gate", ROOT / "scripts" / "self_assess.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["self_assess_battle_gate"] = module
        spec.loader.exec_module(module)

        derived = load_json(ROOT / "examples" / "agent_battle_harness_report_fixture.json")
        self.assertFalse(module._is_independent_agent_battle_report(derived))

        independent = copy.deepcopy(derived)
        independent["protocol"]["evidence_mode"] = "independent_agent_reports"
        independent["protocol"]["independent_contexts"] = True
        independent["outcome"]["verdict"] = "advance"
        for audit in independent["judge_audit"]:
            if audit["check"] == "independent agent evidence present":
                audit["status"] = "pass"
        for judge in independent["judges"]:
            judge["source"] = "external_agent_report"
            judge["source_report"] = f"codex-subagent://unit-{judge['role']}"
            judge["input_run_id"] = independent["run_id"]
            judge["verdict"] = "advance"
        self.schemas.validate("agent_battle_harness_report", independent)
        with self.assertRaisesRegex(ContractError, "real file"):
            validate_artifact_semantics("agent_battle_harness_report", independent)
        self.assertFalse(module._is_independent_agent_battle_report(independent))

        with tempfile.TemporaryDirectory() as tmp:
            self_report_path = Path(tmp) / "self_assessment_report.json"
            battle_report_path = Path(tmp) / "battle_report.json"
            self_report_path.write_text(
                json.dumps(
                    {
                        "run_id": independent["input_reports"]["self_assessment_run_id"],
                        "dimensions": [
                            {"name": "contract_integrity", "score": 95},
                            {"name": "execution_reality", "score": 95},
                            {"name": "evidence_trust", "score": 95},
                            {"name": "replay_strength", "score": 95},
                            {"name": "multi_agent_readiness", "score": 95},
                            {"name": "autonomy_loop", "score": 95}
                        ],
                        "next_actions": []
                    }
                ),
                encoding="utf-8",
            )
            battle_report_path.write_text(
                json.dumps(
                    {
                        "run_id": independent["input_reports"]["battle_report_run_id"],
                        "verdict": "hold",
                        "next_actions": ["Run and preserve an independent multi-agent battle report for APK."],
                        "release_disciplines": ["Maintain the verified corpus and rerun gates before release."]
                    }
                ),
                encoding="utf-8",
            )
            independent["input_reports"]["self_assessment_report"] = str(self_report_path)
            independent["input_reports"]["battle_report"] = str(battle_report_path)
            self.schemas.validate("agent_battle_harness_report", independent)
            validate_artifact_semantics("agent_battle_harness_report", independent)
            self.assertTrue(module._is_independent_agent_battle_attempt(independent))
            self.assertTrue(module._is_independent_agent_battle_report(independent))

    def test_self_assess_rejects_stale_independent_battle_evidence(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "self_assess_battle_freshness", ROOT / "scripts" / "self_assess.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["self_assess_battle_freshness"] = module
        spec.loader.exec_module(module)
        sys.modules["self_assess"] = module
        battle_spec = importlib.util.spec_from_file_location(
            "battle_report_freshness", ROOT / "scripts" / "battle_report.py"
        )
        self.assertIsNotNone(battle_spec)
        self.assertIsNotNone(battle_spec.loader)
        battle_module = importlib.util.module_from_spec(battle_spec)
        sys.modules["battle_report_freshness"] = battle_module
        battle_spec.loader.exec_module(battle_module)

        independent = copy.deepcopy(load_json(ROOT / "examples" / "agent_battle_harness_report_fixture.json"))
        independent["protocol"]["evidence_mode"] = "independent_agent_reports"
        independent["protocol"]["independent_contexts"] = True
        independent["outcome"]["verdict"] = "advance"
        for audit in independent["judge_audit"]:
            if audit["check"] == "independent agent evidence present":
                audit["status"] = "pass"
        for judge in independent["judges"]:
            judge["source"] = "external_agent_report"
            judge["source_report"] = f"codex-subagent://unit-fresh-{judge['role']}"
            judge["input_run_id"] = independent["run_id"]
            judge["verdict"] = "advance"

        with tempfile.TemporaryDirectory() as tmp:
            self_report_path = Path(tmp) / "self_assessment_report.json"
            battle_report_path = Path(tmp) / "battle_report.json"
            harness_report_path = Path(tmp) / "agent_battle_harness_report.json"
            runs = [
                module.CommandRun("python3 scripts/verify.py", "pass", 0, "ok", ""),
                module.CommandRun("python3 scripts/verify_real_repo_corpus.py", "pass", 0, "ok", ""),
                module.CommandRun("python3 -m unittest discover -s tests -v", "pass", 0, "ok", ""),
                module.CommandRun("python3 scripts/replay_regressions.py", "pass", 0, "ok", ""),
                module.CommandRun("python3 -m compileall apkernel scripts tests", "pass", 0, "ok", ""),
            ]
            self_report = module.build_report(runs, "unit-fresh-self")
            battle_report = battle_module.build_battle_report(self_report, "unit-fresh-battle")
            self.mark_reports_ready_for_agent_battle(self_report, battle_report)
            self_report_path.write_text(json.dumps(self_report, indent=2), encoding="utf-8")
            battle_report_path.write_text(json.dumps(battle_report, indent=2), encoding="utf-8")
            independent["input_reports"] = {
                "self_assessment_report": str(self_report_path),
                "self_assessment_run_id": self_report["run_id"],
                "battle_report": str(battle_report_path),
                "battle_report_run_id": battle_report["run_id"],
            }
            harness_report_path.write_text(json.dumps(independent, indent=2), encoding="utf-8")
            fresh_mtime = harness_report_path.stat().st_mtime
            os.utime(self_report_path, (fresh_mtime, fresh_mtime))
            os.utime(battle_report_path, (fresh_mtime, fresh_mtime))
            os.utime(harness_report_path, (fresh_mtime + 1, fresh_mtime + 1))

            self.assertTrue(
                module._is_current_independent_agent_battle_report(
                    independent,
                    harness_report_path,
                    current_evidence_mtime=fresh_mtime,
                )
            )
            self_reports_root = ROOT / ".apk" / "self-assessments"
            battle_reports_root = ROOT / ".apk" / "battle-reports"
            self_reports_root.mkdir(parents=True, exist_ok=True)
            battle_reports_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="unit-open-self-", dir=self_reports_root) as newer_self_tmp:
                newer_self = copy.deepcopy(self_report)
                newer_self["run_id"] = "unit-fresh-self-newer-open-cycle"
                newer_self["next_actions"] = [
                    {
                        "id": "run-independent-agent-battle",
                        "title": "Run and preserve an independent multi-agent battle report for APK",
                        "priority": "P0",
                        "rationale": "A newer report for the same evidence still needs independent battle.",
                        "target_files": ["scripts/agent_battle_harness.py"],
                        "verification_commands": ["python3 scripts/agent_battle_harness.py"],
                    }
                ]
                for dimension in newer_self["dimensions"]:
                    if dimension["name"] == "evaluation_independence":
                        dimension["score"] = 72
                newer_self_path = Path(newer_self_tmp) / "self_assessment_report.json"
                newer_self_path.write_text(json.dumps(newer_self, indent=2), encoding="utf-8")
                os.utime(newer_self_path, (fresh_mtime + 30, fresh_mtime + 30))
                self.assertFalse(
                    module._is_current_independent_agent_battle_report(
                        independent,
                        harness_report_path,
                        current_evidence_mtime=fresh_mtime,
                    )
                )
            with tempfile.TemporaryDirectory(prefix="unit-open-battle-", dir=battle_reports_root) as newer_battle_tmp:
                newer_battle = copy.deepcopy(battle_report)
                newer_battle["run_id"] = "unit-fresh-battle-newer-open-cycle"
                newer_battle["verdict"] = "hold"
                newer_battle["next_actions"] = ["Run and preserve an independent multi-agent battle report for APK."]
                newer_battle_path = Path(newer_battle_tmp) / "battle_report.json"
                newer_battle_path.write_text(json.dumps(newer_battle, indent=2), encoding="utf-8")
                os.utime(newer_battle_path, (fresh_mtime + 30, fresh_mtime + 30))
                self.assertFalse(
                    module._is_current_independent_agent_battle_report(
                        independent,
                        harness_report_path,
                        current_evidence_mtime=fresh_mtime,
                    )
                )
            hold = copy.deepcopy(independent)
            hold["outcome"]["verdict"] = "hold"
            hold["outcome"]["score"] = 92
            hold["critic_veto"]["active"] = True
            hold["critic_veto"]["reason"] = "Hold for test."
            hold["judges"][0]["verdict"] = "hold"
            hold["judges"][0]["score"] = 92
            self.schemas.validate("agent_battle_harness_report", hold)
            validate_artifact_semantics("agent_battle_harness_report", hold)
            self.assertTrue(
                module._is_current_independent_agent_battle_report(
                    hold,
                    harness_report_path,
                    current_evidence_mtime=fresh_mtime,
                    require_advance=False,
                )
            )
            self.assertFalse(
                module._is_current_independent_agent_battle_report(
                    hold,
                    harness_report_path,
                    current_evidence_mtime=fresh_mtime,
                )
            )
            with tempfile.TemporaryDirectory(prefix="unit-address-finding-self-", dir=self_reports_root) as newer_self_tmp:
                newer_self = copy.deepcopy(self_report)
                newer_self["run_id"] = "unit-fresh-self-newer-address-findings"
                newer_self["next_actions"] = [
                    {
                        "id": "address-independent-agent-battle-findings",
                        "title": "Address the current independent Agent Battle hold findings",
                        "priority": "P0",
                        "rationale": "The independent battle already ran and held; repair its findings.",
                        "target_files": ["scripts/agent_battle_harness.py"],
                        "verification_commands": ["python3 scripts/agent_battle_harness.py"],
                    }
                ]
                for dimension in newer_self["dimensions"]:
                    if dimension["name"] == "evaluation_independence":
                        dimension["score"] = 72
                newer_self_path = Path(newer_self_tmp) / "self_assessment_report.json"
                newer_self_path.write_text(json.dumps(newer_self, indent=2), encoding="utf-8")
                os.utime(newer_self_path, (fresh_mtime + 30, fresh_mtime + 30))
                self.assertTrue(
                    module._is_current_independent_agent_battle_report(
                        hold,
                        harness_report_path,
                        current_evidence_mtime=fresh_mtime,
                        require_advance=False,
                    )
                )
                self.assertFalse(
                    module._is_current_independent_agent_battle_report(
                        independent,
                        harness_report_path,
                        current_evidence_mtime=fresh_mtime,
                    )
                )
            independent["input_reports"]["self_assessment_run_id"] = "stale-self-run"
            self.assertFalse(
                module._is_current_independent_agent_battle_report(
                    independent,
                    harness_report_path,
                    current_evidence_mtime=fresh_mtime,
                )
            )
            independent["input_reports"]["self_assessment_run_id"] = self_report["run_id"]
            independent["input_reports"]["battle_report_run_id"] = "stale-battle-run"
            self.assertFalse(
                module._is_current_independent_agent_battle_report(
                    independent,
                    harness_report_path,
                    current_evidence_mtime=fresh_mtime,
                )
            )
            independent["input_reports"]["battle_report_run_id"] = battle_report["run_id"]
            self.assertFalse(
                module._is_current_independent_agent_battle_report(
                    independent,
                    harness_report_path,
                    current_evidence_mtime=fresh_mtime + 0.5,
                )
            )
            self.assertFalse(
                module._is_current_independent_agent_battle_report(
                    independent,
                    harness_report_path,
                    current_evidence_mtime=fresh_mtime + 10,
                )
            )
            os.utime(self_report_path, (fresh_mtime + 20, fresh_mtime + 20))
            self.assertFalse(
                module._is_current_independent_agent_battle_report(
                    independent,
                    harness_report_path,
                    current_evidence_mtime=fresh_mtime,
                )
            )
            self_report_path.write_text("{}", encoding="utf-8")
            os.utime(self_report_path, (fresh_mtime - 20, fresh_mtime - 20))
            self.assertFalse(
                module._is_current_independent_agent_battle_report(
                    independent,
                    harness_report_path,
                    current_evidence_mtime=fresh_mtime,
                )
            )

    def test_self_assess_rejects_newer_same_fingerprint_open_battle_cycle(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "self_assess_same_fingerprint_freshness", ROOT / "scripts" / "self_assess.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["self_assess_same_fingerprint_freshness"] = module
        spec.loader.exec_module(module)
        sys.modules["self_assess"] = module
        battle_spec = importlib.util.spec_from_file_location(
            "battle_report_same_fingerprint_freshness", ROOT / "scripts" / "battle_report.py"
        )
        self.assertIsNotNone(battle_spec)
        self.assertIsNotNone(battle_spec.loader)
        battle_module = importlib.util.module_from_spec(battle_spec)
        sys.modules["battle_report_same_fingerprint_freshness"] = battle_module
        battle_spec.loader.exec_module(battle_module)

        independent = copy.deepcopy(load_json(ROOT / "examples" / "agent_battle_harness_report_fixture.json"))
        independent["protocol"]["evidence_mode"] = "independent_agent_reports"
        independent["protocol"]["independent_contexts"] = True
        independent["outcome"]["verdict"] = "advance"
        for audit in independent["judge_audit"]:
            if audit["check"] == "independent agent evidence present":
                audit["status"] = "pass"
        for judge in independent["judges"]:
            judge["source"] = "external_agent_report"
            judge["source_report"] = f"codex-subagent://unit-same-fingerprint-{judge['role']}"
            judge["input_run_id"] = independent["run_id"]
            judge["verdict"] = "advance"

        with tempfile.TemporaryDirectory() as tmp:
            self_report_path = Path(tmp) / "self_assessment_report.json"
            battle_report_path = Path(tmp) / "battle_report.json"
            harness_report_path = Path(tmp) / "agent_battle_harness_report.json"
            runs = [
                module.CommandRun("python3 scripts/verify.py", "pass", 0, "ok", ""),
                module.CommandRun("python3 scripts/verify_real_repo_corpus.py", "pass", 0, "ok", ""),
                module.CommandRun("python3 -m unittest discover -s tests -v", "pass", 0, "ok", ""),
                module.CommandRun("python3 scripts/replay_regressions.py", "pass", 0, "ok", ""),
                module.CommandRun("python3 -m compileall apkernel scripts tests", "pass", 0, "ok", ""),
            ]
            self_report = module.build_report(runs, "unit-same-fingerprint-self")
            battle_report = battle_module.build_battle_report(self_report, "unit-same-fingerprint-battle")
            self.mark_reports_ready_for_agent_battle(self_report, battle_report)
            self_report_path.write_text(json.dumps(self_report, indent=2), encoding="utf-8")
            battle_report_path.write_text(json.dumps(battle_report, indent=2), encoding="utf-8")
            independent["input_reports"] = {
                "self_assessment_report": str(self_report_path),
                "self_assessment_run_id": self_report["run_id"],
                "battle_report": str(battle_report_path),
                "battle_report_run_id": battle_report["run_id"],
            }
            harness_report_path.write_text(json.dumps(independent, indent=2), encoding="utf-8")
            fresh_mtime = harness_report_path.stat().st_mtime
            os.utime(self_report_path, (fresh_mtime, fresh_mtime))
            os.utime(battle_report_path, (fresh_mtime, fresh_mtime))
            os.utime(harness_report_path, (fresh_mtime + 1, fresh_mtime + 1))
            self.assertTrue(
                module._is_current_independent_agent_battle_report(
                    independent,
                    harness_report_path,
                    current_evidence_mtime=fresh_mtime,
                )
            )

            self_reports_root = ROOT / ".apk" / "self-assessments"
            battle_reports_root = ROOT / ".apk" / "battle-reports"
            self_reports_root.mkdir(parents=True, exist_ok=True)
            battle_reports_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="unit-same-fingerprint-self-", dir=self_reports_root) as newer_self_tmp:
                newer_self = copy.deepcopy(self_report)
                newer_self["run_id"] = "unit-same-fingerprint-newer-open-self"
                newer_self["next_actions"] = [
                    {
                        "id": "run-independent-agent-battle",
                        "title": "Run and preserve an independent multi-agent battle report for APK",
                        "priority": "P0",
                        "rationale": "Same evidence fingerprint still has an unresolved battle cycle.",
                        "target_files": ["scripts/agent_battle_harness.py"],
                        "verification_commands": ["python3 scripts/agent_battle_harness.py"],
                    }
                ]
                for dimension in newer_self["dimensions"]:
                    if dimension["name"] == "evaluation_independence":
                        dimension["score"] = 72
                newer_self_path = Path(newer_self_tmp) / "self_assessment_report.json"
                newer_self_path.write_text(json.dumps(newer_self, indent=2), encoding="utf-8")
                os.utime(newer_self_path, (fresh_mtime + 30, fresh_mtime + 30))
                self.assertFalse(
                    module._is_current_independent_agent_battle_report(
                        independent,
                        harness_report_path,
                        current_evidence_mtime=fresh_mtime,
                    )
                )
            with tempfile.TemporaryDirectory(prefix="unit-same-fingerprint-battle-", dir=battle_reports_root) as newer_battle_tmp:
                newer_battle = copy.deepcopy(battle_report)
                newer_battle["run_id"] = "unit-same-fingerprint-newer-open-battle"
                newer_battle["verdict"] = "hold"
                newer_battle["next_actions"] = ["Run and preserve an independent multi-agent battle report for APK."]
                newer_battle_path = Path(newer_battle_tmp) / "battle_report.json"
                newer_battle_path.write_text(json.dumps(newer_battle, indent=2), encoding="utf-8")
                os.utime(newer_battle_path, (fresh_mtime + 30, fresh_mtime + 30))
                self.assertFalse(
                    module._is_current_independent_agent_battle_report(
                        independent,
                        harness_report_path,
                        current_evidence_mtime=fresh_mtime,
                    )
                )

    def test_battle_report_validates_and_preserves_judge_disagreement(self) -> None:
        self_spec = importlib.util.spec_from_file_location(
            "self_assess", ROOT / "scripts" / "self_assess.py"
        )
        self.assertIsNotNone(self_spec)
        self.assertIsNotNone(self_spec.loader)
        self_module = importlib.util.module_from_spec(self_spec)
        sys.modules["self_assess"] = self_module
        self_spec.loader.exec_module(self_module)

        battle_spec = importlib.util.spec_from_file_location(
            "battle_report", ROOT / "scripts" / "battle_report.py"
        )
        self.assertIsNotNone(battle_spec)
        self.assertIsNotNone(battle_spec.loader)
        battle_module = importlib.util.module_from_spec(battle_spec)
        sys.modules["battle_report"] = battle_module
        battle_spec.loader.exec_module(battle_module)

        runs = [
            self_module.CommandRun("python3 scripts/verify.py", "pass", 0, "ok", ""),
            self_module.CommandRun("python3 -m unittest discover -s tests -v", "pass", 0, "ok", ""),
            self_module.CommandRun("python3 scripts/replay_regressions.py", "pass", 0, "ok", ""),
            self_module.CommandRun("python3 -m compileall apkernel scripts tests", "pass", 0, "ok", ""),
        ]
        self_report = self_module.build_report(runs, "unit-battle")
        battle_report = battle_module.build_battle_report(self_report, "unit-battle")
        self.schemas.validate("battle_report", battle_report)
        self.assertIn(
            "Maintain the verified corpus and rerun gates before release.",
            battle_report["release_disciplines"],
        )
        if battle_report["next_actions"]:
            self.assertIn(
                battle_report["next_actions"][0],
                {
                    "Run and preserve an independent multi-agent battle report for APK.",
                    "Address the current independent Agent Battle hold findings.",
                    "Add branch-level replay for awaiting_human, failed, and blocked states.",
                    "Run one real bug-fix scenario through the kernel and preserve the artifacts.",
                    "Add one non-software domain pack to prove pack generality.",
                    "Add schema-valid semantic false-green tests.",
                    "Add a bounded safe-action runner for self-assessed next_actions.",
                    "Expand to five non-author real repository bug scenarios after human approval.",
                },
            )
            self.assertEqual(battle_report["verdict"], "hold")
        self.assertEqual(
            {judge["role"] for judge in battle_report["judges"]},
            {"architect", "test_engineer", "code_reviewer", "critic"},
        )
        self.assertTrue(battle_report["dissent"])

    def test_battle_report_uses_release_disciplines_for_non_blocking_maintenance(self) -> None:
        battle_spec = importlib.util.spec_from_file_location(
            "battle_report_release_disciplines", ROOT / "scripts" / "battle_report.py"
        )
        self.assertIsNotNone(battle_spec)
        self.assertIsNotNone(battle_spec.loader)
        battle_module = importlib.util.module_from_spec(battle_spec)
        sys.modules["battle_report_release_disciplines"] = battle_module
        battle_spec.loader.exec_module(battle_module)

        self_report = {
            "run_id": "unit-release-disciplines",
            "evidence_fingerprint": "sha256:unit-release-disciplines",
            "dimensions": [
                {"name": "contract_integrity", "score": 96},
                {"name": "execution_reality", "score": 95},
                {"name": "evidence_trust", "score": 96},
                {"name": "replay_strength", "score": 96},
                {"name": "multi_agent_readiness", "score": 96},
                {"name": "autonomy_loop", "score": 96},
                {"name": "evaluation_independence", "score": 96},
            ],
        }
        with (
            mock.patch.object(battle_module.self_assess, "_has_real_bug_run_evidence", return_value=True),
            mock.patch.object(battle_module.self_assess, "_has_branch_level_replay", return_value=True),
            mock.patch.object(battle_module.self_assess, "_has_bounded_autonomy_runner", return_value=True),
            mock.patch.object(battle_module.self_assess, "_real_repo_corpus_count", return_value=5),
            mock.patch.object(
                battle_module.self_assess,
                "_real_repo_corpus_stats",
                return_value={"failure_family_count": 6},
            ),
            mock.patch.object(battle_module.self_assess, "_has_second_domain_pack", return_value=True),
            mock.patch.object(battle_module.self_assess, "_has_semantic_fake_green_tests", return_value=True),
            mock.patch.object(battle_module.self_assess, "_has_independent_agent_battle_evidence", return_value=True),
            mock.patch.object(battle_module.self_assess, "_has_current_independent_agent_battle_attempt", return_value=True),
        ):
            battle_report = battle_module.build_battle_report(self_report, "unit-release-disciplines")

        self.schemas.validate("battle_report", battle_report)
        self.assertEqual(battle_report["verdict"], "advance")
        self.assertEqual(battle_report["next_actions"], [])
        self.assertEqual(
            battle_report["release_disciplines"],
            ["Maintain the verified corpus and rerun gates before release."],
        )

    def test_self_assess_evaluation_lock_serializes_gate_runs(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "self_assess_lock", ROOT / "scripts" / "self_assess.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["self_assess_lock"] = module
        spec.loader.exec_module(module)

        if module.fcntl is None:
            self.skipTest("fcntl is not available on this platform")

        order: list[str] = []
        first_entered = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            module.EVALUATION_LOCK_PATH = Path(tmp) / "evaluation.lock"

            def first() -> None:
                with module._evaluation_lock():
                    order.append("first-enter")
                    first_entered.set()
                    time.sleep(0.1)
                    order.append("first-exit")

            def second() -> None:
                first_entered.wait(timeout=1)
                with module._evaluation_lock():
                    order.append("second-enter")

            first_thread = threading.Thread(target=first)
            second_thread = threading.Thread(target=second)
            first_thread.start()
            second_thread.start()
            first_thread.join(timeout=1)
            second_thread.join(timeout=1)

        self.assertEqual(order, ["first-enter", "first-exit", "second-enter"])

    def test_agent_battle_harness_fixture_validates_protocol(self) -> None:
        fixture = load_json(ROOT / "examples" / "agent_battle_harness_report_fixture.json")
        self.schemas.validate("agent_battle_harness_report", fixture)
        validate_artifact_semantics("agent_battle_harness_report", fixture)
        self.assertIn("validation", fixture["subject"].lower())
        self.assertEqual(fixture["protocol"]["evidence_mode"], "derived_report")
        self.assertFalse(fixture["protocol"]["independent_contexts"])
        self.assertTrue(fixture["protocol"]["blind_review"])
        self.assertFalse(fixture["critic_veto"]["active"])
        self.assertEqual(fixture["outcome"]["verdict"], "hold")
        self.assertEqual(
            fixture["outcome"]["release_disciplines"],
            ["Maintain the verified corpus and rerun gates before release."],
        )

    def test_agent_battle_harness_semantics_require_release_disciplines(self) -> None:
        fixture = copy.deepcopy(load_json(ROOT / "examples" / "agent_battle_harness_report_fixture.json"))
        fixture["outcome"]["release_disciplines"] = ["Rerun some gates eventually."]
        self.schemas.validate("agent_battle_harness_report", fixture)
        with self.assertRaisesRegex(ContractError, "release_disciplines"):
            validate_artifact_semantics("agent_battle_harness_report", fixture)

    def test_agent_battle_harness_semantics_reject_peer_score_leak(self) -> None:
        fixture = copy.deepcopy(load_json(ROOT / "examples" / "agent_battle_harness_report_fixture.json"))
        fixture["judges"][0]["peer_scores_visible"] = True
        self.schemas.validate("agent_battle_harness_report", fixture)
        with self.assertRaisesRegex(ContractError, "peer_scores_visible"):
            validate_artifact_semantics("agent_battle_harness_report", fixture)

    def test_agent_battle_harness_semantics_reject_active_veto_advance(self) -> None:
        fixture = copy.deepcopy(load_json(ROOT / "examples" / "agent_battle_harness_report_fixture.json"))
        fixture["critic_veto"]["active"] = True
        fixture["critic_veto"]["reason"] = "critic veto for test"
        fixture["outcome"]["verdict"] = "advance"
        for judge in fixture["judges"]:
            if judge["role"] == "critic":
                judge["veto_vote"]["active"] = True
                judge["veto_vote"]["reason"] = "critic veto for test"
        self.schemas.validate("agent_battle_harness_report", fixture)
        with self.assertRaisesRegex(ContractError, "cannot advance"):
            validate_artifact_semantics("agent_battle_harness_report", fixture)

    def test_agent_battle_harness_semantics_reject_derived_advance(self) -> None:
        fixture = copy.deepcopy(load_json(ROOT / "examples" / "agent_battle_harness_report_fixture.json"))
        fixture["outcome"]["verdict"] = "advance"
        self.schemas.validate("agent_battle_harness_report", fixture)
        with self.assertRaisesRegex(ContractError, "derived_report"):
            validate_artifact_semantics("agent_battle_harness_report", fixture)

    def test_agent_battle_harness_script_builds_auditable_report(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "agent_battle_harness", ROOT / "scripts" / "agent_battle_harness.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent_battle_harness"] = module
        spec.loader.exec_module(module)
        runs = [
            module.self_assess.CommandRun("python3 scripts/verify.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 scripts/verify_real_repo_corpus.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 -m unittest discover -s tests -v", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 scripts/replay_regressions.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 -m compileall apkernel scripts tests", "pass", 0, "ok", ""),
        ]
        self_report = module.self_assess.build_report(runs, "unit-agent-battle")
        battle = module.battle_report.build_battle_report(self_report, "unit-agent-battle")
        report = module.build_agent_battle_harness_report(self_report, battle, "unit-agent-battle")
        self.schemas.validate("agent_battle_harness_report", report)
        validate_artifact_semantics("agent_battle_harness_report", report)
        self.assertEqual({judge["role"] for judge in report["judges"]}, set(module.REQUIRED_JUDGES))
        self.assertEqual(report["protocol"]["evidence_mode"], "derived_report")
        self.assertEqual(report["outcome"]["verdict"], "hold")

    def test_agent_battle_harness_accepts_external_judge_reports(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "agent_battle_harness", ROOT / "scripts" / "agent_battle_harness.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent_battle_harness_external"] = module
        spec.loader.exec_module(module)
        runs = [
            module.self_assess.CommandRun("python3 scripts/verify.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 scripts/verify_real_repo_corpus.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 -m unittest discover -s tests -v", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 scripts/replay_regressions.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 -m compileall apkernel scripts tests", "pass", 0, "ok", ""),
        ]
        self_report = module.self_assess.build_report(runs, "unit-agent-battle-external")
        battle = module.battle_report.build_battle_report(self_report, "unit-agent-battle-external")
        self.mark_reports_ready_for_agent_battle(self_report, battle)
        judge_reports = [
            {
                "version": "1.0",
                "run_id": "unit-agent-battle-external",
                "role": role,
                "score": 96,
                "verdict": "advance",
                "stance": f"{role} external report",
                "findings": ["Independent agent reviewed current code and gates."],
                "context_refs": ["apkernel/core.py"],
                "source_report": f"codex-subagent://unit-{role}",
                "veto_active": False,
                "veto_reason": "No veto.",
            }
            for role in module.REQUIRED_JUDGES
        ]
        for judge_report in judge_reports:
            self.schemas.validate("agent_judge_report", judge_report)
            validate_artifact_semantics("agent_judge_report", judge_report)
        with tempfile.TemporaryDirectory() as tmp:
            self_report_path = Path(tmp) / "self_assessment_report.json"
            battle_report_path = Path(tmp) / "battle_report.json"
            self_report_path.write_text(json.dumps(self_report, indent=2), encoding="utf-8")
            battle_report_path.write_text(json.dumps(battle, indent=2), encoding="utf-8")
            report = module.build_agent_battle_harness_report(
                self_report,
                battle,
                "unit-agent-battle-external",
                input_reports={
                    "self_assessment_report": str(self_report_path),
                    "self_assessment_run_id": self_report["run_id"],
                    "battle_report": str(battle_report_path),
                    "battle_report_run_id": battle["run_id"],
                },
                judge_reports=judge_reports,
            )
            self.schemas.validate("agent_battle_harness_report", report)
            validate_artifact_semantics("agent_battle_harness_report", report)
            self.assertEqual(report["protocol"]["evidence_mode"], "independent_agent_reports")
            self.assertTrue(report["protocol"]["independent_contexts"])
            self.assertEqual(report["outcome"]["verdict"], "advance")

            stale = copy.deepcopy(report)
            stale["input_reports"]["self_assessment_run_id"] = "stale-self-run"
            self.schemas.validate("agent_battle_harness_report", stale)
            with self.assertRaisesRegex(ContractError, "self_assessment_run_id"):
                validate_artifact_semantics("agent_battle_harness_report", stale)

            with tempfile.TemporaryDirectory(dir=ROOT) as repo_tmp, tempfile.TemporaryDirectory() as other_cwd:
                relative_self_path = Path(repo_tmp) / "self_assessment_report.json"
                relative_battle_path = Path(repo_tmp) / "battle_report.json"
                relative_self_path.write_text(json.dumps(self_report, indent=2), encoding="utf-8")
                relative_battle_path.write_text(json.dumps(battle, indent=2), encoding="utf-8")
                relative_report = module.build_agent_battle_harness_report(
                    self_report,
                    battle,
                    "unit-agent-battle-external",
                    input_reports={
                        "self_assessment_report": str(relative_self_path.relative_to(ROOT)),
                        "self_assessment_run_id": self_report["run_id"],
                        "battle_report": str(relative_battle_path.relative_to(ROOT)),
                        "battle_report_run_id": battle["run_id"],
                    },
                    judge_reports=judge_reports,
                )
                original_cwd = Path.cwd()
                try:
                    os.chdir(other_cwd)
                    self.schemas.validate("agent_battle_harness_report", relative_report)
                    validate_artifact_semantics("agent_battle_harness_report", relative_report)
                finally:
                    os.chdir(original_cwd)

    def test_agent_battle_harness_allows_prebattle_ready_inputs_below_full_score(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "agent_battle_harness_prebattle", ROOT / "scripts" / "agent_battle_harness.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent_battle_harness_prebattle"] = module
        spec.loader.exec_module(module)
        run_id = "unit-agent-battle-prebattle-ready"
        runs = [
            module.self_assess.CommandRun("python3 scripts/verify.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 scripts/verify_real_repo_corpus.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 -m unittest discover -s tests -v", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 scripts/replay_regressions.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 -m compileall apkernel scripts tests", "pass", 0, "ok", ""),
        ]
        self_report = module.self_assess.build_report(runs, run_id)
        battle = module.battle_report.build_battle_report(self_report, run_id)
        for dimension in self_report["dimensions"]:
            if dimension["name"] == "evaluation_independence":
                dimension["score"] = 84
            else:
                dimension["score"] = 96
        self_report["overall_score"] = round(
            sum(dimension["score"] for dimension in self_report["dimensions"])
            / len(self_report["dimensions"]),
            1,
        )
        self_report["next_actions"] = [
            {
                "id": "run-independent-agent-battle",
                "title": "Run and preserve an independent multi-agent battle report for APK",
                "priority": "P0",
                "rationale": "All pre-battle readiness dimensions are satisfied; independent evaluation is the remaining gate.",
                "target_files": ["scripts/agent_battle_harness.py"],
                "verification_commands": ["python3 scripts/agent_battle_harness.py"],
            }
        ]
        battle["overall_score"] = 94
        battle["verdict"] = "hold"
        battle["next_actions"] = ["Run and preserve an independent multi-agent battle report for APK."]
        judge_reports = [
            {
                "version": "1.0",
                "run_id": run_id,
                "role": role,
                "score": 96,
                "verdict": "advance",
                "stance": f"{role} external report",
                "findings": ["Independent agent confirmed pre-battle readiness and advanced the evaluation gate."],
                "context_refs": ["apkernel/core.py"],
                "source_report": f"codex-subagent://unit-prebattle-{role}",
                "veto_active": False,
                "veto_reason": "No veto.",
            }
            for role in module.REQUIRED_JUDGES
        ]
        with tempfile.TemporaryDirectory() as tmp:
            self_report_path = Path(tmp) / "self_assessment_report.json"
            battle_report_path = Path(tmp) / "battle_report.json"
            self_report_path.write_text(json.dumps(self_report, indent=2), encoding="utf-8")
            battle_report_path.write_text(json.dumps(battle, indent=2), encoding="utf-8")
            report = module.build_agent_battle_harness_report(
                self_report,
                battle,
                run_id,
                input_reports={
                    "self_assessment_report": str(self_report_path),
                    "self_assessment_run_id": self_report["run_id"],
                    "battle_report": str(battle_report_path),
                    "battle_report_run_id": battle["run_id"],
                },
                judge_reports=judge_reports,
            )
            self.assertLess(self_report["overall_score"], 95)
            self.schemas.validate("agent_battle_harness_report", report)
            validate_artifact_semantics("agent_battle_harness_report", report)
            self.assertEqual(report["outcome"]["verdict"], "advance")
            readiness_audit = [
                item for item in report["judge_audit"]
                if item["check"] == "input reports meet advance readiness gate"
            ]
            self.assertEqual(readiness_audit[0]["status"], "pass")

    def test_agent_battle_harness_rejects_advance_when_input_reports_below_ready(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "agent_battle_harness_input_readiness", ROOT / "scripts" / "agent_battle_harness.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent_battle_harness_input_readiness"] = module
        spec.loader.exec_module(module)
        run_id = "unit-agent-battle-input-readiness"
        runs = [
            module.self_assess.CommandRun("python3 scripts/verify.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 scripts/verify_real_repo_corpus.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 -m unittest discover -s tests -v", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 scripts/replay_regressions.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 -m compileall apkernel scripts tests", "pass", 0, "ok", ""),
        ]
        self_report = module.self_assess.build_report(runs, run_id)
        battle = module.battle_report.build_battle_report(self_report, run_id)
        for dimension in self_report["dimensions"]:
            if dimension["name"] == "contract_integrity":
                dimension["score"] = 90
        self_report["overall_score"] = 90
        judge_reports = [
            {
                "version": "1.0",
                "run_id": run_id,
                "role": role,
                "score": 96,
                "verdict": "advance",
                "stance": f"{role} external report",
                "findings": ["Independent agent reviewed current code and gates."],
                "context_refs": ["apkernel/core.py"],
                "source_report": f"codex-subagent://unit-input-readiness-{role}",
                "veto_active": False,
                "veto_reason": "No veto.",
            }
            for role in module.REQUIRED_JUDGES
        ]
        with tempfile.TemporaryDirectory() as tmp:
            self_report_path = Path(tmp) / "self_assessment_report.json"
            battle_report_path = Path(tmp) / "battle_report.json"
            self_report_path.write_text(json.dumps(self_report, indent=2), encoding="utf-8")
            battle_report_path.write_text(json.dumps(battle, indent=2), encoding="utf-8")
            report = module.build_agent_battle_harness_report(
                self_report,
                battle,
                run_id,
                input_reports={
                    "self_assessment_report": str(self_report_path),
                    "self_assessment_run_id": self_report["run_id"],
                    "battle_report": str(battle_report_path),
                    "battle_report_run_id": battle["run_id"],
                },
                judge_reports=judge_reports,
            )
            self.schemas.validate("agent_battle_harness_report", report)
            validate_artifact_semantics("agent_battle_harness_report", report)
            self.assertEqual(report["outcome"]["verdict"], "hold")
            readiness_audit = [
                item for item in report["judge_audit"]
                if item["check"] == "input reports meet advance readiness gate"
            ]
            self.assertEqual(readiness_audit[0]["status"], "fail")

            tampered = copy.deepcopy(report)
            tampered["outcome"]["verdict"] = "advance"
            self.schemas.validate("agent_battle_harness_report", tampered)
            with self.assertRaisesRegex(ContractError, "contract_integrity"):
                validate_artifact_semantics("agent_battle_harness_report", tampered)

    def test_agent_battle_harness_allows_empty_next_actions_with_release_disciplines(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "agent_battle_harness_maintenance_next_action", ROOT / "scripts" / "agent_battle_harness.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent_battle_harness_maintenance_next_action"] = module
        spec.loader.exec_module(module)
        run_id = "unit-agent-battle-maintenance-action"
        runs = [
            module.self_assess.CommandRun("python3 scripts/verify.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 scripts/verify_real_repo_corpus.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 -m unittest discover -s tests -v", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 scripts/replay_regressions.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 -m compileall apkernel scripts tests", "pass", 0, "ok", ""),
        ]
        self_report = module.self_assess.build_report(runs, run_id)
        battle = module.battle_report.build_battle_report(self_report, run_id)
        self.mark_reports_ready_for_agent_battle(self_report, battle)
        battle["next_actions"] = []
        battle["release_disciplines"] = ["Maintain the verified corpus and rerun gates before release."]
        judge_reports = [
            {
                "version": "1.0",
                "run_id": run_id,
                "role": role,
                "score": 96,
                "verdict": "advance",
                "stance": f"{role} external report",
                "findings": ["Independent agent reviewed current code and gates."],
                "context_refs": ["apkernel/core.py"],
                "source_report": f"codex-subagent://unit-maintenance-action-{role}",
                "veto_active": False,
                "veto_reason": "No veto.",
            }
            for role in module.REQUIRED_JUDGES
        ]
        with tempfile.TemporaryDirectory() as tmp:
            self_report_path = Path(tmp) / "self_assessment_report.json"
            battle_report_path = Path(tmp) / "battle_report.json"
            self_report_path.write_text(json.dumps(self_report, indent=2), encoding="utf-8")
            battle_report_path.write_text(json.dumps(battle, indent=2), encoding="utf-8")
            report = module.build_agent_battle_harness_report(
                self_report,
                battle,
                run_id,
                input_reports={
                    "self_assessment_report": str(self_report_path),
                    "self_assessment_run_id": self_report["run_id"],
                    "battle_report": str(battle_report_path),
                    "battle_report_run_id": battle["run_id"],
                },
                judge_reports=judge_reports,
            )
            self.schemas.validate("agent_battle_harness_report", report)
            validate_artifact_semantics("agent_battle_harness_report", report)
            self.assertEqual(report["outcome"]["verdict"], "advance")
            readiness_audit = [
                item for item in report["judge_audit"]
                if item["check"] == "input reports meet advance readiness gate"
            ]
            self.assertEqual(readiness_audit[0]["status"], "pass")

            battle["next_actions"] = ["Skip release discipline and advance anyway."]
            battle_report_path.write_text(json.dumps(battle, indent=2), encoding="utf-8")
            blocked_report = module.build_agent_battle_harness_report(
                self_report,
                battle,
                run_id,
                input_reports={
                    "self_assessment_report": str(self_report_path),
                    "self_assessment_run_id": self_report["run_id"],
                    "battle_report": str(battle_report_path),
                    "battle_report_run_id": battle["run_id"],
                },
                judge_reports=judge_reports,
            )
            self.schemas.validate("agent_battle_harness_report", blocked_report)
            validate_artifact_semantics("agent_battle_harness_report", blocked_report)
            self.assertEqual(blocked_report["outcome"]["verdict"], "hold")

            tampered = copy.deepcopy(blocked_report)
            tampered["outcome"]["verdict"] = "advance"
            self.schemas.validate("agent_battle_harness_report", tampered)
            with self.assertRaisesRegex(ContractError, "non-battle next_actions"):
                validate_artifact_semantics("agent_battle_harness_report", tampered)

    def test_agent_judge_report_rejects_low_score_advance(self) -> None:
        judge_report = {
            "version": "1.0",
            "run_id": "unit-agent-battle-low-score",
            "role": "critic",
            "score": 10,
            "verdict": "advance",
            "stance": "Low score cannot advance.",
            "findings": ["The report is intentionally inconsistent."],
            "context_refs": ["apkernel/core.py"],
            "source_report": "codex-subagent://unit-low-score",
            "veto_active": False,
            "veto_reason": "No veto.",
        }
        self.schemas.validate("agent_judge_report", judge_report)
        with self.assertRaisesRegex(ContractError, "at least 95"):
            validate_artifact_semantics("agent_judge_report", judge_report)

    def test_agent_battle_harness_rejects_duplicate_external_judge_roles(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "agent_battle_harness_duplicate", ROOT / "scripts" / "agent_battle_harness.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent_battle_harness_duplicate"] = module
        spec.loader.exec_module(module)
        runs = [
            module.self_assess.CommandRun("python3 scripts/verify.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 scripts/verify_real_repo_corpus.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 -m unittest discover -s tests -v", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 scripts/replay_regressions.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 -m compileall apkernel scripts tests", "pass", 0, "ok", ""),
        ]
        run_id = "unit-agent-battle-duplicate"
        self_report = module.self_assess.build_report(runs, run_id)
        battle = module.battle_report.build_battle_report(self_report, run_id)
        judge_reports = [
            {
                "version": "1.0",
                "run_id": run_id,
                "role": role,
                "score": 96,
                "verdict": "advance",
                "stance": f"{role} external report",
                "findings": ["Independent agent reviewed current code and gates."],
                "context_refs": ["apkernel/core.py"],
                "source_report": f"codex-subagent://unit-duplicate-{role}",
                "veto_active": False,
                "veto_reason": "No veto.",
            }
            for role in module.REQUIRED_JUDGES
        ]
        judge_reports.append(
            {
                "version": "1.0",
                "run_id": run_id,
                "role": "architect",
                "score": 100,
                "verdict": "advance",
                "stance": "Duplicate high-score architect report",
                "findings": ["This duplicate must not be allowed to stuff the score."],
                "context_refs": ["apkernel/core.py"],
                "source_report": "codex-subagent://unit-duplicate-architect-extra",
                "veto_active": False,
                "veto_reason": "No veto.",
            }
        )
        report = module.build_agent_battle_harness_report(
            self_report,
            battle,
            run_id,
            judge_reports=judge_reports,
        )
        self.schemas.validate("agent_battle_harness_report", report)
        with self.assertRaisesRegex(ContractError, "duplicate roles"):
            validate_artifact_semantics("agent_battle_harness_report", report)

    def test_agent_battle_harness_rejects_external_judge_run_id_mismatch(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "agent_battle_harness_mismatch", ROOT / "scripts" / "agent_battle_harness.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent_battle_harness_mismatch"] = module
        spec.loader.exec_module(module)
        runs = [
            module.self_assess.CommandRun("python3 scripts/verify.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 scripts/verify_real_repo_corpus.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 -m unittest discover -s tests -v", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 scripts/replay_regressions.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 -m compileall apkernel scripts tests", "pass", 0, "ok", ""),
        ]
        self_report = module.self_assess.build_report(runs, "unit-agent-battle-mismatch")
        battle = module.battle_report.build_battle_report(self_report, "unit-agent-battle-mismatch")
        judge_reports = [
            {
                "version": "1.0",
                "run_id": "other-run",
                "role": role,
                "score": 96,
                "verdict": "advance",
                "stance": f"{role} external report",
                "findings": ["Independent agent reviewed current code and gates."],
                "context_refs": ["apkernel/core.py"],
                "source_report": f"codex-subagent://unit-{role}",
                "veto_active": False,
                "veto_reason": "No veto.",
            }
            for role in module.REQUIRED_JUDGES
        ]
        report = module.build_agent_battle_harness_report(
            self_report,
            battle,
            "unit-agent-battle-mismatch",
            judge_reports=judge_reports,
        )
        self.schemas.validate("agent_battle_harness_report", report)
        with self.assertRaisesRegex(ContractError, "input_run_id"):
            validate_artifact_semantics("agent_battle_harness_report", report)

    def test_agent_battle_harness_preserves_external_judge_hold_verdict(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "agent_battle_harness_hold", ROOT / "scripts" / "agent_battle_harness.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent_battle_harness_hold"] = module
        spec.loader.exec_module(module)
        runs = [
            module.self_assess.CommandRun("python3 scripts/verify.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 scripts/verify_real_repo_corpus.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 -m unittest discover -s tests -v", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 scripts/replay_regressions.py", "pass", 0, "ok", ""),
            module.self_assess.CommandRun("python3 -m compileall apkernel scripts tests", "pass", 0, "ok", ""),
        ]
        self_report = module.self_assess.build_report(runs, "unit-agent-battle-hold")
        battle = module.battle_report.build_battle_report(self_report, "unit-agent-battle-hold")
        judge_reports = [
            {
                "version": "1.0",
                "run_id": "unit-agent-battle-hold",
                "role": role,
                "score": 92 if role == "critic" else 96,
                "verdict": "hold" if role == "critic" else "advance",
                "stance": f"{role} external report",
                "findings": ["Independent agent reviewed current code and gates."],
                "context_refs": ["apkernel/core.py"],
                "source_report": f"codex-subagent://unit-{role}",
                "veto_active": False,
                "veto_reason": "No veto.",
            }
            for role in module.REQUIRED_JUDGES
        ]
        report = module.build_agent_battle_harness_report(
            self_report,
            battle,
            "unit-agent-battle-hold",
            judge_reports=judge_reports,
        )
        self.schemas.validate("agent_battle_harness_report", report)
        validate_artifact_semantics("agent_battle_harness_report", report)
        self.assertEqual(report["outcome"]["verdict"], "hold")
        self.assertIn(
            "fail",
            [
                item["status"]
                for item in report["judge_audit"]
                if item["check"] == "external judge verdicts allow advance"
            ],
        )

    def test_agent_battle_harness_script_accepts_external_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = importlib.util.spec_from_file_location(
                "agent_battle_harness_output_dir", ROOT / "scripts" / "agent_battle_harness.py"
            )
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            sys.modules["agent_battle_harness_output_dir"] = module
            spec.loader.exec_module(module)
            runs = [
                module.self_assess.CommandRun("python3 scripts/verify.py", "pass", 0, "ok", ""),
                module.self_assess.CommandRun("python3 scripts/verify_real_repo_corpus.py", "pass", 0, "ok", ""),
                module.self_assess.CommandRun("python3 -m unittest discover -s tests -v", "pass", 0, "ok", ""),
                module.self_assess.CommandRun("python3 scripts/replay_regressions.py", "pass", 0, "ok", ""),
                module.self_assess.CommandRun("python3 -m compileall apkernel scripts tests", "pass", 0, "ok", ""),
            ]
            self_report = module.self_assess.build_report(runs, "unit-agent-battle-output-dir")
            battle = module.battle_report.build_battle_report(self_report, "unit-agent-battle-output-dir")
            self_report_path = Path(tmp) / "self_assessment_report.json"
            battle_report_path = Path(tmp) / "battle_report.json"
            self_report_path.write_text(json.dumps(self_report, indent=2), encoding="utf-8")
            battle_report_path.write_text(json.dumps(battle, indent=2), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/agent_battle_harness.py",
                    "--self-report",
                    str(self_report_path),
                    "--battle-report",
                    str(battle_report_path),
                    "--output-dir",
                    tmp,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            summary = json.loads(result.stdout)
            report_path = Path(summary["report"])
            self.assertTrue(report_path.is_absolute())
            self.assertTrue(report_path.exists())
            self.assertEqual(report_path.parent, Path(tmp))

    def test_agent_battle_harness_report_writer_can_mirror_current(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "agent_battle_harness_writer", ROOT / "scripts" / "agent_battle_harness.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["agent_battle_harness_writer"] = module
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            report = {"version": "1.0", "run_id": "unit-writer"}
            report_path = Path(tmp) / "run" / "agent_battle_harness_report.json"
            current_path = Path(tmp) / "current" / "agent_battle_harness_report.json"
            module._write_report_files(report, report_path, current_report_path=current_path)
            self.assertEqual(load_json(report_path), report)
            self.assertEqual(load_json(current_path), report)

    def test_agent_battle_harness_cli_builds_independent_report_with_run_id(self) -> None:
        run_id = "unit-agent-battle-cli-independent"
        with tempfile.TemporaryDirectory() as tmp:
            spec = importlib.util.spec_from_file_location(
                "agent_battle_harness_cli", ROOT / "scripts" / "agent_battle_harness.py"
            )
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            sys.modules["agent_battle_harness_cli"] = module
            spec.loader.exec_module(module)
            runs = [
                module.self_assess.CommandRun("python3 scripts/verify.py", "pass", 0, "ok", ""),
                module.self_assess.CommandRun("python3 scripts/verify_real_repo_corpus.py", "pass", 0, "ok", ""),
                module.self_assess.CommandRun("python3 -m unittest discover -s tests -v", "pass", 0, "ok", ""),
                module.self_assess.CommandRun("python3 scripts/replay_regressions.py", "pass", 0, "ok", ""),
                module.self_assess.CommandRun("python3 -m compileall apkernel scripts tests", "pass", 0, "ok", ""),
            ]
            self_report = module.self_assess.build_report(runs, run_id)
            battle = module.battle_report.build_battle_report(self_report, run_id)
            self.mark_reports_ready_for_agent_battle(self_report, battle)
            self_report_path = Path(tmp) / "self_assessment_report.json"
            battle_report_path = Path(tmp) / "battle_report.json"
            self_report_path.write_text(json.dumps(self_report, indent=2), encoding="utf-8")
            battle_report_path.write_text(json.dumps(battle, indent=2), encoding="utf-8")
            judge_paths = []
            for role in ("architect", "test_engineer", "code_reviewer", "critic"):
                judge = {
                    "version": "1.0",
                    "run_id": run_id,
                    "role": role,
                    "score": 96,
                    "verdict": "advance",
                    "stance": f"{role} external report",
                    "findings": ["Independent agent reviewed current code and gates."],
                    "context_refs": ["apkernel/core.py"],
                    "source_report": f"codex-subagent://unit-cli-{role}",
                    "veto_active": False,
                    "veto_reason": "No veto.",
                }
                judge_path = Path(tmp) / f"{role}.json"
                judge_path.write_text(json.dumps(judge, indent=2), encoding="utf-8")
                judge_paths.extend(["--judge-report", str(judge_path)])
            output_dir = Path(tmp) / "battle-output"
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/agent_battle_harness.py",
                    "--run-id",
                    run_id,
                    "--self-report",
                    str(self_report_path),
                    "--battle-report",
                    str(battle_report_path),
                    "--output-dir",
                    str(output_dir),
                    *judge_paths,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["status"], "pass")
            report = load_json(Path(summary["report"]))
            self.schemas.validate("agent_battle_harness_report", report)
            validate_artifact_semantics("agent_battle_harness_report", report)
            self.assertEqual(report["run_id"], run_id)
            self.assertEqual(report["protocol"]["evidence_mode"], "independent_agent_reports")
            self.assertEqual(report["outcome"]["verdict"], "advance")

    def test_demo_bug_fix_pipeline_checkpoints_every_stage(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        scenario = self.bug_scenario()
        artifacts = build_demo_bug_fix_artifacts(scenario)
        reviewer = Reviewer(self.schemas)

        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas)
            engine = RunEngine(store, reviewer)
            payloads = {
                stage_name: {
                    artifact_name: artifacts[artifact_name]
                    for artifact_name in manifest.get_stage(stage_name).get("produces", [])
                }
                for stage_name in manifest.stage_names()
            }
            checkpoints = engine.run(manifest, "unit-demo", payloads)
            for checkpoint in checkpoints.values():
                self.assertTrue(Path(checkpoint).exists())

            self.assertTrue((Path(tmp) / "unit-demo" / "software-bug-fix" / "decision_log.json").exists())

    def test_checkpoint_paths_are_pipeline_scoped_for_shared_run_id(self) -> None:
        reviewer = Reviewer(self.schemas)
        run_id = "shared-run"
        bug_manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        bug_artifacts = build_demo_bug_fix_artifacts(self.bug_scenario(run_id))
        bug_payloads = {
            stage_name: {
                artifact_name: bug_artifacts[artifact_name]
                for artifact_name in bug_manifest.get_stage(stage_name).get("produces", [])
            }
            for stage_name in bug_manifest.stage_names()
        }
        incident_manifest = PipelineManifest.load(ROOT / "pipelines" / "software-incident-postmortem.json")
        incident_scenario = load_json(ROOT / "examples" / "incident_postmortem_scenario.json")
        incident_scenario["run_id"] = run_id

        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas)
            engine = RunEngine(store, reviewer)
            bug_checkpoints = engine.run(bug_manifest, run_id, bug_payloads)
            incident_checkpoints = engine.run_with_executor(
                incident_manifest,
                run_id,
                build_demo_incident_executor(incident_scenario),
                scenario=incident_scenario,
            )
            bug_root_cause = load_json(Path(bug_checkpoints["root_cause"]))
            incident_root_cause = load_json(Path(incident_checkpoints["root_cause"]))
            self.assertNotEqual(bug_checkpoints["root_cause"], incident_checkpoints["root_cause"])
            self.assertEqual(bug_root_cause["pipeline"], "software-bug-fix")
            self.assertEqual(incident_root_cause["pipeline"], "software-incident-postmortem")
            with self.assertRaisesRegex(ContractError, "ambiguous"):
                store.read_checkpoint(run_id, "root_cause")

    def test_demo_bug_fix_pipeline_can_run_with_stage_executor(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        scenario = self.bug_scenario()
        reviewer = Reviewer(self.schemas)

        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas)
            engine = RunEngine(store, reviewer)
            checkpoints = engine.run_with_executor(
                manifest,
                "unit-demo",
                build_demo_bug_fix_executor(scenario),
                scenario=scenario,
            )
            self.assertEqual(set(checkpoints), set(manifest.stage_names()))
            verification_checkpoint = Path(checkpoints["verification"])
            self.assertTrue(verification_checkpoint.exists())
            actual = artifacts_from_checkpoints(tuple(checkpoints.values()))
            self.assertIn("verification_report", actual)
            self.assertEqual(actual["verification_report"]["overall_status"], "pass")

    def test_all_demo_pipelines_run_with_stage_executors(self) -> None:
        scenarios = {
            "software-bug-fix": ("bug_fix_scenario.json", build_demo_bug_fix_executor),
            "software-feature-build": ("feature_build_scenario.json", build_demo_feature_executor),
            "software-refactor": ("refactor_scenario.json", build_demo_refactor_executor),
            "software-incident-postmortem": ("incident_postmortem_scenario.json", build_demo_incident_executor),
            "software-release": ("release_scenario.json", build_demo_release_executor),
        }
        reviewer = Reviewer(self.schemas)
        role_policy = RolePolicy.load(ROOT / "packs" / "software" / "roles.json")

        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas, role_policy)
            engine = RunEngine(store, reviewer)
            for pipeline_name, (scenario_file, executor_factory) in scenarios.items():
                manifest = PipelineManifest.load(ROOT / "pipelines" / f"{pipeline_name}.json")
                scenario = load_json(ROOT / "examples" / scenario_file)
                checkpoints = engine.run_with_executor(
                    manifest,
                    scenario["run_id"],
                    executor_factory(scenario),
                    scenario=scenario,
                    metadata={"execution_mode": "stage_executor"},
                )
                self.assertEqual(set(checkpoints), set(manifest.stage_names()))
                for checkpoint_path in checkpoints.values():
                    checkpoint = load_json(Path(checkpoint_path))
                    self.assertEqual(checkpoint["metadata"]["execution_mode"], "stage_executor")
                    self.assertIn("actor_role", checkpoint["metadata"])
                    actor_role = checkpoint["metadata"]["actor_role"]
                    review_role = checkpoint["review"]["reviewer_role"]
                    approval_roles = {
                        approval["role"]
                        for approval in checkpoint["review"].get("approvals", [])
                        if approval.get("decision") == "pass"
                    }
                    allowed_reviewers = role_policy.roles[actor_role].review_by
                    if allowed_reviewers:
                        self.assertIn(review_role, allowed_reviewers)
                        self.assertLessEqual(set(allowed_reviewers), approval_roles)
                    approval_by = role_policy.roles[actor_role].approval_by
                    if approval_by:
                        approval = checkpoint["artifacts"].get("approval_decision")
                        self.assertIsInstance(approval, dict)
                        self.assertIn(approval["approver_role"], approval_by)
                        self.assertEqual(approval["decision"], "approved")
                        source_ref = approval.get("source_checkpoint")
                        self.assertIsInstance(source_ref, str)
                        source_checkpoint = load_json(Path(source_ref))
                        self.assertEqual(source_checkpoint["metadata"]["actor_role"], approval["approver_role"])
                        self.assertEqual(source_checkpoint["metadata"]["approval_for_pipeline"], checkpoint["pipeline"])
                        self.assertEqual(source_checkpoint["metadata"]["approval_for_stage"], checkpoint["stage"])
                        self.assertEqual(
                            source_checkpoint["artifacts"]["approval_decision"]["stage"],
                            checkpoint["stage"],
                        )
                        if approval["approver_role"] == "human_operator":
                            self.assertEqual(source_checkpoint["metadata"]["approval_source"], "explicit_human_grant")
                            self.assertIn("human_grant_id", source_checkpoint["metadata"])
                        else:
                            self.assertEqual(source_checkpoint["metadata"]["approval_source"], "runtime_approval_gate")
                    self.assertIn("tools_available", checkpoint["metadata"])
                    self.assertIn("tools_used", checkpoint["metadata"])
                    self.assertLessEqual(
                        set(checkpoint["metadata"]["tools_used"]),
                        set(checkpoint["metadata"]["tools_available"]),
                    )

    def test_release_pipeline_without_human_grant_stops_awaiting_human(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-release.json")
        scenario = copy.deepcopy(load_json(ROOT / "examples" / "release_scenario.json"))
        scenario.pop("human_approval_grants", None)
        reviewer = Reviewer(self.schemas)
        role_policy = RolePolicy.load(ROOT / "packs" / "software" / "roles.json")

        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas, role_policy)
            engine = RunEngine(store, reviewer)
            checkpoints = engine.run_with_executor(
                manifest,
                scenario["run_id"],
                build_demo_release_executor(scenario),
                scenario=scenario,
                metadata={"execution_mode": "stage_executor"},
            )
            self.assertEqual(set(checkpoints), {"release_gate"})
            checkpoint = load_json(Path(checkpoints["release_gate"]))
            self.assertEqual(checkpoint["status"], "awaiting_human")
            self.assertEqual(checkpoint["metadata"]["awaiting_approval_by"], "human_operator")
            self.assertEqual(checkpoint["metadata"]["approval_boundary"], "explicit_human_required")
            self.assertNotIn("approval_decision", checkpoint["artifacts"])

    def test_release_pipeline_rejects_malformed_human_grant(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-release.json")
        scenario = copy.deepcopy(load_json(ROOT / "examples" / "release_scenario.json"))
        scenario["human_approval_grants"] = {"release_gate": "not-a-dict"}
        reviewer = Reviewer(self.schemas)
        role_policy = RolePolicy.load(ROOT / "packs" / "software" / "roles.json")

        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas, role_policy)
            engine = RunEngine(store, reviewer)
            with self.assertRaisesRegex(ContractError, "human_approval_grants"):
                engine.run_with_executor(
                    manifest,
                    scenario["run_id"],
                    build_demo_release_executor(scenario),
                    scenario=scenario,
                    metadata={"execution_mode": "stage_executor"},
                )

    def test_research_pack_pipeline_runs_with_stage_executor(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "research-brief.json")
        scenario = load_json(ROOT / "examples" / "research_brief_scenario.json")
        reviewer = Reviewer(self.schemas)
        role_policy = RolePolicy.load(ROOT / "packs" / "research" / "roles.json")

        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas, role_policy)
            engine = RunEngine(store, reviewer)
            checkpoints = engine.run_with_executor(
                manifest,
                scenario["run_id"],
                build_demo_research_brief_executor(scenario),
                scenario=scenario,
                metadata={"execution_mode": "stage_executor", "domain_pack": "research"},
            )
            self.assertEqual(set(checkpoints), set(manifest.stage_names()))
            actual = artifacts_from_checkpoints(tuple(checkpoints.values()))
            self.assertEqual(
                actual["research_brief"]["thesis"],
                "A general agent production kernel must separate domain packs from the core control plane.",
            )
            for checkpoint_path in checkpoints.values():
                checkpoint = load_json(Path(checkpoint_path))
                actor_role = checkpoint["metadata"]["actor_role"]
                review_role = checkpoint["review"]["reviewer_role"]
                approval_roles = {
                    approval["role"]
                    for approval in checkpoint["review"].get("approvals", [])
                    if approval.get("decision") == "pass"
                }
                allowed_reviewers = role_policy.roles[actor_role].review_by
                if allowed_reviewers:
                    self.assertIn(review_role, allowed_reviewers)
                    self.assertLessEqual(set(allowed_reviewers), approval_roles)
                approval_by = role_policy.roles[actor_role].approval_by
                if approval_by:
                    approval = checkpoint["artifacts"].get("approval_decision")
                    self.assertIsInstance(approval, dict)
                    self.assertIn(approval["approver_role"], approval_by)
                    self.assertEqual(approval["decision"], "approved")
                self.assertIn("tools_available", checkpoint["metadata"])
                self.assertIn("tools_used", checkpoint["metadata"])
                self.assertLessEqual(
                    set(checkpoint["metadata"]["tools_used"]),
                    set(checkpoint["metadata"]["tools_available"]),
                )

    def test_design_pack_pipeline_runs_with_stage_executor(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "design-review.json")
        scenario = load_json(ROOT / "examples" / "design_review_scenario.json")
        reviewer = Reviewer(self.schemas)
        role_policy = RolePolicy.load(ROOT / "packs" / "design" / "roles.json")

        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas, role_policy)
            engine = RunEngine(store, reviewer)
            checkpoints = engine.run_with_executor(
                manifest,
                scenario["run_id"],
                build_demo_design_review_executor(scenario),
                scenario=scenario,
                metadata={"execution_mode": "stage_executor", "domain_pack": "design"},
            )
            self.assertEqual(set(checkpoints), set(manifest.stage_names()))
            actual = artifacts_from_checkpoints(tuple(checkpoints.values()))
            self.assertEqual(actual["accessibility_audit"]["verdict"], "pass")
            self.assertEqual(actual["visual_quality_report"]["verdict"], "pass")
            self.assertEqual(actual["design_release_report"]["status"], "ready")
            self.assertIn(
                "Trystan-SA/claude-design-system-prompt",
                actual["design_context"]["upstream_attribution"],
            )
            self.assertIn(
                "examples/design_sources/claude_design_system_prompt_summary.md",
                actual["design_release_report"]["attribution_refs"],
            )
            self.assertIn(
                "browser-render-probe",
                [check["name"] for check in actual["design_prototype_report"]["render_checks"]],
            )
            for checkpoint_path in checkpoints.values():
                checkpoint = load_json(Path(checkpoint_path))
                actor_role = checkpoint["metadata"]["actor_role"]
                review_role = checkpoint["review"]["reviewer_role"]
                allowed_reviewers = role_policy.roles[actor_role].review_by
                if allowed_reviewers:
                    self.assertIn(review_role, allowed_reviewers)
                self.assertIn("tools_available", checkpoint["metadata"])
                self.assertIn("tools_used", checkpoint["metadata"])
                self.assertLessEqual(
                    set(checkpoint["metadata"]["tools_used"]),
                    set(checkpoint["metadata"]["tools_available"]),
                )

    def test_design_prototype_probe_script_renders_demo(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/probe_design_prototype.py", "examples/design_review_demo.html"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "pass")
        self.assertIn(
            "browser-render-probe",
            [check["name"] for check in report["checks"]],
        )

    def test_accessibility_audit_semantics_reject_fake_pass_with_blocker(self) -> None:
        report = {
            "version": "1.0",
            "design_id": "DESIGN-FAIL",
            "wcag_level": "AA",
            "verdict": "pass",
            "checks": [
                {
                    "category": "contrast",
                    "status": "pass",
                    "evidence": "Claimed pass.",
                }
            ],
            "blockers": ["Focus ring removed without replacement."],
            "fixes_applied": [],
        }
        self.schemas.validate("accessibility_audit", report)
        with self.assertRaisesRegex(ContractError, "requires no blockers"):
            validate_artifact_semantics("accessibility_audit", report)

    def test_design_brief_semantics_reject_missing_local_source_ref(self) -> None:
        report = {
            "version": "1.0",
            "design_id": "DESIGN-FAIL",
            "surface": "Prototype",
            "audience": "Operators",
            "primary_goal": "Inspect evidence.",
            "constraints": ["Use local sources."],
            "source_refs": ["examples/design_sources/missing.md"],
        }
        self.schemas.validate("design_brief", report)
        with self.assertRaisesRegex(ContractError, "missing local source"):
            validate_artifact_semantics("design_brief", report)

    def test_design_semantics_lazy_load_from_public_api(self) -> None:
        code = """
from apkernel import ContractError, validate_artifact_semantics

report = {
    "version": "1.0",
    "design_id": "DESIGN-FAIL",
    "surface": "Prototype",
    "audience": "Operators",
    "primary_goal": "Inspect evidence.",
    "constraints": ["Use local sources."],
    "source_refs": ["examples/design_sources/missing.md"],
}
try:
    validate_artifact_semantics("design_brief", report)
except ContractError as exc:
    raise SystemExit(0 if "missing local source" in str(exc) else 2)
raise SystemExit(1)
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_design_semantics_reject_external_noop_pre_registration(self) -> None:
        code = """
from apkernel import ContractError, register_artifact_semantic_validator

try:
    register_artifact_semantic_validator("design_brief", lambda artifact: [])
except ContractError as exc:
    raise SystemExit(0 if "must be registered by" in str(exc) else 2)
raise SystemExit(1)
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_design_semantics_loader_failure_does_not_cache_bypass(self) -> None:
        code = """
import apkernel.core as core

report = {
    "version": "1.0",
    "design_id": "DESIGN-FAIL",
    "surface": "Prototype",
    "audience": "Operators",
    "primary_goal": "Inspect evidence.",
    "constraints": ["Use local sources."],
    "source_refs": ["examples/design_sources/missing.md"],
}

original_load_json = core.load_json
failed_once = {"value": False}

def flaky_load_json(path):
    if str(path).endswith("packs/registry.json") and not failed_once["value"]:
        failed_once["value"] = True
        raise OSError("synthetic registry read failure")
    return original_load_json(path)

core.load_json = flaky_load_json
try:
    try:
        core.validate_artifact_semantics("design_brief", report)
    except core.ContractError:
        pass
    else:
        raise SystemExit(1)
finally:
    core.load_json = original_load_json

try:
    core.validate_artifact_semantics("design_brief", report)
except core.ContractError as exc:
    raise SystemExit(0 if "missing local source" in str(exc) else 2)
raise SystemExit(3)
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_design_prototype_report_semantics_reject_missing_artifact(self) -> None:
        report = {
            "version": "1.0",
            "design_id": "DESIGN-FAIL",
            "artifact_path": "examples/missing_design_artifact.html",
            "medium": "prototype",
            "interaction_model": "Clickable prototype.",
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
                "status": "pass",
                "screenshot_path": "examples/missing_probe.png",
            },
            "render_checks": [
                {
                    "name": "browser-render-probe",
                    "status": "pass",
                    "evidence": "Render probe claims pass.",
                },
                {
                    "name": "interaction-state-probe",
                    "status": "pass",
                    "evidence": "Interaction probe claims pass.",
                }
            ],
        }
        self.schemas.validate("design_prototype_report", report)
        with self.assertRaisesRegex(ContractError, "artifact_path does not exist"):
            validate_artifact_semantics("design_prototype_report", report)

    def test_design_prototype_report_semantics_reject_claimed_loading_without_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "prototype.html"
            screenshot = Path(tmp) / "probe.png"
            artifact.write_text(
                """
                <!doctype html>
                <style>
                  button:hover {}
                  button:active {}
                  button:focus-visible {}
                  button:disabled {}
                </style>
                <button type="button" disabled>Saving</button>
                """,
                encoding="utf-8",
            )
            screenshot.write_bytes(b"fake-png")
            report = {
                "version": "1.0",
                "design_id": "DESIGN-FAIL",
                "artifact_path": str(artifact),
                "medium": "prototype",
                "interaction_model": "Clickable prototype.",
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
                    "status": "pass",
                    "screenshot_path": str(screenshot),
                },
                "render_checks": [
                    {
                        "name": "browser-render-probe",
                        "status": "pass",
                        "evidence": "Render probe claims pass.",
                    },
                    {
                        "name": "interaction-state-probe",
                        "status": "pass",
                        "evidence": "Interaction probe claims pass.",
                    }
                ],
            }
            self.schemas.validate("design_prototype_report", report)
            with self.assertRaisesRegex(ContractError, "claims 'loading' without an HTML state marker"):
                validate_artifact_semantics("design_prototype_report", report)

    def test_design_prototype_report_semantics_reject_forged_probe_without_screenshot(self) -> None:
        report = {
            "version": "1.0",
            "design_id": "DESIGN-FAIL",
            "artifact_path": "examples/design_review_demo.html",
            "medium": "prototype",
            "interaction_model": "Clickable prototype.",
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
                "status": "pass",
                "screenshot_path": "examples/missing_probe.png",
            },
            "render_checks": [
                {
                    "name": "browser-render-probe",
                    "status": "pass",
                    "evidence": "Render probe claims pass.",
                },
                {
                    "name": "interaction-state-probe",
                    "status": "pass",
                    "evidence": "Interaction probe claims pass.",
                },
            ],
        }
        self.schemas.validate("design_prototype_report", report)
        with self.assertRaisesRegex(ContractError, "probe.screenshot_path does not exist"):
            validate_artifact_semantics("design_prototype_report", report)

    def test_visual_quality_report_semantics_reject_open_ai_slop_pass(self) -> None:
        report = {
            "version": "1.0",
            "design_id": "DESIGN-FAIL",
            "verdict": "pass",
            "ai_slop_findings": [
                {
                    "rule": "ai-slop-check.gradients",
                    "severity": "quality",
                    "status": "open",
                    "evidence": "Hero still uses saturated multi-stop gradient.",
                }
            ],
            "hierarchy_findings": [],
            "interaction_state_findings": [],
            "fixes_applied": [],
        }
        self.schemas.validate("visual_quality_report", report)
        with self.assertRaisesRegex(ContractError, "requires no open blocker or quality findings"):
            validate_artifact_semantics("visual_quality_report", report)

    def test_design_release_report_semantics_reject_ready_with_failed_gate(self) -> None:
        report = {
            "version": "1.0",
            "design_id": "DESIGN-FAIL",
            "status": "ready",
            "gates": [
                {
                    "name": "accessibility-aa",
                    "status": "fail",
                    "evidence_artifact": "accessibility_audit",
                }
            ],
            "open_decisions": [],
            "attribution_refs": ["https://github.com/Trystan-SA/claude-design-system-prompt"],
        }
        self.schemas.validate("design_release_report", report)
        with self.assertRaisesRegex(ContractError, "requires every gate to pass"):
            validate_artifact_semantics("design_release_report", report)

    def test_design_release_report_semantics_reject_missing_upstream_provenance(self) -> None:
        report = {
            "version": "1.0",
            "design_id": "DESIGN-FAIL",
            "status": "ready",
            "gates": [
                {
                    "name": "accessibility-aa",
                    "status": "pass",
                    "evidence_artifact": "accessibility_audit",
                },
                {
                    "name": "visual-quality",
                    "status": "pass",
                    "evidence_artifact": "visual_quality_report",
                },
            ],
            "open_decisions": [],
            "attribution_refs": ["examples/design_sources/local-only.md"],
        }
        self.schemas.validate("design_release_report", report)
        with self.assertRaisesRegex(ContractError, "upstream project provenance"):
            validate_artifact_semantics("design_release_report", report)

    def test_role_policy_rejects_wrong_stage_owner(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        reviewer = Reviewer(self.schemas)
        role_policy = RolePolicy.load(ROOT / "packs" / "software" / "roles.json")
        produced = {"bug_report": artifacts["bug_report"]}
        review = reviewer.review(manifest, "reproduce", produced)
        self.assertEqual(review.decision, "pass")
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas, role_policy)
            with self.assertRaises(ContractError):
                store.write(
                    manifest,
                    "unit-demo",
                    "reproduce",
                    status="completed",
                    artifacts=produced,
                    review=review,
                    actor_role="release_captain",
                )

    def test_role_policy_rejects_unapproved_reviewer_role(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        role_policy = RolePolicy.load(ROOT / "packs" / "software" / "roles.json")
        produced = {"bug_report": artifacts["bug_report"]}
        review = Reviewer(self.schemas, reviewer_role="release_captain").review(
            manifest,
            "reproduce",
            produced,
        )
        self.assertEqual(review.decision, "pass")
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas, role_policy)
            with self.assertRaisesRegex(ContractError, "must be reviewed"):
                store.write(
                    manifest,
                    "unit-demo",
                    "reproduce",
                    status="completed",
                    artifacts=produced,
                    review=review,
                    actor_role="diagnoser",
                )

    def test_role_policy_requires_declared_approval_decision(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        role_policy = RolePolicy.load(ROOT / "packs" / "software" / "roles.json")
        reviewer = Reviewer(self.schemas)

        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas, role_policy)
            for stage_name in ["reproduce", "root_cause"]:
                produced = {
                    artifact_name: artifacts[artifact_name]
                    for artifact_name in manifest.get_stage(stage_name).get("produces", [])
                }
                review = reviewer.review(
                    manifest,
                    stage_name,
                    produced,
                    reviewer_roles=role_policy.reviewers_for_stage(stage_name),
                )
                store.write(
                    manifest,
                    "unit-demo",
                    stage_name,
                    status="completed",
                    artifacts=produced,
                    review=review,
                    actor_role=role_policy.owner_for_stage(stage_name),
                )

            produced = {
                "system_fault_report": artifacts["system_fault_report"],
                "decision_log": artifacts["decision_log"],
            }
            review = reviewer.review(
                manifest,
                "system_fault",
                produced,
                reviewer_roles=role_policy.reviewers_for_stage("system_fault"),
            )
            self.assertEqual(review.decision, "pass")
            with self.assertRaisesRegex(ContractError, "approval_decision"):
                store.write(
                    manifest,
                    "unit-demo",
                    "system_fault",
                    status="completed",
                    artifacts=produced,
                    review=review,
                    actor_role="system_repairer",
                )

    def test_role_policy_rejects_wrong_approval_role(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        role_policy = RolePolicy.load(ROOT / "packs" / "software" / "roles.json")
        reviewer = Reviewer(self.schemas)

        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas, role_policy)
            for stage_name in ["reproduce", "root_cause"]:
                produced = {
                    artifact_name: artifacts[artifact_name]
                    for artifact_name in manifest.get_stage(stage_name).get("produces", [])
                }
                review = reviewer.review(
                    manifest,
                    stage_name,
                    produced,
                    reviewer_roles=role_policy.reviewers_for_stage(stage_name),
                )
                store.write(
                    manifest,
                    "unit-demo",
                    stage_name,
                    status="completed",
                    artifacts=produced,
                    review=review,
                    actor_role=role_policy.owner_for_stage(stage_name),
                )

            produced = {
                "system_fault_report": artifacts["system_fault_report"],
                "decision_log": artifacts["decision_log"],
                "approval_decision": {
                    "version": "1.0",
                    "run_id": "unit-demo",
                    "stage": "system_fault",
                    "approver_role": "human_operator",
                    "decision": "approved",
                    "reason": "wrong role for test",
                    "evidence_refs": ["system_fault_report"],
                },
            }
            review = reviewer.review(
                manifest,
                "system_fault",
                produced,
                reviewer_roles=role_policy.reviewers_for_stage("system_fault"),
            )
            self.assertEqual(review.decision, "pass")
            with self.assertRaisesRegex(ContractError, "must be approved"):
                store.write(
                    manifest,
                    "unit-demo",
                    "system_fault",
                    status="completed",
                    artifacts=produced,
                    review=review,
                    actor_role="system_repairer",
                )

    def test_role_policy_rejects_forged_inline_approval_without_source_checkpoint(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        role_policy = RolePolicy.load(ROOT / "packs" / "software" / "roles.json")
        reviewer = Reviewer(self.schemas)

        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas, role_policy)
            for stage_name in ["reproduce", "root_cause"]:
                produced = {
                    artifact_name: artifacts[artifact_name]
                    for artifact_name in manifest.get_stage(stage_name).get("produces", [])
                }
                review = reviewer.review(
                    manifest,
                    stage_name,
                    produced,
                    reviewer_roles=role_policy.reviewers_for_stage(stage_name),
                )
                store.write(
                    manifest,
                    "unit-demo",
                    stage_name,
                    status="completed",
                    artifacts=produced,
                    review=review,
                    actor_role=role_policy.owner_for_stage(stage_name),
                )

            produced = {
                "system_fault_report": artifacts["system_fault_report"],
                "decision_log": artifacts["decision_log"],
                "approval_decision": {
                    "version": "1.0",
                    "run_id": "unit-demo",
                    "stage": "system_fault",
                    "approver_role": "release_captain",
                    "decision": "approved",
                    "reason": "inline approval is not enough",
                    "evidence_refs": ["system_fault_report"],
                },
            }
            review = reviewer.review(
                manifest,
                "system_fault",
                produced,
                reviewer_roles=role_policy.reviewers_for_stage("system_fault"),
            )
            self.assertEqual(review.decision, "pass")
            with self.assertRaisesRegex(ContractError, "source_checkpoint"):
                store.write(
                    manifest,
                    "unit-demo",
                    "system_fault",
                    status="completed",
                    artifacts=produced,
                    review=review,
                    actor_role="system_repairer",
                )

    def test_role_policy_rejects_forged_approval_source_without_review(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        role_policy = RolePolicy.load(ROOT / "packs" / "software" / "roles.json")
        reviewer = Reviewer(self.schemas)

        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas, role_policy)
            for stage_name in ["reproduce", "root_cause"]:
                produced = {
                    artifact_name: artifacts[artifact_name]
                    for artifact_name in manifest.get_stage(stage_name).get("produces", [])
                }
                review = reviewer.review(
                    manifest,
                    stage_name,
                    produced,
                    reviewer_roles=role_policy.reviewers_for_stage(stage_name),
                )
                store.write(
                    manifest,
                    "unit-demo",
                    stage_name,
                    status="completed",
                    artifacts=produced,
                    review=review,
                    actor_role=role_policy.owner_for_stage(stage_name),
                )

            source_approval = {
                "version": "1.0",
                "run_id": "unit-demo",
                "stage": "system_fault",
                "approver_role": "release_captain",
                "decision": "approved",
                "reason": "forged source without review",
                "evidence_refs": ["system_fault_report"],
            }
            approval_stage = "approve_system_fault_by_release_captain"
            source_path = store.checkpoint_path("unit-demo", approval_stage, "approval-decision")
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text(
                json.dumps(
                    {
                        "version": "1.0",
                        "run_id": "unit-demo",
                        "pipeline": "approval-decision",
                        "pipeline_version": "1.0",
                        "stage": approval_stage,
                        "status": "completed",
                        "timestamp": "2026-07-03T00:00:00+00:00",
                        "artifacts": {"approval_decision": source_approval},
                        "review": None,
                        "metadata": {
                            "actor_role": "release_captain",
                            "approval_for_pipeline": "software-bug-fix",
                            "approval_for_stage": "system_fault",
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            produced = {
                "system_fault_report": artifacts["system_fault_report"],
                "decision_log": artifacts["decision_log"],
                "approval_decision": dict(source_approval, source_checkpoint=str(source_path.resolve())),
            }
            review = reviewer.review(
                manifest,
                "system_fault",
                produced,
                reviewer_roles=role_policy.reviewers_for_stage("system_fault"),
            )
            self.assertEqual(review.decision, "pass")
            with self.assertRaisesRegex(ContractError, "review.decision"):
                store.write(
                    manifest,
                    "unit-demo",
                    "system_fault",
                    status="completed",
                    artifacts=produced,
                    review=review,
                    actor_role="system_repairer",
                )

    def test_role_policy_rejects_forged_approval_source_with_fabricated_review(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        role_policy = RolePolicy.load(ROOT / "packs" / "software" / "roles.json")
        reviewer = Reviewer(self.schemas)

        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas, role_policy)
            for stage_name in ["reproduce", "root_cause"]:
                produced = {
                    artifact_name: artifacts[artifact_name]
                    for artifact_name in manifest.get_stage(stage_name).get("produces", [])
                }
                review = reviewer.review(
                    manifest,
                    stage_name,
                    produced,
                    reviewer_roles=role_policy.reviewers_for_stage(stage_name),
                )
                store.write(
                    manifest,
                    "unit-demo",
                    stage_name,
                    status="completed",
                    artifacts=produced,
                    review=review,
                    actor_role=role_policy.owner_for_stage(stage_name),
                )

            source_approval = {
                "version": "1.0",
                "run_id": "unit-demo",
                "stage": "system_fault",
                "approver_role": "release_captain",
                "decision": "approved",
                "reason": "forged source with fabricated review",
                "evidence_refs": ["system_fault_report"],
            }
            approval_stage = "approve_system_fault_by_release_captain"
            source_path = store.checkpoint_path("unit-demo", approval_stage, "approval-decision")
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text(
                json.dumps(
                    {
                        "version": "1.0",
                        "run_id": "unit-demo",
                        "pipeline": "approval-decision",
                        "pipeline_version": "1.0",
                        "stage": approval_stage,
                        "status": "completed",
                        "timestamp": "2026-07-03T00:00:00+00:00",
                        "artifacts": {"approval_decision": source_approval},
                        "review": {
                            "stage": approval_stage,
                            "decision": "pass",
                            "reviewer_role": "reviewer",
                            "approvals": [
                                {
                                    "role": "reviewer",
                                    "decision": "pass",
                                    "source": "fabricated",
                                    "findings": [],
                                }
                            ],
                            "findings": [],
                        },
                        "metadata": {
                            "actor_role": "release_captain",
                            "approval_for_pipeline": "software-bug-fix",
                            "approval_for_stage": "system_fault",
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            produced = {
                "system_fault_report": artifacts["system_fault_report"],
                "decision_log": artifacts["decision_log"],
                "approval_decision": dict(source_approval, source_checkpoint=str(source_path.resolve())),
            }
            review = reviewer.review(
                manifest,
                "system_fault",
                produced,
                reviewer_roles=role_policy.reviewers_for_stage("system_fault"),
            )
            self.assertEqual(review.decision, "pass")
            with self.assertRaisesRegex(ContractError, "trusted runtime"):
                store.write(
                    manifest,
                    "unit-demo",
                    "system_fault",
                    status="completed",
                    artifacts=produced,
                    review=review,
                    actor_role="system_repairer",
                )

    def test_role_policy_does_not_trust_directly_written_approval_checkpoint(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        role_policy = RolePolicy.load(ROOT / "packs" / "software" / "roles.json")
        reviewer = Reviewer(self.schemas)
        approval_stage = "approve_system_fault_by_release_captain"
        approval_manifest = PipelineManifest.from_dict(
            {
                "name": "approval-decision",
                "version": "1.0",
                "stages": [
                    {
                        "name": approval_stage,
                        "produces": ["approval_decision"],
                        "tools_available": [],
                        "review_focus": ["Approval decision is explicit and schema-valid."],
                        "success_criteria": ["requires:decision", "requires:approver_role"],
                    }
                ],
            }
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas, role_policy)
            self.assertFalse(hasattr(store, "trust_approval_source"))
            for stage_name in ["reproduce", "root_cause"]:
                produced = {
                    artifact_name: artifacts[artifact_name]
                    for artifact_name in manifest.get_stage(stage_name).get("produces", [])
                }
                review = reviewer.review(
                    manifest,
                    stage_name,
                    produced,
                    reviewer_roles=role_policy.reviewers_for_stage(stage_name),
                )
                store.write(
                    manifest,
                    "unit-demo",
                    stage_name,
                    status="completed",
                    artifacts=produced,
                    review=review,
                    actor_role=role_policy.owner_for_stage(stage_name),
                )

            source_approval = {
                "version": "1.0",
                "run_id": "unit-demo",
                "stage": "system_fault",
                "approver_role": "release_captain",
                "decision": "approved",
                "reason": "direct checkpoint write is not runtime trust",
                "evidence_refs": ["system_fault_report"],
            }
            approval_review = reviewer.review(
                approval_manifest,
                approval_stage,
                {"approval_decision": source_approval},
            )
            self.assertEqual(approval_review.decision, "pass")
            approval_store = CheckpointStore(Path(tmp), self.schemas)
            source_path = approval_store.write(
                approval_manifest,
                "unit-demo",
                approval_stage,
                status="completed",
                artifacts={"approval_decision": source_approval},
                review=approval_review,
                metadata={
                    "approval_for_pipeline": "software-bug-fix",
                    "approval_for_stage": "system_fault",
                    "approval_source": "runtime_approval_gate",
                },
                actor_role="release_captain",
            )

            produced = {
                "system_fault_report": artifacts["system_fault_report"],
                "decision_log": artifacts["decision_log"],
                "approval_decision": dict(source_approval, source_checkpoint=str(source_path.resolve())),
            }
            review = reviewer.review(
                manifest,
                "system_fault",
                produced,
                reviewer_roles=role_policy.reviewers_for_stage("system_fault"),
            )
            self.assertEqual(review.decision, "pass")
            with self.assertRaisesRegex(ContractError, "trusted runtime"):
                store.write(
                    manifest,
                    "unit-demo",
                    "system_fault",
                    status="completed",
                    artifacts=produced,
                    review=review,
                    actor_role="system_repairer",
                )

    def test_checkpoint_rejects_undeclared_tool_usage(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        produced = {"bug_report": artifacts["bug_report"]}
        review = Reviewer(self.schemas).review(manifest, "reproduce", produced)
        self.assertEqual(review.decision, "pass")
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas)
            with self.assertRaisesRegex(ContractError, "used tools not declared"):
                store.write(
                    manifest,
                    "unit-demo",
                    "reproduce",
                    status="completed",
                    artifacts=produced,
                    review=review,
                    metadata={"tools_used": ["undeclared-tool"]},
                )

    def test_reviewer_rejects_failed_command_marked_overall_pass(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        report = dict(artifacts["verification_report"])
        report["commands"] = [dict(report["commands"][0], status="fail", exit_code=17)]
        review = Reviewer(self.schemas).review(
            manifest,
            "verification",
            {"verification_report": report},
        )
        self.assertEqual(review.decision, "revise")

    def test_reviewer_rejects_unstructured_release_evidence(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-release.json")
        report = {
            "version": "1.0",
            "release_id": "REL-TEST",
            "status": "ready",
            "gates": [{"name": "unit-tests", "status": "pass", "evidence": "trust me"}],
            "rollback_plan": "rollback",
        }
        review = Reviewer(self.schemas).review(
            manifest,
            "release_gate",
            {"release_report": report},
        )
        self.assertEqual(review.decision, "revise")

    def test_checkpoint_rejects_stage_skip(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        reviewer = Reviewer(self.schemas)
        produced = {"patch_plan": artifacts["patch_plan"]}
        review = reviewer.review(manifest, "product_patch", produced)
        self.assertEqual(review.decision, "pass")
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas)
            with self.assertRaises(ContractError):
                store.write(
                    manifest,
                    "unit-demo",
                    "product_patch",
                    status="completed",
                    artifacts=produced,
                    review=review,
                )

    def test_checkpoint_store_can_record_blocked_branch_without_completion_artifacts(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-release.json")
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas)
            path = store.write(
                manifest,
                "unit-blocked",
                "release_gate",
                status="blocked",
                artifacts={},
                metadata={"stop_reason": "external approval required"},
            )
            checkpoint = load_json(path)
            self.assertEqual(checkpoint["status"], "blocked")
            self.assertEqual(checkpoint["metadata"]["stop_reason"], "external approval required")

    def test_autonomy_runner_executes_allowlisted_action_and_checkpoints(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "run_next_action", ROOT / "scripts" / "run_next_action.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["run_next_action"] = module
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as tmp:
            summary = module.run_next_action(
                action_file=ROOT / "examples" / "autonomy_safe_action.json",
                run_id="unit-autonomy-safe",
                output_root=Path(tmp),
            )
            self.assertEqual(summary["status"], "executed")
            checkpoint = load_json(Path(summary["checkpoint"]))
            self.assertEqual(checkpoint["status"], "completed")
            self.assertEqual(checkpoint["metadata"]["actor_role"], "autonomy_runner")

    def test_autonomy_runner_blocks_external_repo_action(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "run_next_action", ROOT / "scripts" / "run_next_action.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["run_next_action"] = module
        spec.loader.exec_module(module)

        action = {
            "id": "run-real-bug-scenario",
            "title": "Run one real repository bug through the kernel and preserve artifacts",
            "priority": "P2",
            "target_files": ["examples", "scripts", "docs"],
            "verification_commands": ["python3 scripts/verify.py"],
        }
        report = module.build_autonomy_report(action, "unit-autonomy-blocked")
        self.assertEqual(report["decision"], "blocked")
        self.assertIn("external_repo", report["boundaries"])

    def test_autonomy_runner_rejects_malformed_action_file(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "run_next_action", ROOT / "scripts" / "run_next_action.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["run_next_action"] = module
        spec.loader.exec_module(module)

        with self.assertRaisesRegex(ContractError, "action.id"):
            module._load_action(ROOT / "examples" / "autonomy_run_replay_fixture.json", "unit-bad-action")

    def test_real_bug_runner_rejects_dirty_external_worktree(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "run_real_bug_scenario", ROOT / "scripts" / "run_real_bug_scenario.py"
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules["run_real_bug_scenario"] = module
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            module._ensure_clean_worktree(repo)
            (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "clean external repo"):
                module._ensure_clean_worktree(repo)

    def test_checkpoint_rejects_unrelated_extra_artifact(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        produced = {
            "bug_report": artifacts["bug_report"],
            "release_report": {
                "version": "1.0",
                "release_id": "REL-TEST",
                "status": "ready",
                "gates": [
                    {
                        "name": "x",
                        "status": "pass",
                        "evidence": {
                            "command": "true",
                            "status": "pass",
                            "exit_code": 0,
                            "stdout_digest": "sha256:stdout",
                            "stderr_digest": "sha256:stderr",
                            "commit_sha": "workspace-uncommitted",
                            "tool_version": "sh",
                            "timestamp": "2026-06-30T00:00:00Z",
                            "artifact_refs": ["release_report"],
                        },
                    }
                ],
                "rollback_plan": "rollback",
            },
        }
        reviewer = Reviewer(self.schemas)
        review = reviewer.review(manifest, "reproduce", produced)
        self.assertEqual(review.decision, "pass")
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas)
            with self.assertRaises(ContractError):
                store.write(
                    manifest,
                    "unit-demo",
                    "reproduce",
                    status="completed",
                    artifacts=produced,
                    review=review,
                )

    def test_checkpoint_rejects_decision_log_run_id_mismatch(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        reviewer = Reviewer(self.schemas)
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas)
            for stage_name in ["reproduce", "root_cause"]:
                produced = {
                    artifact_name: artifacts[artifact_name]
                    for artifact_name in manifest.get_stage(stage_name).get("produces", [])
                }
                review = reviewer.review(manifest, stage_name, produced)
                store.write(
                    manifest,
                    "unit-demo",
                    stage_name,
                    status="completed",
                    artifacts=produced,
                    review=review,
                )

            bad_log = dict(artifacts["decision_log"])
            bad_log["run_id"] = "other-run"
            produced = {
                "system_fault_report": artifacts["system_fault_report"],
                "decision_log": bad_log,
            }
            review = reviewer.review(manifest, "system_fault", produced)
            self.assertEqual(review.decision, "pass")
            with self.assertRaises(ContractError):
                store.write(
                    manifest,
                    "unit-demo",
                    "system_fault",
                    status="completed",
                    artifacts=produced,
                    review=review,
                )

    def test_checkpoint_rejects_decision_stage_mismatch(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas)
            self.write_bug_stages(store, manifest, artifacts, "unit-demo", ["reproduce", "root_cause"])
            bad_log = dict(artifacts["decision_log"])
            bad_log["decisions"] = [dict(bad_log["decisions"][0], stage="verification")]
            produced = {
                "system_fault_report": artifacts["system_fault_report"],
                "decision_log": bad_log,
            }
            review = Reviewer(self.schemas).review(manifest, "system_fault", produced)
            self.assertEqual(review.decision, "pass")
            with self.assertRaises(ContractError):
                store.write(
                    manifest,
                    "unit-demo",
                    "system_fault",
                    status="completed",
                    artifacts=produced,
                    review=review,
                )

    def test_checkpoint_rejects_unknown_selected_option(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas)
            self.write_bug_stages(store, manifest, artifacts, "unit-demo", ["reproduce", "root_cause"])
            bad_log = dict(artifacts["decision_log"])
            bad_log["decisions"] = [dict(bad_log["decisions"][0], selected="missing")]
            produced = {
                "system_fault_report": artifacts["system_fault_report"],
                "decision_log": bad_log,
            }
            review = Reviewer(self.schemas).review(manifest, "system_fault", produced)
            self.assertEqual(review.decision, "pass")
            with self.assertRaises(ContractError):
                store.write(
                    manifest,
                    "unit-demo",
                    "system_fault",
                    status="completed",
                    artifacts=produced,
                    review=review,
                )

    def test_checkpoint_rejects_unknown_artifact_reference(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas)
            self.write_bug_stages(store, manifest, artifacts, "unit-demo", ["reproduce", "root_cause"])
            bad_decision = dict(artifacts["decision_log"]["decisions"][0])
            bad_decision["artifact_refs"] = ["missing_artifact"]
            bad_log = dict(artifacts["decision_log"], decisions=[bad_decision])
            produced = {
                "system_fault_report": artifacts["system_fault_report"],
                "decision_log": bad_log,
            }
            review = Reviewer(self.schemas).review(manifest, "system_fault", produced)
            self.assertEqual(review.decision, "pass")
            with self.assertRaises(ContractError):
                store.write(
                    manifest,
                    "unit-demo",
                    "system_fault",
                    status="completed",
                    artifacts=produced,
                    review=review,
                )

    def test_checkpoint_rejects_conflicting_duplicate_decision_id(self) -> None:
        manifest = PipelineManifest.load(ROOT / "pipelines" / "software-bug-fix.json")
        artifacts = build_demo_bug_fix_artifacts(self.bug_scenario())
        with tempfile.TemporaryDirectory() as tmp:
            store = CheckpointStore(Path(tmp), self.schemas)
            self.write_bug_stages(
                store,
                manifest,
                artifacts,
                "unit-demo",
                ["reproduce", "root_cause", "system_fault"],
            )
            changed_decision = dict(artifacts["decision_log"]["decisions"][0], reason="changed")
            changed_log = dict(artifacts["decision_log"], decisions=[changed_decision])
            with self.assertRaises(ContractError):
                store.write(
                    manifest,
                    "unit-demo",
                    "system_fault",
                    status="in_progress",
                    artifacts={"decision_log": changed_log},
                )

    def test_replay_harness_accepts_expected_partial_artifacts(self) -> None:
        scenario = self.bug_scenario()
        actual = build_demo_bug_fix_artifacts(scenario)
        checkpoints = {
            "system_fault": {
                "status": "completed",
                "metadata": {
                    "actor_role": "system_repairer",
                    "execution_mode": "stage_executor",
                    "tools_available": ["harness_editor", "schema_checker", "eval_case_writer"],
                    "tools_used": [],
                },
                "review": {"decision": "pass", "reviewer_role": "reviewer"},
            },
            "verification": {
                "status": "completed",
                "metadata": {
                    "actor_role": "verifier",
                    "execution_mode": "stage_executor",
                    "tools_available": ["test_runner", "typecheck_runner", "lint_runner", "runtime_probe"],
                    "tools_used": [],
                },
                "review": {"decision": "pass", "reviewer_role": "reviewer"},
            },
        }
        replay = ReplayHarness(ROOT / "examples")
        scenario = next(
            item
            for item in replay.load_scenarios("bug-fix")
            if item.name == "cache-key-privacy-scope-regression"
        )
        result = replay.evaluate(scenario, actual, checkpoints)
        self.assertTrue(result.passed, result.errors)

    def test_replay_harness_accepts_checkpoint_expectations(self) -> None:
        replay = ReplayHarness(ROOT / "examples")
        scenario = next(
            item
            for item in replay.load_scenarios("release")
            if item.name == "release-readiness-gates"
        )
        actual = {
            "release_report": {
                "status": "ready",
                "gates": [
                    {
                        "name": "unit-tests",
                        "status": "pass",
                        "evidence": {
                            "status": "pass",
                            "exit_code": 0,
                            "artifact_refs": ["release_report"],
                        },
                    },
                    {
                        "name": "rollback-plan",
                        "status": "pass",
                        "evidence": {
                            "status": "pass",
                            "exit_code": 0,
                            "artifact_refs": ["release_report"],
                        },
                    },
                ],
            }
        }
        checkpoints = {
            "release_gate": {
                "status": "completed",
                "metadata": {
                    "actor_role": "release_captain",
                    "execution_mode": "stage_executor",
                },
                "review": {"decision": "pass"},
            }
        }
        result = replay.evaluate(scenario, actual, checkpoints)
        self.assertTrue(result.passed, result.errors)

    def test_replay_harness_expected_failure_uses_mutations(self) -> None:
        replay = ReplayHarness(ROOT / "examples")
        scenario = next(
            item
            for item in replay.load_scenarios("negative")
            if item.name == "role-metadata-failure-detected"
        )
        actual = {"release_report": {"status": "ready"}}
        checkpoints = {
            "release_readiness": {
                "metadata": {
                    "actor_role": "release_captain",
                    "execution_mode": "stage_executor",
                },
                "review": {"decision": "pass"},
            }
        }
        result = replay.evaluate(scenario, actual, checkpoints)
        self.assertTrue(result.passed)
        self.assertTrue(result.errors)

    def test_real_repo_bug_run_replay_detects_false_passing_evidence(self) -> None:
        replay = ReplayHarness(ROOT / "examples")
        scenario = next(
            item
            for item in replay.load_scenarios("real-repo")
            if item.name == "real-repo-bug-run-failure-detected"
        )
        actual = {"real_repo_bug_run": load_json(ROOT / "examples" / "real_repo_bug_run_fixture.json")}
        result = replay.evaluate(scenario, actual, {})
        self.assertTrue(result.passed)
        self.assertTrue(result.errors)

    def test_real_repo_corpus_replay_detects_boundary_mutation(self) -> None:
        replay = ReplayHarness(ROOT / "examples")
        scenario = next(
            item
            for item in replay.load_scenarios("corpus")
            if item.name == "real-repo-corpus-boundary-failure-detected"
        )
        actual = {"real_repo_corpus_report": load_json(ROOT / "examples" / "real_repo_corpus_report_fixture.json")}
        result = replay.evaluate(scenario, actual, {})
        self.assertTrue(result.passed)
        self.assertTrue(result.errors)

    def test_agent_battle_harness_replay_detects_blind_review_mutation(self) -> None:
        replay = ReplayHarness(ROOT / "examples")
        scenario = next(
            item
            for item in replay.load_scenarios("battle-harness")
            if item.name == "agent-battle-harness-blind-review-failure-detected"
        )
        actual = {"agent_battle_harness_report": load_json(ROOT / "examples" / "agent_battle_harness_report_fixture.json")}
        result = replay.evaluate(scenario, actual, {})
        self.assertTrue(result.passed)
        self.assertTrue(result.errors)

    def test_checkpoint_branch_replay_detects_blocked_status_mutation(self) -> None:
        replay = ReplayHarness(ROOT / "examples")
        scenario = next(
            item
            for item in replay.load_scenarios("branch-replay")
            if item.name == "checkpoint-branch-status-failure-detected"
        )
        fixture = load_json(ROOT / "examples" / "checkpoint_branch_replay_fixture.json")
        actual = {"checkpoint_branch_replay": fixture}
        result = replay.evaluate(scenario, actual, fixture["checkpoints"])
        self.assertTrue(result.passed)
        self.assertTrue(result.errors)

    def test_autonomy_boundary_replay_detects_false_execution(self) -> None:
        replay = ReplayHarness(ROOT / "examples")
        scenario = next(
            item
            for item in replay.load_scenarios("autonomy")
            if item.name == "autonomy-boundary-failure-detected"
        )
        fixture = load_json(ROOT / "examples" / "autonomy_run_replay_fixture.json")
        actual = {"autonomy_run_report": fixture["autonomy_run_report"]}
        result = replay.evaluate(scenario, actual, fixture["checkpoints"])
        self.assertTrue(result.passed)
        self.assertTrue(result.errors)

    def test_verify_script_passes_from_clean_state_without_legacy_apk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clean_root = Path(tmp) / "agent-production-kernel"
            for name in (
                "apkernel",
                "examples",
                "integrations",
                "packs",
                "pipelines",
                "schemas",
                "scripts",
            ):
                shutil.copytree(
                    ROOT / name,
                    clean_root / name,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
            result = subprocess.run(
                [sys.executable, "scripts/verify.py"],
                cwd=clean_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertFalse((clean_root / ".apk" / "verify-runs").exists())

    def test_real_repo_corpus_script_reports_gap_without_external_network(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/verify_real_repo_corpus.py"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        summary = json.loads(result.stdout)
        self.assertTrue(summary["target_met"])
        self.assertEqual(summary["missing_non_author_repos"], 0)
        self.assertFalse(summary["approval_required"])
        self.assertEqual(summary["approval_status"], "not_required")
        self.assertEqual(summary["current_report"], ".apk/real-repo-corpus/current/real_repo_corpus_report.json")
        self.assertTrue((ROOT / summary["current_report"]).exists())

    def test_real_repo_corpus_script_accepts_external_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "scripts/verify_real_repo_corpus.py", "--output-dir", tmp],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            summary = json.loads(result.stdout)
            report_path = Path(summary["report"])
            self.assertTrue(report_path.is_absolute())
            self.assertTrue(report_path.exists())
            self.assertEqual(report_path.parent, Path(tmp))
            self.assertIsNone(summary["current_report"])


if __name__ == "__main__":
    unittest.main()
