"""Single-process B3 *read-only* Project watcher scheduling and receipts.

The owner is the existing #298 nonroot app. This job never authorizes a Vault
Writer, Core Promotion, GitHub/Gitea push or a Gitea Runner. Its only remote
effect is the B4 source's pull-only Project mirror and GitHub GET observations.
It is deliberately OFF by default, and no collection occurs in constructor,
GET/status, HEAD or settings update.
"""
from __future__ import annotations

import fcntl
from contextlib import nullcontext
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
        phase_collector: Callable[[str], dict[str, object]] | None = None,
        phase_generation: Callable[[], str | None] | None = None,
        source_lock: Callable[[], object] | None = None,
    ):
        if (not isinstance(state, Path) or not state.is_absolute()
                or state.is_symlink() or not callable(collector)
                or not callable(configured) or not callable(generation)
                or not callable(clock)
                or (phase_collector is not None and not callable(phase_collector))
                or (phase_collector is not None
                    and (not callable(source_lock)
                         or not callable(phase_generation)))
                or (phase_generation is not None
                    and not callable(phase_generation))
                or (source_lock is not None and not callable(source_lock))):
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
        # A phase-admitted run must receive its exact source-generation
        # binding *inside* the collector's source acquisition. Manual and
        # legacy relative runs continue to use the existing zero-arg path.
        self.phase_collector = phase_collector
        # A phase slot must NOT fall back to the legacy Nextcloud-only
        # generation. Require a distinct GitHub-credential-bound callback.
        self.get_phase_generation = phase_generation
        # Callable factory for the trusted phase-only Source/Receipt guard.
        # Its acquisition order is DailySetup._lock -> SetupStore._lock,
        # exactly matching the B4 source acquisition path. A fresh context
        # manager is created per terminal attempt and held through COMMIT;
        # never retain these locks across remote provider I/O.
        self.source_lock = source_lock
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

    def _phase_generation(self) -> str | None:
        if self.get_phase_generation is None:
            return None
        return _source_generation(self.is_configured, self.get_phase_generation)

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
                "configuration_missing" if current is None
                else "source_changed" if enabled and not valid
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
            # The new phase-based scheduler and the legacy relative interval
            # must never both be enabled. Both admissions use this same Job
            # lock and SQLite Owner, not a separate UI or systemd authority.
            if payload["enabled"] and self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='phase_settings'"
            ).fetchone() and self.db.execute(
                "SELECT enabled FROM phase_settings WHERE id=1"
            ).fetchone()[0]:
                raise Invalid("b3_phase_schedule_already_enabled")
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
                "SELECT run_id FROM runs ORDER BY rowid DESC LIMIT 1"
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
            self._reconcile_dead_worker()
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
            self._finish(run_id, status="unknown", reason="worker_unavailable")
            # Preserve the reference until a later submit/tick can
            # reconcile any still-queued record after a failed terminal DB
            # update. No worker ever started, so it is safe to retry that
            # local reconciliation without re-running a collector.
            raise

    def _completion_time(self, created: float) -> float:
        # A broken injected clock must not leave a durable run in "running".
        # Never persist a completion before admission if the wall clock moved.
        try:
            return max(created, _bounded_now(self.clock))
        except Exception:
            return max(created, _bounded_now(time.time))

    def _finish(
        self, run_id: str, *, status: str, reason: str,
        count: int = 0, pending: int = 0,
        expected_generation: str | None = None,
    ) -> bool:
        """Bounded terminal write; persistent DB faults remain fail-closed.

        Positive phase results require one last source-generation check
        while the phase receipt guard holds DailySetup._lock THEN the shared
        SetupStore._lock through SQLite COMMIT, mirroring source acquisition.
        This short local critical section never includes provider I/O;
        manual/relative runs preserve their previous receipt semantics.
        """
        for attempt in range(3):
            try:
                with self._lock:
                    guard = (
                        self.source_lock()
                        if status == "observed" and expected_generation is not None
                        else nullcontext()
                    )
                    with guard:
                        with self.db:
                            row = self.db.execute(
                                "SELECT created FROM runs WHERE run_id=?", (run_id,)
                            ).fetchone()
                            if row is None:
                                return False
                            if (status == "observed" and expected_generation is not None
                                    and self._phase_generation() != expected_generation):
                                status, reason, count, pending = (
                                    "unknown", "source_changed", 0, 0
                                )
                            at = self._completion_time(float(row[0]))
                            self.db.execute(
                                "UPDATE runs SET status=?,reason=?,observations=?,"
                                "pending_proposals=?,completed=? WHERE run_id=?",
                                (status, reason, count, pending, at, run_id),
                            )
                return True
            except (sqlite3.Error, OSError, ValueError):
                if attempt != 2:
                    time.sleep(0.05 * (attempt + 1))
        return False

    def _reconcile_dead_worker(self) -> None:
        """Admission-only recovery, never a GET side effect.

        This is called under self._lock by submit/tick. A live worker may
        still own its observation; a *dead* worker may not. No automatic
        collection retry is admitted under the same request ID.
        """
        worker = self._worker
        if worker is None or worker.is_alive():
            return
        try:
            rows = self.db.execute(
                "SELECT run_id FROM runs WHERE status IN ('queued','running')"
            ).fetchall()
        except sqlite3.Error:
            # A temporarily unavailable local journal cannot prove the
            # worker is complete. Leave it pending until the next healthy
            # scheduler tick; do not admit an overlapping collection here.
            return
        for (run_id,) in rows:
            if not self._finish(
                run_id, status="unknown", reason="worker_interrupted"
            ):
                return

    def _phase_generation_for_run(self, run_id: str) -> str | None:
        """A phase slot's source binding, if this job was phase-admitted.

        Legacy manual/relative runs and old B3 databases have no phase slot.
        Phase admissions and run rows were committed in one SQLite
        transaction by B3PhaseRuntime, so this lookup is durable across
        thread-start delays and cannot depend on an in-memory map.
        """
        if self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='phase_slots'"
        ).fetchone() is None:
            return None
        row = self.db.execute(
            "SELECT s.generation FROM phase_slots AS s "
            "JOIN runs AS r ON s.request_id=r.request_id "
            "WHERE r.run_id=?", (run_id,)
        ).fetchone()
        return row[0] if row else None

    def _execute(self, run_id: str) -> None:
        status, reason, count, pending = "unknown", "collector_unavailable", 0, 0
        expected_generation = None
        try:
            with self._lock:
                with self.db:
                    self.db.execute(
                        "UPDATE runs SET status='running' WHERE run_id=?", (run_id,)
                    )
                    expected_generation = self._phase_generation_for_run(run_id)
            # Connection rotation after a phase slot was admitted but before
            # the worker runs must never authorize a collection from the
            # replacement source under the old schedule binding.
            if (expected_generation is not None
                    and self._phase_generation() != expected_generation):
                status, reason = "unknown", "source_changed"
            elif expected_generation is not None and self.phase_collector is None:
                # No source-bound observer is configured. Never silently
                # fall back to an unpinned collector on a phase admission.
                status, reason = "unknown", "source_binding_unavailable"
            else:
                try:
                    value = (
                        self.collector() if expected_generation is None
                        else self.phase_collector(expected_generation)
                    )
                    # A mid-flight source swap must not turn an old-source
                    # admission into an observed receipt for the new source.
                    if (expected_generation is not None
                            and self._phase_generation() != expected_generation):
                        status, reason = "unknown", "source_changed"
                    else:
                        status, reason, count, pending = _result(value)
                except BaseException:
                    # A collector process crash/cancellation is never evidence
                    # that GitHub observations were fully verified.
                    pass
        except BaseException:
            status, reason, count, pending = "unknown", "worker_unavailable", 0, 0
        self._finish(run_id, status=status, reason=reason,
                     count=count, pending=pending,
                     expected_generation=expected_generation)

    def tick(self) -> bool:
        """Called by the existing single in-app scheduler; never runs inline."""
        with self._lock:
            self._reconcile_dead_worker()
            s = self._settings()
            if s["enabled"] and not s["effective_enabled"]:
                # Missing / temporarily unavailable configuration is not
                # evidence of rotation. Pause the current tick but preserve
                # the desired schedule for automatic recovery of the same
                # source. Only an actual different valid generation revokes.
                current = self._generation()
                bound = self.db.execute(
                    "SELECT generation FROM settings WHERE id=1"
                ).fetchone()[0]
                if current is not None and current != bound:
                    with self.db:
                        self.db.execute(
                            "UPDATE settings SET enabled=0,next_due=NULL,"
                            "generation=NULL WHERE id=1"
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
