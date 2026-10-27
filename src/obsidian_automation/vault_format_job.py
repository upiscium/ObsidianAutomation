"""Persistent, default-off Vault Formatter job; uses the shared app scheduler.

One owner, one worker, durable run records. No direct WebDAV credential
lookup and no background execution until the containing app explicitly ticks.
"""
from __future__ import annotations

import fcntl
import os
import stat
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .vault_formatter import POLICY_VERSION, SCOPE
from .vault_format_writer import _validated_plan, FormatWriteError

_ID = re.compile(r"[a-f0-9]{32}\Z")
_TERMINAL = {"would_change", "no_changes", "applied", "conflict", "failed",
             "indeterminate", "unconfirmed"}


class FormatterInvalid(ValueError):
    pass


class FormatterBusy(RuntimeError):
    pass


class FormatterJob:
    """Independent state, controlled by snapshot_app's single tick thread.

    - Installed mode is off; manual Run Once is strictly dry-run.
    - Auto apply requires a separately injected, verified writer, never a
      default app flag, and is unreachable in ordinary deployment.
    - Runs orphaned by a crash are unconfirmed, never re-submitted.
    """

    def __init__(
        self, state: Path, runner: Callable[[], dict],
        *, configured: Callable[[], bool] = lambda: False,
        source_generation: Callable[[], str | None] = lambda: None,
        writer: Callable[[dict], dict] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        if state.is_symlink():
            raise FormatterInvalid("unsafe_formatter_state")
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if state.is_symlink() or not state.is_dir():
            raise FormatterInvalid("unsafe_formatter_state")
        info = state.lstat()
        if (not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise FormatterInvalid("unsafe_formatter_state_permissions")
        self.state = state
        self._lock_file = (state / "formatter.lock").open("a+b")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise FormatterBusy("formatter_owner_busy") from exc

        self.runner = runner
        self.is_configured = configured
        self.source_generation = source_generation
        self.writer = writer
        self.clock = clock
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        try:
            self.db = sqlite3.connect(state / "formatter.sqlite3", check_same_thread=False)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK (id=1), "
                "mode TEXT NOT NULL, interval_seconds INTEGER NOT NULL, next_due REAL, "
                "source_generation TEXT)"
            )
            self.db.execute(
                "INSERT OR IGNORE INTO settings VALUES (1,'off',3600,NULL,NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS runs ("
                "run_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, "
                "trigger TEXT NOT NULL, run_mode TEXT NOT NULL, "
                "status TEXT NOT NULL, created REAL NOT NULL, completed REAL, "
                "policy_version TEXT, scope TEXT, plan_sha256 TEXT, "
                "would_change INTEGER, skipped INTEGER, errors INTEGER, "
                "writer_outcome TEXT, reason_code TEXT)"
            )
            existing_columns = {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}
            if "reason_code" not in existing_columns:
                self.db.execute("ALTER TABLE runs ADD COLUMN reason_code TEXT")
            self.db.execute(
                "UPDATE runs SET status='unconfirmed',completed=? "
                "WHERE status IN ('queued','running')", (self.clock(),)
            )
            self.db.commit()
            with self._lock:
                self._refresh_generation()
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._worker:
            self._worker.join(timeout=15)
            if self._worker.is_alive():
                raise FormatterBusy("formatter_worker_still_running")
        self.db.close()
        fcntl.flock(self._lock_file, fcntl.LOCK_UN)
        self._lock_file.close()

    def configured(self) -> bool:
        try:
            return self.is_configured() is True and self.source_generation() is not None
        except Exception:
            return False

    def _generation(self) -> str | None:
        try:
            value = self.source_generation()
        except Exception:
            return None
        if value is None:
            return None
        if type(value) is not str or not re.fullmatch(r"[a-f0-9]{64}", value):
            return None
        return value

    def _refresh_generation(self) -> None:
        # Under self._lock: new/rotated Nextcloud connection invalidates
        # any prior scheduled mode and does not restart an uncertain run.
        current = self._generation()
        saved = self.db.execute(
            "SELECT source_generation FROM settings WHERE id=1"
        ).fetchone()[0]
        if current != saved:
            with self.db:
                self.db.execute(
                    "UPDATE settings SET mode='off',next_due=NULL,"
                    "source_generation=? WHERE id=1", (current,)
                )

    def connections_saved(self) -> None:
        """Reset independent formatter scheduling before changing credentials."""
        with self._lock:
            with self.db:
                self.db.execute("UPDATE settings SET mode='off',next_due=NULL WHERE id=1")

    def settings(self) -> dict:
        with self._lock:
            self._refresh_generation()
            mode, interval, due = self.db.execute(
                "SELECT mode,interval_seconds,next_due FROM settings WHERE id=1"
            ).fetchone()
            return {
                "mode": mode,
                "interval_seconds": interval,
                "next_due": due if mode != "off" else None,
                "configured": self.configured(),
                "write_capable": self.writer is not None,
                "scope": SCOPE,
                "policy_version": POLICY_VERSION,
            }

    def update(self, payload: dict) -> dict:
        if (type(payload) is not dict
                or set(payload) != {"mode", "interval_seconds"}
                or payload.get("mode") not in {"off", "dry-run", "auto-apply"}
                or type(payload.get("interval_seconds")) is not int
                or not 300 <= payload["interval_seconds"] <= 86400):
            raise FormatterInvalid("invalid_formatter_settings")
        mode, interval = payload["mode"], payload["interval_seconds"]
        with self._lock:
            previous = self.settings()
            if mode != "off" and not previous["configured"]:
                raise FormatterInvalid("formatter_connection_missing")
            if mode == "auto-apply" and self.writer is None:
                raise FormatterInvalid("formatter_writer_not_authorized")
            due = (self.clock() + interval if mode != "off" and
                   (previous["mode"] != mode or previous["interval_seconds"] != interval)
                   else previous["next_due"] if mode != "off" else None)
            with self.db:
                self.db.execute(
                    "UPDATE settings SET mode=?, interval_seconds=?, next_due=? WHERE id=1",
                    (mode, interval, due),
                )
            return self.settings()

    def run(self, run_id: str) -> dict | None:
        if type(run_id) is not str or _ID.fullmatch(run_id) is None:
            return None
        with self._lock:
            row = self.db.execute(
                "SELECT run_id,request_id,trigger,run_mode,status,created,completed,"
                "policy_version,scope,plan_sha256,would_change,skipped,errors,writer_outcome,reason_code "
                "FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            return dict(zip(
                ("run_id", "request_id", "trigger", "run_mode", "status",
                 "created", "completed", "policy_version", "scope", "plan_sha256",
                 "would_change", "skipped", "errors", "writer_outcome", "reason_code"),
                row,
            )) if row else None

    def status(self) -> dict:
        with self._lock:
            row = self.db.execute(
                "SELECT run_id FROM runs ORDER BY created DESC,rowid DESC LIMIT 1"
            ).fetchone()
            return {
                "version": 1, "job": "vault_formatter",
                "settings": self.settings(),
                "last_run": self.run(row[0]) if row else None,
            }

    def submit(self, request_id: str, trigger: str = "manual") -> tuple[str, dict | None]:
        if (type(request_id) is not str or _ID.fullmatch(request_id) is None
                or trigger not in ("manual", "scheduled")):
            raise FormatterInvalid("invalid_formatter_request")
        with self._lock:
            self._refresh_generation()
            old = self.db.execute(
                "SELECT run_id,trigger FROM runs WHERE request_id=?", (request_id,)
            ).fetchone()
            if old:
                if old[1] != trigger:
                    raise FormatterInvalid("formatter_request_conflict")
                return "replayed", self.run(old[0])
            if not self.configured():
                return "unconfigured", None
            if self.db.execute(
                "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone():
                return "busy", None
            mode = "dry-run" if trigger == "manual" else self.settings()["mode"]
            if mode == "off":
                return "disabled", None
            if mode == "auto-apply" and self.writer is None:
                return "unavailable", None
            run_id = uuid.uuid4().hex
            with self.db:
                self.db.execute(
                    "INSERT INTO runs (run_id,request_id,trigger,run_mode,status,created) "
                    "VALUES (?,?,?,?,?,?)",
                    (run_id, request_id, trigger, mode, "queued", self.clock()),
                )
            self._launch_worker(run_id)
            return "accepted", self.run(run_id)

    def _launch_worker(self, run_id: str) -> None:
        self._worker = threading.Thread(
            target=self._execute, args=(run_id,), daemon=False
        )
        try:
            self._worker.start()
        except Exception:
            self._worker = None
            with self.db:
                self.db.execute(
                    "UPDATE runs SET status='unconfirmed',completed=? "
                    "WHERE run_id=? AND status='queued'", (self.clock(), run_id)
                )
            raise

    def _execute(self, run_id: str) -> None:
        with self._lock:
            current = self.run(run_id)
            if current is None:
                return
            mode = current["run_mode"]
            with self.db:
                self.db.execute("UPDATE runs SET status='running' WHERE run_id=?", (run_id,))
        fields: dict = {}
        status = "failed"
        writer_outcome = None
        reason_code = None
        try:
            plan = self.runner()
            digest, entries = _validated_plan(plan)
            counts = plan["counts"]
            if counts.get("error", 0):
                raise FormatterInvalid("formatter_scan_error")
            if any(row["status"] == "error" for row in entries.values()):
                raise FormatterInvalid("formatter_scan_error")
            changed = sum(row["status"] == "would_change" for row in entries.values())
            skipped = sum(row["status"] == "skipped" for row in entries.values())
            if changed != counts.get("would_change", 0) or skipped != counts.get("skipped", 0):
                raise FormatterInvalid("formatter_count_mismatch")
            fields = {
                "policy_version": POLICY_VERSION, "scope": SCOPE,
                "plan_sha256": digest, "would_change": changed,
                "skipped": skipped, "errors": 0,
            }
            status = "would_change" if changed else "no_changes"
            if mode == "auto-apply" and changed:
                if self.writer is None or not self.configured():
                    raise FormatterInvalid("formatter_writer_unavailable")
                # Every actual effect remains owned by the independently
                # authorized writer. This job never assembles PUT bytes.
                try:
                    result = self.writer(plan)
                except Exception:
                    # Unknown whether WebDAV side effects happened.
                    status = "indeterminate"
                    reason_code = "writer_result_unknown"
                else:
                    if (type(result) is not dict or result.get("plan_sha256") != digest
                            or result.get("status") not in
                            {"complete", "blocked", "partial", "indeterminate"}
                            or type(result.get("outcomes")) is not list
                            or not result["outcomes"]
                            or not all(type(row) is dict and row.get("result") in
                                       {"applied", "recovered", "already_desired",
                                        "cas_conflict", "write_rejected"} for row in result["outcomes"])
                            or (result["status"] == "complete" and
                                any(row["result"] not in
                                    {"applied", "recovered", "already_desired"}
                                    for row in result["outcomes"]))):
                        status = "indeterminate"
                        reason_code = "writer_result_invalid"
                    elif result["status"] == "indeterminate":
                        status = "indeterminate"
                        reason_code = "remote_effect_unknown"
                    elif result["status"] in {"blocked", "partial"}:
                        status = "conflict"
                        reason_code = "writer_conflict" if result["status"] == "blocked" else "writer_partial_conflict"
                        writer_outcome = result["status"]
                    else:
                        status = "applied" if any(
                            row.get("result") in {"applied", "recovered"}
                            for row in result["outcomes"] if type(row) is dict
                        ) else "no_changes"
                        writer_outcome = result["status"]
        except Exception:
            if status != "indeterminate":
                status = "failed"
                reason_code = "source_or_plan_failed"
        with self._lock:
            with self.db:
                self.db.execute(
                    "UPDATE runs SET status=?,completed=?,policy_version=?,scope=?,"
                    "plan_sha256=?,would_change=?,skipped=?,errors=?,writer_outcome=?,reason_code=? "
                    "WHERE run_id=?",
                    (status, self.clock(), fields.get("policy_version"),
                     fields.get("scope"), fields.get("plan_sha256"),
                     fields.get("would_change"), fields.get("skipped"),
                     fields.get("errors"), writer_outcome, reason_code, run_id),
                )

    def tick(self) -> bool:
        with self._lock:
            s = self.settings()
            if not (s["mode"] != "off" and s["configured"]
                    and s["next_due"] is not None and self.clock() >= s["next_due"]):
                return False
            if self.db.execute(
                "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone():
                return False
            run_id, request_id = uuid.uuid4().hex, uuid.uuid4().hex
            with self.db:
                self.db.execute(
                    "INSERT INTO runs (run_id,request_id,trigger,run_mode,status,created) "
                    "VALUES (?,?,'scheduled',?,'queued',?)",
                    (run_id, request_id, s["mode"], self.clock()),
                )
                self.db.execute(
                    "UPDATE settings SET next_due=? WHERE id=1",
                    (self.clock() + s["interval_seconds"],),
                )
            self._launch_worker(run_id)
            return True
