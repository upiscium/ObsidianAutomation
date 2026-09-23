from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


life = load(Path("tools/host_runtime_lifecycle.py"), "host_lifecycle_tests")
bootstrap = load(Path("tools/production_bootstrap.py"), "bootstrap_lifecycle_integration")
TARGET = "a" * 40


class System:
    def __init__(self, existing=True):
        self.states = {name: dict(LoadState="loaded" if existing else "not-found",
                      ActiveState="inactive", SubState="dead",
                      UnitFileState="disabled" if existing else "",
                      NextElapseUSecMonotonic="infinity",
                      MainPID="0", ControlPID="0", Job="") for name in (*life.TIMERS, *life.SERVICES)}
        self.commands = []
        self.fail = None
        self.elapsed_on_start = set()
        self.no_next_on_start = set()
        self.source = None
        self.app = None
        self.deployed = False

    def __call__(self, args):
        args = tuple(map(str, args))
        self.commands.append(args)
        if self.fail and self.fail(args):
            return subprocess.CompletedProcess(args, 1, "", "PRIVATE-COMMAND-CANARY")
        out, rc = "", 0
        if args[0] == "git":
            tail = args[3:]
            if tail == ("rev-parse", "HEAD"):
                out = TARGET if args[2] == str(self.source) or self.deployed else "b" * 40
            elif tail == ("branch", "--show-current"):
                out = "main"
            elif tail == ("reset", "--hard", TARGET):
                self.deployed = True
        elif args[0] == "systemctl":
            action = args[1]
            if action == "show":
                values = self.states[args[2]]
                if "--value" in args:
                    key = next(a.split("=", 1)[1] for a in args if a.startswith("--property="))
                    out = values[key]
                else:
                    out = "\n".join(f"{key}={value}" for key, value in values.items())
            elif action == "is-active":
                out = self.states[args[2]]["ActiveState"]
                rc = 0 if out == "active" else 3
            elif action == "is-enabled":
                out = self.states[args[2]]["UnitFileState"] or "not-found"
                rc = 0 if out == "enabled" else 1
            elif action == "daemon-reload":
                for s in self.states.values():
                    s["LoadState"] = "loaded"
                    s["UnitFileState"] = s["UnitFileState"] or "disabled"
            elif action == "disable":
                name = args[-1]
                self.states[name]["UnitFileState"] = "disabled"
                if "--now" in args:
                    self.states[name]["ActiveState"] = "inactive"
                    self.states[name]["SubState"] = "dead"
                    self.states[name]["NextElapseUSecMonotonic"] = "infinity"
            elif action in {"stop", "reset-failed"}:
                name = args[-1]
                self.states[name]["ActiveState"] = "inactive"
                self.states[name]["SubState"] = "dead"
                self.states[name]["NextElapseUSecMonotonic"] = "infinity"
            elif action == "enable":
                self.states[args[-1]]["UnitFileState"] = "enabled"
            elif action == "start":
                name = args[-1]
                self.states[name]["ActiveState"] = "active"
                if name.endswith(".timer"):
                    if name in self.elapsed_on_start:
                        self.states[name]["SubState"] = "elapsed"
                        self.states[name]["NextElapseUSecMonotonic"] = "infinity"
                    else:
                        self.states[name]["SubState"] = "waiting"
                        self.states[name]["NextElapseUSecMonotonic"] = (
                            "infinity" if name in self.no_next_on_start else "5min"
                        )
                else:
                    self.states[name]["SubState"] = "running"
            else:
                raise AssertionError(args)
        return subprocess.CompletedProcess(args, rc, out + "\n", "")


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    shutil.copytree("tools", source / "tools", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree("examples", source / "examples")
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    revision = tmp_path / "etc/pre-review-revision.env"
    revision.parent.mkdir()
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/pip").write_text("#!/bin/sh\n")
    app = tmp_path / "app"
    app.mkdir()
    system = System()
    system.source, system.app = source, app
    kwargs = dict(source_root=source, target_sha=TARGET, venv_root=venv,
                  receipt_dir=tmp_path / "receipts", runner=system, require_root=False,
                  systemd_dir=systemd, revision_env=revision, config_exists=lambda path: True)
    return kwargs, system, app


def complete(kwargs):
    with life.RuntimeTransaction(**kwargs) as t:
        t.prepare()
        t.stage_and_smoke()
        t.restore()
        t.finish()
    return t


@pytest.mark.parametrize("enabled,active", [(False, False), (False, True), (True, False), (True, True)])
def test_timer_state_matrix_and_no_unrequested_activation(setup, enabled, active):
    kwargs, system, _ = setup
    for name in life.TIMERS:
        system.states[name].update(UnitFileState="enabled" if enabled else "disabled",
                                   ActiveState="active" if active else "inactive")
    complete(kwargs)
    for name in life.TIMERS:
        assert system.states[name]["ActiveState"] == ("active" if active else "inactive")
        assert system.states[name]["UnitFileState"] == ("enabled" if enabled else "disabled")
    starts = [c for c in system.commands if c[:2] == ("systemctl", "start")]
    assert len(starts) == (4 if active else 0)
    assert not (kwargs["receipt_dir"] / "pending-runtime.json").exists()
    assert len(list(kwargs["receipt_dir"].glob("*.runtime.json"))) == 1
    assert kwargs["revision_env"].read_text() == f"OBSIDIAN_AUTOMATION_REVISION={TARGET}\n"
    assert len(list(kwargs["systemd_dir"].glob("*.service"))) == 19
    assert all("gitea-runner" not in " ".join(c) and "obsidian-snapshot" not in " ".join(c) for c in system.commands)


def test_fresh_install_leaves_everything_disabled(setup):
    kwargs, system, _ = setup
    for state in system.states.values():
        state.update(LoadState="not-found", UnitFileState="")
    result = complete(kwargs)
    assert result.host_activation == "not_attempted"
    assert not any(c[:2] in {("systemctl", "start"), ("systemctl", "enable")} for c in system.commands)


@pytest.mark.parametrize("enabled", ["masked", "masked-runtime", "enabled-runtime", "linked", "static"])
def test_unsupported_states_refused_without_mutation(setup, enabled):
    kwargs, system, _ = setup
    system.states[life.TIMERS[0]]["UnitFileState"] = enabled
    with pytest.raises(life.LifecycleError):
        complete(kwargs)
    assert not any(c[:2] == ("systemctl", "disable") for c in system.commands)
    assert not (kwargs["receipt_dir"] / "pending-runtime.json").exists()


def test_query_error_is_not_treated_as_absent(setup):
    kwargs, system, _ = setup
    system.fail = lambda args: args[:2] == ("systemctl", "show")
    with pytest.raises(life.LifecycleError, match="query_failed"):
        complete(kwargs)
    assert not any(c[:2] == ("systemctl", "disable") for c in system.commands)


def test_drain_timeout_precedes_any_package_or_unit_mutation(setup):
    kwargs, system, _ = setup
    system.states[life.SERVICES[2]]["ActiveState"] = "activating"
    ticks = iter([0, 400])
    kwargs["clock"] = lambda: next(ticks)
    with pytest.raises(life.LifecycleError, match="drain_timeout"):
        complete(kwargs)
    assert not list(kwargs["systemd_dir"].iterdir())
    assert not any(c[:2] == ("systemctl", "stop") for c in system.commands)
    assert all(system.states[t]["ActiveState"] == "inactive" for t in life.TIMERS)


def test_failed_smoke_disables_and_same_target_retry_restores_original_intent(setup):
    kwargs, system, _ = setup
    for name in life.TIMERS:
        system.states[name].update(UnitFileState="enabled", ActiveState="active")
    system.fail = lambda c: "--profile" in c and "safe" in c and c[0].endswith("obsidian-github-production-smoke")
    with pytest.raises(life.LifecycleError, match="github_safe_smoke"):
        complete(kwargs)
    assert all(system.states[t]["UnitFileState"] == "disabled" for t in life.TIMERS)
    pending = kwargs["receipt_dir"] / "pending-runtime.json"
    value = json.loads(pending.read_text())
    assert value["phase"] == "failed"
    assert all(t["active"] for t in value["timers"].values())
    assert "PRIVATE-COMMAND-CANARY" not in pending.read_text()
    with pytest.raises(life.LifecycleError, match="same_target"):
        complete({**kwargs, "target_sha": "c" * 40})
    system.fail = None
    complete(kwargs)
    assert all(system.states[t]["ActiveState"] == "active" for t in life.TIMERS)


def test_partial_restore_failure_disables_all(setup):
    kwargs, system, _ = setup
    for name in life.TIMERS:
        system.states[name].update(UnitFileState="enabled", ActiveState="active")
    system.fail = lambda c: c == ("systemctl", "start", life.TIMERS[1])
    with pytest.raises(life.LifecycleError, match="timer_start"):
        complete(kwargs)
    assert all(system.states[t]["ActiveState"] == "inactive" for t in life.TIMERS)
    assert all(system.states[t]["UnitFileState"] == "disabled" for t in life.TIMERS)


def test_elapsed_timer_after_restore_fails_closed(setup):
    kwargs, system, _ = setup
    for name in life.TIMERS:
        system.states[name].update(UnitFileState="enabled", ActiveState="active")
    system.elapsed_on_start.add(life.TIMERS[0])

    with pytest.raises(life.LifecycleError, match="timer_not_armed_after_restore"):
        complete(kwargs)

    assert all(system.states[t]["ActiveState"] == "inactive" for t in life.TIMERS)
    assert all(system.states[t]["UnitFileState"] == "disabled" for t in life.TIMERS)
    pending = json.loads((kwargs["receipt_dir"] / "pending-runtime.json").read_text())
    assert pending["phase"] == "failed"
    assert pending["containment"] == "disabled"


def test_waiting_timer_without_next_elapse_fails_closed(setup):
    kwargs, system, _ = setup
    for name in life.TIMERS:
        system.states[name].update(UnitFileState="enabled", ActiveState="active")
    system.no_next_on_start.add(life.TIMERS[2])

    with pytest.raises(life.LifecycleError, match="timer_not_armed_after_restore"):
        complete(kwargs)

    assert all(system.states[t]["ActiveState"] == "inactive" for t in life.TIMERS)
    assert all(system.states[t]["UnitFileState"] == "disabled" for t in life.TIMERS)


def test_update_lock_and_interrupted_body(setup):
    kwargs, system, _ = setup
    with pytest.raises(KeyboardInterrupt):
        with life.RuntimeTransaction(**kwargs) as t:
            t.prepare()
            with pytest.raises(BlockingIOError):
                with life.RuntimeTransaction(**kwargs):
                    pytest.fail("parallel update acquired lock")
            raise KeyboardInterrupt
    assert json.loads((kwargs["receipt_dir"] / "pending-runtime.json").read_text())["containment"] == "disabled"


def test_full_bootstrap_orders_quiesce_package_smoke_restore(setup, monkeypatch):
    kwargs, system, app = setup
    factory = lambda **args: life.RuntimeTransaction(**{**args,
        "systemd_dir": kwargs["systemd_dir"], "revision_env": kwargs["revision_env"],
        "config_exists": lambda path: True})
    monkeypatch.setattr(bootstrap, "_load_host_lifecycle", lambda _: factory)
    for name in life.TIMERS:
        system.states[name].update(UnitFileState="enabled", ActiveState="active")
    wheelhouse = app.parent / "wheels"
    wheelhouse.mkdir()
    receipt, path = bootstrap.apply_from_target(
        source_root=kwargs["source_root"], target_sha=TARGET, profile="automation", app_root=app,
        venv_root=kwargs["venv_root"], launcher_path=app.parent / "bin/update",
        receipt_dir=kwargs["receipt_dir"], wheelhouse=wheelhouse, runner=system, require_root=False,
    )
    assert receipt.host_activation == "restored"
    assert json.loads(path.read_text())["bootstrap_contract"] == 4
    commands = system.commands
    first_disable = next(i for i, c in enumerate(commands) if c[:2] == ("systemctl", "disable"))
    install = next(i for i, c in enumerate(commands) if "--no-build-isolation" in c)
    smoke = next(i for i, c in enumerate(commands) if c[0].endswith("obsidian-github-production-smoke"))
    authority = next(i for i, c in enumerate(commands)
                     if c[:1] == ("sh",) and c[1].endswith("bootstrap-pre-review-authority.sh"))
    start = next(i for i, c in enumerate(commands) if c[:2] == ("systemctl", "start"))
    assert first_disable < install < smoke < authority < start


def test_failed_package_does_not_resume_timers(setup, monkeypatch):
    kwargs, system, app = setup
    monkeypatch.setattr(bootstrap, "_load_host_lifecycle", lambda _: lambda **args: life.RuntimeTransaction(**{
        **args, "systemd_dir": kwargs["systemd_dir"], "revision_env": kwargs["revision_env"]}))
    for name in life.TIMERS:
        system.states[name].update(UnitFileState="enabled", ActiveState="active")
    system.fail = lambda c: "--no-build-isolation" in c
    wheels = app.parent / "wheels"
    wheels.mkdir()
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.apply_from_target(source_root=kwargs["source_root"], target_sha=TARGET, profile="automation",
            app_root=app, venv_root=kwargs["venv_root"], launcher_path=app.parent / "bin/update",
            receipt_dir=kwargs["receipt_dir"], wheelhouse=wheels, runner=system, require_root=False)
    assert all(system.states[t]["UnitFileState"] == "disabled" for t in life.TIMERS)
    assert not any(c[:2] == ("systemctl", "start") for c in system.commands)
    assert "PRIVATE-COMMAND-CANARY" not in "".join(p.read_text() for p in kwargs["receipt_dir"].glob("*.json"))


def test_failed_authority_migration_does_not_resume_timers(setup):
    kwargs, system, _ = setup
    for name in life.TIMERS:
        system.states[name].update(UnitFileState="enabled", ActiveState="active")
    system.fail = lambda c: c[:1] == ("sh",) and c[1].endswith("bootstrap-pre-review-authority.sh")

    with pytest.raises(life.LifecycleError, match="pre_review_authority_failed"):
        complete(kwargs)

    assert all(system.states[t]["ActiveState"] == "inactive" for t in life.TIMERS)
    assert all(system.states[t]["UnitFileState"] == "disabled" for t in life.TIMERS)
    assert not any(c[:2] == ("systemctl", "start") for c in system.commands)
    pending = json.loads((kwargs["receipt_dir"] / "pending-runtime.json").read_text())
    assert pending["phase"] == "failed"
    assert pending["containment"] == "disabled"


def test_structured_not_found_rc1_is_not_confused_with_bus_failure(setup):
    kwargs, system, _ = setup
    original = kwargs["runner"]
    def absent(args):
        result = original(args)
        if args[:2] == ("systemctl", "show") and "LoadState=not-found" in result.stdout:
            return subprocess.CompletedProcess(args, 1, result.stdout, "No such unit")
        return result
    for state in system.states.values():
        state.update(LoadState="not-found", UnitFileState="")
    complete({**kwargs, "runner": absent})


def test_corrupt_intent_never_reenables_or_overwrites_saved_intent(setup):
    kwargs, system, _ = setup
    directory = kwargs["receipt_dir"]
    directory.mkdir()
    path = directory / "pending-runtime.json"
    data = b'{"record_version":1,"record_version":1}'
    path.write_bytes(data)
    path.chmod(0o600)
    with pytest.raises(life.LifecycleError, match="invalid_runtime_intent"):
        complete(kwargs)
    assert path.read_bytes() == data
    assert not any(c[:2] in {("systemctl", "disable"), ("systemctl", "start")} for c in system.commands)


def test_group_writable_runtime_journal_parent_refused(setup):
    kwargs, system, _ = setup
    kwargs["receipt_dir"].mkdir()
    kwargs["receipt_dir"].chmod(0o770)
    with pytest.raises(life.LifecycleError, match="receipt_directory"):
        complete(kwargs)
    assert not system.commands
