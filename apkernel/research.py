"""Research domain pack helpers."""

from __future__ import annotations

from typing import Any

from .core import ContractError, FunctionStageExecutor, StageExecutionContext


def _require_prior(context: StageExecutionContext, artifact_name: str) -> dict[str, Any]:
    artifact = context.prior_artifacts.get(artifact_name)
    if artifact is None:
        raise ContractError(f"{context.stage_name} requires prior artifact {artifact_name!r}")
    return artifact


def build_demo_research_brief_executor(scenario: dict[str, Any]) -> FunctionStageExecutor:
    """Create a deterministic executor for a non-software research pack."""

    question_id = scenario["question_id"]

    def scope_question(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        return {
            "research_question": {
                "version": "1.0",
                "question_id": question_id,
                "question": scenario["question"],
                "success_criteria": scenario["success_criteria"],
                "constraints": scenario["constraints"],
            }
        }

    def gather_evidence(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        question = _require_prior(context, "research_question")
        return {
            "source_evidence": {
                "version": "1.0",
                "question_id": question["question_id"],
                "sources": scenario["sources"],
                "coverage_notes": "Evidence includes primary mechanism, boundary condition, and counterexample notes.",
            }
        }

    def synthesize(context: StageExecutionContext) -> dict[str, dict[str, Any]]:
        evidence = _require_prior(context, "source_evidence")
        return {
            "research_brief": {
                "version": "1.0",
                "question_id": evidence["question_id"],
                "thesis": scenario["expected_thesis"],
                "findings": [
                    "A reusable agent system needs contracts for process, artifacts, and checkpoints.",
                    "Replay is useful only when it includes semantic and negative checks.",
                    "The second domain pack proves the kernel is not limited to software production.",
                ],
                "confidence": 0.82,
                "open_questions": ["Whether live external research tools should be adapters or pack-local tools."],
            }
        }

    return FunctionStageExecutor(
        {
            "scope_question": scope_question,
            "gather_evidence": gather_evidence,
            "synthesize": synthesize,
        }
    )
