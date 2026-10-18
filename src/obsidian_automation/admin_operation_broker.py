"""#300 effect-capable *unwired* local broker core with durable fail-closed audit.

NO HTTP route, daemon, privileged socket, installer or production activation.
An authorized installer must independently construct a real deployment/host
maintenance gate and OS identity boundary before wiring this module.
"""
from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
import re
import select
import time
from pathlib import Path
import stat
from typing import Callable, Mapping, Protocol

from .admin_operation_preflight import (
    OperationCandidate, PreflightRejected, preflight_operation,
)
from .admin_status import (
    SERVICE_FIELDS, SERVICES, TIMER_FIELDS, ServiceSpec,
    classify_service, classify_timer,
)
from .admin_workflow_quiescence import (
    UNIT_NAMES, GROUP_UNITS, TIMER_TARGETS,
    TIMER_READ_FIELDS as WORKFLOW_TIMER_READ_FIELDS,
    WORKFLOW_READ_FIELDS, WORKFLOW_REJECTION_CODES,
    WorkflowObservation, WorkflowNotQuiescent, require_run_once_quiescent,
)

# Never accept arbitrary unit names, shell fragments, or commands from HTTP.
TIMER_READ_FIELDS = (*TIMER_FIELDS, "Unit", "Job", "FragmentPath")
SERVICE_READ_FIELDS = (*SERVICE_FIELDS, "Job", "MainPID", "ControlPID", "FragmentPath")
MAX_RECORD_BYTES = 2048
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


