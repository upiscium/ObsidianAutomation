from __future__ import annotations

from datetime import datetime, timezone

from obsidian_automation.github_project_watcher import (
    GitHubClient,
    ProjectBinding,
    ProjectState,
    RepositorySnapshot,
    decide_status,
)


class _StubClient(GitHubClient):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def _request_json(self, path: str) -> object:
        self.paths.append(path)
        if "activity?activity_type=push" in path:
            return [{"after": "feature-sha", "pushed_at": "2026-09-16T12:00:00Z"}]
        if "activity?activity_type=force_push" in path:
            return [{"after": "force-sha", "pushed_at": "2026-09-16T13:00:00Z"}]
        if "/issues?state=open" in path:
            return [
                {"number": 1},
                {"number": 2, "pull_request": {"url": "example"}},
            ]
        if "/pulls?state=open" in path:
            return [{"number": 2}]
        raise AssertionError(path)


def test_snapshot_uses_latest_repository_push_across_refs() -> None:
    client = _StubClient()
    observed = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)

    snapshot = client.snapshot("upiscium/Test", observed_at=observed)

    assert snapshot.latest_commit_sha == "force-sha"
    assert snapshot.latest_commit_at == datetime(2026, 9, 16, 13, 0, tzinfo=timezone.utc)
    assert snapshot.open_issues == frozenset({1})
    assert snapshot.open_prs == frozenset({2})
    assert snapshot.observed_at == observed
    activity_paths = [path for path in client.paths if "/activity?" in path]
    assert len(activity_paths) == 2
    assert all("time_period=year" in path for path in activity_paths)
    assert any("activity_type=push" in path for path in activity_paths)
    assert any("activity_type=force_push" in path for path in activity_paths)


def test_pending_transition_is_discarded_when_repository_binding_changes() -> None:
    now = datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc)
    project = ProjectBinding(
        path="10-Project/Test.md",
        repository="upiscium/NewRepo",
        status="planning",
    )
    previous = ProjectState(
        project_path=project.path,
        repository="upiscium/OldRepo",
        last_status="planning",
        latest_commit_sha="old",
        latest_commit_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        open_issues=frozenset(),
        open_prs=frozenset(),
        observed_at=datetime(2026, 9, 16, 14, 45, tzinfo=timezone.utc),
        pending_status="running",
        pending_reason="old repository proposal",
    )
    snapshot = RepositorySnapshot(
        repository=project.repository,
        latest_commit_sha="new",
        latest_commit_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        open_issues=frozenset(),
        open_prs=frozenset(),
        observed_at=now,
    )

    decision = decide_status(
        project,
        snapshot,
        previous,
        now=now,
        active_window_days=7,
    )

    assert decision.proposed_status == "planning"
    assert decision.pending is False
