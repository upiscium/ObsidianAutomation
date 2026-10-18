from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.github_production_update import (
    CommandResult,
    ProductionUpdateError,
    execute_update,
)


PREVIOUS = "a" * 40
TARGET = "b" * 40
UNIT_NAMES = (
    "obsidian-github-sync-vault-pull.service",
    "obsidian-github-sync.service",
    "obsidian-github-sync.timer",
    "obsidian-github-writer.service",
    "obsidian-github-compactor.service",
    "obsidian-github-daily-schedule.service",
    "obsidian-github-daily-collect.service",
    "obsidian-github-daily-summary.service",
    "obsidian-github-daily-render.service",
    "obsidian-github-daily-apply.service",
    "obsidian-github-daily-progress.timer",
)


def _layout(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    app_root = tmp_path / "app"
    unit_sources = app_root / "examples" / "github-sync"
    unit_sources.mkdir(parents=True)
    for name in UNIT_NAMES:
        (unit_sources / name).write_text(f"[Unit]\nDescription={name}\n", encoding="utf-8")

    venv_root = tmp_path / "venv"
    bin_dir = venv_root / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "obsidian-github-production-smoke").write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "obsidian-github-daily-production-smoke").write_text(
        "#!/bin/sh\n",
        encoding="utf-8",
    )

    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    (tmp_path / "etc/obsidian-github-summarizer").mkdir(parents=True)
    receipt_dir = tmp_path / "receipts"
    return app_root, venv_root, systemd_dir, receipt_dir


