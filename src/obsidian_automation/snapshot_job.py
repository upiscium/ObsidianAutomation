"""Single-owner nonroot Snapshot job. Construction never starts automation."""
from __future__ import annotations

import fcntl
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

_ID = re.compile(r"[a-f0-9]{32}\Z")
TERMINAL = {"saved", "no_changes", "failed", "unconfirmed"}


class Busy(Exception):
    pass


class Invalid(Exception):
    pass


class SnapshotJob:
    """Own one SQLite state directory and one executing worker (exclusive OS lock).

    The caller explicitly calls tick()/start_scheduler(); a queued/running run
    recovered after a crash is indeterminate and is never replayed.
    """

    def __init__(self, state: Path, runner: Callable[[str], tuple[str, str, str | None]],
                 *, configured: bool = True, clock: Callable[[], float] = time.time,
                 connection_generation: str | None = None):
        self.state = state
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock_file = (state / "snapshot.lock").open("a+b")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise Busy("snapshot_owner_busy") from exc
        self.db = sqlite3.connect(state / "snapshot.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL, interval INTEGER NOT NULL, next_due REAL, source_name TEXT NOT NULL, destination_name TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, trigger TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, completed REAL, stage TEXT NOT NULL, remote_ref TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS connection_write (id INTEGER PRIMARY KEY CHECK(id=1), verified INTEGER NOT NULL)")
        self.db.execute("INSERT OR IGNORE INTO connection_write VALUES (1,0)")
        self.db.execute("CREATE TABLE IF NOT EXISTS connection_binding (id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT)")
        if connection_generation is not None:
            previous = self.db.execute("SELECT generation FROM connection_binding WHERE id=1").fetchone()
            if previous is None or previous[0] != connection_generation:
                self.db.execute("UPDATE settings SET enabled=0,next_due=NULL WHERE id=1")
                self.db.execute("UPDATE connection_write SET verified=0 WHERE id=1")
                self.db.execute("INSERT OR REPLACE INTO connection_binding VALUES (1,?)", (connection_generation,))
        self.db.execute("INSERT OR IGNORE INTO settings VALUES (1,0,300,NULL,'nextcloud','gitea')")
        if not configured:
            self.db.execute("UPDATE settings SET enabled=0,next_due=NULL WHERE id=1")
            self.db.execute("UPDATE connection_write SET verified=0 WHERE id=1")
        self.db.execute("UPDATE runs SET status='unconfirmed',stage='interrupted',completed=? WHERE status IN ('queued','running')", (clock(),))
        self.db.commit()
        self.runner, self.configured, self.clock = runner, configured, clock
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._scheduler: threading.Thread | None = None

    def close(self) -> None:
        self._stop.set()
        if self._scheduler:
            self._scheduler.join(timeout=3)
        if self._worker:
            self._worker.join(timeout=10)
            if self._worker.is_alive():
                raise Busy("worker_still_running")
        self.db.close()
        fcntl.flock(self._lock_file, fcntl.LOCK_UN)
        self._lock_file.close()

    def settings(self) -> dict:
        with self._lock:
            enabled, interval, due, source, destination = self.db.execute(
                "SELECT enabled,interval,next_due,source_name,destination_name FROM settings WHERE id=1"
            ).fetchone()
            return dict(version=1, configured=self.configured, enabled=bool(enabled),
                        interval_seconds=interval, next_due=due if enabled else None,
                        source_name=source, destination_name=destination,
                        gitea_write="verified" if self.db.execute("SELECT verified FROM connection_write WHERE id=1").fetchone()[0] else "unverified")

    def connections_saved(self, generation: str | None = None) -> None:
        """Caller holds the job lock across probe/save and this schedule reset."""
        with self._lock:
            with self.db:
                self.db.execute("UPDATE settings SET enabled=0,next_due=NULL WHERE id=1")
                self.db.execute("UPDATE connection_write SET verified=0 WHERE id=1")
                if generation is not None:
                    self.db.execute("INSERT OR REPLACE INTO connection_binding VALUES (1,?)", (generation,))
            self.configured = True

    def update(self, payload: dict) -> dict:
        if type(payload) is not dict or set(payload) != {"enabled", "interval_seconds", "source_name", "destination_name"}:
            raise Invalid("invalid_settings")
        enabled, interval = payload["enabled"], payload["interval_seconds"]
        if (type(enabled) is not bool or type(interval) is not int or not 60 <= interval <= 86400
                or payload["source_name"] != "nextcloud" or payload["destination_name"] != "gitea"):
            raise Invalid("invalid_settings")
        with self._lock:
            previous = self.settings()
            due = (self.clock() + interval if enabled and
                   (not previous["enabled"] or interval != previous["interval_seconds"])
                   else previous["next_due"] if enabled else None)
            with self.db:
                self.db.execute("UPDATE settings SET enabled=?,interval=?,next_due=? WHERE id=1",
                                (enabled, interval, due))
            return self.settings()

    def run(self, run_id: str) -> dict | None:
        if not _ID.fullmatch(run_id):
            return None
        with self._lock:
            row = self.db.execute("SELECT run_id,request_id,trigger,status,created,completed,stage,remote_ref FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return dict(zip(("run_id", "request_id", "trigger", "status", "created", "completed", "stage", "remote_ref"), row)) if row else None

    def status(self) -> dict:
        with self._lock:
            row = self.db.execute("SELECT run_id FROM runs ORDER BY created DESC,rowid DESC LIMIT 1").fetchone()
            return dict(version=1, job="vault_snapshot", settings=self.settings(),
                        last_run=self.run(row[0]) if row else None)

    def submit(self, request_id: str, trigger: str = "manual") -> tuple[str, dict | None]:
        if not isinstance(request_id, str) or not _ID.fullmatch(request_id) or trigger not in ("manual", "scheduled"):
            raise Invalid("invalid_request")
        with self._lock:
            row = self.db.execute("SELECT run_id,trigger FROM runs WHERE request_id=?", (request_id,)).fetchone()
            if row:
                if row[1] != trigger:
                    raise Invalid("request_conflict")
                return "replayed", self.run(row[0])
            if not self.configured:
                return "unconfigured", None
            if self.db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                return "busy", None
            run_id = uuid.uuid4().hex
            with self.db:
                self.db.execute("INSERT INTO runs VALUES (?,?,?,?,?,NULL,?,NULL)",
                                (run_id, request_id, trigger, "queued", self.clock(), "queued"))
            self._worker = threading.Thread(target=self._execute, args=(run_id,), daemon=False)
            self._worker.start()
            return "accepted", self.run(run_id)

    def _execute(self, run_id: str) -> None:
        with self._lock:
            with self.db:
                self.db.execute("UPDATE runs SET status='running',stage='running' WHERE run_id=?", (run_id,))
        try:
            status, stage, remote_ref = self.runner(run_id)
            if status not in TERMINAL or stage not in {"sync", "fetch", "snapshot", "push", "verify", "interrupted"} or (remote_ref is not None and not re.fullmatch(r"[a-f0-9]{40}", remote_ref)):
                raise Invalid("invalid_runner_result")
        except Exception:
            status, stage, remote_ref = "unconfirmed", "interrupted", None
        with self._lock:
            with self.db:
                self.db.execute("UPDATE runs SET status=?,stage=?,remote_ref=?,completed=? WHERE run_id=?",
                                (status, stage, remote_ref, self.clock(), run_id))
                if status == "saved":
                    self.db.execute("UPDATE connection_write SET verified=1 WHERE id=1")

    def tick(self) -> bool:
        with self._lock:
            settings = self.settings()
            if not settings["enabled"] or not self.configured or settings["next_due"] is None or self.clock() < settings["next_due"]:
                return False
            if self.db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                return False
            with self.db:
                self.db.execute("UPDATE settings SET next_due=? WHERE id=1", (self.clock() + settings["interval_seconds"],))
            return self.submit(uuid.uuid4().hex, "scheduled")[0] == "accepted"

    def start_scheduler(self, *, tick_hooks: tuple[Callable[[], bool], ...] = ()) -> None:
        """One application scheduler may tick other independently persisted jobs."""
        if self._scheduler is not None:
            raise Busy("scheduler_already_started")
        if type(tick_hooks) is not tuple or any(not callable(t) for t in tick_hooks):
            raise Invalid("invalid_scheduler_hooks")
        self._tick_hooks = tick_hooks
        self._scheduler = threading.Thread(target=self._schedule_loop, daemon=False)
        self._scheduler.start()

    def _schedule_loop(self) -> None:
        while not self._stop.wait(1):
            for tick in (self.tick, *self._tick_hooks):
                try:
                    tick()
                except Exception:
                    # One job failure cannot disable scheduling of other jobs.
                    # Never auto-replay an uncertain run; its owner tracks it.
                    pass
