"""Disposable source-only tests: no live GitHub/Nextcloud/Vault effects."""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from obsidian_automation.github_b3_watcher_bridge import (
    VERSION, collect_b3_read_only,
)
from obsidian_automation.github_project_watcher import (
    RepositorySnapshot, WatcherConfig,
)


class Source:
    def __init__(self, root: Path, *, token: str = "fake-token",
                 configured: bool = True):
        self.root = root
        self.token = token
        self.configured = configured

    @contextmanager
    def project_source(self):
        if not self.configured:
            raise ValueError("private source unavailable: do not leak")
        yield WatcherConfig(
            vault_root=self.root,
            state_db=self.root / "B4-must-not-be-used.sqlite3",
        ), self.token


def private(tmp_path: Path) -> Path:
    state = tmp_path / "b3"
    state.mkdir(mode=0o700)
    return state


def fake_watcher(config, *, client, stdout, stderr):
    assert config.state_db.parent.name == "b3"
    assert config.state_db.name == "watcher.sqlite3"
    assert client.token == "fake-token"
    print(json.dumps({"event": "project-status-observation", "pending": True,
                      "project": "private-note.md"}), file=stdout)
    print(json.dumps({"event": "project-status-observation", "pending": False}),
          file=stdout)
    return 0


class FakeClient:
    def __init__(self, *, token, timeout_seconds, active_window_days):
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.active_window_days = active_window_days


def invoke(source: Source, root: Path, *, watcher=fake_watcher,
           client_factory=FakeClient):
    return collect_b3_read_only(
        source, state_root=root, watcher=watcher,
        client_factory=client_factory,
    )


def test_valid_read_only_aggregate_redacts_project_detail(tmp_path):
    state = private(tmp_path)
    result = invoke(Source(tmp_path), state)
    assert result == {
        "schema_version": VERSION, "state": "observed",
        "reason": "local_read_only_observation",
        "observations": 2, "pending_proposals": 1,
        "vault_write_authorized": False, "cutover_authorized": False,
    }
    assert "private-note.md" not in str(result)
    assert (state / "watcher.lock").stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "B4-must-not-be-used.sqlite3").exists()


@pytest.mark.parametrize("bad", ["absent", "world_readable", "symlink",
                                 "database_symlink", "file_instead_dir"])
def test_refuse_unsafe_local_state(tmp_path, bad):
    root = private(tmp_path)
    if bad == "absent":
        root.rmdir()
    elif bad == "world_readable":
        root.chmod(0o755)
    elif bad == "symlink":
        root.rmdir()
        root.symlink_to(tmp_path, target_is_directory=True)
    elif bad == "database_symlink":
        (root / "watcher.sqlite3").symlink_to(tmp_path / "untrusted")
    else:
        root.rmdir()
        root.write_text("not a directory")
    result = invoke(Source(tmp_path), root)
    assert result["state"] == "unknown"
    assert result["observations"] == 0
    assert result["cutover_authorized"] is False


