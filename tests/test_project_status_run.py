"""B3-lite step 2b: a run end to end against synthetic WebDAV and a fake GitHub."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from obsidian_automation import github_project_watcher as watcher
from obsidian_automation.project_status_run import ChangeJournal, parse_project_note, run_once
from obsidian_automation.vault_layout import role_folder
from test_knowledge_proposal_publish import store  # noqa: F401 (fixture)
from test_project_status_vault import Vault

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
FOLDER = role_folder("project")


def note(status="planning", repo="a/b", watch="true", kind="project"):
    return (f"---\ntype: {kind}\nstatus: {status}\ngithub_repo: {repo}\ngithub_watch: {watch}\n"
            "---\n# T\nbody\n").encode()


class FakeGitHub:
    def __init__(self, days_ago=1, fail=()):
        self.days_ago, self.fail, self.calls = days_ago, set(fail), []

    def snapshot(self, repository, *, observed_at):
        self.calls.append(repository)
        if repository in self.fail:
            raise watcher.GitHubProjectWatcherError("synthetic")
        at = observed_at - timedelta(days=self.days_ago)
        return watcher.RepositorySnapshot(
            repository=repository, latest_commit_sha="s", latest_commit_at=at,
            open_issues=frozenset(), open_prs=frozenset(), observed_at=observed_at,
            recent_commit_sha="s", recent_commit_at=at)


@pytest.fixture
def vault():
    v = Vault()
    v.files[f"{FOLDER}/quiet.md"] = note("running", "a/b")      # recent commit -> stays running
    v.files[f"{FOLDER}/wake.md"] = note("planning", "a/b")      # recent commit -> running
    v.files[f"{FOLDER}/stopped.md"] = note("stopped")           # never touched
    v.files[f"{FOLDER}/off.md"] = note("planning", watch="false")
    v.files[f"{FOLDER}/other.md"] = note("planning", kind="project-note")
    v.files[f"{FOLDER}/bad.md"] = note("planning", repo="not a repo")
    return v


@pytest.mark.parametrize("text,expected", [
    (note().decode(), watcher.ProjectBinding("p.md", "a/b", "planning")),
    (note(watch="false").decode(), None),
    (note("done").decode(), None),
    (note(kind="project-note").decode(), None),
    (note(repo="bad repo").decode(), "invalid_repository"),
    (note("weird").decode(), "invalid_status"),
])
def test_parse_project_note(text, expected):
    assert parse_project_note("p.md", text) == expected


def test_a_dry_run_reports_counts_and_writes_nothing_to_the_vault(store, vault, tmp_path):
    before = dict(vault.files)
    report = run_once(shared=store, guard=None, client=FakeGitHub(), state_dir=tmp_path / "ps",
                      now=NOW, transport=vault)
    assert report["mode"] == "dry_run"
    assert (report["notes"], report["watched"], report["invalid"], report["repos"]) == (6, 2, 1, 1)
    assert report["planned"] == 1 and report["transitions"] == {"planning->running": 1}
    assert report["applied"] == 0 and vault.files == before
    assert not any(c[0] == "PUT" for c in vault.calls)
    blob = json.dumps(report)
    assert "a/b" not in blob and "wake" not in blob  # count-only


def test_apply_changes_one_line_journals_it_and_then_respects_the_cooldown(store, vault, tmp_path):
    state = tmp_path / "ps"
    report = run_once(shared=store, guard=None, client=FakeGitHub(), state_dir=state, now=NOW,
                      apply=True, transport=vault)
    assert report["applied"] == 1 and report["failed"] == {}
    wake = f"{FOLDER}/wake.md"
    assert vault.files[wake] == note("planning").replace(b"status: planning", b"status: running")
    assert vault.files[f"{FOLDER}/stopped.md"] == note("stopped")
    journal = ChangeJournal(state)
    assert journal.last_changed() == {wake: pytest.approx(journal.last_changed()[wake])}
    journal.close()
    # silence for 30 days would flip it back, but not within 24 hours
    later = run_once(shared=store, guard=None, client=FakeGitHub(days_ago=30), state_dir=state,
                     now=NOW + timedelta(hours=1), apply=True, transport=vault)
    assert later["held"]["cooldown"] == 1  # wake.md: changed an hour ago
    assert vault.files[wake] == note("planning").replace(b"status: planning", b"status: running")
    assert later["applied"] == 1  # quiet.md was never changed by us, so silence applies at once


def test_an_owner_edit_during_the_run_is_not_overwritten_and_is_counted(store, vault, tmp_path):
    wake = f"{FOLDER}/wake.md"
    owner = note("planning") + b"owner edit\n"

    def edit():
        vault.files[wake] = owner
    vault.before_put = edit
    report = run_once(shared=store, guard=None, client=FakeGitHub(), state_dir=tmp_path / "ps",
                      now=NOW, apply=True, transport=vault)
    assert report["applied"] == 0 and report["failed"] == {"note_changed": 1}
    assert vault.files[wake] == owner


def test_a_repository_that_cannot_be_read_is_counted_and_its_notes_are_left_alone(store, vault, tmp_path):
    report = run_once(shared=store, guard=None, client=FakeGitHub(fail={"a/b"}),
                      state_dir=tmp_path / "ps", now=NOW, apply=True, transport=vault)
    assert report["repo_errors"] == 1 and report["planned"] == 0 and report["applied"] == 0
    assert report["held"]["no_snapshot"] == 2


def test_the_run_cap_limits_changes_per_run(store, vault, tmp_path):
    for i in range(4):
        vault.files[f"{FOLDER}/more{i}.md"] = note("planning")
    report = run_once(shared=store, guard=None, client=FakeGitHub(), state_dir=tmp_path / "ps",
                      now=NOW, apply=True, transport=vault, max_changes=2)
    assert report["applied"] == 2 and report["held"]["run_cap"] == 3
