"""#300 Phase I: durable *disabled-by-default* single-LXC operator intake.

This module is not installed and does not execute systemd. A separate,
explicitly configured, peer-verified OS-local worker may accept a fixed
operation into a durable one-at-a-time queue, and a different worker context
may process it using a separately qualified OperationBroker.

No HTTP executor, sudo/polkit authority, production socket unit, bypass of the
deployment gate, or Run once effect authorization is supplied here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Callable, Mapping

from .admin_operation_broker import (
    BrokerRejected, _read_private, _safe_open_root, _store_new, _utc_clock,
)
from .admin_operation_preflight import _REQUEST_ID
from .admin_status import SERVICES

_ACTIONS = frozenset(("pause", "resume"))
_SERVICE_IDS = frozenset(spec.identifier for spec in SERVICES)
_CURRENT_VERSION = 1
MAX_AUDIT_ENTRIES = 2048  # a full journal must be archived by a reviewed tool
_LOCK_FILE = "admission.lock"
_ACTIVE_FILE = "active.json"
_REQUEST_FIELDS = frozenset((
    "record_version", "request_id", "service_id", "action", "state", "queued_at",
))
_RESULT_FIELDS = frozenset((
    "record_version", "request_id", "service_id", "action", "state",
))
_ERROR_CODES = frozenset((
    "queue_disabled", "invalid_operation", "queue_busy", "queue_recovery_required",
    "unsafe_admission_lock", "admission_lock_busy", "queue_state_invalid",
    "queue_missing_receipt", "indeterminate_effect", "queue_internal_error",
    "privileged_worker_required", "run_once_unqualified",
))
_ALLOWED_RESULTS = frozenset(("completed", "indeterminate"))
_ALLOWED_STATES = frozenset((
    "accepted", "processing", "completed", "completed_pending_cleanup",
    "indeterminate",
))


@dataclass(frozen=True)
class QueuedOperation:
    request_id: str
    service_id: str
    action: str

    def as_public(self, state: str) -> dict[str, object]:
        return {
            "version": 1, "request_id": self.request_id,
            "service_id": self.service_id, "action": self.action,
            "state": state, "authority": "durable_queue_only",
            "can_execute": False,
        }


def _request(value: object) -> QueuedOperation:
    if (
        type(value) is not dict
        or any(type(key) is not str for key in value)
        or set(value) != {"version", "request_id", "service_id", "action"}
        or type(value.get("version")) is not int
        or value["version"] != 1
        or type(value["request_id"]) is not str
        or _REQUEST_ID.fullmatch(value["request_id"]) is None
        or type(value["service_id"]) is not str
        or value["service_id"] not in _SERVICE_IDS
        or type(value["action"]) is not str
        or value["action"] not in _ACTIONS
    ):
        raise BrokerRejected("invalid_operation")
    return QueuedOperation(value["request_id"], value["service_id"], value["action"])


def _receipt_id(value: object) -> str:
    if type(value) is not str or _REQUEST_ID.fullmatch(value) is None:
        raise BrokerRejected("invalid_operation")
    return value


def _safe_identity(value: Mapping[str, object]) -> QueuedOperation:
    if (
        type(value) is not dict
        or set(value) != _REQUEST_FIELDS
        or type(value["record_version"]) is not int
        or value["record_version"] != _CURRENT_VERSION
        or type(value["request_id"]) is not str
        or _REQUEST_ID.fullmatch(value["request_id"]) is None
        or type(value["service_id"]) is not str
        or value["service_id"] not in _SERVICE_IDS
        or type(value["action"]) is not str
        or value["action"] not in _ACTIONS
        or value["state"] not in ("accepted", "processing")
        or type(value["queued_at"]) is not str
        or len(value["queued_at"]) > 40
    ):
        raise BrokerRejected("queue_state_invalid")
    return QueuedOperation(value["request_id"], value["service_id"], value["action"])


def _lock(fd: int) -> int:
    lock_fd = None
    try:
        lock_fd = os.open(
            _LOCK_FILE,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=fd,
        )
        before = os.fstat(lock_fd)
        current = os.stat(_LOCK_FILE, dir_fd=fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise BrokerRejected("unsafe_admission_lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BrokerRejected("admission_lock_busy") from None
        current = os.stat(_LOCK_FILE, dir_fd=fd, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise BrokerRejected("unsafe_admission_lock")
        locked, lock_fd = lock_fd, None
        return locked
    except BrokerRejected:
        raise
    except OSError:
        raise BrokerRejected("unsafe_admission_lock") from None
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def _exists(fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise BrokerRejected("queue_state_invalid") from None


def _unlink(fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=fd)
        os.fsync(fd)
    except OSError:
        raise BrokerRejected("queue_state_invalid") from None


class DurableOperationQueue:
    """Bounded global singleton queue and audit receipt store.

    'enabled' controls intake only. It is not an effects permission, and
    neither submitting nor polling can ever call an executor. The runner is
    an explicitly separate trusted call under a separately qualified gate.
    """

    def __init__(
        self, *,
        queue_root: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        require_root: bool = True,
        enabled: bool = False,
    ):
        self.queue_root = queue_root
        self.clock = clock
        self.require_root = require_root
        self.enabled = enabled

    def _open(self) -> tuple[int, int]:
        if self.require_root and os.geteuid() != 0:
            raise BrokerRejected("privileged_worker_required")
        dirfd = _safe_open_root(self.queue_root, self.require_root)
        try:
            lockfd = _lock(dirfd)
            return dirfd, lockfd
        except Exception:
            os.close(dirfd)
            raise

    @staticmethod
    def _close(dirfd: int, lockfd: int) -> None:
        try:
            os.close(lockfd)
        finally:
            os.close(dirfd)

    @staticmethod
    def _id_path(rid: str, prefix: str) -> str:
        # Never used without _receipt_id, no path traversal or arbitrary names.
        return prefix + "-" + rid + ".json"

    def _pending(self, dirfd: int) -> dict[str, object] | None:
        return _read_private(dirfd, _ACTIVE_FILE) if _exists(dirfd, _ACTIVE_FILE) else None

    def _state(self, dirfd: int, request: QueuedOperation) -> str:
        rid = request.request_id
        record = _read_private(dirfd, self._id_path(rid, "request"))
        original = _safe_identity(record)
        if original != request:
            raise BrokerRejected("queue_state_invalid")
        processing_exists = _exists(dirfd, self._id_path(rid, "processing"))
        if processing_exists:
            claim = _read_private(dirfd, self._id_path(rid, "processing"))
            if claim != {
                "record_version": 1, "request_id": rid,
                "service_id": request.service_id, "action": request.action,
                "state": "processing",
            }:
                raise BrokerRejected("queue_state_invalid")
        active = self._pending(dirfd)
        if _exists(dirfd, self._id_path(rid, "result")):
            result = _read_private(dirfd, self._id_path(rid, "result"))
            if (
                type(result) is not dict or set(result) != _RESULT_FIELDS
                or type(result["record_version"]) is not int
                or result["record_version"] != 1
                or result["request_id"] != rid
                or result["service_id"] != request.service_id
                or result["action"] != request.action
                or result["state"] not in _ALLOWED_RESULTS
                or not processing_exists
            ):
                # A completion without its prior durable processing claim
                # cannot be accepted as proven; no automatic history repair.
                raise BrokerRejected("queue_state_invalid")
            if result["state"] == "indeterminate":
                if active != record:
                    raise BrokerRejected("queue_state_invalid")
                return "indeterminate"
            if active == record:
                return "completed_pending_cleanup"
            # A *different* accepted or processing operation may now be
            # active. It does not invalidate this older immutable completion.
            return "completed"
        if active is None or active != record:
            raise BrokerRejected("queue_state_invalid")
        return "processing" if processing_exists else "accepted"

    @staticmethod
    def _prior_work_fully_settled(dirfd: int) -> None:
        """No new effects if a durable older request lacks a valid completion.

        The active marker normally provides this guard. Independently scan a
        bounded history so a lost/deleted active marker or orphaned processing
        record cannot silently authorize a new effect. Never automatically
        reconcile an indeterminate result or delete any journal files.
        """
        try:
            names = os.listdir(dirfd)
        except OSError:
            raise BrokerRejected("queue_state_invalid") from None
        if len(names) > MAX_AUDIT_ENTRIES:
            raise BrokerRejected("queue_recovery_required")
        requests: set[str] = set()
        processing: set[str] = set()
        results: set[str] = set()
        for name in names:
            if type(name) is not str:
                raise BrokerRejected("queue_state_invalid")
            for prefix, dest in (
                ("request-", requests),
                ("processing-", processing),
                ("result-", results),
            ):
                if name.startswith(prefix):
                    if not name.endswith(".json"):
                        raise BrokerRejected("queue_state_invalid")
                    rid = name[len(prefix):-len(".json")]
                    if _REQUEST_ID.fullmatch(rid) is None:
                        raise BrokerRejected("queue_state_invalid")
                    dest.add(rid)
                    break
        if (processing | results) - requests:
            raise BrokerRejected("queue_recovery_required")
        for rid in requests:
            # A complete outcome must have its own immutable receipt. The
            # absence of active.json is NOT by itself proof of safe recovery.
            if rid not in results:
                raise BrokerRejected("queue_recovery_required")
            record = _read_private(dirfd, f"request-{rid}.json")
            action = _safe_identity(record)
            if action.request_id != rid:
                raise BrokerRejected("queue_state_invalid")
            final = _read_private(dirfd, f"result-{rid}.json")
            if (
                type(final) is not dict
                or set(final) != _RESULT_FIELDS
                or type(final["record_version"]) is not int
                or final["record_version"] != 1
                or final["request_id"] != rid
                or final["service_id"] != action.service_id
                or final["action"] != action.action
                or final["state"] != "completed"
            ):
                raise BrokerRejected("queue_recovery_required")
            if rid not in processing:
                # A finalized action without a durable claim is inconsistent.
                raise BrokerRejected("queue_recovery_required")
            claim = _read_private(dirfd, f"processing-{rid}.json")
            if claim != {
                "record_version": 1, "request_id": rid,
                "service_id": action.service_id, "action": action.action,
                "state": "processing",
            }:
                raise BrokerRejected("queue_recovery_required")

    def submit(self, raw: object) -> dict[str, object]:
        """Durably accept an exact, fixed action or return a replay receipt.

        No effect is performed. A second request ID is rejected while the
        first is accepted/processing or its completion remains uncertain.
        """
        if self.enabled is not True:
            raise BrokerRejected("queue_disabled")
        action = _request(raw)
        dirfd, lockfd = self._open()
        try:
            existing = self._id_path(action.request_id, "request")
            if _exists(dirfd, existing):
                return action.as_public(self._state(dirfd, action))
            if _exists(dirfd, self._id_path(action.request_id, "result")):
                raise BrokerRejected("queue_state_invalid")
            if self._pending(dirfd) is not None:
                raise BrokerRejected("queue_busy")
            self._prior_work_fully_settled(dirfd)
            queued_at = _utc_clock(self.clock).isoformat(timespec="seconds")
            record = {
                "record_version": 1, "request_id": action.request_id,
                "service_id": action.service_id, "action": action.action,
                "state": "accepted", "queued_at": queued_at,
            }
            # Global pending marker FIRST; an interrupted first fsync leaves
            # a marker that blocks all new work, rather than an orphan action.
            _store_new(dirfd, _ACTIVE_FILE, record)
            _store_new(dirfd, existing, record)
            return action.as_public("accepted")
        finally:
            self._close(dirfd, lockfd)

    def receipt(self, raw_id: object) -> dict[str, object]:
        rid = _receipt_id(raw_id)
        dirfd, lockfd = self._open()
        try:
            if not _exists(dirfd, self._id_path(rid, "request")):
                raise BrokerRejected("queue_missing_receipt")
            raw = _read_private(dirfd, self._id_path(rid, "request"))
            action = _safe_identity(raw)
            return action.as_public(self._state(dirfd, action))
        finally:
            self._close(dirfd, lockfd)

    def claim_for_worker(self) -> QueuedOperation:
        """One-way durable transition, NEVER execute or resubmit on replay.

        After this transition a crash may mean the effect was attempted. No
        auto-retry is possible; an operator must explicitly reconcile it.
        """
        if self.enabled is not True:
            raise BrokerRejected("queue_disabled")
        dirfd, lockfd = self._open()
        try:
            raw = self._pending(dirfd)
            if raw is None:
                raise BrokerRejected("queue_missing_receipt")
            action = _safe_identity(raw)
            if not _exists(dirfd, self._id_path(action.request_id, "request")):
                raise BrokerRejected("queue_state_invalid")
            if _read_private(dirfd, self._id_path(action.request_id, "request")) != raw:
                raise BrokerRejected("queue_state_invalid")
            if (
                raw["state"] != "accepted"
                or _exists(dirfd, self._id_path(action.request_id, "processing"))
                or _exists(dirfd, self._id_path(action.request_id, "result"))
            ):
                raise BrokerRejected("queue_recovery_required")
            # Append immutable claim as its OWN fsynced record. Crash before
            # any effect but after claim is safe to classify as uncertain.
            _store_new(dirfd, self._id_path(action.request_id, "processing"), {
                "record_version": 1, "request_id": action.request_id,
                "service_id": action.service_id, "action": action.action,
                "state": "processing",
            })
            # Since active.json is immutable and retains accepted, its claim
            # record is the authoritative processing state.
            return action
        finally:
            self._close(dirfd, lockfd)

    def settle(self, action: QueuedOperation, *, completed: bool) -> dict[str, object]:
        """Worker-only completion after an independent effect result.

        Any unknown/failed return must be indeterminate; never claim an
        operation was not applied based on a lost response or timeout.
        """
        if self.enabled is not True:
            raise BrokerRejected("queue_disabled")
        if type(action) is not QueuedOperation:
            raise BrokerRejected("invalid_operation")
        dirfd, lockfd = self._open()
        try:
            original = self._pending(dirfd)
            if original is None or _safe_identity(original) != action:
                raise BrokerRejected("queue_state_invalid")
            claim = _read_private(dirfd, self._id_path(action.request_id, "processing"))
            if claim != {
                "record_version": 1, "request_id": action.request_id,
                "service_id": action.service_id, "action": action.action,
                "state": "processing",
            }:
                raise BrokerRejected("queue_state_invalid")
            if _exists(dirfd, self._id_path(action.request_id, "result")):
                raise BrokerRejected("queue_recovery_required")
            state = "completed" if completed is True else "indeterminate"
            _store_new(dirfd, self._id_path(action.request_id, "result"), {
                "record_version": 1, "request_id": action.request_id,
                "service_id": action.service_id, "action": action.action,
                "state": state,
            })
            if completed is True:
                _unlink(dirfd, _ACTIVE_FILE)
                return action.as_public("completed")
            return action.as_public("indeterminate")
        finally:
            self._close(dirfd, lockfd)

    def work_once(
        self, executor: Callable[[QueuedOperation], bool],
    ) -> dict[str, object]:
        """Explicit caller-controlled processing; never started from socket.

        Future live worker must be an independently authenticated supervisor,
        must re-observe systemd, pin code/unit identity and hold a qualified
        updater/launcher gate for the entire effect.
        """
        action = self.claim_for_worker()
        try:
            applied = executor(action)
        except BaseException:
            # Leave processing evidence intact if executor died/raised or was
            # interrupted. A new worker must not replay or assume no effect.
            self.settle(action, completed=False)
            raise BrokerRejected("indeterminate_effect") from None
        if applied is not True:
            self.settle(action, completed=False)
            raise BrokerRejected("indeterminate_effect")
        return self.settle(action, completed=True)
