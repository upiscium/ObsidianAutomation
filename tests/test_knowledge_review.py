from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    ensure_artifact_layout,
    evaluation_bound_review_record_bytes,
    load_review_record,
    sha256_bytes,
    store_untrusted_proposal,
    store_validated_mutation,
    store_validation_record,
)
from obsidian_automation.canonical_mutation import validate_create_note
from obsidian_automation.execution_orchestrator import (
    ExecutionOrchestrationError,
    prepare_execution_intent,
    reconcile_execution,
)
from obsidian_automation.knowledge_review import create_evaluation_bound_review


def _setup(
    tmp_path: Path,
    *,
    recommendation: str = "do_not_proceed",
    evaluation_mutation_sha256: str | None = None,
):
    vault = tmp_path / "vault"
    (vault / "11-Knowledge").mkdir(parents=True)
    ai_root = tmp_path / "state"
    ai_root.mkdir()
    ensure_artifact_layout(ai_root)
    (ai_root / "15-Evaluation").mkdir()

    proposal = (
        b'{"contract_version":1,"operation":"create_note",'
        b'"mutation_id":"human-review-test",'
        b'"target":{"path":"11-Knowledge/reviewed.md"},'
        b'"content":"# Reviewed\\n"}\n'
    )
    validated = validate_create_note(
        proposal,
        vault_root=vault,
        allowed_roots=["11-Knowledge"],
    )
    proposal_sha, _ = store_untrusted_proposal(ai_root, proposal)
    store_validated_mutation(ai_root, validated)
    store_validation_record(
        ai_root,
        proposal_sha256=proposal_sha,
        result="accepted",
        mutation_sha256=validated.mutation_sha256,
        validated_at="2026-09-15T00:00:00Z",
    )

    mutation_sha = evaluation_mutation_sha256 or validated.mutation_sha256
    evaluation_bytes = _canonical_json_bytes(
        {
            "record_version": 1,
            "proposal_sha256": proposal_sha,
            "mutation_sha256": mutation_sha,
            "generation_sha256": "1" * 64,
            "evaluation_context_sha256": "2" * 64,
            "evaluator": {
                "implementation_revision": "a" * 40,
                "prompt_template_version": "knowledge-note-evaluator-v3",
                "prompt_template_sha256": "3" * 64,
            },
            "model": {
                "provider": "ollama",
                "identifier": "gemma4:12b",
                "revision": "model-revision",
            },
            "model_config": {},
            "assessment": {
                "groundedness": "pass",
                "redundancy": "likely" if recommendation == "do_not_proceed" else "none",
                "consistency": "pass",
                "recommendation": recommendation,
                "findings": [],
            },
            "evaluated_at": "2026-09-15T00:01:00Z",
        }
    )
    evaluation_sha = sha256_bytes(evaluation_bytes)
    evaluation_path = ai_root / "15-Evaluation" / f"{evaluation_sha}.evaluation.json"
    evaluation_path.write_bytes(evaluation_bytes)
    return vault, ai_root, validated, proposal_sha, evaluation_sha, evaluation_path


def test_human_can_approve_do_not_proceed_evaluation(tmp_path: Path) -> None:
    _, ai_root, validated, proposal_sha, evaluation_sha, _ = _setup(
        tmp_path,
        recommendation="do_not_proceed",
    )

    result = create_evaluation_bound_review(
        ai_root,
        evaluation_sha256=evaluation_sha,
        decision="approve",
        approver="human",
        decided_at="2026-09-15T00:02:00Z",
    )

    assert result.proposal_sha256 == proposal_sha
    assert result.mutation_sha256 == validated.mutation_sha256
    assert result.evaluation_sha256 == evaluation_sha
    assert result.decision == "approve"
    assert result.review_sha256 == sha256_bytes(result.review_path.read_bytes())

    review = load_review_record(ai_root, validated.mutation_sha256)
    assert review.record_version == 2
    assert review.evaluation_sha256 == evaluation_sha
    assert review.approved is True


