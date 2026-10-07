from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from obsidian_automation.github_daily_activity import (
    BODY_MAX_BYTES,
    CANONICAL_TIMEZONE,
    COMMENT_MAX_BYTES,
    GitHubDailyActivityError,
    ProjectBinding,
    _bounded_text,
    _date_window,
    collect_daily_evidence,
    make_daily_evidence_bundle,
    persist_daily_evidence,
)
from obsidian_automation.github_project_watcher import (
    GitHubAPIHTTPError,
    WatcherConfig,
)


class _FakeClient:
    @staticmethod
    def _repo_path(repository: str) -> str:
        return repository

    def _paged(self, path: str) -> list[dict[str, object]]:
        if "/issues?" in path:
            return [
                {
                    "number": 1,
                    "title": "Issue one",
                    "body": "x" * (BODY_MAX_BYTES + 100),
                    "created_at": "2026-10-05T01:00:00Z",
                    "updated_at": "2026-10-05T01:00:00Z",
                    "html_url": "https://github.com/upiscium/Test/issues/1",
                    "state": "open",
                    "user": {"login": "upiscium"},
                },
                {
                    "number": 2,
                    "title": "PR two",
                    "body": "PR body",
                    "created_at": "2026-10-01T01:00:00Z",
                    "updated_at": "2026-10-05T02:00:00Z",
                    "html_url": "https://github.com/upiscium/Test/pull/2",
                    "state": "open",
                    "user": {"login": "upiscium"},
                    "pull_request": {
                        "url": "https://api.github.com/repos/upiscium/Test/pulls/2"
                    },
                },
            ]
        if path.endswith("/issues/1/events"):
            return [
                {
                    "id": 11,
                    "event": "closed",
                    "created_at": "2026-10-05T03:00:00Z",
                    "actor": {"login": "upiscium"},
                }
            ]
        if path.endswith("/issues/2/events"):
            return [
                {
                    "id": 21,
                    "event": "ready_for_review",
                    "created_at": "2026-10-05T03:30:00Z",
                    "actor": {"login": "upiscium"},
                },
                {
                    "id": 22,
                    "event": "convert_to_draft",
                    "created_at": "2026-10-05T04:00:00Z",
                    "actor": {"login": "upiscium"},
                },
                {
                    "id": 23,
                    "event": "merged",
                    "created_at": "2026-10-05T05:00:00Z",
                    "actor": {"login": "upiscium"},
                },
            ]
        if "/issues/comments?" in path:
            return [
                {
                    "id": 31,
                    "created_at": "2026-10-05T06:00:00Z",
                    "issue_url": "https://api.github.com/repos/upiscium/Test/issues/1",
                    "html_url": (
                        "https://github.com/upiscium/Test/issues/1#issuecomment-31"
                    ),
                    "body": "y" * (COMMENT_MAX_BYTES + 100),
                    "user": {"login": "upiscium"},
                },
                {
                    "id": 32,
                    "created_at": "2026-10-05T16:00:00Z",
                    "issue_url": "https://api.github.com/repos/upiscium/Test/issues/1",
                    "html_url": (
                        "https://github.com/upiscium/Test/issues/1#issuecomment-32"
                    ),
                    "body": "outside JST day",
                    "user": {"login": "upiscium"},
                },
            ]
        if "/pulls/comments?" in path:
            return [
                {
                    "id": 41,
                    "created_at": "2026-10-05T07:00:00Z",
                    "pull_request_url": (
                        "https://api.github.com/repos/upiscium/Test/pulls/2"
                    ),
                    "html_url": (
                        "https://github.com/upiscium/Test/pull/2#discussion_r41"
                    ),
                    "body": "review comment",
                    "user": {"login": "reviewer"},
                }
            ]
        if path.endswith("/pulls/2/reviews"):
            return [
                {
                    "id": 51,
                    "submitted_at": "2026-10-05T08:00:00Z",
                    "html_url": (
                        "https://github.com/upiscium/Test/pull/2"
                        "#pullrequestreview-51"
                    ),
                    "body": "LGTM",
                    "state": "APPROVED",
                    "user": {"login": "reviewer"},
                }
            ]
        if path.endswith("/pulls/2/commits"):
            return [
                {
                    "sha": "a" * 40,
                    "html_url": (
                        "https://github.com/upiscium/Test/commit/" + "a" * 40
                    ),
                    "author": {"login": "upiscium"},
                    "commit": {
                        "message": "feat: implement the thing",
                        "committer": {"date": "2026-10-05T09:00:00Z"},
                    },
                }
            ]
        if "/commits?" in path:
            return [
                {
                    "sha": "b" * 40,
                    "html_url": (
                        "https://github.com/upiscium/Test/commit/" + "b" * 40
                    ),
                    "author": {"login": "upiscium"},
                    "commit": {
                        "message": "merge the thing",
                        "committer": {"date": "2026-10-05T10:00:00Z"},
                    },
                }
            ]
        raise AssertionError(path)


class _EmptyRepositoryClient(_FakeClient):
    def _paged(self, path: str) -> list[dict[str, object]]:
        if "/commits?" in path:
            raise GitHubAPIHTTPError(
                status=409,
                path=path,
                detail='{"message":"Git Repository is empty."}',
            )
        return super()._paged(path)


