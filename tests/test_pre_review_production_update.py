from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from obsidian_automation.pre_review_production_update import (
    CommandResult,
    PreReviewProductionUpdateError,
    RefreshUpdateInhibit,
    MIRROR_SERVICE_UNIT,
    MIRROR_TIMER_UNIT,
    SEMANTIC_REFRESH_SERVICES,
    PRE_REVIEW_SERVICES,
    REQUIRED_UNITS,
    TIMER_UNIT,
    execute_update,
)


PREVIOUS = "a" * 40
TARGET = "b" * 40


def _layout(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    app_root = tmp_path / "app"
    source = app_root / "examples" / "ai"
    source.mkdir(parents=True)
    for name in REQUIRED_UNITS:
        (source / name).write_text(
            f"[Unit]\nDescription={name}\n",
            encoding="utf-8",
        )

    venv_root = tmp_path / "venv"
    bin_dir = venv_root / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "obsidian-pre-review-production-smoke").write_text(
        "#!/bin/sh\n",
        encoding="utf-8",
    )

    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    config_dir = tmp_path / "etc" / "obsidian-ai"
    config_dir.mkdir(parents=True)
    revision_env = config_dir / "pre-review-revision.env"
    return app_root, venv_root, systemd_dir, receipt_dir, revision_env


