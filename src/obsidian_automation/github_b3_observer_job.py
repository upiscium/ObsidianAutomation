"""Single-process B3 *read-only* Project watcher scheduling and receipts.

The owner is the existing #298 nonroot app. This job never authorizes a Vault
Writer, Core Promotion, GitHub/Gitea push or a Gitea Runner. Its only remote
effect is the B4 source's pull-only Project mirror and GitHub GET observations.
It is deliberately OFF by default, and no collection occurs in constructor,
GET/status, HEAD or settings update.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .github_b3_watcher_bridge import VERSION as BRIDGE_VERSION

VERSION = "github-b3-readonly-observer-job/v1"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class Invalid(ValueError):
    pass


class Busy(RuntimeError):
    pass


def parse_request(raw: bytes) -> dict:
    """Reject ambiguous JSON before any scheduler/Run-Once admission."""
    if type(raw) is not bytes or not 0 < len(raw) <= 4096:
        raise Invalid("invalid_b3_observer_request")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise Invalid("invalid_b3_observer_request")
            value[key] = item
        return value

    def reject_nonfinite(_):
        raise Invalid("invalid_b3_observer_request")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=reject_nonfinite)
    except (UnicodeError, ValueError, TypeError):
        raise Invalid("invalid_b3_observer_request") from None
    if type(value) is not dict:
        raise Invalid("invalid_b3_observer_request")
    return value


def _bounded_now(clock: Callable[[], float]) -> float:
    value = clock()
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= 253402300799):
        raise Invalid("invalid_clock")
    return float(value)


def _source_generation(configured: Callable[[], bool],
                       generation: Callable[[], str | None]) -> str | None:
    try:
        if configured() is not True:
            return None
        value = generation()
        if type(value) is str and _SHA.fullmatch(value):
            return value
    except Exception:
        pass
    return None


def _result(value: object) -> tuple[str, str, int, int]:
    """Never turn an injected or malformed collector reply into a success."""
    if type(value) is not dict or set(value) != {
        "schema_version", "state", "reason", "observations",
        "pending_proposals", "vault_write_authorized", "cutover_authorized",
    } or value["schema_version"] != BRIDGE_VERSION or (
        value["vault_write_authorized"] is not False
        or value["cutover_authorized"] is not False
    ):
        return "unknown", "collector_unavailable", 0, 0
    state, reason = value["state"], value["reason"]
    count, pending = value["observations"], value["pending_proposals"]
    if (type(count) is not int or type(pending) is not int
            or not 0 <= pending <= count <= 500):
        return "unknown", "collector_unavailable", 0, 0
    if state == "observed" and reason == "local_read_only_observation" and count > 0:
        return "observed", reason, count, pending
    if state == "blocked" and reason == "watcher_already_running":
        return "blocked", reason, 0, 0
    return "unknown", "collector_unavailable", 0, 0


class B3ObserverJob:
    """One owner, one durable SQLite schedule, one local GET-only worker.

    All public methods return finite, redacted values. Duplicate request IDs
    are replayed without a second network call. Interrupted collection stays
    unknown after restart; it is safe to explicitly schedule a *new*
    observation because this class has no canonical writer.
    """

    def __init__(
        self, state: Path, collector: Callable[[], dict[str, object]], *,
        configured: Callable[[], bool],
        generation: Callable[[], str | None],
        clock: Callable[[], float] = time.time,
    ):
        if (not isinstance(state, Path) or not state.is_absolute()
                or state.is_symlink() or not callable(collector)
                or not callable(configured) or not callable(generation)
                or not callable(clock)):
            raise Invalid("invalid_b3_observer_config")
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = state.stat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise Invalid("unsafe_b3_observer_root")
        db_path = state / "observer-job.sqlite3"
        if db_path.is_symlink() or (db_path.exists() and not db_path.is_file()):
            raise Invalid("unsafe_b3_observer_database")
        self.state, self.collector = state, collector
        self.is_configured, self.get_generation = configured, generation
        self.clock = clock
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._fd = os.open(state / "observer-job.lock",
                           os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            locked = os.fstat(self._fd)
            if (not stat.S_ISREG(locked.st_mode)
                    or locked.st_uid != os.geteuid()
                    or locked.st_mode & 0o077 or locked.st_nlink != 1):
                raise Invalid("unsafe_b3_observer_lock")
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self._fd)
            raise
        try:
            self.db = sqlite3.connect(db_path, check_same_thread=False, timeout=2)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS settings ("
                "id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL,"
                "interval_seconds INTEGER NOT NULL, next_due REAL, generation TEXT)"
            )
            self.db.execute(
                "INSERT OR IGNORE INTO settings VALUES (1,0,3600,NULL,NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS runs ("
                "run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,"
                "trigger TEXT NOT NULL, status TEXT NOT NULL,"
                "created REAL NOT NULL, completed REAL,"
                "observations INTEGER NOT NULL DEFAULT 0,"
                "pending_proposals INTEGER NOT NULL DEFAULT 0,"
                "reason TEXT NOT NULL)"
            )
            self.db.execute(
                "UPDATE runs SET status='unknown', reason='interrupted', completed=? "
                "WHERE status IN ('queued','running')", (_bounded_now(clock),)
            )
            self.db.commit()
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        if self._worker is not None:
            self._worker.join(timeout=15)
            if self._worker.is_alive():
                raise Busy("b3_observer_worker_still_running")
        self.db.close()
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)

    def _generation(self) -> str | None:
        return _source_generation(self.is_configured, self.get_generation)

    def _settings(self) -> dict[str, object]:
        enabled, interval, due, bound = self.db.execute(
            "SELECT enabled,interval_seconds,next_due,generation FROM settings WHERE id=1"
        ).fetchone()
        current = self._generation()
        valid = bool(current and current == bound)
        return {
            "enabled": bool(enabled),
            "effective_enabled": bool(enabled) and valid,
            "interval_seconds": interval,
            "next_due": due if enabled and valid else None,
            "configured": current is not None,
            "reason": (
                "source_changed" if enabled and not valid
                else "configuration_missing" if current is None
                else None
            ),
        }

    def settings(self) -> dict[str, object]:
        with self._lock:
            return self._settings()

    def update(self, payload: dict) -> dict[str, object]:
        if (type(payload) is not dict or set(payload) != {"enabled", "interval_seconds"}
                or type(payload["enabled"]) is not bool
                or type(payload["interval_seconds"]) is not int
                or not 3600 <= payload["interval_seconds"] <= 86400):
            raise Invalid("invalid_b3_observer_settings")
        with self._lock:
            current = self._generation()
            if payload["enabled"] and current is None:
                raise Invalid("b3_source_not_configured")
            previous = self._settings()
            enabled = payload["enabled"]
            due = (
                _bounded_now(self.clock) + payload["interval_seconds"]
                if enabled and (not previous["effective_enabled"]
                                or previous["interval_seconds"] != payload["interval_seconds"])
                else previous["next_due"] if enabled else None
            )
            with self.db:
                self.db.execute(
                    "UPDATE settings SET enabled=?,interval_seconds=?,next_due=?,generation=? WHERE id=1",
                    (int(enabled), payload["interval_seconds"], due,
                     current if enabled else None),
                )
            return self._settings()

    def _run(self, run_id: str) -> dict[str, object] | None:
        row = self.db.execute(
            "SELECT run_id,request_id,trigger,status,created,completed,"
            "observations,pending_proposals,reason FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return dict(zip(
            ("run_id", "request_id", "trigger", "status", "created", "completed",
             "observations", "pending_proposals", "reason"), row,
        )) if row else None

    def run(self, run_id: str) -> dict[str, object] | None:
        if type(run_id) is not str or _ID.fullmatch(run_id) is None:
            return None
        with self._lock:
            return self._run(run_id)

    def status(self) -> dict[str, object]:
        with self._lock:
            last = self.db.execute(
                "SELECT run_id FROM runs ORDER BY created DESC, rowid DESC LIMIT 1"
            ).fetchone()
            return {
                "schema_version": VERSION, "job": "b3_read_only_watcher",
                "authority": "observation_only",
                "settings": self._settings(),
                "last_run": self._run(last[0]) if last else None,
                "vault_write_authorized": False,
                "cutover_authorized": False,
            }

    def _admit(self, request_id: str, trigger: str, now: float) -> dict[str, object]:
        run_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO runs VALUES (?,?,?,'queued',?,NULL,0,0,'pending')",
            (run_id, request_id, trigger, now),
        )
        return self._run(run_id)

    def submit(self, request_id: str, trigger: str = "manual") -> tuple[str, dict | None]:
        if (type(request_id) is not str or _ID.fullmatch(request_id) is None
                or type(trigger) is not str or trigger != "manual"):
            raise Invalid("invalid_b3_observer_request")
        with self._lock:
            found = self.db.execute(
                "SELECT run_id FROM runs WHERE request_id=?", (request_id,)
            ).fetchone()
            if found is not None:
                return "replayed", self._run(found[0])
            if self._generation() is None:
                return "unconfigured", None
            if self.db.execute(
                "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone():
                return "busy", None
            with self.db:
                row = self._admit(request_id, trigger, _bounded_now(self.clock))
            self._launch(row["run_id"])
            return "accepted", row

    def _launch(self, run_id: str) -> None:
        self._worker = threading.Thread(
            target=self._execute, args=(run_id,), daemon=False,
        )
        try:
            self._worker.start()
        except Exception:
            with self.db:
                self.db.execute(
                    "UPDATE runs SET status='unknown',reason='worker_unavailable',"
                    "completed=? WHERE run_id=?",
                    (_bounded_now(self.clock), run_id),
                )
            self._worker = None
            raise

    def _execute(self, run_id: str) -> None:
        with self._lock:
            with self.db:
                self.db.execute(
                    "UPDATE runs SET status='running' WHERE run_id=?", (run_id,)
                )
        try:
            value = self.collector()
            status, reason, count, pending = _result(value)
        except Exception:
            status, reason, count, pending = "unknown", "collector_unavailable", 0, 0
        with self._lock:
            with self.db:
                self.db.execute(
                    "UPDATE runs SET status=?,reason=?,observations=?,"
                    "pending_proposals=?,completed=? WHERE run_id=?",
                    (status, reason, count, pending, _bounded_now(self.clock), run_id),
                )

    def tick(self) -> bool:
        """Called by the existing single in-app scheduler; never runs inline."""
        with self._lock:
            s = self._settings()
            if s["enabled"] and not s["effective_enabled"]:
                # Persist Off on source rotation, rather than silently
                # using a new Nextcloud/credential binding next cycle.
                with self.db:
                    self.db.execute(
                        "UPDATE settings SET enabled=0,next_due=NULL,generation=NULL WHERE id=1"
                    )
                return False
            if (not s["effective_enabled"] or s["next_due"] is None
                    or _bounded_now(self.clock) < s["next_due"]):
                return False
            if self.db.execute(
                "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone():
                return False
            now = _bounded_now(self.clock)
            with self.db:
                row = self._admit(uuid.uuid4().hex, "scheduled", now)
                self.db.execute(
                    "UPDATE settings SET next_due=? WHERE id=1",
                    (now + s["interval_seconds"],),
                )
            self._launch(row["run_id"])
            return True
