"""#472 B2c-D0 disposable Worker transport tests; no Vault or credentials.

Local tests use a deliberately synthetic peer-UID/path trust shim to exercise
framing and non-replay without pretending to prove distinct OS principals.
Only the separate root-capable fixture uses real Linux SO_PEERCRED, actual
Owner/Worker UIDs, group-only 0710 directory, and 0660 filesystem socket.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from test_472_owner_coordination import owner_fixture
from obsidian_automation.vault_format_run_ledger import RunIntent
from obsidian_automation.vault_format_owner_ipc import OperatorUnixIPC
from obsidian_automation.vault_format_owner_listener import OwnerUnixListener
import obsidian_automation.vault_format_worker_ipc_client as module
from obsidian_automation.vault_format_worker_ipc_client import (
    WorkerUnixClient, WorkerIPCDenied, RunStatusObservation, _socket_identity,
    _trusted_parent,
)


@pytest.fixture
def synthetic_client_site(tmp_path, monkeypatch, owner_fixture):
    # Tests below are NOT UID authentication acceptance. The real Worker
    # cannot own an Owner-only directory, so only protocol transport is
    # shimmed while using real Unix socket streams and kernel reads.
    owner, intent, *_ = owner_fixture
    owner_uid = os.geteuid() + 1
    path = tmp_path / "owner.sock"
    def synthetic_parent(_path, _uid, _gid):
        return os.open(tmp_path, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    def synthetic_socket(fd, name, _uid, _gid):
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISSOCK(st.st_mode):
            raise WorkerIPCDenied("worker_socket_untrusted")
        return st.st_dev, st.st_ino
    monkeypatch.setattr(module, "_trusted_parent", synthetic_parent)
    monkeypatch.setattr(module, "_socket_identity", synthetic_socket)
    monkeypatch.setattr(WorkerUnixClient, "_server_uid",
                        staticmethod(lambda sock: owner_uid))
    return WorkerUnixClient(path, owner_uid=owner_uid,
                            ipc_group_gid=os.getegid()), intent, path


def _run_stub(path: Path, responder, *, expect_requests=1):
    """Return a real listening socket and one bounded background test thread."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(3)
    server.settimeout(3)
    seen = []
    errors = []
    def worker():
        try:
            for i in range(expect_requests):
                conn, _ = server.accept()
                with conn:
                    conn.settimeout(2)
                    data = bytearray()
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data.extend(chunk)
                    seen.append(bytes(data))
                    reply = responder(i, bytes(data))
                    if reply is not None:
                        conn.sendall(reply)
        except BaseException as error:
            errors.append(type(error).__name__)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return server, thread, seen, errors


def _finish(server, thread, errors):
    thread.join(4)
    server.close()
    assert not thread.is_alive()
    assert errors == []


def _frame(obj):
    return json.dumps(obj, separators=(",", ":")).encode() + b"\n"


def test_claim_once_exact_request_and_no_second_send(synthetic_client_site):
    client, intent, path = synthetic_client_site
    server, thread, seen, errors = _run_stub(
        path, lambda i, raw: _frame({"v": 1, "ok": True, "claimed_once": True}),
    )
    try:
        assert client.claim_once(intent) is None
        with pytest.raises(WorkerIPCDenied, match="^worker_claim_consumed$"):
            client.claim_once(intent)
    finally:
        _finish(server, thread, errors)
    assert len(seen) == 1
    assert json.loads(seen[0]) == {"v": 1, "op": "run.claim", **asdict(intent)}
    assert seen[0].endswith(b"\n") and seen[0].count(b"\n") == 1


