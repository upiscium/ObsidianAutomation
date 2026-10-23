"""Disposable-root tests for the exact-revision disabled overlay stager."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_tool():
    path = ROOT / "tools/stage_admin_disabled_quiescence.py"
    spec = importlib.util.spec_from_file_location(
        "stage_admin_disabled_quiescence_tool", path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()
MOUNTINFO = (b"31 1 0:1 / / rw - ext4 /dev/root rw\n"
             b"32 31 0:2 / /run rw - tmpfs tmpfs rw\n")


def _sandbox_parent_fd(root: Path, path: Path, uid: int) -> int:
    """Anchor the tool's fd-relative walk at pytest's private fake root.

    The production walk starts at ``/`` and intentionally rejects writable
    ancestors such as ``/tmp``.  For disposable-root tests, the pytest-owned
    root is the trust anchor; every component below it is still opened with
    O_NOFOLLOW and checked with the production ownership/mode predicates.
    """
    root = Path(root)
    path = Path(path)
    tool.require(path.is_absolute() and ".." not in path.parts, "unsafe_path")
    try:
        relative = path.relative_to(root)
    except ValueError:
        tool.require(False, "unsafe_path")
        raise AssertionError("unreachable")

    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        tool.require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0, uid)
                     and info.st_gid in (0, os.getgid())
                     and not stat.S_IMODE(info.st_mode) & 0o022,
                     "unsafe_parent")
        virtual = Path("/")
        for component in relative.parts[:-1]:
            nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY |
                          os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                virtual /= component
                info = os.fstat(nxt)
                entry = os.stat(component, dir_fd=fd, follow_symlinks=False)
                tool.require((entry.st_dev, entry.st_ino) == (info.st_dev, info.st_ino),
                             "replaced_parent")
                tool.require_parent_identity(info, virtual, uid, tool.web_group_gid(uid))
            except BaseException:
                os.close(nxt)
                raise
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _systemd_snapshot():
    snapshot = {}
    for name in (*tool.TIMERS, *tool.SERVICES, "obsidian-admin-operator.service"):
        common = {
            "LoadState": "loaded",
            "Job": "0",
            "FragmentPath": "/etc/systemd/system/" + name,
        }
        if name.endswith(".timer"):
            snapshot[name] = {
                **common,
                "UnitFileState": "enabled",
                "ActiveState": "active",
                "SubState": "waiting",
            }
        else:
            snapshot[name] = {
                **common,
                "MainPID": "0",
                "ControlPID": "0",
                "UnitFileState": "disabled" if name == "obsidian-admin-operator.service"
                else "enabled",
                "ActiveState": "inactive",
                "SubState": "dead",
            }
    return snapshot


TIMER_SHOW_KEYS = frozenset((
    "LoadState", "ActiveState", "SubState", "Job", "FragmentPath", "UnitFileState",
))
SERVICE_SHOW_KEYS = TIMER_SHOW_KEYS | {"MainPID", "ControlPID"}


def _mock_live_systemctl(monkeypatch, *, fault_unit=None, fault=None):
    """Debian13-like six-field timers/eight-field services, no real systemctl."""
    snapshot = _systemd_snapshot()
    commands = []

    def run(command, *, capture_output, timeout, check):
        command = tuple(command)
        commands.append(command)
        assert command[:2] == ("/usr/bin/systemctl", "show")
        assert command[-1] == "--no-pager"
        assert (capture_output, timeout, check) == (True, 5, True)
        name = command[2]
        assert name in snapshot
        requested = {part[2:] for part in command[3:-1]}
        assert requested == (TIMER_SHOW_KEYS if name in tool.TIMERS else SERVICE_SHOW_KEYS)
        fields = dict(snapshot[name])
        if name == fault_unit and fault is not None:
            fields, raw = fault(fields)
        else:
            raw = None
        if raw is None:
            raw = ("\n".join(f"{key}={value}" for key, value in fields.items()) + "\n").encode()
        return SimpleNamespace(stdout=raw)

    monkeypatch.setattr(tool.subprocess, "run", run)
    return commands


def test_observe_systemd_uses_real_timer_and_service_property_shapes(monkeypatch):
    commands = _mock_live_systemctl(monkeypatch)
    observed = tool.observe_systemd()
    assert len(commands) == 34 == len(observed)
    assert set(observed) == set((*tool.TIMERS, *tool.SERVICES,
                                 "obsidian-admin-operator.service"))
    assert all(set(observed[name]) == TIMER_SHOW_KEYS for name in tool.TIMERS)
    assert all(set(observed[name]) == SERVICE_SHOW_KEYS for name in
               (*tool.SERVICES, "obsidian-admin-operator.service"))
    assert len(tool.validate_systemd(observed)) == 5


@pytest.mark.parametrize(("unit", "fault", "reason"), [
    ("obsidian-github-sync.timer", lambda fields: (dict(
        (key, value) for key, value in fields.items() if key != "Job"), None),
        "systemd_incomplete"),
    ("obsidian-github-sync.timer", lambda fields: (dict(fields, MainPID="0"), None),
        "systemd_incomplete"),
    ("obsidian-github-sync.timer", lambda fields: (fields,
        ("\n".join(f"{key}={value}" for key, value in fields.items()) +
         "\nJob=0\n").encode()), "systemd_incomplete"),
    ("obsidian-github-sync.service", lambda fields: (dict(
        (key, value) for key, value in fields.items() if key != "MainPID"), None),
        "systemd_incomplete"),
    ("obsidian-github-sync.service", lambda fields: (dict(
        (key, value) for key, value in fields.items() if key != "ControlPID"), None),
        "systemd_incomplete"),
    ("obsidian-github-sync.service", lambda fields: (dict(fields, Other="value"), None),
        "systemd_incomplete"),
    ("obsidian-github-sync.timer", lambda fields: (fields,
        ("\n".join(f"{key}={value}" for key, value in fields.items()) + "\n")
        .replace("Job=0\n", "Job\\x\n").encode()), "systemd_malformed"),
    ("obsidian-github-sync.timer", lambda fields: (fields,
        ("\n".join(f"{key}={value}" for key, value in fields.items()) + "\n")
        .encode().replace(b"Job=0\n", b"Job=\xff\n")), "systemd_nonascii"),
])
def test_observe_systemd_rejects_missing_extra_duplicate_or_malformed_properties(
        monkeypatch, unit, fault, reason):
    _mock_live_systemctl(monkeypatch, fault_unit=unit, fault=fault)
    with pytest.raises(tool.Blocked, match=reason):
        tool.observe_systemd()


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("MainPID", "42", "worker_or_domain_active"),
    ("ControlPID", "7", "worker_or_domain_active"),
    ("FragmentPath", "/etc/systemd/system/other.service", "domain_job_or_identity"),
    ("Job", "123", "domain_job_or_identity"),
])
def test_observe_systemd_retains_service_busy_and_identity_gate(
        monkeypatch, field, value, reason):
    def mutate(fields):
        fields[field] = value
        return fields, None

    _mock_live_systemctl(monkeypatch, fault_unit="obsidian-github-sync.service",
                         fault=mutate)
    with pytest.raises(tool.Blocked, match=reason):
        tool.validate_systemd(tool.observe_systemd())


def _queue_proof(*, request_extra=None):
    request = {"record_version": 1, "request_id": tool.OLD_ID,
               "service_id": "github-sync", "action": "run_once",
               "state": "accepted", "queued_at": "2026-10-22T00:00:00+00:00"}
    if request_extra:
        request.update(request_extra)
    request_bytes = tool.canonical(request)
    processing_bytes = tool.canonical({"record_version": 1, "request_id": tool.OLD_ID,
        "service_id": "github-sync", "action": "run_once", "state": "processing"})
    result_bytes = tool.canonical({"record_version": 1, "request_id": tool.OLD_ID,
        "service_id": "github-sync", "action": "run_once", "state": "indeterminate"})
    request_hash = tool.digest(request_bytes)
    processing_hash = tool.digest(processing_bytes)
    result_hash = tool.digest(result_bytes)
    evidence_bytes = tool.canonical({
        "record_version": 1, "request_id": tool.OLD_ID,
        "service_id": "github-sync", "action": "run_once",
        "state": "root_verified_manual_evidence",
        "decision": "acknowledge_unknown_no_replay",
        "approval_reference": "https://github.com/upiscium/ObsidianAutomation/issues/442#issuecomment-1",
        "observed_at": "2026-10-22T00:00:00+00:00",
        "boot_id": "12345678-1234-1234-1234-123456789abc",
        "operator_inactive": True, "authority_disabled": True,
        "socket_absent": True, "role_services_idle": True,
        "timer_intents_restored": True, "active_sha256": request_hash,
        "business_outcome": "unknown_no_success_claim",
        "request_sha256": request_hash,
        "processing_sha256": processing_hash,
        "result_sha256": result_hash,
    })
    reconcile_bytes = tool.canonical({
        "record_version": 1, "request_id": tool.OLD_ID,
        "service_id": "github-sync", "action": "run_once",
        "state": "manually_reconciled_unknown",
        "decision": "acknowledge_unknown_no_replay",
        "approval_reference": "https://github.com/upiscium/ObsidianAutomation/issues/442#issuecomment-1",
        "approved_at": "2026-10-22T00:00:00+00:00",
        "boot_id": "12345678-1234-1234-1234-123456789abc",
        "active_sha256": request_hash,
        "request_sha256": request_hash,
        "processing_sha256": processing_hash,
        "result_sha256": result_hash,
        "evidence_sha256": tool.digest(evidence_bytes),
    })
    return dict(zip(tool.OLD_FILES, (
        request_bytes, processing_bytes, result_bytes, evidence_bytes, reconcile_bytes,
    )))


def _pause_proof():
    identity = {"record_version": 1, "request_id": tool.PAUSE_ID,
                "service_id": "github-sync", "action": "pause"}
    return dict(zip(tool.PAUSE_FILES, (
        tool.canonical({**identity, "state": "accepted",
                        "queued_at": "2026-10-22T00:00:00+00:00"}),
        tool.canonical({**identity, "state": "processing"}),
        tool.canonical({**identity, "state": "completed"}),
    )))


def _write(path: Path, data: bytes, mode: int) -> None:
    path.write_bytes(data)
    os.chmod(path, mode)


def _fake_root(tmp_path: Path, monkeypatch, *, boundary=None, source_bytes=None):
    root = tmp_path
    os.chmod(root, 0o700)
    queue_gid = next((group for group in os.getgroups() if group != os.getgid()),
                     os.getgid())
    monkeypatch.setattr(tool, "web_group_gid", lambda uid: queue_gid)

    # These constants are pins in production.  Replace them only in this
    # disposable fixture so its canonical old and candidate manifests are
    # self-consistent without depending on the production OLD_AUTH value.
    old_revision = "1" * 40
    revision = "2" * 40
    old_module = b"# fixture: previously installed module\n"
    source = b"# fixture: exact candidate source bytes\n"
    monkeypatch.setattr(tool, "OLD_REVISION", old_revision)
    monkeypatch.setattr(tool, "REVISION", revision)
    monkeypatch.setattr(tool, "OLD_MODULE", tool.digest(old_module))
    monkeypatch.setattr(tool, "NEW_MODULE", tool.digest(source))
    monkeypatch.setattr(tool, "GIT_BLOB", hashlib.sha1(
        b"blob " + str(len(source)).encode() + b"\0" + source).hexdigest())
    monkeypatch.setattr(tool, "RELEASE", {"version": 1, "release_commit": revision,
        "source_path": "src/obsidian_automation/admin_workflow_quiescence.py",
        "git_blob": tool.GIT_BLOB, "source_sha256": tool.NEW_MODULE})
    monkeypatch.setattr(tool, "STAGE_NAME", "disabled-quiescence-" + revision[:12])

    module_bytes = {
        name: (old_module if name == tool.CHANGED else
               f"# fixture module {name}\n".encode())
        for name in sorted(tool.MODULES)
    }
    unit_bytes = {
        name: f"# fixture systemd unit {name}\n".encode()
        for name in sorted(tool.UNITS)
    }
    old_manifest = {
        "version": 1,
        "enabled": False,
        "revision": old_revision,
        "web_user": "obsidian-admin-web",
        "single_active_lxc": True,
        "unit_sha256": {name: tool.digest(data) for name, data in unit_bytes.items()},
        "module_sha256": {name: tool.digest(data) for name, data in module_bytes.items()},
        "coordinated_updaters_verified": False,
        "uncoordinated_launchers_retired": False,
    }
    old_authority = tool.canonical(old_manifest)
    candidate = dict(old_manifest)
    candidate["module_sha256"] = dict(old_manifest["module_sha256"])
    candidate["module_sha256"][tool.CHANGED] = tool.NEW_MODULE
    candidate["revision"] = revision
    new_authority = tool.canonical(candidate)
    monkeypatch.setattr(tool, "OLD_AUTH", tool.digest(old_authority))
    monkeypatch.setattr(tool, "NEW_AUTH", tool.digest(new_authority))

    paths = tool.Paths(root)
    directories = (
        ("/opt", 0o755),
        ("/opt/obsidian-automation", 0o755),
        ("/opt/obsidian-automation/venv", 0o755),
        ("/opt/obsidian-automation/venv/lib", 0o755),
        ("/opt/obsidian-automation/venv/lib/python3.13", 0o755),
        ("/opt/obsidian-automation/venv/lib/python3.13/site-packages", 0o755),
        ("/opt/obsidian-automation/venv/lib/python3.13/site-packages/obsidian_automation", 0o755),
        ("/etc", 0o755),
        ("/etc/obsidian-admin", 0o710),
        ("/etc/systemd", 0o755),
        ("/etc/systemd/system", 0o755),
        ("/var", 0o755),
        ("/var/lib", 0o755),
        ("/var/lib/obsidian-admin", 0o700),
        ("/var/lib/obsidian-admin/queue", 0o700),
        ("/var/lib/obsidian-admin/operator-stages", 0o700),
        ("/var/lib/obsidian-automation", 0o755),
        ("/var/lib/obsidian-automation/deployments", 0o755),
        ("/run", 0o755),
        ("/run/obsidian-automation", 0o755),
    )
    for name, mode in directories:
        directory = paths.at(name)
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, mode)
    for name in ("/etc/obsidian-admin", "/var/lib/obsidian-admin/queue"):
        os.chown(paths.at(name), os.getuid(), queue_gid)

    package = paths.at(
        "/opt/obsidian-automation/venv/lib/python3.13/site-packages/obsidian_automation"
    )
    for name, data in module_bytes.items():
        _write(package / name, data, 0o644)
    units = paths.at("/etc/systemd/system")
    for name, data in unit_bytes.items():
        _write(units / name, data, 0o644)

    _write(paths.at("/etc/obsidian-admin/authority.json"), old_authority, 0o600)
    for name, data in {**_queue_proof(), **_pause_proof()}.items():
        item = paths.at("/var/lib/obsidian-admin/queue") / name
        _write(item, data, 0o600)
        if not name.startswith("manual-"):
            os.chown(item, os.getuid(), queue_gid)
    admission = paths.at("/var/lib/obsidian-admin/queue/admission.lock")
    _write(admission, b"", 0o600)
    os.chown(admission, os.getuid(), queue_gid)
    for name in (
        "/var/lib/obsidian-automation/deployments/host-update.lock",
        "/run/obsidian-automation/semantic-refresh-inhibited.lock",
    ):
        _write(paths.at(name), b"", 0o600)

    current_systemd = _systemd_snapshot()

    def observer():
        return copy.deepcopy(current_systemd)

    # The original parent_fd begins at the real /.  Anchor its otherwise
    # unchanged no-follow walk at the test root so /tmp itself need not be
    # weakened to run these isolated tests.
    monkeypatch.setattr(
        tool, "parent_fd",
        lambda path, uid: _sandbox_parent_fd(root, path, uid),
    )
    injected_source = source if source_bytes is None else source_bytes
    injected_boundary = boundary
    if injected_boundary is None:
        injected_boundary = lambda name: None
    mountinfo = {"raw": MOUNTINFO}
    instance = tool.Stage(
        paths, uid=os.getuid(), observer=observer, source=injected_source,
        boundary=injected_boundary, queue_gid=queue_gid,
        mount_reader=lambda: mountinfo["raw"],
    )
    return {
        "root": root,
        "paths": paths,
        "instance": instance,
        "source": source,
        "old_module": old_module,
        "old_authority": old_authority,
        "new_authority": new_authority,
        "module_bytes": module_bytes,
        "unit_bytes": unit_bytes,
        "systemd": current_systemd,
        "observer": observer,
        "queue_gid": queue_gid,
        "mountinfo": mountinfo,
    }


def _crash_at(target):
    def boundary(name):
        if name == target:
            raise RuntimeError("simulated crash at " + name)
    return boundary


def _assert_rejected(instance, action="inspect"):
    with pytest.raises((tool.Blocked, OSError, ValueError, KeyError, TypeError)):
        instance.run(action)


def _tree_snapshot(root: Path):
    entries = [root, *sorted(root.rglob("*"))]
    result = []
    for path in entries:
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        common = (str(path.relative_to(root)), info.st_uid, info.st_gid, mode,
                  info.st_mtime_ns, info.st_ctime_ns)
        if stat.S_ISLNK(info.st_mode):
            result.append(("link", *common, os.readlink(path)))
        elif stat.S_ISDIR(info.st_mode):
            result.append(("directory", *common))
        elif stat.S_ISREG(info.st_mode):
            result.append(("file", *common, info.st_nlink, path.read_bytes()))
        else:
            result.append(("other", *common, stat.S_IFMT(info.st_mode)))
    return result


def test_fixture_uses_canonical_fixture_pins_and_complete_inventory(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    instance = fake["instance"]

    assert len(tool.MODULES) == 14
    assert len(tool.UNITS) == 10
    assert len(tool.TIMERS) == 5
    assert len(tool.SERVICES) == 28
    assert len(fake["systemd"]) == 5 + 28 + 1
    assert tool.digest(fake["old_authority"]) == tool.OLD_AUTH
    assert tool.digest(fake["new_authority"]) == tool.NEW_AUTH
    assert tool.digest(fake["old_module"]) == tool.OLD_MODULE
    assert tool.digest(fake["source"]) == tool.NEW_MODULE
    assert instance.paths is fake["paths"]
    assert instance.uid == os.getuid()
    assert instance.source == fake["source"]
    assert stat.S_IMODE(instance.authority.parent.stat().st_mode) == 0o710
    assert instance.authority.parent.stat().st_gid == fake["queue_gid"]
    assert instance.queue.stat().st_gid == fake["queue_gid"]
    assert not fake["paths"].at("/run/obsidian-admin").exists()
    assert stat.S_IMODE(instance.stage.parent.stat().st_mode) == 0o700


def test_inspect_is_read_only(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    before = _tree_snapshot(fake["root"])

    result = fake["instance"].run("inspect")

    assert result == {
        "status": "PRECHECK_PASS",
        "revision": tool.REVISION,
        "authority_sha256": tool.OLD_AUTH,
        "source_sha256": tool.NEW_MODULE,
        "enabled": False,
    }
    assert _tree_snapshot(fake["root"]) == before
    assert not fake["instance"].stage.exists()


@pytest.mark.parametrize("role_path", ["/etc/obsidian-admin",
                                      "/var/lib/obsidian-admin/queue"])
@pytest.mark.parametrize("fault", ["wrong_group", "wrong_mode", "symlink"])
def test_exact_role_ancestors_fail_closed(tmp_path, monkeypatch, role_path, fault):
    fake = _fake_root(tmp_path, monkeypatch)
    path = fake["paths"].at(role_path)
    if fault == "wrong_group":
        os.chown(path, os.getuid(), os.getgid())
    elif fault == "wrong_mode":
        os.chmod(path, 0o750 if role_path.startswith("/etc/") else 0o710)
    else:
        replacement = path.with_name(path.name + "-original")
        path.rename(replacement)
        path.symlink_to(replacement, target_is_directory=True)
    _assert_rejected(fake["instance"])


def test_role_group_on_unrelated_parent_is_not_allowed(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    unrelated = fake["paths"].at("/etc/systemd")
    os.chown(unrelated, os.getuid(), fake["queue_gid"])
    _assert_rejected(fake["instance"])


@pytest.mark.parametrize("role_path", ["/etc/obsidian-admin",
                                      "/var/lib/obsidian-admin/queue"])
def test_role_parent_predicate_refuses_foreign_uid(tmp_path, monkeypatch, role_path):
    fake = _fake_root(tmp_path, monkeypatch)
    real = fake["paths"].at(role_path).stat()
    foreign = SimpleNamespace(st_mode=real.st_mode, st_uid=os.getuid() + 1,
                              st_gid=fake["queue_gid"])
    with pytest.raises(tool.Blocked, match="unsafe_role_parent"):
        tool.require_parent_identity(foreign, Path(role_path), os.getuid(),
                                     fake["queue_gid"])


def test_alternate_role_group_directory_is_not_canonical(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    alias = fake["paths"].at("/etc/obsidian-admin-alternate")
    alias.mkdir(mode=0o710)
    os.chown(alias, os.getuid(), fake["queue_gid"])
    fake["instance"].authority = alias / "authority.json"
    _write(fake["instance"].authority, fake["old_authority"], 0o600)
    _assert_rejected(fake["instance"])


def test_required_parent_absence_does_not_inherit_socket_exception(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    fake["instance"].refresh_lock.parent.rename(
        fake["instance"].refresh_lock.parent.with_name("not-present"))
    _assert_rejected(fake["instance"])


def test_existing_reviewed_runtime_directory_with_absent_socket(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    runtime = fake["paths"].at("/run/obsidian-admin")
    runtime.mkdir(mode=0o710)
    os.chown(runtime, os.getuid(), fake["queue_gid"])
    assert fake["instance"].run("inspect")["status"] == "PRECHECK_PASS"


@pytest.mark.parametrize("fault", ["wrong_group", "wrong_mode", "symlink",
                                   "file", "dangling_socket", "bad_run_anchor"])
def test_untrusted_runtime_parent_or_socket_rejected(tmp_path, monkeypatch, fault):
    fake = _fake_root(tmp_path, monkeypatch)
    runtime = fake["paths"].at("/run/obsidian-admin")
    if fault == "bad_run_anchor":
        os.chmod(runtime.parent, 0o777)
    elif fault == "file":
        _write(runtime, b"not a directory\n", 0o600)
    elif fault == "symlink":
        other = fake["paths"].at("/run/runtime-alias")
        other.mkdir(mode=0o710)
        os.chown(other, os.getuid(), fake["queue_gid"])
        runtime.symlink_to(other, target_is_directory=True)
    else:
        runtime.mkdir(mode=0o710)
        os.chown(runtime, os.getuid(), fake["queue_gid"])
        if fault == "wrong_group":
            os.chown(runtime, os.getuid(), os.getgid())
        elif fault == "wrong_mode":
            os.chmod(runtime, 0o700)
        else:
            (runtime / "operation.sock").symlink_to("missing.sock")
    _assert_rejected(fake["instance"])


def test_inspect_does_not_create_stage_parent_and_stage_can_create_it(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    parent = fake["instance"].stage.parent
    parent.rmdir()
    assert fake["instance"].run("inspect")["status"] == "PRECHECK_PASS"
    assert not parent.exists()
    assert fake["instance"].run("stage")["status"] == "STAGED_DISABLED"
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700


def test_stage_installs_exact_candidate_but_keeps_authority_disabled(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    instance = fake["instance"]

    assert instance.run("stage") == {"status": "STAGED_DISABLED", "revision": tool.REVISION}

    assert instance.module.read_bytes() == fake["source"]
    installed_authority = instance.authority.read_bytes()
    assert tool.digest(installed_authority) == tool.NEW_AUTH
    manifest = json.loads(installed_authority)
    assert manifest["enabled"] is False
    assert manifest["revision"] == tool.REVISION
    assert manifest["module_sha256"][tool.CHANGED] == tool.NEW_MODULE
    for name, data in fake["unit_bytes"].items():
        assert (instance.units / name).read_bytes() == data
    assert fake["observer"]() == fake["systemd"]
    assert (instance.stage / "completed.json").is_file()
    assert not (instance.stage / "rolled-back.json").exists()
    assert stat.S_IMODE(instance.module.stat().st_mode) == 0o644
    assert stat.S_IMODE(instance.authority.stat().st_mode) == 0o600


def test_stale_global_checkout_uses_protected_exact_release_not_checkout(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    instance = fake["instance"]
    checkout = fake["paths"].at("/opt/obsidian-automation/app")
    (checkout / ".git").mkdir(parents=True)
    stale_source = checkout / "src/obsidian_automation"
    stale_source.mkdir(parents=True)
    _write(stale_source / tool.CHANGED, b"stale global source\n", 0o644)
    instance.release_dir.mkdir(parents=True)
    os.chmod(instance.release_dir.parent.parent, 0o700)
    os.chmod(instance.release_dir.parent, 0o700)
    os.chmod(instance.release_dir, 0o700)
    _write(instance.source_path, fake["source"], 0o400)
    _write(instance.release_dir / "release.json", tool.canonical(tool.RELEASE), 0o400)
    monkeypatch.setattr(tool.subprocess, "run", lambda *a, **kw:
                        type("GitResult", (), {"stdout": tool.GLOBAL_HEAD.encode() + b"\n"})())
    instance.source = None
    assert instance.run("inspect")["source_sha256"] == tool.NEW_MODULE
    assert instance.run("stage")["status"] == "STAGED_DISABLED"
    assert instance.module.read_bytes() == fake["source"]


@pytest.mark.parametrize("fault", ["wrong_commit", "wrong_record", "wrong_source",
                                   "extra_bundle_member"])
def test_release_provenance_fail_closed(tmp_path, monkeypatch, fault):
    fake = _fake_root(tmp_path, monkeypatch)
    instance = fake["instance"]
    checkout = fake["paths"].at("/opt/obsidian-automation/app")
    (checkout / ".git").mkdir(parents=True)
    instance.release_dir.mkdir(parents=True)
    os.chmod(instance.release_dir.parent.parent, 0o700)
    os.chmod(instance.release_dir.parent, 0o700)
    os.chmod(instance.release_dir, 0o700)
    _write(instance.source_path, fake["source"] if fault != "wrong_source" else
           b"unreviewed source\n", 0o400)
    record = dict(tool.RELEASE)
    if fault == "wrong_record":
        record["release_commit"] = "0" * 40
    _write(instance.release_dir / "release.json", tool.canonical(record), 0o400)
    if fault == "extra_bundle_member":
        _write(instance.release_dir / "other", b"unexpected\n", 0o400)
    monkeypatch.setattr(tool.subprocess, "run", lambda *a, **kw:
                        type("GitResult", (), {"stdout":
                             ("0" * 40 if fault == "wrong_commit" else tool.GLOBAL_HEAD).encode()
                             + b"\n"})())
    instance.source = None
    _assert_rejected(instance)


@pytest.mark.parametrize(("crash_boundary", "recovery"), [
    ("prepared", "rollback"),
    ("module", "rollback"),
    ("authority", "finalize"),
    ("readback", "finalize"),
])
def test_crash_boundaries_are_recoverable(tmp_path, monkeypatch, crash_boundary, recovery):
    fake = _fake_root(tmp_path, monkeypatch, boundary=_crash_at(crash_boundary))
    instance = fake["instance"]

    with pytest.raises(RuntimeError, match="simulated crash"):
        instance.run("stage")
    result = instance.run(recovery)

    if recovery == "finalize":
        assert result == {"status": "STAGED_DISABLED", "revision": tool.REVISION}
        assert instance.module.read_bytes() == fake["source"]
        assert tool.digest(instance.authority.read_bytes()) == tool.NEW_AUTH
        assert (instance.stage / "completed.json").is_file()
    else:
        assert result == {"status": "RESTORED_DISABLED", "revision": tool.OLD_REVISION}
        assert instance.module.read_bytes() == fake["old_module"]
        assert instance.authority.read_bytes() == fake["old_authority"]
        assert (instance.stage / "rolled-back.json").is_file()


def test_rollback_restores_partial_install_without_overwriting_unknown_state(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch, boundary=_crash_at("authority"))
    instance = fake["instance"]
    with pytest.raises(RuntimeError, match="simulated crash"):
        instance.run("stage")

    result = instance.run("rollback")

    assert result == {"status": "RESTORED_DISABLED", "revision": tool.OLD_REVISION}
    assert instance.module.read_bytes() == fake["old_module"]
    assert instance.authority.read_bytes() == fake["old_authority"]
    assert (instance.stage / "rolled-back.json").is_file()
    assert not (instance.stage / "completed.json").exists()


@pytest.mark.parametrize("rollback_boundary", ["rollback_authority", "rollback_module"])
def test_rollback_itself_can_resume_after_a_crash(tmp_path, monkeypatch, rollback_boundary):
    fake = _fake_root(tmp_path, monkeypatch, boundary=_crash_at("authority"))
    instance = fake["instance"]
    with pytest.raises(RuntimeError, match="simulated crash"):
        instance.run("stage")

    instance.boundary = _crash_at(rollback_boundary)
    with pytest.raises(RuntimeError, match="simulated crash"):
        instance.run("rollback")
    instance.boundary = lambda name: None

    assert instance.run("rollback") == {
        "status": "RESTORED_DISABLED", "revision": tool.OLD_REVISION,
    }
    assert instance.module.read_bytes() == fake["old_module"]
    assert instance.authority.read_bytes() == fake["old_authority"]


def test_completed_stage_cannot_be_rolled_back_without_separate_review(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    instance = fake["instance"]
    instance.run("stage")

    with pytest.raises(tool.Blocked, match="completed_requires_separate_review"):
        instance.run("rollback")


def test_rejects_symlinked_installed_module(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    module = fake["instance"].module
    target = fake["root"] / "outside-module"
    _write(target, fake["old_module"], 0o644)
    module.unlink()
    module.symlink_to(target)

    _assert_rejected(fake["instance"])


def test_rejects_hardlinked_installed_module(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    module = fake["instance"].module
    os.link(module, fake["root"] / "module-hardlink")

    _assert_rejected(fake["instance"])


@pytest.mark.parametrize("permission_fault", ["file", "parent"])
def test_rejects_unsafe_file_or_parent_permissions(tmp_path, monkeypatch, permission_fault):
    fake = _fake_root(tmp_path, monkeypatch)
    module = fake["instance"].module
    if permission_fault == "file":
        os.chmod(module, 0o664)
    else:
        os.chmod(module.parent, 0o777)

    _assert_rejected(fake["instance"])


def test_rejects_module_hash_drift(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    _write(fake["instance"].module, b"tampered installed module\n", 0o644)

    with pytest.raises(tool.Blocked, match="module_inventory_drift"):
        fake["instance"].run("inspect")


def test_rejects_unit_hash_drift(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    name = sorted(tool.UNITS)[0]
    _write(fake["instance"].units / name, b"tampered systemd unit\n", 0o644)

    with pytest.raises(tool.Blocked, match="unit_inventory_drift"):
        fake["instance"].run("inspect")


def test_rejects_authority_hash_drift(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    changed_manifest = json.loads(fake["old_authority"])
    changed_manifest["enabled"] = True
    _write(fake["instance"].authority, tool.canonical(changed_manifest), 0o600)

    with pytest.raises(tool.Blocked, match="manifest_schema_or_authority"):
        fake["instance"].run("inspect")


def test_rejects_unpinned_source_bytes(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch, source_bytes=b"different source\n")

    with pytest.raises(tool.Blocked, match="release_source_pin_mismatch"):
        fake["instance"].run("inspect")


def test_rejects_broken_queue_proof_chain(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    request = fake["paths"].at("/var/lib/obsidian-admin/queue") / tool.OLD_FILES[0]
    _write(request, tool.canonical({"request_id": tool.OLD_ID, "action": "other"}), 0o600)

    with pytest.raises(tool.Blocked, match="old_queue_proof_invalid"):
        fake["instance"].run("inspect")


@pytest.mark.parametrize("nested", [False, True])
def test_queue_rejects_duplicate_json_keys_before_schema(tmp_path, monkeypatch, nested):
    fake = _fake_root(tmp_path, monkeypatch)
    request = fake["instance"].queue / tool.PAUSE_FILES[0]
    raw = request.read_bytes()
    inserted = (b',"metadata":{"same":1,"same":1}' if nested else
                b',"action":"pause"')
    _write(request, raw[:-2] + inserted + b"}\n", 0o600)
    with pytest.raises(tool.Blocked, match="queue_duplicate_key"):
        fake["instance"].run("inspect")


def test_queue_rejects_noncanonical_but_unique_json(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    request = fake["instance"].queue / tool.PAUSE_FILES[0]
    _write(request, json.dumps(json.loads(request.read_bytes())).encode() + b"\n", 0o600)
    with pytest.raises(tool.Blocked, match="queue_noncanonical_record"):
        fake["instance"].run("inspect")


def test_same_device_bind_mount_on_fixed_module_parent_blocks(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    before = _tree_snapshot(fake["root"])
    mount = (b"33 31 0:1 /reviewed-other-source "
             b"/opt/obsidian-automation/venv/lib/python3.13/"
             b"site-packages/obsidian_automation rw - ext4 /dev/root rw\n")
    fake["mountinfo"]["raw"] += mount  # SAME major:minor as '/' above
    with pytest.raises(tool.Blocked, match="unapproved_target_mount"):
        fake["instance"].run("inspect")
    assert _tree_snapshot(fake["root"]) == before


def test_mountinfo_escape_and_unrelated_mount_are_bounded(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    extra = b"33 31 0:1 /other /tmp/unrelated\\040space rw - ext4 /dev/root rw\n"
    fake["mountinfo"]["raw"] += extra
    assert any(entry[4] == "/tmp/unrelated space" for entry in
               tool.mount_inventory(fake["mountinfo"]["raw"]))
    assert fake["instance"].run("inspect")["status"] == "PRECHECK_PASS"


@pytest.mark.parametrize("invalid", [
    b"", b"33 31 0:1 / /etc/obsidian-admin rw - ext4 /dev/root rw",
    b"33 31 0:1 / /etc/obsidian-admin\\04 rw - ext4 /dev/root rw\n",
    b"33 31 bad / /etc/obsidian-admin rw - ext4 /dev/root rw\n",
    b"32 31 0:1 / /run rw - ext4 /dev/root rw\n",
])
def test_mount_metadata_malformed_or_ambiguous_fails_closed(tmp_path, monkeypatch, invalid):
    fake = _fake_root(tmp_path, monkeypatch)
    fake["mountinfo"]["raw"] = invalid if not invalid else MOUNTINFO + invalid
    _assert_rejected(fake["instance"])


def test_mount_reader_unavailable_fails_closed(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    fake["instance"].mount_reader = lambda: (_ for _ in ()).throw(OSError("unavailable"))
    _assert_rejected(fake["instance"])


def test_mount_alias_emerging_after_durable_pending_blocks_install(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)

    def mount_drift(name):
        if name == "prepared":
            fake["mountinfo"]["raw"] += (
                b"33 31 0:1 /another /etc/obsidian-admin rw - ext4 /dev/root rw\n")

    fake["instance"].boundary = mount_drift
    with pytest.raises(tool.Blocked, match="unapproved_target_mount"):
        fake["instance"].run("stage")
    assert fake["instance"].module.read_bytes() == fake["old_module"]
    assert fake["instance"].authority.read_bytes() == fake["old_authority"]
    assert (fake["instance"].stage / "pending.json").exists()


def test_replacement_run_mount_is_not_an_expected_boundary(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    fake["mountinfo"]["raw"] = MOUNTINFO.replace(
        b"/run rw - tmpfs tmpfs", b"/run rw - ext4 /dev/root")
    with pytest.raises(tool.Blocked, match="unexpected_run_mount"):
        fake["instance"].run("inspect")


def test_mount_alias_after_module_write_blocks_manifest_and_recovery(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)

    def mount_drift(name):
        if name == "module":
            fake["mountinfo"]["raw"] += (
                b"33 31 0:1 /other /etc/obsidian-admin rw - ext4 /dev/root rw\n")

    fake["instance"].boundary = mount_drift
    with pytest.raises(tool.Blocked, match="unapproved_target_mount"):
        fake["instance"].run("stage")
    assert fake["instance"].module.read_bytes() == fake["source"]
    assert fake["instance"].authority.read_bytes() == fake["old_authority"]
    _assert_rejected(fake["instance"], "rollback")
    fake["mountinfo"]["raw"] = MOUNTINFO
    assert fake["instance"].run("rollback")["status"] == "RESTORED_DISABLED"


def test_mount_drift_mid_rollback_requires_resumption(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch, boundary=_crash_at("authority"))
    with pytest.raises(RuntimeError, match="simulated crash"):
        fake["instance"].run("stage")

    def mount_drift(name):
        if name == "rollback_authority":
            fake["mountinfo"]["raw"] += (
                b"33 31 0:1 /other /opt/obsidian-automation/venv/lib/"
                b"python3.13/site-packages/obsidian_automation rw - ext4 /dev/root rw\n")

    fake["instance"].boundary = mount_drift
    with pytest.raises(tool.Blocked, match="unapproved_target_mount"):
        fake["instance"].run("rollback")
    assert fake["instance"].module.read_bytes() == fake["source"]
    assert fake["instance"].authority.read_bytes() == fake["old_authority"]
    fake["mountinfo"]["raw"] = MOUNTINFO
    fake["instance"].boundary = lambda name: None
    assert fake["instance"].run("rollback")["status"] == "RESTORED_DISABLED"


def test_rejects_unknown_unsettled_queue_entry(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    _write(fake["instance"].queue / "request-rogue.json", b"{}\n", 0o600)

    with pytest.raises(tool.Blocked, match="unsettled_or_unknown_queue"):
        fake["instance"].run("inspect")


def test_rejects_wrong_queue_group_and_mode(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    request = fake["instance"].queue / tool.PAUSE_FILES[0]
    os.chmod(request, 0o640)
    _assert_rejected(fake["instance"])
    os.chmod(request, 0o600)
    fake["instance"].queue_gid = fake["queue_gid"] + 1
    _assert_rejected(fake["instance"])


@pytest.mark.parametrize("fault", ["pause_result", "pause_orphan", "old_success"])
def test_rejects_corrupt_or_orphan_history(tmp_path, monkeypatch, fault):
    fake = _fake_root(tmp_path, monkeypatch)
    queue = fake["instance"].queue
    if fault == "pause_orphan":
        (queue / tool.PAUSE_FILES[1]).unlink()
    else:
        name = tool.PAUSE_FILES[2] if fault == "pause_result" else tool.OLD_FILES[2]
        record = json.loads((queue / name).read_bytes())
        record["state"] = "indeterminate" if fault == "pause_result" else "completed"
        _write(queue / name, tool.canonical(record), 0o600)
        os.chown(queue / name, os.getuid(), fake["queue_gid"])
    _assert_rejected(fake["instance"])


def test_unrelated_core_failed_is_observed_but_github_busy_rejected(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    core = fake["systemd"]["obsidian-core-promotion.service"]
    core["ActiveState"] = core["SubState"] = "failed"
    assert fake["instance"].run("inspect")["status"] == "PRECHECK_PASS"
    github = fake["systemd"]["obsidian-github-sync.service"]
    github["ActiveState"] = "active"
    _assert_rejected(fake["instance"])


def test_rejects_busy_host_lock(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    lock_fd = os.open(fake["instance"].host_lock, os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _assert_rejected(fake["instance"])
    finally:
        os.close(lock_fd)


def test_rejects_operation_socket(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    socket_path = fake["paths"].at("/run/obsidian-admin/operation.sock")
    socket_path.parent.mkdir(mode=0o710)
    os.chown(socket_path.parent, os.getuid(), fake["queue_gid"])
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        monkeypatch.chdir(fake["root"])
        listener.bind("run/obsidian-admin/operation.sock")
        assert stat.S_ISSOCK(socket_path.lstat().st_mode)
        with pytest.raises(tool.Blocked, match="pending_socket_or_active_queue"):
            fake["instance"].run("inspect")
    finally:
        listener.close()
        socket_path.unlink(missing_ok=True)


def test_rejects_non_quiescent_systemd_snapshot(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    worker = "obsidian-github-sync.service"
    fake["systemd"][worker]["ActiveState"] = "active"

    with pytest.raises(tool.Blocked, match="worker_or_domain_active"):
        fake["instance"].run("inspect")


def test_rejects_enabled_operator_service(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    fake["systemd"]["obsidian-admin-operator.service"]["UnitFileState"] = "enabled"

    with pytest.raises(tool.Blocked, match="worker_enabled"):
        fake["instance"].run("inspect")


def test_rejects_timer_drift_between_preflight_and_install(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)

    def drift(name):
        if name == "prepared":
            timer = tool.TIMERS[0]
            fake["systemd"][timer]["ActiveState"] = "inactive"
            fake["systemd"][timer]["SubState"] = "dead"

    fake["instance"].boundary = drift
    with pytest.raises(tool.Blocked, match="timer_drift"):
        fake["instance"].run("stage")
    assert fake["instance"].module.read_bytes() == fake["old_module"]
    assert fake["instance"].authority.read_bytes() == fake["old_authority"]


def test_rejects_replaced_updater_lock_during_install(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)

    def drift(name):
        if name == "prepared":
            lock_path = fake["instance"].host_lock
            replacement = lock_path.parent / "replacement.lock"
            _write(replacement, b"", 0o600)
            os.replace(replacement, lock_path)

    fake["instance"].boundary = drift
    with pytest.raises(tool.Blocked, match="updater_lock_replaced"):
        fake["instance"].run("stage")
    assert fake["instance"].module.read_bytes() == fake["old_module"]


def test_rejects_valid_but_changed_queue_snapshot_during_install(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)

    def drift(name):
        if name == "prepared":
            queue = fake["instance"].queue
            for filename, data in _queue_proof(request_extra={
                    "queued_at": "2026-10-22T00:00:01+00:00"}).items():
                _write(queue / filename, data, 0o600)
                if not filename.startswith("manual-"):
                    os.chown(queue / filename, os.getuid(), fake["queue_gid"])

    fake["instance"].boundary = drift
    with pytest.raises(tool.Blocked, match="queue_drift"):
        fake["instance"].run("stage")
    assert fake["instance"].module.read_bytes() == fake["old_module"]
    assert fake["instance"].authority.read_bytes() == fake["old_authority"]


def test_rejects_authority_replacement_after_module_write(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)

    def drift(name):
        if name == "module":
            _write(fake["instance"].authority, b"concurrent authority change\n", 0o600)

    fake["instance"].boundary = drift
    with pytest.raises(tool.Blocked, match="authority_drift"):
        fake["instance"].run("stage")
    assert fake["instance"].module.read_bytes() == fake["source"]


@pytest.mark.skipif(os.getuid() != 0, reason="foreign ownership requires root chown")
def test_rejects_foreign_owned_module_when_root_can_chown(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)
    try:
        os.chown(fake["instance"].module, 65534, 65534)
    except OSError as exc:
        pytest.skip(f"foreign-owner fixture cannot chown on this filesystem: {exc}")

    with pytest.raises(tool.Blocked, match="unsafe_file"):
        fake["instance"].run("inspect")


def test_finalize_rejects_changes_after_prepared_receipt(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch, boundary=_crash_at("prepared"))
    with pytest.raises(RuntimeError, match="simulated crash"):
        fake["instance"].run("stage")
    _write(fake["instance"].module, b"unrecognized concurrent module\n", 0o644)

    _assert_rejected(fake["instance"], action="finalize")


def test_stage_refuses_finalizing_or_rolling_back_without_prepared_receipt(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch)

    _assert_rejected(fake["instance"], action="finalize")
    _assert_rejected(fake["instance"], action="rollback")


@pytest.mark.parametrize("boundary", ["stage_dir", "backup_authority",
                                    "backup_module", "rollback_receipt"])
def test_crash_before_durable_pending_is_manual_gate(tmp_path, monkeypatch, boundary):
    fake = _fake_root(tmp_path, monkeypatch, boundary=_crash_at(boundary))
    with pytest.raises(RuntimeError, match="simulated crash"):
        fake["instance"].run("stage")
    assert fake["instance"].module.read_bytes() == fake["old_module"]
    assert fake["instance"].authority.read_bytes() == fake["old_authority"]
    _assert_rejected(fake["instance"], "stage")
    _assert_rejected(fake["instance"], "rollback")


def test_partial_receipt_and_foreign_terminal_receipt_fail_closed(tmp_path, monkeypatch):
    fake = _fake_root(tmp_path, monkeypatch, boundary=_crash_at("authority"))
    instance = fake["instance"]
    with pytest.raises(RuntimeError, match="simulated crash"):
        instance.run("stage")
    _write(instance.stage / "completed.json", b"{}\n", 0o600)
    with pytest.raises(tool.Blocked, match="untrusted_completed_receipt"):
        instance.run("finalize")


def _fake_cli(monkeypatch, *, uid=0, isolated=1, no_site=1, no_bytecode=1,
              cache="/dev/null/obsidian-admin-source-only"):
    """Exercise real argparse/main, but replace its root Stage with a no-I/O spy."""
    calls = []

    class NoEffectsStage:
        def run(self, action):
            calls.append(action)
            return {"status": "SYNTHETIC_NO_EFFECT", "action": action}

    monkeypatch.setattr(tool, "Stage", NoEffectsStage)
    monkeypatch.setattr(tool, "os", SimpleNamespace(geteuid=lambda: uid))
    monkeypatch.setattr(tool, "sys", SimpleNamespace(
        flags=SimpleNamespace(isolated=isolated, no_site=no_site,
                              dont_write_bytecode=no_bytecode),
        pycache_prefix=cache, stderr=sys.stderr))
    return calls


def test_cli_default_inspect_is_only_implicit_action(monkeypatch, capsys):
    calls = _fake_cli(monkeypatch)
    assert tool.main([]) == 0
    assert calls == ["inspect"]
    assert json.loads(capsys.readouterr().out) == {
        "status": "SYNTHETIC_NO_EFFECT", "action": "inspect"}


@pytest.mark.parametrize("action", ["--stage-disabled", "--finalize", "--rollback"])
def test_cli_mutations_require_exact_authorization(monkeypatch, capsys, action):
    calls = _fake_cli(monkeypatch)
    assert tool.main([action]) == 2
    assert json.loads(capsys.readouterr().err)["reason"] == (
        "separate_human_authorization_required")
    assert calls == []
    with pytest.raises(SystemExit) as invalid:
        tool.main([action, "--authorization", "not-the-approval"])
    assert invalid.value.code == 2
    assert calls == []


@pytest.mark.parametrize("option", ["--revision", "--source-path", "--unit"])
def test_cli_has_no_path_revision_or_unit_selector(monkeypatch, capsys, option):
    calls = _fake_cli(monkeypatch)
    with pytest.raises(SystemExit) as invalid:
        tool.main(["--stage-disabled", option, "arbitrary"])
    assert invalid.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
    assert calls == []


@pytest.mark.parametrize("flags", [
    {"isolated": 0}, {"no_site": 0}, {"no_bytecode": 0},
    {"cache": "/tmp/pycache"},
])
def test_cli_requires_isolated_source_only_interpreter(monkeypatch, capsys, flags):
    calls = _fake_cli(monkeypatch, **flags)
    assert tool.main([]) == 2
    assert json.loads(capsys.readouterr().err)["reason"] == (
        "isolated_source_only_python_required")
    assert calls == []


def test_cli_nonroot_rejected_before_stage_creation(monkeypatch, capsys):
    calls = _fake_cli(monkeypatch, uid=1000)
    assert tool.main([]) == 2
    assert json.loads(capsys.readouterr().err)["reason"] == "root_required"
    assert calls == []


def test_cli_exact_authorization_routes_only_to_no_effect_spy(monkeypatch, capsys):
    calls = _fake_cli(monkeypatch)
    assert tool.main(["--stage-disabled", "--authorization",
                      "OA403-DS1-disabled-overlay"]) == 0
    assert calls == ["stage"]
    assert json.loads(capsys.readouterr().out)["status"] == "SYNTHETIC_NO_EFFECT"
