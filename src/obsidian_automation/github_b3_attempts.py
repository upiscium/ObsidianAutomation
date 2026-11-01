"""Durable B3 *attempt admission* journal for a future single-LXC job owner.

This module does NOT run Project Sync, touch Vault, call providers, enqueue a
Writer, or register with the WebUI. Only a future explicitly connected B3 Job
may call its state transitions. Its private SQLite records attest that *this
local job owner* recorded an admission, start or terminal disposition; they
never attest remote CAS readback or complete GitHub pipeline success.

Design intentionally follows the existing single-service-user B4 Job pattern:
one nonroot owner, one private state root, one process-wide lock, and no
second privileged service or separate health-authority database.
"""
from __future__ import annotations

import fcntl
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

VERSION = "github-b3-attempts/v1"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_ACTIVE = frozenset(("queued", "running"))
_DETERMINISTIC_REFUSALS = frozenset((
    "preflight_rejected", "cas_conflict", "authority_rejected",
))
# An HTTP non-2xx alone does not prove that remote state was unchanged.
_UNCONFIRMED = frozenset((
    "http_rejected", "source_finished_unverified", "runner_failed",
    "effect_unknown", "interrupted_unknown",
))
# Queued admissions have not passed the persisted start() effect boundary.
_ABANDONED_BEFORE_START = "abandoned_before_start"
_SAFE_RETRY_PARENTS = _DETERMINISTIC_REFUSALS | {_ABANDONED_BEFORE_START}
_MARKER_NAME = "b3-attempts.initialized"
_MARKER_BYTES = b"b3-attempts/v1\n"
_COLS = (
    "seq", "run_id", "request_id", "binding_ref", "expected_revision",
    "trigger", "retry_of", "created", "started", "completed", "state",
    "terminal_code",
)


class InvalidAttempt(ValueError):
    """Fixed nonsecret code. Do not echo request payloads or exception text."""

    def __init__(self, code: str = "invalid_attempt") -> None:
        self.code = code
        super().__init__(code)


class Busy(RuntimeError):
    """Single active owner, ambiguous-effect hold or concurrent admission."""

    def __init__(self, code: str = "busy") -> None:
        self.code = code
        super().__init__(code)


def _is_id(value: object) -> bool:
    return type(value) is str and _ID.fullmatch(value) is not None


def _when(value: object) -> float:
    if type(value) not in (int, float):
        raise InvalidAttempt("invalid_time")
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        raise InvalidAttempt("invalid_time") from None
    if not math.isfinite(n) or n < 0:
        raise InvalidAttempt("invalid_time")
    return n


def _open_private_root(root: Path) -> int:
    """Open each *existing* ancestor with NOFOLLOW; the app installer owns it."""
    if not isinstance(root, Path) or not root.is_absolute():
        raise InvalidAttempt("invalid_private_state")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open("/", flags)
        try:
            for component in root.parts[1:]:
                if component in ("", ".", ".."):
                    raise InvalidAttempt("invalid_private_state")
                next_fd = os.open(component, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            info = os.fstat(fd)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or bool(info.st_mode & 0o077)):
                raise InvalidAttempt("invalid_private_state")
            return fd
        except BaseException:
            os.close(fd)
            raise
    except OSError:
        raise InvalidAttempt("invalid_private_state") from None


def _private_file(dir_fd: int, name: str, *, create: bool = False) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    if create:
        flags |= os.O_CREAT
    fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or bool(info.st_mode & 0o077)):
            raise InvalidAttempt("invalid_private_state")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _present(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _check_marker(dir_fd: int) -> None:
    fd = os.open(
        _MARKER_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=dir_fd,
    )
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or bool(info.st_mode & 0o077)
                or info.st_size != len(_MARKER_BYTES)
                or os.read(fd, len(_MARKER_BYTES) + 1) != _MARKER_BYTES):
            raise InvalidAttempt("invalid_store_marker")
    finally:
        os.close(fd)


def _write_marker(dir_fd: int) -> None:
    fd = os.open(
        _MARKER_NAME,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600, dir_fd=dir_fd,
    )
    try:
        written = os.write(fd, _MARKER_BYTES)
        if written != len(_MARKER_BYTES):
            raise InvalidAttempt("invalid_store_marker")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(dir_fd)


def _public(row: tuple | None) -> dict[str, object] | None:
    if row is None:
        return None
    data = dict(zip(_COLS, row))
    # Explicitly DO NOT disclose project binding, revision or arbitrary paths.
    return {
        "run_id": data["run_id"],
        "trigger": data["trigger"],
        "retry_of": data["retry_of"],
        "state": data["state"],
        "terminal_code": data["terminal_code"],
        "created_at": data["created"],
        "started_at": data["started"],
        "completed_at": data["completed"],
    }