def test_status_observation_is_not_a_writer_lease(synthetic_client_site):
    client, intent, path = synthetic_client_site
    server, thread, seen, errors = _run_stub(
        path, lambda i, raw: _frame(
            {"v": 1, "ok": True, "claimed_now": i == 0}
        ), expect_requests=2,
    )
    try:
        assert client.status(intent) == RunStatusObservation(claimed_now=True)
        assert client.status(intent) == RunStatusObservation(claimed_now=False)
    finally:
        _finish(server, thread, errors)
    assert len(seen) == 2
    assert all(set(json.loads(x)) ==
               {"v", "op", "run_id", "plan_sha256", "policy_sha256",
                "operation_id"} for x in seen)
    assert not hasattr(RunStatusObservation(True), "writer")
    assert not hasattr(RunStatusObservation(True), "lease")


@pytest.mark.parametrize("reply,reason", [
    (None, "worker_response_invalid"),
    (_frame({"v": 1, "ok": True, "claimed_once": False}),
     "worker_response_invalid"),
    (_frame({"v": True, "ok": True, "claimed_once": True}),
     "worker_response_invalid"),
    (_frame({"v": 1, "ok": 1, "claimed_once": True}),
     "worker_response_invalid"),
    (_frame({"v": 1, "ok": True, "claimed_once": True,
             "write_allowed": True}), "worker_response_invalid"),
    (b'{"v":1,"v":1,"ok":true,"claimed_once":true}\n',
     "worker_response_invalid"),
    (_frame({"v": 1, "ok": False, "reason": "secret/path"}),
     "worker_response_invalid"),
    (_frame({"v": 1, "ok": False, "reason": "ipc_claim_denied"}),
     "worker_claim_denied"),
    (b'{"v":1,"ok":true,"claimed_once":true}', "worker_response_invalid"),
    (_frame({"v": 1, "ok": True, "claimed_once": True}) * 2,
     "worker_response_invalid"),
    (b"x" * 257, "worker_response_invalid"),
])
def test_mutating_claim_failure_never_allows_retry(
    synthetic_client_site, reply, reason,
):
    client, intent, path = synthetic_client_site
    server, thread, seen, errors = _run_stub(path, lambda i, raw: reply)
    try:
        with pytest.raises(WorkerIPCDenied, match=f"^{reason}$"):
            client.claim_once(intent)
        with pytest.raises(WorkerIPCDenied, match="^worker_claim_consumed$"):
            client.claim_once(intent)
    finally:
        _finish(server, thread, errors)
    assert len(seen) == 1


def test_response_split_across_deterministic_recv_boundaries():
    """Simulate real Unix-stream fragment boundaries independent of scheduling."""
    reply = _frame({"v": 1, "ok": True, "claimed_once": True})

    class Fragments:
        def __init__(self, fragments):
            self.fragments = list(fragments)
            self.timeouts = []

        def settimeout(self, seconds):
            self.timeouts.append(seconds)

        def recv(self, limit):
            result = self.fragments.pop(0)
            assert len(result) <= limit
            return result

    for parts in (
        [reply, b""],
        [reply[:1], reply[1:9], reply[9:-1], reply[-1:], b""],
        [bytes([v]) for v in reply] + [b""],
    ):
        stub = Fragments(parts)
        assert WorkerUnixClient._receive(stub, time.monotonic() + 3) == {
            "v": 1, "ok": True, "claimed_once": True,
        }
        assert not stub.fragments and stub.timeouts

    # A frame terminator alone is never sufficient: read through EOF.
    stub = Fragments([reply, b'{"v":1,"ok":true}\n', b""])
    with pytest.raises(WorkerIPCDenied, match="^worker_response_invalid$"):
        WorkerUnixClient._receive(stub, time.monotonic() + 3)


def test_wrong_server_peer_never_sends_claim(
    synthetic_client_site, monkeypatch,
):
    client, intent, path = synthetic_client_site
    monkeypatch.setattr(WorkerUnixClient, "_server_uid",
                        staticmethod(lambda sock: client._owner_uid + 1))
    server, thread, seen, errors = _run_stub(path, lambda i, raw: None)
    try:
        with pytest.raises(WorkerIPCDenied, match="^worker_peer_untrusted$"):
            client.claim_once(intent)
    finally:
        _finish(server, thread, errors)
    assert seen == [b""]