class Runner:
    def __init__(
        self,
        *,
        systemd_dir: Path,
        timer_exists: bool,
        enabled: bool = False,
        active: bool = False,
        fail_install: bool = False,
        mirror_enabled: bool = True,
        mirror_active: bool = True,
        refresh_exists: bool = True,
    ) -> None:
        self.systemd_dir = systemd_dir
        self.timer_exists = timer_exists
        self.enabled = enabled
        self.active = active
        self.fail_install = fail_install
        self.mirror_enabled = mirror_enabled
        self.mirror_active = mirror_active
        self.refresh_exists = refresh_exists
        self.head = PREVIOUS
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, argv) -> CommandResult:
        command = tuple(str(item) for item in argv)
        self.commands.append(command)

        if command[-2:] == ("branch", "--show-current"):
            return CommandResult(0, "main\n", "")
        if command[-2:] == ("status", "--porcelain"):
            return CommandResult(0, "", "")
        if command[-2:] == ("rev-parse", "HEAD"):
            return CommandResult(0, self.head + "\n", "")
        if "fetch" in command:
            return CommandResult(0, "", "")
        if "rev-parse" in command and "--verify" in command:
            return CommandResult(0, TARGET + "\n", "")
        if "merge-base" in command:
            return CommandResult(0, "", "")
        if "reset" in command and "--hard" in command:
            self.head = command[-1]
            return CommandResult(0, "", "")

        if command == ("systemctl", "is-enabled", MIRROR_TIMER_UNIT):
            return CommandResult(
                0 if self.mirror_enabled else 1,
                ("enabled" if self.mirror_enabled else "disabled") + "\n",
                "",
            )
        if command == ("systemctl", "is-active", MIRROR_TIMER_UNIT):
            return CommandResult(
                0 if self.mirror_active else 3,
                ("active" if self.mirror_active else "inactive") + "\n",
                "",
            )
        if command == ("systemctl", "disable", "--now", MIRROR_TIMER_UNIT):
            self.mirror_enabled = False
            self.mirror_active = False
            return CommandResult(0, "", "")
        if command == ("systemctl", "enable", "--now", MIRROR_TIMER_UNIT):
            self.mirror_enabled = True
            self.mirror_active = True
            return CommandResult(0, "", "")
        if command == ("systemctl", "enable", MIRROR_TIMER_UNIT):
            self.mirror_enabled = True
            return CommandResult(0, "", "")
        if command == ("systemctl", "disable", MIRROR_TIMER_UNIT):
            self.mirror_enabled = False
            return CommandResult(0, "", "")
        if command == ("systemctl", "start", MIRROR_TIMER_UNIT):
            self.mirror_active = True
            return CommandResult(0, "", "")
        if command == ("systemctl", "stop", MIRROR_TIMER_UNIT):
            self.mirror_active = False
            return CommandResult(0, "", "")
        if command == ("systemctl", "stop", MIRROR_SERVICE_UNIT):
            return CommandResult(0, "", "")
        if command[:2] == ("systemctl", "show") and command[2] in SEMANTIC_REFRESH_SERVICES:
            if "--value" in command:
                return CommandResult(0, "loaded\n" if self.refresh_exists else "not-found\n", "")
            return CommandResult(0, "ActiveState=inactive\nMainPID=0\nControlPID=0\nJob=\n", "")
        if command == ("systemctl", "stop", *SEMANTIC_REFRESH_SERVICES):
            return CommandResult(0, "", "")
        if (
            len(command) == 3
            and command[:2] == ("systemctl", "stop")
            and command[2] in PRE_REVIEW_SERVICES
        ):
            return CommandResult(0, "", "")

        if command == ("systemctl", "is-enabled", TIMER_UNIT):
            if not self.timer_exists:
                return CommandResult(1, "not-found\n", "")
            return CommandResult(0 if self.enabled else 1, ("enabled" if self.enabled else "disabled") + "\n", "")
        if command == ("systemctl", "is-active", TIMER_UNIT):
            if not self.timer_exists:
                return CommandResult(3, "inactive\n", "")
            return CommandResult(0 if self.active else 3, ("active" if self.active else "inactive") + "\n", "")
        if command == ("systemctl", "daemon-reload"):
            self.timer_exists = (self.systemd_dir / TIMER_UNIT).is_file()
            return CommandResult(0, "", "")
        if command == ("systemctl", "disable", "--now", TIMER_UNIT):
            self.enabled = False
            self.active = False
            return CommandResult(0, "", "")
        if command == ("systemctl", "enable", "--now", TIMER_UNIT):
            self.timer_exists = True
            self.enabled = True
            self.active = True
            return CommandResult(0, "", "")
        if command == ("systemctl", "enable", TIMER_UNIT):
            self.timer_exists = True
            self.enabled = True
            return CommandResult(0, "", "")
        if command == ("systemctl", "disable", TIMER_UNIT):
            self.timer_exists = True
            self.enabled = False
            return CommandResult(0, "", "")
        if command == ("systemctl", "start", TIMER_UNIT):
            self.timer_exists = True
            self.active = True
            return CommandResult(0, "", "")
        if command == ("systemctl", "stop", TIMER_UNIT):
            self.active = False
            return CommandResult(0, "", "")

        if command and command[0].endswith("/pip"):
            if self.fail_install:
                return CommandResult(1, "", "TOPSECRET install failure")
            return CommandResult(0, "", "")
        if command and command[0].endswith(
            "/obsidian-pre-review-production-smoke"
        ):
            return CommandResult(0, '{"status":"passed"}\n', "")
        if command[:1] == ("sh",) and command[1].endswith("bootstrap-pre-review-authority.sh"):
            return CommandResult(0, "", "")
        if command[-1:] == ("--semantic-refresh-only",):
            return CommandResult(0, "", "")

        raise AssertionError(f"unexpected command: {command!r}")


def test_first_install_leaves_new_timer_disabled_and_installs_exact_revision(
    tmp_path: Path,
) -> None:
    app, venv, systemd, receipts, revision_env = _layout(tmp_path)
    obsolete = systemd / "obsidian-pre-review-evaluator.timer"
    obsolete.write_text("old\n", encoding="utf-8")
    runner = Runner(systemd_dir=systemd, timer_exists=False)

    receipt, receipt_path = execute_update(
        target_sha=TARGET,
        app_root=app,
        venv_root=venv,
        systemd_dir=systemd,
        receipt_dir=receipts,
        revision_env=revision_env,
        refresh_inhibit_path=receipts / "semantic-refresh-inhibited.json",
        runner=runner,
        require_root=False,
    )

    assert receipt.result == "success"
    assert receipt.timer_existed is False
    assert receipt.first_install_left_disabled is True
    assert receipt.safe_smoke == "passed"
    assert receipt.disposable_canary == "pending_manual_acceptance"
    assert runner.enabled is False
    assert runner.active is False
    assert runner.mirror_enabled is True
    assert runner.mirror_active is True
    assert runner.head == TARGET

    assert revision_env.read_text(encoding="utf-8") == (
        f"OBSIDIAN_AUTOMATION_REVISION={TARGET}\n"
    )
    for name in REQUIRED_UNITS:
        assert (systemd / name).is_file()
    assert not obsolete.exists()

    pip_commands = [
        command for command in runner.commands if command and command[0].endswith("/pip")
    ]
    assert len(pip_commands) == 1
    assert "--force-reinstall" in pip_commands[0]
    assert "--no-deps" in pip_commands[0]
    assert "-e" not in pip_commands[0]

    assert ("systemctl", "enable", "--now", TIMER_UNIT) not in runner.commands
    assert receipt_path.is_file()
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert payload["target_sha"] == TARGET
    assert payload["first_install_left_disabled"] is True


