from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.pre_review_production_update import (
    CommandResult,
    PreReviewProductionUpdateError,
    MIRROR_SERVICE_UNIT,
    MIRROR_TIMER_UNIT,
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
    ) -> None:
        self.systemd_dir = systemd_dir
        self.timer_exists = timer_exists
        self.enabled = enabled
        self.active = active
        self.fail_install = fail_install
        self.mirror_enabled = mirror_enabled
        self.mirror_active = mirror_active
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
        if (
            len(command) == 3
            and command[:2] == ("systemctl", "stop")
            and command[2].startswith("obsidian-pre-review-")
            and command[2].endswith(".service")
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
            runner=runner,
            require_root=False,
            bootstrap_mirror_pre_disabled=True,
        )
