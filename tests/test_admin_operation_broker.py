"""Synthetic only: never accesses host systemd, Nextcloud, Vault or root."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Any

import pytest

from obsidian_automation.admin_operation_broker import (
    BrokerRejected, LiveUnits, OperationBroker, SERVICES, SystemctlAdapter,
    TIMER_READ_FIELDS, SERVICE_READ_FIELDS, _command,
)
from obsidian_automation.admin_operation_preflight import MAX_OBSERVATION_AGE
from obsidian_automation.admin_workflow_quiescence import (
    GROUP_UNITS, TIMER_TARGETS, WorkflowObservation, WorkflowNotQuiescent,
)


NOW = datetime(2026, 10, 16, 7, 0, tzinfo=timezone.utc)
RID = "aabbccdd-1234-4abc-9abc-112233445566"
RID2 = "bbccddee-4567-4abc-8abc-112233445566"
SPEC = SERVICES[2]  # github-sync, fixed synthetic only
BOOT = "12345678-1234-1234-8234-123456789abc"


class FakeUnits:
    def __init__(
        self, *, timer_state="scheduled", service_state="succeeded",
        fail_action=False, raise_action=False, wrong_after=False,
        post_failure=False, reboot_after=False,
    ):
        self.timer_state = timer_state
        self.service_state = service_state
        self.fail_action = fail_action
        self.raise_action = raise_action
        self.wrong_after = wrong_after
        self.post_failure = post_failure
        self.reboot_after = reboot_after
        self.started = 100 if service_state == "succeeded" else 0
        self.calls: list[tuple[str, ...]] = []
        self.observed = 0
        self.override_timer = {}
        self.override_service = {}
        self.boot = BOOT

    def boot_id(self):
        return self.boot

    def observe(self, spec):
        assert spec == SPEC
        self.observed += 1
        timer = {
            "LoadState": "loaded",
            "UnitFileState": "enabled" if self.timer_state == "scheduled" else "disabled",
            "ActiveState": "active" if self.timer_state == "scheduled" else "inactive",
            "SubState": "waiting" if self.timer_state == "scheduled" else "dead",
            "Unit": spec.service, "Job": "0",
            "FragmentPath": f"/etc/systemd/system/{spec.timer}",
        }
        service = {
            "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
            "Type": "oneshot", "Result": "success",
            "ExecMainCode": "1" if self.service_state == "succeeded" else "0",
            "ExecMainStatus": "0",
            "ExecMainStartTimestampMonotonic": str(self.started),
            "ExecMainExitTimestampMonotonic": str(self.started + 1 if self.started else 0),
            "Job": "0", "MainPID": "0", "ControlPID": "0",
            "FragmentPath": f"/etc/systemd/system/{spec.service}",
        }
        if self.service_state == "failed":
            service["ActiveState"] = "failed"
            service["Result"] = "exit-code"
            service["ExecMainCode"] = "1"
            service["ExecMainStatus"] = "1"
        if self.service_state == "running":
            service["ActiveState"] = "activating"
            service["SubState"] = "start"
            service["Job"] = "75"
            service["MainPID"] = "4000"
        timer.update(self.override_timer)
        service.update(self.override_service)
        return LiveUnits(timer=timer, service=service)

    def apply(self, argv):
        self.calls.append(argv)
        if self.raise_action:
            raise RuntimeError("PASSWORD=DO_NOT_LEAK_THIS_EXCEPTION")
        if self.fail_action:
            return False
        if not self.wrong_after:
            if argv == _command("pause", SPEC):
                self.timer_state = "paused"
            elif argv == _command("resume", SPEC):
                self.timer_state = "scheduled"
            elif argv == _command("run_once", SPEC):
                self.service_state = "failed" if self.post_failure else "succeeded"
                self.started += 100
                if self.reboot_after:
                    self.boot = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            else:
                raise AssertionError("bad command")
        return True


def snapshot(adapter: FakeUnits, at=NOW):
    live = adapter.observe(SPEC)
    schedule = adapter.timer_state
    state = adapter.service_state
    health = (
        "failed" if state == "failed" else
        "paused" if schedule == "paused" else
        "observed_success" if state == "succeeded" else
        "unverified"
    )
    return {
        "generated_at": at.isoformat(timespec="seconds"),
        "services": [{
            "id": SPEC.identifier, "schedule": schedule,
            "latest_execution": state, "health": health,
        }],
    }


def request(action="pause", rid=RID, at=NOW):
    return {
        "version": 1, "request_id": rid, "service_id": SPEC.identifier,
        "action": action, "observed_at": at.isoformat(timespec="seconds"),
    }


def audit(tmp_path):
    root = tmp_path / "admin-journal"
    root.mkdir(mode=0o700)
    return root


def quiescent_workflow(spec):
    role = spec.identifier
    if role in ("ai-vault-pull", "pre-review"):
        timers = (SERVICES[0].timer, SERVICES[1].timer)
    elif role in ("github-sync", "github-daily"):
        timers = (SERVICES[2].timer, SERVICES[3].timer)
    else:
        timers = (SERVICES[4].timer,)
    return WorkflowObservation(
        timers={
            timer: {
                "LoadState": "loaded", "UnitFileState": "disabled",
                "ActiveState": "inactive", "SubState": "dead",
                "Unit": TIMER_TARGETS[timer], "Job": "0",
                "FragmentPath": f"/etc/systemd/system/{timer}",
            }
            for timer in timers
        },
        services={
            name: {
                "LoadState": "loaded", "ActiveState": "inactive",
                "SubState": "dead", "Type": "oneshot", "Job": "0",
                "MainPID": "0", "ControlPID": "0",
                "FragmentPath": f"/etc/systemd/system/{name}",
            }
            for name in GROUP_UNITS[role]
        },
    )


def configured(
    root, adapter, *, enabled=True, gate=None, clock=None, require_root=False,
    workflow_observer=quiescent_workflow,
):
    if gate is None:
        @contextmanager
        def gate():
            yield
    return OperationBroker(
        audit_root=root, adapter=adapter, maintenance_gate=gate,
        clock=clock or (lambda: NOW), require_root=require_root,
        effects_enabled=enabled, workflow_observer=workflow_observer,
    )


def receipt(root, name, rid=RID):
    return json.loads((root / f"{name}-{rid}.json").read_text())


def test_disabled_by_default_even_with_valid_request(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    broker = configured(root, adapter, enabled=False)
    with pytest.raises(BrokerRejected, match="effect_authority_disabled"):
        broker.apply(request(), snapshot(adapter))
    assert adapter.calls == []
    assert not list(root.iterdir())


def test_requires_root_by_default(tmp_path, monkeypatch):
    root, adapter = audit(tmp_path), FakeUnits()
    broker = configured(root, adapter, require_root=True)
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    with pytest.raises(BrokerRejected, match="privileged_broker_required"):
        broker.apply(request(), snapshot(adapter))
    assert adapter.calls == []


def test_pause_and_resume_have_durable_original_intent_and_outcome(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    broker = configured(root, adapter)
    result = broker.apply(request(), snapshot(adapter))
    assert result.public()["state"] == "completed"
    assert adapter.calls == [_command("pause", SPEC)]
    assert adapter.timer_state == "paused"
    assert not (root / "pending.json").exists()
    assert receipt(root, "request")["state"] == "pending"
    assert receipt(root, "result")["state"] == "completed"
    assert all(p.stat().st_mode & 0o077 == 0 for p in root.iterdir())
    result2 = broker.apply(request("resume", RID2), snapshot(adapter))
    assert result2.public()["action"] == "resume"
    assert adapter.calls[-1] == _command("resume", SPEC)
    assert adapter.timer_state == "scheduled"
    assert receipt(root, "result", RID2)["state"] == "completed"
    assert not (root / "pending.json").exists()


def test_paused_only_run_once_requires_new_successful_attempt(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits(timer_state="paused")
    broker = configured(root, adapter)
    result = broker.apply(request("run_once"), snapshot(adapter))
    assert result.state == "completed"
    assert result.authority == "local_effect_observed"
    assert not (root / "pending.json").exists()
    assert receipt(root, "result")["state"] == "completed"
    assert adapter.calls == [_command("run_once", SPEC)]
    assert adapter.started == 200
    assert adapter.timer_state == "paused"
    assert broker.lookup_result(RID).state == "completed"
    assert adapter.calls == [_command("run_once", SPEC)]

def test_run_once_rejected_when_timer_scheduled(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    broker = configured(root, adapter)
    with pytest.raises(BrokerRejected, match="operation_state_mismatch"):
        broker.apply(request("run_once"), snapshot(adapter))
    assert adapter.calls == []


def test_run_once_failed_service_denied_even_when_paused(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits(timer_state="paused", service_state="failed")
    broker = configured(root, adapter)
    with pytest.raises(BrokerRejected, match="operation_state_mismatch"):
        broker.apply(request("run_once"), snapshot(adapter))
    assert adapter.calls == []


def test_repeated_uuid_never_reexecutes(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    broker = configured(root, adapter)
    old = request()
    broker.apply(old, snapshot(adapter))
    with pytest.raises(BrokerRejected, match="pending_or_duplicate_operation"):
        broker.apply(old, snapshot(FakeUnits()))
    assert len(adapter.calls) == 1


def test_different_id_blocked_by_indeterminate_prior_effect(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits(fail_action=True)
    broker = configured(root, adapter)
    with pytest.raises(BrokerRejected, match="effect_indeterminate"):
        broker.apply(request(), snapshot(adapter))
    assert receipt(root, "result")["state"] == "indeterminate"
    assert (root / "pending.json").exists()
    with pytest.raises(BrokerRejected, match="pending_or_duplicate_operation"):
        broker.apply(request(rid=RID2), snapshot(adapter))
    assert len(adapter.calls) == 1


def test_exception_after_effect_preserves_pending_and_sanitizes_logs(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits(raise_action=True)
    broker = configured(root, adapter)
    with pytest.raises(BrokerRejected, match="effect_indeterminate") as exc:
        broker.apply(request(), snapshot(adapter))
    assert "PASSWORD" not in str(exc.value)
    data = "".join(p.read_text() for p in root.iterdir() if p.suffix == ".json")
    assert "DO_NOT_LEAK" not in data
    assert receipt(root, "result")["state"] == "indeterminate"


def test_false_postcondition_keeps_pending_for_manual_reconciliation(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits(wrong_after=True)
    broker = configured(root, adapter)
    with pytest.raises(BrokerRejected, match="effect_indeterminate"):
        broker.apply(request(), snapshot(adapter))
    assert len(adapter.calls) == 1
    assert (root / "pending.json").exists()
    assert receipt(root, "result")["state"] == "indeterminate"


@pytest.mark.parametrize("key,val", [
    ("Unit", "evil-daemon.service"),
    ("Job", "7"),
    ("LoadState", "not-found"),
    ("UnitFileState", "masked"),
    ("FragmentPath", "/tmp/evil.timer"),
    ("SubState", "running"),
])
def test_broker_refuses_timer_identity_job_or_unstable_state(tmp_path, key, val):
    root, adapter = audit(tmp_path), FakeUnits()
    snap = snapshot(adapter)
    adapter.override_timer[key] = val
    broker = configured(root, adapter)
    with pytest.raises(BrokerRejected):
        broker.apply(request(), snap)
    assert adapter.calls == []
    assert not (root / "pending.json").exists()


@pytest.mark.parametrize("key,val", [
    ("Job", "2"),
    ("MainPID", "42"),
    ("ControlPID", "33"),
    ("LoadState", "masked"),
    ("FragmentPath", "/tmp/other.service"),
    ("Type", "notify"),
    ("SubState", "running"),
])
def test_broker_refuses_busy_or_untrusted_service(tmp_path, key, val):
    root, adapter = audit(tmp_path), FakeUnits()
    snap = snapshot(adapter)
    adapter.override_service[key] = val
    broker = configured(root, adapter)
    with pytest.raises(BrokerRejected):
        broker.apply(request(), snap)
    assert adapter.calls == []


def test_browser_preview_stale_or_not_matching_live_status(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    broker = configured(root, adapter)
    stale = snapshot(adapter, NOW - MAX_OBSERVATION_AGE - timedelta(seconds=1))
    with pytest.raises(BrokerRejected, match="stale_observation"):
        broker.apply(request(at=NOW - timedelta(seconds=31)), stale)
    fresh = snapshot(adapter)
    adapter.timer_state = "paused"
    with pytest.raises(BrokerRejected, match="live_state_changed"):
        broker.apply(request(), fresh)
    assert adapter.calls == []
    assert not (root / "pending.json").exists()


def test_unknown_service_and_extra_fields_do_not_create_journal(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    broker = configured(root, adapter)
    payload = request()
    payload["service_id"] = "sshd; reboot"
    with pytest.raises(BrokerRejected, match="unknown_service"):
        broker.apply(payload, snapshot(adapter))
    payload = request()
    payload["secret"] = "canary"
    with pytest.raises(BrokerRejected, match="invalid_request"):
        broker.apply(payload, snapshot(adapter))
    assert not list(root.iterdir())


def test_gate_failure_blocks_before_journal_or_systemctl(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    @contextmanager
    def deny():
        raise BrokerRejected("maintenance_authority_unavailable")
        yield
    broker = configured(root, adapter, gate=deny)
    with pytest.raises(BrokerRejected, match="maintenance_authority_unavailable"):
        broker.apply(request(), snapshot(adapter))
    assert not (root / "pending.json").exists()
    assert adapter.calls == []


def test_root_permissions_and_symlinks_are_rejected(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    root.chmod(0o750)
    with pytest.raises(BrokerRejected, match="unsafe_audit_root"):
        configured(root, adapter).apply(request(), snapshot(adapter))
    root.chmod(0o700)
    bad = tmp_path / "symlink"
    bad.symlink_to(root, target_is_directory=True)
    with pytest.raises(BrokerRejected, match="unsafe_audit_root"):
        configured(bad, adapter).apply(request(), snapshot(adapter))
    assert adapter.calls == []


def test_broker_lock_symlink_is_fail_closed(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    (root / "broker.lock").symlink_to(root / "target")
    with pytest.raises(BrokerRejected):
        configured(root, adapter).apply(request(), snapshot(adapter))
    assert adapter.calls == []


def test_existing_pending_marker_cannot_be_bypassed(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    (root / "pending.json").symlink_to("missing")
    with pytest.raises(BrokerRejected, match="pending_or_duplicate_operation"):
        configured(root, adapter).apply(request(), snapshot(adapter))
    assert adapter.calls == []


def test_nonblocking_same_journal_lock(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    lock = root / "broker.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(BrokerRejected, match="another_operation_active"):
            configured(root, adapter).apply(request(), snapshot(adapter))
        assert adapter.calls == []
    finally:
        os.close(fd)


def test_root_is_not_automatically_created(tmp_path):
    missing = tmp_path / "journal-missing"
    adapter = FakeUnits()
    with pytest.raises(BrokerRejected, match="unsafe_audit_root"):
        configured(missing, adapter).apply(request(), snapshot(adapter))
    assert not missing.exists()


@pytest.mark.parametrize("bad", [
    ("systemctl", "start", "sshd.service"),
    ("systemctl", "stop", "obsidian-core-promotion.service"),
    ("systemctl", "disable", "--now", "malicious.timer"),
    ("sh", "-c", "systemctl start foo"),
])
def test_systemctl_adapter_enforces_command_allowlist_without_exec(bad):
    calls = []
    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("must not run")
    a = SystemctlAdapter(runner=runner)
    with pytest.raises(BrokerRejected, match="command_not_allowlisted"):
        a.apply(bad)
    assert calls == []


def test_systemctl_adapter_runs_exact_args_without_shell():
    calls = []
    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    a = SystemctlAdapter(runner=runner)
    assert a.apply(_command("pause", SPEC))
    assert calls[0][0] == (
        "/usr/bin/systemctl", "disable", "--now", SPEC.timer,
    )
    assert calls[0][1]["timeout"] == 15
    assert calls[0][1]["stdout"] is subprocess.DEVNULL
    assert calls[0][1]["stderr"] is subprocess.DEVNULL
    assert calls[0][1]["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    assert "shell" not in calls[0][1]


def fake_show_process(payload_func, result_code=0):
    """Popen double backed by a real fd, but never launches subprocesses."""
    observed = []
    class FakeProcess:
        def __init__(self, args, **kwargs):
            self.stdout = tempfile.TemporaryFile(mode="w+b")
            self.stdout.write(payload_func(args).encode("utf-8"))
            self.stdout.seek(0)
            self.returncode = result_code
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            return self.returncode
        def kill(self):
            self.returncode = -9
    def factory(args, **kwargs):
        assert args[0] == "/usr/bin/systemctl"
        assert args[1] == "show"
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["stdin"] is subprocess.DEVNULL
        observed.append((args, kwargs))
        return FakeProcess(args, **kwargs)
    return factory, observed


def test_systemctl_observation_rejects_missing_or_extra_properties():
    factory, events = fake_show_process(lambda args: "LoadState=loaded\nBad=evil\n")
    adapter = SystemctlAdapter(popen=factory)
    with pytest.raises(BrokerRejected, match="systemd_observation_failed"):
        adapter.observe(SPEC)
    assert len(events) == 1


def test_immutable_result_lookup_does_not_rerun_effect(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits(timer_state="paused")
    broker = configured(root, adapter)
    assert broker.apply(request("run_once"), snapshot(adapter)).state == "completed"
    for _ in range(3):
        assert broker.lookup_result(RID).state == "completed"
    assert adapter.calls == [_command("run_once", SPEC)]

def test_boot_change_during_effect_is_indeterminate_not_success(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits(timer_state="paused", reboot_after=True)
    broker = configured(root, adapter)
    with pytest.raises(BrokerRejected, match="effect_indeterminate"):
        broker.apply(request("run_once"), snapshot(adapter))
    assert (root / "pending.json").exists()
    assert receipt(root, "result")["state"] == "indeterminate"
    assert broker.lookup_result(RID).state == "indeterminate"

def test_run_once_failure_is_indeterminate_and_blocks_new_commands(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits(timer_state="paused", post_failure=True)
    broker = configured(root, adapter)
    with pytest.raises(BrokerRejected, match="effect_indeterminate"):
        broker.apply(request("run_once"), snapshot(adapter))
    assert receipt(root, "result")["state"] == "indeterminate"
    assert (root / "pending.json").exists()
    assert broker.lookup_result(RID).state == "indeterminate"
    assert broker.lookup_result(RID).authority == "manual_reconciliation_required"
    assert adapter.calls == [_command("run_once", SPEC)]

def test_maintenance_gate_wait_does_not_let_expired_snapshot_apply(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    time = [NOW]
    @contextmanager
    def delayed():
        time[0] = NOW + timedelta(seconds=31)
        yield
    broker = configured(root, adapter, gate=delayed, clock=lambda: time[0])
    with pytest.raises(BrokerRejected, match="stale_observation"):
        broker.apply(request(), snapshot(adapter))
    assert adapter.calls == []
    assert not (root / "pending.json").exists()


def test_maintenance_gate_exception_message_is_redacted(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    @contextmanager
    def broken():
        raise RuntimeError("PASSWORD=PRIVATE_GATE_TOKEN")
        yield
    broker = configured(root, adapter, gate=broken)
    with pytest.raises(BrokerRejected, match="maintenance_or_observation_failed") as e:
        broker.apply(request(), snapshot(adapter))
    assert "PRIVATE_GATE_TOKEN" not in str(e.value)
    assert not (root / "pending.json").exists()
    assert adapter.calls == []


def test_initial_adapter_exception_message_is_redacted(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    old_observe = adapter.observe
    original_snapshot = snapshot(adapter)
    def broken(_):
        raise RuntimeError("password=PRIVATE_ADAPTER_TOKEN")
    adapter.observe = broken
    broker = configured(root, adapter)
    with pytest.raises(BrokerRejected, match="maintenance_or_observation_failed") as e:
        broker.apply(request(), original_snapshot)
    assert "PRIVATE_ADAPTER_TOKEN" not in str(e.value)
    assert adapter.calls == []
    assert not (root / "pending.json").exists()
    adapter.observe = old_observe


def test_missing_boot_identity_rejected_before_journal(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    snapshot_value = snapshot(adapter)
    adapter.boot = "attacker-controlled-invalid"
    broker = configured(root, adapter)
    with pytest.raises(BrokerRejected, match="boot_identity_unavailable"):
        broker.apply(request(), snapshot_value)
    assert adapter.calls == []
    assert not (root / "pending.json").exists()


def test_post_observation_identity_change_keeps_pending(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits(timer_state="paused")
    original_apply = adapter.apply
    def corrupt_after(argv):
        ok = original_apply(argv)
        adapter.override_service["FragmentPath"] = "/tmp/tampered.service"
        return ok
    adapter.apply = corrupt_after
    broker = configured(root, adapter)
    with pytest.raises(BrokerRejected, match="effect_indeterminate"):
        broker.apply(request("run_once"), snapshot(adapter))
    assert (root / "pending.json").exists()
    assert broker.lookup_result(RID).state == "indeterminate"

def test_systemctl_run_once_waits_full_oneshot_with_bounded_timeout():
    calls = []
    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    adapter = SystemctlAdapter(runner=runner)
    assert adapter.apply(_command("run_once", SPEC))
    assert calls[0][0] == ("/usr/bin/systemctl", "start", SPEC.service)
    assert calls[0][1]["timeout"] == 7200
    assert calls[0][1]["stdout"] is subprocess.DEVNULL
    assert calls[0][1]["stderr"] is subprocess.DEVNULL
    assert "shell" not in calls[0][1]



def test_completed_gate_exit_exception_can_be_recovered_by_receipt(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    @contextmanager
    def failed_exit():
        yield
        raise RuntimeError("SECRET=NO_LOGGING")
    broker = configured(root, adapter, gate=failed_exit)
    with pytest.raises(BrokerRejected, match="maintenance_or_observation_failed") as e:
        broker.apply(request(), snapshot(adapter))
    assert "NO_LOGGING" not in str(e.value)
    assert adapter.calls == [_command("pause", SPEC)]
    assert not (root / "pending.json").exists()
    assert broker.lookup_result(RID).state == "completed"


def test_receipt_lookup_is_rejected_on_corruption(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    broker = configured(root, adapter)
    broker.apply(request(), snapshot(adapter))
    (root / f"result-{RID}.json").unlink()
    (root / f"result-{RID}.json").symlink_to(root / "secrets")
    with pytest.raises(BrokerRejected):
        broker.lookup_result(RID)
    assert adapter.calls == [_command("pause", SPEC)]


def test_journal_result_lookup_requires_strict_uuid(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    broker = configured(root, adapter)
    with pytest.raises(BrokerRejected, match="invalid_request_id"):
        broker.lookup_result("../../etc/passwd")
    assert not list(root.iterdir())


def test_systemctl_adapter_reads_only_fixed_bounded_unit_properties():
    source = FakeUnits()
    expected = source.observe(SPEC)
    def response(args):
        fields = [arg.partition("=")[2] for arg in args[4:]]
        values = expected.timer if args[2] == SPEC.timer else expected.service
        assert set(fields) == set(values)
        return "".join(f"{key}={values[key]}\n" for key in fields)
    factory, received = fake_show_process(response)
    live = SystemctlAdapter(popen=factory).observe(SPEC)
    assert live == expected
    assert len(received) == 2
    assert all(r[1]["env"] == {
        "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
    } for r in received)


def test_systemctl_timeout_is_indeterminate_not_accepted():
    from subprocess import TimeoutExpired
    def runner(args, **kwargs):
        raise TimeoutExpired(args, kwargs["timeout"])
    assert not SystemctlAdapter(runner=runner).apply(_command("run_once", SPEC))


def test_completed_result_with_interrupted_pending_cleanup_is_recoverable(tmp_path, monkeypatch):
    root, adapter = audit(tmp_path), FakeUnits()
    broker = configured(root, adapter)
    original_unlink = os.unlink
    def fail_pending_cleanup(path, **kwargs):
        if path == "pending.json":
            raise OSError("sensitive-journal-path-must-not-echo")
        return original_unlink(path, **kwargs)
    monkeypatch.setattr(os, "unlink", fail_pending_cleanup)
    with pytest.raises(BrokerRejected, match="maintenance_or_observation_failed") as exc:
        broker.apply(request(), snapshot(adapter))
    assert "sensitive-" not in str(exc.value)
    assert broker.lookup_result(RID).state == "completed_pending_cleanup"
    assert broker.lookup_result(RID).authority == "manual_reconciliation_required"
    assert adapter.calls == [_command("pause", SPEC)]


def test_preexisting_hardlinked_lock_is_rejected(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    (root / "broker.lock").touch(mode=0o600)
    os.link(root / "broker.lock", root / "hardlink")
    with pytest.raises(BrokerRejected):
        configured(root, adapter).apply(request(), snapshot(adapter))
    assert adapter.calls == []


def test_systemctl_show_kills_over_bound_stdout_without_buffering():
    factory, observed = fake_show_process(lambda args: "A" * 50000)
    with pytest.raises(BrokerRejected, match="systemd_observation_failed"):
        SystemctlAdapter(popen=factory).observe(SPEC)
    assert len(observed) == 1


def test_systemctl_show_rejects_nonzero_child_exit():
    factory, _ = fake_show_process(lambda args: "", result_code=42)
    with pytest.raises(BrokerRejected, match="systemd_observation_failed"):
        SystemctlAdapter(popen=factory).observe(SPEC)


def test_run_once_requires_explicit_workflow_observer_even_if_effects_enabled(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits(timer_state="paused")
    broker = configured(root, adapter, workflow_observer=None)
    with pytest.raises(BrokerRejected, match="workflow_observer_unavailable"):
        broker.apply(request("run_once"), snapshot(adapter))
    assert adapter.calls == []
    assert not (root / "pending.json").exists()


def test_run_once_fails_before_journal_when_other_role_timer_scheduled(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits(timer_state="paused")
    def raced(spec):
        result = quiescent_workflow(spec)
        result.timers[SERVICES[3].timer]["UnitFileState"] = "enabled"
        result.timers[SERVICES[3].timer]["ActiveState"] = "active"
        result.timers[SERVICES[3].timer]["SubState"] = "waiting"
        return result
    broker = configured(root, adapter, workflow_observer=raced)
    with pytest.raises(BrokerRejected, match="workflow_timer_not_quiescent"):
        broker.apply(request("run_once"), snapshot(adapter))
    assert adapter.calls == []
    assert not (root / "pending.json").exists()


def test_run_once_blocks_on_busy_hidden_prerequisite(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits(timer_state="paused")
    def hidden_running(spec):
        result = quiescent_workflow(spec)
        result.services["obsidian-github-daily-collect.service"]["Job"] = "4"
        return result
    broker = configured(root, adapter, workflow_observer=hidden_running)
    with pytest.raises(BrokerRejected, match="workflow_service_not_quiescent"):
        broker.apply(request("run_once"), snapshot(adapter))
    assert adapter.calls == []
    assert not (root / "pending.json").exists()


def test_workflow_observer_exception_must_not_leak_secret_or_write_journal(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits(timer_state="paused")
    def broken(_):
        raise RuntimeError("admin-broker-bearer=secret-exception-value")
    broker = configured(root, adapter, workflow_observer=broken)
    with pytest.raises(BrokerRejected, match="workflow_observation_failed") as exc:
        broker.apply(request("run_once"), snapshot(adapter))
    assert "secret-" not in str(exc.value)
    assert adapter.calls == []
    assert not (root / "pending.json").exists()


def test_pause_and_resume_not_blocked_by_run_once_dependency_snapshot(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits()
    def forbidden(_):
        raise RuntimeError("must not inspect unrelated dependencies for pause")
    broker = configured(root, adapter, workflow_observer=forbidden)
    assert broker.apply(request("pause"), snapshot(adapter)).state == "completed"
    assert broker.apply(request("resume", RID2), snapshot(adapter)).state == "completed"


def test_systemctl_adapter_observes_only_full_fixed_github_domain():
    from obsidian_automation.admin_workflow_quiescence import (
        WORKFLOW_READ_FIELDS, TIMER_READ_FIELDS, require_run_once_quiescent,
    )
    expected = quiescent_workflow(SPEC)
    seen = set()
    def payload(args):
        unit = args[2]
        fields = [arg.partition("=")[2] for arg in args[4:]]
        assert unit in set(expected.timers) | set(expected.services)
        observed = expected.timers.get(unit) or expected.services[unit]
        assert set(fields) == set(observed)
        seen.add(unit)
        return "".join(f"{key}={observed[key]}\n" for key in fields)
    factory, _events = fake_show_process(payload)
    result = SystemctlAdapter(popen=factory).observe_workflow(SPEC)
    require_run_once_quiescent(SPEC, result)
    assert seen == set(expected.timers) | set(expected.services)


def test_workflow_observer_cannot_inject_custom_rejection_code(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits(timer_state="paused")
    def malicious(_):
        raise WorkflowNotQuiescent("Authorization: bearer=private-data")
    broker = configured(root, adapter, workflow_observer=malicious)
    with pytest.raises(BrokerRejected, match="workflow_observation_failed") as exc:
        broker.apply(request("run_once"), snapshot(adapter))
    assert "private-data" not in str(exc.value)
    assert "Authorization" not in str(exc.value)
    assert adapter.calls == []
    assert not (root / "pending.json").exists()


def test_workflow_observer_returned_mapping_cannot_inject_error_code(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits(timer_state="paused")
    class MaliciousMapping(dict):
        def __iter__(self):
            raise WorkflowNotQuiescent("Bearer private-token-from-unsafe-mapping")
    def malicious(_):
        original = quiescent_workflow(SPEC)
        return WorkflowObservation(
            timers=MaliciousMapping(original.timers),
            services=original.services,
        )
    broker = configured(root, adapter, workflow_observer=malicious)
    with pytest.raises(BrokerRejected, match="workflow_inventory_unverified") as exc:
        broker.apply(request("run_once"), snapshot(adapter))
    assert "private-token" not in str(exc.value)
    assert adapter.calls == []
    assert not (root / "pending.json").exists()


def test_workflow_observer_malicious_string_key_cannot_leak_code(tmp_path):
    root, adapter = audit(tmp_path), FakeUnits(timer_state="paused")
    class StrangeKey(str):
        attack = False
        def __hash__(self):
            if self.attack:
                raise WorkflowNotQuiescent("auth-token=must-not-leak")
            return super().__hash__()
    key = StrangeKey(SERVICES[3].timer)
    def malicious(spec):
        original = quiescent_workflow(spec)
        replacement = dict(original.timers)
        current = replacement.pop(SERVICES[3].timer)
        replacement[key] = current
        key.attack = True
        return WorkflowObservation(timers=replacement, services=original.services)
    broker = configured(root, adapter, workflow_observer=malicious)
    with pytest.raises(BrokerRejected, match="workflow_inventory_unverified") as exc:
        broker.apply(request("run_once"), snapshot(adapter))
    assert "must-not-leak" not in str(exc.value)
    assert adapter.calls == []
    assert not (root / "pending.json").exists()


@pytest.mark.parametrize("action, timer_state", [
    ("pause", "scheduled"), ("resume", "paused"),
])
def test_real_systemctl_empty_idle_job_allows_fixed_timer_control(
    tmp_path, action, timer_state,
):
    a = FakeUnits(timer_state=timer_state)
    a.override_timer["Job"] = ""
    a.override_service["Job"] = ""
    broker = configured(audit(tmp_path), a)
    receipt = broker.apply(request(action), snapshot(a))
    assert receipt.state == "completed"
    assert a.calls == [_command(action, SPEC)]
    assert broker.lookup_result(RID).state == "completed"