def test_socket_inode_changed_after_connect_never_sends(
    synthetic_client_site, monkeypatch,
):
    client, intent, path = synthetic_client_site
    real = module._socket_identity
    calls = [0]
    def changed(fd, name, uid, gid):
        calls[0] += 1
        dev, ino = real(fd, name, uid, gid)
        return dev, ino + (1 if calls[0] == 2 else 0)
    monkeypatch.setattr(module, "_socket_identity", changed)
    server, thread, seen, errors = _run_stub(path, lambda i, raw: None)
    try:
        with pytest.raises(WorkerIPCDenied, match="^worker_socket_untrusted$"):
            client.claim_once(intent)
    finally:
        _finish(server, thread, errors)
    assert seen == [b""]


def test_wrong_uid_euid_or_forked_client_denied_before_io(
    synthetic_client_site, monkeypatch,
):
    client, intent, path = synthetic_client_site
    monkeypatch.setattr(module.os, "geteuid",
                        lambda: client._worker_uid + 2)
    with pytest.raises(WorkerIPCDenied, match="^worker_process_mismatch$"):
        client.claim_once(intent)
    # Changing back does not reset a consumed claim slot.
    monkeypatch.undo()
    with pytest.raises(WorkerIPCDenied, match="^worker_claim_consumed$"):
        client.claim_once(intent)


def test_invalid_intent_consumes_claim_without_network(synthetic_client_site):
    client, intent, path = synthetic_client_site
    with pytest.raises(WorkerIPCDenied, match="^worker_request_invalid$"):
        client.claim_once({"v": 1, "op": "run.claim"})
    with pytest.raises(WorkerIPCDenied, match="^worker_claim_consumed$"):
        client.claim_once(intent)


def test_reject_untrusted_parent_and_socket_symlink(tmp_path):
    root = tmp_path / "owner"
    root.mkdir()
    root.chmod(0o710)
    # A Worker-owned parent is never a valid Owner trust boundary.
    candidate = root / "owner.sock"
    with pytest.raises(WorkerIPCDenied, match="^worker_path_untrusted$"):
        fd = _trusted_parent(candidate, os.geteuid() + 1, os.getegid())
        os.close(fd)
    path = tmp_path / "actual.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
        candidate.symlink_to(path)
        parent_fd = os.open(root, os.O_PATH | os.O_DIRECTORY)
        try:
            with pytest.raises(WorkerIPCDenied, match="^worker_socket_untrusted$"):
                _socket_identity(parent_fd, candidate.name,
                                 os.geteuid(), os.getegid())
        finally:
            os.close(parent_fd)
    finally:
        sock.close()


def test_same_uid_owner_client_construction_is_rejected(tmp_path):
    with pytest.raises(WorkerIPCDenied, match="^worker_policy_invalid$"):
        WorkerUnixClient(tmp_path / "owner.sock",
                         owner_uid=os.geteuid(),
                         ipc_group_gid=os.getegid())


def _restricted_worker_child_env(source_dir: Path) -> dict[str, str]:
    """Permit only import path and the shared-library path Python needs.

    actions/setup-python may require LD_LIBRARY_PATH to load libpython;
    dropping it makes the nobody child exit before announcing READY. Never
    forward the full Owner environment, which might contain credentials.
    """
    safe = {"PYTHONPATH": str(source_dir)}
    library_path = os.environ.get("LD_LIBRARY_PATH")
    if library_path:
        safe["LD_LIBRARY_PATH"] = library_path
    return safe


def test_disposable_child_env_preserves_python_loader_not_secrets(
    monkeypatch, tmp_path,
):
    with monkeypatch.context() as env:
        env.setenv("LD_LIBRARY_PATH", "/opt/hostedtoolcache/Python/mock/lib")
        env.setenv("D0_TEST_PRIVATE_TOKEN", "TEST_ONLY_PRIVATE")
        assert _restricted_worker_child_env(tmp_path) == {
            "PYTHONPATH": str(tmp_path),
            "LD_LIBRARY_PATH": "/opt/hostedtoolcache/Python/mock/lib",
        }