class FakeRunner:
    def __init__(
        self,
        app_root: Path,
        venv_root: Path,
        *,
        dirty: bool = False,
        enabled: bool = True,
        active: bool = True,
        fail_profile: str | None = None,
        fail_pip: bool = False,
        daily_present: bool = False,
        daily_enabled: bool = False,
        daily_active: bool = False,
    ) -> None:
        self.app_root = app_root
        self.venv_root = venv_root
        self.dirty = dirty
        self.enabled = enabled
        self.active = active
        self.fail_profile = fail_profile
        self.fail_pip = fail_pip
        self.daily_present = daily_present
        self.daily_enabled = daily_enabled
        self.daily_active = daily_active
        self.current_sha = PREVIOUS
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...]) -> CommandResult:
        argv = tuple(str(item) for item in argv)
        self.calls.append(argv)

        prefix = ("git", "-C", str(self.app_root))
        if argv == (*prefix, "branch", "--show-current"):
            return CommandResult(0, "main\n", "")
        if argv == (*prefix, "status", "--porcelain"):
            return CommandResult(0, " M local-change\n" if self.dirty else "", "")
        if argv == (*prefix, "rev-parse", "HEAD"):
            return CommandResult(0, self.current_sha + "\n", "")
        if argv == (*prefix, "fetch", "origin", "main"):
            return CommandResult(0, "", "")
        if argv == (*prefix, "rev-parse", "--verify", f"{TARGET}^{{commit}}"):
            return CommandResult(0, TARGET + "\n", "")
        if argv == (*prefix, "merge-base", "--is-ancestor", TARGET, "origin/main"):
            return CommandResult(0, "", "")
        if argv == (*prefix, "reset", "--hard", TARGET):
            self.current_sha = TARGET
            return CommandResult(0, "", "")

        if argv == ("systemctl", "is-enabled", "obsidian-github-sync.timer"):
            return CommandResult(0 if self.enabled else 1, "enabled\n" if self.enabled else "disabled\n", "")
        if argv == (
            "systemctl",
            "is-enabled",
            "obsidian-github-daily-progress.timer",
        ):
            if not self.daily_present:
                return CommandResult(1, "not-found\n", "")
            return CommandResult(
                0 if self.daily_enabled else 1,
                "enabled\n" if self.daily_enabled else "disabled\n",
                "",
            )
        if argv == ("systemctl", "is-active", "obsidian-github-sync.timer"):
            return CommandResult(0 if self.active else 3, "active\n" if self.active else "inactive\n", "")
        if argv == (
            "systemctl",
            "is-active",
            "obsidian-github-daily-progress.timer",
        ):
            if not self.daily_present:
                return CommandResult(3, "inactive\n", "")
            return CommandResult(
                0 if self.daily_active else 3,
                "active\n" if self.daily_active else "inactive\n",
                "",
            )
        if argv == ("systemctl", "disable", "--now", "obsidian-github-sync.timer"):
            self.enabled = False
            self.active = False
            return CommandResult(0, "", "")
        if argv == (
            "systemctl",
            "disable",
            "--now",
            "obsidian-github-daily-progress.timer",
        ):
            self.daily_present = True
            self.daily_enabled = False
            self.daily_active = False
            return CommandResult(0, "", "")
        if argv == ("systemctl", "enable", "--now", "obsidian-github-sync.timer"):
            self.enabled = True
            self.active = True
            return CommandResult(0, "", "")
        if argv == (
            "systemctl",
            "enable",
            "--now",
            "obsidian-github-daily-progress.timer",
        ):
            self.daily_present = True
            self.daily_enabled = True
            self.daily_active = True
            return CommandResult(0, "", "")
        if argv == ("systemctl", "enable", "obsidian-github-sync.timer"):
            self.enabled = True
            return CommandResult(0, "", "")
        if argv == (
            "systemctl",
            "enable",
            "obsidian-github-daily-progress.timer",
        ):
            self.daily_present = True
            self.daily_enabled = True
            return CommandResult(0, "", "")
        if argv == ("systemctl", "start", "obsidian-github-sync.timer"):
            self.active = True
            return CommandResult(0, "", "")
        if argv == (
            "systemctl",
            "start",
            "obsidian-github-daily-progress.timer",
        ):
            self.daily_present = True
            self.daily_active = True
            return CommandResult(0, "", "")
        if argv == ("systemctl", "daemon-reload"):
            return CommandResult(0, "", "")

        if argv[:1] == (str(self.venv_root / "bin" / "pip"),):
            if self.fail_pip:
                return CommandResult(7, "", "TOPSECRET credential material")
            return CommandResult(0, "", "")

        smoke = str(self.venv_root / "bin" / "obsidian-github-production-smoke")
        if argv == (smoke, "--profile", "safe"):
            if self.fail_profile == "safe":
                return CommandResult(1, "", "safe failure")
            return CommandResult(0, "", "")
        if argv == (smoke, "--profile", "live"):
            if self.fail_profile == "live":
                return CommandResult(1, "", "live failure")
            return CommandResult(0, "", "")

        daily_smoke = str(
            self.venv_root / "bin" / "obsidian-github-daily-production-smoke"
        )
        if argv == (daily_smoke, "--profile", "safe"):
            if self.fail_profile == "daily-safe":
                return CommandResult(1, "", "daily safe failure")
            return CommandResult(0, "", "")

        raise AssertionError(argv)


