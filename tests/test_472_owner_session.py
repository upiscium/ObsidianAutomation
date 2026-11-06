"""#472 R2/B2c-C0: disposable bounded Owner process-session acceptance.

No installed service, production users, credentials, Writer, WebDAV or Vault.
One fork-based case proves an actual separate PID and boot-time Ledger
reconciliation. It is not proof of a separately provisioned Owner UID.
"""
from __future__ import annotations

from multiprocessing import get_context
import json
import os
from pathlib import Path
import socket

import pytest

from test_472_owner_coordination import owner_fixture
from test_472_signed_run_drain import RUN, PLAN, POLICY, OPERATION
from test_472_owner_listener import disposable_listener_root, _read_json_reply
from obsidian_automation.vault_format_owner_coordination import (
    OperatorRunCoordinator,
)
from obsidian_automation.vault_format_owner_ipc import OperatorUnixIPC
from obsidian_automation.vault_format_owner_listener import OwnerUnixListener
from obsidian_automation.vault_format_owner_session import (
    BoundedOwnerSession, OwnerSessionDenied, OwnerSessionResult,
)
from obsidian_automation.vault_format_run_ledger import DurableRunLedger


@pytest.fixture
def session_site(owner_fixture, disposable_listener_root):
    owner, intent, *_ = owner_fixture
    socket_path = disposable_listener_root / "owner.sock"
    listener = OwnerUnixListener(
        OperatorUnixIPC(owner, worker_uid=os.geteuid() + 1),
        socket_path, ipc_group_gid=os.getegid(),
    )
    yield listener, owner, intent, socket_path
    if listener._listener is not None:
        listener.close()


def test_bounded_idle_exit_and_socket_cleanup(session_site):
    listener, owner, intent, path = session_site
    session = BoundedOwnerSession(listener)
    result = session.run(max_requests=1, max_idle_polls=2)
    assert result == OwnerSessionResult(
        accepted=0, idle_polls=2, boot_unconfirmed=0,
        exit_reason="idle_limit",
    )
    assert owner.ledger._boot_reconciled is True
    assert not path.exists()
    assert not hasattr(result, "writer")
    assert not hasattr(result, "lease")
    with pytest.raises(OwnerSessionDenied, match="^owner_session_consumed$"):
        session.run()


@pytest.mark.parametrize("params", [
    {"max_requests": 0}, {"max_requests": 17},
    {"max_requests": True}, {"max_requests": 1.0},
    {"max_idle_polls": 0}, {"max_idle_polls": 25},
    {"max_idle_polls": False}, {"max_idle_polls": "2"},
])
def test_invalid_limits_do_not_start_listener(session_site, params):
    listener, owner, intent, path = session_site
    session = BoundedOwnerSession(listener)
    with pytest.raises(OwnerSessionDenied, match="^owner_session_budget_invalid$"):
        session.run(**params)
    assert not owner.ledger._boot_reconciled
    assert not path.exists()
    # The invalid first call is consumed: no retry after preflight failure.
    with pytest.raises(OwnerSessionDenied, match="^owner_session_consumed$"):
        session.run()


def test_preexisting_boot_mark_cannot_be_reused_as_new_process(session_site):
    listener, owner, intent, path = session_site
    assert owner.recover_after_quiescence() == 0
    session = BoundedOwnerSession(listener)
    with pytest.raises(OwnerSessionDenied, match="^owner_session_boot_not_fresh$"):
        session.run()
    assert not path.exists()
    with pytest.raises(OwnerSessionDenied, match="^owner_session_consumed$"):
        session.run()


def test_recovery_denied_by_effect_in_flight_never_opens_socket(session_site):
    listener, owner, intent, path = session_site
    session = BoundedOwnerSession(listener)
    with owner.drain.effect(RUN, PLAN, POLICY, OPERATION):
        with pytest.raises(OwnerSessionDenied, match="^owner_session_unverified$"):
            session.run()
        assert not path.exists()
        assert not owner.ledger._boot_reconciled
    with pytest.raises(OwnerSessionDenied, match="^owner_session_consumed$"):
        session.run()


