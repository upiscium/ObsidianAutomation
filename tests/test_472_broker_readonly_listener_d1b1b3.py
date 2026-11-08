"""#472 D1/B1/B3: disposable Broker read-only listener and lifecycle tests.

Tests never create accounts, systemd services, Provider credentials, Vault
files, or production sockets. Positive Owner identity is SO_PEERCRED-mocked
under a one-UID local runner; actual same-UID kernel denial is tested.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading

import pytest

from obsidian_automation.vault_format_broker_journal import BrokerPrivateJournal
from obsidian_automation.vault_format_broker_readonly_session import (
    BrokerReadOnlySession,
)
from obsidian_automation.vault_format_broker_readonly_listener import (
    BrokerListenerDenied, BrokerUnixReadOnlyListener,
)
from test_472_broker_readonly_session_d1b1b2 import (
    ACCOUNT, GEN, attempt, credential_request, finality_request,
)

OWNER_UID = os.geteuid() + 1001
OWNER_GID = os.getegid() + 1002


@pytest.fixture
def broker_site():
    with (
        tempfile.TemporaryDirectory(prefix="oa472b3-ipc-", dir="/tmp") as ipc,
        tempfile.TemporaryDirectory(prefix="oa472b3-db-", dir="/tmp") as db,
    ):
        parent = Path(ipc)
        parent.chmod(0o710)
        lock = parent / "broker-readonly-listener.lock"
        lock.touch(mode=0o600, exist_ok=False)
        lock.chmod(0o600)
        journal = BrokerPrivateJournal(Path(db), bootstrap=True)
        assert journal.start_boot() == 1
        assert journal.reconcile_boot() is True
        journal.provision_generation_metadata(ACCOUNT, GEN)
        session = BrokerReadOnlySession(
            journal, owner_uid=OWNER_UID, owner_gid=OWNER_GID,
        )
        listener = BrokerUnixReadOnlyListener(
            session, parent / "broker.sock", ipc_group_gid=os.getegid(),
        )
        yield listener, session, journal, parent
        # A failed pin/lock cleanup is deliberately not silently repaired
        # by the component. Only the disposable test fixture removes its own
        # isolated files after inspecting the result.
        if listener._listener is not None:
            try:
                listener.close()
            except BrokerListenerDenied:
                pass
        parent.chmod(0o700)


def exchange(listener, request: dict, monkeypatch, *, authorized: bool):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with monkeypatch.context() as patch:
            if authorized:
                patch.setattr(
                    BrokerReadOnlySession, "_connected_peer",
                    staticmethod(lambda _: (os.getpid(), OWNER_UID, OWNER_GID)),
                )
            client.settimeout(3)
            client.connect(str(listener.path))
            client.sendall(json.dumps(request, separators=(",", ":")).encode("ascii") + b"\n")
            client.shutdown(socket.SHUT_WR)
            assert listener.serve_once() is True
            chunks = []
            while True:
                try:
                    data = client.recv(2048)
                except ConnectionResetError:
                    break
                if not data:
                    break
                chunks.append(data)
            return b"".join(chunks)
    finally:
        client.close()


def test_b3_preprovisioned_lifecycle_and_socket_inode(broker_site, monkeypatch):
    listener, session, journal, parent = broker_site
    lock = parent / "broker-readonly-listener.lock"
    initial = lock.stat()
    with listener:
        st = listener.path.lstat()
        assert stat.S_ISSOCK(st.st_mode)
        assert (st.st_uid, st.st_gid) == (os.geteuid(), os.getegid())
        assert stat.S_IMODE(st.st_mode) == 0o660
        assert stat.S_IMODE(parent.stat().st_mode) == 0o710
        assert listener.serve_once() is False  # bounded empty accept
        observed = exchange(listener, credential_request(), monkeypatch, authorized=True)
        result = json.loads(observed)
        assert result["v"] == 1 and result["ok"] is True
        assert result["credential_generation"] == GEN
        assert "provider_secret" not in result
        # This actual same-UID connected peer is never an Owner.
        assert exchange(listener, credential_request(), monkeypatch, authorized=False) == b""
    assert not listener.path.exists()
    after = lock.stat()
    assert (initial.st_dev, initial.st_ino) == (after.st_dev, after.st_ino)
    with listener:
        assert json.loads(exchange(
            listener, credential_request(), monkeypatch, authorized=True,
        ))["ok"] is True


def test_b3_exact_finality_pending_and_local_no_effect(
    broker_site, monkeypatch,
):
    listener, session, journal, parent = broker_site
    op = attempt()
    journal.fence_pre_dispatch(op)
    with listener:
        response = json.loads(exchange(
            listener, finality_request(op), monkeypatch, authorized=True,
        ))
        assert response["ok"] is True and response["disposition"] == "pending"
        assert "receipt_sha256" not in response
        terminal = journal.cancel_never_dispatched(op)
        response = json.loads(exchange(
            listener, finality_request(op), monkeypatch, authorized=True,
        ))
        assert response["disposition"] == "verified_no_effect"
        assert response["receipt_sha256"] == terminal.receipt_sha256
        assert response["broker_journal_seq"] == terminal.broker_journal_seq
        assert "target_path" not in response


def test_b3_forbidden_provider_mutation_never_exposed(broker_site, monkeypatch):
    listener, session, journal, parent = broker_site
    with listener:
        response = exchange(listener, {"v": 1, "op": "provider.put"}, monkeypatch,
                            authorized=True)
        assert json.loads(response) == {
            "v": 1, "ok": False, "reason": "broker_denied",
        }
        assert not hasattr(listener, "send_put")
        assert not hasattr(listener, "start_service")


def test_b3_real_kernel_same_uid_connect_has_no_broker_data(
    broker_site, monkeypatch,
):
    listener, session, journal, parent = broker_site
    with listener:
        assert exchange(listener, credential_request(), monkeypatch, authorized=False) == b""


def test_b3_preexisting_stale_socket_never_deleted(broker_site):
    listener, session, journal, parent = broker_site
    original = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    original.bind(str(listener.path))
    original.close()
    inode = listener.path.lstat().st_ino
    with pytest.raises(BrokerListenerDenied, match="^listener_path_occupied$"):
        listener.open()
    assert listener.path.lstat().st_ino == inode
    listener.path.unlink()


def test_b3_second_listener_same_lock_is_rejected(broker_site):
    listener, session, journal, parent = broker_site
    with listener:
        competitor = BrokerUnixReadOnlyListener(
            session, listener.path, ipc_group_gid=os.getegid(),
        )
        with pytest.raises(BrokerListenerDenied, match="^listener_busy$"):
            competitor.open()


@pytest.mark.parametrize("mode", [0o700, 0o750, 0o770, 0o711, 0o777])
def test_b3_incorrect_parent_mode_refused(broker_site, mode):
    listener, session, journal, parent = broker_site
    parent.chmod(mode)
    with pytest.raises(
        BrokerListenerDenied, match="^listener_(parent|ancestor)_unsafe$",
    ):
        listener.open()


def test_b3_missing_lock_file_does_not_create_one(broker_site):
    listener, session, journal, parent = broker_site
    lock = parent / "broker-readonly-listener.lock"
    lock.unlink()
    with pytest.raises(BrokerListenerDenied, match="^listener_lock_unsafe$"):
        listener.open()
    assert not lock.exists() and not listener.path.exists()


def test_b3_symlink_lock_rejected(broker_site):
    listener, session, journal, parent = broker_site
    lock = parent / "broker-readonly-listener.lock"
    other = parent / "other-lock"
    lock.rename(other)
    lock.symlink_to(other)
    with pytest.raises(BrokerListenerDenied, match="^listener_lock_unsafe$"):
        listener.open()
    assert not listener.path.exists()


def test_b3_hardlinked_lock_rejected(broker_site):
    listener, session, journal, parent = broker_site
    os.link(parent / "broker-readonly-listener.lock", parent / "link")
    with pytest.raises(BrokerListenerDenied, match="^listener_lock_unsafe$"):
        listener.open()


def test_b3_wrong_lock_mode_refused(broker_site):
    listener, session, journal, parent = broker_site
    (parent / "broker-readonly-listener.lock").chmod(0o660)
    with pytest.raises(BrokerListenerDenied, match="^listener_lock_unsafe$"):
        listener.open()


def test_b3_untrusted_socket_replacement_not_unlinked(
    broker_site,
):
    listener, session, journal, parent = broker_site
    listener.open()
    original = parent / "original-broker.sock"
    listener.path.rename(original)
    foreign = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        foreign.bind(str(listener.path))
        foreign_inode = listener.path.stat().st_ino
        with pytest.raises(BrokerListenerDenied, match="^listener_identity_changed$"):
            listener.serve_once()
        with pytest.raises(BrokerListenerDenied, match="^listener_cleanup_unverified$"):
            listener.close()
        assert listener.path.stat().st_ino == foreign_inode
    finally:
        foreign.close()
        listener.path.unlink(missing_ok=True)
        original.unlink(missing_ok=True)


def test_b3_replaced_lock_invalidates_accept_and_cleanup(broker_site):
    listener, session, journal, parent = broker_site
    listener.open()
    lock = parent / "broker-readonly-listener.lock"
    preserved = parent / "previous-lock"
    lock.rename(preserved)
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    with pytest.raises(BrokerListenerDenied, match="^listener_lock_unsafe$"):
        listener.serve_once()
    with pytest.raises(BrokerListenerDenied, match="^listener_cleanup_unverified$"):
        listener.close()
    assert listener.path.exists()  # no unlink with an unsafe changed lock
    listener.path.unlink()


def test_b3_wrong_uid_fails_before_accept(broker_site, monkeypatch):
    listener, session, journal, parent = broker_site
    listener.open()
    with monkeypatch.context() as patch:
        patch.setattr(os, "geteuid", lambda: OWNER_UID)
        with pytest.raises(Exception):
            listener.serve_once()
    listener.close()
    assert not listener.path.exists()


def test_b3_close_while_accepted_handler_busy_is_rejected(
    broker_site, monkeypatch,
):
    listener, session, journal, parent = broker_site
    listener.open()
    entered = threading.Event()
    unblock = threading.Event()
    failures = []

    def blocked_response(self, conn):
        entered.set()
        assert unblock.wait(timeout=3)

    with monkeypatch.context() as patch:
        patch.setattr(BrokerReadOnlySession, "_serve_connected", blocked_response)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(listener.path))
            worker = threading.Thread(
                target=lambda: _serve_record(listener, failures), daemon=True,
            )
            worker.start()
            assert entered.wait(timeout=3)
            with pytest.raises(BrokerListenerDenied, match="^listener_busy$"):
                listener.close()
            unblock.set()
            worker.join(timeout=3)
            assert not worker.is_alive() and not failures
        finally:
            client.close()
            unblock.set()
    listener.close()
    assert not listener.path.exists()


def _serve_record(listener, errors):
    try:
        assert listener.serve_once() is True
    except BaseException as error:
        errors.append(error)


def test_b3_wrong_socket_mode_refuses_to_serve(broker_site):
    listener, session, journal, parent = broker_site
    listener.open()
    listener.path.chmod(0o600)
    try:
        with pytest.raises(BrokerListenerDenied, match="^listener_identity_changed$"):
            listener.serve_once()
        with pytest.raises(BrokerListenerDenied, match="^listener_cleanup_unverified$"):
            listener.close()
        assert listener.path.exists()
    finally:
        listener.path.unlink(missing_ok=True)


def test_b3_no_runtime_imports_or_service_unit():
    source = Path(__file__).resolve().parents[1] / "src/obsidian_automation"
    name = "vault_format_broker_readonly_listener"
    for existing in (
        "snapshot_app.py", "vault_format_job.py", "vault_format_writer.py",
        "vault_format_owner_ipc.py", "vault_format_owner_listener.py",
        "vault_format_broker_observation.py", "vault_format_broker_readonly_session.py",
    ):
        assert name not in (source / existing).read_text()
    listener_source = (source / f"{name}.py").read_text()
    assert "subprocess." not in listener_source
    assert "systemctl" not in listener_source
    assert "requests.put(" not in listener_source


def test_b3_reject_invalid_path_and_missing_principal(broker_site):
    listener, session, journal, parent = broker_site
    for unsafe in (
        Path("relative.sock"), parent / ".." / "other.sock",
        parent / "socket" / ".." / "fake.sock",
    ):
        with pytest.raises(BrokerListenerDenied, match="^listener_path_invalid$"):
            BrokerUnixReadOnlyListener(session, unsafe, ipc_group_gid=os.getegid())
    with pytest.raises(BrokerListenerDenied, match="^listener_policy_invalid$"):
        BrokerUnixReadOnlyListener(session, listener.path, ipc_group_gid=-1)


def test_b3_symlink_in_socket_ancestry_refused(broker_site):
    listener, session, journal, parent = broker_site
    with tempfile.TemporaryDirectory(prefix="oa472b3-link-", dir="/tmp") as raw:
        link = Path(raw) / "alias"
        link.symlink_to(parent, target_is_directory=True)
        alternate = BrokerUnixReadOnlyListener(
            session, link / listener.path.name, ipc_group_gid=os.getegid(),
        )
        with pytest.raises(BrokerListenerDenied, match="^listener_parent_unsafe$"):
            alternate.open()
        assert not listener.path.exists()


def test_b3_parent_inode_replacement_blocks_accept_without_cleanup(
    broker_site,
):
    listener, session, journal, parent = broker_site
    listener.open()
    renamed = parent.with_name(parent.name + "-original")
    parent.rename(renamed)
    parent.mkdir(mode=0o710)
    lock = parent / "broker-readonly-listener.lock"
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    try:
        with pytest.raises(BrokerListenerDenied, match="^listener_identity_changed$"):
            listener.serve_once()
        with pytest.raises(BrokerListenerDenied, match="^listener_cleanup_unverified$"):
            listener.close()
        assert (renamed / listener.path.name).exists()
        assert not listener.path.exists()
    finally:
        (renamed / listener.path.name).unlink(missing_ok=True)
        (renamed / "broker-readonly-listener.lock").unlink(missing_ok=True)
        renamed.rmdir()


def test_b3_setup_chmod_failure_cleans_only_pinned_new_socket(
    broker_site, monkeypatch,
):
    listener, session, journal, parent = broker_site
    real_chmod = os.chmod

    def broken_chmod(name, mode, *args, **kwargs):
        if str(name) == str(listener.path):
            raise PermissionError("synthetic socket chmod failure")
        return real_chmod(name, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "chmod", broken_chmod)
        with pytest.raises(BrokerListenerDenied, match="^listener_unavailable$"):
            listener.open()
    assert not listener.path.exists()
    # A known clean failed setup did not poison the lease or leave a name.
    with listener:
        assert listener.path.exists()


def test_b3_post_bind_socket_identity_unverifiable_poisoned(
    broker_site, monkeypatch,
):
    import obsidian_automation.vault_format_broker_readonly_listener as module

    listener, session, journal, parent = broker_site
    original = module._sock_state
    called = 0

    def fail_first_bound_inspection(fd, filename):
        nonlocal called
        if filename == listener.path.name:
            called += 1
            if called == 2:  # first check is pre-bind absence
                raise OSError("synthetic post-bind verification failure")
        return original(fd, filename)

    with monkeypatch.context() as patch:
        patch.setattr(module, "_sock_state", fail_first_bound_inspection)
        with pytest.raises(
            BrokerListenerDenied, match="^listener_cleanup_unverified$",
        ):
            listener.open()
    assert listener._cleanup_unverified is True
    assert listener.path.exists()
    with pytest.raises(BrokerListenerDenied, match="^listener_cleanup_unverified$"):
        listener.open()
    listener.path.unlink()


def test_b3_close_listener_fd_error_leaves_name_for_operator(
    broker_site,
):
    listener, session, journal, parent = broker_site
    listener.open()
    actual = listener._listener

    class FailingClose:
        def close(self):
            raise OSError("synthetic close failure")

    listener._listener = FailingClose()
    try:
        with pytest.raises(
            BrokerListenerDenied, match="^listener_cleanup_unverified$",
        ):
            listener.close()
        assert listener._cleanup_unverified is True
        assert listener.path.exists()
        with pytest.raises(
            BrokerListenerDenied, match="^listener_cleanup_unverified$",
        ):
            listener.open()
    finally:
        actual.close()
        listener.path.unlink(missing_ok=True)


def test_b3_socket_swap_between_preflight_and_accept_is_denied_before_handler(
    broker_site, monkeypatch,
):
    listener, session, journal, parent = broker_site
    listener.open()
    original_socket = parent / "old-broker.sock"
    original_listener = listener._listener
    foreign = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    queried = []

    def refused_if_called(conn):
        queried.append(True)
        raise AssertionError("handler must not observe swapped socket")

    class SwappedAccept:
        def accept(self):
            accepted, address = original_listener.accept()
            listener.path.rename(original_socket)
            foreign.bind(str(listener.path))
            return accepted, address

        def close(self):
            original_listener.close()

    listener._listener = SwappedAccept()
    with monkeypatch.context() as patch:
        patch.setattr(BrokerReadOnlySession, "_serve_connected", refused_if_called)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(3)
            client.connect(str(listener.path))
            client.sendall(json.dumps(credential_request()).encode() + b"\n")
            client.shutdown(socket.SHUT_WR)
            with pytest.raises(BrokerListenerDenied,
                               match="^listener_identity_changed$"):
                listener.serve_once()
            try:
                assert client.recv(128) == b""
            except ConnectionResetError:
                pass
            assert not queried
            with pytest.raises(BrokerListenerDenied,
                               match="^listener_cleanup_unverified$"):
                listener.close()
            assert listener.path.exists()  # foreign socket remains untouched
        finally:
            client.close()
            foreign.close()
            listener.path.unlink(missing_ok=True)
            original_socket.unlink(missing_ok=True)


def test_b3_accepted_descriptor_close_failure_poisoned_and_not_acked(
    broker_site, monkeypatch,
):
    """B2 must not swallow a close error before B3 reports success."""
    listener, session, journal, parent = broker_site
    listener.open()
    actual_listener = listener._listener
    accepted_fds = []

    class AcceptedCloseUnknown:
        def __init__(self, actual):
            self._actual = actual
            self.close_calls = 0

        def __getattr__(self, name):
            return getattr(self._actual, name)

        def close(self):
            self.close_calls += 1
            # The first attempt really releases the fd but reports an error.
            # A second call would return successfully: this is the precise
            # old B2-swallow + B3-double-close false-acknowledgement trace.
            self._actual.close()
            if self.close_calls == 1:
                raise OSError("synthetic close released fd but failed")

    class AcceptWithFailedPeerClose:
        def accept(self):
            conn, addr = actual_listener.accept()
            wrapped = AcceptedCloseUnknown(conn)
            accepted_fds.append(wrapped)
            return wrapped, addr

        def close(self):
            actual_listener.close()

    listener._listener = AcceptWithFailedPeerClose()
    with monkeypatch.context() as patch:
        patch.setattr(
            BrokerReadOnlySession, "_connected_peer",
            staticmethod(lambda _: (os.getpid(), OWNER_UID, OWNER_GID)),
        )
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(3)
            client.connect(str(listener.path))
            client.sendall(json.dumps(credential_request()).encode() + b"\n")
            client.shutdown(socket.SHUT_WR)
            with pytest.raises(
                BrokerListenerDenied, match="^listener_cleanup_unverified$",
            ):
                listener.serve_once()
            assert listener._cleanup_unverified is True
            assert len(accepted_fds) == 1
            assert accepted_fds[0].close_calls == 1
            assert accepted_fds[0]._actual.fileno() == -1
            # Even though the observation might have been transmitted,
            # neither the request nor the socket cleanup is ACKed as safe.
            with pytest.raises(
                BrokerListenerDenied, match="^listener_cleanup_unverified$",
            ):
                listener.close()
            assert listener.path.exists()
        finally:
            client.close()
            for accepted in accepted_fds:
                assert accepted.close_calls == 1
                accepted._actual.close()
            listener.path.unlink(missing_ok=True)


def test_b3_standalone_b2_close_failure_is_not_silently_discarded(
    broker_site,
):
    from obsidian_automation.vault_format_broker_readonly_session import (
        BrokerReadOnlyDenied,
    )

    listener, session, journal, parent = broker_site
    server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

    class FailedClose:
        def __getattr__(self, name):
            return getattr(server, name)

        def close(self):
            raise OSError("synthetic B2 close unknown")

    try:
        # Same-UID kernel peer is refused without reading; standalone
        # serve_once MUST still report its own uncertain close.
        with pytest.raises(
            BrokerReadOnlyDenied, match="^broker_cleanup_unverified$",
        ):
            session.serve_once(FailedClose())
    finally:
        server.close()
        client.close()


def test_b3_failed_open_latches_cleanup_before_releasing_gate(
    broker_site, monkeypatch,
):
    """No competing open can enter between the gate release and poison."""
    listener, session, journal, parent = broker_site
    real_gate = listener._gate
    real_chmod = os.chmod
    real_fsync = os.fsync
    inspected = []

    class CheckBeforeRelease:
        def acquire(self, *args, **kwargs):
            return real_gate.acquire(*args, **kwargs)

        def release(self):
            inspected.append(listener._cleanup_unverified)
            try:
                assert listener._cleanup_unverified is True
            finally:
                real_gate.release()

    def failed_socket_chmod(path, mode, *args, **kwargs):
        if str(path) == str(listener.path):
            raise PermissionError("synthetic post-bind chmod failure")
        return real_chmod(path, mode, *args, **kwargs)

    def failed_cleanup_fsync(fd):
        if os.fstat(fd).st_ino == parent.stat().st_ino:
            raise OSError("synthetic directory fsync failure")
        return real_fsync(fd)

    listener._gate = CheckBeforeRelease()
    with monkeypatch.context() as patch:
        patch.setattr(os, "chmod", failed_socket_chmod)
        patch.setattr(os, "fsync", failed_cleanup_fsync)
        with pytest.raises(
            BrokerListenerDenied, match="^listener_cleanup_unverified$",
        ):
            listener.open()
    assert inspected == [True]
    assert listener._cleanup_unverified is True
    # Even if the failed setup already unlinked its pinned socket, a new
    # open on this instance must not attempt to rebind it.
    with pytest.raises(BrokerListenerDenied, match="^listener_cleanup_unverified$"):
        listener.open()
    assert not listener.path.exists()
