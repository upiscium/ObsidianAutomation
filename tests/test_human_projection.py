from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import obsidian_automation.human_projection as projection
from obsidian_automation.human_projection import (
    HumanProjectionConflict,
    HumanProjectionError,
    build_request,
    parse_request,
    parse_result,
    run_projection_sync,
    store_request,
)
from obsidian_automation.webdav_create import WebDAVTargetExists


CASE = "a" * 64
SOURCE = "b" * 64


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    requests = state / "16-Human-Projection"
    requests.mkdir()
    for role in projection.ROLE_NAMES:
        (requests / role).mkdir()
    (state / "17-Human-Projection-Result").mkdir()
    (state / "24-Locks").mkdir()
    return state


def _request(content: str = "# Projection\n"):
    return build_request(
        case_id=CASE,
        stage="review",
        source_kind="evaluation_record",
        source_sha256=SOURCE,
        content=content,
        created_at="2026-09-20T00:00:00Z",
    )


def test_request_is_content_bound_and_target_is_deterministic() -> None:
    request = _request()
    parsed = parse_request(request.to_json_bytes())
    assert parsed == request
    assert parsed.target_path == f"04-AI/50-Review/{CASE}.md"

    value = request.to_json_bytes().replace(b"# Projection", b"# Tampered")
    with pytest.raises(HumanProjectionError, match="content"):
        parse_request(value)


def test_legacy_03_ai_request_and_result_remain_readable() -> None:
    request = _request()
    legacy_bytes = request.to_json_bytes().replace(b"04-AI/", b"03-AI/")
    legacy = parse_request(legacy_bytes)
    assert legacy.target_path == f"03-AI/50-Review/{CASE}.md"

    result = projection.ProjectionResult(
        request_sha256="c" * 64,
        target_path=legacy.target_path,
        content_sha256=legacy.content_sha256,
        result="created",
        completed_at="2026-09-20T00:00:01Z",
    )
    assert parse_result(result.to_json_bytes()).target_path == legacy.target_path


def test_projection_request_rejects_unrecognized_ai_root() -> None:
    request = _request()
    invalid = request.to_json_bytes().replace(b"04-AI/", b"05-AI/")
    with pytest.raises(HumanProjectionError, match="deterministic"):
        parse_request(invalid)


def test_projection_sync_creates_once_and_replay_is_local_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    digest, _ = store_request(state, role="evaluator", request=_request())

    collections: list[str] = []
    creates: list[str] = []

    monkeypatch.setattr(
        projection,
        "ensure_collection",
        lambda **kwargs: collections.append(str(kwargs["target_path"])) or "created",
    )

    def create(**kwargs):
        creates.append(str(kwargs["target_path"]))
        return SimpleNamespace(content_sha256="unused")

    monkeypatch.setattr(projection, "conditional_create", create)

    first = run_projection_sync(
        state,
        base_url="https://nextcloud.example/dav",
        username="sync",
        password="secret",
    )
    assert first == {
        "event": "ai-human-projection-sync",
        "status": "completed",
        "processed": 1,
        "created": 1,
        "already_matching": 0,
    }
    assert collections == ["04-AI", "04-AI/50-Review"]
    assert creates == [f"04-AI/50-Review/{CASE}.md"]

    result_path = (
        state
        / "17-Human-Projection-Result"
        / f"{digest}.projection-result.json"
    )
    result = parse_result(result_path.read_bytes())
    assert result.result == "created"
    assert result.request_sha256 == digest

    collections.clear()
    creates.clear()
    second = run_projection_sync(
        state,
        base_url="https://nextcloud.example/dav",
        username="sync",
        password="secret",
    )
    assert second["processed"] == 0
    assert collections == []
    assert creates == []


def test_projection_sync_adopts_exact_existing_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    digest, _ = store_request(state, role="reader", request=_request("# Existing\n"))

    monkeypatch.setattr(projection, "ensure_collection", lambda **_kwargs: "existing")

    def exists(**_kwargs):
        raise WebDAVTargetExists("exists")

    monkeypatch.setattr(projection, "conditional_create", exists)
    monkeypatch.setattr(
        projection,
        "observe_remote",
        lambda **_kwargs: SimpleNamespace(result="matching"),
    )

    result = run_projection_sync(
        state,
        base_url="https://nextcloud.example/dav",
        username="sync",
        password="secret",
    )
    assert result["already_matching"] == 1
    stored = parse_result(
        (
            state
            / "17-Human-Projection-Result"
            / f"{digest}.projection-result.json"
        ).read_bytes()
    )
    assert stored.result == "already_matching"


def test_projection_conflict_is_durable_and_remains_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    digest, _ = store_request(state, role="generator", request=_request("# Expected\n"))
    monkeypatch.setattr(projection, "ensure_collection", lambda **_kwargs: "existing")

    def exists(**_kwargs):
        raise WebDAVTargetExists("exists")

    monkeypatch.setattr(projection, "conditional_create", exists)
    monkeypatch.setattr(
        projection,
        "observe_remote",
        lambda **_kwargs: SimpleNamespace(result="conflict"),
    )

    with pytest.raises(HumanProjectionConflict, match="different bytes"):
        run_projection_sync(
            state,
            base_url="https://nextcloud.example/dav",
            username="sync",
            password="secret",
        )

    stored = parse_result(
        (
            state
            / "17-Human-Projection-Result"
            / f"{digest}.projection-result.json"
        ).read_bytes()
    )
    assert stored.result == "conflict"

    monkeypatch.setattr(
        projection,
        "conditional_create",
        lambda **_kwargs: pytest.fail("remote must not be retried after durable conflict"),
    )
    with pytest.raises(HumanProjectionConflict, match="unresolved"):
        run_projection_sync(
            state,
            base_url="https://nextcloud.example/dav",
            username="sync",
            password="secret",
        )
