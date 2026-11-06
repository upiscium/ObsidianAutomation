"""#472 B2c-B1 disposable listener acceptance, never live services.

No users/groups or production systemd services are created. The cross-UID
fixture requires an already root-capable ephemeral test environment; normal
non-root development skips that single acceptance check.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import select
import socket
import stat
import subprocess
import sys
import tempfile
import threading

import pytest

from test_472_owner_coordination import owner_fixture
from test_472_signed_run_drain import RUN, PLAN, POLICY, OPERATION
from obsidian_automation.vault_format_owner_ipc import OperatorUnixIPC
from obsidian_automation.vault_format_owner_listener import (
    OwnerUnixListener, OwnerListenerDenied,
)


@pytest.fixture
def disposable_listener_root():
    # Absolute unix socket path stays below sockaddr_un's 108-byte bound,
    # entirely outside Vault/Nextcloud state and removed after each test.
    with tempfile.TemporaryDirectory(prefix="oa472-b1-", dir="/tmp") as raw:
        directory = Path(raw)
        directory.chmod(0o710)
        lock = directory / "owner-listener.lock"
        lock.touch(mode=0o600, exist_ok=False)
        lock.chmod(0o600)
        yield directory
        directory.chmod(0o700)


@pytest.fixture
def listener_site(owner_fixture, disposable_listener_root):
    owner, intent, *_ = owner_fixture
    handler = OperatorUnixIPC(owner, worker_uid=os.geteuid() + 1)
    listener = OwnerUnixListener(
        handler, disposable_listener_root / "owner.sock",
        ipc_group_gid=os.getegid(),
    )
    yield listener, owner, intent, disposable_listener_root
    # Explicitly close in each test; only release owned listener state.
    if listener._listener is not None:
        listener.close()


def _read_json_reply(conn: socket.socket) -> dict:
    """Read one bounded response line across arbitrary Unix-stream chunks."""
    buffer = bytearray()
    while len(buffer) <= 256:
        part = conn.recv(257 - len(buffer))
        if not part:
            break
        buffer.extend(part)
        if b"\n" in buffer:
            break
    assert len(buffer) <= 256 and buffer.endswith(b"\n")
    assert buffer.count(b"\n") == 1
    return json.loads(buffer)


def _client_exchange(listener, request: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(str(listener.path))
        client.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        client.shutdown(socket.SHUT_WR)
        assert listener.serve_once() is True
        return _read_json_reply(client)


def test_owner_lifecycle_and_real_connected_same_uid_policy(listener_site):
    listener, owner, intent, root = listener_site
    before_lock = (root / "owner-listener.lock").stat()
    with listener:
        sock = root / "owner.sock"
        info = sock.lstat()
        assert stat.S_ISSOCK(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o660
        assert (info.st_uid, info.st_gid) == (os.geteuid(), os.getegid())
        assert stat.S_IMODE(root.stat().st_mode) == 0o710
        assert _client_exchange(listener, {"v": 1, "op": "owner.recover"}) == {
            "v": 1, "ok": True, "unconfirmed": 0,
        }
        owner.register_signed(intent)
        # This connected same-UID client must NOT acquire Worker authority.
        denied = {"v": 1, "op": "run.claim", **asdict(intent)}
        assert _client_exchange(listener, denied) == {
            "v": 1, "ok": False, "reason": "ipc_denied",
        }
        assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION) is False
        owner.claim_signed(intent)
        assert _client_exchange(listener, {"v": 1, "op": "run.stop", "run_id": RUN}) == {
            "v": 1, "ok": True, "stopped": True,
        }
        assert owner.ledger.is_revoked(RUN)
    assert not (root / "owner.sock").exists()
    after_lock = (root / "owner-listener.lock").stat()
    assert (before_lock.st_dev, before_lock.st_ino) == (
        after_lock.st_dev, after_lock.st_ino,
    )
    with listener:
        assert _client_exchange(listener, {"v": 1, "op": "owner.recover"})["ok"] is True


def test_busy_and_stale_paths_are_not_removed_automatically(listener_site):
    listener, owner, intent, root = listener_site
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(listener.path))
    stale.close()
    original = listener.path.lstat()
    with pytest.raises(OwnerListenerDenied, match="^listener_path_occupied$"):
        listener.open()
    left = listener.path.lstat()
    assert (left.st_dev, left.st_ino) == (original.st_dev, original.st_ino)
    listener.path.unlink()
    listener.open()
    try:
        competing = OwnerUnixListener(
            listener.handler, listener.path, ipc_group_gid=os.getegid(),
        )
        with pytest.raises(OwnerListenerDenied, match="^listener_busy$"):
            competing.open()
    finally:
        listener.close()


@pytest.mark.parametrize("unsafe_mode", [0o700, 0o750, 0o770, 0o711, 0o777])
def test_parent_requires_exact_owner_and_mode(listener_site, unsafe_mode):
    listener, owner, intent, root = listener_site
    root.chmod(unsafe_mode)
    with pytest.raises(OwnerListenerDenied, match="^listener_(parent|ancestor)_unsafe$"):
        listener.open()
    assert not listener.path.exists()


def test_lock_is_preprovisioned_and_not_replaced(listener_site):
    listener, owner, intent, root = listener_site
    lock = root / "owner-listener.lock"
    lock.unlink()
    with pytest.raises(OwnerListenerDenied, match="^listener_lock_unsafe$"):
        listener.open()
    assert not listener.path.exists()
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    original = lock.lstat()
    with lock.open("rb") as fd:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(OwnerListenerDenied, match="^listener_busy$"):
            listener.open()
    assert lock.lstat().st_ino == original.st_ino


def test_symbolic_linked_lock_refused(listener_site):
    listener, owner, intent, root = listener_site
    lock = root / "owner-listener.lock"
    lock.unlink()
    target = root / "operator-owned-file"
    target.write_bytes(b"fixture")
    lock.symlink_to(target.name)
    with pytest.raises(OwnerListenerDenied, match="^listener_lock_unsafe$"):
        listener.open()
    assert lock.is_symlink()
    assert target.read_bytes() == b"fixture"


def test_post_bind_unverifiable_inode_is_fail_closed_and_requires_manual_cleanup(
    listener_site, monkeypatch,
):
    import obsidian_automation.vault_format_owner_listener as module

    listener, owner, intent, root = listener_site
    observed = {"socket": 0}
    original = module._sock_state

    def fail_first_postbind_stat(parent_fd, name):
        if name == listener.path.name:
            observed["socket"] += 1
            # First stat checks the absent pre-bind path. The second is the
            # first stat after bind; the inode cannot be independently pinned.
            if observed["socket"] == 2:
                raise OSError("synthetic failure; not a public reason")
        return original(parent_fd, name)

    with monkeypatch.context() as scope:
        scope.setattr(module, "_sock_state", fail_first_postbind_stat)
        with pytest.raises(
            OwnerListenerDenied, match="^listener_cleanup_unverified$",
        ):
            listener.open()
    assert listener.path.is_socket()
    with pytest.raises(OwnerListenerDenied, match="^listener_path_occupied$"):
        listener.open()
    # Test-only trusted operator removes this exact synthetic stale socket.
    listener.path.unlink()


def test_post_bind_chmod_failure_cleans_only_newly_pinned_owned_socket(
    listener_site, monkeypatch,
):
    import obsidian_automation.vault_format_owner_listener as module

    listener, owner, intent, root = listener_site
    original = module.os.chmod

    def fail_socket_mode(path, *args, **kwargs):
        if str(path) == str(listener.path):
            raise OSError("test-only chmod failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as scope:
        scope.setattr(module.os, "chmod", fail_socket_mode)
        with pytest.raises(OwnerListenerDenied, match="^listener_unavailable$"):
            listener.open()
    assert not listener.path.exists()


def test_post_bind_failed_fsync_is_redacted_and_all_locks_released(
    listener_site, monkeypatch,
):
    import obsidian_automation.vault_format_owner_listener as module

    listener, owner, intent, root = listener_site
    original_chmod = module.os.chmod
    original_fsync = module.os.fsync

    def fail_mode(path, *args, **kwargs):
        if str(path) == str(listener.path):
            raise OSError("SENSITIVE-SETUP-VALUE")
        return original_chmod(path, *args, **kwargs)

    def fail_fsync(fd):
        raise OSError("SENSITIVE-FSYNC-VALUE")

    with monkeypatch.context() as scope:
        scope.setattr(module.os, "chmod", fail_mode)
        scope.setattr(module.os, "fsync", fail_fsync)
        with pytest.raises(
            OwnerListenerDenied, match="^listener_cleanup_unverified$",
        ) as error:
            listener.open()
    assert "SENSITIVE" not in str(error.value)
    assert not listener.path.exists()
    # No stranded owner guard or flock after error.
    listener.open()
    listener.close()


def test_post_bind_failed_unlink_is_redacted_and_stale_socket_not_reused(
    listener_site, monkeypatch,
):
    import obsidian_automation.vault_format_owner_listener as module

    listener, owner, intent, root = listener_site
    original_chmod = module.os.chmod
    original_unlink = module.os.unlink

    def fail_mode(path, *args, **kwargs):
        if str(path) == str(listener.path):
            raise OSError("SENSITIVE-SETUP-VALUE")
        return original_chmod(path, *args, **kwargs)

    def fail_unlink(path, *args, **kwargs):
        if str(path) == listener.path.name and kwargs.get("dir_fd") is not None:
            raise OSError("SENSITIVE-UNLINK-VALUE")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as scope:
        scope.setattr(module.os, "chmod", fail_mode)
        scope.setattr(module.os, "unlink", fail_unlink)
        with pytest.raises(
            OwnerListenerDenied, match="^listener_cleanup_unverified$",
        ) as error:
            listener.open()
    assert "SENSITIVE" not in str(error.value)
    assert listener.path.is_socket()
    with pytest.raises(OwnerListenerDenied, match="^listener_path_occupied$"):
        listener.open()
    # Separate trusted operator recovery only in disposable fixture.
    listener.path.unlink()


def test_untrusted_socket_replacement_is_not_unlinked(listener_site):
    listener, owner, intent, root = listener_site
    listener.open()
    moved = root / "saved-original.sock"
    listener.path.rename(moved)
    foreign = root / "foreign"
    foreign.write_bytes(b"not ours")
    listener.path.symlink_to(foreign.name)
    with pytest.raises(OwnerListenerDenied, match="^listener_cleanup_unverified$"):
        listener.close()
    assert listener.path.is_symlink()
    assert foreign.read_bytes() == b"not ours"
    assert moved.exists()
    listener.path.unlink()
    moved.unlink()


def test_close_cannot_ack_while_a_request_is_in_flight(listener_site):
    listener, owner, intent, root = listener_site
    listener.open()
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(3)
    conn.connect(str(listener.path))
    conn.sendall(b'{"v":1,"op":"owner.recover"}\n')
    entered = threading.Event()
    actual = listener.handler.serve_once

    def paused(request_conn):
        entered.set()
        return actual(request_conn)

    listener.handler.serve_once = paused
    completed = []
    worker = threading.Thread(
        target=lambda: completed.append(listener.serve_once()), daemon=True,
    )
    try:
        worker.start()
        assert entered.wait(2)
        with pytest.raises(OwnerListenerDenied, match="^listener_busy$"):
            listener.close()
        assert listener.path.exists()
        conn.shutdown(socket.SHUT_WR)
        assert _read_json_reply(conn)["ok"] is True
        worker.join(3)
        assert not worker.is_alive()
        assert completed == [True]
    finally:
        conn.close()
        worker.join(3)
        listener.close()


def test_listener_requires_exact_owner_euid(listener_site, monkeypatch):
    listener, owner, intent, root = listener_site
    import obsidian_automation.vault_format_owner_listener as module

    monkeypatch.setattr(module.os, "geteuid", lambda: listener.owner_uid + 2)
    with pytest.raises(OwnerListenerDenied, match="^listener_owner_required$"):
        listener.open()
    assert not listener.path.exists()


def test_replacement_socket_inode_is_preserved_on_close(listener_site):
    listener, owner, intent, root = listener_site
    listener.open()
    saved = root / "original.sock"
    listener.path.rename(saved)
    foreign = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        foreign.bind(str(listener.path))
        os.chmod(listener.path, 0o660)
        before = listener.path.lstat()
    finally:
        foreign.close()
    with pytest.raises(OwnerListenerDenied, match="^listener_cleanup_unverified$"):
        listener.close()
    after = listener.path.lstat()
    assert stat.S_ISSOCK(after.st_mode)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    listener.path.unlink()
    saved.unlink()


def test_wrong_lock_mode_and_hardlink_are_rejected(listener_site):
    listener, owner, intent, root = listener_site
    lock = root / "owner-listener.lock"
    lock.chmod(0o640)
    with pytest.raises(OwnerListenerDenied, match="^listener_lock_unsafe$"):
        listener.open()
    lock.chmod(0o600)
    extra_link = root / "second-hardlink"
    os.link(lock, extra_link)
    with pytest.raises(OwnerListenerDenied, match="^listener_lock_unsafe$"):
        listener.open()
    assert not listener.path.exists()
    extra_link.unlink()


def test_replaced_lock_inode_invalidates_serving_and_cleanup(listener_site):
    listener, owner, intent, root = listener_site
    listener.open()
    saved = root / "original-listener.lock"
    (root / "owner-listener.lock").rename(saved)
    replacement = root / "owner-listener.lock"
    replacement.touch(mode=0o600)
    replacement.chmod(0o600)
    lock_inode = replacement.lstat().st_ino
    with pytest.raises(OwnerListenerDenied, match="^listener_lock_unsafe$"):
        listener.serve_once()
    with pytest.raises(OwnerListenerDenied, match="^listener_cleanup_unverified$"):
        listener.close()
    assert replacement.lstat().st_ino == lock_inode
    assert listener.path.is_socket()
    listener.path.unlink()
    replacement.unlink()
    saved.unlink()


def test_wrong_parent_group_rejected(listener_site):
    listener, owner, intent, root = listener_site
    other_groups = [g for g in os.getgroups() if g != os.getegid()]
    if not other_groups and os.geteuid() == 0:
        # Root-capable isolated CI must not skip this negative ACL proof
        # simply because its test OS principal has a single group.
        other_groups = [os.getegid() + 1]
    if not other_groups:
        pytest.skip("Unprivileged Owner has no permitted alternate group")
    try:
        os.chown(root, os.geteuid(), other_groups[0])
    except PermissionError:
        pytest.skip("Owner lacks permission for alternate group")
    try:
        with pytest.raises(
            OwnerListenerDenied, match="^listener_parent_unsafe$",
        ):
            listener.open()
        assert not listener.path.exists()
    finally:
        os.chown(root, os.geteuid(), os.getegid())


def test_symlink_in_socket_ancestry_rejected(listener_site):
    listener, owner, intent, root = listener_site
    alias = root.with_name(root.name + "-alias")
    alias.symlink_to(root, target_is_directory=True)
    try:
        different = OwnerUnixListener(
            listener.handler, alias / "owner.sock", ipc_group_gid=os.getegid(),
        )
        with pytest.raises(OwnerListenerDenied, match="^listener_parent_unsafe$"):
            different.open()
    finally:
        alias.unlink()
    assert not listener.path.exists()


def test_switched_parent_identity_fails_closed_without_foreign_unlink(listener_site):
    listener, owner, intent, root = listener_site
    listener.open()
    saved = root.with_name(root.name + "-saved")
    root.rename(saved)
    root.mkdir(mode=0o710)
    root.chmod(0o710)
    new_lock = root / "owner-listener.lock"
    new_lock.touch(mode=0o600)
    new_lock.chmod(0o600)
    try:
        with pytest.raises(OwnerListenerDenied, match="^listener_cleanup_unverified$"):
            listener.close()
        assert (saved / "owner.sock").is_socket()
        assert not (root / "owner.sock").exists()
    finally:
        (saved / "owner.sock").unlink(missing_ok=True)
        (saved / "owner-listener.lock").unlink(missing_ok=True)
        saved.rmdir()


@pytest.mark.skipif(os.geteuid() != 0, reason="ephemeral test requires root-capable CI runner")
def test_genuine_distinct_uid_worker_claim_and_owner_only_stop(
    owner_fixture,
):
    """Root-only disposable fixture; no useradd, chown of host resources or daemon."""
    import pwd

    try:
        other = pwd.getpwnam("nobody")
    except KeyError:
        pytest.skip("nobody OS principal unavailable")
    owner, intent, *_ = owner_fixture
    if other.pw_uid == os.geteuid():
        pytest.skip("no distinct UID")
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    with tempfile.TemporaryDirectory(prefix="oa472-crossuid-", dir="/tmp") as raw:
        directory = Path(raw)
        os.chown(directory, os.geteuid(), other.pw_gid)
        directory.chmod(0o710)
        lock = directory / "owner-listener.lock"
        lock.touch(mode=0o600)
        lock.chmod(0o600)
        handler = OperatorUnixIPC(owner, worker_uid=other.pw_uid)
        listener = OwnerUnixListener(
            handler, directory / "owner.sock", ipc_group_gid=other.pw_gid,
        )
        with listener:
            program = (
                "import json,socket,sys\n"
                "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
                "s.settimeout(8)\n"
                "s.connect(sys.argv[1])\n"
                "print('READY',flush=True)\n"
                "s.sendall(bytes.fromhex(sys.argv[2]))\n"
                "s.shutdown(socket.SHUT_WR)\n"
                "buf=b''\n"
                "while b'\\n' not in buf and len(buf) <= 256:\n"
                "    part=s.recv(257-len(buf))\n"
                "    if not part: break\n"
                "    buf+=part\n"
                "assert len(buf) <= 256 and buf.endswith(b'\\n')\n"
                "print(buf.decode().strip(),flush=True)\n"
                "s.close()\n"
            )

            def distinct_uid_request(message):
                payload = (json.dumps(message) + "\n").encode().hex()
                child = subprocess.Popen(
                    [sys.executable, "-c", program, str(listener.path), payload],
                    user=other.pw_uid, group=other.pw_gid, extra_groups=[],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                try:
                    # Wait for an explicit connected-client readiness event
                    # instead of inferring readiness from a wall-clock delay.
                    # The accepted request still executes only ONCE.
                    readable, _, _ = select.select([child.stdout], [], [], 12)
                    assert readable, "cross-UID client did not reach connected state"
                    assert child.stdout.readline().strip() == "READY"
                    assert listener.serve_once() is True
                    stdout, stderr = child.communicate(timeout=8)
                    assert child.returncode == 0, stderr
                    return json.loads(stdout)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=5)

            assert distinct_uid_request({
                "v": 1, "op": "run.claim", **asdict(intent),
            }) == {"v": 1, "ok": True, "claimed_once": True}
            assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
            # Actual different-UID Worker is refused Owner-only operations.
            assert distinct_uid_request({
                "v": 1, "op": "run.stop", "run_id": RUN,
            }) == {"v": 1, "ok": False, "reason": "ipc_denied"}
            assert distinct_uid_request({
                "v": 1, "op": "owner.recover",
            }) == {"v": 1, "ok": False, "reason": "ipc_denied"}
            assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
            assert not (owner.drain.verifier.authority_root / (RUN + ".revoked")).exists()
            # Only the trusted Owner UID can acknowledge stop.
            assert _client_exchange(listener, {
                "v": 1, "op": "run.stop", "run_id": RUN,
            }) == {"v": 1, "ok": True, "stopped": True}
            assert owner.ledger.is_revoked(RUN)
        directory.chmod(0o700)


def test_close_socket_error_is_redacted_and_requires_operator_cleanup(listener_site):
    listener, owner, intent, root = listener_site
    listener.open()
    underlying = listener._listener

    class FailClosed:
        def close(self):
            underlying.close()
            raise OSError("SECRET-OWNER-PATH-SHOULD-NOT-LEAK")

    listener._listener = FailClosed()
    with pytest.raises(
        OwnerListenerDenied, match="^listener_cleanup_unverified$",
    ) as error:
        listener.close()
    assert "SECRET" not in str(error.value)
    assert listener.path.is_socket()
    # Trusted synthetic cleanup after no successful Off acknowledgement.
    listener.path.unlink()


@pytest.mark.parametrize("fd_role", ["parent", "lock"])
def test_close_fd_error_releases_gate_and_reports_fixed_reason(
    listener_site, monkeypatch, fd_role,
):
    import obsidian_automation.vault_format_owner_listener as module

    listener, owner, intent, root = listener_site
    listener.open()
    target_fd = (listener._parent_fd if fd_role == "parent"
                 else listener._lock_fd)
    original_close = module.os.close

    def fail_selected_close(fd):
        original_close(fd)
        if fd == target_fd:
            raise OSError("SECRET-FD-ERROR")

    with monkeypatch.context() as scoped:
        scoped.setattr(module.os, "close", fail_selected_close)
        with pytest.raises(
            OwnerListenerDenied, match="^listener_cleanup_unverified$",
        ) as error:
            listener.close()
    assert "SECRET" not in str(error.value)
    assert not listener.path.exists()
    # A failure never leaves a permanently blocked in-process gate/flock.
    listener.open()
    listener.close()


def test_validation_descriptor_close_failure_redacted_and_gate_released(
    listener_site, monkeypatch,
):
    import obsidian_automation.vault_format_owner_listener as module

    listener, owner, intent, root = listener_site
    listener.open()
    real_open = module._open_trusted_parent
    real_close = module.os.close
    captured = {"fresh": None}

    def record_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        captured["fresh"] = fd
        return fd

    def fail_selected_close(fd):
        real_close(fd)
        if fd == captured["fresh"]:
            raise OSError("SECRET-PATH-MUST-NOT-LEAK")

    with monkeypatch.context() as scoped:
        scoped.setattr(module, "_open_trusted_parent", record_open)
        scoped.setattr(module.os, "close", fail_selected_close)
        with pytest.raises(
            OwnerListenerDenied, match="^listener_cleanup_unverified$",
        ) as error:
            listener.serve_once()
    assert "SECRET" not in str(error.value)
    assert listener.path.is_socket()
    assert listener.serve_once() is False
    listener.close()


def test_ancestry_descriptor_close_failure_does_not_expose_os_error(
    listener_site, monkeypatch,
):
    import obsidian_automation.vault_format_owner_listener as module

    listener, owner, intent, root = listener_site
    real_close = module.os.close
    first = {"done": False}

    def fail_first_close(fd):
        real_close(fd)
        if not first["done"]:
            first["done"] = True
            raise OSError("SECRET-TRAVERSAL-VALUE")

    with monkeypatch.context() as scoped:
        scoped.setattr(module.os, "close", fail_first_close)
        with pytest.raises(
            OwnerListenerDenied, match="^listener_cleanup_unverified$",
        ) as error:
            listener.open()
    assert "SECRET" not in str(error.value)
    assert not listener.path.exists()
    listener.open()
    listener.close()


@pytest.mark.skipif(os.geteuid() != 0, reason="root-only disposable ownership test")
def test_root_fixture_rejects_wrong_parent_owner(owner_fixture):
    """No persistent user changes; only a disposable /tmp directory is chown'd."""
    import pwd

    try:
        other = pwd.getpwnam("nobody")
    except KeyError:
        pytest.skip("nobody OS principal unavailable")
    owner, intent, *_ = owner_fixture
    if other.pw_uid == os.geteuid():
        pytest.skip("no distinct existing UID")
    with tempfile.TemporaryDirectory(prefix="oa472-b1-owner-", dir="/tmp") as raw:
        directory = Path(raw)
        original_gid = os.getegid()
        os.chown(directory, other.pw_uid, original_gid)
        directory.chmod(0o710)
        try:
            listener = OwnerUnixListener(
                OperatorUnixIPC(owner, worker_uid=other.pw_uid),
                directory / "owner.sock", ipc_group_gid=original_gid,
            )
            with pytest.raises(
                OwnerListenerDenied, match="^listener_(parent|ancestor)_unsafe$",
            ):
                listener.open()
            assert not (directory / "owner.sock").exists()
        finally:
            os.chown(directory, os.geteuid(), original_gid)
            directory.chmod(0o700)


