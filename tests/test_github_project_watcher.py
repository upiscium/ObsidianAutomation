from __future__ import annotations

import io
import json
import ssl
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from obsidian_automation.github_project_watcher import (
    GitHubClient,
    GitHubProjectWatcherError,
    ProjectBinding,
    ProjectState,
    RepositorySnapshot,
    StateStore,
    WatcherConfig,
    decide_status,
    run_once,
    scan_projects,
)


NOW = datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc)


def _snapshot(
    *,
    sha: str | None = "abc",
    committed_at: datetime | None = None,
    issues: frozenset[int] = frozenset(),
    prs: frozenset[int] = frozenset(),
) -> RepositorySnapshot:
    return RepositorySnapshot(
        repository="upiscium/Test",
        latest_commit_sha=sha,
        latest_commit_at=committed_at,
        open_issues=issues,
        open_prs=prs,
        observed_at=NOW,
    )


def _state(
    *,
    status: str,
    sha: str | None = "old",
    committed_at: datetime | None = None,
    issues: frozenset[int] = frozenset(),
    prs: frozenset[int] = frozenset(),
    pending_status: str | None = None,
    pending_reason: str | None = None,
    repository: str = "upiscium/Test",
) -> ProjectState:
    return ProjectState(
        project_path="10-Project/Test.md",
        repository=repository,
        last_status=status,
        latest_commit_sha=sha,
        latest_commit_at=committed_at,
        open_issues=issues,
        open_prs=prs,
        observed_at=NOW - timedelta(minutes=15),
        pending_status=pending_status,
        pending_reason=pending_reason,
    )


def _project(status: str = "planning", *, repository: str = "upiscium/Test") -> ProjectBinding:
    return ProjectBinding(
        path="10-Project/Test.md",
        repository=repository,
        status=status,
    )


def test_scan_projects_requires_explicit_watch_and_valid_metadata(tmp_path: Path) -> None:
    project_dir = tmp_path / "10-Project"
    project_dir.mkdir()
    (project_dir / "Watched.md").write_text(
        "---\ntype: project\nstatus: planning\ngithub_repo: upiscium/Test\ngithub_watch: true\n---\n",
        encoding="utf-8",
    )
    (project_dir / "Ignored.md").write_text(
        "---\ntype: project\nstatus: running\ngithub_repo: upiscium/Ignored\n---\n",
        encoding="utf-8",
    )
    (project_dir / "Stopped.md").write_text(
        "---\ntype: project\nstatus: stopped\ngithub_repo: upiscium/Stopped\ngithub_watch: true\n---\n",
        encoding="utf-8",
    )
    (project_dir / "Done.md").write_text(
        "---\ntype: project\nstatus: done\ngithub_repo: upiscium/Done\ngithub_watch: true\n---\n",
        encoding="utf-8",
    )
    (project_dir / "Cancelled.md").write_text(
        "---\ntype: project\nstatus: cancelled\ngithub_repo: upiscium/Cancelled\ngithub_watch: true\n---\n",
        encoding="utf-8",
    )
    (project_dir / "Broken.md").write_text(
        "---\ntype: project\nstatus: unknown\ngithub_repo: not-a-repo\ngithub_watch: true\n---\n",
        encoding="utf-8",
    )

    projects, warnings = scan_projects(tmp_path)

    assert projects == [
        ProjectBinding(
            path="10-Project/Watched.md",
            repository="upiscium/Test",
            status="planning",
        )
    ]
    assert len(warnings) == 1
    assert "invalid github_repo" in warnings[0]


def test_recent_commit_makes_normal_project_running() -> None:
    decision = decide_status(
        _project("planning"),
        _snapshot(committed_at=NOW - timedelta(days=2)),
        None,
        now=NOW,
        active_window_days=7,
    )
    assert decision.proposed_status == "running"
    assert decision.pending is True


def test_stale_or_missing_commit_makes_normal_project_planning() -> None:
    stale = decide_status(
        _project("running"),
        _snapshot(committed_at=NOW - timedelta(days=8)),
        None,
        now=NOW,
        active_window_days=7,
    )
    missing = decide_status(
        _project("running"),
        _snapshot(sha=None, committed_at=None),
        None,
        now=NOW,
        active_window_days=7,
    )
    assert stale.proposed_status == "planning"
    assert missing.proposed_status == "planning"


def test_stopped_is_never_changed_by_github_activity() -> None:
    decision = decide_status(
        _project("stopped"),
        _snapshot(committed_at=NOW, issues=frozenset({1}), prs=frozenset({2})),
        _state(status="stopped", sha="old"),
        now=NOW,
        active_window_days=7,
    )
    assert decision.proposed_status == "stopped"
    assert decision.pending is False


def test_terminal_statuses_are_never_reactivated() -> None:
    for status in ("done", "cancelled"):
        decision = decide_status(
            _project(status),
            _snapshot(sha="new", committed_at=NOW, issues=frozenset({1}), prs=frozenset({2})),
            _state(status=status, sha="old"),
            now=NOW,
            active_window_days=7,
        )
        assert decision.proposed_status == status
        assert decision.pending is False


def test_stable_first_observation_only_initializes_baseline() -> None:
    decision = decide_status(
        _project("stable"),
        _snapshot(committed_at=NOW, issues=frozenset({1}), prs=frozenset({2})),
        None,
        now=NOW,
        active_window_days=7,
    )
    assert decision.proposed_status == "stable"
    assert "baseline" in decision.reason