def test_existing_enabled_active_timer_is_restored_after_safe_update(
    tmp_path: Path,
) -> None:
    app, venv, systemd, receipts, revision_env = _layout(tmp_path)
    runner = Runner(
        systemd_dir=systemd,
        timer_exists=True,
        enabled=True,
        active=True,
    )

    receipt, _ = execute_update(
        target_sha=TARGET,
        app_root=app,
        venv_root=venv,
        systemd_dir=systemd,
        receipt_dir=receipts,
        revision_env=revision_env,
        refresh_inhibit_path=receipts / "semantic-refresh-inhibited.json",
        runner=runner,
        require_root=False,
    )

    assert receipt.timer_existed is True
    assert receipt.timer_was_enabled is True
    assert receipt.timer_was_active is True
    assert receipt.first_install_left_disabled is False
    assert runner.enabled is True
    assert runner.active is True
    assert runner.mirror_enabled is True
    assert runner.mirror_active is True
    assert ("systemctl", "enable", "--now", TIMER_UNIT) in runner.commands


def test_existing_disabled_inactive_timer_stays_disabled(
    tmp_path: Path,
) -> None:
    app, venv, systemd, receipts, revision_env = _layout(tmp_path)
    runner = Runner(
        systemd_dir=systemd,
        timer_exists=True,
        enabled=False,
        active=False,
    )

    receipt, _ = execute_update(
        target_sha=TARGET,
        app_root=app,
        venv_root=venv,
        systemd_dir=systemd,
        receipt_dir=receipts,
        revision_env=revision_env,
        refresh_inhibit_path=receipts / "semantic-refresh-inhibited.json",
        runner=runner,
        require_root=False,
    )

    assert receipt.result == "success"
    assert runner.enabled is False
    assert runner.active is False


def test_failure_after_timer_stop_leaves_timer_disabled_and_persists_safe_receipt(
    tmp_path: Path,
) -> None:
    app, venv, systemd, receipts, revision_env = _layout(tmp_path)
    runner = Runner(
        systemd_dir=systemd,
        timer_exists=True,
        enabled=True,
        active=True,
        fail_install=True,
    )

    with pytest.raises(
        PreReviewProductionUpdateError,
        match="install_package",
    ):
        execute_update(
            target_sha=TARGET,
            app_root=app,
            venv_root=venv,
            systemd_dir=systemd,
            receipt_dir=receipts,
            revision_env=revision_env,
            refresh_inhibit_path=receipts / "semantic-refresh-inhibited.json",
            runner=runner,
            require_root=False,
        )

    assert runner.enabled is False
    assert runner.active is False
    assert runner.mirror_enabled is False
    assert runner.mirror_active is False
    paths = list(receipts.glob("*.pre-review.json"))
    assert len(paths) == 1
    text = paths[0].read_text(encoding="utf-8")
    assert "TOPSECRET" not in text
    payload = json.loads(text)
    assert payload["result"] == "failed"
    assert payload["failed_stage"] == "install_package"