@pytest.mark.skipif(os.geteuid() != 0, reason="root-only disposable real-UID fixture")
def test_root_real_distinct_uid_worker_client_claim_and_status(owner_fixture):
    """Actual worker EUID != Owner EUID, filesystem ACL and SO_PEERCRED."""
    import pwd
    try:
        other = pwd.getpwnam("nobody")
    except KeyError:
        pytest.skip("nobody principal absent")
    if other.pw_uid == os.geteuid():
        pytest.skip("no distinct Worker UID")
    owner, intent, *_ = owner_fixture
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    with tempfile.TemporaryDirectory(prefix="oa472-d0-real-", dir="/tmp") as raw:
        directory = Path(raw)
        os.chown(directory, os.geteuid(), other.pw_gid)
        directory.chmod(0o710)
        lock = directory / "owner-listener.lock"
        lock.touch(mode=0o600)
        lock.chmod(0o600)
        listener = OwnerUnixListener(
            OperatorUnixIPC(owner, worker_uid=other.pw_uid),
            directory / "owner.sock", ipc_group_gid=other.pw_gid,
        )
        program = (
            "from pathlib import Path\n"
            "from obsidian_automation.vault_format_run_ledger import RunIntent\n"
            "from obsidian_automation.vault_format_worker_ipc_client "
            "import WorkerUnixClient,WorkerIPCDenied\n"
            "import json,os,sys\n"
            "intent=RunIntent(**json.loads(bytes.fromhex(sys.argv[2])))\n"
            "client=WorkerUnixClient(Path(sys.argv[1]),owner_uid=int(sys.argv[3]),"
            "ipc_group_gid=int(sys.argv[4]))\n"
            "print('READY',flush=True)\n"
            "assert client.claim_once(intent) is None\n"
            "print('CLAIM_OK',flush=True)\n"
            "assert client.status(intent).claimed_now is True\n"
            "print('STATUS_OBSERVED',flush=True)\n"
            "try: client.claim_once(intent)\n"
            "except WorkerIPCDenied as e: "
            "assert str(e)=='worker_claim_consumed'\n"
            "else: raise AssertionError('claim replay allowed')\n"
            "print('REPLAY_DENIED',flush=True)\n"
            "again=WorkerUnixClient(Path(sys.argv[1]),owner_uid=int(sys.argv[3]),"
            "ipc_group_gid=int(sys.argv[4]))\n"
            "try: again.claim_once(intent)\n"
            "except WorkerIPCDenied as e: assert str(e)=='worker_claim_denied'\n"
            "else: raise AssertionError('Owner ledger allowed another Claim')\n"
            "print('LEDGER_REPLAY_DENIED',flush=True)\n"
        )
        with listener:
            # Independent real-filesystem ACL proof, not merely an inference
            # from the Client's successful connect/response.
            parent_state = directory.lstat()
            socket_state = listener.path.lstat()
            lock_state = lock.lstat()
            assert stat.S_IMODE(parent_state.st_mode) == 0o710
            assert (parent_state.st_uid, parent_state.st_gid) == (
                os.geteuid(), other.pw_gid,
            )
            assert stat.S_ISSOCK(socket_state.st_mode)
            assert stat.S_IMODE(socket_state.st_mode) == 0o660
            assert (socket_state.st_uid, socket_state.st_gid) == (
                os.geteuid(), other.pw_gid,
            )
            assert stat.S_IMODE(lock_state.st_mode) == 0o600
            assert lock_state.st_uid == os.geteuid()
            child = subprocess.Popen(
                [sys.executable, "-c", program, str(listener.path),
                 json.dumps(asdict(intent)).encode().hex(),
                 str(os.geteuid()), str(other.pw_gid)],
                user=other.pw_uid, group=other.pw_gid, extra_groups=[],
                env=_restricted_worker_child_env(
                    Path(__file__).resolve().parents[1] / "src",
                ),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
            try:
                import select
                assert select.select([child.stdout], [], [], 10)[0]
                ready = child.stdout.readline().strip()
                if ready != "READY":
                    _, stderr = child.communicate(timeout=5)
                    # No raw error, source path or credential is copied into
                    # the test log: only a static pre-READY failure category.
                    cause = ("python_loader" if "error while loading shared libraries" in stderr
                             else "module_import" if "ModuleNotFoundError" in stderr
                             else "permission" if "PermissionError" in stderr
                             else "syntax" if "SyntaxError" in stderr
                             else "unknown")
                    pytest.fail(
                        f"root Worker child pre-ready {cause} (exit {child.returncode})",
                        pytrace=False,
                    )
                deadline = time.monotonic() + 12
                served = 0
                while served < 3 and time.monotonic() < deadline:
                    if listener.serve_once():
                        served += 1
                assert served == 3
                stdout, stderr = child.communicate(timeout=7)
                assert child.returncode == 0, stderr
                assert "CLAIM_OK" in stdout
                assert "STATUS_OBSERVED" in stdout
                assert "REPLAY_DENIED" in stdout
                assert "LEDGER_REPLAY_DENIED" in stdout
                assert owner.ledger.verify_claimed(
                    intent.run_id, intent.plan_sha256,
                    intent.policy_sha256, intent.operation_id,
                )
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)
        directory.chmod(0o700)


