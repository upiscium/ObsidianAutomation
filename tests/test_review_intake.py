from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import (
    _canonical_json_bytes,
    ensure_artifact_layout,
    load_review_record,
    sha256_bytes,
    store_untrusted_proposal,
    store_validated_mutation,
    store_validation_record,
)
from obsidian_automation.canonical_mutation import validate_create_note
from obsidian_automation.evaluation_artifact import EVALUATION_STAGE
from obsidian_automation.human_projection import (
    ProjectionResult,
    _store_result,
    emit_evaluation_and_review_projections,
    load_post_review_projection_binding,
    parse_request,
)
from obsidian_automation.human_projection_cleanup import (
    parse_cleanup_request,
)
from obsidian_automation.review_intake import (
    RemoteReview,
    ReviewIntakeError,
    extract_review_decision,
    run_review_intake,
)


CASE = "a" * 64


def _setup(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "11-Knowledge").mkdir(parents=True)

    state = tmp_path / "state"
    state.mkdir()
    ensure_artifact_layout(state)
    (state / EVALUATION_STAGE).mkdir()
    (state / "16-Human-Projection").mkdir()
    for role in ("reader", "generator", "validator", "evaluator", "reviewer", "executor", "sync"):
        (state / "16-Human-Projection" / role).mkdir()
    (state / "17-Human-Projection-Result").mkdir()

    proposal = (
        b'{"contract_version":1,"operation":"create_note",'
        b'"mutation_id":"review-intake-test",'
        b'"target":{"path":"11-Knowledge/review-intake.md"},'
        b'"content":"# Reviewed\\n"}\n'
    )
    validated = validate_create_note(
        proposal,
        vault_root=vault,
        allowed_roots=["11-Knowledge"],
    )
    proposal_sha, _ = store_untrusted_proposal(state, proposal)
    store_validated_mutation(state, validated)
    store_validation_record(
        state,
        proposal_sha256=proposal_sha,
        result="accepted",
        mutation_sha256=validated.mutation_sha256,
        validated_at="2026-09-21T00:00:00Z",
    )

    evaluation_bytes = _canonical_json_bytes(
        {
            "record_version": 1,
            "proposal_sha256": proposal_sha,
            "mutation_sha256": validated.mutation_sha256,
            "generation_sha256": "1" * 64,
            "evaluation_context_sha256": "2" * 64,
            "evaluator": {
                "implementation_revision": "b" * 40,
                "prompt_template_version": "knowledge-note-evaluator-v3",
                "prompt_template_sha256": "3" * 64,
            },
            "model": {
                "provider": "ollama",
                "identifier": "gemma4:12b",
                "revision": "4" * 64,
            },
            "model_config": {},
            "assessment": {
                "groundedness": "pass",
                "redundancy": "none",
                "consistency": "pass",
                "recommendation": "proceed",
                "findings": [],
            },
            "evaluated_at": "2026-09-21T00:01:00Z",
        }
    )
    evaluation_sha = sha256_bytes(evaluation_bytes)
    (
        state
        / EVALUATION_STAGE
        / f"{evaluation_sha}.evaluation.json"
    ).write_bytes(evaluation_bytes)

    emitted = emit_evaluation_and_review_projections(
        state,
        case_id=CASE,
        evaluation_sha256=evaluation_sha,
    )
    assert emitted is not None

    review_paths = sorted(
        (state / "16-Human-Projection" / "evaluator").glob("*.projection.json")
    )
    review_path = next(
        path
        for path in review_paths
        if parse_request(path.read_bytes()).stage == "review"
    )
    request_sha = review_path.name.removesuffix(".projection.json")
    request = parse_request(review_path.read_bytes())

    _store_result(
        state,
        ProjectionResult(
            request_sha256=request_sha,
            target_path=request.target_path,
            content_sha256=request.content_sha256,
            result="created",
            completed_at="2026-09-21T00:02:00Z",
        ),
    )
    return state, validated, evaluation_sha, request


