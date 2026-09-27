from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import (
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
    TERMINAL_CLEANUP_STAGES,
    ProjectionCleanupResult,
    ProjectionCleanupTargetResult,
    _store_cleanup_result,
    build_cleanup_request,
    build_terminal_cleanup_request,
    cleanup_target_paths,
    store_cleanup_request,
    store_terminal_cleanup_request,
)
from obsidian_automation.legacy_projection_retirement import (
    ARCHIVE_ROOT,
    ARCHIVE_STAGE,
    LegacyProjectionRetirementError,
    apply_retirement,
    build_retirement_plan,
    retirement_status,
)


CASE = "a" * 64
CASE2 = "b" * 64
SOURCE = "c" * 64
MUTATION = "d" * 64


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    root.mkdir()
    ensure_artifact_layout(root)
    (root / "24-Locks").mkdir()
    requests = root / "16-Human-Projection"
    requests.mkdir()
    for role in (
        "reader",
        "generator",
        "validator",
        "evaluator",
        "reviewer",
        "executor",
        "sync",
    ):
        (requests / role).mkdir()
    (root / "17-Human-Projection-Result").mkdir()
    return root


def _legacy_request(
    root: Path,
    *,
    role: str,
    stage: str,
    case_id: str,
) -> tuple[str, object]:
    request = build_request(
        case_id=case_id,
        stage=stage,
        source_kind="test",
        source_sha256=SOURCE,
        content=f"# {stage}\n",
        created_at="2026-09-27T00:00:00Z",
    )
    request = replace(
        request,
        target_path=request.target_path.replace("04-AI/", "03-AI/", 1),
    )
    digest, _ = store_request(root, role=role, request=request)
    _store_result(
        root,
        ProjectionResult(
            request_sha256=digest,
            target_path=request.target_path,
            content_sha256=request.content_sha256,
            result="created",
            completed_at="2026-09-27T00:00:01Z",
        ),
    )
    return digest, request


def _cleanup_result(
    root: Path,
    *,
    request_sha: str,
    case_id: str,
    stages: tuple[str, ...],
) -> None:
    targets = tuple(
        ProjectionCleanupTargetResult(path, "already_absent")
        for path in cleanup_target_paths(
            case_id,
            projection_root="03-AI",
            stages=stages,
        )
    )
    _store_cleanup_result(
        root,
        ProjectionCleanupResult(
            cleanup_request_sha256=request_sha,
            case_id=case_id,
            targets=targets,
            completed_at="2026-09-27T00:00:02Z",
        ),
    )


def _resolved_state(tmp_path: Path) -> tuple[Path, Path]:
    root = _root(tmp_path)

    review_sha, review = _legacy_request(
        root,
        role="evaluator",
        stage="review",
        case_id=CASE,
    )
    completed_sha, completed = _legacy_request(
        root,
        role="executor",
        stage="completed",
        case_id=CASE2,
    )

    reject = build_cleanup_request(
        case_id=CASE,
        review_projection_request_sha256=review_sha,
        evaluation_sha256=SOURCE,
        mutation_sha256=MUTATION,
        review_sha256="e" * 64,
        created_at="2026-09-27T00:00:00Z",
    )
    reject_sha, reject_path = store_cleanup_request(root, reject)
    _cleanup_result(
        root,
        request_sha=reject_sha,
        case_id=CASE,
        stages=(
            "input",
            "context",
            "generation",
            "validation",
            "evaluation",
            "review",
        ),
    )

    terminal = build_terminal_cleanup_request(
        case_id=CASE2,
        completed_projection_request_sha256=completed_sha,
        mutation_sha256=MUTATION,
        receipt_sha256="f" * 64,
        created_at="2026-09-27T00:00:00Z",
    )
    terminal_sha, terminal_path = store_terminal_cleanup_request(root, terminal)
    _cleanup_result(
        root,
        request_sha=terminal_sha,
        case_id=CASE2,
        stages=TERMINAL_CLEANUP_STAGES,
    )

    current = build_request(
        case_id="9" * 64,
        stage="input",
        source_kind="test",
        source_sha256=SOURCE,
        content="# current\n",
        created_at="2026-09-27T00:00:00Z",
    )
    current_sha, current_path = store_request(root, role="reader", request=current)
    _store_result(
        root,
        ProjectionResult(
            request_sha256=current_sha,
            target_path=current.target_path,
            content_sha256=current.content_sha256,
            result="created",
            completed_at="2026-09-27T00:00:01Z",
        ),
    )

    assert review.target_path.startswith("03-AI/")
    assert completed.target_path.startswith("03-AI/")
    assert reject_path.is_file()
    assert terminal_path.is_file()
    return root, current_path


def test_retirement_archives_only_fully_resolved_legacy_runtime_state(
    tmp_path: Path,
) -> None:
    root, current_path = _resolved_state(tmp_path)

    plan = build_retirement_plan(root)
    assert len(plan) == 8
    assert {entry.kind for entry in plan} == {
        "projection-request:evaluator",
        "projection-request:executor",
        "projection-result",
        "cleanup-request",
        "terminal-cleanup-request",
        "cleanup-result",
    }

    result = apply_retirement(root, require_root=False)
    assert result["status"] == "completed"
    assert result["archived"] == 8
    assert current_path.is_file()

    archive = root / ARCHIVE_STAGE / ARCHIVE_ROOT
    assert (archive / "retirement-intent.json").is_file()
    assert (archive / "retirement-completed.json").is_file()
    assert len(build_retirement_plan(root)) == 0
    assert retirement_status(root)["legacy_entries"] == 0

    archived_files = [
        path
        for path in archive.rglob("*")
        if path.is_file()
        and path.name not in {"retirement-intent.json", "retirement-completed.json"}
    ]
    assert len(archived_files) == 8

    replay = apply_retirement(root, require_root=False)
    assert replay["status"] == "already_completed"
    assert replay["already_archived"] == 8


def test_pending_legacy_projection_blocks_retirement(tmp_path: Path) -> None:
    root = _root(tmp_path)
    request = build_request(
        case_id=CASE,
        stage="review",
        source_kind="test",
        source_sha256=SOURCE,
        content="# pending\n",
        created_at="2026-09-27T00:00:00Z",
    )
    request = replace(
        request,
        target_path=request.target_path.replace("04-AI/", "03-AI/", 1),
    )
    store_request(root, role="evaluator", request=request)

    with pytest.raises(
        LegacyProjectionRetirementError,
        match="legacy projection result",
    ):
        build_retirement_plan(root)


def test_conflicted_legacy_projection_blocks_retirement(tmp_path: Path) -> None:
    root = _root(tmp_path)
    request = build_request(
        case_id=CASE,
        stage="review",
        source_kind="test",
        source_sha256=SOURCE,
        content="# conflict\n",
        created_at="2026-09-27T00:00:00Z",
    )
    request = replace(
        request,
        target_path=request.target_path.replace("04-AI/", "03-AI/", 1),
    )
    digest, _ = store_request(root, role="evaluator", request=request)
    _store_result(
        root,
        ProjectionResult(
            request_sha256=digest,
            target_path=request.target_path,
            content_sha256=request.content_sha256,
            result="conflict",
            completed_at="2026-09-27T00:00:01Z",
        ),
    )

    with pytest.raises(
        LegacyProjectionRetirementError,
        match="not terminally resolved",
    ):
        build_retirement_plan(root)
