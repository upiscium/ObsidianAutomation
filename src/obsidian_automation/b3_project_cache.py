"""GET-only B3 Project watch cache from an authorized collector-time mirror.

Discovery reads *only* the disposable local Project source already pulled by
the B3 watcher. GET never re-scans the Vault or calls GitHub/Nextcloud.
Neither a watch observation nor this projection is a canonical Vault Writer.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .github_project_watch_view import (
    GlobalWatchStatus, ProjectWatchConfig, ProjectWatchObservation,
    project_watch_projection,
)
from .github_project_watcher import _frontmatter_scalars

_VERSION = "b3-project-cache/v1"
_FILENAME = "project-watch-cache.json"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_PROJECTS = 256
_MAX_NOTES_SCAN = 512
_MAX_SCAN_ENTRIES = 2048
_MAX_DIR_ENTRIES = 1024
_MAX_SCAN_DIRS = 128
_MAX_SCAN_DEPTH = 8
_MAX_NOTE_SIZE = 65536
_MAX_CACHE_BYTES = 262144
_MAX_CACHE_AGE_SECONDS = 86400


class CacheUnavailable(ValueError):
    def __init__(self):
        super().__init__("b3_project_cache_unavailable")


def _secure_root(root: Path):
    if not isinstance(root, Path) or not root.is_absolute() or root.is_symlink():
        raise CacheUnavailable()
    try:
        info = root.stat()
    except OSError:
        raise CacheUnavailable() from None
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077):
        raise CacheUnavailable()


def _bounded_markdown_files(root: Path):
    """Depth-first lexical traversal with hard bounds *before* sorting."""
    pending = [(root, 0)]
    scanned_dirs = scanned_entries = 0
    while pending:
        path, depth = pending.pop()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            # The former rglob("*.md") yielded matching directories, then
            # rejected them at the opened-inode check. Keep that fail-closed
            # behavior even for an empty or nested Markdown-named directory.
            if path != root and path.name.endswith(".md"):
                raise CacheUnavailable()
            scanned_dirs += 1
            if depth > _MAX_SCAN_DEPTH or scanned_dirs > _MAX_SCAN_DIRS:
                raise CacheUnavailable()
            children: list[Path] = []
            with os.scandir(path) as listing:
                for entry in listing:
                    scanned_entries += 1
                    if (scanned_entries > _MAX_SCAN_ENTRIES
                            or len(children) >= _MAX_DIR_ENTRIES):
                        raise CacheUnavailable()
                    children.append(Path(entry.path))
            # Only a bounded directory's entries are sorted; DFS preserves
            # lexical path order without materializing an unbounded rglob.
            pending.extend(
                (child, depth + 1) for child in reversed(sorted(children))
            )
        elif path.name.endswith(".md"):
            # Match rglob("*.md"), including the literal filename ".md".
            # Refuse FIFOs/devices before open; symlinks are rejected by
            # O_NOFOLLOW at the final note boundary below.
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise CacheUnavailable()
            yield path


def _read_all_projects(vault_root: Path) -> tuple[ProjectWatchConfig, ...]:
    root = vault_root / "10-Project"
    if not root.is_dir() or root.is_symlink():
        raise CacheUnavailable()
    rows: list[ProjectWatchConfig] = []
    count = 0
    try:
        for path in _bounded_markdown_files(root):
            count += 1
            if count > _MAX_NOTES_SCAN:
                raise CacheUnavailable()
            # Open the exact file with O_NOFOLLOW, then inspect the *opened*
            # inode. A symlink swap after an is_symlink() check must not
            # permit reading outside the authorized disposable mirror.
            fd = os.open(
                path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            )
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode)
                        or info.st_size > _MAX_NOTE_SIZE):
                    raise CacheUnavailable()
                raw = stream.read(_MAX_NOTE_SIZE + 1)
            if len(raw) > _MAX_NOTE_SIZE:
                raise CacheUnavailable()
            # Note body is parsed locally but never copied to the cache/UI.
            fm = _frontmatter_scalars(raw.decode("utf-8"))
            if fm.get("type") != "project":
                continue
            relative = path.relative_to(vault_root).as_posix()
            raw_watch = fm.get("github_watch")
            if raw_watch is None:
                watch = None
            elif raw_watch.strip().lower() in ("true", "yes", "on", "1"):
                watch = True
            elif raw_watch.strip().lower() in ("false", "no", "off", "0"):
                watch = False
            else:
                watch = None
            rows.append(ProjectWatchConfig(
                relative, fm.get("status"), watch, fm.get("github_repo")
            ))
            if len(rows) > _MAX_PROJECTS:
                raise CacheUnavailable()
    except (OSError, UnicodeError, ValueError):
        raise CacheUnavailable() from None
    return tuple(rows)


def _read_watcher_evidence(db_path: Path) -> tuple[ProjectWatchObservation, ...]:
    if not db_path.is_absolute() or not db_path.is_file() or db_path.is_symlink():
        raise CacheUnavailable()
    try:
        with sqlite3.connect(db_path.as_uri() + "?mode=ro",
                             uri=True, timeout=2) as db:
            db.execute("PRAGMA query_only=ON")
            rows = db.execute(
                "SELECT project_path,repository,observed_at "
                "FROM project_state ORDER BY project_path LIMIT 257"
            ).fetchall()
    except sqlite3.Error:
        raise CacheUnavailable() from None
    if len(rows) > _MAX_PROJECTS:
        raise CacheUnavailable()
    try:
        return tuple(
            ProjectWatchObservation(path, repo, datetime.fromisoformat(
                observed.replace("Z", "+00:00")
            )) for path, repo, observed in rows
        )
    except (ValueError, TypeError, AttributeError):
        raise CacheUnavailable() from None


def build_project_snapshot(
    vault_root: Path, watcher_db: Path, *, now: datetime,
) -> dict[str, object]:
    """Trusted collector-time local read; zero provider API or Vault writes."""
    try:
        configs = _read_all_projects(vault_root)
        observations = _read_watcher_evidence(watcher_db)
        # A local source invocation alone is not trusted evidence that every
        # independent old producer/Writer was excluded.
        return project_watch_projection(
            configs, observations, GlobalWatchStatus(None, False, False),
            now=now,
        )
    except (OSError, ValueError, TypeError):
        raise CacheUnavailable() from None


_PROJECT_KEYS = frozenset({
    "projectId", "name", "repository", "projectStatus",
    "githubWatchConfigured", "monitoringState",
    "lastWatcherObservedAt", "lastVerifiedVaultWrite",
})
_PROJECT_STATUSES = frozenset({
    "planning", "running", "stopped", "stable", "done", "cancelled",
})
# This initial cache has *no trusted global scheduler/legacy-exclusion
# attestation*. The read-only producer can emit only these statuses.
# A future verified SourceOwner needs a separately reviewed contract/version.
_OBSERVER_ONLY_STATES = frozenset({
    "disabled_by_project", "configuration_unknown", "misconfigured",
    "misconfigured_status", "ineligible_status", "global_control_unverified",
})
_TERMINAL_PROJECT_STATES = frozenset({"stopped", "done", "cancelled"})
_MAX_CAPTURE_LAG_SECONDS = 600
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_ID = re.compile(r"[0-9a-f]{24}\Z")


def _validate_projection(snapshot: object) -> None:
    # The GET boundary must not echo unknown fields added to an otherwise
    # valid-looking, but replaced/corrupted, local JSON file.
    if (type(snapshot) is not dict
            or set(snapshot) != {
                "schemaVersion", "projectedAt", "globalDesiredEnabled",
                "globalControlVerified", "projectCount", "projects",
            }
            or snapshot["schemaVersion"] != "b3-project-watch-list/v1"
            or type(snapshot["projectedAt"]) is not str
            or len(snapshot["projectedAt"]) > 64
            # This R0 collector has NO trusted global run/legacy-exclusion
            # attestation. A modified JSON cache must never claim otherwise.
            or snapshot["globalDesiredEnabled"] is not None
            or snapshot["globalControlVerified"] is not False
            or type(snapshot["projectCount"]) is not int
            or not 0 <= snapshot["projectCount"] <= _MAX_PROJECTS
            or type(snapshot["projects"]) not in (list, tuple)
            or len(snapshot["projects"]) != snapshot["projectCount"]):
        raise CacheUnavailable()
    try:
        moment = datetime.fromisoformat(snapshot["projectedAt"])
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise CacheUnavailable()
    except (ValueError, TypeError, OverflowError):
        raise CacheUnavailable() from None
    ids = set()
    for row in snapshot["projects"]:
        if (type(row) is not dict or set(row) != _PROJECT_KEYS
                or type(row["projectId"]) is not str
                or _ID.fullmatch(row["projectId"]) is None
                or row["projectId"] in ids
                or type(row["name"]) is not str or not 1 <= len(row["name"]) <= 120
                or (row["repository"] is not None and (
                    type(row["repository"]) is not str or
                    len(row["repository"]) > 180 or
                    _REPOSITORY.fullmatch(row["repository"]) is None))
                or type(row["projectStatus"]) not in (str, type(None))
                or row["projectStatus"] not in _PROJECT_STATUSES | {None}
                or type(row["githubWatchConfigured"]) not in (bool, type(None))
                or type(row["monitoringState"]) is not str
                or row["monitoringState"] not in _OBSERVER_ONLY_STATES
                or row["lastWatcherObservedAt"] is not None
                or row["lastVerifiedVaultWrite"] is not None
                or (row["lastWatcherObservedAt"] is not None and (
                    type(row["lastWatcherObservedAt"]) is not str
                    or len(row["lastWatcherObservedAt"]) > 64))):
            raise CacheUnavailable()
        # Match the exact state precedence of project_watch_projection()
        # when GlobalWatchStatus(None, False, False) is the only authority.
        # A disk-replaced cache cannot claim a fresher GitHub observation,
        # an active/paused scheduler, or a verified Vault write.
        watch = row["githubWatchConfigured"]
        if watch is False:
            expected = "disabled_by_project"
        elif watch is None:
            expected = "configuration_unknown"
        elif row["repository"] is None:
            expected = "misconfigured"
        elif row["projectStatus"] is None:
            expected = "misconfigured_status"
        elif row["projectStatus"] in _TERMINAL_PROJECT_STATES:
            expected = "ineligible_status"
        else:
            expected = "global_control_unverified"
        if row["monitoringState"] != expected:
            raise CacheUnavailable()
        ids.add(row["projectId"])
    return moment


def persist_project_snapshot(
    root: Path, generation: str, snapshot: dict, *, now: float | None = None,
):
    """Atomic private local cache; publish only after verified collector end."""
    _secure_root(root)
    if type(generation) is not str or _SHA.fullmatch(generation) is None:
        raise CacheUnavailable()
    projected = _validate_projection(snapshot)
    when = time.time() if now is None else now
    if (type(when) not in (float, int) or not 0 <= when < 253402300799
            or not 0 <= when - projected.timestamp() <= _MAX_CAPTURE_LAG_SECONDS):
        # Rendered snapshot must have been generated shortly before the
        # verified collector's cache publication, never in the future.
        raise CacheUnavailable()
    payload = {
        "schemaVersion": _VERSION,
        "generation": generation,
        "cachedAt": float(when),
        "projectList": snapshot,
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    if len(raw) > _MAX_CACHE_BYTES:
        raise CacheUnavailable()
    target = root / _FILENAME
    if target.is_symlink():
        raise CacheUnavailable()
    temp = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".b3-project-cache-",dir=root,
                                         delete=False) as writer:
            temp = Path(writer.name)
            os.fchmod(writer.fileno(), 0o600)
            writer.write(raw)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temp, target)
    except OSError:
        raise CacheUnavailable() from None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def read_project_snapshot(
    root: Path, generation: str | None, *, now: float | None = None,
) -> dict[str, object]:
    """Local GET read-only; no network, no cache rewrite or Watcher launch."""
    _secure_root(root)
    if type(generation) is not str or _SHA.fullmatch(generation) is None:
        raise CacheUnavailable()
    target = root / _FILENAME
    if target.is_symlink():
        raise CacheUnavailable()
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1
                    or info.st_size > _MAX_CACHE_BYTES):
                raise CacheUnavailable()
            raw = stream.read(_MAX_CACHE_BYTES + 1)
        if len(raw) > _MAX_CACHE_BYTES:
            raise CacheUnavailable()
        data = json.loads(raw)
    except (OSError, ValueError, TypeError):
        raise CacheUnavailable() from None
    when = time.time() if now is None else now
    if (type(data) is not dict
            or set(data) != {"schemaVersion", "generation", "cachedAt", "projectList"}
            or data.get("schemaVersion") != _VERSION
            or data.get("generation") != generation
            or type(data.get("cachedAt")) is not float
            or type(when) not in (float, int)
            or not 0 <= when-data["cachedAt"] <= _MAX_CACHE_AGE_SECONDS):
        raise CacheUnavailable()
    snapshot = data.get("projectList")
    projected = _validate_projection(snapshot)
    if not 0 <= data["cachedAt"] - projected.timestamp() <= _MAX_CAPTURE_LAG_SECONDS:
        raise CacheUnavailable()
    return snapshot