def test_dirty_checkout_fails_before_timer_is_touched(tmp_path: Path) -> None:
    app, venv, systemd, receipts, revision_env = _layout(tmp_path)
    base = Runner(
        systemd_dir=systemd,
        timer_exists=True,
        enabled=True,
        active=True,
    )

    def dirty_runner(argv):
        command = tuple(str(item) for item in argv)
        if command[-2:] == ("status", "--porcelain"):
            base.commands.append(command)
            return CommandResult(0, " M local-change\n", "")
        return base(command)

    with pytest.raises(
        PreReviewProductionUpdateError,
        match="preflight",
    ):
        execute_update(
            target_sha=TARGET,
            app_root=app,
            venv_root=venv,
            systemd_dir=systemd,
            receipt_dir=receipts,
            revision_env=revision_env,
            refresh_inhibit_path=receipts / "semantic-refresh-inhibited.json",
            runner=dirty_runner,
            require_root=False,
        )

    assert ("systemctl", "disable", "--now", TIMER_UNIT) not in base.commands
    assert ("systemctl", "disable", "--now", MIRROR_TIMER_UNIT) not in base.commands
    assert base.enabled is True
    assert base.active is True
    assert base.mirror_enabled is True
    assert base.mirror_active is True


def test_bootstrap_pre_disabled_mirror_is_logically_restored(
    tmp_path: Path,
) -> None:
    app, venv, systemd, receipts, revision_env = _layout(tmp_path)
    runner = Runner(
        systemd_dir=systemd,
        timer_exists=False,
        mirror_enabled=False,
        mirror_active=False,
    )

    receipt, _ = execute_update(
        target_sha=TARGET,
        app_root=app,
        venv_root=venv,
        systemd_dir=systemd,
        receipt_dir=receipts,
        revision_env=revision_env,
        refresh_inhibit_path=receipts / "semantic-refresh-inhibited.json",
        runner=runner,
        require_root=False,
        bootstrap_mirror_pre_disabled=True,
    )

    assert receipt.bootstrap_mirror_pre_disabled is True
    assert receipt.mirror_timer_was_enabled is True
    assert receipt.mirror_timer_was_active is True
    assert runner.mirror_enabled is True
    assert runner.mirror_active is True
    assert runner.enabled is False
    assert runner.active is False


def test_bootstrap_pre_disabled_mode_rejects_running_mirror(
    tmp_path: Path,
) -> None:
    app, venv, systemd, receipts, revision_env = _layout(tmp_path)
    runner = Runner(
        systemd_dir=systemd,
        timer_exists=False,
        mirror_enabled=True,
        mirror_active=True,
    )

    with pytest.raises(
        PreReviewProductionUpdateError,
        match="pre-disabled",
    ):
        execute_update(
            target_sha=TARGET,
            app_root=app,
            venv_root=venv,
            systemd_dir=systemd,
            receipt_dir=receipts,
            revision_env=revision_env,
            refresh_inhibit_path=receipts / "semantic-refresh-inhibited.json",
            runner=runner,
            require_root=False,
            bootstrap_mirror_pre_disabled=True,
        )


def test_refresh_is_inhibited_and_stopped_before_package_mutation(tmp_path: Path) -> None:
    app, venv, systemd, receipts, revision_env = _layout(tmp_path)
    gate = receipts / "semantic-refresh-inhibited.json"
    base = Runner(systemd_dir=systemd, timer_exists=True, enabled=True, active=True)

    def runner(argv):
        command = tuple(map(str, argv))
        if command[:2] in {("systemctl", "disable"), ("systemctl", "stop"), ("systemctl", "enable")}:
            # A queued OnSuccess callback cannot start at any of these points.
            assert gate.is_file()
        if command and command[0].endswith("/pip"):
            assert gate.is_file()
            assert ("systemctl", "stop", *SEMANTIC_REFRESH_SERVICES) in base.commands
        return base(command)

    execute_update(
        target_sha=TARGET, app_root=app, venv_root=venv, systemd_dir=systemd,
        receipt_dir=receipts, revision_env=revision_env, runner=runner,
        require_root=False, refresh_inhibit_path=gate,
    )
    commands = base.commands
    assert commands.index(("systemctl", "stop", MIRROR_SERVICE_UNIT)) < commands.index(
        ("systemctl", "stop", *SEMANTIC_REFRESH_SERVICES)
    )
    authority = next(i for i, c in enumerate(commands) if c[:1] == ("sh",))
    smoke = next(i for i, c in enumerate(commands) if c[0].endswith("production-smoke"))
    assert authority < smoke
    assert not gate.exists()