def test_open_socket_close_failure_preserves_stale_name_and_live_fd(
    listener_site, monkeypatch,
):
    """A failed close during setup must never unlink a possibly live socket."""
    import types
    import obsidian_automation.vault_format_owner_listener as module

    listener, owner, intent, root = listener_site
    real_socket = socket.socket
    real_chmod = os.chmod
    created = []

    class UncertainClose:
        def __init__(self, *args, **kwargs):
            self.inner = real_socket(*args, **kwargs)
            created.append(self)

        def __getattr__(self, attr):
            return getattr(self.inner, attr)

        def close(self):
            # Keep the FD genuinely open to model an indeterminate cleanup.
            raise OSError("SENSITIVE-UNCERTAIN-SOCKET-FD")

    def fail_socket_mode(path, *args, **kwargs):
        if str(path) == str(listener.path):
            raise OSError("SENSITIVE-POST-BIND-SETUP")
        return real_chmod(path, *args, **kwargs)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                module, "socket",
                types.SimpleNamespace(
                    socket=UncertainClose,
                    AF_UNIX=socket.AF_UNIX,
                    SOCK_STREAM=socket.SOCK_STREAM,
                    timeout=socket.timeout,
                ),
            )
            scoped.setattr(module.os, "chmod", fail_socket_mode)
            with pytest.raises(
                OwnerListenerDenied, match="^listener_cleanup_unverified$",
            ) as error:
                listener.open()
        assert "SENSITIVE" not in str(error.value)
        assert len(created) == 1
        assert created[0].inner.fileno() >= 0
        assert listener.path.is_socket()
        # Never silently reuse/unlink the uncertain socket on restart.
        with pytest.raises(OwnerListenerDenied, match="^listener_path_occupied$"):
            listener.open()
    finally:
        for item in created:
            item.inner.close()
        listener.path.unlink(missing_ok=True)