def _edited(content: str, decision: str, *, crlf: bool = False) -> bytes:
    original = "review_request: "
    replacement = f"review_request: {decision}"
    assert original in content
    text = content.replace(original, replacement, 1)
    if crlf:
        text = text.replace("\n", "\r\n")
    return text.encode("utf-8")


def test_extract_review_decision_accepts_only_review_request_change() -> None:
    expected = "---\nreview_request: \n---\n\nBody\n"

    assert extract_review_decision(
        expected,
        _edited(expected, "approve"),
    ) == "approve"
    assert extract_review_decision(
        expected,
        _edited(expected, '"reject"', crlf=True),
    ) == "reject"
    assert extract_review_decision(
        expected,
        _edited(expected, "keep_as_idea"),
    ) == "keep_as_idea"
    assert extract_review_decision(expected, expected.encode()) is None

    with pytest.raises(ReviewIntakeError, match="outside review_request"):
        extract_review_decision(
            expected,
            _edited(expected.replace("Body", "Changed"), "approve"),
        )


def test_review_intake_creates_evaluation_bound_approval(tmp_path: Path) -> None:
    state, validated, evaluation_sha, request = _setup(tmp_path)

    def read_remote(**_kwargs):
        return RemoteReview(
            200,
            _edited(request.content, "approve", crlf=True),
            '"etag"',
        )

    result = run_review_intake(
        state,
        base_url="https://nextcloud.example/dav/Vault",
        username="review-reader",
        password="secret",
        approver="human",
        read_remote=read_remote,
    )

    assert result["processed"] == 1
    assert result["cleanup_requested"] == 0
    assert list(
        (state / "16-Human-Projection" / "reviewer").glob(
            "*.projection-cleanup.json"
        )
    ) == []
    review = load_review_record(state, validated.mutation_sha256)
    assert review.record_version == 3
    assert review.evaluation_sha256 == evaluation_sha
    assert review.decision == "approve"
    binding = load_post_review_projection_binding(
        state,
        validated.mutation_sha256,
    )
    assert binding is not None
    assert binding.case_id == CASE
    assert binding.evaluation_sha256 == evaluation_sha
    assert binding.mutation_sha256 == validated.mutation_sha256


def test_review_intake_reject_queues_projection_cleanup(tmp_path: Path) -> None:
    state, validated, evaluation_sha, request = _setup(tmp_path)

    result = run_review_intake(
        state,
        base_url="https://nextcloud.example/dav/Vault",
        username="review-reader",
        password="secret",
        approver="human",
        read_remote=lambda **_kwargs: RemoteReview(
            200,
            _edited(request.content, "reject"),
            '"etag"',
        ),
    )

    assert result["processed"] == 1
    assert result["cleanup_requested"] == 1
    review = load_review_record(state, validated.mutation_sha256)
    assert review.decision == "reject"
    assert review.evaluation_sha256 == evaluation_sha
    binding = load_post_review_projection_binding(
        state,
        validated.mutation_sha256,
    )
    assert binding is not None
    assert binding.case_id == CASE
    assert binding.evaluation_sha256 == evaluation_sha

    cleanup_paths = list(
        (state / "16-Human-Projection" / "reviewer").glob(
            "*.projection-cleanup.json"
        )
    )
    assert len(cleanup_paths) == 1
    cleanup = parse_cleanup_request(cleanup_paths[0].read_bytes())
    assert cleanup.case_id == CASE
    assert cleanup.evaluation_sha256 == evaluation_sha
    assert cleanup.mutation_sha256 == validated.mutation_sha256


def test_review_intake_keep_as_idea_queues_projection_cleanup(tmp_path: Path) -> None:
    state, validated, evaluation_sha, request = _setup(tmp_path)

    result = run_review_intake(
        state,
        base_url="https://nextcloud.example/dav/Vault",
        username="review-reader",
        password="secret",
        approver="human",
        read_remote=lambda **_kwargs: RemoteReview(
            200,
            _edited(request.content, "keep_as_idea"),
            '"etag"',
        ),
    )

    assert result["processed"] == 1
    assert result["cleanup_requested"] == 1
    review = load_review_record(state, validated.mutation_sha256)
    assert review.record_version == 3
    assert review.decision == "keep_as_idea"
    assert review.evaluation_sha256 == evaluation_sha

    cleanup_paths = list(
        (state / "16-Human-Projection" / "reviewer").glob(
            "*.projection-cleanup.json"
        )
    )
    assert len(cleanup_paths) == 1
    cleanup = parse_cleanup_request(cleanup_paths[0].read_bytes())
    assert cleanup.case_id == CASE
    assert cleanup.evaluation_sha256 == evaluation_sha
    assert cleanup.mutation_sha256 == validated.mutation_sha256