def test_handler_rebinding_before_boot_is_denied(session_site):
    listener, owner, intent, path = session_site
    session = BoundedOwnerSession(listener)
    original = listener.handler
    try:
        listener.handler = object()
        with pytest.raises(OwnerSessionDenied, match="^owner_session_binding_changed$"):
            session.run()
        assert not owner.ledger._boot_reconciled
        assert not path.exists()
    finally:
        listener.handler = original
    with pytest.raises(OwnerSessionDenied, match="^owner_session_consumed$"):
        session.run()


def _child_fork_inherited_session(session, pipe):
    try:
        session.run(max_requests=1, max_idle_polls=1)
        pipe.send(("UNEXPECTED_SUCCESS", os.getpid()))
    except OwnerSessionDenied as error:
        pipe.send(("DENIED", str(error)))
    except BaseException:
        pipe.send(("ERROR", "unverified"))
    finally:
        pipe.close()


def test_inherited_instance_fails_closed_across_fork(session_site):
    listener, owner, intent, path = session_site
    session = BoundedOwnerSession(listener)
    ctx = get_context("fork")
    parent, child = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_child_fork_inherited_session, args=(session, child))
    process.start()
    try:
        assert parent.poll(4)
        assert parent.recv() == ("DENIED", "owner_session_process_mismatch")
        process.join(4)
        assert process.exitcode == 0
        assert not path.exists()
        assert not owner.ledger._boot_reconciled
    finally:
        if process.is_alive():
            process.terminate()
            process.join(3)
        parent.close()
        child.close()


def _fresh_owner_child(inherited_owner, path, output, allow_accept=None):
    """Build a fresh ledger/coordinator/session in a real child process."""
    try:
        ledger = DurableRunLedger(inherited_owner.ledger.path.parent)
        owner = OperatorRunCoordinator(inherited_owner.drain, ledger)
        listener = OwnerUnixListener(
            OperatorUnixIPC(owner, worker_uid=os.geteuid() + 1),
            Path(path), ipc_group_gid=os.getegid(),
        )
        service = BoundedOwnerSession(listener)
        if allow_accept is not None:
            # One test-only event PER accept, never exceeding one queued
            # connection. Recovery and Listener setup finish before READY.
            original_serve = listener.serve_once
            pending = iter(enumerate(allow_accept, start=1))

            def gated_serve():
                try:
                    number, release = next(pending)
                except StopIteration:
                    raise OwnerSessionDenied("owner_session_unverified") from None
                output.send(("READY", number, os.getpid()))
                if not release.wait(12):
                    raise OwnerSessionDenied("owner_session_unverified")
                return original_serve()

            listener.serve_once = gated_serve
        output.send(("PID", os.getpid()))
        result = service.run(max_requests=2, max_idle_polls=16)
        output.send(("RESULT", result.accepted, result.idle_polls,
                     result.boot_unconfirmed, result.exit_reason))
    except BaseException:
        # No internal exception details or sensitive file paths on the pipe.
        output.send(("ERROR", "owner_session_unverified"))
    finally:
        output.close()


