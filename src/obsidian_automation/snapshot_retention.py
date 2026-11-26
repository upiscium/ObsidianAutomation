"""Bounded retention for Snapshot run directories (``state/runs/<run_id>``).

Every Snapshot attempt keeps a mirror, a bare fetch and a worktree (~215 MB for
the current Vault), and nothing ever removed them. This module deletes whole
run directories that are older than the newest ``keep`` runs.

Safety rules (tested):

* only directories named by a 32-hex ``run_id`` that also has a row in the
  ``runs`` table are candidates; other entries are only counted, never touched;
* queued / running / unconfirmed runs and the most recent successful run are
  never deleted, however old;
* a candidate must be a real directory (no symlink) owned by this process's user
  and directly inside ``state/runs``;
* a failure to remove one directory is counted and does not stop the others, and
  never affects a Snapshot run.

Pruning is OFF unless the service is started with ``--keep-runs N``. The CLI
(``python -m obsidian_automation.snapshot_retention``) is a dry run unless
``--apply`` is given.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

_RUN_ID = re.compile(r"[a-f0-9]{32}")
# A run in one of these states may still be needed to understand what happened.
PROTECTED_STATUSES = frozenset({"queued", "running", "unconfirmed"})
SUCCESS_STATUSES = frozenset({"saved", "no_changes"})
MIN_KEEP = 2


@dataclass(frozen=True)
class PrunePlan:
    delete: tuple[str, ...]
    kept: int
    protected: int
    unknown_directories: int
    missing_directories: int
    ignored_entries: int = 0


@dataclass
class PruneResult:
    deleted: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)


def plan_prune(state: Path, db: sqlite3.Connection, keep: int) -> PrunePlan:
    """Decide which run directories to remove; touches nothing."""
    if type(keep) is not int or keep < MIN_KEEP:
        raise ValueError(f"keep must be an integer >= {MIN_KEEP}")
    runs_dir = state / "runs"
    on_disk: set[str] = set()
    ignored = 0
    if runs_dir.is_dir():
        for entry in os.scandir(runs_dir):
            if _RUN_ID.fullmatch(entry.name):
                on_disk.add(entry.name)
            else:
                ignored += 1
    rows = db.execute(
        "SELECT run_id,status FROM runs ORDER BY created DESC,rowid DESC"
    ).fetchall()
    known = {row[0] for row in rows}
    last_success = next((row[0] for row in rows if row[1] in SUCCESS_STATUSES), None)
    protected_ids = {row[0] for row in rows if row[1] in PROTECTED_STATUSES}
    if last_success is not None:
        protected_ids.add(last_success)
    older = rows[keep:]
    delete = tuple(
        row[0] for row in older
        if row[0] not in protected_ids and row[0] in on_disk
    )
    return PrunePlan(
        delete=delete,
        kept=min(keep, len(rows)),
        protected=sum(1 for row in older if row[0] in protected_ids),
        unknown_directories=len(on_disk - known),
        missing_directories=sum(1 for row in older if row[0] not in on_disk),
        ignored_entries=ignored,
    )


def _remove_one(runs_dir: Path, run_id: str) -> None:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("invalid run id")
    path = runs_dir / run_id
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("not a plain directory")
    if info.st_uid != os.geteuid():
        raise ValueError("not owned by this user")
    if path.resolve().parent != runs_dir.resolve():
        raise ValueError("outside the runs directory")
    shutil.rmtree(path)


def apply_prune(state: Path, plan: PrunePlan) -> PruneResult:
    """Remove exactly the planned directories, one at a time."""
    result = PruneResult()
    runs_dir = state / "runs"
    for run_id in plan.delete:
        try:
            _remove_one(runs_dir, run_id)
        except Exception as exc:  # one bad directory must not stop the rest
            result.failed += 1
            result.failures.append(f"{run_id}:{type(exc).__name__}")
        else:
            result.deleted += 1
    return result


def prune_runs(state: Path, db: sqlite3.Connection, keep: int) -> PruneResult:
    """Plan and apply in one step (used by the service after a run)."""
    return apply_prune(state, plan_prune(state, db, keep))


def _open_readonly(state: Path) -> sqlite3.Connection:
    database = state / "snapshot.sqlite3"
    return sqlite3.connect(f"file:{database}?mode=ro", uri=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List (default) or remove old Snapshot run directories")
    parser.add_argument("--state", type=Path, required=True,
                        help="Absolute service-owned state directory")
    parser.add_argument("--keep", type=int, required=True,
                        help=f"Keep the newest N runs (>= {MIN_KEEP})")
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete; without it this is a dry run")
    parser.add_argument("--list", action="store_true",
                        help="Include the run ids that would be deleted")
    args = parser.parse_args(argv)
    if not args.state.is_absolute():
        parser.error("state must be absolute")
    if args.keep < MIN_KEEP:
        parser.error(f"keep must be >= {MIN_KEEP}")
    db = _open_readonly(args.state)
    try:
        plan = plan_prune(args.state, db, args.keep)
    finally:
        db.close()
    report: dict[str, object] = {
        "mode": "apply" if args.apply else "dry-run",
        "keep": args.keep,
        "would_delete": len(plan.delete),
        "kept": plan.kept,
        "protected_older": plan.protected,
        "unknown_directories": plan.unknown_directories,
        "missing_directories": plan.missing_directories,
        "ignored_entries": plan.ignored_entries,
    }
    if args.list:
        report["delete"] = list(plan.delete)
    exit_code = 0
    if args.apply:
        result = apply_prune(args.state, plan)
        report.update(deleted=result.deleted, failed=result.failed,
                      failures=result.failures[:20])
        exit_code = 1 if result.failed else 0
    print(json.dumps(report, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