def test_legacy_captures_restored_timers_and_head_after_host_releases_shared_lock(tmp_path: Path) -> None:
    app, venv, systemd, receipts, revision_env = _layout(tmp_path)
    gate = receipts / "semantic-refresh-inhibited.json"
    base = Runner(
        systemd_dir=systemd, timer_exists=True, enabled=False, active=False,
        mirror_enabled=False, mirror_active=False,
    )
    predecessor_sha = "c" * 40
    predecessor = RefreshUpdateInhibit(gate, predecessor_sha, "host-runtime", require_root=False)
    predecessor.acquire()
    predecessor_restored = False

    def runner(argv):
        nonlocal predecessor_restored
        command = tuple(map(str, argv))
        result = base(command)
        if not predecessor_restored and command == ("systemctl", "is-active", MIRROR_TIMER_UNIT):
            # Return the final old preflight value, then let the host update
            # complete before the legacy updater acquires the shared lock.
            base.enabled = base.active = base.mirror_enabled = base.mirror_active = True
            base.head = predecessor_sha
            predecessor.release()
            predecessor.close()
            predecessor_restored = True
        return result

    try:
        receipt, _ = execute_update(
            target_sha=TARGET, app_root=app, venv_root=venv, systemd_dir=systemd,
            receipt_dir=receipts, revision_env=revision_env, runner=runner,
            require_root=False, refresh_inhibit_path=gate,
        )
    finally:
        predecessor.close()
    assert predecessor_restored
    assert receipt.previous_sha == predecessor_sha
    assert receipt.timer_was_enabled and receipt.timer_was_active
    assert receipt.mirror_timer_was_enabled and receipt.mirror_timer_was_active
    assert base.enabled and base.active and base.mirror_enabled and base.mirror_active
    assert not gate.exists()


def test_legacy_revalidates_timers_under_shared_lock_before_publishing_marker(tmp_path: Path) -> None:
    app, venv, systemd, receipts, revision_env = _layout(tmp_path)
    gate = receipts / "semantic-refresh-inhibited.json"
    base = Runner(systemd_dir=systemd, timer_exists=True, enabled=True, active=True)
    changed = False

    def runner(argv):
        nonlocal changed
        command = tuple(map(str, argv))
        if changed and command == ("systemctl", "is-enabled", TIMER_UNIT):
            return CommandResult(1, "masked\n", "")
        result = base(command)
        if command == ("systemctl", "is-active", MIRROR_TIMER_UNIT):
            changed = True
        return result

    with pytest.raises(PreReviewProductionUpdateError, match="enablement state"):
        execute_update(
            target_sha=TARGET, app_root=app, venv_root=venv, systemd_dir=systemd,
            receipt_dir=receipts, revision_env=revision_env, runner=runner,
            require_root=False, refresh_inhibit_path=gate,
        )
    assert not gate.exists()
    assert base.head == PREVIOUS
    assert not any(c[:2] == ("systemctl", "disable") for c in base.commands)


def test_absent_refresh_units_are_supported_on_first_upgrade(tmp_path: Path) -> None:
    app, venv, systemd, receipts, revision_env = _layout(tmp_path)
    runner = Runner(systemd_dir=systemd, timer_exists=True, refresh_exists=False)
    execute_update(
        target_sha=TARGET, app_root=app, venv_root=venv, systemd_dir=systemd,
        receipt_dir=receipts, revision_env=revision_env, runner=runner,
        require_root=False, refresh_inhibit_path=receipts / "inhibit.json",
    )
    assert ("systemctl", "stop", *SEMANTIC_REFRESH_SERVICES) not in runner.commands
    assert all((systemd / unit).is_file() for unit in SEMANTIC_REFRESH_SERVICES)