def test_review_intake_blank_decision_is_non_mutating(tmp_path: Path) -> None:
    state, validated, _evaluation_sha, request = _setup(tmp_path)

    result = run_review_intake(
        state,
        base_url="https://nextcloud.example/dav/Vault",
        username="review-reader",
        password="secret",
        approver="human",
        read_remote=lambda **_kwargs: RemoteReview(
            200,
            request.content.encode("utf-8"),
            None,
        ),
    )

    assert result["processed"] == 0
    assert result["waiting"] == 1
    assert not (
        state / "20-Review" / f"{validated.mutation_sha256}.approval.json"
    ).exists()


def test_invalid_remote_review_does_not_starve_later_case(tmp_path: Path) -> None:
    state, validated, evaluation_sha, _request = _setup(tmp_path)
    second_case = "f" * 64

    emitted = emit_evaluation_and_review_projections(
        state,
        case_id=second_case,
        evaluation_sha256=evaluation_sha,
    )
    assert emitted is not None

    review_rows = []
    for path in sorted(
        (state / "16-Human-Projection" / "evaluator").glob("*.projection.json")
    ):
        request = parse_request(path.read_bytes())
        if request.stage == "review":
            review_rows.append(
                (path.name.removesuffix(".projection.json"), request)
            )
    assert len(review_rows) == 2

    for request_sha, request in review_rows:
        result_path = (
            state
            / "17-Human-Projection-Result"
            / f"{request_sha}.projection-result.json"
        )
        if not result_path.exists():
            _store_result(
                state,
                ProjectionResult(
                    request_sha256=request_sha,
                    target_path=request.target_path,
                    content_sha256=request.content_sha256,
                    result="created",
                    completed_at="2026-09-21T00:02:00Z",
                ),
            )

    invalid_target = review_rows[0][1].target_path
    valid_request = review_rows[1][1]

    def read_remote(**kwargs):
        target_path = str(kwargs["target_path"])
        request = next(
            request
            for _request_sha, request in review_rows
            if request.target_path == target_path
        )
        if target_path == invalid_target:
            malformed = request.content.replace(
                'target_path: "11-Knowledge/review-intake.md"',
                "target_path: {child: changed}",
                1,
            )
            return RemoteReview(200, malformed.encode("utf-8"), None)
        return RemoteReview(200, _edited(request.content, "approve"), None)

    result = run_review_intake(
        state,
        base_url="https://nextcloud.example/dav/Vault",
        username="review-reader",
        password="secret",
        approver="human",
        read_remote=read_remote,
    )

    assert result["invalid_remote"] == 1
    assert result["processed"] == 1
    review = load_review_record(state, validated.mutation_sha256)
    assert review.decision == "approve"
    binding = load_post_review_projection_binding(
        state,
        validated.mutation_sha256,
    )
    assert binding is not None
    assert binding.case_id == valid_request.case_id


def test_review_intake_rejects_other_human_edits(tmp_path: Path) -> None:
    state, validated, _evaluation_sha, request = _setup(tmp_path)
    changed = _edited(
        request.content.replace("Human Review", "Tampered Review", 1),
        "approve",
    )

    result = run_review_intake(
        state,
        base_url="https://nextcloud.example/dav/Vault",
        username="review-reader",
        password="secret",
        approver="human",
        read_remote=lambda **_kwargs: RemoteReview(200, changed, None),
    )

    assert result["invalid_remote"] == 1
    assert result["processed"] == 0
    assert not (
        state / "20-Review" / f"{validated.mutation_sha256}.approval.json"
    ).exists()