class B3AttemptJournal:
    """One producer-owned SQLite attempt owner; no thread/worker execution.

    Construction distinguishes a queued admission (effects cannot have
    begun before start() is durably recorded) from an interrupted running
    attempt (effect unknown). Neither is relaunched automatically.

    Only a one-time explicit initialize=True may create a new store. A durable
    marker prevents silent fresh initialization if the database later vanishes.
    The nonroot installer provisions an existing private 0700 state directory.

    This class is intentionally UNATTACHED: creating it is not an assertion
    that #483 has a live connected B3 Writer or a verified Vault result.
    """

    def __init__(
        self, state: Path, *, clock: Callable[[], float] = time.time,
        initialize: bool = False,
    ):
        if not callable(clock):
            raise InvalidAttempt("invalid_clock")
        if type(initialize) is not bool:
            raise InvalidAttempt("invalid_initialize")
        # Validate before creating any on-disk state or taking an owner lock.
        recovered_at = _when(clock())
        self.clock = clock
        self._lock = threading.RLock()
        self._closed = False
        self._dir_fd = _open_private_root(state)
        try:
            self._lock_fd = _private_file(
                self._dir_fd, "b3-attempts.lock", create=True,
            )
            try:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise Busy("b3_owner_busy") from None

                has_marker = _present(self._dir_fd, _MARKER_NAME)
                has_db = _present(self._dir_fd, "b3-attempts.sqlite3")
                if has_marker != has_db:
                    raise InvalidAttempt("store_missing_or_incomplete")
                if not has_marker and not initialize:
                    raise InvalidAttempt("store_not_initialized")
                if has_marker:
                    _check_marker(self._dir_fd)

                dbfd = _private_file(
                    self._dir_fd, "b3-attempts.sqlite3", create=not has_db,
                )
                os.close(dbfd)
                # Anchored to the verified existing directory descriptor.
                # SQLite and WAL sidecars remain in this same private root.
                db_path = f"/proc/self/fd/{self._dir_fd}/b3-attempts.sqlite3"
                self.db = sqlite3.connect(db_path, check_same_thread=False)
                try:
                    self.db.execute("PRAGMA journal_mode=WAL")
                    self.db.execute("PRAGMA synchronous=FULL")
                    version = self.db.execute("PRAGMA user_version").fetchone()[0]
                    if version != (1 if has_db else 0):
                        raise InvalidAttempt("unsupported_store_version")
                    if has_db:
                        columns = self.db.execute(
                            "PRAGMA table_info(runs)"
                        ).fetchall()
                        if not columns:
                            raise InvalidAttempt("missing_runs_table")
                        expected = (
                            ("seq", "INTEGER", 0, 1),
                            ("run_id", "TEXT", 1, 0),
                            ("request_id", "TEXT", 1, 0),
                            ("binding_ref", "TEXT", 1, 0),
                            ("expected_revision", "TEXT", 1, 0),
                            ("trigger", "TEXT", 1, 0),
                            ("retry_of", "TEXT", 0, 0),
                            ("created", "REAL", 1, 0),
                            ("started", "REAL", 0, 0),
                            ("completed", "REAL", 0, 0),
                            ("state", "TEXT", 1, 0),
                            ("terminal_code", "TEXT", 0, 0),
                        )
                        shape = tuple(
                            (r[1], r[2], r[3], r[5]) for r in columns
                        )
                        if shape != expected:
                            raise InvalidAttempt("invalid_runs_schema")
                        unique_keys = set()
                        for index in self.db.execute("PRAGMA index_list(runs)"):
                            if index[2] != 1:
                                continue
                            if index[4] != 0:
                                raise InvalidAttempt("invalid_runs_constraints")
                            # SELECT projects one 'name' column, so [0]
                            # is the indexed column name (not PRAGMA seqno).
                            unique_keys.add(tuple(
                                name for (name,) in self.db.execute(
                                    "SELECT name FROM pragma_index_info(?) "
                                    "ORDER BY seqno", (index[1],),
                                )
                            ))
                        if unique_keys != {
                            ("run_id",), ("request_id",), ("retry_of",),
                        }:
                            raise InvalidAttempt("invalid_runs_constraints")
                        # Never make an old admission appear to finish before
                        # it was created or before its effect had begun.
                        regressed = self.db.execute(
                            "SELECT 1 FROM runs WHERE state IN ('queued','running') "
                            "AND (created > ? OR completed IS NOT NULL "
                            "OR terminal_code IS NOT NULL "
                            "OR (state='queued' AND started IS NOT NULL) "
                            "OR (state='running' AND "
                            "(started IS NULL OR started < created OR started > ?))) "
                            "LIMIT 1",
                            (recovered_at, recovered_at),
                        ).fetchone()
                        if regressed is not None:
                            raise InvalidAttempt("clock_regression")
                    with self.db:
                        self.db.execute(
                            "CREATE TABLE IF NOT EXISTS runs ("
                            "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                            "run_id TEXT UNIQUE NOT NULL, "
                            "request_id TEXT UNIQUE NOT NULL, "
                            "binding_ref TEXT NOT NULL, "
                            "expected_revision TEXT NOT NULL, "
                            "trigger TEXT NOT NULL, retry_of TEXT UNIQUE, "
                            "created REAL NOT NULL, started REAL, completed REAL, "
                            "state TEXT NOT NULL, terminal_code TEXT)"
                        )
                        self.db.execute("PRAGMA user_version=1")
                        # No effect was admitted before the persisted start().
                        self.db.execute(
                            "UPDATE runs SET state='completed', "
                            "terminal_code='abandoned_before_start', completed=? "
                            "WHERE state='queued'", (recovered_at,),
                        )
                        # Running effects may have completed remotely: never replay.
                        self.db.execute(
                            "UPDATE runs SET state='unconfirmed', "
                            "terminal_code='interrupted_unknown', completed=? "
                            "WHERE state='running'", (recovered_at,),
                        )
                    if not has_marker:
                        _write_marker(self._dir_fd)
                except BaseException:
                    self.db.close()
                    raise
            except BaseException:
                os.close(self._lock_fd)
                raise
        except BaseException:
            os.close(self._dir_fd)
            raise

    def _ready(self) -> None:
        if self._closed:
            raise InvalidAttempt("journal_closed")

    def __enter__(self) -> B3AttemptJournal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.db.close()
            finally:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
                os.close(self._dir_fd)

    def _fetch(self, query: str, args: tuple = ()) -> tuple | None:
        return self.db.execute(query, args).fetchone()

    @staticmethod
    def _select() -> str:
        return ",".join(_COLS)

    def lookup(self, run_id: str) -> dict[str, object] | None:
        if not _is_id(run_id):
            return None
        with self._lock:
            self._ready()
            return _public(self._fetch(
                f"SELECT {self._select()} FROM runs WHERE run_id=?", (run_id,),
            ))

    def admit(
        self, request_id: str, binding_ref: str, expected_revision: str, *,
        trigger: str = "manual", retry_of: str | None = None,
    ) -> tuple[str, dict[str, object]]:
        """Persist *one* admission before any future effect is launched.

        The caller must validate its binding against its own registered server
        scope. This journal alone neither grants authority nor starts work.
        Duplicate request IDs always return the same durable run.
        """
        if not all(_is_id(x) for x in (request_id, binding_ref, expected_revision)):
            raise InvalidAttempt("invalid_request_identity")
        if trigger not in ("manual", "scheduled") or type(trigger) is not str:
            raise InvalidAttempt("invalid_trigger")
        if retry_of is not None and not _is_id(retry_of):
            raise InvalidAttempt("invalid_retry_reference")
        with self._lock:
            self._ready()
            prior = self._fetch(
                f"SELECT {self._select()} FROM runs WHERE request_id=?", (request_id,),
            )
            if prior is not None:
                data = dict(zip(_COLS, prior))
                if (data["binding_ref"], data["expected_revision"],
                    data["trigger"], data["retry_of"]) != (
                        binding_ref, expected_revision, trigger, retry_of):
                    raise InvalidAttempt("request_identity_conflict")
                return "replayed", _public(prior)

            if self._fetch("SELECT 1 FROM runs WHERE state='unconfirmed' LIMIT 1"):
                raise Busy("unconfirmed_effect")
            if self._fetch(
                "SELECT 1 FROM runs WHERE state IN ('queued','running') LIMIT 1"
            ):
                raise Busy("b3_attempt_in_progress")
            if retry_of is not None:
                parent = self._fetch(
                    f"SELECT {self._select()} FROM runs WHERE run_id=?", (retry_of,),
                )
                if parent is None:
                    raise InvalidAttempt("retry_parent_missing")
                old = dict(zip(_COLS, parent))
                if (old["state"] != "completed"
                        or old["terminal_code"] not in _SAFE_RETRY_PARENTS
                        or old["binding_ref"] != binding_ref):
                    raise InvalidAttempt("unsafe_retry_parent")
                if (old["terminal_code"] == "cas_conflict"
                        and old["expected_revision"] == expected_revision):
                    raise InvalidAttempt("stale_cas_retry")
                if self._fetch("SELECT 1 FROM runs WHERE retry_of=?", (retry_of,)):
                    raise InvalidAttempt("duplicate_retry_child")
            now = _when(self.clock())
            run_id = uuid.uuid4().hex
            with self.db:
                self.db.execute(
                    "INSERT INTO runs (run_id,request_id,binding_ref,"
                    "expected_revision,trigger,retry_of,created,state) "
                    "VALUES (?,?,?,?,?,?,?,'queued')",
                    (run_id, request_id, binding_ref, expected_revision,
                     trigger, retry_of, now),
                )
            return "accepted", self.lookup(run_id)

    def start(self, run_id: str) -> dict[str, object]:
        if not _is_id(run_id):
            raise InvalidAttempt("invalid_run_id")
        with self._lock:
            self._ready()
            row = self._fetch(
                f"SELECT {self._select()} FROM runs WHERE run_id=?", (run_id,),
            )
            if row is None:
                raise InvalidAttempt("unknown_run")
            data = dict(zip(_COLS, row))
            if data["state"] == "running":
                return _public(row)
            if data["state"] != "queued":
                raise InvalidAttempt("invalid_transition")
            now = _when(self.clock())
            if now < data["created"]:
                raise InvalidAttempt("clock_regression")
            with self.db:
                self.db.execute(
                    "UPDATE runs SET state='running', started=? WHERE run_id=?",
                    (now, run_id),
                )
            return self.lookup(run_id)

    def finish(self, run_id: str, *, terminal_code: str) -> dict[str, object]:
        """Record a producer-reported disposition, never a verified Vault win.

        Successful pipeline certification is deliberately outside this module.
        Uncertain effects are held, not retried automatically.
        """
        if not _is_id(run_id):
            raise InvalidAttempt("invalid_run_id")
        if type(terminal_code) is not str or terminal_code not in (
                _DETERMINISTIC_REFUSALS | (_UNCONFIRMED - {"interrupted_unknown"})):
            raise InvalidAttempt("invalid_terminal_code")
        with self._lock:
            self._ready()
            row = self._fetch(
                f"SELECT {self._select()} FROM runs WHERE run_id=?", (run_id,),
            )
            if row is None:
                raise InvalidAttempt("unknown_run")
            data = dict(zip(_COLS, row))
            if data["state"] in ("completed", "unconfirmed"):
                if data["terminal_code"] == terminal_code:
                    return _public(row)
                raise InvalidAttempt("terminal_conflict")
            if data["state"] != "running":
                raise InvalidAttempt("invalid_transition")
            now = _when(self.clock())
            if now < data["started"]:
                raise InvalidAttempt("clock_regression")
            state = ("completed" if terminal_code in _DETERMINISTIC_REFUSALS
                     else "unconfirmed")
            with self.db:
                self.db.execute(
                    "UPDATE runs SET state=?, terminal_code=?, completed=? "
                    "WHERE run_id=?", (state, terminal_code, now, run_id),
                )
            return self.lookup(run_id)

    def snapshot(self, *, recent: int = 32) -> dict[str, object]:
        """Only local admissions and producer-reported dispositions.

        last_verified_pipeline_success is ALWAYS null until a real B3 Job
        supplies separately attested CAS readback for all required stages.
        """
        if type(recent) is not int or not 1 <= recent <= 64:
            raise InvalidAttempt("invalid_limit")
        with self._lock:
            self._ready()
            rows = self.db.execute(
                f"SELECT {self._select()} FROM runs ORDER BY seq DESC LIMIT ?",
                (recent,),
            ).fetchall()
            failed = self._fetch(
                f"SELECT {self._select()} FROM runs WHERE "
                "terminal_code IN ('preflight_rejected','cas_conflict',"
                "'authority_rejected') ORDER BY seq DESC LIMIT 1"
            )
            uncertain = self._fetch(
                f"SELECT {self._select()} FROM runs "
                "WHERE state='unconfirmed' ORDER BY seq DESC LIMIT 1"
            )
            active = self._fetch(
                f"SELECT {self._select()} FROM runs "
                "WHERE state IN ('queued','running') ORDER BY seq DESC LIMIT 1"
            )
            return {
                "schema_version": VERSION,
                "integration": "unattached",
                "scope": "local_b3_attempts_only",
                "last_attempt": _public(rows[0]) if rows else None,
                "last_deterministic_refusal": _public(failed),
                "last_unconfirmed": _public(uncertain),
                "recent_attempts": [_public(r) for r in rows],
                "retrying": (
                    "recorded" if active is not None
                    and dict(zip(_COLS, active))["retry_of"] is not None
                    else "unknown"
                ),
                "active_incident": "unknown",
                "overall_health": "unknown",
                "last_verified_pipeline_success": None,
            }