def test_queued_refresh_job_blocks_legacy_update_before_checkout(tmp_path: Path) -> None:
    app, venv, systemd, receipts, revision_env = _layout(tmp_path)
    gate = receipts / "inhibit.json"
    base = Runner(systemd_dir=systemd, timer_exists=True, enabled=True, active=True)

    def runner(argv):
        command = tuple(map(str, argv))
        if command[:2] == ("systemctl", "show") and "--property=ActiveState" in command:
            return CommandResult(0, "ActiveState=inactive\nMainPID=0\nControlPID=0\nJob=17\n", "")
        return base(command)

    with pytest.raises(PreReviewProductionUpdateError, match="did not become inert"):
        execute_update(
            target_sha=TARGET, app_root=app, venv_root=venv, systemd_dir=systemd,
            receipt_dir=receipts, revision_env=revision_env, runner=runner,
            require_root=False, refresh_inhibit_path=gate,
        )
    assert gate.is_file()
    assert base.head == PREVIOUS
    assert not any(c[0].endswith("/pip") for c in base.commands)
    assert not base.active and not base.mirror_active


def test_interrupted_legacy_restore_keeps_timers_and_refresh_inert(tmp_path: Path) -> None:
    app, venv, systemd, receipts, revision_env = _layout(tmp_path)
    gate = receipts / "inhibit.json"
    base = Runner(systemd_dir=systemd, timer_exists=True, enabled=True, active=True)

    def runner(argv):
        command = tuple(map(str, argv))
        if command == ("systemctl", "enable", "--now", TIMER_UNIT):
            # The mirror timer was already restored when interruption arrived.
            assert base.mirror_active
            raise KeyboardInterrupt
        return base(command)

    with pytest.raises(PreReviewProductionUpdateError, match="restore_pre_review_timer"):
        execute_update(
            target_sha=TARGET, app_root=app, venv_root=venv, systemd_dir=systemd,
            receipt_dir=receipts, revision_env=revision_env, runner=runner,
            require_root=False, refresh_inhibit_path=gate,
        )
    assert gate.exists()
    assert not base.active and not base.mirror_active
    assert not base.enabled and not base.mirror_enabled


@pytest.mark.parametrize("other_target,other_owner", [(PREVIOUS, "host-runtime"), (TARGET, "pre-review")])
def test_inhibit_preserves_other_update_intent(tmp_path: Path, other_target: str, other_owner: str) -> None:
    path = tmp_path / "inhibit.json"
    first = RefreshUpdateInhibit(path, TARGET, "host-runtime", require_root=False)
    first.acquire()
    before = path.read_bytes()
    first.close()  # Interrupted updates intentionally leave the marker.
    other = RefreshUpdateInhibit(path, other_target, other_owner, require_root=False)
    with pytest.raises(PreReviewProductionUpdateError, match="another target or updater"):
        other.acquire()
    assert path.read_bytes() == before
    retry = RefreshUpdateInhibit(path, TARGET, "host-runtime", require_root=False)
    retry.acquire()
    retry.release()
    retry.close()
    assert not path.exists()


def test_shared_inhibit_serializes_both_updater_paths(tmp_path: Path) -> None:
    path = tmp_path / "inhibit.json"
    first = RefreshUpdateInhibit(path, TARGET, "host-runtime", require_root=False)
    second = RefreshUpdateInhibit(path, TARGET, "pre-review", require_root=False)
    first.acquire()
    try:
        assert path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(PreReviewProductionUpdateError, match="already in progress"):
            second.acquire()
        first.release()
    finally:
        first.close()


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "public"])
def test_unsafe_inhibit_is_rejected_without_replacement(tmp_path: Path, unsafe: str) -> None:
    path = tmp_path / "inhibit.json"
    value = {"record_version": 1, "target_sha": TARGET, "owner": "host-runtime"}
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(value))
    outside.chmod(0o600)
    if unsafe == "symlink":
        path.symlink_to(outside)
    elif unsafe == "hardlink":
        os.link(outside, path)
    else:
        path.write_text(json.dumps(value))
        path.chmod(0o644)
    before = outside.read_bytes()
    gate = RefreshUpdateInhibit(path, TARGET, "host-runtime", require_root=False)
    with pytest.raises(PreReviewProductionUpdateError, match="unsafe"):
        gate.acquire()
    assert outside.read_bytes() == before
