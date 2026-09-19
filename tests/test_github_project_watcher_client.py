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
        if "/issues?state=open" in path:
            return [
                {"number": 1},
                {"number": 2, "pull_request": {"url": "example"}},
            ]
        if "/pulls?state=open" in path:
            return [{"number": 2}]
        raise AssertionError(path)


def test_snapshot_uses_default_branch_head_with_three_requests() -> None:
    client = _StubClient()
    observed = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)

    snapshot = client.snapshot("upiscium/Test", observed_at=observed)

    assert snapshot.latest_commit_sha == "head-sha"
    assert snapshot.latest_commit_at == datetime(2026, 9, 16, 13, 0, tzinfo=timezone.utc)
    assert snapshot.open_issues == frozenset({1})
    assert snapshot.open_prs == frozenset({2})
    assert snapshot.observed_at == observed
    assert len(client.paths) == 3
    assert client.paths[0] == "/repos/upiscium/Test/commits?per_page=1"
    assert not any("/activity?" in path for path in client.paths)


def test_nonterminal_pending_is_recomputed_from_fresh_snapshot() -> None:
    now = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)
    project = ProjectBinding(
        path="10-Project/Test.md",
        repository="upiscium/Test",
        status="running",
    )
    previous = ProjectState(
        project_path=project.path,
        repository=project.repository,
        last_status="running",
        latest_commit_sha="old",
        latest_commit_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        open_issues=frozenset(),
        open_prs=frozenset(),
        observed_at=datetime(2026, 9, 17, 2, 45, tzinfo=timezone.utc),
        pending_status="planning",
        pending_reason="no commit within 7 days",
    )
    snapshot = RepositorySnapshot(
        repository=project.repository,
        latest_commit_sha="new",
        latest_commit_at=datetime(2026, 9, 17, 2, 55, tzinfo=timezone.utc),
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

    assert decision.proposed_status == "running"
    assert decision.pending is False


def test_terminal_pending_planning_is_upgraded_by_new_commit() -> None:
    now = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)
    project = ProjectBinding(
        path="10-Project/Test.md",
        repository="upiscium/Test",
        status="done",
    )
    previous = ProjectState(
        project_path=project.path,
        repository=project.repository,
        last_status="done",
        latest_commit_sha="old",
        latest_commit_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        open_issues=frozenset({1, 2}),
        open_prs=frozenset(),
        observed_at=datetime(2026, 9, 17, 2, 45, tzinfo=timezone.utc),
        pending_status="planning",
        pending_reason="new open GitHub activity after terminal baseline: issues=2",
    )
    snapshot = RepositorySnapshot(
        repository=project.repository,
        latest_commit_sha="new",
        latest_commit_at=datetime(2026, 9, 17, 2, 55, tzinfo=timezone.utc),
        open_issues=frozenset({1, 2}),
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

    assert decision.proposed_status == "running"
    assert decision.pending is True


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


def test_terminal_project_reactivates_when_head_sha_changes_to_older_commit() -> None:
    now = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)
    project = ProjectBinding(
        path="10-Project/Test.md",
        repository="upiscium/Test",
        status="done",
    )
    previous = ProjectState(
        project_path=project.path,
        repository=project.repository,
        last_status="done",
        latest_commit_sha="newer-head",
        latest_commit_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
        open_issues=frozenset(),
        open_prs=frozenset(),
        observed_at=datetime(2026, 9, 17, 2, 45, tzinfo=timezone.utc),
        pending_status=None,
        pending_reason=None,
    )
    snapshot = RepositorySnapshot(
        repository=project.repository,
        latest_commit_sha="older-head-after-force-push",
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

    assert decision.proposed_status == "running"
    assert decision.pending is True
    assert decision.reason == "new commit observed after terminal baseline"