def test_human_can_reject_proceed_evaluation(tmp_path: Path) -> None:
    _, ai_root, validated, _, evaluation_sha, _ = _setup(
        tmp_path,
        recommendation="proceed",
    )

    result = create_evaluation_bound_review(
        ai_root,
        evaluation_sha256=evaluation_sha,
        decision="reject",
        approver="human",
        decided_at="2026-09-15T00:02:00Z",
    )

    assert result.decision == "reject"
    review = load_review_record(ai_root, validated.mutation_sha256)
    assert review.evaluation_sha256 == evaluation_sha
    assert review.approved is False


def test_cross_bound_evaluation_is_rejected(tmp_path: Path) -> None:
    _, ai_root, _, _, evaluation_sha, _ = _setup(
        tmp_path,
        evaluation_mutation_sha256="f" * 64,
    )

    with pytest.raises(
        ArtifactLifecycleError,
        match="evaluation mutation does not match accepted validation",
    ):
        create_evaluation_bound_review(
            ai_root,
            evaluation_sha256=evaluation_sha,
            decision="approve",
            approver="human",
        )


def test_modified_evaluation_is_rejected_before_review_persistence(tmp_path: Path) -> None:
    _, ai_root, validated, _, evaluation_sha, evaluation_path = _setup(tmp_path)
    value = json.loads(evaluation_path.read_text())
    value["assessment"]["recommendation"] = "manual_review"
    evaluation_path.write_text(json.dumps(value, ensure_ascii=False) + "\n")

    with pytest.raises(ArtifactLifecycleError, match="artifact hash mismatch"):
        create_evaluation_bound_review(
            ai_root,
            evaluation_sha256=evaluation_sha,
            decision="approve",
            approver="human",
        )
    assert not (
        ensure_artifact_layout(ai_root).review
        / f"{validated.mutation_sha256}.approval.json"
    ).exists()


def test_review_persistence_is_immutable_and_idempotent(tmp_path: Path) -> None:
    _, ai_root, _, _, evaluation_sha, _ = _setup(tmp_path)

    first = create_evaluation_bound_review(
        ai_root,
        evaluation_sha256=evaluation_sha,
        decision="approve",
        approver="human",
        decided_at="2026-09-15T00:02:00Z",
    )
    second = create_evaluation_bound_review(
        ai_root,
        evaluation_sha256=evaluation_sha,
        decision="approve",
        approver="human",
        decided_at="2026-09-15T00:02:00Z",
    )
    assert second.review_path == first.review_path
    assert second.review_sha256 == first.review_sha256

    with pytest.raises(ArtifactLifecycleError, match="different bytes"):
        create_evaluation_bound_review(
            ai_root,
            evaluation_sha256=evaluation_sha,
            decision="reject",
            approver="human",
            decided_at="2026-09-15T00:03:00Z",
        )


def test_v2_review_bytes_are_bound_into_execution_intent(tmp_path: Path) -> None:
    vault, ai_root, validated, _, evaluation_sha, _ = _setup(tmp_path)
    review_result = create_evaluation_bound_review(
        ai_root,
        evaluation_sha256=evaluation_sha,
        decision="approve",
        approver="human",
        decided_at="2026-09-15T00:02:00Z",
    )

    intent = prepare_execution_intent(
        ai_root,
        vault,
        validated.mutation_sha256,
        allowed_roots=["11-Knowledge"],
        prepared_at="2026-09-15T00:03:00Z",
    )
    assert intent.approval_sha256 == review_result.review_sha256

    review_result.review_path.write_bytes(
        evaluation_bound_review_record_bytes(
            mutation_sha256=validated.mutation_sha256,
            evaluation_sha256=evaluation_sha,
            decision="approve",
            approver="different-human",
            decided_at="2026-09-15T00:04:00Z",
        )
    )
    with pytest.raises(
        ExecutionOrchestrationError,
        match="approval artifact changed after intent preparation",
    ):
        reconcile_execution(
            ai_root,
            vault,
            validated.mutation_sha256,
            allowed_roots=["11-Knowledge"],
        )
