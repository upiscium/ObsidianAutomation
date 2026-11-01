from __future__ import annotations

import io
import json
from pathlib import Path

from obsidian_automation import github_project_watcher as watcher
from obsidian_automation.github_project_status_queue import run_and_enqueue
from obsidian_automation.github_project_watcher_runtime import GitHubClient


class _RuntimeClient(GitHubClient):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def _request_json(self, path: str) -> object:
        self.paths.append(path)
        if path.endswith("/commits?per_page=1"):
            return []
        if "/branches?per_page=" in path:
            return []
        if "/issues?state=open" in path:
            return [
                {"number": 10, "title": "Issue ten"},
                {
                    "number": 20,
                    "title": "PR shadow from issues endpoint",
                    "pull_request": {"url": "https://api.github.test/pulls/20"},
                },
            ]
        if "/pulls?state=open" in path:
            return [
                {
                    "number": 20,
                    "title": "PR twenty",
                    "draft": True,
                    "body": "Refs #10",
                }
            ]
        raise AssertionError(path)


def _write_project(root: Path, relative: str, *, repository: str = "upiscium/Test") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: project\n"
        "status: planning\n"
        f"github_repo: {repository}\n"
        "github_watch: true\n"
        "---\n",
        encoding="utf-8",
    )


def test_status_queue_reuses_exact_watcher_snapshot_for_overview(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_project(vault, "10-Project/Test/Test.md")
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    config = watcher.WatcherConfig(vault_root=vault, state_db=tmp_path / "state.sqlite3")
    client = _RuntimeClient()
    stdout = io.StringIO()
    stderr = io.StringIO()

    rc = run_and_enqueue(
        config,
        request_dir=request_dir,
        client=client,
        stdout=stdout,
        stderr=stderr,
    )

    assert rc == 0
    assert stderr.getvalue() == ""
    assert sum("/issues?state=open" in path for path in client.paths) == 1
    assert sum("/pulls?state=open" in path for path in client.paths) == 1
    assert sum("/commits?per_page=1" in path for path in client.paths) == 1
    assert len(client.paths) == 4
    assert sum("/branches?" in path for path in client.paths) == 1
    assert not any("/activity?" in path for path in client.paths)
    overview_files = list(request_dir.glob("*.github-overview.json"))
    assert len(overview_files) == 1
    payload = json.loads(overview_files[0].read_text(encoding="utf-8"))
    assert payload == {
        "event": "project-overview-desired",
        "project": "10-Project/Test/Test.md",
        "repository": "upiscium/Test",
        "issues": [{"number": 10, "title": "Issue ten"}],
        "pull_requests": [
            {
                "number": 20,
                "title": "PR twenty",
                "draft": True,
                "bound_issues": [{"repository": "upiscium/Test", "number": 10}],
            }
        ],
    }
    rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert any(row.get("event") == "project-status-observation" for row in rows)
    assert any(row.get("event") == "project-overview-enqueued" for row in rows)
    assert not list(request_dir.glob("*.github-status.json"))


def test_overview_request_is_stable_when_snapshot_is_unchanged(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_project(vault, "10-Project/Test/Test.md")
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    config = watcher.WatcherConfig(vault_root=vault, state_db=tmp_path / "state.sqlite3")

    first_out = io.StringIO()
    first = run_and_enqueue(
        config,
        request_dir=request_dir,
        client=_RuntimeClient(),
        stdout=first_out,
        stderr=io.StringIO(),
    )
    before = next(request_dir.glob("*.github-overview.json")).read_bytes()

    second_out = io.StringIO()
    second = run_and_enqueue(
        config,
        request_dir=request_dir,
        client=_RuntimeClient(),
        stdout=second_out,
        stderr=io.StringIO(),
    )
    after = next(request_dir.glob("*.github-overview.json")).read_bytes()

    assert first == second == 0
    assert before == after
    events = [json.loads(line) for line in second_out.getvalue().splitlines()]
    overview = next(row for row in events if row.get("event") == "project-overview-enqueued")
    assert overview["queue_result"] == "already_queued"


def test_two_watched_projects_cannot_target_same_status_note(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_project(vault, "10-Project/A.md")
    _write_project(vault, "10-Project/B.md")
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    config = watcher.WatcherConfig(vault_root=vault, state_db=tmp_path / "state.sqlite3")
    stderr = io.StringIO()

    rc = run_and_enqueue(
        config,
        request_dir=request_dir,
        client=_RuntimeClient(),
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert rc == 1
    assert "multiple watched Projects target the same Status.md" in stderr.getvalue()


class _RecentFeatureRuntimeClient(_RuntimeClient):
    def _request_json(self, path: str) -> object:
        if path.endswith("/commits?per_page=1"):
            self.paths.append(path)
            return [{
                "sha": "a" * 40,
                "commit": {"committer": {"date": "2026-10-01T00:00:00Z"}},
            }]
        if path.endswith("/branches?per_page=100&page=1"):
            self.paths.append(path)
            return [
                {"name": "main", "commit": {"sha": "a" * 40}},
                {"name": "feature", "commit": {"sha": "b" * 40}},
            ]
        if path.endswith("/commits/" + "b" * 40):
            self.paths.append(path)
            return {
                "sha": "b" * 40,
                "commit": {"committer": {"date": "2026-10-31T00:00:00Z"}},
            }
        return super()._request_json(path)


def test_recent_feature_branch_flows_through_strict_queue_and_legacy_proposal(
    tmp_path: Path, monkeypatch,
) -> None:
    """A full synthetic Watcher -> Queue -> old Worker proposal contract."""
    from datetime import datetime, timezone
    from obsidian_automation.github_project_status_mutation import parse_watcher_proposal

    monkeypatch.setattr(
        watcher, "_utc_now", lambda: datetime(2026, 11, 1, tzinfo=timezone.utc)
    )
    vault = tmp_path / "vault"
    _write_project(vault, "10-Project/Test/Test.md")
    request_dir = tmp_path / "requests"
    request_dir.mkdir()
    config = watcher.WatcherConfig(
        vault_root=vault, state_db=tmp_path / "state.sqlite3"
    )

    outputs = []
    for run in (1, 2):
        client = _RecentFeatureRuntimeClient()
        stdout = io.StringIO()
        stderr = io.StringIO()
        rc = run_and_enqueue(
            config, request_dir=request_dir,
            client=client, stdout=stdout, stderr=stderr,
        )
        assert rc == 0, stderr.getvalue()
        assert "project-status-enqueue-error" not in stderr.getvalue()
        events = [json.loads(s) for s in stdout.getvalue().splitlines()]
        observation = next(
            event for event in events if event.get("event") == "project-status-observation"
        )
        assert observation["current_status"] == "planning"
        assert observation["proposed_status"] == "running"
        assert observation["latest_commit_sha"] == "a" * 40
        assert observation["recent_commit_sha"] == "b" * 40
        status = next(
            event for event in events if event.get("event") == "project-status-enqueued"
        )
        assert status["queue_result"] == ("queued" if run == 1 else "already_queued")
        assert sum("/issues?state=open" in path for path in client.paths) == 1
        assert sum("/pulls?state=open" in path for path in client.paths) == 1
        assert any(
            path.endswith("/commits/" + "b" * 40) for path in client.paths
        ) == (run == 1)
        outputs.append(status)

    requests = list(request_dir.glob("*.github-status.json"))
    assert len(requests) == 1
    proposal = parse_watcher_proposal(requests[0].read_bytes())
    assert proposal.expected_status == "planning"
    assert proposal.desired_status == "running"
    assert proposal.latest_commit_sha == "a" * 40
    assert "recent_commit_sha" not in json.loads(proposal.canonical_bytes)
    assert outputs[0]["proposal_sha256"] == outputs[1]["proposal_sha256"]
    assert len(list(request_dir.glob("*.github-overview.json"))) == 1
