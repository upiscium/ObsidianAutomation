from __future__ import annotations

from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import (
    _canonical_json_bytes,
    ensure_artifact_layout,
    sha256_bytes,
)
from obsidian_automation.human_projection import (
    ProjectionResult,
    _store_result,
    build_request,
    store_request,
)
from obsidian_automation.human_projection_cleanup import (
    HumanProjectionCleanupError,
    build_cleanup_request,
    cleanup_target_paths,
    parse_cleanup_result,
    run_cleanup_sync,
    store_cleanup_request,
)


CASE = "a" * 64
EVALUATION = "b" * 64
MUTATION = "c" * 64


def _state(tmp_path: Path) -> tuple[Path, str]:
    state = tmp_path / "state"
    state.mkdir()
    ensure_artifact_layout(state)
    (state / "24-Locks").mkdir()
    (state / "16-Human-Projection").mkdir()
    for role in (
        "reader",
        "generator",
        "validator",
        "evaluator",
        "reviewer",
        "executor",
        "sync",
    ):
        (state / "16-Human-Projection" / role).mkdir()
    (state / "17-Human-Projection-Result").mkdir()

    review_projection = build_request(
        case_id=CASE,
        stage="review",
        source_kind="evaluation_record",
        source_sha256=EVALUATION,
        content="# Human Review\n",
        created_at="2026-09-23T00:00:00Z",
    )
    review_request_sha, _ = store_request(
        state,
        role="evaluator",
        request=review_projection,
    )
    _store_result(
        state,
        ProjectionResult(
            request_sha256=review_request_sha,
            target_path=review_projection.target_path,
            content_sha256=review_projection.content_sha256,
            result="created",
            completed_at="2026-09-23T00:00:01Z",
        ),
    )

    review_bytes = _canonical_json_bytes(
        {
            "record_version": 2,
            "mutation_sha256": MUTATION,
            "evaluation_sha256": EVALUATION,
            "decision": "reject",
            "decided_at": "2026-09-23T00:01:00Z",
            "approver": "human",
        }
    )
    review_path = state / "20-Review" / f"{MUTATION}.approval.json"
    review_path.write_bytes(review_bytes)

    cleanup = build_cleanup_request(
        case_id=CASE,
        review_projection_request_sha256=review_request_sha,
        evaluation_sha256=EVALUATION,
        mutation_sha256=MUTATION,
        review_sha256=sha256_bytes(review_bytes),
        created_at="2026-09-23T00:01:00Z",
    )
    cleanup_sha, _ = store_cleanup_request(state, cleanup)
    return state, cleanup_sha


def test_reject_cleanup_deletes_exact_six_projection_paths_and_is_idempotent(
    tmp_path: Path,
) -> None:
    state, cleanup_sha = _state(tmp_path)
    calls: list[str] = []

    def delete_remote(**kwargs: object) -> str:
        calls.append(str(kwargs["target_path"]))
        return "deleted"

    first = run_cleanup_sync(
        state,
        base_url="https://nextcloud.example/dav/Vault",
        username="sync",
        password="secret",
        delete_remote=delete_remote,
    )

    assert first == {
        "event": "ai-human-projection-cleanup-sync",
        "status": "completed",
        "processed": 1,
        "deleted": 6,
        "already_absent": 0,
    }
    assert calls == list(cleanup_target_paths(CASE))
    assert (state / "20-Review" / f"{MUTATION}.approval.json").is_file()

    result_path = (
        state
        / "17-Human-Projection-Result"
        / f"{cleanup_sha}.projection-cleanup-result.json"
    )
    stored = parse_cleanup_result(result_path.read_bytes())
    assert stored.case_id == CASE
    assert [item.target_path for item in stored.targets] == list(
        cleanup_target_paths(CASE)
    )
    assert all(item.result == "deleted" for item in stored.targets)

    calls.clear()
    second = run_cleanup_sync(
        state,
        base_url="https://nextcloud.example/dav/Vault",
        username="sync",
        password="secret",
        delete_remote=delete_remote,
    )
    assert second["processed"] == 0
    assert calls == []


def test_cleanup_rejects_case_not_bound_to_review_projection(tmp_path: Path) -> None:
    state, _cleanup_sha = _state(tmp_path)

    reviewer = state / "16-Human-Projection" / "reviewer"
    for path in reviewer.glob("*.projection-cleanup.json"):
        path.unlink()

    review_bytes = (state / "20-Review" / f"{MUTATION}.approval.json").read_bytes()
    mismatched = build_cleanup_request(
        case_id="d" * 64,
        review_projection_request_sha256=next(
            (
                path.name.removesuffix(".projection.json")
                for path in (state / "16-Human-Projection" / "evaluator").glob(
                    "*.projection.json"
                )
            )
        ),
        evaluation_sha256=EVALUATION,
        mutation_sha256=MUTATION,
        review_sha256=sha256_bytes(review_bytes),
        created_at="2026-09-23T00:01:00Z",
    )
    store_cleanup_request(state, mismatched)

    called = False

    def should_not_delete(**_kwargs: object) -> str:
        nonlocal called
        called = True
        return "deleted"

    with pytest.raises(
        HumanProjectionCleanupError,
        match="does not match the authoritative review projection",
    ):
        run_cleanup_sync(
            state,
            base_url="https://nextcloud.example/dav/Vault",
            username="sync",
            password="secret",
            delete_remote=should_not_delete,
        )

    assert called is False


def test_cleanup_requires_authoritative_reject(tmp_path: Path) -> None:
    state, _cleanup_sha = _state(tmp_path)
    review_path = state / "20-Review" / f"{MUTATION}.approval.json"
    approve_bytes = _canonical_json_bytes(
        {
            "record_version": 2,
            "mutation_sha256": MUTATION,
            "evaluation_sha256": EVALUATION,
            "decision": "approve",
            "decided_at": "2026-09-23T00:01:00Z",
            "approver": "human",
        }
    )
    review_path.write_bytes(approve_bytes)

    called = False

    def should_not_delete(**_kwargs: object) -> str:
        nonlocal called
        called = True
        return "deleted"

    with pytest.raises(HumanProjectionCleanupError, match="review SHA"):
        run_cleanup_sync(
            state,
            base_url="https://nextcloud.example/dav/Vault",
            username="sync",
            password="secret",
            delete_remote=should_not_delete,
        )

    assert called is False
