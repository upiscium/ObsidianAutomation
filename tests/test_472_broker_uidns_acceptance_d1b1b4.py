"""#472 D1/B1/B4 — genuine kernel Owner/Broker UID/GID acceptance in a disposable user namespace.

Never run against host root or a production service. This opt-in test requires
an unprivileged user namespace mapping the caller to namespace UID 0 and a
subordinate UID/GID range, plus a private mount namespace. It creates a
temporary chroot/mount view, then forks real distinct UID/GID processes.
No mocked SO_PEERCRED, useradd, sudo, host ACL changes, Provider I/O,
Vault access, daemon installation or source publication.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import socket
import stat
import subprocess
import tempfile
import time
from typing import Callable

import pytest

from obsidian_automation.vault_format_broker_journal import BrokerPrivateJournal
from obsidian_automation.vault_format_broker_readonly_listener import (
    BrokerUnixReadOnlyListener,
)
from obsidian_automation.vault_format_broker_readonly_session import (
    BrokerReadOnlySession,
)
from obsidian_automation.vault_format_broker_observation import (
    OwnerBrokerObservationClient,
)
from test_472_broker_readonly_session_d1b1b2 import (
    ACCOUNT, GEN, OTHER_ACCOUNT, attempt, credential_request,
)

BROKER_UID = 21001
OWNER_UID = 21002
STRANGER_UID = 21003
IPC_GID = 21004
OTHER_GID = 21005
SOCKET_PATH = Path("/tmp/oa472-b4-ipc/broker.sock")
DB_PATH = Path("/tmp/oa472-b4-db")
_ATTEMPT_PENDING = attempt(run="a", op="b", target="98-System/pending.md")
_ATTEMPT_TERMINAL = attempt(run="c", op="d", target="98-System/terminal.md")
_ROLE_TIMEOUT = 10.0


def _sandbox_proof() -> bool:
    """Never turn a host-root test into a privileged host mutation."""
    if (os.environ.get("OA472_DISPOSABLE_UIDNS") != "1"
            or os.geteuid() != 0 or os.getpid() != 1):
        return False
    try:
        uid_lines = [
            tuple(map(int, row.split())) for row in
            Path("/proc/self/uid_map").read_text().splitlines()
        ]
        gid_lines = [
            tuple(map(int, row.split())) for row in
            Path("/proc/self/gid_map").read_text().splitlines()
        ]
    except (OSError, ValueError):
        return False
    # namespace root must map to a NONROOT host user and additional roles
    # must map into a separate subordinate range, not real system accounts.
    return (
        any(a == 0 and b > 0 and c == 1 for a, b, c in uid_lines)
        and any(a == 0 and b > 0 and c == 1 for a, b, c in gid_lines)
        and any(a <= BROKER_UID and a + c > OTHER_GID
                and b >= 65536 for a, b, c in uid_lines)
        and any(a <= BROKER_UID and a + c > OTHER_GID
                and b >= 65536 for a, b, c in gid_lines)
    )


def _drop_to(uid: int, gid: int, *, supplementary: tuple[int, ...] = ()) -> None:
    # This changes credentials of the calling FORK CHILD only, in a userns.
    os.setgroups(list(supplementary))
    os.setresgid(gid, gid, gid)
    os.setresuid(uid, uid, uid)
    assert os.geteuid() == uid and os.getegid() == gid


def _read(sock: socket.socket) -> bytes:
    sock.settimeout(_ROLE_TIMEOUT)
    part = sock.recv(1)
    if not part:
        raise AssertionError("disposable Broker control connection closed")
    if part == b"E":
        raw = bytearray()
        while len(raw) < 256:
            byte = sock.recv(1)
            if not byte or byte == b"\n":
                break
            raw.extend(byte)
        raise AssertionError("Broker synthetic failure: " + raw.decode("ascii", "replace"))
    return part


def _server(control: socket.socket) -> None:
    """One bounded explicit accept per synthetic command; no daemon loop."""
    _drop_to(BROKER_UID, IPC_GID)
    journal = BrokerPrivateJournal(DB_PATH, bootstrap=True)
    assert journal.start_boot() == 1
    assert journal.reconcile_boot() is True
    journal.provision_generation_metadata(ACCOUNT, GEN)
    journal.fence_pre_dispatch(_ATTEMPT_PENDING)
    journal.fence_pre_dispatch(_ATTEMPT_TERMINAL)
    journal.cancel_never_dispatched(_ATTEMPT_TERMINAL)
    handler = BrokerReadOnlySession(
        journal, owner_uid=OWNER_UID, owner_gid=IPC_GID,
    )
    listener = BrokerUnixReadOnlyListener(
        handler, SOCKET_PATH, ipc_group_gid=IPC_GID,
    )
    with listener:
        st = SOCKET_PATH.lstat()
        assert stat.S_ISSOCK(st.st_mode)
        assert (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (
            BROKER_UID, IPC_GID, 0o660,
        )
        control.sendall(b"R")  # fully initialized and listening
        while True:
            request = _read(control)
            if request == b"X":
                break
            if request != b"S":
                raise AssertionError("unsupported synthetic test command")
            served = False
            for _ in range(30):
                if listener.serve_once():
                    served = True
                    break
            control.sendall(b"Y" if served else b"N")


def _raw_request() -> bytes:
    return json.dumps(credential_request(), separators=(",", ":")).encode("ascii") + b"\n"


def _raw_exchange() -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(3.0)
        conn.connect(str(SOCKET_PATH))
        try:
            conn.sendall(_raw_request())
            conn.shutdown(socket.SHUT_WR)
        except (BrokenPipeError, ConnectionResetError):
            # A rejected SO_PEERCRED peer may be closed by Broker before
            # sendall/shutdown completes. This is a denial, not proof of
            # delivered JSON. Still insist no response bytes are received.
            pass
        parts = []
        while True:
            try:
                part = conn.recv(1024)
            except ConnectionResetError:
                break  # denied before reading the unauthenticated payload
            if not part:
                break
            parts.append(part)
        return b"".join(parts)


def _client_result(kind: str) -> dict:
    if kind in ("credential", "pending", "terminal", "unknown"):
        client = OwnerBrokerObservationClient(
            SOCKET_PATH, broker_uid=BROKER_UID, ipc_group_gid=IPC_GID,
        )
        if kind == "credential":
            obs = client.observe_credential(ACCOUNT)
            assert obs.account_binding_sha256 == ACCOUNT
            assert obs.credential_generation == GEN
            assert obs.credential_epoch == 0
            assert len(obs.broker_ledger_revision) == 64
            return {"credential_valid": True}
        if kind == "pending":
            obs = client.observe_finality(_ATTEMPT_PENDING)
            assert obs.disposition == "pending"
            assert obs.broker_journal_seq is None and obs.receipt_sha256 is None
            return {"pending": True}
        if kind == "terminal":
            obs = client.observe_finality(_ATTEMPT_TERMINAL)
            assert obs.disposition == "verified_no_effect"
            assert isinstance(obs.broker_journal_seq, int)
            assert obs.broker_journal_seq > 0
            assert obs.receipt_sha256 is not None
            assert len(obs.receipt_sha256) == 64
            return {"broker_local_no_effect": True}
        from obsidian_automation.vault_format_broker_observation import (
            BrokerObservationDenied,
        )
        try:
            client.observe_credential(OTHER_ACCOUNT)
        except BrokerObservationDenied as err:
            assert str(err) == "broker_observation_unavailable"
            return {"unknown_denied": True}
        raise AssertionError("unregistered account unexpectedly observed")
    response = _raw_exchange()
    assert response == b"", "unauthorized Unix peer received Broker bytes"
    return {"unauthorized_denied": True}


def _spawn_client(kind: str, uid: int, gid: int,
                  *, supplementary: tuple[int, ...] = ()) -> tuple[int, int]:
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        outcome = {}
        code = 0
        try:
            _drop_to(uid, gid, supplementary=supplementary)
            outcome = _client_result(kind)
        except BaseException as error:
            outcome = {"error": type(error).__name__, "reason": str(error)[:120]}
            code = 1
        try:
            os.write(wfd, json.dumps(outcome, sort_keys=True).encode("ascii"))
        finally:
            os.close(wfd)
        os._exit(code)
    os.close(wfd)
    return pid, rfd


def _wait_client(pid: int, fd: int) -> dict:
    try:
        readable, _, _ = select.select([fd], [], [], _ROLE_TIMEOUT)
        if not readable:
            raise AssertionError("disposable client timed out")
        raw = os.read(fd, 8192)
        assert raw
        result = json.loads(raw)
        waited, status = os.waitpid(pid, 0)
        assert waited == pid
        assert os.waitstatus_to_exitcode(status) == 0, result
        return result
    finally:
        os.close(fd)


def _request(control: socket.socket, kind: str,
             uid: int, gid: int,
             *, supplementary: tuple[int, ...] = ()) -> dict:
    pid, fd = _spawn_client(kind, uid, gid, supplementary=supplementary)
    try:
        try:
            control.sendall(b"S")
        except BrokenPipeError:
            _read(control)  # propagate a queued structured Broker error
            raise AssertionError("Broker exited before a request: " + kind) from None
        result = _wait_client(pid, fd)
        assert _read(control) == b"Y", "Broker failed to accept: " + kind
        return result
    finally:
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            waited = pid
        if waited == 0:
            os.kill(pid, 9)
            os.waitpid(pid, 0)


def _exercise_chroot() -> dict:
    control, broker_control = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    phase = "spawn"
    pid = os.fork()
    if pid == 0:
        control.close()
        try:
            _server(broker_control)
        except BaseException as error:
            # Only fixed synthetic exception class/reason is reported;
            # never echo real Provider state or credentials.
            msg = (type(error).__name__ + ": " + str(error)[:180]).encode(
                "ascii", "replace",
            )
            try:
                broker_control.sendall(b"E" + msg + b"\n")
            except OSError:
                pass
            os._exit(3)
        finally:
            broker_control.close()
        os._exit(0)
    broker_control.close()
    try:
        assert _read(control) == b"R", "Broker did not start"
        summary = {}
        phase = "credential"
        summary["credential"] = _request(
            control, "credential", OWNER_UID, IPC_GID,
        )
        phase = "pending"
        summary["pending"] = _request(
            control, "pending", OWNER_UID, IPC_GID,
        )
        phase = "terminal"
        summary["terminal"] = _request(
            control, "terminal", OWNER_UID, IPC_GID,
        )
        phase = "unknown"
        summary["unknown"] = _request(
            control, "unknown", OWNER_UID, IPC_GID,
        )
        # Filesystem group permits access, but peer UID is not Owner.
        phase = "wrong_uid"
        summary["wrong_uid"] = _request(
            control, "raw", STRANGER_UID, IPC_GID,
        )
        # Supplementary IPC group permits connect, but kernel primary
        # SO_PEERCRED GID is wrong and must be independently rejected.
        phase = "wrong_gid"
        summary["wrong_gid"] = _request(
            control, "raw", OWNER_UID, OTHER_GID,
            supplementary=(IPC_GID,),
        )
        phase = "broker_shutdown"
        control.sendall(b"X")
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert not SOCKET_PATH.exists()
        assert (SOCKET_PATH.parent / "broker-readonly-listener.lock").exists()
        return summary
    except BaseException as error:
        raise AssertionError(
            "synthetic phase=" + phase + " " +
            type(error).__name__ + ": " + str(error)[:180],
        ) from None
    finally:
        control.close()
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            waited = pid
        if waited == 0:
            os.kill(pid, 9)
            os.waitpid(pid, 0)


def _terminate_reap_coordinator_group(group_pid: int) -> None:
    """Kill and REAP a coordinator and any adopted Broker/Owner descendants.

    The guarded test runner must be PID 1 in its disposable PID namespace:
    orphaned grandchildren reparent to it, rather than escaping as zombies
    or retaining sockets while the mount/chroot fixture is dismantled.
    """
    assert os.getpid() == 1 and group_pid > 1
    try:
        os.killpg(group_pid, signal.SIGKILL)
    except ProcessLookupError:
        # Child has not yet established its own group (it does so BEFORE
        # chroot or any fork), or all of that group has already exited.
        try:
            os.kill(group_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        # SIGKILL makes the coordinator exit, which causes its Broker/Owner
        # descendants to be adopted by this PID-namespace init process.
        while True:
            try:
                waited, _ = os.waitpid(-group_pid, os.WNOHANG)
            except ChildProcessError:
                break
            if waited <= 0:
                break
        try:
            os.killpg(group_pid, 0)
        except ProcessLookupError:
            # The group may never have been established; still reap the
            # directly forked coordinator before acknowledging cleanup.
            try:
                waited, _ = os.waitpid(group_pid, os.WNOHANG)
            except ChildProcessError:
                return
            if waited == group_pid:
                return
        time.sleep(0.01)
    # A live/unreapable child makes socket unlink, bind umount and scratch
    # removal unsafe. This test is PID 1 in a disposable PID namespace:
    # terminating it exits WITHOUT invoking Python finally/context-manager
    # cleanup, while the namespace kernel tears down the isolated processes.
    # Leave any scratch path untouched for separate operator inspection.
    # Never continue ordinary teardown after an unverified reap.
    os._exit(78)



def _never_finish_with_broker_descendant() -> dict:
    """Fault injection: coordinator and its forked child remain live."""
    if os.fork() == 0:
        while True:
            signal.pause()
    # Record that a descendant was REALLY forked before the timeout.
    # The marker lives only in the chroot's disposable /tmp.
    Path("/tmp/oa472-b4-orphan-forked").write_text("forked\n", encoding="ascii")
    while True:
        signal.pause()


def _run_chroot_in_child(
    root: Path, *, timeout_s: float = 55.0,
    exercise: Callable[[], dict] = _exercise_chroot,
) -> dict:
    assert os.getpid() == 1, "requires private PID namespace init"
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        result = {}
        status = 0
        try:
            # Establish the coordinator's own process group BEFORE it can
            # fork the Broker; all role descendants inherit this group.
            os.setpgid(0, 0)
            os.chroot(root)
            os.chdir("/")
            assert os.stat("/").st_uid == 0
            assert os.stat("/tmp").st_uid == 0
            assert os.stat("/tmp/oa472-b4-ipc").st_uid == BROKER_UID
            assert os.stat("/tmp/oa472-b4-db").st_uid == BROKER_UID
            result = exercise()
        except BaseException as error:
            result = {"error": type(error).__name__,
                      "reason": str(error)[:240]}
            status = 1
        try:
            os.write(wfd, json.dumps(result, sort_keys=True).encode("ascii"))
        finally:
            os.close(wfd)
        os._exit(status)

    os.close(wfd)
    completed = False
    try:
        # Close the scheduling window before the child reaches its first
        # fork. Either side may set the SAME private group ID.
        try:
            os.setpgid(pid, pid)
        except ProcessLookupError:
            pass
        readable, _, _ = select.select([rfd], [], [], timeout_s)
        assert readable, "disposable coordinator timed out"
        raw = os.read(rfd, 8192)
        assert raw, "disposable coordinator lost result"
        result = json.loads(raw)
        # A child could send its last result and then hang before exit;
        # never use an unbounded waitpid or unmount underneath it.
        deadline = time.monotonic() + 5.0
        while True:
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                break
            assert time.monotonic() < deadline, "coordinator exit unverified"
            time.sleep(0.01)
        assert os.waitstatus_to_exitcode(status) == 0, result
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("coordinator left a live descendant")
        completed = True
        return result
    finally:
        os.close(rfd)
        if not completed:
            _terminate_reap_coordinator_group(pid)


def test_uidns_gate_is_opt_in_and_not_host_root():
    # The actual test cannot accidentally mutate a real host-root test
    # runner. Even if someone exports the flag outside a subordinate map,
    # the guard returns false.
    if os.environ.get("OA472_DISPOSABLE_UIDNS") != "1":
        assert _sandbox_proof() is False


@pytest.mark.skipif(
    not _sandbox_proof(),
    reason="requires explicit subordinate-ID user + private mount namespace",
)
def test_actual_distinct_owner_broker_kernel_peer_credentials():
    """All effects live in a temporary chroot, with real kernel credentials."""
    with tempfile.TemporaryDirectory(
        prefix="oa472-b4-uidns-", dir="/tmp",
    ) as raw:
        root = Path(raw) / "root"
        root.mkdir(mode=0o755)
        (root / "tmp").mkdir()
        (root / "tmp").chmod(0o1777)
        (root / "proc").mkdir()
        (root / "nix" / "store").mkdir(parents=True)
        ipc = root / "tmp" / "oa472-b4-ipc"
        db = root / "tmp" / "oa472-b4-db"
        ipc.mkdir()
        db.mkdir()
        os.chown(ipc, BROKER_UID, IPC_GID)
        os.chmod(ipc, 0o710)
        os.chown(db, BROKER_UID, IPC_GID)
        os.chmod(db, 0o700)
        lock = ipc / "broker-readonly-listener.lock"
        lock.touch(mode=0o600, exist_ok=False)
        os.chown(lock, BROKER_UID, IPC_GID)
        os.chmod(lock, 0o600)
        mount_targets = [(Path("/proc"), root / "proc"),
                         (Path("/nix/store"), root / "nix" / "store")]
        mounted = []
        try:
            for origin, target in mount_targets:
                subprocess.run(
                    ["mount", "--bind", str(origin), str(target)],
                    check=True, capture_output=True, text=True,
                )
                mounted.append(target)
            summary = _run_chroot_in_child(root)
            assert summary == {
                "credential": {"credential_valid": True},
                "pending": {"pending": True},
                "terminal": {"broker_local_no_effect": True},
                "unknown": {"unknown_denied": True},
                "wrong_uid": {"unauthorized_denied": True},
                "wrong_gid": {"unauthorized_denied": True},
            }
            # R0 independent P2 regression: a timed-out coordinator with
            # a still-live Broker child MUST be killed and reaped before
            # either scratch bind-mount is torn down. Do not sleep 55s.
            with pytest.raises(AssertionError, match="disposable coordinator timed out"):
                _run_chroot_in_child(
                    root, timeout_s=0.4,
                    exercise=_never_finish_with_broker_descendant,
                )
            assert (root / "tmp" / "oa472-b4-orphan-forked").read_text() == "forked\n"
            # This PID-namespace init is the adoptive parent of orphans.
            # A zombie/live Broker left over from the timeout fails here.
            with pytest.raises(ChildProcessError):
                os.waitpid(-1, os.WNOHANG)
        finally:
            # A failed synthetic coordinator can leave an owned bound name.
            # The TEST fixture alone removes its uniquely scoped scratch
            # socket after all descendants have been waited/killed.
            stale = ipc / "broker.sock"
            if stale.exists():
                info = stale.lstat()
                assert stat.S_ISSOCK(info.st_mode)
                assert info.st_uid == BROKER_UID
                stale.unlink()
            for target in reversed(mounted):
                subprocess.run(
                    ["umount", str(target)], check=True,
                    capture_output=True, text=True,
                )
            # No host-account or real service/credential resources touched.


def test_uidns_gate_rejects_host_root_or_missing_subordinate_ids(monkeypatch):
    """The opt-in flag alone must never authorize real host-root testing."""
    original_read = Path.read_text

    def host_root_uid_maps(path, *args, **kwargs):
        if str(path) in ("/proc/self/uid_map", "/proc/self/gid_map"):
            return "0 0 4294967295\n"
        return original_read(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setenv("OA472_DISPOSABLE_UIDNS", "1")
        patch.setattr(os, "geteuid", lambda: 0)
        patch.setattr(os, "getpid", lambda: 1)
        patch.setattr(Path, "read_text", host_root_uid_maps)
        assert _sandbox_proof() is False

    def no_subordinate_mapping(path, *args, **kwargs):
        if str(path) in ("/proc/self/uid_map", "/proc/self/gid_map"):
            return "0 1000 1\n"
        return original_read(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setenv("OA472_DISPOSABLE_UIDNS", "1")
        patch.setattr(os, "geteuid", lambda: 0)
        patch.setattr(os, "getpid", lambda: 1)
        patch.setattr(Path, "read_text", no_subordinate_mapping)
        assert _sandbox_proof() is False


def test_uidns_gate_rejects_non_pid1_even_with_valid_subordinate_mapping(monkeypatch):
    def valid_subordinate_maps(path, *args, **kwargs):
        if str(path) in ("/proc/self/uid_map", "/proc/self/gid_map"):
            return "0 1000 1\n1 100000 65535\n"
        raise AssertionError("unexpected host file access")

    with monkeypatch.context() as patch:
        patch.setenv("OA472_DISPOSABLE_UIDNS", "1")
        patch.setattr(os, "geteuid", lambda: 0)
        patch.setattr(os, "getpid", lambda: 301)
        patch.setattr(Path, "read_text", valid_subordinate_maps)
        assert _sandbox_proof() is False


def test_uidns_unreapable_descendant_fatally_aborts_before_teardown(monkeypatch):
    """Simulate an unkillable Broker: never enter ordinary mount/tmp cleanup."""
    class ExpectedFatalAbort(BaseException):
        def __init__(self, code):
            self.code = code

    # The simulated process group remains live after SIGKILL and its child
    # cannot be reaped. The bounded five-second deadline must lead to
    # os._exit(78), not a catchable Python exception allowing cleanup.
    times = iter((100.0, 100.1, 106.0))
    actions = []

    def fake_group_signal(group_pid, signum):
        assert group_pid == 123
        actions.append(("killpg", signum))

    def fake_waitpid(pid, flags):
        assert pid == -123 and flags == os.WNOHANG
        return (0, 0)

    def intercept_fatal_exit(code):
        actions.append(("os._exit", code))
        raise ExpectedFatalAbort(code)

    with monkeypatch.context() as patch:
        patch.setattr(os, "getpid", lambda: 1)
        patch.setattr(os, "killpg", fake_group_signal)
        patch.setattr(os, "waitpid", fake_waitpid)
        patch.setattr(os, "_exit", intercept_fatal_exit)
        patch.setattr(time, "monotonic", lambda: next(times))
        patch.setattr(time, "sleep", lambda _: None)
        with pytest.raises(ExpectedFatalAbort) as caught:
            _terminate_reap_coordinator_group(123)

    assert caught.value.code == 78
    assert actions == [
        ("killpg", signal.SIGKILL),
        ("killpg", 0),
        ("os._exit", 78),
    ]