def test_worker_client_is_never_root_even_when_owner_is_other_uid(
    tmp_path, monkeypatch,
):
    # A privileged Worker would defeat the intended OS-principal separation.
    with monkeypatch.context() as changed:
        changed.setattr(module.os, "geteuid", lambda: 0)
        with pytest.raises(WorkerIPCDenied, match="^worker_policy_invalid$"):
            WorkerUnixClient(tmp_path / "owner.sock",
                             owner_uid=1234, ipc_group_gid=1234)


def _forked_client_attempt(client, intent, pipe):
    try:
        client.claim_once(intent)
        pipe.send("UNEXPECTED_SUCCESS")
    except WorkerIPCDenied as error:
        pipe.send(str(error))
    except BaseException:
        pipe.send("unexpected_exception")
    finally:
        pipe.close()


def test_inherited_client_cannot_claim_after_fork(synthetic_client_site):
    from multiprocessing import get_context
    client, intent, path = synthetic_client_site
    ctx = get_context("fork")
    recv, send = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_forked_client_attempt, args=(client, intent, send))
    proc.start()
    try:
        assert recv.poll(4)
        assert recv.recv() == "worker_process_mismatch"
        proc.join(4)
        assert proc.exitcode == 0
        # A rejected forked process cannot consume the original's Claim slot.
        assert client._claim_consumed is False
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(4)
        recv.close()
        send.close()


def test_root_fixture_child_source_compiles_without_root():
    """Keep root-only CI fixture syntax checked on every ordinary dev run."""
    import ast
    source = Path(__file__).read_text()
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "test_root_real_distinct_uid_worker_client_claim_and_status"]
    assert len(functions) == 1
    programs = [node.value for node in ast.walk(functions[0])
                if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "program"
                        for t in node.targets)]
    assert len(programs) == 1
    embedded = ast.literal_eval(programs[0])
    compile(embedded, "<root-only-worker-client-source>", "exec")
    assert "claim_once" in embedded and "status" in embedded


def test_unencodable_unicode_path_is_fixed_error_before_io(tmp_path):
    """An invalid filesystem path never leaks UnicodeEncodeError details."""
    malformed = tmp_path / ("not-encodable-" + chr(0xD800) + ".sock")
    with pytest.raises(WorkerIPCDenied, match="^worker_path_invalid$"):
        WorkerUnixClient(
            malformed, owner_uid=os.geteuid() + 1,
            ipc_group_gid=os.getegid(),
        )
    with pytest.raises(WorkerIPCDenied, match="^worker_path_invalid$"):
        _trusted_parent(malformed, os.geteuid() + 1, os.getegid())
