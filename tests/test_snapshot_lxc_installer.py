from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import stat
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "snapshot_lxc_installer_test_module", ROOT / "tools/install_snapshot_lxc.py"
)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=75"]\n',
        encoding="utf-8",
    )
    return source


def test_build_plan_is_pure_and_contains_single_nonroot_service(tmp_path: Path) -> None:
    source = _source(tmp_path)
    root = tmp_path / "mock-root"
    root.mkdir()

    plan = installer.build_plan(source, target_root=root)

    assert list(root.iterdir()) == []
    value = json.loads(plan.to_json())
    assert value["mode"] == "plan_only"
    assert value["account"] == "obsidian-snapshot"
    assert value["state_root"] == "/var/lib/obsidian-snapshot"
    commands = [step["argv"] for step in value["steps"] if step["kind"] == "command"]
    assert [command for command in commands if command[0] == "systemctl"] == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "obsidian-snapshot.service"],
    ]
    assert [command for command in commands if command[0] == "apt-get"] == [
        ["apt-get", "update"],
        [
            "apt-get", "install", "--yes", "python3", "python3-venv",
            "python3-setuptools-whl", "python3-wheel-whl",
            "git", "rclone", "ca-certificates",
        ],
    ]
    assert any(command[0] == "useradd" for command in commands)
    assert all(
        "--no-index" in command
        for command in commands
        if command and command[0].endswith("/bin/pip")
    )
    unit_step = next(step for step in value["steps"] if step["kind"] == "write_file")
    assert unit_step["path"] == "/etc/systemd/system/obsidian-snapshot.service"
    assert "User=obsidian-snapshot\n" in unit_step["content"]
    assert "Restart=on-failure\n" in unit_step["content"]
    assert "Restart=always" not in unit_step["content"]
    assert ".timer" not in unit_step["content"]


def test_fake_execution_writes_only_inside_disposable_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    root = tmp_path / "mock-root"
    root.mkdir()
    plan = installer.build_plan(source, target_root=root)
    calls: list[tuple[str, ...]] = []

    def fake_runner(argv):
        command = tuple(str(item) for item in argv)
        calls.append(command)
        if command == ("getent", "passwd", "obsidian-snapshot"):
            return installer.CommandResult(2)
        return installer.CommandResult(0)

    def forbid_subprocess(*_args, **_kwargs):
        raise AssertionError("fake execution must never launch a host command")

    monkeypatch.setattr(installer.subprocess, "run", forbid_subprocess)
    installer.execute_plan(plan, fake_runner, require_root=False)

    unit = root / "etc/systemd/system/obsidian-snapshot.service"
    assert unit.is_file()
    assert stat.S_IMODE(unit.stat().st_mode) == 0o644
    assert "Restart=on-failure" in unit.read_text(encoding="utf-8")
    assert calls[0] == ("getent", "passwd", "obsidian-snapshot")
    assert ("apt-get", "update") in calls
    assert ("useradd", "--system", "--user-group", "--home-dir", "/var/lib/obsidian-snapshot",
            "--no-create-home", "--shell", "/usr/sbin/nologin", "obsidian-snapshot") in calls
    assert ("systemctl", "enable", "--now", "obsidian-snapshot.service") in calls


def test_executor_requires_mock_mode_for_non_host_root(tmp_path: Path) -> None:
    source = _source(tmp_path)
    root = tmp_path / "mock-root"
    root.mkdir()
    plan = installer.build_plan(source, target_root=root)

    with pytest.raises(installer.InstallerError, match="non_host_target_requires_test_mode"):
        installer.execute_plan(plan, lambda _argv: installer.CommandResult(0))


def test_executor_refuses_preexisting_fresh_install_paths_before_commands(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    root = tmp_path / "mock-root"
    (root / "opt/obsidian-snapshot").mkdir(parents=True)
    plan = installer.build_plan(source, target_root=root)
    calls: list[tuple[str, ...]] = []

    def fake_runner(argv):
        calls.append(tuple(argv))
        return installer.CommandResult(2)

    with pytest.raises(installer.InstallerError, match="fresh_install_destination_already_exists"):
        installer.execute_plan(plan, fake_runner, require_root=False)

    assert calls == []


def test_existing_service_account_fails_before_install_steps(tmp_path: Path) -> None:
    source = _source(tmp_path)
    root = tmp_path / "mock-root"
    root.mkdir()
    plan = installer.build_plan(source, target_root=root)
    calls: list[tuple[str, ...]] = []

    def fake_runner(argv):
        calls.append(tuple(argv))
        return installer.CommandResult(0)

    with pytest.raises(installer.InstallerError, match="service_account_already_exists"):
        installer.execute_plan(plan, fake_runner, require_root=False)

    assert calls == [("getent", "passwd", "obsidian-snapshot")]
