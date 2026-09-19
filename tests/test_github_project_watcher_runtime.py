from __future__ import annotations

from datetime import datetime, timezone

from obsidian_automation.github_project_watcher_runtime import GitHubClient


class _SnapshotClient(GitHubClient):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def _request_json(self, path: str) -> object:
        self.paths.append(path)
        if path == "/repos/upiscium/Test/commits?per_page=1":
            return [
                {
                    "sha": "head-sha",
                    "commit": {
                        "committer": {"date": "2026-09-16T13:00:00Z"},
                        "author": {"date": "2026-09-16T12:59:00Z"},
                    },
                }
            ]
        if path == "/repos/upiscium/Test/issues?state=open&per_page=100&page=1":
            return [
                {"number": 10, "title": "Issue ten"},
                {
                    "number": 20,
                    "title": "PR shadow",
                    "pull_request": {"url": "example"},
                },
            ]
        if path == "/repos/upiscium/Test/pulls?state=open&per_page=100&page=1":
            return [
                {
                    "number": 20,
                    "title": "PR twenty",
                    "draft": True,
                    "body": "Refs #10",
                }
            ]
        raise AssertionError(path)


class _AuthorTimestampClient(GitHubClient):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def _request_json(self, path: str) -> object:
        self.paths.append(path)
        if path == "/repos/upiscium/Test/commits?per_page=1":
            return [
                {
                    "sha": "author-sha",
                    "commit": {
                        "committer": {"date": None},
                        "author": {"date": "2026-09-13T10:20:00Z"},
                    },
                }
            ]
        raise AssertionError(path)


class _EmptyRepositoryClient(GitHubClient):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def _request_json(self, path: str) -> object:
        self.paths.append(path)
        if path == "/repos/upiscium/Test/commits?per_page=1":
            return []
        raise AssertionError(path)


def test_production_snapshot_uses_three_requests_per_single_page_repository() -> None:
    client = _SnapshotClient()
    observed = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)

    snapshot = client.snapshot("upiscium/Test", observed_at=observed)

    assert snapshot.latest_commit_sha == "head-sha"
    assert snapshot.latest_commit_at == datetime(
        2026, 9, 16, 13, 0, tzinfo=timezone.utc
    )
    assert snapshot.open_issues == frozenset({10})
    assert snapshot.open_prs == frozenset({20})
    assert snapshot.observed_at == observed
    assert client.paths == [
        "/repos/upiscium/Test/commits?per_page=1",
        "/repos/upiscium/Test/issues?state=open&per_page=100&page=1",
        "/repos/upiscium/Test/pulls?state=open&per_page=100&page=1",
    ]
    assert not any("/activity?" in path for path in client.paths)

    issues, pulls = client.overview_rows("upiscium/Test")
    assert tuple(row["number"] for row in issues) == (10, 20)
    assert tuple(row["number"] for row in pulls) == (20,)


def test_latest_default_branch_commit_falls_back_to_author_timestamp() -> None:
    client = _AuthorTimestampClient()

    sha, committed_at = client._latest_push("upiscium/Test")

    assert sha == "author-sha"
    assert committed_at == datetime(2026, 9, 13, 10, 20, tzinfo=timezone.utc)
    assert client.paths == ["/repos/upiscium/Test/commits?per_page=1"]


def test_empty_repository_remains_without_latest_commit() -> None:
    client = _EmptyRepositoryClient()

    sha, committed_at = client._latest_push("upiscium/Test")

    assert sha is None
    assert committed_at is None
    assert client.paths == ["/repos/upiscium/Test/commits?per_page=1"]
