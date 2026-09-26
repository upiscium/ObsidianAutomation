from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import obsidian_automation.human_projection_cleanup as cleanup
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


def _state(tmp_path: Path, *, legacy_root: bool = False) -> tuple[Path, str]:
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
    if legacy_root:
        review_projection = replace(
            review_projection,
            target_path=review_projection.target_path.replace("04-AI/", "03-AI/", 1),
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


def test_reject_cleanup_preserves_legacy_03_ai_root(
    tmp_path: Path,
) -> None:
    state, _cleanup_sha = _state(tmp_path, legacy_root=True)
    calls: list[str] = []

    def delete_remote(**kwargs: object) -> str:
        calls.append(str(kwargs["target_path"]))
        return "already_absent"

    result = run_cleanup_sync(
        state,
        base_url="https://nextcloud.example/dav/Vault",
        username="sync",
        password="secret",
        delete_remote=delete_remote,
    )

    assert result["processed"] == 1
    assert calls == list(
        cleanup_target_paths(CASE, projection_root="03-AI")
    )


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


class _Response:
    def __init__(self, status: int):
        self.status = status

    def read(self) -> bytes:
        return b""


class _Connection:
    def __init__(self, status: int, calls: list[str]):
        self.status = status
        self.calls = calls

    def request(self, method: str, _path: str, **_kwargs: object) -> None:
        self.calls.append(method)

    def getresponse(self) -> _Response:
        return _Response(self.status)

    def close(self) -> None:
        return None


def test_delete_remote_target_verifies_absence_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    connections = [
        _Connection(204, calls),
        _Connection(404, calls),
    ]
    monkeypatch.setattr(
        cleanup,
        "_connection",
        lambda _parsed, *, timeout: connections.pop(0),
    )

    result = cleanup._delete_remote_target(
        base_url="https://nextcloud.example/dav/Vault",
        target_path=f"04-AI/50-Review/{CASE}.md",
        username="sync",
        password="secret",
        timeout=30.0,
        allow_http=False,
    )

    assert result == "deleted"
    assert calls == ["DELETE", "GET"]


def test_delete_remote_target_accepts_already_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    connections = [_Connection(404, calls)]
    monkeypatch.setattr(
        cleanup,
        "_connection",
        lambda _parsed, *, timeout: connections.pop(0),
    )

    result = cleanup._delete_remote_target(
        base_url="https://nextcloud.example/dav/Vault",
        target_path=f"04-AI/50-Review/{CASE}.md",
        username="sync",
        password="secret",
        timeout=30.0,
        allow_http=False,
    )

    assert result == "already_absent"
    assert calls == ["DELETE"]