def test_bounded_flock_serializes_two_pollers(tmp_path):
    root = private(tmp_path)
    fd = os.open(root / "watcher.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert invoke(Source(tmp_path), root)["reason"] == "watcher_already_running"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert invoke(Source(tmp_path), root)["state"] == "observed"


def test_source_failure_is_redacted(tmp_path):
    result = invoke(Source(tmp_path, configured=False), private(tmp_path))
    assert result["state"] == "unknown"
    assert "private source" not in str(result)
    assert result["vault_write_authorized"] is False


@pytest.mark.parametrize("bad_watcher", [
    lambda c, **kw: 1,
    lambda c, **kw: True,
    lambda c, **kw: 0,
])
def test_no_verified_result_from_incomplete_or_missing_watcher(tmp_path, bad_watcher):
    result = invoke(Source(tmp_path), private(tmp_path), watcher=bad_watcher)
    assert result["state"] == "unknown"
    assert result["observations"] == 0


def test_partial_watcher_error_does_not_claim_all_observed(tmp_path):
    def partial(config, *, client, stdout, stderr):
        print(json.dumps({"event": "project-status-observation",
                          "pending": True}), file=stdout)
        print("private /secrets/project.md: unauthorized", file=stderr)
        return 0
    result = invoke(Source(tmp_path), private(tmp_path), watcher=partial)
    assert result["state"] == "unknown"
    assert "secrets" not in str(result)
    assert result["pending_proposals"] == 0


@pytest.mark.parametrize("line", ['not-json',
    json.dumps({"event": "error", "pending": True}),
    json.dumps({"event": "project-status-observation", "pending": "yes"})])
def test_noncanonical_observation_never_succeeds(tmp_path, line):
    def untrusted(config, *, client, stdout, stderr):
        print(line, file=stdout)
        return 0
    result = invoke(Source(tmp_path), private(tmp_path), watcher=untrusted)
    assert result["state"] == "unknown"


def test_client_failure_is_unknown_and_private(tmp_path):
    def broken(**kwargs):
        raise ValueError("fake-token and private URL")
    result = invoke(Source(tmp_path), private(tmp_path), client_factory=broken)
    assert result["state"] == "unknown"
    assert "fake-token" not in str(result)


def test_real_watcher_function_with_synthetic_files_and_fake_github(tmp_path):
    root = private(tmp_path)
    notes = tmp_path / "10-Project"
    notes.mkdir()
    (notes / "demo.md").write_text(
        "---\ntype: project\nstatus: planning\ngithub_watch: true\n"
        "github_repo: example/demo\n---\n# Demo\n", encoding="utf-8")
    class Client(FakeClient):
        def snapshot(self, repository, *, observed_at):
            assert repository == "example/demo"
            return RepositorySnapshot(
                repository=repository, latest_commit_sha="a" * 40,
                latest_commit_at=observed_at,
                open_issues=frozenset({1}), open_prs=frozenset(),
                observed_at=observed_at,
            )
    result = invoke(Source(tmp_path), root, watcher=None or __import__(
        "obsidian_automation.github_project_watcher",
        fromlist=["run_once"]).run_once, client_factory=Client)
    assert result["state"] == "observed"
    assert result["observations"] == 1
    assert result["pending_proposals"] == 1
    db = sqlite3.connect("file:" + str(root / "watcher.sqlite3") + "?mode=ro",
                         uri=True)
    db.execute("PRAGMA query_only=ON")
    assert db.execute("SELECT count(*) FROM project_state").fetchone() == (1,)
    db.close()
    assert not (tmp_path / "B4-must-not-be-used.sqlite3").exists()


def test_runtime_registration_is_observation_only_without_writer(tmp_path):
    source = Path(__file__).resolve().parents[1] / "src" / "obsidian_automation"
    app = (source / "snapshot_app.py").read_text()
    dashboard = (source / "admin_dashboard.py").read_text()
    assert "github_b3_watcher_bridge" in app
    assert "B3ObserverJob" in app
    assert "github_b3_watcher_bridge" not in dashboard
    assert "run_b3_writer_once" not in app
    assert "apply_project_status" not in app
    assert "apply_project_overview" not in app


def test_huge_undisclosed_watcher_output_is_bounded(tmp_path):
    def huge(config, *, client, stdout, stderr):
        print("sensitive" * 100000, file=stdout)
        return 0
    result = invoke(Source(tmp_path), private(tmp_path), watcher=huge)
    assert result["state"] == "unknown"
    assert result["observations"] == 0
    assert "sensitive" not in str(result)


def test_huge_error_detail_is_bounded_and_never_returned(tmp_path):
    def huge_error(config, *, client, stdout, stderr):
        print("private value" * 100000, file=stderr)
        return 1
    result = invoke(Source(tmp_path), private(tmp_path), watcher=huge_error)
    assert result["state"] == "unknown"
    assert "private" not in str(result)


def test_bounded_sink_counts_utf8_bytes_not_unicode_characters():
    from obsidian_automation.github_b3_watcher_bridge import _BoundedSink
    sink = _BoundedSink()
    with pytest.raises(ValueError, match="bounded_observation_exceeded"):
        sink.write("界" * 180000)
    assert sink.getvalue() == ""
    assert sink.write("界") == 1
    assert sink.getvalue() == "界"
