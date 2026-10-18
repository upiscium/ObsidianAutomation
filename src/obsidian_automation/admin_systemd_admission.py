"""#300 minimal one-shot systemd admission; deliberately not installed or wired.

A fixed allowlisted StartUnit request is sent directly to systemd's Manager
with job mode 'fail'. The returned Job object is evidence of *admission*, not
of completion of the service, OnSuccess chain or external business effects.

A durable Queue claims the operator UUID BEFORE invoking this module. A
separately qualified local updater/unit-exclusion gate must be supplied and
held across the state check and submission; no default/no-op gate is provided.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import re
import select
import subprocess
import time
from typing import Callable, Mapping, Protocol

from .admin_operation_broker import (
    BrokerRejected, UnitAdapter, _boot_identity, _utc_clock, _verified_live,
)
from .admin_operation_preflight import PreflightRejected, preflight_operation, _REQUEST_ID
from .admin_status import SERVICES, ServiceSpec
from .admin_workflow_quiescence import (
    WorkflowObservation, WorkflowNotQuiescent, require_run_once_nonoverlap,
)

_JOB_PATH = re.compile(r"/org/freedesktop/systemd1/job/([1-9][0-9]{0,18})\Z")
_BUSCTL_OBJECT_LINE = re.compile(r'o "(/org/freedesktop/systemd1/job/[1-9][0-9]{0,18})"\n?\Z')
_BOOT_ID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z")
MAX_ADMISSION_REPLY_BYTES = 512
MAX_ADMISSION_SECONDS = 5.0


@dataclass(frozen=True)
class SystemdJobAdmission:
    """Precisely one Job ID returned by the systemd Manager, not job success."""
    request_id: str
    service_id: str
    job_path: str
    boot_id: str


def verify_job_admission(value: object, *, request_id: str, service_id: str) -> SystemdJobAdmission:
    if (
        type(value) is not SystemdJobAdmission
        or type(value.request_id) is not str or value.request_id != request_id
        or _REQUEST_ID.fullmatch(value.request_id) is None
        or type(value.service_id) is not str or value.service_id != service_id
        or value.service_id not in {spec.identifier for spec in SERVICES}
        or type(value.job_path) is not str or _JOB_PATH.fullmatch(value.job_path) is None
        or type(value.boot_id) is not str or _BOOT_ID.fullmatch(value.boot_id) is None
    ):
        raise BrokerRejected("systemd_admission_unverifiable")
    return value


class SystemdJobManager(Protocol):
    def start_unit(self, spec: ServiceSpec) -> str: ...


class BusctlStartUnitManager:
    """Bounded, fixed-command D-Bus StartUnit adapter; no shell/log parsing.

    Production path is an immutable installed system busctl binary. For tests
    an isolated Popen factory is injected, not a caller-supplied unit/path.
    """

    def __init__(
        self, *, popen: Callable = subprocess.Popen,
        executable: str = "/usr/bin/busctl",
    ):
        if executable != "/usr/bin/busctl":
            # Test-only alternate binary path must not be silently used by
            # a production privileged process.
            raise BrokerRejected("systemd_manager_binary_unqualified")
        self.popen = popen

    def start_unit(self, spec: ServiceSpec) -> str:
        if spec not in SERVICES:
            raise BrokerRejected("unknown_service")
        argv = (
            "/usr/bin/busctl", "--system", "call",
            "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", "StartUnit",
            "ss", spec.service, "fail",
        )
        proc = None
        try:
            proc = self.popen(
                argv,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, close_fds=True,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
            if proc.stdout is None:
                raise BrokerRejected("systemd_admission_unconfirmed")
            deadline = time.monotonic() + MAX_ADMISSION_SECONDS
            contents = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrokerRejected("systemd_admission_unconfirmed")
                readable, _, _ = select.select([proc.stdout.fileno()], [], [], remaining)
                if not readable:
                    raise BrokerRejected("systemd_admission_unconfirmed")
                part = os.read(
                    proc.stdout.fileno(), min(512, MAX_ADMISSION_REPLY_BYTES + 1 - len(contents)),
                )
                if not part:
                    break
                contents.extend(part)
                if len(contents) > MAX_ADMISSION_REPLY_BYTES:
                    raise BrokerRejected("systemd_admission_unconfirmed")
            if proc.wait(timeout=max(.001, deadline - time.monotonic())) != 0:
                raise BrokerRejected("systemd_admission_unconfirmed")
            try:
                response = contents.decode("ascii")
            except UnicodeError:
                raise BrokerRejected("systemd_admission_unconfirmed") from None
            match = _BUSCTL_OBJECT_LINE.fullmatch(response)
            if match is None:
                raise BrokerRejected("systemd_admission_unconfirmed")
            return match.group(1)
        except (OSError, subprocess.SubprocessError, ValueError):
            raise BrokerRejected("systemd_admission_unconfirmed") from None
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                if proc.stdout is not None:
                    proc.stdout.close()


class RunOnceJobDispatcher:
    """A separate, opt-in systemd admission boundary with no business retries.

    Unlike the old synchronous Broker.run_once contract, this function neither
    waits for nor asserts that a service or its successors succeeded. The
    caller's durable queue journal is the operator request/ack identity.
    """

    def __init__(
        self, *,
        adapter: UnitAdapter,
        manager: SystemdJobManager,
        maintenance_gate: Callable[[], AbstractContextManager[None]],
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        require_root: bool = True,
        enabled: bool = False,
        workflow_observer: Callable[[ServiceSpec], WorkflowObservation] | None = None,
    ):
        self.workflow_observer = workflow_observer
        self.adapter = adapter
        self.manager = manager
        self.maintenance_gate = maintenance_gate
        self.clock = clock
        self.require_root = require_root
        self.enabled = enabled

    def dispatch(
        self, *, request_id: str, service_id: str,
        trusted_snapshot: Mapping[str, object],
    ) -> SystemdJobAdmission:
        if self.enabled is not True:
            raise BrokerRejected("systemd_dispatch_disabled")
        if self.require_root and os.geteuid() != 0:
            raise BrokerRejected("privileged_dispatch_required")
        spec = next((s for s in SERVICES if s.identifier == service_id), None)
        if spec is None or type(request_id) is not str or _REQUEST_ID.fullmatch(request_id) is None:
            raise BrokerRejected("invalid_dispatch_request")
        if type(trusted_snapshot) is not dict or type(trusted_snapshot.get("generated_at")) is not str:
            raise BrokerRejected("trusted_snapshot_unavailable")
        request = {
            "version": 1, "request_id": request_id, "service_id": service_id,
            "action": "run_once", "observed_at": trusted_snapshot["generated_at"],
        }
        # All state verification is performed under a separately qualified
        # updater/launcher gate; there is no optimistic no-lock variant.
        try:
            with self.maintenance_gate():
                live = self.adapter.observe(spec)
                state = _verified_live(spec, live)
                now = _utc_clock(self.clock)
                preflight_operation(request, trusted_snapshot, now=now, deployment_quiescent=True)
                listed = [
                    value for value in trusted_snapshot["services"]
                    if type(value) is dict and value.get("id") == service_id
                ]
                if (
                    len(listed) != 1
                    or state != (listed[0]["schedule"], listed[0]["latest_execution"], listed[0]["health"])
                    or state[0] != "paused"
                    or state[1] == "failed"
                ):
                    raise BrokerRejected("systemd_run_once_state_mismatch")
                # The root-owned production path MUST inspect actual hidden
                # prerequisite/OnSuccess services before dispatch. An enabled
                # source-only unit test with require_root=False may explicitly
                # use the fake adapter without a workflow observer.
                if self.workflow_observer is None:
                    if self.require_root:
                        raise BrokerRejected("workflow_observer_unavailable")
                else:
                    try:
                        observed = self.workflow_observer(spec)
                        require_run_once_nonoverlap(spec, observed)
                    except WorkflowNotQuiescent as exc:
                        raise BrokerRejected(exc.code) from None
                    except BrokerRejected:
                        raise
                    except Exception:
                        raise BrokerRejected("workflow_observation_failed") from None
                boot = _boot_identity(self.adapter)
                # Manager mode='fail' refuses transactions that would replace
                # queued conflicting jobs. Systemd serializes its own unit Job
                # creation; no journalctl-tail read participates in admission.
                path = self.manager.start_unit(spec)
                if type(path) is not str or _JOB_PATH.fullmatch(path) is None:
                    raise BrokerRejected("systemd_admission_unconfirmed")
                if _boot_identity(self.adapter) != boot:
                    raise BrokerRejected("systemd_admission_unconfirmed")
                return verify_job_admission(
                    SystemdJobAdmission(request_id, service_id, path, boot),
                    request_id=request_id, service_id=service_id,
                )
        except PreflightRejected as exc:
            raise BrokerRejected(exc.reason) from None
        except BrokerRejected:
            raise
        except Exception:
            raise BrokerRejected("systemd_admission_unconfirmed") from None
