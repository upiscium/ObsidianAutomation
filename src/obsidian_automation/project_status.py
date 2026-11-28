"""B3-lite step 1: decide Project ``status`` changes and edit exactly one line.

Design: docs/b3-status-lite-design.md. This module is pure (no network, Vault
or clock access beyond what the caller passes in). It reuses the existing
watcher's rule (``decide_status``) and adds the two things an unattended
status writer needs on top of it:

* ``apply_status_line`` changes only the ``status:`` line inside the front
  matter and refuses anything it cannot change with certainty, so the rest of
  the note stays byte for byte the same;
* ``plan_status_changes`` adds brakes: a minimum interval between two changes
  of the same note (so a project does not flip every day around the 7-day
  boundary) and a cap on changes per run.

Responses and logs built from these results are count-only; note paths and
repository names are never part of an error message here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from .github_project_watcher import (
    ProjectBinding, ProjectState, RepositorySnapshot, decide_status,
)

MIN_CHANGE_INTERVAL_SECONDS = 24 * 3600
MAX_CHANGES_PER_RUN = 10
# Statuses the automation may write. Terminal and "stopped" are human decisions.
AUTO_STATUSES = frozenset({"planning", "running", "stable"})


class StatusEditError(ValueError):
    """Fixed codes only: never note text."""


@dataclass(frozen=True)
class StatusChange:
    path: str
    repository: str
    old: str
    new: str
    reason: str


_KEY = re.compile(r"status[ \t]*:")


def apply_status_line(text: str, old: str, new: str) -> str:
    """Return ``text`` with the front matter ``status`` changed from ``old`` to ``new``.

    Refuses (``StatusEditError``) unless the note starts with a ``---`` front matter that
    has exactly one top-level ``status`` key whose plain value is ``old``. The line is
    rewritten in place: indentation-free key, a trailing ``# comment`` and the line ending
    are kept; nothing else in the note is touched.
    """
    if old not in AUTO_STATUSES or new not in AUTO_STATUSES or old == new:
        raise StatusEditError("status_not_allowed")
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        raise StatusEditError("no_front_matter")
    hits = []
    closed = False
    for index in range(1, len(lines)):
        line = lines[index]
        bare = line.rstrip("\r")
        if bare == "---":
            closed = True
            break
        if line[:1] not in ("", " ", "\t") and _KEY.match(line):
            hits.append(index)
    if not closed:
        raise StatusEditError("front_matter_not_closed")
    if len(hits) != 1:
        raise StatusEditError("status_key_not_unique")
    line = lines[hits[0]]
    ending = "\r" if line.endswith("\r") else ""
    body = line[:-1] if ending else line
    token = re.escape(old)
    match = re.fullmatch(
        r"(status[ \t]*:[ \t]*)(?:\"" + token + r"\"|'" + token + r"'|" + token + r")([ \t]*(?:#.*)?)", body)
    if match is None:
        raise StatusEditError("status_value_unexpected")
    raw = body[match.end(1):len(body) - len(match.group(2))]
    quote = raw[0] if raw[0] in "\"'" else ""
    lines[hits[0]] = f"{match.group(1)}{quote}{new}{quote}{match.group(2)}{ending}"
    result = "\n".join(lines)
    if result.count("\n") != text.count("\n") or len(result) - len(text) != len(new) - len(old):
        raise StatusEditError("edit_not_minimal")
    return result


def plan_status_changes(
    projects: Sequence[ProjectBinding],
    snapshots: Mapping[str, RepositorySnapshot],
    previous: Mapping[str, ProjectState],
    last_changed: Mapping[str, float],
    *, now: datetime, active_window_days: int = 7,
    min_interval_seconds: int = MIN_CHANGE_INTERVAL_SECONDS,
    max_changes: int = MAX_CHANGES_PER_RUN,
    decide: Callable[..., object] = decide_status,
) -> tuple[list[StatusChange], dict[str, int]]:
    """Changes to write now, and counts of why the others were not written.

    ``last_changed`` maps a note path to the unix time of this automation's last change.
    Order is stable (by path) so a capped run is deterministic.
    """
    moment = now.astimezone(timezone.utc)
    changes: list[StatusChange] = []
    held: dict[str, int] = {"unchanged": 0, "cooldown": 0, "no_snapshot": 0,
                            "not_automatic": 0, "run_cap": 0}
    for project in sorted(projects, key=lambda p: p.path):
        snapshot = snapshots.get(project.repository)
        if snapshot is None:
            held["no_snapshot"] += 1
            continue
        decision = decide(project, snapshot, previous.get(project.path),
                          now=moment, active_window_days=active_window_days)
        if decision.proposed_status == project.status:
            held["unchanged"] += 1
            continue
        if decision.proposed_status not in AUTO_STATUSES or project.status not in AUTO_STATUSES:
            held["not_automatic"] += 1
            continue
        since = moment.timestamp() - last_changed.get(project.path, float("-inf"))
        if since < min_interval_seconds:
            held["cooldown"] += 1
            continue
        if len(changes) >= max_changes:
            held["run_cap"] += 1
            continue
        changes.append(StatusChange(project.path, project.repository, project.status,
                                    decision.proposed_status, decision.reason))
    return changes, held
