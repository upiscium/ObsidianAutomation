from __future__ import annotations

from datetime import datetime, timezone

from obsidian_automation.github_project_watcher_runtime import (
    COMMIT_ACTIVITY_TYPES,
    GitHubClient,
)


class _StubClient(GitHubClient):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def _request_json(self, path: str) -> object:
        self.paths.append(path)
        if "activity_type=push" in path:
            return [{"after": "push-sha", "pushed_at": None}]
        if "activity_type=force_push" in path:
            return []
        if "activity_type=pr_merge" in path:
            return [{"after": "merge-sha", "timestamp": "2026-09-16T13:00:00Z"}]
        if "activity_type=merge_queue_merge" in path:
            return []
        if path.endswith("/commits/push-sha"):
            return {
                "commit": {
                    "committer": {"date": "2026-09-10T12:00:00Z"},
                    "author": {"date": "2026-09-10T11:59:00Z"},
                }
            }
        raise AssertionError(path)


class _PushedAtClient(GitHubClient):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def _request_json(self, path: str) -> object:
        self.paths.append(path)
        if "activity_type=push" in path:
            return [{"after": "push-sha", "pushed_at": "2026-09-15T12:00:00Z"}]
        if "activity_type=" in path:
            return []
        raise AssertionError(path)


def test_commit_activity_types_include_direct_and_merge_commits() -> None:
    assert COMMIT_ACTIVITY_TYPES == (
        "push",
        "force_push",
        "pr_merge",
        "merge_queue_merge",
    )


def test_latest_activity_uses_pr_merge_when_it_is_newest_commit() -> None:
    client = _StubClient()

    sha, committed_at = client._latest_push("upiscium/Test")

    assert sha == "merge-sha"
    assert committed_at == datetime(2026, 9, 16, 13, 0, tzinfo=timezone.utc)
    for activity_type in COMMIT_ACTIVITY_TYPES:
        assert any(f"activity_type={activity_type}" in path for path in client.paths)
    assert all(
        "time_period=year" in path
        for path in client.paths
        if "/activity?" in path
    )


def test_activity_without_timestamp_resolves_after_commit_metadata() -> None:
    client = _StubClient()

    sha, committed_at = client._latest_push("upiscium/Test")

    assert sha == "merge-sha"
    assert committed_at == datetime(2026, 9, 16, 13, 0, tzinfo=timezone.utc)
    assert "/repos/upiscium/Test/commits/push-sha" in client.paths


def test_documented_pushed_at_is_used_without_commit_lookup() -> None:
    client = _PushedAtClient()

    sha, committed_at = client._latest_push("upiscium/Test")

    assert sha == "push-sha"
    assert committed_at == datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    assert not any("/commits/" in path for path in client.paths)