def _receipt_payload(receipt_dir: Path) -> dict[str, object]:
    paths = list(receipt_dir.glob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def test_update_deploys_exact_target_runs_smokes_and_restores_timer(tmp_path: Path) -> None:
    app_root, venv_root, systemd_dir, receipt_dir = _layout(tmp_path)
    runner = FakeRunner(app_root, venv_root)

    receipt, receipt_path = execute_update(
        target_sha=TARGET,
        app_root=app_root,
        venv_root=venv_root,
        systemd_dir=systemd_dir,
        receipt_dir=receipt_dir,
        daily_revision_env=(
            tmp_path / "etc/obsidian-github-summarizer/revision.env"
        ),
        runner=runner,
        require_root=False,
    )

    assert receipt.result == "success"
    assert receipt.previous_sha == PREVIOUS
    assert receipt.target_sha == TARGET
    assert receipt.safe_smoke == "passed"
    assert receipt.live_smoke == "passed"
    assert receipt.bootstrap_pre_disabled_timer is False
    assert receipt_path.is_file()
    assert runner.current_sha == TARGET
    assert runner.enabled is True
    assert runner.active is True
    assert runner.daily_present is True
    assert runner.daily_enabled is False
    assert runner.daily_active is False
    assert receipt.daily_safe_smoke == "passed"
    assert receipt.daily_live_canary == "not_attempted"
    assert receipt.managed_timer_states[
        "obsidian-github-daily-progress.timer"
    ] == {"enabled": None, "active": None}
    assert (
        tmp_path / "etc/obsidian-github-summarizer/revision.env"
    ).read_text() == f"OBSIDIAN_AUTOMATION_REVISION={TARGET}\n"
    assert (
        "git",
        "-C",
        str(app_root),
        "reset",
        "--hard",
        TARGET,
    ) in runner.calls
    assert (
        "git",
        "-C",
        str(app_root),
        "reset",
        "--hard",
        "origin/main",
    ) not in runner.calls

    for name in UNIT_NAMES:
        assert (systemd_dir / name).read_bytes() == (
            app_root / "examples" / "github-sync" / name
        ).read_bytes()


def test_bootstrap_pre_disabled_timer_restores_original_enabled_active_state(
    tmp_path: Path,
) -> None:
    app_root, venv_root, systemd_dir, receipt_dir = _layout(tmp_path)
    runner = FakeRunner(app_root, venv_root, enabled=False, active=False)

    receipt, _ = execute_update(
        target_sha=TARGET,
        app_root=app_root,
        venv_root=venv_root,
        systemd_dir=systemd_dir,
        receipt_dir=receipt_dir,
        daily_revision_env=(
            tmp_path / "etc/obsidian-github-summarizer/revision.env"
        ),
        runner=runner,
        require_root=False,
        bootstrap_pre_disabled_timer=True,
    )

    assert receipt.result == "success"
    assert receipt.bootstrap_pre_disabled_timer is True
    assert receipt.timer_was_enabled is True
    assert receipt.timer_was_active is True
    assert runner.enabled is True
    assert runner.active is True
    assert ("systemctl", "enable", "--now", "obsidian-github-sync.timer") in runner.calls

    payload = _receipt_payload(receipt_dir)
    assert payload["bootstrap_pre_disabled_timer"] is True
    assert payload["timer_was_enabled"] is True
    assert payload["timer_was_active"] is True


def test_bootstrap_pre_disabled_timer_rejects_nonstopped_timer(tmp_path: Path) -> None:
    app_root, venv_root, systemd_dir, receipt_dir = _layout(tmp_path)
    runner = FakeRunner(app_root, venv_root, enabled=True, active=True)

    with pytest.raises(ProductionUpdateError, match="disabled and inactive"):
        execute_update(
            target_sha=TARGET,
            app_root=app_root,
            venv_root=venv_root,
            systemd_dir=systemd_dir,
            receipt_dir=receipt_dir,
            daily_revision_env=(
                tmp_path / "etc/obsidian-github-summarizer/revision.env"
            ),
            runner=runner,
            require_root=False,
            bootstrap_pre_disabled_timer=True,
        )

    assert ("systemctl", "disable", "--now", "obsidian-github-sync.timer") not in runner.calls
    assert runner.enabled is True
    assert runner.active is True

    payload = _receipt_payload(receipt_dir)
    assert payload["result"] == "failed"
    assert payload["failed_stage"] == "preflight"
    assert payload["bootstrap_pre_disabled_timer"] is True


def test_success_preserves_disabled_inactive_timer_state(tmp_path: Path) -> None:
    app_root, venv_root, systemd_dir, receipt_dir = _layout(tmp_path)
    runner = FakeRunner(app_root, venv_root, enabled=False, active=False)

    receipt, _ = execute_update(
        target_sha=TARGET,
        app_root=app_root,
        venv_root=venv_root,
        systemd_dir=systemd_dir,
        receipt_dir=receipt_dir,
        daily_revision_env=(
            tmp_path / "etc/obsidian-github-summarizer/revision.env"
        ),
        runner=runner,
        require_root=False,
    )

    assert receipt.result == "success"
    assert receipt.timer_was_enabled is False
    assert receipt.timer_was_active is False
    assert runner.enabled is False
    assert runner.active is False
    assert ("systemctl", "enable", "--now", "obsidian-github-sync.timer") not in runner.calls
    assert ("systemctl", "enable", "obsidian-github-sync.timer") not in runner.calls
    assert ("systemctl", "start", "obsidian-github-sync.timer") not in runner.calls


def test_existing_daily_timer_state_is_restored(tmp_path: Path) -> None:
    app_root, venv_root, systemd_dir, receipt_dir = _layout(tmp_path)
    runner = FakeRunner(
        app_root,
        venv_root,
        daily_present=True,
        daily_enabled=True,
        daily_active=True,
    )

    receipt, _ = execute_update(
        target_sha=TARGET,
        app_root=app_root,
        venv_root=venv_root,
        systemd_dir=systemd_dir,
        receipt_dir=receipt_dir,
        daily_revision_env=(
            tmp_path / "etc/obsidian-github-summarizer/revision.env"
        ),
        runner=runner,
        require_root=False,
    )

    assert receipt.result == "success"
    assert receipt.managed_timer_states[
        "obsidian-github-daily-progress.timer"
    ] == {"enabled": True, "active": True}
    assert runner.daily_enabled is True
    assert runner.daily_active is True
    assert (
        "systemctl",
        "disable",
        "--now",
        "obsidian-github-daily-progress.timer",
    ) in runner.calls
    assert (
        "systemctl",
        "enable",
        "--now",
        "obsidian-github-daily-progress.timer",
    ) in runner.calls


def test_invalid_target_uses_safe_receipt_filename(tmp_path: Path) -> None:
    app_root, venv_root, systemd_dir, receipt_dir = _layout(tmp_path)
    runner = FakeRunner(app_root, venv_root)

    with pytest.raises(ProductionUpdateError, match="target SHA"):
        execute_update(
            target_sha="../unsafe-target",
            app_root=app_root,
            venv_root=venv_root,
            systemd_dir=systemd_dir,
            receipt_dir=receipt_dir,
            daily_revision_env=(
                tmp_path / "etc/obsidian-github-summarizer/revision.env"
            ),
            runner=runner,
            require_root=False,
        )

    receipts = list(receipt_dir.glob("*.json"))
    assert len(receipts) == 1
    assert receipts[0].parent == receipt_dir
    assert ".." not in receipts[0].name


def test_dirty_checkout_fails_before_timer_is_touched(tmp_path: Path) -> None:
    app_root, venv_root, systemd_dir, receipt_dir = _layout(tmp_path)
    runner = FakeRunner(app_root, venv_root, dirty=True)

    with pytest.raises(ProductionUpdateError, match="preflight"):
        execute_update(
            target_sha=TARGET,
            app_root=app_root,
            venv_root=venv_root,
            systemd_dir=systemd_dir,
            receipt_dir=receipt_dir,
            daily_revision_env=(
                tmp_path / "etc/obsidian-github-summarizer/revision.env"
            ),
            runner=runner,
            require_root=False,
        )

    assert not any(
        call[:3] == ("systemctl", "disable", "--now")
        for call in runner.calls
    )
    payload = _receipt_payload(receipt_dir)
    assert payload["result"] == "failed"
    assert payload["failed_stage"] == "preflight"


def test_live_smoke_failure_leaves_timer_disabled_and_records_failure(tmp_path: Path) -> None:
    app_root, venv_root, systemd_dir, receipt_dir = _layout(tmp_path)
    runner = FakeRunner(app_root, venv_root, fail_profile="live")

    with pytest.raises(ProductionUpdateError, match="live_smoke"):
        execute_update(
            target_sha=TARGET,
            app_root=app_root,
            venv_root=venv_root,
            systemd_dir=systemd_dir,
            receipt_dir=receipt_dir,
            daily_revision_env=(
                tmp_path / "etc/obsidian-github-summarizer/revision.env"
            ),
            runner=runner,
            require_root=False,
        )

    assert runner.enabled is False
    assert runner.active is False
    assert ("systemctl", "enable", "--now", "obsidian-github-sync.timer") not in runner.calls
    assert runner.calls.count(
        ("systemctl", "disable", "--now", "obsidian-github-sync.timer")
    ) >= 2

    payload = _receipt_payload(receipt_dir)
    assert payload["result"] == "failed"
    assert payload["failed_stage"] == "live_smoke"
    assert payload["safe_smoke"] == "passed"
    assert payload["live_smoke"] == "not_run"


def test_command_stderr_is_not_persisted_in_failure_receipt(tmp_path: Path) -> None:
    app_root, venv_root, systemd_dir, receipt_dir = _layout(tmp_path)
    runner = FakeRunner(app_root, venv_root, fail_pip=True)

    with pytest.raises(ProductionUpdateError, match="install_package"):
        execute_update(
            target_sha=TARGET,
            app_root=app_root,
            venv_root=venv_root,
            systemd_dir=systemd_dir,
            receipt_dir=receipt_dir,
            daily_revision_env=(
                tmp_path / "etc/obsidian-github-summarizer/revision.env"
            ),
            runner=runner,
            require_root=False,
        )

    path = next(receipt_dir.glob("*.json"))
    data = path.read_text(encoding="utf-8")
    assert "TOPSECRET" not in data
    payload = json.loads(data)
    assert payload["failed_stage"] == "install_package"


# #300 — synthetic single-LXC migration compatibility. Never run live commands.
from obsidian_automation.admin_local_maintenance_gate import HOST_LOCK, HOST_PENDING
from obsidian_automation.admin_status import SERVICES
from obsidian_automation.admin_workflow_quiescence import (
    GROUP_UNITS, TIMER_TARGETS, WorkflowObservation,
)
from obsidian_automation.pre_review_production_update import (
    RefreshUpdateInhibit, PreReviewProductionUpdateError,
)
from obsidian_automation.admin_operation_broker import SystemctlAdapter
from obsidian_automation.admin_local_maintenance_gate import (
    cooperating_updater_gate,
)
from obsidian_automation.admin_operation_broker import BrokerRejected

import fcntl
import os


def _coordinated_layout(tmp_path):
    app, venv, systemd, receipts = _layout(tmp_path)
    host = tmp_path / "host-deployments"
    host.mkdir(mode=0o750)
    lock = host / HOST_LOCK
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    inhibit = tmp_path / "run/semantic-refresh-inhibited.json"
    # #300 production coordination must never silently create a replacement
    # stable semantic lock inode while a different updater owns the old one.
    # The disposable test fixture provisions the same reviewed lock first.
    inhibit.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    refresh_lock = inhibit.with_suffix(".lock")
    refresh_lock.touch(mode=0o600)
    refresh_lock.chmod(0o600)
    return app, venv, systemd, receipts, host, inhibit


def _quiescent_github():
    return WorkflowObservation(
        timers={
            spec.timer: {
                "LoadState": "loaded", "UnitFileState": "disabled",
                "ActiveState": "inactive", "SubState": "dead",
                "Unit": TIMER_TARGETS[spec.timer],
                "Job": "0",
                "FragmentPath": f"/etc/systemd/system/{spec.timer}",
            }
            for spec in SERVICES[2:4]
        },
        services={
            unit: {
                "LoadState": "loaded", "ActiveState": "inactive",
                "SubState": "dead", "Type": "oneshot", "Job": "0",
                "MainPID": "0", "ControlPID": "0",
                "FragmentPath": f"/etc/systemd/system/{unit}",
            }
            for unit in GROUP_UNITS["github-sync"]
        },
    )


@pytest.fixture
def guarded_systemd_observer(monkeypatch):
    state = {"observer": _quiescent_github}

    def fake_observe(self, spec):
        assert spec == SERVICES[2]
        return state["observer"]()

    monkeypatch.setattr(SystemctlAdapter, "observe_workflow", fake_observe)
    return state


def _coordinated_args(tmp_path, layout, runner):
    app, venv, systemd, receipts, host, inhibit = layout
    return {
        "target_sha": TARGET, "app_root": app, "venv_root": venv,
        "systemd_dir": systemd, "receipt_dir": receipts,
        "daily_revision_env": tmp_path / "etc/obsidian-github-summarizer/revision.env",
        "runner": runner, "require_root": False,
        "host_receipt_dir": host, "refresh_inhibit_path": inhibit,
        "single_lxc_coordinated": True,
    }


def test_single_lxc_github_update_serializes_with_host_and_inhibits_ai(tmp_path, guarded_systemd_observer):
    layout = _coordinated_layout(tmp_path)
    app, venv, systemd, receipts, host, inhibit = layout
    fake = FakeRunner(app, venv, daily_present=True)
    checked = {"pre_update": 0, "after_quiesce": 0}

    def runner(argv):
        command = tuple(argv)
        if command[:3] == ("systemctl", "disable", "--now"):
            assert inhibit.is_file()
            data = json.loads(inhibit.read_text())
            assert data == {
                "record_version": 1, "target_sha": TARGET, "owner": "github-updater",
            }
            with open(host / HOST_LOCK, "rb") as handle:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(BrokerRejected, match="local_update_busy"):
                with cooperating_updater_gate(
                    host_dir=host, refresh_dir=inhibit.parent,
                    require_root=False,
                ):
                    pass
            checked["pre_update"] += 1
        if command[:4] == ("git", "-C", str(app), "reset"):
            assert checked["after_quiesce"] == 1
            assert inhibit.is_file()
        return fake(command)

    def observe():
        assert inhibit.is_file()
        assert checked["pre_update"] >= 2
        checked["after_quiesce"] += 1
        return _quiescent_github()

    guarded_systemd_observer["observer"] = observe
    args = _coordinated_args(tmp_path, layout, runner)
    result, _ = execute_update(**args)
    assert result.result == "success"
    assert checked["pre_update"] >= 2
    assert checked["after_quiesce"] == 1
    assert not inhibit.exists()
    assert (inhibit.with_suffix(".lock")).is_file()
    assert fake.current_sha == TARGET
    assert fake.enabled and fake.active
    assert fake.daily_enabled is False  # preserved previous inactive Daily state


def test_single_lxc_refuses_missing_host_lock_before_touching_timer(tmp_path, guarded_systemd_observer):
    layout = _coordinated_layout(tmp_path)
    app, venv, _, _, host, inhibit = layout
    (host / HOST_LOCK).unlink()
    runner = FakeRunner(app, venv)
    with pytest.raises(ProductionUpdateError, match="single_lxc_exclusion_unavailable|untrusted host-wide update lock|host-wide update already in progress|host lifecycle reconciliation required"):
        execute_update(**_coordinated_args(tmp_path, layout, runner))
    assert runner.calls == []
    assert not inhibit.exists()


def test_single_lxc_refuses_busy_host_updater_before_touching_timer(tmp_path, guarded_systemd_observer):
    layout = _coordinated_layout(tmp_path)
    app, venv, _, _, host, inhibit = layout
    runner = FakeRunner(app, venv)
    fd = os.open(host / HOST_LOCK, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(ProductionUpdateError, match="single_lxc_exclusion_unavailable|host-wide update already in progress"):
            execute_update(**_coordinated_args(tmp_path, layout, runner))
    finally:
        os.close(fd)
    assert runner.calls == []
    assert not inhibit.exists()


def test_single_lxc_refuses_host_pending_without_mutating(tmp_path, guarded_systemd_observer):
    layout = _coordinated_layout(tmp_path)
    app, venv, _, _, host, inhibit = layout
    (host / HOST_PENDING).write_text("unreconciled")
    runner = FakeRunner(app, venv)
    with pytest.raises(ProductionUpdateError, match="single_lxc_exclusion_unavailable|untrusted host-wide update lock|host-wide update already in progress|host lifecycle reconciliation required"):
        execute_update(**_coordinated_args(tmp_path, layout, runner))
    assert runner.calls == []
    assert not inhibit.exists()


def test_single_lxc_refuses_existing_other_updater_inhibit(tmp_path, guarded_systemd_observer):
    layout = _coordinated_layout(tmp_path)
    app, venv, _, _, _, inhibit = layout
    inhibit.parent.mkdir(parents=True, exist_ok=True)
    predecessor = RefreshUpdateInhibit(
        inhibit, TARGET, "pre-review", require_root=False,
    )
    predecessor.acquire()
    runner = FakeRunner(app, venv)
    try:
        with pytest.raises(ProductionUpdateError, match="single_lxc_exclusion_unavailable"):
            execute_update(**_coordinated_args(tmp_path, layout, runner))
        assert runner.calls == []
        assert json.loads(inhibit.read_text())["owner"] == "pre-review"
    finally:
        predecessor.release()
        predecessor.close()


def test_single_lxc_workflow_busy_leaves_recoverable_inhibit_and_timers_stopped(tmp_path, guarded_systemd_observer):
    layout = _coordinated_layout(tmp_path)
    app, venv, _, receipts, _, inhibit = layout
    runner = FakeRunner(app, venv, daily_present=True, daily_enabled=True, daily_active=True)

    def busy():
        observation = _quiescent_github()
        observation.services["obsidian-github-daily-collect.service"]["Job"] = "80"
        return observation

    guarded_systemd_observer["observer"] = busy
    args = _coordinated_args(tmp_path, layout, runner)
    with pytest.raises(ProductionUpdateError, match="quiesce_workflow"):
        execute_update(**args)
    assert runner.enabled is False
    assert runner.active is False
    assert runner.daily_enabled is False
    assert runner.daily_active is False
    assert json.loads(inhibit.read_text())["owner"] == "github-updater"
    assert _receipt_payload(receipts)["failed_stage"] == "quiesce_workflow"
    # Even a same-target retry must not silently forget the original timer
    # restoration intent captured before the interrupted transaction.
    runner.calls.clear()
    with pytest.raises(ProductionUpdateError, match="single_lxc_update_needs_reconciliation"):
        execute_update(**args)
    assert runner.calls == []


def test_single_lxc_failed_live_smoke_keeps_marker_and_blocks_host_updater(tmp_path, guarded_systemd_observer):
    layout = _coordinated_layout(tmp_path)
    app, venv, _, receipts, _, inhibit = layout
    runner = FakeRunner(app, venv, fail_profile="live")
    with pytest.raises(ProductionUpdateError, match="live_smoke"):
        execute_update(**_coordinated_args(tmp_path, layout, runner))
    assert runner.enabled is False
    assert runner.active is False
    assert json.loads(inhibit.read_text())["owner"] == "github-updater"
    probe = RefreshUpdateInhibit(inhibit, TARGET, "host-runtime", require_root=False)
    with pytest.raises(PreReviewProductionUpdateError):
        probe.acquire(publish=False)
    probe.close()
    assert _receipt_payload(receipts)["failed_stage"] == "live_smoke"


def test_single_lxc_dirty_checkout_never_publishes_inhibit(tmp_path, guarded_systemd_observer):
    layout = _coordinated_layout(tmp_path)
    app, venv, _, receipts, _, inhibit = layout
    runner = FakeRunner(app, venv, dirty=True)
    with pytest.raises(ProductionUpdateError, match="preflight"):
        execute_update(**_coordinated_args(tmp_path, layout, runner))
    assert not inhibit.exists()
    assert _receipt_payload(receipts)["failed_stage"] == "preflight"
    assert not any(c[:2] == ("systemctl", "disable") for c in runner.calls)


def test_single_lxc_refuses_invalid_mode_before_any_action(tmp_path, guarded_systemd_observer):
    layout = _coordinated_layout(tmp_path)
    app, venv, _, _, _, _ = layout
    runner = FakeRunner(app, venv)
    args = _coordinated_args(tmp_path, layout, runner)
    args["single_lxc_coordinated"] = "yes"
    with pytest.raises(ProductionUpdateError, match="invalid_single_lxc_mode"):
        execute_update(**args)
    assert runner.calls == []


def test_single_lxc_first_timer_stop_failure_contains_both_timers(
    tmp_path, guarded_systemd_observer,
):
    layout = _coordinated_layout(tmp_path)
    app, venv, _, receipts, _, inhibit = layout
    fake = FakeRunner(
        app, venv, daily_present=True, daily_enabled=True, daily_active=True,
    )
    stopped = [0]

    def runner(argv):
        command = tuple(argv)
        if command[:2] == ("systemctl", "disable") and command[-1] in (
            "obsidian-github-sync.timer", "obsidian-github-daily-progress.timer",
        ):
            stopped[0] += 1
            if stopped[0] == 1:
                assert inhibit.is_file()
                return CommandResult(1, "", "first disable failed")
        return fake(command)

    with pytest.raises(ProductionUpdateError, match="stop_timers"):
        execute_update(**_coordinated_args(tmp_path, layout, runner))
    # A stop failure cannot fall through to package checkout/installation.
    assert fake.current_sha == PREVIOUS
    assert ("systemctl", "disable", "--now", "obsidian-github-sync.timer") in fake.calls
    assert ("systemctl", "disable", "--now", "obsidian-github-daily-progress.timer") in fake.calls
    assert not fake.enabled and not fake.active
    assert not fake.daily_enabled and not fake.daily_active
    assert json.loads(inhibit.read_text())["owner"] == "github-updater"
    assert _receipt_payload(receipts)["failed_stage"] == "stop_timers"


def test_single_lxc_preexisting_same_sha_marker_is_not_automatically_replayed(
    tmp_path, guarded_systemd_observer,
):
    layout = _coordinated_layout(tmp_path)
    app, venv, _, _, _, inhibit = layout
    inhibit.parent.mkdir(parents=True, exist_ok=True)
    prior = RefreshUpdateInhibit(
        inhibit, TARGET, "github-updater", require_root=False,
    )
    prior.acquire()
    prior.close()
    before = inhibit.read_bytes()
    runner = FakeRunner(app, venv)
    with pytest.raises(
        ProductionUpdateError, match="single_lxc_update_needs_reconciliation"
    ):
        execute_update(**_coordinated_args(tmp_path, layout, runner))
    assert inhibit.read_bytes() == before
    assert runner.calls == []


def test_single_lxc_requires_full_git_sha_before_any_lock_creation(
    tmp_path, guarded_systemd_observer,
):
    layout = _coordinated_layout(tmp_path)
    app, venv, _, _, _, inhibit = layout
    args = _coordinated_args(tmp_path, layout, FakeRunner(app, venv))
    args["target_sha"] = "../private-source"
    original_lock_inode = inhibit.with_suffix(".lock").stat().st_ino
    with pytest.raises(ProductionUpdateError, match="invalid_single_lxc_target_sha"):
        execute_update(**args)
    assert not inhibit.exists()
    assert inhibit.with_suffix(".lock").stat().st_ino == original_lock_inode


def test_coordinated_inhibitor_release_failure_contains_restored_timers(
    tmp_path, guarded_systemd_observer, monkeypatch,
):
    # A failed marker fsync/unlink AFTER timer restoration must not leave
    # recurrence running with a reported success receipt.
    layout = _coordinated_layout(tmp_path)
    app, venv, _, receipts, _, inhibit = layout
    fake = FakeRunner(
        app, venv, daily_present=True, daily_enabled=True, daily_active=True,
    )
    def failed_release(self):
        raise PreReviewProductionUpdateError("injected_release_failure")
    monkeypatch.setattr(RefreshUpdateInhibit, "release", failed_release)
    with pytest.raises(ProductionUpdateError, match="release_inhibit"):
        execute_update(**_coordinated_args(tmp_path, layout, fake))
    assert not fake.enabled and not fake.active
    assert not fake.daily_enabled and not fake.daily_active
    assert inhibit.is_file()
    assert _receipt_payload(receipts)["failed_stage"] == "release_inhibit"


def test_standalone_live_guard_validates_extra_installed_github_inventory():
    # Root-mode standalone updater reuses the main Stage service enumeration
    # before package/unit replacement; synthetic nonroot paths need only the
    # immutable fixed services and must not query host systemd.
    import inspect
    source = inspect.getsource(execute_update)
    assert 'if require_root:' in source
    # Keep this regression resilient to Python multiline import layout while
    # checking that BOTH actual installed service and timer inventories are
    # called under the root-only coordinated guard before any package update.
    assert '_assert_known_timer_inventory(runner, systemd_dir)' in source
    assert '_require_idle_services(runner, systemd_dir)' in source
    assert source.index('_assert_known_timer_inventory(runner, systemd_dir)') < source.index(
        '_require_idle_services(runner, systemd_dir)'
    )
