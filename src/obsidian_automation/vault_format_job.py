"""Persistent, default-off Vault Formatter job; uses the shared app scheduler.

One owner, one worker, durable run records. No direct WebDAV credential
lookup and no background execution until the containing app explicitly ticks.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Callable

from .vault_formatter import POLICY_VERSION, SCOPE
from .vault_format_writer import _validated_plan, FormatWriteError

_ID = re.compile(r"[a-f0-9]{32}\Z")
_TERMINAL = {"would_change", "no_changes", "applied", "conflict", "failed",
             "indeterminate", "unconfirmed"}
# Writer's fixed, non-sensitive result vocabulary; keep real preflight
# refusals distinct from unobservable PUT outcomes.
_WRITER_SUCCESS = frozenset({"applied", "recovered", "already_desired"})
_WRITER_PRECHECK = frozenset({
    "ready", "remote_unavailable", "remote_missing_or_rejected",
    "source_changed", "formatter_policy_mismatch", "weak_or_missing_etag",
})
_WRITER_TERMINAL = frozenset({
    "cas_conflict", "write_rejected", "indeterminate",
})
_WRITER_RESULTS = _WRITER_SUCCESS | _WRITER_PRECHECK | _WRITER_TERMINAL
_WRITER_CONFLICT = frozenset({"source_changed", "cas_conflict"})
_WRITER_FAILURE = frozenset({
    "remote_unavailable", "remote_missing_or_rejected",
    "weak_or_missing_etag", "formatter_policy_mismatch", "write_rejected",
})

# Static non-sensitive policy reasons. Do not publish an arbitrary reason or
# any filename, user note, or private metadata from the source plan.
_SAFE_SKIP_REASONS = frozenset({
    "binary-content", "file-too-large", "hidden-directory", "hidden-path",
    "invalid-utf8", "invalid-utf8-filename", "mixed-line-endings",
    "path-name-collision", "symlink", "unsafe-path", "unsupported-extension",
    "unsupported-file-type", "unsupported-fence-syntax",
    "unsupported-html-block", "unsupported-line-endings",
    "unsupported-line-separators", "unsupported-nested-fence",
    "unterminated-frontmatter", "unterminated-protected-block",
})


def _skip_reason_counts(entries: dict[str, dict]) -> dict[str, int]:
    """Count fixed policy reasons; fold anything unknown into an opaque bucket."""
    counts: Counter[str] = Counter()
    for row in entries.values():
        if row["status"] == "skipped":
            raw = row.get("reason")
            code = raw if type(raw) is str and raw in _SAFE_SKIP_REASONS else "unclassified"
            counts[code] += 1
    return dict(sorted(counts.items()))


def _writer_status(
    result: object, plan_sha: str, entries: dict[str, dict]
) -> tuple[str, str | None, str | None]:
    """Validate an exact CAS Writer receipt before reporting any success.

    These checks are *shape and plan consistency*, not independent proof that
    a real remote effect occurred; only the separately authorized Writer
    produces and durably verifies its own content-addressed receipt.
    """
    invalid = ("indeterminate", "writer_result_invalid", None)
    if type(result) is not dict or result.get("plan_sha256") != plan_sha:
        return invalid
    state, outcomes = result.get("status"), result.get("outcomes")
    operation, receipt = result.get("operation_id"), result.get("receipt_sha256")
    if state not in ("complete", "blocked", "partial", "indeterminate"):
        return invalid
    if (type(operation) is not str or _ID.fullmatch(operation) is None
            or type(receipt) is not str
            or re.fullmatch(r"[a-f0-9]{64}", receipt) is None
            or type(outcomes) is not list or not 1 <= len(outcomes) <= 32):
        return invalid
    codes: list[str] = []
    paths: set[str] = set()
    for row in outcomes:
        if type(row) is not dict:
            return invalid
        path, code = row.get("path"), row.get("result")
        entry = entries.get(path) if type(path) is str else None
        if (entry is None or entry.get("status") != "would_change"
                or path in paths
                or type(code) is not str or code not in _WRITER_RESULTS
                or row.get("before_sha256") != entry.get("before_sha256")
                or row.get("after_sha256") != entry.get("after_sha256")):
            return invalid
        paths.add(path)
        codes.append(code)
    effect = result.get("vault_modified")
    if state == "complete":
        # R1 has no per-run selected-path contract: complete means the entire
        # Plan's would_change set, not an arbitrary subset. R2 must define
        # explicit selected-scope admission before allowing partial scope.
        planned = {path for path, entry in entries.items()
                   if entry.get("status") == "would_change"}
        if paths != planned or any(code not in _WRITER_SUCCESS for code in codes):
            return invalid
        changed = any(code in {"applied", "recovered"} for code in codes)
        if effect is not changed:
            return invalid
        return ("applied" if changed else "no_changes", None, state)
    if state == "blocked":
        if effect is not False or any(
            code in {"applied", "recovered", "indeterminate"} for code in codes
        ):
            return invalid
        if any(code in _WRITER_FAILURE for code in codes):
            return ("failed", "writer_preflight_or_remote_failed", state)
        if any(code in _WRITER_CONFLICT for code in codes):
            return ("conflict", "writer_conflict", state)
        return invalid
    if state == "partial":
        terminal = [code for code in codes if code in {"cas_conflict", "write_rejected"}]
        if (effect is not True
                or not any(code in {"applied", "recovered"} for code in codes)
                or len(terminal) != 1 or codes[-1] != terminal[0]
                or any(code in _WRITER_PRECHECK or code == "indeterminate"
                       for code in codes)):
            return invalid
        if terminal[0] == "write_rejected":
            # A 401/403/404 after an earlier successful PUT is an incident,
            # not a CAS conflict; preserve writer_outcome='partial'.
            return ("failed", "writer_partial_write_rejected", state)
        return ("conflict", "writer_partial_conflict", state)
    if effect is not None or "indeterminate" not in codes:
        return invalid
    return ("indeterminate", "remote_effect_unknown", state)


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
        # A separate gate serializes mode acknowledgements and the Writer
        # effect window. Keep the normal Job lock free during remote I/O so
        # the shared scheduler and unrelated jobs continue to tick.
        self._write_gate = threading.RLock()
        self._worker: threading.Thread | None = None
        # A queued auto-apply run is bound to the exact settings/source epoch
        # from admission. Turning Off (or even Off then On during a scan) must
        # not silently re-authorize that already queued operation.
        self._settings_epoch = 0
        self._write_admissions: dict[str, tuple[int, str | None]] = {}
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
                "writer_outcome TEXT, reason_code TEXT, skip_reasons_json TEXT)"
            )
            existing_columns = {row[1] for row in self.db.execute("PRAGMA table_info(runs)")}
            if "reason_code" not in existing_columns:
                self.db.execute("ALTER TABLE runs ADD COLUMN reason_code TEXT")
            if "skip_reasons_json" not in existing_columns:
                # NULL for legacy/pending/failed runs: never imply zero skipped.
                self.db.execute("ALTER TABLE runs ADD COLUMN skip_reasons_json TEXT")
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
            self._settings_epoch += 1

    def connections_saved(self) -> None:
        """Revoke scheduled writes after a verified shared-connection change.

        Lock order must match update() and the Dashboard credential handlers:
        Writer gate -> other workload locks (when present) -> Formatter lock.
        """
        with self._write_gate, self._lock:
            with self.db:
                self.db.execute("UPDATE settings SET mode='off',next_due=NULL WHERE id=1")
            self._settings_epoch += 1

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
        # Strict order: write gate -> Formatter lock -> shared SetupStore.
        # Do not call update() while already holding Formatter's lock.
        # An Off acknowledgement cannot race a Writer starting. If a
        # conditional PUT has already been admitted, Off waits for its
        # bounded Writer call to return; it cannot revoke an in-flight PUT.
        with self._write_gate, self._lock:
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
            if previous["mode"] != mode or previous["interval_seconds"] != interval:
                self._settings_epoch += 1
            return self.settings()

    def run(self, run_id: str) -> dict | None:
        if type(run_id) is not str or _ID.fullmatch(run_id) is None:
            return None
        with self._lock:
            row = self.db.execute(
                "SELECT run_id,request_id,trigger,run_mode,status,created,completed,"
                "policy_version,scope,plan_sha256,would_change,skipped,errors,writer_outcome,"
                "reason_code,skip_reasons_json FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            values = dict(zip(
                ("run_id", "request_id", "trigger", "run_mode", "status",
                 "created", "completed", "policy_version", "scope", "plan_sha256",
                 "would_change", "skipped", "errors", "writer_outcome",
                 "reason_code", "skip_reasons_json"),
                row,
            ))
            raw = values.pop("skip_reasons_json")
            try:
                reasons = json.loads(raw) if raw is not None else None
            except (TypeError, ValueError):
                reasons = None
            if (type(reasons) is not dict or any(
                key not in _SAFE_SKIP_REASONS | {"unclassified"}
                or type(count) is not int or count <= 0
                for key, count in reasons.items()
            ) or sum(reasons.values()) != values["skipped"]):
                reasons = None
            values["skip_reasons"] = reasons
            return values

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
            if mode == "auto-apply":
                self._write_admissions[run_id] = (self._settings_epoch, self._generation())
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
            self._write_admissions.pop(run_id, None)
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
            admitted = self._write_admissions.pop(run_id, None)
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
            breakdown = _skip_reason_counts(entries)
            if sum(breakdown.values()) != skipped:
                raise FormatterInvalid("formatter_reason_count_mismatch")
            fields = {
                "policy_version": POLICY_VERSION, "scope": SCOPE,
                "plan_sha256": digest, "would_change": changed,
                "skipped": skipped, "errors": 0,
                "skip_reasons_json": json.dumps(
                    breakdown, sort_keys=True, separators=(",", ":")
                ),
            }
            status = "would_change" if changed else "no_changes"
            if mode == "auto-apply" and changed:
                # Serialize only the Writer effect window with mode updates.
                # Unlike self._lock, this gate is never acquired by tick(),
                # read-only status, or independent Snapshot/Daily workers.
                with self._write_gate:
                    with self._lock:
                        self._refresh_generation()
                        settings_mode = self.db.execute(
                            "SELECT mode FROM settings WHERE id=1"
                        ).fetchone()[0]
                        valid_admission = (
                            admitted is not None
                            and admitted == (self._settings_epoch, self._generation())
                            and settings_mode == "auto-apply"
                            and self.writer is not None
                            and self.configured()
                        )
                    if not valid_admission:
                        # Off/On, credential rotation or config change during
                        # a long scan cancels this run before remote effects.
                        status = "failed"
                        reason_code = "writer_admission_revoked"
                    else:
                        # Every actual effect remains owned by the separately
                        # authorized Writer. This Job never constructs PUT bytes.
                        try:
                            result = self.writer(plan)
                        except Exception:
                            # The effect may have been sent: do not replay.
                            status = "indeterminate"
                            reason_code = "writer_result_unknown"
                        else:
                            status, reason_code, writer_outcome = _writer_status(result, digest, entries)
        except Exception:
            if status != "indeterminate":
                status = "failed"
                reason_code = "source_or_plan_failed"
        with self._lock:
            with self.db:
                self.db.execute(
                    "UPDATE runs SET status=?,completed=?,policy_version=?,scope=?,"
                    "plan_sha256=?,would_change=?,skipped=?,errors=?,writer_outcome=?,reason_code=?,"
                    "skip_reasons_json=? WHERE run_id=?",
                    (status, self.clock(), fields.get("policy_version"),
                     fields.get("scope"), fields.get("plan_sha256"),
                     fields.get("would_change"), fields.get("skipped"),
                     fields.get("errors"), writer_outcome, reason_code,
                     fields.get("skip_reasons_json"), run_id),
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
            if s["mode"] == "auto-apply":
                self._write_admissions[run_id] = (self._settings_epoch, self._generation())
            self._launch_worker(run_id)
            return True
