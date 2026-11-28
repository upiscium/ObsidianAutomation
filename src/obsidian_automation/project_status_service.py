"""B3-lite step 3: panel backend and opt-in schedule for automatic Project status.

Built only with ``--project-status-ui``. One run reads the Project notes and GitHub, and
(for an applying run) changes the ``status`` line of the notes whose rule says so; see
``project_status_run`` for the run and ``docs/b3-status-lite-design.md`` for the brakes.

* the schedule is off until switched on in the panel; never more often than
  ``interval_seconds`` (>= 1 hour); one run at a time;
* a scheduled run always applies; the panel can also start a "check only" run that
  writes nothing to the Vault, or an applying run now;
* every response is count-only: no note paths, repository names or text.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import github_project_watcher as watcher
from .job_health import job_health
from .project_status import MAX_CHANGES_PER_RUN
from .project_status_run import ChangeJournal, run_once
from .project_status_vault import ProjectVaultError

MIN_INTERVAL_SECONDS = 3600
RECENT_RUNS = 8
ACTIVE = ("queued", "running")
_JST = timezone(timedelta(hours=9))


class ProjectStatusRefused(ValueError):
    def __init__(self, code: str, http: int = 409):
        super().__init__(code)
        self.code, self.http = code, http


@dataclass(frozen=True)
class ProjectStatusConfig:
    interval_seconds: int = 6 * 3600
    max_changes: int = MAX_CHANGES_PER_RUN     # per run
    max_per_day: int = 30                      # per Japan-time day
    cooldown_hours: int = 24                   # between two changes of the same note
    active_window_days: int | None = None      # None: the Daily watcher setting (7)

    def __post_init__(self) -> None:
        if type(self.interval_seconds) is not int or self.interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError("interval_seconds must be an integer >= 3600")
        if type(self.max_changes) is not int or not 1 <= self.max_changes <= 100:
            raise ValueError("max_changes must be 1..100")
        if type(self.max_per_day) is not int or not 1 <= self.max_per_day <= 500:
            raise ValueError("max_per_day must be 1..500")
        if type(self.cooldown_hours) is not int or not 1 <= self.cooldown_hours <= 720:
            raise ValueError("cooldown_hours must be 1..720")
        if self.active_window_days is not None and (
                type(self.active_window_days) is not int or not 1 <= self.active_window_days <= 90):
            raise ValueError("active_window_days must be 1..90")


def _jst_midnight(now: float) -> float:
    local = datetime.fromtimestamp(now, _JST)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def default_client_factory(state: Path):
    """The GitHub client Daily already uses (read-only token in ``github-daily/``)."""
    from .github_daily_runonce import DailyPreviewRunError, load_run_config
    try:
        config, token = load_run_config(state / "github-daily")
    except DailyPreviewRunError:
        raise ProjectVaultError("github_credential_unavailable") from None
    return watcher.GitHubClient(token=token, active_window_days=config.active_window_days,
                                timeout_seconds=config.request_timeout_seconds), config.active_window_days


class ProjectStatusService:
    def __init__(self, state: Path, *, shared, guard: Callable[[], object] | None,
                 config: ProjectStatusConfig, client_factory=None, transport=None,
                 clock: Callable[[], float] = time.time):
        self.state, self.shared, self.guard, self.config = state, shared, guard, config
        self.client_factory = client_factory or default_client_factory
        self.transport, self.clock = transport, clock
        self.root = state / "project-status"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "service.sqlite3", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1),"
                        " enabled INTEGER NOT NULL, next_due REAL)")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES (1,0,NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, trigger TEXT NOT NULL,"
                        " mode TEXT NOT NULL, status TEXT NOT NULL, error_code TEXT, report TEXT,"
                        " created REAL NOT NULL, completed REAL)")
        # Limits edited in the panel; a NULL column keeps the command-line value.
        self.db.execute("CREATE TABLE IF NOT EXISTS limits (id INTEGER PRIMARY KEY CHECK(id=1),"
                        " interval_seconds INTEGER, max_changes INTEGER, max_per_day INTEGER,"
                        " cooldown_hours INTEGER, active_window_days INTEGER, anchor_minute INTEGER)")
        self.db.execute("INSERT OR IGNORE INTO limits VALUES (1,NULL,NULL,NULL,NULL,NULL,NULL)")
        row = self.db.execute("SELECT interval_seconds,max_changes,max_per_day,cooldown_hours,"
                              "active_window_days,anchor_minute FROM limits WHERE id=1").fetchone()
        self.anchor_minute: int | None = row[5]
        try:
            self.config = replace(self.config, **{k: v for k, v in zip(
                ("interval_seconds", "max_changes", "max_per_day", "cooldown_hours",
                 "active_window_days"), row[:5]) if v is not None})
        except ValueError:
            pass  # a stored value that no longer validates falls back to the command line
        # A run left queued/running by a crash is indeterminate: never replayed.
        self.db.execute("UPDATE runs SET status='failed',error_code='interrupted',completed=?"
                        " WHERE status IN ('queued','running')", (self.clock(),))
        self.db.commit()
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._last_skip: str | None = None

    # ------------------------------------------------------------------ status

    def settings(self) -> dict:
        row = self.db.execute("SELECT enabled,next_due FROM settings WHERE id=1").fetchone()
        return {"enabled": bool(row[0]), "next_due": row[1]}

    def _busy(self) -> bool:
        return self.db.execute(
            "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone() is not None

    def _recent(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT run_id,trigger,mode,status,error_code,report,created,completed FROM runs"
            " ORDER BY created DESC,rowid DESC LIMIT ?", (RECENT_RUNS,)).fetchall()
        out = []
        for run_id, trigger, mode, status, error, report, created, completed in rows:
            try:
                counts = json.loads(report) if report else None
            except ValueError:
                counts = None
            out.append({"run_id": run_id, "trigger": trigger, "mode": mode, "status": status,
                        "error_code": error, "counts": counts, "created": created,
                        "completed": completed})
        return out

    def status(self) -> dict:
        with self._lock:
            settings = self.settings()
            journal = ChangeJournal(self.root)
            try:
                changed_today = journal.changes_today(_jst_midnight(self.clock()))
            finally:
                journal.close()
            return {"version": 1, "enabled": True,
                    "schedule": {"enabled": settings["enabled"], "next_due": settings["next_due"],
                                 "interval_seconds": self.config.interval_seconds,
                                 "max_changes": self.config.max_changes,
                                 "max_per_day": self.config.max_per_day,
                                 "cooldown_hours": self.config.cooldown_hours,
                                 "active_window_days": self.config.active_window_days,
                                 "anchor_minute": self.anchor_minute,
                                 "changed_today": changed_today, "last_skip": self._last_skip},
                    "busy": self._busy(), "recent_runs": self._recent(), "vault_write": True}

    def health(self) -> dict:
        return job_health(self.db, self._lock, service="project_status", success=("ok",),
                          failure=("failed",), neutral=("skipped",), threshold=3)

    def _next_slot(self, now: float) -> float:
        """The next scheduled time after ``now``: one interval away, or, with a base
        time (JST minute of day), the next point of base + k * interval."""
        interval = self.config.interval_seconds
        if self.anchor_minute is None:
            return now + interval
        base = _jst_midnight(now) + self.anchor_minute * 60
        return base + (math.floor((now - base) / interval) + 1) * interval

    def update_settings(self, payload: object) -> dict:
        """Any subset of enabled / interval_seconds / max_changes / max_per_day /
        cooldown_hours / active_window_days / anchor_minute (0..1439, JST, or null); nothing else."""
        keys = ("interval_seconds", "max_changes", "max_per_day", "cooldown_hours", "active_window_days")
        allowed = {"enabled", "anchor_minute", *keys}
        if (type(payload) is not dict or not payload or not set(payload) <= allowed
                or ("enabled" in payload and type(payload["enabled"]) is not bool)):
            raise ProjectStatusRefused("invalid_request", 400)
        anchor = payload.get("anchor_minute", self.anchor_minute)
        if anchor is not None and (type(anchor) is not int or not 0 <= anchor < 1440):
            raise ProjectStatusRefused("invalid_request", 400)
        try:
            config = replace(self.config, **{k: payload[k] for k in keys if k in payload})
        except ValueError:
            raise ProjectStatusRefused("invalid_request", 400)
        with self._lock:
            current = self.settings()
            enabled = payload.get("enabled", current["enabled"])
            self.config, self.anchor_minute = config, anchor
            due = current["next_due"]
            if not enabled:
                due = None
            elif (not current["enabled"] or due is None
                    or {"interval_seconds", "anchor_minute"} & set(payload)):
                due = self._next_slot(self.clock())
            with self.db:
                self.db.execute(
                    "UPDATE limits SET interval_seconds=?,max_changes=?,max_per_day=?,cooldown_hours=?,"
                    "active_window_days=?,anchor_minute=? WHERE id=1",
                    (config.interval_seconds, config.max_changes, config.max_per_day,
                     config.cooldown_hours, config.active_window_days, anchor))
                self.db.execute("UPDATE settings SET enabled=?,next_due=? WHERE id=1", (int(enabled), due))
            self._last_skip = None
            return self.settings()

    # -------------------------------------------------------------------- runs

    def _start(self, trigger: str, apply: bool) -> str:
        run_id = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO runs (run_id,trigger,mode,status,created) VALUES (?,?,?,?,?)",
                            (run_id, trigger, "apply" if apply else "dry_run", "queued", self.clock()))
        self._worker = threading.Thread(target=self._execute, args=(run_id, apply), daemon=False)
        self._worker.start()
        return run_id

    def start_manual(self, payload: object) -> dict:
        if type(payload) is not dict or set(payload) != {"apply"} or type(payload["apply"]) is not bool:
            raise ProjectStatusRefused("invalid_request", 400)
        with self._lock:
            if self._busy():
                raise ProjectStatusRefused("project_status_busy")
            return {"outcome": "accepted", "run_id": self._start("manual", payload["apply"])}

    def tick(self) -> bool:
        """Cheap, called by the app scheduler. The heavy work happens in the worker."""
        with self._lock:
            settings = self.settings()
            now = self.clock()
            due = settings["next_due"]
            if not settings["enabled"] or due is None or now < due:
                return False
            with self.db:  # never a catch-up queue: the next slot is one interval away
                self.db.execute("UPDATE settings SET next_due=? WHERE id=1",
                                (self._next_slot(now),))
            if self._busy():
                self._last_skip = "busy"
                return False
            self._start("scheduled", True)
            return True

    def _finish(self, run_id: str, status: str, *, error: str | None = None,
                report: dict | None = None) -> None:
        with self._lock:
            with self.db:
                self.db.execute("UPDATE runs SET status=?,error_code=?,report=?,completed=? WHERE run_id=?",
                                (status, error, json.dumps(report, sort_keys=True) if report else None,
                                 self.clock(), run_id))

    def _execute(self, run_id: str, apply: bool) -> None:
        with self._lock:
            with self.db:
                self.db.execute("UPDATE runs SET status='running' WHERE run_id=?", (run_id,))
        try:
            client, window = self.client_factory(self.state)
            budget = self.config.max_changes
            if apply:
                journal = ChangeJournal(self.root)
                try:
                    remaining = self.config.max_per_day - journal.changes_today(_jst_midnight(self.clock()))
                finally:
                    journal.close()
                if remaining <= 0:
                    self._last_skip = "daily_cap"
                    self._finish(run_id, "skipped", error="daily_cap")
                    return
                budget = min(budget, remaining)
            report = run_once(shared=self.shared, guard=self.guard, client=client,
                              state_dir=self.root, apply=apply, transport=self.transport,
                              active_window_days=self.config.active_window_days or window,
                              max_changes=budget,
                              min_interval_seconds=self.config.cooldown_hours * 3600)
        except ProjectVaultError as exc:
            self._finish(run_id, "failed", error=str(exc))
            return
        except Exception:
            # Never persist an exception text: it can hold paths, URLs or note text.
            self._finish(run_id, "failed", error="unexpected_exception")
            return
        # Notes edited by the owner meanwhile are normal; any other write failure is not.
        failures = {k: v for k, v in report.get("failed", {}).items() if k != "note_changed"}  # type: ignore[union-attr]
        self._finish(run_id, "failed" if failures else "ok",
                     error=sorted(failures)[0] if failures else None, report=report)

    def close(self) -> None:
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)
        self.db.close()
