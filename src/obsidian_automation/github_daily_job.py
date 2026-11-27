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

from .github_daily_progress import DailyProgressTargetMissingBeforeWrite
from .job_health import job_health

from .phase_schedule import (
    StaleScheduleError, due_after_tick, due_for_update, missed,
    parse_update, schedule_status, upgrade_settings,
)

_ID = re.compile(r"[a-f0-9]{32}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_DATE = re.compile(r"20\d{2}-\d{2}-\d{2}\Z")


class Invalid(ValueError):
    pass


class Busy(RuntimeError):
    pass


# Finite diagnostic vocabulary. Never persist a provider error, URL or
# arbitrary exception message in a public HTTP-visible SQLite receipt.
_FAILURE_PAIRS = frozenset({
    ("source_admission", "not_configured"),
    ("source_admission", "source_unavailable"),
    ("source_pull", "pull_failed"),
    ("project_selection", "no_watched_projects"),
    ("source_verification", "source_changed"),
    ("collection_preview", "collection_or_preview_failed"),
    ("runner", "unexpected_exception"),
    ("preview_validation", "invalid_preview_receipt"),
    ("publication", "outcome_unconfirmed"),
    # #584: the pin check right before the PUT refused; nothing was sent.
    ("publication", "vault_root_refused"),
    # The Daily Note did not exist at the read before the PUT; nothing was sent.
    ("publication", "target_missing"),
    ("publication", "invalid_publication_receipt"),
})


class DailyStageFailure(RuntimeError):
    """Trusted B4 stage marker; never carry a raw exception message."""

    def __init__(self, stage: str, code: str):
        if (type(stage) is not str or type(code) is not str
                or (stage, code) not in _FAILURE_PAIRS):
            raise Invalid("invalid_daily_failure_diagnostic")
        self.stage, self.code = stage, code
        super().__init__("daily_stage_failed")


class DailyJob:
    def __init__(self, state: Path, runner: Callable[[], dict],
                 *, configured: Callable[[], bool] = lambda: False,
                 publisher: Callable[[dict], dict] | None = None,
                 source_generation: Callable[[], str | None] = lambda: None,
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
            self.db.execute("INSERT OR IGNORE INTO settings (id,enabled,interval_seconds,next_due) VALUES (1,0,86400,NULL)")
            upgrade_settings(self.db)
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
            # Old receipts stay NULL: historic failure causes cannot be
            # reconstructed after the fact.
            if "failure_stage" not in columns:
                self.db.execute("ALTER TABLE runs ADD COLUMN failure_stage TEXT")
            if "failure_code" not in columns:
                self.db.execute("ALTER TABLE runs ADD COLUMN failure_code TEXT")
            # Existing run rows have no trustworthy source identity. They are
            # historical until a newly admitted run has a pinned generation.
            if "source_generation" not in columns:
                self.db.execute("ALTER TABLE runs ADD COLUMN source_generation TEXT")
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
        self.source_generation = source_generation
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

    def _generation(self) -> str | None:
        try:
            generation = self.source_generation()
            return (generation if type(generation) is str
                    and _SHA.fullmatch(generation) else None)
        except Exception:
            return None

    def settings(self) -> dict:
        with self._lock:
            enabled, interval, due, tz, anchor, revision = self.db.execute(
                "SELECT enabled,interval_seconds,next_due,schedule_tz,"
                "schedule_anchor,schedule_revision FROM settings WHERE id=1"
            ).fetchone()
            return {"enabled": bool(enabled), "interval_seconds": interval,
                    "next_due": due if enabled else None,
                    "configured": self.configured(),
                    "schedule": schedule_status(tz,anchor,revision)}

    def update(self, payload: dict) -> dict:
        with self._lock:
            old = self.settings()
            try:
                enabled,interval,tz,anchor = parse_update(
                    payload, prefix=frozenset({"enabled","interval_seconds"}),
                    minimum=3600, old=old)
            except StaleScheduleError:
                raise
            except ValueError:
                raise Invalid("invalid_daily_settings") from None
            if enabled and not self.configured():
                raise Invalid("daily_connections_missing")
            if (enabled == old["enabled"]
                and interval == old["interval_seconds"]
                and tz == old["schedule"]["timezone"]
                and anchor == old["schedule"]["anchor_local_time"]):
                return old
            due = due_for_update(self.clock(), old, enabled, interval, tz, anchor)
            with self.db:
                self.db.execute(
                    "UPDATE settings SET enabled=?,interval_seconds=?,next_due=?,"
                    "schedule_tz=?,schedule_anchor=?,schedule_revision=? WHERE id=1",
                    (int(enabled), interval, due, tz, anchor,
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
                    "UPDATE settings SET enabled=0,next_due=NULL,schedule_revision=schedule_revision+1 WHERE id=1"
                )
        return self.settings()

    def run(self, run_id: str) -> dict | None:
        if type(run_id) is not str or _ID.fullmatch(run_id) is None:
            return None
        with self._lock:
            row = self.db.execute(
                "SELECT run_id,request_id,trigger,status,created,completed,date_jst,"
                "capture_sha256,digest_sha256,original_event_count,rendered_entity_count,"
                "publication_outcome,failure_stage,failure_code,source_generation FROM runs WHERE run_id=?",
                (run_id,)).fetchone()
            if row is None:
                return None
            result = dict(zip((
                "run_id","request_id","trigger","status","created","completed",
                "date_jst","capture_sha256","digest_sha256",
                "original_event_count","rendered_entity_count",
                "publication_outcome","failure_stage","failure_code",
            ), row[:14]))
            # Only a Boolean is HTTP-visible: never expose private
            # credential-derived source generation digests to the browser.
            result["source_matches_current"] = (
                row[14] is not None
                and row[14] == self._generation()
                and self.configured()
            )
            # Even tampered SQLite text must never become an arbitrary
            # externally observable diagnostic.
            pair = (result["failure_stage"], result["failure_code"])
            if (pair not in _FAILURE_PAIRS or result["status"]
                    not in ("failed", "publish_failed")):
                result["failure_stage"] = result["failure_code"] = None
            return result

    def status(self) -> dict:
        with self._lock:
            row = self.db.execute("SELECT run_id FROM runs ORDER BY created DESC,rowid DESC LIMIT 1").fetchone()
            return {"version":1, "job":"github_daily_preview",
                    "publication_enabled": self.publisher is not None,
                    "settings":self.settings(),
                    "last_run":self.run(row[0]) if row else None}

    def health(self) -> dict:
        """Consecutive-failure streak for the panel banner (counts and times only)."""
        return job_health(self.db, self._lock, service="daily", success=("published", "preview_ready"),
                          failure=("failed", "publish_failed", "unconfirmed"), threshold=2)

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
            generation = self._generation()
            with self.db:
                self.db.execute("INSERT INTO runs (run_id,request_id,trigger,status,created,source_generation) "
                                "VALUES (?,?,?,?,?,?)", (run_id,request_id,trigger,
                                                        "queued",self.clock(),generation))
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
        data, publication = {}, None
        result, failure_stage, failure_code = "failed", None, None
        try:
            raw = self.runner()
        except DailyStageFailure as exc:
            # A malicious/faulty caller may mutate this exception or override
            # its attribute accessors. Read each attribute at most once, and
            # persist ONLY those local immutable, allowlisted string values.
            try:
                stage, code = exc.stage, exc.code
            except Exception:
                stage, code = None, None
            if (type(stage) is str and type(code) is str
                    and (stage, code) in _FAILURE_PAIRS):
                failure_stage, failure_code = stage, code
            else:
                failure_stage, failure_code = "runner", "unexpected_exception"
        except Exception:
            failure_stage, failure_code = "runner", "unexpected_exception"
        else:
            try:
                data = self._validate_result(raw)
            except Exception:
                data = {}
                failure_stage, failure_code = (
                    "preview_validation", "invalid_preview_receipt",
                )
            else:
                result = "preview_ready"
                if self.publisher is not None:
                    try:
                        output = self.publisher(data)
                    except DailyProgressTargetMissingBeforeWrite:
                        # Proven no-effect: the Daily Note was absent at the read
                        # that precedes the PUT, so no write was attempted.
                        result = "publish_failed"
                        failure_stage, failure_code = (
                            "publication", "target_missing",
                        )
                    except DailyStageFailure as exc:
                        # Only the trusted pre-PUT refusal is "no effect";
                        # any other marker stays unconfirmed.
                        pair = (getattr(exc, "stage", None), getattr(exc, "code", None))
                        refused = pair == ("publication", "vault_root_refused")
                        result = "publish_failed"
                        failure_stage, failure_code = (
                            "publication",
                            "vault_root_refused" if refused else "outcome_unconfirmed",
                        )
                    except Exception:
                        # A remote write might have happened before the
                        # exception. Do not interpret this as "no effect".
                        result = "publish_failed"
                        failure_stage, failure_code = (
                            "publication", "outcome_unconfirmed",
                        )
                    else:
                        try:
                            publication = self._validate_publication(output, data)
                        except Exception:
                            result = "publish_failed"
                            failure_stage, failure_code = (
                                "publication", "invalid_publication_receipt",
                            )
                        else:
                            result = "published"
        with self._lock:
            with self.db:
                self.db.execute(
                    "UPDATE runs SET status=?,completed=?,date_jst=?,capture_sha256=?,"
                    "digest_sha256=?,original_event_count=?,rendered_entity_count=?,"
                    "publication_outcome=?,failure_stage=?,failure_code=? WHERE run_id=?",
                    (result,self.clock(),data.get("date_jst"),data.get("capture_sha256"),
                     data.get("digest_sha256"),data.get("original_event_count"),
                     data.get("rendered_entity_count"),publication,
                     failure_stage,failure_code,run_id))

    def tick(self) -> bool:
        """One durable scheduler admission; skip missed JST phase slots."""
        with self._lock:
            s = self.settings()
            now = self.clock()
            due = s["next_due"]
            if (not s["enabled"] or not s["configured"] or due is None
                or now < due):
                return False
            busy = self.db.execute(
                "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone() is not None
            if busy or missed(now,due,s["schedule"]):
                if s["schedule"]["mode"] == "anchored":
                    with self.db:
                        self.db.execute("UPDATE settings SET next_due=? WHERE id=1",
                                        (due_after_tick(now,s["interval_seconds"],
                                                        s["schedule"]),))
                return False
            # Job admission and next schedule commit atomically.
            run_id, request_id = uuid.uuid4().hex, uuid.uuid4().hex
            generation = self._generation()
            with self.db:
                self.db.execute(
                    "INSERT INTO runs (run_id,request_id,trigger,status,created,source_generation) "
                    "VALUES (?,?,'scheduled','queued',?,?)",
                    (run_id,request_id,now,generation))
                self.db.execute("UPDATE settings SET next_due=? WHERE id=1",
                                (due_after_tick(now,s["interval_seconds"],
                                                s["schedule"]),))
            self._launch_worker(run_id)
            return True