class BrokerRejected(RuntimeError):
    """Stable, redacted fixed error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LiveUnits:
    timer: Mapping[str, str]
    service: Mapping[str, str]


class UnitAdapter(Protocol):
    """Trusted, locally constructed adapter. Not a browser-controlled input."""

    def observe(self, spec: ServiceSpec) -> LiveUnits: ...
    def apply(self, argv: tuple[str, ...]) -> bool: ...
    def boot_id(self) -> str: ...


@dataclass(frozen=True)
class BrokerResult:
    request_id: str
    service_id: str
    action: str
    state: str = "completed"
    authority: str = "local_effect_observed"

    def public(self) -> dict[str, str]:
        return {
            "request_id": self.request_id, "service_id": self.service_id,
            "action": self.action, "state": self.state,
            "authority": self.authority,
        }


MaintenanceGateFactory = Callable[[], AbstractContextManager[None]]
Clock = Callable[[], datetime]


def _command(action: str, spec: ServiceSpec) -> tuple[str, ...]:
    """Static argv only, no shell or caller-provided unit/command."""
    if action == "pause":
        return ("systemctl", "disable", "--now", spec.timer)
    if action == "resume":
        return ("systemctl", "enable", "--now", spec.timer)
    if action == "run_once":
        return ("systemctl", "start", spec.service)
    raise BrokerRejected("unknown_action")


def _fields(raw: Mapping[str, str], expected: tuple[str, ...]) -> bool:
    return (
        isinstance(raw, dict)
        and set(raw) == set(expected)
        and all(
            type(value) is str
            and 0 <= len(value) <= 256
            and all(32 <= ord(char) <= 126 for char in value)
            for value in raw.values()
        )
    )


def _verified_live(spec: ServiceSpec, live: LiveUnits) -> tuple[str, str, str]:
    """Recognize only stable OS unit identity, idle jobs and known states."""
    if not isinstance(live, LiveUnits):
        raise BrokerRejected("invalid_live_observation")
    if not _fields(live.timer, TIMER_READ_FIELDS) or not _fields(
        live.service, SERVICE_READ_FIELDS
    ):
        raise BrokerRejected("invalid_live_observation")
    timer = live.timer
    service = live.service
    if (
        timer["Unit"] != spec.service
        or timer["FragmentPath"] != f"/etc/systemd/system/{spec.timer}"
        or service["FragmentPath"] != f"/etc/systemd/system/{spec.service}"
        or service["Type"] != "oneshot"
        or timer["LoadState"] != "loaded"
        or service["LoadState"] != "loaded"
        # Real systemctl show Job= renders an empty string when no job is
        # queued; older/test projections may use "0". Both are idle only
        # when the corresponding ActiveState/SubState and PID guards agree.
        or timer["Job"] not in ("", "0")
        or service["Job"] not in ("", "0")
        or service["MainPID"] != "0"
        or service["ControlPID"] != "0"
    ):
        raise BrokerRejected("unverified_unit_identity_or_busy")
    schedule = classify_timer(timer)
    execution = classify_service(service)
    if schedule not in ("scheduled", "paused") or execution not in (
        "succeeded", "failed", "not_observed"
    ):
        raise BrokerRejected("unsafe_live_state")
    # The strict observed schedule is insufficient if the service is busy.
    if schedule == "scheduled" and (
        timer["UnitFileState"] != "enabled"
        or timer["ActiveState"] != "active"
        or timer["SubState"] != "waiting"
    ):
        raise BrokerRejected("unsafe_live_state")
    if schedule == "paused" and (
        timer["UnitFileState"] != "disabled"
        or timer["ActiveState"] != "inactive"
        or timer["SubState"] != "dead"
    ):
        raise BrokerRejected("unsafe_live_state")
    health = (
        "failed" if execution == "failed" else "paused" if schedule == "paused"
        else "observed_success" if execution == "succeeded" else "unverified"
    )
    return schedule, execution, health


def _start_monotonic(live: LiveUnits) -> int:
    raw = live.service["ExecMainStartTimestampMonotonic"]
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 20:
        raise BrokerRejected("unverified_execution_identity")
    return int(raw)


def _boot_identity(adapter: UnitAdapter) -> str:
    try:
        value = adapter.boot_id()
        if type(value) is not str or _BOOT_ID.fullmatch(value) is None:
            raise BrokerRejected("boot_identity_unavailable")
        return value
    except Exception:
        raise BrokerRejected("boot_identity_unavailable") from None


def _safe_open_root(path: Path, require_root: bool) -> int:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise BrokerRejected("invalid_audit_root")
    # Componentwise openat, not a check-then-open symlink walk. Hold each
    # directory descriptor while opening its child, without following links.
    parent_fd = None
    try:
        parent_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for part in path.parts[1:]:
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
            info = os.fstat(parent_fd)
            if not stat.S_ISDIR(info.st_mode):
                raise BrokerRejected("unsafe_audit_root")
            if require_root and (
                info.st_uid != 0 or (stat.S_IMODE(info.st_mode) & 0o022)
            ):
                raise BrokerRejected("unsafe_audit_root")
        info = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != (0 if require_root else os.geteuid())
        ):
            raise BrokerRejected("unsafe_audit_root")
        fd, parent_fd = parent_fd, None
        return fd
    except (OSError, BrokerRejected):
        raise BrokerRejected("unsafe_audit_root") from None
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _locked(fd: int) -> int:
    try:
        lock_fd = os.open(
            "broker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=fd,
        )
        info = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise BrokerRejected("unsafe_broker_lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            raise BrokerRejected("another_operation_active") from None
        return lock_fd
    except (OSError, BrokerRejected):
        if "lock_fd" in locals():
            os.close(lock_fd)
        raise BrokerRejected("another_operation_active") from None


def _present(fd: int, name: str) -> bool:
    try:
        # Includes symlinks: any occupant of a reserved name blocks use.
        os.stat(name, dir_fd=fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise BrokerRejected("journal_unavailable") from None


def _store_new(fd: int, name: str, record: Mapping[str, object]) -> None:
    payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(payload) > MAX_RECORD_BYTES:
        raise BrokerRejected("audit_record_over_bound")
    handle = None
    try:
        handle = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=fd,
        )
        info = os.fstat(handle)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise BrokerRejected("unsafe_audit_record")
        offset = 0
        while offset < len(payload):
            written = os.write(handle, payload[offset:])
            if written <= 0:
                raise BrokerRejected("audit_write_failed")
            offset += written
        os.fsync(handle)
        os.fsync(fd)
    except (OSError, BrokerRejected):
        # A partial/ambiguous file remains on disk and blocks replay.
        raise BrokerRejected("audit_write_failed") from None
    finally:
        if handle is not None:
            os.close(handle)


def _utc_clock(clock: Clock) -> datetime:
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise BrokerRejected("invalid_broker_clock")
    utc = now.astimezone(timezone.utc)
    if utc.utcoffset() is None:
        raise BrokerRejected("invalid_broker_clock")
    return utc


def _read_private(fd: int, name: str) -> dict[str, object]:
    handle = None
    try:
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        info = os.fstat(handle)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
            or not 1 <= info.st_size <= MAX_RECORD_BYTES
        ):
            raise BrokerRejected("unsafe_audit_record")
        data = os.read(handle, MAX_RECORD_BYTES + 1)
        if len(data) != info.st_size:
            raise BrokerRejected("unsafe_audit_record")
        def unique(pairs):
            obj = {}
            for key, value in pairs:
                if key in obj:
                    raise BrokerRejected("unsafe_audit_record")
                obj[key] = value
            return obj
        parsed = json.loads(data, object_pairs_hook=unique)
        if not isinstance(parsed, dict):
            raise BrokerRejected("unsafe_audit_record")
        return parsed
    except (OSError, ValueError, UnicodeError):
        raise BrokerRejected("unsafe_audit_record") from None
    finally:
        if handle is not None:
            os.close(handle)


class OperationBroker:
    """Crash-conservative transaction engine; unavailable by default.

    The caller MUST supply an independently qualified cross-updater maintenance
    exclusion context manager. A non-root HTTP server must never instantiate
    a root-capable broker or provide arbitrary adapters / filesystem paths.
    """

    def __init__(
        self, *, audit_root: Path, adapter: UnitAdapter,
        maintenance_gate: MaintenanceGateFactory, clock: Clock,
        require_root: bool = True, effects_enabled: bool = False,
        workflow_observer: Callable[[ServiceSpec], WorkflowObservation] | None = None,
    ):
        self.audit_root = audit_root
        self.adapter = adapter
        self.maintenance_gate = maintenance_gate
        self.workflow_observer = workflow_observer
        self.clock = clock
        self.require_root = require_root
        self.effects_enabled = effects_enabled

    @contextmanager
    def _guarded_maintenance(self):
        try:
            with self.maintenance_gate():
                yield
        except BrokerRejected:
            raise
        except Exception:
            # Before journal writes: no side effect. After a journal starts:
            # pending blocks replay even if an unexpected exception occurs.
            raise BrokerRejected("maintenance_or_observation_failed") from None

    def apply(
        self, request: Mapping[str, object], trusted_snapshot: Mapping[str, object]
    ) -> BrokerResult:
        if self.effects_enabled is not True:
            raise BrokerRejected("effect_authority_disabled")
        if self.require_root and os.geteuid() != 0:
            raise BrokerRejected("privileged_broker_required")
        # Importantly: this preflight is only a syntactic/snapshot hint; the
        # trusted snapshot MUST be server-owned and subsequently live-rechecked.
        now = _utc_clock(self.clock)
        try:
            intent = preflight_operation(
                request, trusted_snapshot, now=now, deployment_quiescent=True,
            )
        except PreflightRejected as exc:
            raise BrokerRejected(exc.reason) from None
        spec = next((s for s in SERVICES if s.identifier == intent.service_id), None)
        if spec is None:
            raise BrokerRejected("unknown_service")
        journal_fd = _safe_open_root(self.audit_root, self.require_root)
        try:
            lock_fd = _locked(journal_fd)
            try:
                rid = intent.request_id
                if (
                    _present(journal_fd, "pending.json")
                    or _present(journal_fd, f"request-{rid}.json")
                    or _present(journal_fd, f"result-{rid}.json")
                ):
                    raise BrokerRejected("pending_or_duplicate_operation")
                # Maintenance gate must hold all necessary update locks until
                # outcome receipt is durable; a fake or incomplete gate cannot
                # establish production approval by itself.
                with self._guarded_maintenance():
                    before = self.adapter.observe(spec)
                    observed = _verified_live(spec, before)
                    listed = next(
                        (entry for entry in trusted_snapshot["services"]
                         if entry["id"] == intent.service_id),
                        None,
                    )
                    if listed is None or observed != (
                        listed["schedule"], listed["latest_execution"], listed["health"],
                    ):
                        raise BrokerRejected("live_state_changed")
                    # A maintenance gate may wait a long time. Check the
                    # original browser snapshot again AFTER acquiring it.
                    try:
                        preflight_operation(
                            request, trusted_snapshot, now=_utc_clock(self.clock),
                            deployment_quiescent=True,
                        )
                    except PreflightRejected as exc:
                        raise BrokerRejected(exc.reason) from None
                    if intent.action == "pause" and observed[0] != "scheduled":
                        raise BrokerRejected("operation_state_mismatch")
                    if intent.action == "resume" and observed[0] != "paused":
                        raise BrokerRejected("operation_state_mismatch")
                    if intent.action == "run_once" and (
                        observed[0] != "paused" or observed[1] == "failed"
                    ):
                        # A paused timer cannot race with its own scheduled
                        # activation; never run an uncontrolled live recurrence.
                        raise BrokerRejected("run_once_requires_paused_idle_timer")
                    # A final service being inactive does NOT establish its
                    # Requires/OnSuccess role chain is idle. All other role
                    # timers must be paused and every fixed dependent unit
                    # verified idle before a manually triggered full cycle.
                    # This does not replace full launcher/update exclusion.
                    if intent.action == "run_once":
                        if self.workflow_observer is None:
                            raise BrokerRejected("workflow_observer_unavailable")
                        try:
                            workflow = self.workflow_observer(spec)
                        except Exception:
                            # The observer is an independently supplied local
                            # source, not a fixed-code rejection authority.
                            # Never copy its exception body or .code to logs,
                            # HTTP results, or durable evidence.
                            raise BrokerRejected("workflow_observation_failed") from None
                        try:
                            require_run_once_quiescent(spec, workflow)
                        except WorkflowNotQuiescent as exc:
                            # A synthetic/broken injected map can raise the
                            # same exception class from key hashing/equality.
                            # Only exact reviewed reason codes may escape.
                            code = (
                                exc.code if type(exc) is WorkflowNotQuiescent
                                and type(exc.code) is str
                                and exc.code in WORKFLOW_REJECTION_CODES
                                else "workflow_validation_unavailable"
                            )
                            raise BrokerRejected(code) from None
                        except Exception:
                            raise BrokerRejected("workflow_validation_unavailable") from None
                    entry = {
                        "record_version": 1, "request_id": rid,
                        "service_id": intent.service_id, "action": intent.action,
                        "observed_at": intent.observed_at,
                        "started_at": now.isoformat(),
                        "state": "pending",
                        "start_monotonic_before": _start_monotonic(before),
                        "boot_id": _boot_identity(self.adapter),
                    }
                    _store_new(journal_fd, "pending.json", entry)
                    _store_new(journal_fd, f"request-{rid}.json", entry)
                    argv = _command(intent.action, spec)
                    try:
                        ok = self.adapter.apply(argv)
                        if ok is not True:
                            raise BrokerRejected("systemd_effect_unverified")
                        # The guarded, synchronous start waits for the full
                        # oneshot and dependency chain. The timeout configured
                        # in SystemctlAdapter must exceed the reviewed unit
                        # runtime ceiling; an exceeded timeout remains
                        # indeterminate, never an assumed successful run.
                        after = self.adapter.observe(spec)
                        state = _verified_live(spec, after)
                        if _boot_identity(self.adapter) != entry["boot_id"]:
                            raise BrokerRejected("boot_changed_during_effect")
                        if intent.action == "pause" and state[0] != "paused":
                            raise BrokerRejected("pause_not_confirmed")
                        if intent.action == "resume" and state[0] != "scheduled":
                            raise BrokerRejected("resume_not_confirmed")
                        if intent.action == "run_once" and (
                            state[0] != "paused" or state[1] != "succeeded"
                            or _start_monotonic(after) <= entry["start_monotonic_before"]
                        ):
                            raise BrokerRejected("run_once_not_confirmed")
                    except Exception:
                        # Might have been applied before failure, including
                        # failed post-observation. Keep durable pending marker.
                        _store_new(journal_fd, f"result-{rid}.json", {
                            "record_version": 1, "request_id": rid,
                            "service_id": intent.service_id, "action": intent.action,
                            "state": "indeterminate",
                        })
                        raise BrokerRejected("effect_indeterminate") from None
                    _store_new(journal_fd, f"result-{rid}.json", {
                        "record_version": 1, "request_id": rid,
                        "service_id": intent.service_id, "action": intent.action,
                        "state": "completed",
                    })
                    # Completed outcome durable before pending removal.
                    os.unlink("pending.json", dir_fd=journal_fd)
                    os.fsync(journal_fd)
                    return BrokerResult(rid, intent.service_id, intent.action)
            finally:
                os.close(lock_fd)
        finally:
            os.close(journal_fd)



    def lookup_result(self, request_id: str) -> BrokerResult:
        """Recover a lost response using an immutable receipt; no systemd effect.

        A result persisted before a failed maintenance gate exit is still
        discoverable. If pending remains after a completed result, the result
        is distinguished from a completely settled audit transaction.
        """
        if self.require_root and os.geteuid() != 0:
            raise BrokerRejected("privileged_broker_required")
        if not isinstance(request_id, str):
            raise BrokerRejected("invalid_request_id")
        from .admin_operation_preflight import _REQUEST_ID
        if not _REQUEST_ID.fullmatch(request_id):
            raise BrokerRejected("invalid_request_id")
        journal_fd = _safe_open_root(self.audit_root, self.require_root)
        try:
            lock_fd = _locked(journal_fd)
            try:
                original = _read_private(journal_fd, f"request-{request_id}.json")
                result = _read_private(journal_fd, f"result-{request_id}.json")
                if (
                    set(original) != {
                        "record_version", "request_id", "service_id", "action",
                        "observed_at", "started_at", "state",
                        "start_monotonic_before", "boot_id",
                    }
                    or type(original["record_version"]) is not int
                    or original["record_version"] != 1
                    or original["request_id"] != request_id
                    or original["service_id"] not in {s.identifier for s in SERVICES}
                    or original["action"] not in ("pause", "resume", "run_once")
                    or original["state"] != "pending"
                    or type(original["start_monotonic_before"]) is not int
                    or not 0 <= original["start_monotonic_before"] < 10**20
                    or type(original["boot_id"]) is not str
                    or _BOOT_ID.fullmatch(original["boot_id"]) is None
                    or result != {
                        "record_version": 1, "request_id": request_id,
                        "service_id": original["service_id"],
                        "action": original["action"],
                        "state": result.get("state"),
                    }
                    or result["state"] not in ("completed", "indeterminate")
                ):
                    raise BrokerRejected("unverifiable_operation_receipt")
                pending = _present(journal_fd, "pending.json")
                if pending:
                    active = _read_private(journal_fd, "pending.json")
                    if active != original:
                        raise BrokerRejected("unverifiable_operation_receipt")
                if result["state"] == "completed" and not pending:
                    return BrokerResult(request_id, original["service_id"], original["action"])
                return BrokerResult(
                    request_id, original["service_id"], original["action"],
                    state=("completed_pending_cleanup" if result["state"] == "completed"
                           else "indeterminate"),
                    authority="manual_reconciliation_required",
                )
            finally:
                os.close(lock_fd)
        finally:
            os.close(journal_fd)


class SystemctlAdapter:
    """Explicit Linux local adapter; NOT wired into the WebUI or installer.

    Only fixed unit IDs from SERVICES, strict property lists and exactly three
    command patterns are supported. Never use shell, env inheritance or log
    stdout/stderr. Production use still requires separately qualified gates.
    """

    def __init__(self, runner=None, popen=None):
        import subprocess
        # Mutations use run with discarded outputs; observations use a
        # separate, bounded streaming reader, never subprocess.run/capture.
        self.runner = runner or subprocess.run
        self.popen = popen or subprocess.Popen

    @staticmethod
    def _clean_environment() -> dict[str, str]:
        return {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}

    def _bounded_show(self, argv: tuple[str, ...], *, timeout: float = 3.0):
        """Consume <=32768 bytes before accepting a systemctl observation.

        stderr is DEVNULL, not an accumulating pipe. On over-bound, timeout,
        decode error or other failure, terminate the *reader process* and
        fail closed. No subprocess stdout content enters errors or logs.
        """
        import subprocess
        proc = None
        try:
            proc = self.popen(
                ("/usr/bin/systemctl", *argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._clean_environment(),
                close_fds=True,
            )
            if proc.stdout is None:
                raise BrokerRejected("systemd_observation_failed")
            deadline = time.monotonic() + timeout
            chunks: list[bytes] = []
            total = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrokerRejected("systemd_observation_failed")
                readable, _, _ = select.select([proc.stdout.fileno()], [], [], remaining)
                if not readable:
                    raise BrokerRejected("systemd_observation_failed")
                data = os.read(proc.stdout.fileno(), min(4096, 32769 - total))
                if not data:
                    break
                total += len(data)
                if total > 32768:
                    raise BrokerRejected("systemd_observation_failed")
                chunks.append(data)
            rc = proc.wait(timeout=max(0.001, deadline - time.monotonic()))
            if rc != 0:
                raise BrokerRejected("systemd_observation_failed")
            try:
                return b"".join(chunks).decode("utf-8")
            except UnicodeError:
                raise BrokerRejected("systemd_observation_failed") from None
        except (OSError, ValueError, subprocess.SubprocessError):
            raise BrokerRejected("systemd_observation_failed") from None
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                if proc.stdout is not None:
                    proc.stdout.close()

    def _read_properties(self, unit: str, fields: tuple[str, ...]) -> dict[str, str]:
        if unit not in (
            {v for spec in SERVICES for v in (spec.timer, spec.service)}
            | UNIT_NAMES
        ):
            raise BrokerRejected("unknown_unit")
        import subprocess
        try:
            raw = self._bounded_show(
                ("show", unit, "--no-pager", *(f"--property={k}" for k in fields)),
                timeout=3.0,
            )
            props: dict[str, str] = {}
            for line in raw.splitlines():
                key, sep, val = line.partition("=")
                if not sep or key not in fields or key in props or len(val) > 256:
                    raise BrokerRejected("systemd_observation_failed")
                props[key] = val
            if set(props) != set(fields):
                raise BrokerRejected("systemd_observation_failed")
            return props
        except (subprocess.SubprocessError, OSError, UnicodeError):
            raise BrokerRejected("systemd_observation_failed") from None

    def observe(self, spec: ServiceSpec) -> LiveUnits:
        if spec not in SERVICES:
            raise BrokerRejected("unknown_service")
        return LiveUnits(
            timer=self._read_properties(spec.timer, TIMER_READ_FIELDS),
            service=self._read_properties(spec.service, SERVICE_READ_FIELDS),
        )

    def observe_workflow(self, spec: ServiceSpec) -> WorkflowObservation:
        """Read only the complete frozen role inventory, never arbitrary units."""
        if spec not in SERVICES:
            raise BrokerRejected("unknown_service")
        timers = {
            timer: self._read_properties(timer, WORKFLOW_TIMER_READ_FIELDS)
            for timer in TIMER_TARGETS
            if timer in (
                (SERVICES[0].timer, SERVICES[1].timer)
                if spec.identifier in ("ai-vault-pull", "pre-review")
                else (SERVICES[2].timer, SERVICES[3].timer)
                if spec.identifier in ("github-sync", "github-daily")
                else (SERVICES[4].timer,)
            )
        }
        services = {
            name: self._read_properties(name, WORKFLOW_READ_FIELDS)
            for name in GROUP_UNITS[spec.identifier]
        }
        return WorkflowObservation(timers=timers, services=services)

    def boot_id(self) -> str:
        handle = None
        try:
            handle = os.open(
                "/proc/sys/kernel/random/boot_id",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            raw = os.read(handle, 64)
            boot = raw.decode("ascii").strip()
            if _BOOT_ID.fullmatch(boot) is None:
                raise BrokerRejected("boot_identity_unavailable")
            return boot
        except (OSError, UnicodeError):
            raise BrokerRejected("boot_identity_unavailable") from None
        finally:
            if handle is not None:
                os.close(handle)

    def apply(self, argv: tuple[str, ...]) -> bool:
        import subprocess
        commands = {
            _command(a, s) for a in ("pause", "resume", "run_once") for s in SERVICES
        }
        if type(argv) is not tuple or argv not in commands:
            raise BrokerRejected("command_not_allowlisted")
        try:
            # A full oneshot may have 10–15 min dependencies. Start is
            # synchronous while the broker gate is held. Future HTTP wiring
            # must delegate to a dedicated broker worker, not block the web
            # event loop. A two-hour ceiling still fails closed on overruns.
            timeout = 7200 if argv[1] == "start" else 15
            result = self.runner(
                ("/usr/bin/systemctl", *argv[1:]),
                check=False, timeout=timeout, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=self._clean_environment(),
            )
            return type(result.returncode) is int and result.returncode == 0
        except (subprocess.SubprocessError, OSError, UnicodeError):
            return False
