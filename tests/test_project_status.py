from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from obsidian_automation.github_project_watcher import (
    ProjectBinding, ProjectState, RepositorySnapshot,
)
from obsidian_automation.project_status import (
    StatusEditError, apply_status_line, plan_status_changes,
)

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)


def note(status_line="status: running", extra=""):
    return ("---\ntype: project\n" + status_line + "\ngithub_repo: a/b\ngithub_watch: true\n"
            + extra + "---\n# Title\nstatus: running in the body\n")


@pytest.mark.parametrize("line,old,new,expected", [
    ("status: running", "running", "planning", "status: planning"),
    ("status:running", "running", "stable", "status:stable"),
    ('status: "running"', "running", "planning", 'status: "planning"'),
    ("status: 'stable'", "stable", "running", "status: 'running'"),
    ("status: running  # keep", "running", "planning", "status: planning  # keep"),
])
def test_only_the_status_line_changes(line, old, new, expected):
    before = note(line)
    after = apply_status_line(before, old, new)
    assert after == before.replace(line, expected, 1)
    assert after.endswith("status: running in the body\n")  # body text is never touched


def test_crlf_and_no_trailing_newline_are_preserved():
    before = "---\r\ntype: project\r\nstatus: running\r\n---\r\nbody"
    assert apply_status_line(before, "running", "planning") == before.replace("running", "planning", 1)


@pytest.mark.parametrize("text,code", [
    ("no front matter\nstatus: running\n", "no_front_matter"),
    ("---\nstatus: running\n", "front_matter_not_closed"),
    ("---\ntype: project\n---\n", "status_key_not_unique"),
    ("---\nstatus: running\nstatus: running\n---\n", "status_key_not_unique"),
    ("---\nstatus: stable\n---\n", "status_value_unexpected"),       # not what we decided from
    ("---\nstatus: running extra\n---\n", "status_value_unexpected"),
    ('---\nstatus: "running\n---\n', "status_value_unexpected"),
])
def test_it_refuses_what_it_cannot_change_with_certainty(text, code):
    with pytest.raises(StatusEditError, match=code):
        apply_status_line(text, "running", "planning")


@pytest.mark.parametrize("old,new", [("running", "done"), ("running", "stopped"), ("done", "running"),
                                     ("running", "running"), ("bogus", "planning")])
def test_terminal_stopped_and_unknown_statuses_are_never_written(old, new):
    with pytest.raises(StatusEditError, match="status_not_allowed"):
        apply_status_line(note("status: " + old), old, new)


def project(path, status, repo="a/b"):
    return ProjectBinding(path=path, repository=repo, status=status)


def snap(repo="a/b", commit_days_ago=1):
    at = NOW - timedelta(days=commit_days_ago) if commit_days_ago is not None else None
    return RepositorySnapshot(repository=repo, latest_commit_sha="x" if at else None,
                              latest_commit_at=at, open_issues=frozenset(), open_prs=frozenset(),
                              observed_at=NOW, recent_commit_sha="x" if at else None,
                              recent_commit_at=at)


def test_recent_commit_makes_planning_running_and_silence_makes_running_planning():
    changes, held = plan_status_changes(
        [project("p1.md", "planning"), project("p2.md", "running"), project("p3.md", "running")],
        {"a/b": snap(commit_days_ago=1)}, {}, {}, now=NOW)
    assert [(c.path, c.old, c.new) for c in changes] == [("p1.md", "planning", "running")]
    assert held["unchanged"] == 2
    changes, _ = plan_status_changes([project("p2.md", "running")],
                                     {"a/b": snap(commit_days_ago=30)}, {}, {}, now=NOW)
    assert [(c.old, c.new) for c in changes] == [("running", "planning")]


def test_a_note_is_not_changed_twice_within_the_cooldown():
    projects = [project("p.md", "planning")]
    changes, held = plan_status_changes(projects, {"a/b": snap()}, {},
                                        {"p.md": NOW.timestamp() - 3600}, now=NOW)
    assert changes == [] and held["cooldown"] == 1
    changes, _ = plan_status_changes(projects, {"a/b": snap()}, {},
                                     {"p.md": NOW.timestamp() - 25 * 3600}, now=NOW)
    assert len(changes) == 1


def test_missing_snapshot_and_run_cap_are_counted_not_raised():
    projects = [project(f"p{i:02}.md", "planning") for i in range(5)] + [project("q.md", "planning", "c/d")]
    changes, held = plan_status_changes(projects, {"a/b": snap()}, {}, {}, now=NOW, max_changes=3)
    assert [c.path for c in changes] == ["p00.md", "p01.md", "p02.md"]  # deterministic order
    assert held["run_cap"] == 2 and held["no_snapshot"] == 1


def test_decisions_outside_the_automatic_set_are_held():
    class Decision:
        proposed_status, reason, pending = "done", "x", True
    changes, held = plan_status_changes([project("p.md", "running")], {"a/b": snap()}, {}, {},
                                        now=NOW, decide=lambda *a, **k: Decision())
    assert changes == [] and held["not_automatic"] == 1
