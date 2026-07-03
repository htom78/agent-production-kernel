# Four Phase Completion Map

This file is the audit map for the requested four phases.

## Phase 1: Agent Production Kernel

Delivered:

- `apkernel/core.py` implements pipeline manifests, schema validation,
  checkpoints, decision-log merging, reviewer output, tool registry, and
  `RunEngine`.
- `apkernel/replay.py` implements a golden-scenario replay harness.
- `schemas/artifacts/*.json` defines canonical artifacts.
- `scripts/verify.py` loads manifests, validates schemas, runs a demo pipeline,
  checks emitted checkpoints, and replays golden scenarios for every software
  pipeline.
- `pipelines/kernel-autonomy.json` and `scripts/run_next_action.py` add a
  bounded self-driving runner that checkpoints safe execution or blocked
  boundaries.
- `packs/registry.json` makes domain packs discoverable instead of hardcoding
  software scenarios in verification scripts.

Completion evidence:

- `python3 scripts/verify.py`
- `python3 -m unittest discover -s tests`

## Phase 2: Software Engineering Pack

Delivered:

- `pipelines/software-bug-fix.json`
- `pipelines/software-feature-build.json`
- `pipelines/software-refactor.json`
- `pipelines/software-incident-postmortem.json`
- `pipelines/software-release.json`
- `packs/software/tool_registry.json`
- `packs/software/*.md` contains stage skills referenced by manifests.
- `examples/*_scenario.json` contains executable scenarios for every pipeline.

The bug-fix pipeline makes the user's key idea explicit:

```text
reproduce -> root_cause -> system_fault -> product_patch -> verification -> regression -> knowledge_update
```

That stage order forces the system/process defect to be named before the product
patch is accepted.

## Phase 2b: Research Pack Generality Proof

Delivered:

- `pipelines/research-brief.json`
- `packs/research/roles.json`
- `packs/research/tool_registry.json`
- `schemas/artifacts/research_question.json`
- `schemas/artifacts/source_evidence.json`
- `schemas/artifacts/research_brief.json`
- `examples/research_brief_scenario.json`
- `examples/golden_research_brief_replay.json`

This pack proves that the kernel can run a non-software workflow through the
same manifest, role, checkpoint, and replay mechanisms.

## Phase 3: External Harness/Loop Adapter Layer

Delivered:

- `integrations/external_adapters.json`

Adapters are contract-only on purpose. SWE-bench/SWE-agent style patch loops,
OpenHands-style sandboxes, LangGraph-style durable execution, and eval/trace
systems can be plugged in without taking over the kernel's source of truth.

The verifier checks adapter contract shape. Runtime integrations remain adapter
boundaries rather than kernel dependencies.

## Phase 4: Multi-Agent Layer

Delivered:

- `packs/software/roles.json`

Roles are not free-form chat participants. Each role owns stages, accepts
canonical inputs, emits canonical outputs, and hands off only after review.

The current engine enforces stage order and artifact lineage; role ownership is
enforced at checkpoint write time for completed approval-sensitive stages. The
`autonomy_runner` role owns the `select_next_action` stage.

## System Invariant

External frameworks and specialist agents can extend execution, but they cannot
replace:

```text
pipeline manifest + artifact schemas + checkpoints + decision log + verifier
```

## Hardening Added After Battle Review

- `RunEngine` executes stages in manifest order.
- `CheckpointStore` rejects stage skips, missing `required_artifacts_in`,
  unrelated checkpoint artifacts, non-passing reviews, and decision-log
  `run_id` mismatches.
- Checkpoint paths and decision logs are pipeline-scoped under
  `run_id/pipeline`, preventing cross-pipeline overwrites when stage names
  overlap.
- Runtime role policy separates review and approval: `review_by` is satisfied
  by schema/semantic review, while `approval_by` requires an explicit
  `approval_decision` artifact from the configured approver role, backed by a
  completed approver-owned `approval-decision` source checkpoint at the expected
  approval path with a passing review and runtime-trusted provenance.
  `human_operator` approval is not synthesized by the runtime; it requires an
  explicit `human_approval_grants` entry or the stage checkpoints as
  `awaiting_human`.
- Runtime checkpoint metadata records `tools_available` and `tools_used`, and
  rejects completed checkpoints that claim undeclared tool usage.
- `scripts/verify.py` fails when a manifest references a missing stage skill.
- `verification_report` now requires structured command evidence:
  `command`, `exit_code`, `stdout_digest`, `stderr_digest`, `commit_sha`,
  `tool_version`, `timestamp`, and `artifact_refs`.
- `scripts/replay_regressions.py` replays golden scenarios across all software
  pipelines.
- `real_repo_bug_run`, `checkpoint_branch_replay`, and `autonomy_run_report`
  fixtures cover a public bug proof, non-completed checkpoint branches, and
  bounded self-driving decisions.
- Semantic false-green tests reject schema-valid but inconsistent artifacts,
  including real-repo commit mismatches and checkpoint state mismatches.
- `real_repo_corpus_report` records external proof against the target of five
  non-author repositories and three failure families. Its approval boundary
  stays `awaiting_human` before approved external work and flips to
  `not_required` after the target is met. It also rejects duplicate corpus
  artifacts/run IDs and records producer-roundtrip freshness separately from
  live external reruns; live artifacts must be timestamp-fresh before they set
  `external_execution=true`, and report-level freshness timestamps are checked
  against entry-level command timestamps. Replay fixtures cover both the
  positive boundary and a mutated false-green boundary.
- `scripts/run_bugsinpy_corpus.py` generates five public non-author repo bug
  artifacts from BugsInPy metadata without credentials, paid services, pushes,
  or pull requests.
- `agent_battle_harness_report` adds a validation layer for applying Agent
  Battle to the kernel: evidence-mode labeling, blind-review enforcement,
  critic veto, cross-examination, and judge-audit checks. Derived local reports
  cannot advance; only explicit `agent_judge_report` inputs from distinct
  `codex-subagent://` sources can claim independent contexts, and the harness
  now requires exactly one report per required role with an advancing verdict
  and minimum score. Self-assessment also rejects independent battle evidence
  when its recorded input run IDs do not match the actual self-assessment and
  battle-report files, or when it is older than the current source evidence.
  The harness audit and semantic validator also block `advance` when those
  bindings are not file-backed. Negative replay mutates blind-review state to
  prove the harness catches protocol drift.
