"""Fixed D-Bus Job admission is NOT business completion or retry approval."""
from __future__ import annotations

from contextlib import contextmanager
import os
import subprocess
import tempfile
import time

import pytest

from obsidian_automation.admin_operation_broker import BrokerRejected, SERVICES
from obsidian_automation.admin_systemd_admission import (
    BusctlStartUnitManager, RunOnceJobDispatcher, SystemdJobAdmission,
    verify_job_admission, MAX_ADMISSION_REPLY_BYTES,
)
from test_admin_operation_broker import SPEC, NOW, FakeUnits, snapshot

JOB = "/org/freedesktop/systemd1/job/1251"
BOOT = "12345678-1234-1234-8234-123456789abc"


class FakeManager:
    def __init__(self, *, reply=JOB):
        self.calls = []
        self.reply = reply

    def start_unit(self, spec):
        self.calls.append(spec)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def dispatcher(adapter=None, manager=None, *, enabled=True, gate=None, require_root=False,
               clock=None):
    if adapter is None:
        adapter = FakeUnits(timer_state="paused")
    if manager is None:
        manager = FakeManager()
    if gate is None:
        @contextmanager
        def gate():
            yield
    return RunOnceJobDispatcher(
        adapter=adapter, manager=manager, maintenance_gate=gate,
        clock=clock or (lambda: NOW),
        require_root=require_root, enabled=enabled,
    )


def test_valid_paused_target_returns_systemd_job_not_service_result():
    a = FakeUnits(timer_state="paused")
    manager = FakeManager()
    observer = dispatcher(a, manager)
    result = observer.dispatch(
        request_id="aabbccdd-1234-4abc-9abc-112233445566",
        service_id=SPEC.identifier, trusted_snapshot=snapshot(a),
    )
    assert result == SystemdJobAdmission(
        "aabbccdd-1234-4abc-9abc-112233445566",
        SPEC.identifier, JOB, BOOT,
    )
    # It never waits for a worker result or claims transport success.
    assert a.calls == []
    assert a.started == 100
    assert manager.calls == [SPEC]


def test_dispatch_is_disabled_by_default():
    a = FakeUnits(timer_state="paused")
    manager = FakeManager()
    target = RunOnceJobDispatcher(
        adapter=a, manager=manager, maintenance_gate=lambda: None,
    )
    with pytest.raises(BrokerRejected, match="systemd_dispatch_disabled"):
        target.dispatch(
            request_id="aabbccdd-1234-4abc-9abc-112233445566",
            service_id=SPEC.identifier, trusted_snapshot=snapshot(a),
        )
    assert manager.calls == []


def test_requires_root_before_looking_at_systemd(monkeypatch):
    a = FakeUnits(timer_state="paused")
    mgr = FakeManager()
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    d = dispatcher(a, mgr, require_root=True)
    with pytest.raises(BrokerRejected, match="privileged_dispatch_required"):
        d.dispatch(
            request_id="aabbccdd-1234-4abc-9abc-112233445566",
            service_id=SPEC.identifier, trusted_snapshot=snapshot(a),
        )
    assert mgr.calls == []


@pytest.mark.parametrize("case", [
    "scheduled", "running",
])
def test_busy_or_scheduled_run_once_never_submits(case):
    a = FakeUnits(timer_state="paused" if case == "running" else "scheduled",
                  service_state="running" if case == "running" else "succeeded")
    mgr = FakeManager()
    with pytest.raises(BrokerRejected):
        dispatcher(a, mgr).dispatch(
            request_id="aabbccdd-1234-4abc-9abc-112233445566",
            service_id=SPEC.identifier, trusted_snapshot=snapshot(a),
        )
    assert mgr.calls == []


def test_exact_live_target_unit_identity_or_job_must_be_idle():
    for field, mutation in (
        ("FragmentPath", "/tmp/forged.service"),
        ("MainPID", "9000"),
        ("Job", "42"),
    ):
        a = FakeUnits(timer_state="paused")
        a.override_service[field] = mutation
        mgr = FakeManager()
        with pytest.raises(BrokerRejected):
            dispatcher(a, mgr).dispatch(
                request_id="aabbccdd-1234-4abc-9abc-112233445566",
                service_id=SPEC.identifier, trusted_snapshot=snapshot(a),
            )
        assert not mgr.calls


def test_maintenance_lock_spans_live_state_check_and_submit():
    a = FakeUnits(timer_state="paused")
    held = {"yes": False}
    @contextmanager
    def lock():
        held["yes"] = True
        try:
            yield
        finally:
            held["yes"] = False
    original = a.observe
    def locked(spec):
        assert held["yes"]
        return original(spec)
    a.observe = locked
    mgr = FakeManager()
    def submit(spec):
        assert held["yes"]
        return JOB
    mgr.start_unit = submit
    # Snapshot is acquired by trusted worker *before* taking this admission
    # gate; the actual check+DBus submission both occur under it.
    local = snapshot(FakeUnits(timer_state="paused"))
    target = dispatcher(a, mgr, gate=lock)
    assert target.dispatch(
        request_id="aabbccdd-1234-4abc-9abc-112233445566",
        service_id=SPEC.identifier, trusted_snapshot=local,
    ).job_path == JOB
    assert held["yes"] is False


