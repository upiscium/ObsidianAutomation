"""Persistent B4 Preview job: one owner, durable receipts, no separate scheduler.

The #298 application's existing scheduler calls tick(). Success means only
private, verified Preview artifacts are ready; it never means a Vault write.
"""
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
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_DATE = re.compile(r"20\d{2}-\d{2}-\d{2}\Z")


class Invalid(ValueError):
    pass


class Busy(RuntimeError):
    pass


class DailyJob:
    def __init__(self, state: Path, runner: Callable[[], dict],
                 *, configured: Callable[[], bool] = lambda: False,
                 publisher: Callable[[dict], dict] | None = None,
                 clock: Callable[[], float] = time.time):
        self.state = state
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock_file = (state / "daily.lock").open("a+b")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise Busy("daily_owner_busy") from exc
        try:
            self.db = sqlite3.connect(state / "daily.sqlite3", check_same_thread=False)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("CREATE TABLE IF NOT EXISTS settings "
                            "(id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL, "
                            "interval_seconds INTEGER NOT NULL, next_due REAL)")
            self.db.execute("INSERT OR IGNORE INTO settings VALUES (1,0,86400,NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS runs ("
                            "run_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, "
                            "trigger TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL, "
                            "completed REAL, date_jst TEXT, capture_sha256 TEXT, digest_sha256 TEXT, "
                            "original_event_count INTEGER, rendered_entity_count INTEGER)")
            # Preserve previous SQLite Job state across preview-to-writer
            # upgrades. No existing records are replayed.
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}
            if "publication_outcome" not in columns:
                self.db.execute("ALTER TABLE runs ADD COLUMN publication_outcome TEXT")
            self.db.execute("UPDATE runs SET status='unconfirmed',completed=? "
                            "WHERE status IN ('queued','running')", (clock(),))
            self.db.commit()
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
            raise
        self.runner = runner
        self.is_configured = configured
        # None preserves the existing Preview-only behavior. An explicitly
        # supplied publisher is the only route to canonical WebDAV writes.
        self.publisher = publisher
        self.clock = clock
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        if self._worker:
            self._worker.join(timeout=15)
            if self._worker.is_alive():
                raise Busy("daily_worker_still_running")
        self.db.close()
        fcntl.flock(self._lock_file, fcntl.LOCK_UN)
        self._lock_file.close()

    def configured(self) -> bool:
        try:
            return self.is_configured() is True
        except Exception:
            return False

    def settings(self) -> dict:
        with self._lock:
            enabled, interval, due = self.db.execute(
                "SELECT enabled,interval_seconds,next_due FROM settings WHERE id=1"
            ).fetchone()
            return {"enabled": bool(enabled), "interval_seconds": interval,
                    "next_due": due if enabled else None,
                    "configured": self.configured()}

    def update(self, payload: dict) -> dict:
        if (type(payload) is not dict or set(payload) != {"enabled","interval_seconds"}
                or type(payload["enabled"]) is not bool
                or type(payload["interval_seconds"]) is not int
                or not 3600 <= payload["interval_seconds"] <= 86400):
            raise Invalid("invalid_daily_settings")
        with self._lock:
            if payload["enabled"] and not self.configured():
                raise Invalid("daily_connections_missing")
            old = self.settings()
            due = (self.clock() + payload["interval_seconds"] if payload["enabled"] and
                   (not old["enabled"] or old["interval_seconds"] != payload["interval_seconds"])
                   else old["next_due"] if payload["enabled"] else None)
            with self.db:
                self.db.execute("UPDATE settings SET enabled=?,interval_seconds=?,next_due=? WHERE id=1",
                                (int(payload["enabled"]),payload["interval_seconds"],due))
            return self.settings()

    def run(self, run_id: str) -> dict | None:
        if type(run_id) is not str or _ID.fullmatch(run_id) is None:
            return None
        with self._lock:
            row = self.db.execute(
                "SELECT run_id,request_id,trigger,status,created,completed,date_jst,"
                "capture_sha256,digest_sha256,original_event_count,rendered_entity_count,"
                "publication_outcome FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return dict(zip(("run_id","request_id","trigger","status","created","completed",
                             "date_jst","capture_sha256","digest_sha256",
                             "original_event_count","rendered_entity_count",
                             "publication_outcome"), row)) if row else None

    def status(self) -> dict:
        with self._lock:
            row = self.db.execute("SELECT run_id FROM runs ORDER BY created DESC,rowid DESC LIMIT 1").fetchone()
            return {"version":1, "job":"github_daily_preview",
                    "publication_enabled": self.publisher is not None,
                    "settings":self.settings(),
                    "last_run":self.run(row[0]) if row else None}

    def submit(self, request_id: str, trigger: str = "manual") -> tuple[str, dict | None]:
        if (type(request_id) is not str or _ID.fullmatch(request_id) is None
                or trigger not in ("manual","scheduled")):
            raise Invalid("invalid_daily_request")
        with self._lock:
            row = self.db.execute("SELECT run_id,trigger FROM runs WHERE request_id=?", (request_id,)).fetchone()
            if row:
                if row[1] != trigger:
                    raise Invalid("daily_request_conflict")
                return "replayed", self.run(row[0])
            if not self.configured():
                return "unconfigured", None
            if self.db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                return "busy", None
            run_id = uuid.uuid4().hex
            with self.db:
                self.db.execute("INSERT INTO runs (run_id,request_id,trigger,status,created) "
                                "VALUES (?,?,?,?,?)", (run_id,request_id,trigger,"queued",self.clock()))
            self._launch_worker(run_id)
            return "accepted", self.run(run_id)

    def _launch_worker(self, run_id: str) -> None:
        # Only launch after all relevant job/schedule state is durable.
        self._worker = threading.Thread(target=self._execute, args=(run_id,), daemon=False)
        try:
            self._worker.start()
        except Exception:
            self._worker = None
            with self.db:
                self.db.execute("UPDATE runs SET status='unconfirmed',completed=? "
                                "WHERE run_id=? AND status='queued'", (self.clock(),run_id))
            raise

    @staticmethod
    def _validate_result(value: object) -> dict:
        if (type(value) is not dict or value.get("version") != 1 or value.get("status") != "preview_ready"
                or value.get("model_used") is not False
                or value.get("publication_authorized") is not False
                or value.get("vault_modified") is not False):
            raise Invalid("not_a_nonpublishing_preview")
        if (type(value.get("date_jst")) is not str or _DATE.fullmatch(value["date_jst"]) is None
                or any(type(value.get(k)) is not str or _SHA.fullmatch(value[k]) is None
                       for k in ("capture_sha256","digest_sha256"))
                or any(type(value.get(k)) is not int or not 0 <= value[k] <= 100000
                       for k in ("original_event_count","rendered_entity_count"))):
            raise Invalid("invalid_preview_result")
        return value

    @staticmethod
    def _validate_publication(value: object, preview: dict) -> str:
        if (
            type(value) is not dict or value.get("version") != 1
            or value.get("status") != "published"
            or value.get("publication_verified") is not True
            or value.get("model_used") is not False
            or type(value.get("vault_modified")) is not bool
            or value.get("publication_outcome") not in
                ("applied", "recovered", "already_desired")
            or any(value.get(k) != preview[k] for k in
                   ("date_jst", "capture_sha256", "digest_sha256"))
            or any(type(value.get(k)) is not str or _SHA.fullmatch(value[k]) is None
                   for k in ("before_content_sha256", "after_content_sha256"))
        ):
            raise Invalid("publication_result_not_verified")
        before, after = value["before_content_sha256"], value["after_content_sha256"]
        if (value["vault_modified"] != (before != after)
                or (value["publication_outcome"] == "already_desired" and before != after)
                or (value["publication_outcome"] != "already_desired" and before == after)):
            raise Invalid("publication_effect_inconsistent")
        return value["publication_outcome"]

    def _execute(self, run_id: str) -> None:
        with self._lock:
            with self.db:
                self.db.execute("UPDATE runs SET status='running' WHERE run_id=?", (run_id,))
        publication = None
        try:
            data = self._validate_result(self.runner())
            result = "preview_ready"
            if self.publisher is not None:
                try:
                    publication = self._validate_publication(self.publisher(data), data)
                    result = "published"
                except Exception:
                    # The local, validated Preview is still useful; do not
                    # erase its evidence when a remote CAS write fails.
                    result, publication = "publish_failed", None
        except Exception:
            data, result = {}, "failed"
        with self._lock:
            with self.db:
                self.db.execute(
                    "UPDATE runs SET status=?,completed=?,date_jst=?,capture_sha256=?,"
                    "digest_sha256=?,original_event_count=?,rendered_entity_count=?,"
                    "publication_outcome=? WHERE run_id=?",
                    (result,self.clock(),data.get("date_jst"),data.get("capture_sha256"),
                     data.get("digest_sha256"),data.get("original_event_count"),
                     data.get("rendered_entity_count"),publication,run_id))

    def tick(self) -> bool:
        """The parent Snapshot app invokes this from its one scheduler thread."""
        with self._lock:
            s = self.settings()
            if not (s["enabled"] and s["configured"] and s["next_due"] is not None
                    and self.clock() >= s["next_due"]):
                return False
            if self.db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone():
                return False
            # Job admission and the next schedule must commit atomically.
            # A crash after commit cannot leave an expired due time which
            # re-admits an unconfirmed source collection on restart.
            run_id, request_id = uuid.uuid4().hex, uuid.uuid4().hex
            with self.db:
                self.db.execute(
                    "INSERT INTO runs (run_id,request_id,trigger,status,created) "
                    "VALUES (?,?,'scheduled','queued',?)",
                    (run_id,request_id,self.clock()))
                self.db.execute("UPDATE settings SET next_due=? WHERE id=1",
                                (self.clock()+s["interval_seconds"],))
            self._launch_worker(run_id)
            return True