def test_entering_stable_resets_baseline_before_reactivation() -> None:
    decision = decide_status(
        _project("stable"),
        _snapshot(sha="new", committed_at=NOW, issues=frozenset({3})),
        _state(status="running", sha="old", committed_at=NOW - timedelta(days=1)),
        now=NOW,
        active_window_days=7,
    )
    assert decision.proposed_status == "stable"
    assert "baseline" in decision.reason


def test_new_commit_after_stable_baseline_reactivates_as_running() -> None:
    decision = decide_status(
        _project("stable"),
        _snapshot(sha="new", committed_at=NOW),
        _state(status="stable", sha="old", committed_at=NOW - timedelta(days=2)),
        now=NOW,
        active_window_days=7,
    )
    assert decision.proposed_status == "running"
    assert decision.pending is True


def test_new_open_issue_or_pr_after_stable_baseline_reactivates_as_planning() -> None:
    previous = _state(status="stable", issues=frozenset({1}), prs=frozenset({10}))
    decision = decide_status(
        _project("stable"),
        _snapshot(
            sha="old",
            committed_at=previous.latest_commit_at,
            issues=frozenset({1, 2}),
            prs=frozenset({10, 11}),
        ),
        previous,
        now=NOW,
        active_window_days=7,
    )
    assert decision.proposed_status == "planning"
    assert "issues=2" in decision.reason
    assert "prs=11" in decision.reason


def test_pending_proposal_repeats_until_canonical_status_changes() -> None:
    previous = _state(
        status="stable",
        sha="new",
        committed_at=NOW,
        pending_status="running",
        pending_reason="new commit observed after stable baseline",
    )
    decision = decide_status(
        _project("stable"),
        _snapshot(sha="new", committed_at=NOW),
        previous,
        now=NOW + timedelta(minutes=15),
        active_window_days=7,
    )
    assert decision.proposed_status == "running"
    assert decision.pending is True


def test_state_store_round_trips_pending_status(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite3"
    project = _project("stable")
    snapshot = _snapshot(
        sha="sha",
        committed_at=NOW,
        issues=frozenset({3, 1}),
        prs=frozenset({7}),
    )
    with StateStore(db) as store:
        store.save(
            project,
            snapshot,
            pending_status="running",
            pending_reason="new commit",
        )
        loaded = store.load(project.path)
    assert loaded is not None
    assert loaded.repository == "upiscium/Test"
    assert loaded.open_issues == frozenset({1, 3})
    assert loaded.open_prs == frozenset({7})
    assert loaded.pending_status == "running"


class _FakeClient:
    def __init__(self, snapshot: RepositorySnapshot) -> None:
        self.value = snapshot
        self.calls: list[str] = []

    def snapshot(self, repository: str, *, observed_at: datetime | None = None) -> RepositorySnapshot:
        self.calls.append(repository)
        return RepositorySnapshot(
            repository=repository,
            latest_commit_sha=self.value.latest_commit_sha,
            latest_commit_at=self.value.latest_commit_at,
            open_issues=self.value.open_issues,
            open_prs=self.value.open_prs,
            observed_at=observed_at or self.value.observed_at,
        )


def test_run_once_fetches_shared_repository_once_and_emits_json(tmp_path: Path) -> None:
    project_dir = tmp_path / "vault" / "10-Project"
    project_dir.mkdir(parents=True)
    for name in ("A", "B"):
        (project_dir / f"{name}.md").write_text(
            "---\ntype: project\nstatus: planning\ngithub_repo: upiscium/Test\ngithub_watch: true\n---\n",
            encoding="utf-8",
        )
    config = WatcherConfig(
        vault_root=tmp_path / "vault",
        state_db=tmp_path / "state.sqlite3",
    )
    client = _FakeClient(_snapshot(committed_at=NOW - timedelta(days=1)))
    stdout = io.StringIO()
    stderr = io.StringIO()

    result = run_once(config, client=client, now=NOW, stdout=stdout, stderr=stderr)

    assert result == 0
    assert client.calls == ["upiscium/Test"]
    rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(rows) == 2
    assert all(row["proposed_status"] == "running" for row in rows)
    assert stderr.getvalue() == ""


class _HTTPResponseFixture:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self.data


def test_github_client_retries_ssl_eof_once_with_tls12(monkeypatch) -> None:
    calls: list[object | None] = []

    def fake_urlopen(request, **kwargs):
        context = kwargs.get("context")
        calls.append(context)
        if len(calls) == 1:
            raise urllib.error.URLError(
                ssl.SSLEOFError(
                    8,
                    "EOF occurred in violation of protocol",
                )
            )
        assert isinstance(context, ssl.SSLContext)
        assert context.maximum_version is ssl.TLSVersion.TLSv1_2
        return _HTTPResponseFixture(b'{"ok":true}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = GitHubClient()
    value = client._request_json("/repos/upiscium/Test")

    assert value == {"ok": True}
    assert calls[0] is None
    assert len(calls) == 2


def test_github_client_does_not_downgrade_non_tls_eof_error(monkeypatch) -> None:
    calls = 0

    def fake_urlopen(request, **kwargs):
        nonlocal calls
        calls += 1
        raise urllib.error.URLError("temporary DNS failure")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    client = GitHubClient()
    try:
        client._request_json("/repos/upiscium/Test")
    except GitHubProjectWatcherError as exc:
        assert "temporary DNS failure" in str(exc)
        assert "TLS 1.2 compatibility retry" not in str(exc)
    else:
        raise AssertionError("non-TLS transport failure was not propagated")

    assert calls == 1
