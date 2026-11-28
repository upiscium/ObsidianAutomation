"""B3-lite step 2b: one run (read notes, read GitHub, decide, optionally write) and a CLI.

Design: docs/b3-status-lite-design.md. A run is *dry* unless ``apply=True``: a dry run
reads the Vault and GitHub and reports counts, and writes nothing to the Vault. The only
local writes are the watcher's observation state (GitHub activity baselines) and, when
applying, the change journal, both under the private state directory.

The report is count-only: no note paths, repository names or note text.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from . import github_project_watcher as watcher
from .project_status import (
    MAX_CHANGES_PER_RUN, MIN_CHANGE_INTERVAL_SECONDS, StatusChange, plan_status_changes,
)
from .project_status_vault import (
    NoteRead, ProjectVaultError, list_project_notes, read_note, set_status,
)

APPLIED = ("updated", "recovered")


def parse_project_note(path: str, text: str) -> watcher.ProjectBinding | str | None:
    """A binding for a watched, valid Project note; ``None`` for a note that is simply not
    watched (other type, ``github_watch`` off, stopped/done/cancelled); a fixed warning code
    for a watched note that cannot be used."""
    fm = watcher._frontmatter_scalars(text)
    if fm.get("type") != "project" or not watcher._watch_enabled(fm.get("github_watch", "")):
        return None
    repository, status = fm.get("github_repo", "").strip(), fm.get("status", "").strip()
    if not watcher._REPOSITORY_RE.fullmatch(repository):
        return "invalid_repository"
    if status not in watcher.VALID_STATUSES:
        return "invalid_status"
    if status in watcher.UNWATCHED_STATUSES:
        return None
    return watcher.ProjectBinding(path=path, repository=repository, status=status)


class ChangeJournal:
    """Every change this automation made, in the private state directory."""

    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "changes.sqlite3")
        self.db.execute("CREATE TABLE IF NOT EXISTS changes (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                        " path TEXT NOT NULL, old TEXT NOT NULL, new TEXT NOT NULL, reason TEXT,"
                        " outcome TEXT NOT NULL, at REAL NOT NULL)")
        self.db.commit()

    def record(self, change: StatusChange, outcome: str, at: float) -> None:
        with self.db:
            self.db.execute("INSERT INTO changes (path,old,new,reason,outcome,at) VALUES (?,?,?,?,?,?)",
                            (change.path, change.old, change.new, change.reason, outcome, at))

    def last_changed(self) -> dict[str, float]:
        rows = self.db.execute(
            "SELECT path, MAX(at) FROM changes WHERE outcome IN ('updated','recovered') GROUP BY path")
        return {path: at for path, at in rows}

    def changes_today(self, since: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM changes WHERE outcome IN ('updated','recovered')"
                               " AND at>=?", (since,)).fetchone()[0]

    def close(self) -> None:
        self.db.close()


def run_once(
    *, shared, guard: Callable[[], object] | None, client, state_dir: Path,
    now: datetime | None = None, apply: bool = False, transport=None,
    active_window_days: int = 7, max_changes: int = MAX_CHANGES_PER_RUN,
    min_interval_seconds: int = MIN_CHANGE_INTERVAL_SECONDS,
) -> dict[str, object]:
    """One pass. Raises ``ProjectVaultError`` only when the Vault cannot be listed."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    paths = list_project_notes(shared=shared, guard=guard, transport=transport)
    projects: list[watcher.ProjectBinding] = []
    reads: dict[str, NoteRead] = {}
    report: dict[str, object] = {"mode": "apply" if apply else "dry_run", "notes": len(paths),
                                 "watched": 0, "invalid": 0, "unreadable": 0, "repos": 0,
                                 "repo_errors": 0}
    for path in paths:
        try:
            note = read_note(path, shared=shared, guard=guard, transport=transport)
            text = note.content.decode("utf-8")
        except (ProjectVaultError, UnicodeDecodeError):
            report["unreadable"] += 1  # type: ignore[operator]
            continue
        parsed = parse_project_note(path, text)
        if parsed is None:
            continue
        if isinstance(parsed, str):
            report["invalid"] += 1  # type: ignore[operator]
            continue
        projects.append(parsed)
        reads[path] = note
    report["watched"] = len(projects)

    snapshots: dict[str, watcher.RepositorySnapshot] = {}
    for repository in sorted({p.repository for p in projects}):
        try:
            snapshots[repository] = client.snapshot(repository, observed_at=moment)
        except (watcher.GitHubProjectWatcherError, OSError, ValueError):
            report["repo_errors"] += 1  # type: ignore[operator]
    report["repos"] = len(snapshots)

    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    journal = ChangeJournal(state_dir)
    try:
        with watcher.StateStore(state_dir / "watcher.sqlite3") as store:
            if isinstance(client, watcher.GitHubClient):
                client.branch_commit_store = store
            previous = {p.path: s for p in projects if (s := store.load(p.path)) is not None}
            changes, held = plan_status_changes(
                projects, snapshots, previous, journal.last_changed(), now=moment,
                active_window_days=active_window_days, max_changes=max_changes,
                min_interval_seconds=min_interval_seconds)
            planned = {c.path: c for c in changes}
            for project in projects:  # baselines advance like the old watcher, applied or not
                snapshot = snapshots.get(project.repository)
                if snapshot is None:
                    continue
                change = planned.get(project.path)
                store.save(project, snapshot,
                           pending_status=change.new if change else None,
                           pending_reason=change.reason if change else None)
        transitions: dict[str, int] = {}
        for change in changes:
            key = f"{change.old}->{change.new}"
            transitions[key] = transitions.get(key, 0) + 1
        report.update({"planned": len(changes), "transitions": transitions, "held": held,
                       "applied": 0, "failed": {}})
        if not apply:
            return report
        failed: dict[str, int] = report["failed"]  # type: ignore[assignment]
        for change in changes:
            try:
                result = set_status(change.path, change.old, change.new, shared=shared, guard=guard,
                                    transport=transport, expected=reads[change.path])
            except ProjectVaultError as exc:
                code = str(exc)
                failed[code] = failed.get(code, 0) + 1
                journal.record(change, "failed:" + code, time.time())
                if code in ("vault_root_refused", "source_changed", "source_not_configured",
                            "invalid_vault_root"):
                    break  # the connection itself is wrong: stop writing
                continue
            journal.record(change, result["outcome"], time.time())  # type: ignore[arg-type]
            report["applied"] += 1  # type: ignore[operator]
        return report
    finally:
        journal.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Change Project note status from GitHub activity. Dry run unless --apply.")
    parser.add_argument("--state", type=Path, required=True,
                        help="the service state directory (e.g. /var/lib/obsidian-snapshot)")
    parser.add_argument("--apply", action="store_true",
                        help="write the status changes to the Vault (default: report only)")
    parser.add_argument("--max-changes", type=int, default=MAX_CHANGES_PER_RUN)
    args = parser.parse_args(argv)
    if not 1 <= args.max_changes <= 100:
        parser.error("--max-changes must be 1..100")

    from .github_daily_runonce import DailyPreviewRunError, load_run_config
    from .snapshot_setup import SetupStore
    from .vault_root_activation import JobRootGuard, https_transport_factory

    try:
        config, token = load_run_config(args.state / "github-daily")
    except DailyPreviewRunError:
        print(json.dumps({"status": "error", "error_code": "github_credential_unavailable"}))
        return 1
    shared = SetupStore(args.state)
    guard = JobRootGuard(shared, transport_factory=https_transport_factory)
    client = watcher.GitHubClient(token=token, active_window_days=config.active_window_days,
                                  timeout_seconds=config.request_timeout_seconds)
    try:
        report = run_once(shared=shared, guard=guard, client=client,
                          state_dir=args.state / "project-status", apply=args.apply,
                          active_window_days=config.active_window_days,
                          max_changes=args.max_changes)
    except ProjectVaultError as exc:
        print(json.dumps({"status": "error", "error_code": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"status": "ok", **report}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
