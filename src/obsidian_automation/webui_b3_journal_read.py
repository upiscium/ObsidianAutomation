"""Unattached, read-only projection of the real single-owner B3 attempt journal.

This is a #483 WorkflowReadPort candidate, NOT an HTTP adapter, a Scheduler,
a real B3 control service or authority to run existing CAS Writers. It never
creates the journal, authenticates the host configuration or calls the Job.
An integrator must explicitly connect this to the existing #480 v1 read
composer; the current optional port registry intentionally accepts only the
B3 fake source, so source presence cannot silently enable a control.

A successful two-Writer GET observation is NOT full Project Sync success:
Watcher, Mirror, Core Promotion, current remote convergence and incident
recovery remain unverified. Therefore v1 lastVerifiedOutcome always stays
null, and even a local completed Writer pair maps to unknown full effect.
"""
from __future__ import annotations

import math
import time
from typing import Callable

from .github_b3_attempts import B3AttemptJournal, VERSION as JOURNAL_VERSION
from .webui_b3_adapter import VERSION as WORKFLOW_VERSION


_REVISION_NOT_AUTHORIZED = "0" * 32
_LABEL = "Project data into Vault"
_EFFECT_UNKNOWN = {"kind": "project_vault", "state": "unknown", "changed": None}


def _valid_time(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return 0 <= value <= 253402300799 and math.isfinite(value)
    except (TypeError, ValueError, OverflowError):
        return False


def _validate_view(view: object, observed: float) -> dict[str, object]:
    if (
        type(view) is not dict
        or set(view) != {
            "schema_version", "last_attempt", "last_writer_pair_at",
            "has_unconfirmed",
        }
        or view["schema_version"] != JOURNAL_VERSION
        or type(view["has_unconfirmed"]) is not bool
        or (view["last_writer_pair_at"] is not None
            and (not _valid_time(view["last_writer_pair_at"])
                 or view["last_writer_pair_at"] > observed))
    ):
        raise ValueError("invalid_b3_read_observation")
    last = view["last_attempt"]
    if last is None:
        if view["has_unconfirmed"]:
            raise ValueError("invalid_b3_read_observation")
        return view
    if type(last) is not dict or set(last) != {
        "request_id", "state", "terminal_code",
        "created_at", "started_at", "completed_at",
    }:
        raise ValueError("invalid_b3_read_observation")
    request = last["request_id"]
    if (type(request) is not str or len(request) != 32
            or any(ch not in "0123456789abcdef" for ch in request)):
        raise ValueError("invalid_b3_read_observation")
    state = last["state"]
    if (
        type(state) is not str
        or state not in ("queued", "running", "completed", "unconfirmed")
        or not _valid_time(last["created_at"])
        or last["created_at"] > observed
        or (last["started_at"] is not None
            and (not _valid_time(last["started_at"])
                 or not last["created_at"] <= last["started_at"] <= observed))
        or (last["completed_at"] is not None
            and (not _valid_time(last["completed_at"])
                 or not last["created_at"] <= last["completed_at"] <= observed))
    ):
        raise ValueError("invalid_b3_read_observation")
    if state == "queued":
        if (last["terminal_code"] is not None or last["started_at"] is not None
                or last["completed_at"] is not None):
            raise ValueError("invalid_b3_read_observation")
    elif state == "running":
        if (last["terminal_code"] is not None or last["started_at"] is None
                or last["completed_at"] is not None):
            raise ValueError("invalid_b3_read_observation")
    elif last["completed_at"] is None or type(last["terminal_code"]) is not str:
        raise ValueError("invalid_b3_read_observation")
    elif (last["started_at"] is not None
          and last["started_at"] > last["completed_at"]):
        raise ValueError("invalid_b3_read_observation")
    # Only the original Journal may validate terminal codes and proof joins.
    # The read port additionally checks bounded presentation fields.
    return view


def _base(now: float, *, integration: str, reason: str) -> dict[str, object]:
    return {
        "schemaVersion": WORKFLOW_VERSION, "id": "b3",
        "label": _LABEL, "adapterMode": "read_only",
        "revision": _REVISION_NOT_AUTHORIZED,
        "observedAt": now, "integration": integration,
        "configuration": {"state": "not_observed", "reason": reason},
        "schedule": {
            "state": "unknown", "intervalSeconds": None,
            "nextDueAt": None, "reason": (
                "scheduler_decision_required" if integration == "pending"
                else reason
            ),
        },
        "execution": {
            "state": "unknown", "requestId": None, "startedAt": None,
        },
        "lastAttempt": None, "lastVerifiedOutcome": None,
        "lastAttemptAt": None,
        "availableActions": [],
        "actionReasons": {
            "sync_project": "read_only" if integration == "pending" else reason,
            "set_schedule": (
                "scheduler_decision_required" if integration == "pending"
                else reason
            ),
        },
    }


class B3JournalReadAdapter:
    """Read-only observation of an already-open B3AttemptJournal.

    No initializers, run methods, credentials, or control capabilities.
    A caller must open the existing private Journal separately and retain the
    single-owner lock; failure never silently constructs an empty new store.
    """

    def __init__(
        self,
        journal: B3AttemptJournal | None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if journal is not None and type(journal) is not B3AttemptJournal:
            raise TypeError("invalid_b3_read_port")
        if not callable(clock):
            raise TypeError("invalid_b3_read_clock")
        self._journal = journal
        self._clock = clock

    def _read(self) -> tuple[float, dict[str, object] | None, str]:
        # Observe the durable run first, then sample the presentation clock.
        # A concurrent new admission should not be made 'future' solely
        # because the clock was sampled before the journal read.
        if self._journal is None:
            value, integration = None, "not_attached"
        else:
            try:
                value = self._journal.workflow_read_observation()
                integration = "pending"
            except Exception:
                value, integration = None, "unavailable"
        try:
            now = self._clock()
            if not _valid_time(now):
                raise ValueError("invalid clock")
        except Exception:
            return 0.0, None, "unavailable"
        if integration != "pending":
            return float(now), None, integration
        try:
            return float(now), _validate_view(value, float(now)), "pending"
        except Exception:
            # No raw SQLite/OS/path/credential/record error text leaves the port.
            return float(now), None, "unavailable"

    def observe(self) -> dict[str, object]:
        now, view, integration = self._read()
        reason = {
            "pending": "pending_integration",
            "not_attached": "not_attached",
            "unavailable": "observation_unavailable",
        }[integration]
        dto = _base(now, integration=integration, reason=reason)
        if view is None:
            return dto
        last = view["last_attempt"]
        if last is None:
            dto["execution"]["state"] = (
                "unknown" if view["has_unconfirmed"] else "idle"
            )
            return dto
        state = last["state"]
        display_state = "completed" if state == "unconfirmed" else state
        dto["lastAttempt"] = {
            "requestId": last["request_id"],
            "state": display_state,
            "outcome": "unknown" if display_state == "completed" else None,
            "completedAt": last["completed_at"] if display_state == "completed" else None,
            "effect": dict(_EFFECT_UNKNOWN),
        }
        dto["lastAttemptAt"] = last["created_at"]
        if state in ("queued", "running"):
            dto["execution"] = {
                "state": state,
                "requestId": last["request_id"],
                "startedAt": last["started_at"],
            }
        # Never call a locally completed Writer pair 'success' in this DTO.
        # A later failure, stale Writer receipt or unresolved attempt remains
        # unknown until the full producer and pipeline have been certified.
        return dto

    def detail(self) -> dict[str, object]:
        _, view, integration = self._read()
        return {
            "watcher": "not_observed",
            "mirror": "not_observed",
            "writer": (
                "local_pair_observed" if view is not None
                and view["last_writer_pair_at"] is not None
                else ("not_observed" if integration == "pending"
                      else integration)
            ),
            "lastWriterPairAt": (
                view["last_writer_pair_at"] if view is not None else None
            ),
            "unconfirmed": (
                view["has_unconfirmed"] if view is not None else None
            ),
            "pipelineSuccess": None,
            "cas": "not_observed",
        }
