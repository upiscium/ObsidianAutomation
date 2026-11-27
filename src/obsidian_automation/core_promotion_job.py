"""Single-owner, fail-closed Core Promotion Job for the three-service app.

The Job owns only scheduling, admission and durable *observations*. The existing
promotion planner, conditional WebDAV writer and checkpoint own the actual
effect. No queued/running action is replayed across a process restart.
"""
from __future__ import annotations

import fcntl
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Mapping

from .job_health import job_health
from .phase_schedule import (
    StaleScheduleError, due_after_tick, due_for_update, missed,
    parse_update, schedule_status, upgrade_settings,
)

_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TERMINAL = {"promoted", "up_to_date", "live_drift", "conflict", "failed", "indeterminate"}


class Invalid(ValueError):
    pass


class Busy(RuntimeError):
    pass


class CorePromotionJob:
    """One local Worker, one SQLite scheduler and a process-exclusive lock."""

    def __init__(
        self,
        state: Path,
        runner: Callable[[], Mapping[str, object]],
        *,
        configured: Callable[[], bool] = lambda: False,
        source_generation: Callable[[], str | None] = lambda: None,
        clock: Callable[[], float] = time.time,
    ):
        self.state = state
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock_file = (state / "job.lock").open("a+b")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise Busy("core_promotion_owner_busy") from exc
        self.db = None
        try:
            self.db = sqlite3.connect(state / "job.sqlite3", check_same_thread=False)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL, interval_seconds INTEGER NOT NULL, next_due REAL, source_generation TEXT)")
            self.db.execute("INSERT OR IGNORE INTO settings (id,enabled,interval_seconds,next_due,source_generation) VALUES (1,0,3600,NULL,NULL)")
            upgrade_settings(self.db)
            self.db.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, trigger TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, completed REAL, base_commit TEXT, head_commit TEXT, plan_sha256 TEXT, receipt_sha256 TEXT, source_generation TEXT)")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}
            if "source_generation" not in columns:
                self.db.execute("ALTER TABLE runs ADD COLUMN source_generation TEXT")
            self.db.execute("UPDATE runs SET status='indeterminate',completed=? WHERE status IN ('queued','running')", (clock(),))
            self.db.commit()
        except BaseException:
            if self.db is not None:
                self.db.close()
            self._lock_file.close()
            raise
        self.runner = runner
        self.is_configured = configured
        self.source_generation = source_generation
        self.clock = clock
        self._lock = threading.RLock()
        # Shared with credential rotation and checkpoint initialization.
        self._effect_gate = threading.Lock()
        self._worker: threading.Thread | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._worker is not None:
            self._worker.join(timeout=15)
            if self._worker.is_alive():
                raise Busy("core_promotion_worker_still_running")
        self.db.close()
        fcntl.flock(self._lock_file, fcntl.LOCK_UN)
        self._lock_file.close()

    def configured(self):
        try:
            return self.is_configured() is True and self._generation() is not None
        except Exception:
            return False

    def _generation(self):
        value = self.source_generation()
        return value if type(value) is str and _DIGEST.fullmatch(value) else None

    def settings(self):
        with self._lock:
            enabled, interval, due, generation, tz, anchor, revision = self.db.execute(
                "SELECT enabled,interval_seconds,next_due,source_generation,"
                "schedule_tz,schedule_anchor,schedule_revision FROM settings WHERE id=1"
            ).fetchone()
            current = self._generation()
            bound = (not enabled or (current is not None and current == generation))
            return {"enabled": bool(enabled), "interval_seconds": interval,
                    "next_due": due if enabled else None,
                    "configured": self.configured(), "source_bound": bound,
                    "schedule": schedule_status(tz,anchor,revision)}

    def _unresolved(self) -> bool:
        """An ambiguous past effect requires operator reconciliation."""
        return self.db.execute(
            "SELECT 1 FROM runs WHERE status='indeterminate' LIMIT 1"
        ).fetchone() is not None

    def update(self, payload: dict):
        with self._lock:
            old = self.settings()
            try:
                enabled,interval,tz,anchor = parse_update(
                    payload, prefix=frozenset({"enabled","interval_seconds"}),
                    minimum=60, old=old)
            except StaleScheduleError:
                raise
            except ValueError:
                raise Invalid("invalid_core_promotion_settings") from None
            if enabled and not self.configured():
                raise Invalid("core_promotion_not_configured")
            if enabled and self._unresolved():
                raise Invalid("core_promotion_requires_reconciliation")
            if (enabled == old["enabled"]
                and interval == old["interval_seconds"]
                and tz == old["schedule"]["timezone"]
                and anchor == old["schedule"]["anchor_local_time"]
                and (not enabled or old["source_bound"])):
                return old
            gen = self._generation() if enabled else None
            due = due_for_update(self.clock(), old, enabled, interval, tz, anchor)
            if enabled and not old["source_bound"]:
                due = (due_after_tick(self.clock(),interval,{
                    "mode": "anchored" if tz else "legacy_interval",
                    "timezone": tz, "anchor_local_time": anchor}) if tz else self.clock()+interval)
            with self.db:
                self.db.execute(
                    "UPDATE settings SET enabled=?,interval_seconds=?,next_due=?,"
                    "source_generation=?,schedule_tz=?,schedule_anchor=?,"
                    "schedule_revision=? WHERE id=1",
                    (int(enabled),interval,due,gen,tz,anchor,
                     old["schedule"]["revision"]+1))
            return self.settings()

    def source_invalidated(self):
        """Pause and invalidate stale browser revisions on source rotation.

        Invoked only under the existing Job/connection fence. It never
        submits a run and does not overwrite the phase/interval settings.
        """
        with self._lock:
            with self.db:
                self.db.execute(
                    "UPDATE settings SET enabled=0,next_due=NULL,source_generation=NULL,schedule_revision=schedule_revision+1 WHERE id=1"
                )
        return self.settings()

    def run(self, run_id: str):
        if type(run_id) is not str or not _ID.fullmatch(run_id):
            return None
        with self._lock:
            row = self.db.execute(
                "SELECT run_id,request_id,trigger,status,created,completed,base_commit,head_commit,plan_sha256,receipt_sha256,source_generation FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            result = dict(zip(("run_id", "request_id", "trigger", "status",
                               "created", "completed", "base_commit", "head_commit",
                               "plan_sha256", "receipt_sha256"), row[:10]))
            # Browser sees a Boolean only, never credential-derived digests.
            result["source_matches_current"] = (
                row[10] is not None and row[10] == self._generation()
                and self.configured()
            )
            return result

    def status(self):
        with self._lock:
            last = self.db.execute(
                "SELECT run_id FROM runs ORDER BY created DESC,rowid DESC LIMIT 1"
            ).fetchone()
            return {"version": 1, "job": "core_promotion", "settings": self.settings(),
                    "last_run": self.run(last[0]) if last else None}

    def health(self) -> dict:
        """Consecutive-failure streak for the panel banner (counts and times only)."""
        return job_health(self.db, self._lock, service="core_promotion", success=("promoted", "up_to_date"),
                          failure=("failed", "conflict", "live_drift", "indeterminate"), threshold=3)

    def submit(self, request_id: str, trigger: str = "manual"):
        if (type(request_id) is not str or not _ID.fullmatch(request_id)
                or trigger not in ("manual", "scheduled")):
            raise Invalid("invalid_core_promotion_request")
        with self._lock:
            old = self.db.execute(
                "SELECT run_id,trigger FROM runs WHERE request_id=?", (request_id,)
            ).fetchone()
            if old:
                if old[1] != trigger:
                    raise Invalid("core_promotion_request_conflict")
                return "replayed", self.run(old[0])
            settings = self.settings()
            if not settings["configured"] or not settings["source_bound"]:
                return "unconfigured", None
            if self._unresolved():
                return "needs_reconciliation", None
            if self.db.execute(
                "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone():
                return "busy", None
            run_id = uuid.uuid4().hex
            generation = self._generation()
            if generation is None:
                return "unconfigured", None
            with self.db:
                self.db.execute(
                    "INSERT INTO runs (run_id,request_id,trigger,status,created,source_generation) VALUES (?,?,?,?,?,?)",
                    (run_id, request_id, trigger, "queued", self.clock(), generation),
                )
            self._worker = threading.Thread(target=self._execute, args=(run_id,), daemon=False)
            try:
                self._worker.start()
            except Exception:
                with self.db:
                    self.db.execute(
                        "UPDATE runs SET status='indeterminate',completed=? WHERE run_id=?",
                        (self.clock(), run_id),
                    )
                raise
            return "accepted", self.run(run_id)

    def _execute(self, run_id: str):
        with self._lock:
            with self.db:
                self.db.execute("UPDATE runs SET status='running' WHERE run_id=?", (run_id,))
        status, base, head, plan, receipt = "indeterminate", None, None, None, None
        try:
            with self._effect_gate:
                result = self.runner()
            if not isinstance(result, Mapping) or set(result) != {
                "status", "base_commit", "head_commit", "plan_sha256", "receipt_sha256"
            }:
                raise Invalid("invalid_core_promotion_result")
            status = result["status"]
            base, head = result["base_commit"], result["head_commit"]
            plan, receipt = result["plan_sha256"], result["receipt_sha256"]
            if (status not in _TERMINAL or status == "indeterminate"
                or any(type(v) is not str or _SHA.fullmatch(v) is None for v in (base, head))
                or any(v is not None and (type(v) is not str or _DIGEST.fullmatch(v) is None)
                       for v in (plan, receipt))
                or (status == "promoted" and (plan is None or receipt is None))):
                raise Invalid("invalid_core_promotion_result")
        except Exception:
            # An exception after a remote PUT could mean a committed effect.
            # Do not mark it safely failed and do not retry automatically.
            status, base, head, plan, receipt = "indeterminate", None, None, None, None
        with self._lock:
            with self.db:
                self.db.execute(
                    "UPDATE runs SET status=?,completed=?,base_commit=?,head_commit=?,plan_sha256=?,receipt_sha256=? WHERE run_id=?",
                    (status, self.clock(), base, head, plan, receipt, run_id),
                )

    def tick(self):
        with self._lock:
            settings = self.settings()
            if self._unresolved():
                return False
            now = self.clock()
            due = settings["next_due"]
            if (not settings["enabled"] or not settings["configured"]
                or not settings["source_bound"] or due is None or now < due):
                return False
            busy = self.db.execute(
                "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone() is not None
            if busy or missed(now,due,settings["schedule"]):
                if settings["schedule"]["mode"] == "anchored":
                    with self.db:
                        self.db.execute("UPDATE settings SET next_due=? WHERE id=1",
                                        (due_after_tick(now,settings["interval_seconds"],
                                                        settings["schedule"]),))
                return False
            with self.db:
                self.db.execute("UPDATE settings SET next_due=? WHERE id=1",
                                (due_after_tick(now,settings["interval_seconds"],
                                                settings["schedule"]),))
            return self.submit(uuid.uuid4().hex, "scheduled")[0] == "accepted"