def _queue_request(socket_path: Path, payload: dict) -> socket.socket:
    """Queue one complete request before releasing the test-only accept gate."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(6)
        conn.connect(str(socket_path))
        conn.sendall(
            json.dumps(payload, separators=(",", ":")).encode() + b"\n"
        )
        conn.shutdown(socket.SHUT_WR)
        return conn
    except BaseException:
        conn.close()
        raise


def test_fresh_separate_owner_pid_reconciles_claim_before_accept(
    owner_fixture, disposable_listener_root,
):
    owner, intent, *_ = owner_fixture
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    owner.claim_signed(intent)
    assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    socket_path = disposable_listener_root / "owner.sock"
    ctx = get_context("fork")
    parent, child = ctx.Pipe(duplex=False)
    accept_one, accept_two = ctx.Event(), ctx.Event()
    connections = []
    process = ctx.Process(
        target=_fresh_owner_child,
        args=(owner, str(socket_path), child, (accept_one, accept_two)),
    )
    process.start()
    try:
        assert parent.poll(8)
        assert parent.recv() == ("PID", process.pid)
        # One test-only gate PER accept. Recovery has completed before the
        # first READY, and no idle polling has started while we prepare.
        assert parent.poll(8)
        assert parent.recv() == ("READY", 1, process.pid)
        assert not owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
        connections.append(_queue_request(
            socket_path, {"v": 1, "op": "owner.recover"},
        ))
        accept_one.set()
        assert _read_json_reply(connections[0]) == {
            "v": 1, "ok": True, "unconfirmed": 0,
        }
        assert parent.poll(8)
        assert parent.recv() == ("READY", 2, process.pid)
        # The first client was already accepted/answered, so at most ONE
        # connection occupies the pending queue at any moment.
        connections.append(_queue_request(socket_path, {
            "v": 1, "op": "run.claim",
            **{field: getattr(intent, field)
               for field in intent.__dataclass_fields__},
        }))
        accept_two.set()
        # Owner EUID is not the configured Worker UID: Worker-only Claim
        # must fail. B1 independently tests genuine different-UID success.
        assert _read_json_reply(connections[1]) == {
            "v": 1, "ok": False, "reason": "ipc_denied",
        }
        assert parent.poll(8)
        assert parent.recv() == ("RESULT", 2, 0, 1, "request_limit")
        process.join(8)
        assert process.exitcode == 0
        assert not socket_path.exists()
    finally:
        accept_one.set()
        accept_two.set()
        for conn in connections:
            conn.close()
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
        child.close()


def test_competing_listener_never_invalidates_active_owner_claim(session_site):
    """A failed contender must not perform even a boot-recovery mutation."""
    listener, owner, intent, path = session_site
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    owner.claim_signed(intent)
    assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    # The original Owner's preprovisioned listener lock is already held.
    listener.open()
    try:
        fresh_ledger = DurableRunLedger(owner.ledger.path.parent)
        contender_owner = OperatorRunCoordinator(owner.drain, fresh_ledger)
        contender = OwnerUnixListener(
            OperatorUnixIPC(contender_owner, worker_uid=os.geteuid() + 1),
            path, ipc_group_gid=os.getegid(),
        )
        new_session = BoundedOwnerSession(contender)
        with pytest.raises(OwnerSessionDenied, match="^owner_session_unverified$"):
            new_session.run(max_requests=1, max_idle_polls=1)
        # If recovery ran before the contender won the listener lease,
        # the existing Owner's legitimate Claim would be invalidated.
        assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
        assert fresh_ledger._boot_reconciled is False
        assert path.is_socket()
    finally:
        listener.close()


def test_socket_may_connect_during_recovery_but_request_not_served(
    session_site, monkeypatch,
):
    """Actual socket request waits until the shared-lock boot gate succeeds."""
    import threading

    listener, owner, intent, path = session_site
    entered = threading.Event()
    permit = threading.Event()
    finished = []
    original = owner.recover_after_quiescence
    dispatches = []
    handler = listener.handler
    original_handler = handler.serve_once

    def record_dispatch(conn):
        dispatches.append(True)
        return original_handler(conn)

    monkeypatch.setattr(handler, "serve_once", record_dispatch)

    def delayed_recovery():
        entered.set()
        assert permit.wait(4)
        return original()

    monkeypatch.setattr(owner, "recover_after_quiescence", delayed_recovery)
    session = BoundedOwnerSession(listener)

    def serve():
        try:
            finished.append(session.run(max_requests=1, max_idle_polls=2))
        except BaseException:
            finished.append("error")

    thread = threading.Thread(target=serve, daemon=True)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        thread.start()
        assert entered.wait(3)
        # A concurrent invocation cannot reenter while boot is in progress.
        with pytest.raises(OwnerSessionDenied, match="^owner_session_consumed$"):
            session.run(max_requests=1, max_idle_polls=1)
        assert path.is_socket()
        client.settimeout(0.12)
        client.connect(str(path))
        client.sendall(b'{"v":1,"op":"owner.recover"}\n')
        client.shutdown(socket.SHUT_WR)
        # Socket is bound during recovery, but the command has NOT dispatched.
        with pytest.raises(TimeoutError):
            client.recv(256)
        assert dispatches == []  # No handler entry before boot recovery.
        permit.set()
        client.settimeout(3)
        assert _read_json_reply(client) == {"v": 1, "ok": True, "unconfirmed": 0}
        thread.join(3)
        assert dispatches == [True]
        assert finished == [OwnerSessionResult(
            accepted=1, idle_polls=0, boot_unconfirmed=0,
            exit_reason="request_limit",
        )]
    finally:
        permit.set()
        client.close()
        thread.join(3)
    assert not path.exists()


def test_no_success_result_when_listener_close_fails(session_site, monkeypatch):
    listener, owner, intent, path = session_site
    original = listener.close

    def close_but_fail():
        original()
        raise OSError("PRIVATE-LISTENER-PATH-SHOULD-NOT-LEAK")

    with monkeypatch.context() as m:
        m.setattr(listener, "close", close_but_fail)
        session = BoundedOwnerSession(listener)
        with pytest.raises(OwnerSessionDenied, match="^owner_session_unverified$") as error:
            session.run(max_requests=1, max_idle_polls=1)
    assert "PRIVATE" not in str(error.value)
    assert not path.exists()
    with pytest.raises(OwnerSessionDenied, match="^owner_session_consumed$"):
        session.run()


def test_real_competing_owner_pid_cannot_reconcile_before_lifetime_lock(
    session_site,
):
    """A contender in another PID cannot invalidate the active Owner's row."""
    listener, owner, intent, path = session_site
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    owner.claim_signed(intent)
    assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    ctx = get_context("fork")
    parent, child = ctx.Pipe(duplex=False)
    process = None
    listener.open()
    try:
        process = ctx.Process(
            target=_fresh_owner_child,
            args=(owner, str(path), child),
        )
        process.start()
        assert parent.poll(5)
        assert parent.recv() == ("PID", process.pid)
        assert parent.poll(5)
        assert parent.recv() == ("ERROR", "owner_session_unverified")
        process.join(5)
        assert process.exitcode == 0
        assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
        assert path.is_socket()
    finally:
        if process is not None and process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
        child.close()
        listener.close()
    assert not path.exists()


