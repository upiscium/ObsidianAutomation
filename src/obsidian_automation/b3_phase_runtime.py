"""B3 GET-only phase scheduler on the existing single SQLite/Job owner.

Not wired to public settings routes. Desired phase is OFF by default. No Writer,
Vault write, Core Promotion or provider I/O is granted by this module.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from .github_b3_observer_job import B3ObserverJob, Invalid, _bounded_now
from .phase_schedule import (
    InvalidPhaseSchedule, PhaseSchedule, next_due_at, preview_json,
)

_VERSION = "b3-observer-phase/v1"
_MAX_LATE_SECONDS = 120


class B3PhaseRuntime:
    """Use the *same* B3 DB + lock for slot fencing and run admission.

    This is a second, explicitly off-by-default schedule mode. Existing
    relative settings are not migrated, altered, or silently enabled.
    """

    def __init__(self, job: B3ObserverJob):
        if type(job) is not B3ObserverJob:
            raise Invalid("invalid_b3_phase_job")
        self.job = job
        with job._lock:
            job.db.execute(
                "CREATE TABLE IF NOT EXISTS phase_settings ("
                "id INTEGER PRIMARY KEY CHECK(id=1), "
                "enabled INTEGER NOT NULL, timezone TEXT NOT NULL, "
                "anchor TEXT NOT NULL, interval_seconds INTEGER NOT NULL, "
                "revision INTEGER NOT NULL, next_due REAL, generation TEXT)"
            )
            job.db.execute(
                "INSERT OR IGNORE INTO phase_settings VALUES "
                "(1,0,'Asia/Tokyo','12:00',3600,0,NULL,NULL)"
            )
            job.db.execute(
                "CREATE TABLE IF NOT EXISTS phase_slots ("
                "revision INTEGER NOT NULL, slot_epoch INTEGER NOT NULL, "
                "generation TEXT NOT NULL, outcome TEXT NOT NULL, "
                "request_id TEXT NOT NULL UNIQUE, "
                "PRIMARY KEY (revision,slot_epoch,generation))"
            )
            job.db.commit()

    def _row(self):
        return self.job.db.execute(
            "SELECT enabled,timezone,anchor,interval_seconds,revision,"
            "next_due,generation FROM phase_settings WHERE id=1"
        ).fetchone()

    @staticmethod
    def _spec(row):
        return PhaseSchedule(
            enabled=bool(row[0]), timezone=row[1],
            anchor_local_time=row[2], interval_seconds=row[3],
        )

    def status(self):
        """Pure local read and phase preview, never a collection or write."""
        with self.job._lock:
            row = self._row()
            current = self.job._phase_generation()
            configured = current is not None
            bound = row[6]
            valid = configured and current == bound
            phase = self._spec(row)
            return {
                "schemaVersion": _VERSION,
                "enabled": bool(row[0]),
                "effectiveEnabled": bool(row[0]) and valid,
                "timezone": row[1],
                "anchorLocalTime": row[2],
                "intervalSeconds": row[3],
                "revision": row[4],
                "nextDueAt": (
                    datetime.fromtimestamp(row[5], timezone.utc).isoformat()
                    if row[0] and valid and row[5] is not None else None
                ),
                "reason": (
                    "configuration_missing" if current is None
                    else "source_changed" if row[0] and not valid
                    else None
                ),
                "authority": "observation_only",
                "vaultWriteAuthorized": False,
            }

    def configure(self, payload):
        """Trusted local-only configuration; public HTTP is intentionally NYI.

        expectedRevision is a durable CAS against concurrent human saves.
        ON does NOT launch a job and cannot coexist with relative B3 ON.
        """
        if type(payload) is not dict or set(payload) != {
            "enabled", "timezone", "anchorLocalTime",
            "intervalSeconds", "expectedRevision",
        } or type(payload["expectedRevision"]) is not int or payload["expectedRevision"] < 0:
            raise Invalid("invalid_b3_phase_settings")
        phase = PhaseSchedule(
            enabled=payload["enabled"],
            timezone=payload["timezone"],
            anchor_local_time=payload["anchorLocalTime"],
            interval_seconds=payload["intervalSeconds"],
        )
        try:
            # Explicit validation even when requested Off.
            preview_json(phase, datetime.now(timezone.utc))
        except (InvalidPhaseSchedule, TypeError):
            raise Invalid("invalid_b3_phase_settings") from None
        with self.job._lock:
            old = self._row()
            if old[4] != payload["expectedRevision"]:
                raise Invalid("stale_b3_phase_revision")
            current = self.job._phase_generation()
            if phase.enabled:
                if current is None:
                    raise Invalid("b3_source_not_configured")
                if self.job._settings()["enabled"]:
                    raise Invalid("b3_relative_schedule_already_enabled")
            now = _bounded_now(self.job.clock)
            next_due = (
                next_due_at(phase, datetime.fromtimestamp(now, timezone.utc)).timestamp()
                if phase.enabled else None
            )
            with self.job.db:
                self.job.db.execute(
                    "UPDATE phase_settings SET enabled=?,timezone=?,anchor=?,"
                    "interval_seconds=?,revision=?,next_due=?,generation=? WHERE id=1",
                    (int(phase.enabled), phase.timezone, phase.anchor_local_time,
                     phase.interval_seconds, old[4] + 1, next_due,
                     current if phase.enabled else None),
                )
            return self.status()

    def tick(self):
        """Advance at most ONE durable UTC slot, never catch up in a burst."""
        job = self.job
        with job._lock:
            # GET stays side-effect-free; recovery is tick/admission only.
            job._reconcile_dead_worker()
            row = self._row()
            if not row[0]:
                return False
            current = job._phase_generation()
            if current is None:
                return False  # temporary source outage: preserve desired On
            if current != row[6]:
                with job.db:
                    job.db.execute(
                        "UPDATE phase_settings SET enabled=0,next_due=NULL,"
                        "generation=NULL,revision=revision+1 WHERE id=1"
                    )
                return False
            if job._settings()["enabled"]:
                # Fail closed if a separate old relative schedule was enabled.
                return False
            now = _bounded_now(job.clock)
            due = row[5]
            if due is None or now < due:
                return False
            phase = self._spec(row)
            next_due = next_due_at(
                phase, datetime.fromtimestamp(now, timezone.utc)
            ).timestamp()
            # No replay on delayed tick, clock jumps or unknown outcomes.
            slot_epoch = int(due)
            request_id = hashlib.sha256(
                f"b3-phase-v1:{row[4]}:{current}:{slot_epoch}".encode("ascii")
            ).hexdigest()[:32]
            late = now - due > _MAX_LATE_SECONDS
            busy = job.db.execute(
                "SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1"
            ).fetchone() is not None
            decision = "skipped_late" if late else (
                "skipped_busy" if busy else "admitted"
            )
            admitted = None
            with job.db:
                job.db.execute(
                    "INSERT OR IGNORE INTO phase_slots "
                    "(revision,slot_epoch,generation,outcome,request_id) "
                    "VALUES (?,?,?,?,?)",
                    (row[4], slot_epoch, current, decision, request_id),
                )
                # If a slot already exists, it must never be re-admitted.
                inserted = job.db.execute("SELECT changes()").fetchone()[0] == 1
                job.db.execute(
                    "UPDATE phase_settings SET next_due=? WHERE id=1",
                    (next_due,),
                )
                if inserted and decision == "admitted":
                    admitted = job._admit(request_id, "scheduled", now)
            if admitted is not None:
                job._launch(admitted["run_id"])
                return True
            return False