def _write_project(vault: Path) -> None:
    path = vault / "10-Project" / "Test" / "Test.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "type: project\n"
        "status: running\n"
        "github_repo: upiscium/Test\n"
        "github_watch: true\n"
        "---\n",
        encoding="utf-8",
    )


def test_jst_date_window_is_exact() -> None:
    start, end = _date_window(date(2026, 10, 5))

    assert CANONICAL_TIMEZONE == "Asia/Tokyo"
    assert start.isoformat() == "2026-10-04T15:00:00+00:00"
    assert end.isoformat() == "2026-10-05T15:00:00+00:00"


def test_utf8_bound_is_byte_based_and_records_truncation() -> None:
    excerpt = _bounded_text("あ" * 10, limit=10, label="test")

    assert excerpt is not None
    assert excerpt.truncated is True
    assert excerpt.original_bytes == 30
    assert excerpt.included_bytes == 9
    assert excerpt.text == "あ" * 3


def test_collects_daily_github_activity_without_daily_event_cap(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write_project(vault)
    config = WatcherConfig(
        vault_root=vault,
        state_db=tmp_path / "state.sqlite3",
    )

    bundle = collect_daily_evidence(
        config,
        target_date=date(2026, 10, 5),
        client=_FakeClient(),
    )

    kinds = [event.kind for event in bundle.events]
    assert kinds == [
        "issue_created",
        "pull_request_snapshot",
        "issue_closed",
        "pull_request_ready_for_review",
        "pull_request_converted_to_draft",
        "pull_request_merged",
        "issue_comment",
        "pull_request_review_comment",
        "pull_request_review",
        "pull_request_commit",
        "default_branch_commit",
    ]

    issue = next(
        event
        for event in bundle.events
        if event.kind == "issue_created"
    )
    assert issue.body is not None
    assert issue.body.truncated is True
    assert issue.body.original_bytes == BODY_MAX_BYTES + 100
    assert issue.body.included_bytes == BODY_MAX_BYTES

    comment = next(
        event
        for event in bundle.events
        if event.kind == "issue_comment"
    )
    assert comment.body is not None
    assert comment.body.truncated is True
    assert comment.body.original_bytes == COMMENT_MAX_BYTES + 100
    assert comment.body.included_bytes == COMMENT_MAX_BYTES

    assert all(
        event.occurred_at < "2026-10-05T15:00:00Z"
        for event in bundle.events
    )


def test_empty_repository_contributes_binding_without_commit_evidence(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write_project(vault)
    config = WatcherConfig(
        vault_root=vault,
        state_db=tmp_path / "state.sqlite3",
    )

    bundle = collect_daily_evidence(
        config,
        target_date=date(2026, 10, 5),
        client=_EmptyRepositoryClient(),
    )

    assert bundle.repositories == ("upiscium/Test",)
    assert all(
        event.kind != "default_branch_commit"
        for event in bundle.events
    )


def test_bundle_keeps_arbitrary_event_count_and_is_deterministic() -> None:
    from obsidian_automation.github_daily_activity import _make_event

    excerpt = _bounded_text("x", limit=10, label="message")
    assert excerpt is not None
    start, _ = _date_window(date(2026, 10, 5))
    events = []

    for index in range(1000):
        events.append(
            _make_event(
                kind="default_branch_commit",
                repository="upiscium/Test",
                occurred_at=start,
                url=(
                    "https://github.com/upiscium/Test/commit/"
                    f"{index:040x}"
                ),
                actor="upiscium",
                entity_type="commit",
                number=None,
                source_id=f"{index:040x}",
                sha=f"{index:040x}",
                message=excerpt,
            )
        )

    first = make_daily_evidence_bundle(
        target_date=date(2026, 10, 5),
        projects=[
            ProjectBinding(
                "10-Project/Test/Test.md",
                "upiscium/Test",
            )
        ],
        events=list(reversed(events)),
    )
    second = make_daily_evidence_bundle(
        target_date=date(2026, 10, 5),
        projects=[
            ProjectBinding(
                "10-Project/Test/Test.md",
                "upiscium/Test",
            )
        ],
        events=events,
    )

    assert len(first.events) == 1000
    assert first.canonical_bytes == second.canonical_bytes
    assert first.sha256 == second.sha256


def test_persist_is_content_addressed_and_idempotent(
    tmp_path: Path,
) -> None:
    bundle = make_daily_evidence_bundle(
        target_date=date(2026, 10, 5),
        projects=[],
        events=[],
    )

    first = persist_daily_evidence(tmp_path, bundle)
    second = persist_daily_evidence(tmp_path, bundle)

    assert first == second
    assert first.name == (
        f"{bundle.sha256}.github-daily-evidence.json"
    )
    assert first.read_bytes() == bundle.canonical_bytes
    assert json.loads(
        first.read_text(encoding="utf-8")
    )["events"] == []


def test_project_scan_warning_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = WatcherConfig(
        vault_root=tmp_path,
        state_db=tmp_path / "state.sqlite3",
    )
    monkeypatch.setattr(
        "obsidian_automation.github_daily_activity.watcher.scan_projects",
        lambda *_args, **_kwargs: ([], ["broken binding"]),
    )

    with pytest.raises(
        GitHubDailyActivityError,
        match="scan is incomplete",
    ):
        collect_daily_evidence(
            config,
            target_date=date(2026, 10, 5),
            client=_FakeClient(),
        )