def test_job_path_malformed_unknown_or_failed_manager_fail_closed():
    for response in (
        "/org/freedesktop/systemd1/unit/sshd_2eservice",
        "/org/freedesktop/systemd1/job/0",
        "/org/freedesktop/systemd1/job/12; rm /",
        "/tmp/stale", "Job accepted", "", None,
        RuntimeError("Bearer PRIVATE_DATA_SHOULD_NOT_LEAK"),
    ):
        a = FakeUnits(timer_state="paused")
        mgr = FakeManager(reply=response)
        with pytest.raises(BrokerRejected) as exc:
            dispatcher(a, mgr).dispatch(
                request_id="aabbccdd-1234-4abc-9abc-112233445566",
                service_id=SPEC.identifier, trusted_snapshot=snapshot(a),
            )
        assert "PRIVATE_DATA" not in str(exc.value)


def test_boot_change_after_submit_does_not_claim_success():
    a = FakeUnits(timer_state="paused")
    mgr = FakeManager()
    def reboot(spec):
        a.boot = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        return JOB
    mgr.start_unit = reboot
    with pytest.raises(BrokerRejected, match="systemd_admission_unconfirmed"):
        dispatcher(a, mgr).dispatch(
            request_id="aabbccdd-1234-4abc-9abc-112233445566",
            service_id=SPEC.identifier, trusted_snapshot=snapshot(a),
        )


@pytest.mark.parametrize("invalid", [
    SystemdJobAdmission("wrong", SPEC.identifier, JOB, BOOT),
    SystemdJobAdmission("aabbccdd-1234-4abc-9abc-112233445566", "sshd", JOB, BOOT),
    SystemdJobAdmission("aabbccdd-1234-4abc-9abc-112233445566",
                        SPEC.identifier, "/tmp/42", BOOT),
    SystemdJobAdmission("aabbccdd-1234-4abc-9abc-112233445566",
                        SPEC.identifier, JOB, "not-a-boot"),
])
def test_job_admission_is_exact_typed_fixed_identity(invalid):
    with pytest.raises(BrokerRejected, match="systemd_admission_unverifiable"):
        verify_job_admission(
            invalid,
            request_id="aabbccdd-1234-4abc-9abc-112233445566",
            service_id=SPEC.identifier,
        )


def popen_fixture(content: bytes, *, rc=0):
    record = []
    class FakeProcess:
        def __init__(self):
            self.stdout = tempfile.TemporaryFile(mode="w+b")
            self.stdout.write(content)
            self.stdout.seek(0)
            self.returncode = rc
        def poll(self): return self.returncode
        def wait(self, timeout=None): return self.returncode
        def kill(self): self.returncode = -9
    def factory(args, **kwargs):
        record.append((args, kwargs))
        return FakeProcess()
    return factory, record


def test_busctl_manager_job_mode_fail_and_strict_fixed_command():
    factory, calls = popen_fixture(b'o "' + JOB.encode() + b'"\n')
    result = BusctlStartUnitManager(popen=factory).start_unit(SPEC)
    assert result == JOB
    assert calls == [(
        (
            "/usr/bin/busctl", "--system", "call",
            "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager", "StartUnit",
            "ss", SPEC.service, "fail",
        ),
        {
            "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL, "close_fds": True,
            "env": {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        },
    )]


@pytest.mark.parametrize("bad,rc", [
    (b'o "/org/freedesktop/systemd1/job/3" trailing\n', 0),
    (b'o "/org/freedesktop/systemd1/job/0"\n', 0),
    (b'o "/org/freedesktop/systemd1/job/3"\n', 1),
    (b'PRIVATE-SECRET', 0),
    (b'A' * (MAX_ADMISSION_REPLY_BYTES + 1), 0),
    (b'\xff', 0),
    (b'', 0),
])
def test_busctl_invalid_exit_unbounded_or_unparseable_response_is_uncertain(bad, rc):
    factory, _ = popen_fixture(bad, rc=rc)
    with pytest.raises(BrokerRejected, match="systemd_admission_unconfirmed") as exc:
        BusctlStartUnitManager(popen=factory).start_unit(SPEC)
    assert "PRIVATE-SECRET" not in str(exc.value)


def test_busctl_rejects_any_unit_outside_fixed_service_allowlist():
    from obsidian_automation.admin_status import ServiceSpec
    spec = ServiceSpec("sshd", "untrusted", "sshd.timer", "sshd.service")
    factory, calls = popen_fixture(b'o "' + JOB.encode() + b'"\n')
    with pytest.raises(BrokerRejected, match="unknown_service"):
        BusctlStartUnitManager(popen=factory).start_unit(spec)
    assert not calls


def test_busctl_custom_executable_disallowed():
    with pytest.raises(BrokerRejected, match="systemd_manager_binary_unqualified"):
        BusctlStartUnitManager(executable="/tmp/fake-busctl")
