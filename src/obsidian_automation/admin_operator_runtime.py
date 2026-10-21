"""#300 privileged operator runtime: fixed local Unix intake and one worker.

The production constructor remains OFF until a root-owned, exact installed
revision/units authority is verified. HTTP never runs as root, never imports a
UnitAdapter, and cannot choose the controlled unit, executable or journal
path. This module has no network endpoints and no generic shell execution.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import signal
import stat
import sys
import threading
import time
from typing import Callable, Iterator, Mapping, Sequence

from .admin_control_bridge import run_accepted_control_once
from .admin_control_queue_socket import (
    OperationQueueSocket, DEFAULT_OPERATION_QUEUE_SOCKET,
)
from .admin_local_maintenance_gate import cooperating_updater_gate
from .admin_operation_broker import (
    BrokerRejected, OperationBroker, SystemctlAdapter,
)
from .admin_operation_queue import DurableOperationQueue
from .admin_preview_daemon import _production_observation
from .admin_status import SERVICES
from .admin_systemd_admission import (
    BusctlStartUnitManager, RunOnceJobDispatcher,
)

DEFAULT_AUTHORITY_PATH = Path("/etc/obsidian-admin/authority.json")
DEFAULT_QUEUE_PATH = Path("/var/lib/obsidian-admin/queue")
DEFAULT_BROKER_AUDIT_PATH = Path("/var/lib/obsidian-admin/broker")
DEFAULT_WEB_USER = "obsidian-admin-web"
_UNITS = frozenset(unit for spec in SERVICES for unit in (spec.timer, spec.service))
_SHA = frozenset("0123456789abcdef")
MAX_AUTHORITY_BYTES = 6500
_BOOTSTRAP_FD_ENV = "OBSIDIAN_ADMIN_BOOTSTRAP_FD"
_BOOTSTRAP_MAGIC = b"OBSIDIAN_ADMIN_VERIFIED_BOOTSTRAP_V1\x00"
_BOOTSTRAP_SEALS = (
    fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK
    | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
)
_AUTHORITY_MODULES = (
    "admin_operator_runtime.py", "admin_operation_broker.py",
    "admin_operation_queue.py", "admin_control_queue_socket.py",
    "admin_control_bridge.py", "admin_systemd_admission.py",
    "admin_local_maintenance_gate.py", "admin_preview_daemon.py",
    "admin_operation_preflight.py", "admin_workflow_quiescence.py",
    # These are part of the transitive privileged import graph. A source
    # hash check that omits them is not an exact-code admission.
    "admin_broker_socket.py", "admin_operation_preview.py",
    "admin_status.py", "__init__.py",
)


def _pinned_root_regular(path: Path, *, max_bytes: int) -> bytes:
    """Open every trusted path component without following symlinks."""
    if type(path) is not Path or not path.is_absolute() or ".." in path.parts:
        raise BrokerRejected("operator_authority_unavailable")
    fd = None
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for part in path.parts[1:-1]:
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY |
                os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd,
            )
            info = os.fstat(next_fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise BrokerRejected("operator_authority_unavailable")
            os.close(fd)
            fd = next_fd
        leaf = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
            os.O_NONBLOCK, dir_fd=fd,
        )
        try:
            info = os.fstat(leaf)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o022
                or not 1 <= info.st_size <= max_bytes
            ):
                raise BrokerRejected("operator_authority_unavailable")
            data = os.read(leaf, max_bytes + 1)
            if len(data) != info.st_size:
                raise BrokerRejected("operator_authority_unavailable")
            return data
        finally:
            os.close(leaf)
    except (OSError, ValueError):
        raise BrokerRejected("operator_authority_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


class OperatorAuthority:
    """Root-owned exact-revision + immutable installed-unit admission gate."""

    def __init__(
        self,
        config_path: Path = DEFAULT_AUTHORITY_PATH,
        *,
        systemd_dir: Path = Path("/etc/systemd/system"),
        module_dir: Path | None = None,
    ):
        self.config_path = config_path
        self.systemd_dir = systemd_dir
        self.module_dir = module_dir if module_dir is not None else Path(__file__).absolute().parent

    def verify(self) -> dict[str, object]:
        if os.geteuid() != 0:
            raise BrokerRejected("operator_root_required")
        try:
            def unique(pairs):
                obj = {}
                for k,v in pairs:
                    if k in obj:
                        raise ValueError("duplicate")
                    obj[k]=v
                return obj
            raw = json.loads(
                _pinned_root_regular(
                    self.config_path, max_bytes=MAX_AUTHORITY_BYTES,
                ).decode("utf-8"),
                object_pairs_hook=unique,
            )
            if type(raw) is not dict or set(raw) != {
                "version", "enabled", "revision", "web_user",
                "single_active_lxc", "unit_sha256", "module_sha256",
                "coordinated_updaters_verified",
                "uncoordinated_launchers_retired",
            }:
                raise ValueError("invalid authority")
            if (
                type(raw["version"]) is not int or raw["version"] != 1
                or raw["enabled"] is not True
                or raw["single_active_lxc"] is not True
                # External state cannot be inferred from lock acquisition:
                # require separately operator-reviewed, root-owned cutover
                # attestations. The staging helper writes both as FALSE.
                or raw["coordinated_updaters_verified"] is not True
                or raw["uncoordinated_launchers_retired"] is not True
                or raw["web_user"] != DEFAULT_WEB_USER
                or type(raw["revision"]) is not str
                or len(raw["revision"]) != 40
                or any(c not in _SHA for c in raw["revision"])
                or type(raw["unit_sha256"]) is not dict
                or set(raw["unit_sha256"]) != _UNITS
                or type(raw["module_sha256"]) is not dict
                or set(raw["module_sha256"]) != set(_AUTHORITY_MODULES)
            ):
                raise ValueError("not qualified")
            for name, digest in raw["unit_sha256"].items():
                if (
                    type(digest) is not str or len(digest) != 64
                    or any(c not in _SHA for c in digest)
                ):
                    raise ValueError("wrong unit hash")
                observed = _pinned_root_regular(
                    self.systemd_dir / name, max_bytes=16384,
                )
                if hashlib.sha256(observed).hexdigest() != digest:
                    raise ValueError("unit revision drift")
            # Same approved revision must pin the actual critical Python
            # package bytes, not just labels in a human-written config.
            # Names are source-controlled, not supplied in the manifest.
            for name in _AUTHORITY_MODULES:
                digest = raw["module_sha256"][name]
                if (
                    type(digest) is not str or len(digest) != 64
                    or any(c not in _SHA for c in digest)
                ):
                    raise ValueError("bad code digest")
                observed = _pinned_root_regular(
                    self.module_dir / name, max_bytes=2 * 1024 * 1024,
                )
                if hashlib.sha256(observed).hexdigest() != digest:
                    raise ValueError("installed code drift")
            return raw
        except (OSError, ValueError, UnicodeError, TypeError, BrokerRejected):
            raise BrokerRejected("operator_authority_unavailable") from None


def _require_verified_bootstrap() -> None:
    """Fail closed on direct root module/console execution.

    Verifies an inherited sealed memfd bound to the root authority bytes.
    The production systemd ExecStart calls the trusted stdlib bootstrap
    exclusively; a naked environment marker is not sufficient to enable
    the application runtime.
    """
    if (
        os.geteuid()!=0 or sys.flags.isolated!=1
        or sys.flags.no_site!=1
        or sys.flags.dont_write_bytecode!=1
        or getattr(sys,"pycache_prefix",None)!="/dev/null/obsidian-admin-source-only"
    ):
        # A Python -I process still executes site-packages .pth hooks.
        # Only -I -S with an explicit, pre-hashed import root is accepted.
        raise BrokerRejected("operator_trusted_bootstrap_required")
    value=os.environ.pop(_BOOTSTRAP_FD_ENV,None)
    if (
        type(value) is not str or not value.isascii()
        or not value.isdecimal() or not 1<=len(value)<=5
    ):
        raise BrokerRejected("operator_trusted_bootstrap_required")
    fd=int(value)
    if fd<=2 or fd>65535:
        raise BrokerRejected("operator_trusted_bootstrap_required")
    try:
        info=os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid!=0
            or info.st_size!=len(_BOOTSTRAP_MAGIC)+32
            or (
                fcntl.fcntl(fd,fcntl.F_GET_SEALS) & _BOOTSTRAP_SEALS
            )!=_BOOTSTRAP_SEALS
        ):
            raise BrokerRejected("operator_trusted_bootstrap_required")
        os.lseek(fd,0,os.SEEK_SET)
        value=os.read(fd,len(_BOOTSTRAP_MAGIC)+33)
        expected=hashlib.sha256(_pinned_root_regular(
            DEFAULT_AUTHORITY_PATH,max_bytes=MAX_AUTHORITY_BYTES,
        )).digest()
        if (
            len(value)!=len(_BOOTSTRAP_MAGIC)+32
            or not value.startswith(_BOOTSTRAP_MAGIC)
            or value[len(_BOOTSTRAP_MAGIC):]!=expected
        ):
            raise BrokerRejected("operator_trusted_bootstrap_required")
    except (OSError, ValueError, BrokerRejected):
        raise BrokerRejected("operator_trusted_bootstrap_required") from None
    finally:
        try: os.close(fd)
        except OSError: pass


class OperatorRuntime:
    """Run the private socket listener and bounded, single-worker dispatcher.

    The socket only enqueues/reads. The executor runs in a separate thread and
    claims the queue's durable processing record before each systemd operation.
    Process crash/restart NEVER auto-replays a previously claimed intent.
    """

    def __init__(
        self, *,
        queue: DurableOperationQueue,
        broker: OperationBroker,
        run_once_dispatcher: RunOnceJobDispatcher,
        observation_provider: Callable[[], Mapping[str, object]],
        socket_path: Path,
        allowed_peer_uid: int,
        allowed_peer_gid: int,
        ready: bool = False,
        require_root: bool = True,
        stop: threading.Event | None = None,
        selected_observation: Callable[[str], Mapping[str, object]] | None = None,
    ):
        self.queue = queue
        self.broker = broker
        self.run_once_dispatcher = run_once_dispatcher
        self.observation_provider = observation_provider
        self.socket_path = socket_path
        self.allowed_peer_uid = allowed_peer_uid
        self.allowed_peer_gid = allowed_peer_gid
        self.ready = ready
        self.require_root = require_root
        self.stop = stop if stop is not None else threading.Event()
        self.selected_observation = selected_observation
        self._worker_failure: str | None = None
        self._worker_thread: threading.Thread | None = None

    def _available(self) -> bool:
        worker = self._worker_thread
        return (
            self.ready is True and not self.stop.is_set()
            and self._worker_failure is None
            and worker is not None and worker.is_alive()
        )

    def verify_ready(self) -> None:
        if (
            self.ready is not True
            or self.queue.enabled is not True
            or self.queue.allow_run_once is not True
            or self.broker.effects_enabled is not True
            or self.run_once_dispatcher.enabled is not True
            or (self.require_root and os.geteuid() != 0)
            or type(self.allowed_peer_uid) is not int
            or self.allowed_peer_uid <= 0
            or type(self.allowed_peer_gid) is not int
            or self.allowed_peer_gid <= 0
        ):
            raise BrokerRejected("operator_runtime_not_qualified")

    def process_pending_once(self) -> bool:
        """Single one-shot claim; never retry an uncertain operation."""
        try:
            run_accepted_control_once(
                queue=self.queue, broker=self.broker,
                trusted_snapshot=self.observation_provider,
                run_once_dispatcher=self.run_once_dispatcher,
            )
        except BrokerRejected as exc:
            if exc.code in ("queue_missing_receipt", "admission_lock_busy"):
                return False
            if exc.code in (
                "queue_recovery_required", "indeterminate_effect",
                "queue_state_invalid", "queue_busy",
            ):
                # Keep durable evidence and allow read-only receipt queries.
                # No automatic replay/reconciliation is attempted.
                self._worker_failure = exc.code
                return False
            self._worker_failure = "operator_worker_unavailable"
            return False
        except Exception:
            self._worker_failure = "operator_worker_unavailable"
            return False
        self._worker_failure = None
        return True

    def _worker(self) -> None:
        while not self.stop.is_set():
            self.process_pending_once()
            self.stop.wait(0.25)

    def serve(self) -> None:
        self.verify_ready()
        with OperationQueueSocket(
            self.socket_path,
            queue=self.queue,
            allowed_peer_uid=self.allowed_peer_uid,
            allowed_peer_gid=self.allowed_peer_gid,
            socket_gid=self.allowed_peer_gid,
            observation_provider=self.observation_provider,
            # The root Socket must immediately withdraw capabilities after
            # an indeterminate Worker outcome or a crashed/stopped thread.
            ready=self._available,
            selected_observation=self.selected_observation,
            enforce_worker_readiness=True,
            require_root=self.require_root,
        ) as listener:
            worker = threading.Thread(
                target=self._worker, name="obsidian-admin-operator-worker",
                daemon=False,  # Do not abandon a live effect on SIGTERM.
            )
            self._worker_thread = worker
            worker.start()
            try:
                while not self.stop.is_set():
                    listener.serve_once()
            finally:
                self.stop.set()
                # Wait for any currently running bounded systemd operation to
                # durably settle rather than abruptly kill an in-flight
                # effect. The service's TimeoutStopSec provides a host-level
                # maximum; interrupted effects retain indeterminate receipts.
                worker.join()
                self._worker_thread = None


def make_live_runtime(
    *, authority_path: Path = DEFAULT_AUTHORITY_PATH,
) -> OperatorRuntime:
    """Production builder never allows activation with an absent authority."""
    policy = OperatorAuthority(authority_path)
    checked = policy.verify()
    try:
        principal = pwd.getpwnam(DEFAULT_WEB_USER)
    except (KeyError, ValueError):
        raise BrokerRejected("operator_web_identity_missing") from None
    if principal.pw_uid <= 0 or principal.pw_gid <= 0:
        raise BrokerRejected("operator_web_identity_missing")

    adapter = SystemctlAdapter()

    @contextmanager
    def gate() -> Iterator[None]:
        # This excludes only participating *installed* update paths.
        # Other unsupported root launchers require the production cutover
        # inventory gate; a positive lock alone is NOT global authority.
        with cooperating_updater_gate():
            policy.verify()  # exact installed SHA/unit bytes under the lock
            yield

    clock = lambda: datetime.now(timezone.utc)
    # Fail before socket publication if mandatory cooperating updater locks
    # or pending update evidence are absent/unsafe. This does NOT qualify
    # legacy uncoordinated binaries; the cutover review must decommission them.
    with gate():
        pass

    queue = DurableOperationQueue(
        queue_root=DEFAULT_QUEUE_PATH, enabled=True, allow_run_once=True,
    )
    broker = OperationBroker(
        audit_root=DEFAULT_BROKER_AUDIT_PATH,
        adapter=adapter, maintenance_gate=gate, clock=clock,
        effects_enabled=True,
    )
    dispatcher = RunOnceJobDispatcher(
        adapter=adapter, manager=BusctlStartUnitManager(),
        maintenance_gate=gate, clock=clock, enabled=True,
        workflow_observer=adapter.observe_workflow,
    )
    return OperatorRuntime(
        queue=queue, broker=broker, run_once_dispatcher=dispatcher,
        observation_provider=_production_observation,
        selected_observation=lambda sid: _production_observation(sid),
        socket_path=DEFAULT_OPERATION_QUEUE_SOCKET,
        allowed_peer_uid=principal.pw_uid, allowed_peer_gid=principal.pw_gid,
        ready=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Approved fixed-systemd operator")
    # Authority source is always a host-installed root document at one fixed
    # path. No CLI flag lets an operator bypass code/unit validation.
    parser.parse_args(list(argv) if argv is not None else None)
    # Only the fixed root-owned stdlib bootstrap can normally mint the
    # sealed, root-owned authority digest descriptor. Root itself is trusted.
    try:
        _require_verified_bootstrap()
    except BrokerRejected:
        raise SystemExit("operator_trusted_bootstrap_required") from None
    os.umask(0o077)
    runtime = make_live_runtime()
    def stop(_signum, _frame):
        runtime.stop.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    runtime.serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