def test_stale_crash_socket_fails_closed_without_reconciling_claim(session_site):
    """A new Owner does not silently adopt a stale pathname or alter Ledger."""
    listener, owner, intent, path = session_site
    owner.recover_after_quiescence()
    owner.register_signed(intent)
    owner.claim_signed(intent)
    assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        stale.bind(str(path))
    finally:
        stale.close()
    before = path.lstat()
    try:
        new_ledger = DurableRunLedger(owner.ledger.path.parent)
        candidate_owner = OperatorRunCoordinator(owner.drain, new_ledger)
        candidate_listener = OwnerUnixListener(
            OperatorUnixIPC(candidate_owner, worker_uid=os.geteuid() + 1),
            path, ipc_group_gid=os.getegid(),
        )
        candidate = BoundedOwnerSession(candidate_listener)
        with pytest.raises(OwnerSessionDenied, match="^owner_session_unverified$"):
            candidate.run(max_requests=1, max_idle_polls=1)
        assert new_ledger._boot_reconciled is False
        assert owner.ledger.verify_claimed(RUN, PLAN, POLICY, OPERATION)
        assert path.is_socket()
        assert path.lstat().st_ino == before.st_ino
        with pytest.raises(OwnerSessionDenied, match="^owner_session_consumed$"):
            candidate.run()
    finally:
        # Explicit trusted *disposable test* cleanup, not a runtime feature.
        path.unlink(missing_ok=True)
