"""Bounded, read-only B3 watcher bridge reusing the configured B4 source.

No UI route or scheduler is registered by this module. It downloads Project
notes only through the pre-existing B4 disposable, pull-only source, observes
GitHub with the existing watcher, and persists only a *local* diagnostic state.
It never constructs a canonical Vault Writer, Core Promotion or cutover grant.
"""
from __future__ import annotations

import fcntl
import io
import json
import os
import re
import stat
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .github_project_watcher import GitHubClient, WatcherConfig, run_once
from .b3_project_cache import (
    CacheUnavailable, build_project_snapshot, persist_project_snapshot,
)

VERSION = "github-b3-watcher-bridge/v1"
_LOCK = "watcher.lock"
_DB = "watcher.sqlite3"
_MAX_CAPTURE_BYTES = 512 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class _BoundedSink(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self._bytes = 0

    def write(self, value: str) -> int:
        if type(value) is not str:
            raise TypeError("invalid_observation")
        size = len(value.encode("utf-8"))
        if self._bytes + size > _MAX_CAPTURE_BYTES:
            raise ValueError("bounded_observation_exceeded")
        result = super().write(value)
        self._bytes += size
        return result


def _result(state: str, reason: str, *, observations: int = 0,
            pending: int = 0) -> dict[str, object]:
    return {
        "schema_version": VERSION,
        "state": state,
        "reason": reason,
        "observations": observations,
        "pending_proposals": pending,
        "vault_write_authorized": False,
        "cutover_authorized": False,
    }


def _private_root(root: Path) -> bool:
    if not isinstance(root, Path) or not root.is_absolute() or root.is_symlink():
        return False
    try:
        info = root.stat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            return False
        database = root / _DB
        if database.is_symlink():
            return False
        if database.exists() and not database.is_file():
            return False
    except (OSError, ValueError):
        return False
    return True


def collect_b3_read_only(
    daily_setup: object,
    *,
    state_root: Path,
    expected_generation: str | None = None,
    client_factory: Callable[..., object] = GitHubClient,
    watcher: Callable[..., int] = run_once,
) -> dict[str, object]:
    """One bounded, non-reentrant local observation; never a Vault effect.

    daily_setup is the already-configured, trusted B4 DailySetup, never a
    request-supplied object. Its project_source() fetches a disposable local
    Project mirror with a read-only Nextcloud pull and yields a server-held
    GitHub token. Per-project details and errors stay off the public response.
    """
    if not _private_root(state_root):
        return _result("unknown", "private_watcher_state_unavailable")
    if expected_generation is not None and (
            type(expected_generation) is not str
            or _SHA.fullmatch(expected_generation) is None):
        return _result("unknown", "invalid_source_binding")
    if not callable(client_factory) or not callable(watcher):
        return _result("unknown", "watcher_unavailable")

    fd = None
    try:
        fd = os.open(
            state_root / _LOCK,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            return _result("unknown", "watcher_lock_unavailable")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _result("blocked", "watcher_already_running")

        source_context = (
            daily_setup.project_source()
            if expected_generation is None
            else daily_setup.project_source(
                expected_generation=expected_generation,
            )
        )
        with source_context as (config, token):
            if (not isinstance(config, WatcherConfig)
                    or type(token) is not str or not token):
                return _result("unknown", "watcher_source_unavailable")
            config = replace(config, state_db=state_root / _DB)
            api = client_factory(
                token=token,
                timeout_seconds=config.request_timeout_seconds,
                active_window_days=config.active_window_days,
            )
            output, errors = _BoundedSink(), _BoundedSink()
            # Existing watcher owns deterministic per-project observation and
            # SQLite commits; errors cannot leak credentials/note paths to UI.
            code = watcher(config, client=api, stdout=output, stderr=errors)
            if type(code) is not int or code != 0 or errors.getvalue():
                return _result("unknown", "watcher_observation_unverified")
            count = 0
            pending = 0
            for line in output.getvalue().splitlines():
                event = json.loads(line)
                if type(event) is not dict or event.get("event") != "project-status-observation":
                    return _result("unknown", "watcher_observation_unverified")
                if type(event.get("pending")) is not bool:
                    return _result("unknown", "watcher_observation_unverified")
                count += 1
                pending += int(event["pending"])
                if count > 500:
                    return _result("unknown", "watcher_observation_unverified")
            if count == 0:
                return _result("unknown", "watcher_no_observations")
            # Build from the *existing disposable mirror*, never a second
            # provider request. Stage in memory until project_source() exits
            # successfully: its teardown checks the original generation.
            # Do not stamp an old mirror with a newly rotated generation.
            snapshot, generation = None, None
            try:
                fingerprint = getattr(daily_setup, "source_fingerprint", None)
                if callable(fingerprint):
                    generation = fingerprint()
                    snapshot = build_project_snapshot(
                        config.vault_root, config.state_db,
                        now=datetime.now(timezone.utc),
                    )
            except (CacheUnavailable, AttributeError, TypeError, OSError):
                snapshot, generation = None, None
        # The source's post-yield generation check is now complete. A
        # subsequent rotation causes a mismatch on both publication and GET.
        if snapshot is not None:
            try:
                if fingerprint() == generation:
                    persist_project_snapshot(state_root, generation, snapshot)
            except (CacheUnavailable, AttributeError, TypeError, OSError):
                pass
        return _result("observed", "local_read_only_observation",
                       observations=count, pending=pending)
    except (OSError, ValueError, TypeError, RuntimeError, AttributeError,
            UnicodeError, json.JSONDecodeError):
        return _result("unknown", "watcher_observation_unavailable")
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