def test_handler_exception_closes_accepted_fd_and_redacts_error(
    listener_site, monkeypatch,
):
    listener, owner, intent, root = listener_site
    listener.open()
    accepted = []
    original = listener._listener

    class CaptureAccept:
        def __getattr__(self, attr):
            return getattr(original, attr)

        def accept(self):
            conn, address = original.accept()
            accepted.append(conn)
            return conn, address

    def fail_handler(conn):
        raise OSError("SENSITIVE-REMOTE-REQUEST")

    listener._listener = CaptureAccept()
    try:
        with monkeypatch.context() as scope:
            scope.setattr(listener.handler, "serve_once", fail_handler)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(listener.path))
                client.sendall(b'{"v":1,"op":"owner.recover"}\n')
                client.shutdown(socket.SHUT_WR)
                with pytest.raises(
                    OwnerListenerDenied, match="^listener_request_unverified$",
                ) as error:
                    listener.serve_once()
                assert "SENSITIVE" not in str(error.value)
        assert len(accepted) == 1
        assert accepted[0].fileno() == -1
        assert listener.path.is_socket()
    finally:
        listener.close()
    assert not listener.path.exists()


def test_accepted_socket_close_failure_poisoned_and_not_unlinked(
    listener_site,
):
    listener, owner, intent, root = listener_site
    listener.open()
    original = listener._listener
    accepted = []

    class CloseFails:
        def __init__(self, conn):
            self.inner = conn
        def __getattr__(self, attr):
            return getattr(self.inner, attr)
        def close(self):
            raise OSError("SENSITIVE-ACCEPTED-FD")

    class InterceptAccept:
        def __getattr__(self, attr):
            return getattr(original, attr)
        def accept(self):
            conn, addr = original.accept()
            accepted.append(conn)
            return CloseFails(conn), addr

    listener._listener = InterceptAccept()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(listener.path))
            client.sendall(b'{"v":1,"op":"owner.recover"}\n')
            client.shutdown(socket.SHUT_WR)
            with pytest.raises(
                OwnerListenerDenied, match="^listener_cleanup_unverified$",
            ) as error:
                listener.serve_once()
            assert "SENSITIVE" not in str(error.value)
        assert len(accepted) == 1
        assert accepted[0].fileno() >= 0
        with pytest.raises(
            OwnerListenerDenied, match="^listener_cleanup_unverified$",
        ):
            listener.serve_once()
        with pytest.raises(
            OwnerListenerDenied, match="^listener_cleanup_unverified$",
        ):
            listener.close()
        assert listener.path.is_socket()
        with pytest.raises(
            OwnerListenerDenied, match="^listener_cleanup_unverified$",
        ):
            listener.open()
    finally:
        for conn in accepted:
            conn.close()
        listener.path.unlink(missing_ok=True)
