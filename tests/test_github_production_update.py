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

    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir()
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
    ) -> None:
        self.app_root = app_root
        self.venv_root = venv_root
        self.dirty = dirty
        self.enabled = enabled
        self.active = active
        self.fail_profile = fail_profile
        self.fail_pip = fail_pip
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
        if argv == ("systemctl", "is-active", "obsidian-github-sync.timer"):
            return CommandResult(0 if self.active else 3, "active\n" if self.active else "inactive\n", "")
        if argv == ("systemctl", "disable", "--now", "obsidian-github-sync.timer"):
            self.enabled = False
            self.active = False
            return CommandResult(0, "", "")
        if argv == ("systemctl", "enable", "--now", "obsidian-github-sync.timer"):
            self.enabled = True
            self.active = True
            return CommandResult(0, "", "")
        if argv == ("systemctl", "enable", "obsidian-github-sync.timer"):
            self.enabled = True
            return CommandResult(0, "", "")
        if argv == ("systemctl", "start", "obsidian-github-sync.timer"):
            self.active = True
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
        runner=runner,
        require_root=False,
    )

    assert receipt.result == "success"
    assert receipt.previous_sha == PREVIOUS
    assert receipt.target_sha == TARGET
    assert receipt.safe_smoke == "passed"
    assert receipt.live_smoke == "passed"
    assert receipt_path.is_file()
    assert runner.current_sha == TARGET
    assert runner.enabled is True
    assert runner.active is True
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
            runner=runner,
            require_root=False,
        )

    path = next(receipt_dir.glob("*.json"))
    data = path.read_text(encoding="utf-8")
    assert "TOPSECRET" not in data
    payload = json.loads(data)
    assert payload["failed_stage"] == "install_package"
