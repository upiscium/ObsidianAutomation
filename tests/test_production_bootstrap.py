from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
import sys

import pytest


def _load_bootstrap():
    path = Path("tools/production_bootstrap.py")
    spec = importlib.util.spec_from_file_location("production_bootstrap_tool", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bootstrap = _load_bootstrap()
TARGET = "a" * 40
PREVIOUS = "b" * 40


class FakeRunner:
    def __init__(self, source_root: Path, app_root: Path):
        self.source_root = source_root
        self.app_root = app_root
        self.deployed = False
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, argv):
        args = tuple(str(item) for item in argv)
        self.commands.append(args)

        if args[:3] == ("git", "-C", str(self.source_root)):
            tail = args[3:]
            if tail == ("rev-parse", "HEAD"):
                return bootstrap.CommandResult(0, TARGET + "\n", "")
            if tail == ("status", "--porcelain"):
                return bootstrap.CommandResult(0, "", "")

        if args[:3] == ("git", "-C", str(self.app_root)):
            tail = args[3:]
            if tail == ("branch", "--show-current"):
                return bootstrap.CommandResult(0, "main\n", "")
            if tail == ("status", "--porcelain"):
                return bootstrap.CommandResult(0, "", "")
            if tail == ("rev-parse", "HEAD"):
                value = TARGET if self.deployed else PREVIOUS
                return bootstrap.CommandResult(0, value + "\n", "")
            if tail == ("reset", "--hard", TARGET):
                self.deployed = True
                return bootstrap.CommandResult(0, "", "")

        if args and args[0].endswith("/bin/pip"):
            return bootstrap.CommandResult(0, "", "")

        return bootstrap.CommandResult(0, "", "")


def _tree(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source = tmp_path / "source"
    app = tmp_path / "app"
    venv = tmp_path / "venv"
    launcher = tmp_path / "bin/obsidian-automation-update"

    (source / "tools").mkdir(parents=True)
    (source / "tools/production_bootstrap.py").write_text(
        "#!/usr/bin/env python3\nprint('target bootstrap')\n",
        encoding="utf-8",
    )
    app.mkdir()
    (venv / "bin").mkdir(parents=True)
    pip = venv / "bin/pip"
    pip.write_text("#!/bin/sh\n", encoding="utf-8")
    pip.chmod(0o755)
    return source, app, venv, launcher


def test_target_command_executes_target_owned_source(tmp_path: Path) -> None:
    source = tmp_path / "target"
    command = bootstrap._target_command(
        python_executable="/usr/bin/python3",
        source_root=source,
        target_sha=TARGET,
        profile="automation",
        repository_url="https://example.invalid/repo.git",
        app_root=Path("/opt/app"),
        venv_root=Path("/opt/venv"),
        launcher_path=Path("/usr/local/sbin/update"),
        receipt_dir=Path("/var/lib/receipts"),
    )

    assert command[0] == "/usr/bin/python3"
    assert command[1] == str(source / "tools/production_bootstrap.py")
    assert "--apply-from-target" in command
    assert command[command.index("--target-sha") + 1] == TARGET
    assert "origin/main" not in command


def test_invalid_or_floating_target_is_rejected() -> None:
    for target in ("main", "origin/main", "abc123", "A" * 40):
        with pytest.raises(bootstrap.BootstrapError):
            bootstrap._validate_target_sha(target)


def test_atomic_launcher_install_is_exact_and_executable(tmp_path: Path) -> None:
    target = tmp_path / "bin/update"
    data = b"#!/usr/bin/env python3\nprint('new')\n"

    bootstrap._atomic_install_bytes(data, target, mode=0o755)

    assert target.read_bytes() == data
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_apply_from_target_replaces_launcher_and_writes_secret_free_receipt(
    tmp_path: Path,
) -> None:
    source, app, venv, launcher = _tree(tmp_path)
    receipts = tmp_path / "receipts"
    runner = FakeRunner(source, app)

    receipt, path = bootstrap.apply_from_target(
        source_root=source,
        target_sha=TARGET,
        profile="automation",
        app_root=app,
        venv_root=venv,
        launcher_path=launcher,
        receipt_dir=receipts,
        runner=runner,
        python_executable="/usr/bin/python3",
        require_root=False,
    )

    assert receipt.result == "success"
    assert receipt.previous_sha == PREVIOUS
    assert receipt.target_sha == TARGET
    assert receipt.package_install == "passed"
    assert receipt.host_activation == "not_attempted"
    assert launcher.read_bytes() == (
        source / "tools/production_bootstrap.py"
    ).read_bytes()
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o755

    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["bootstrap_contract"] == 1
    assert value["target_sha"] == TARGET
    raw = path.read_text(encoding="utf-8")
    for forbidden in (
        "password",
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "webdav",
        "repository_url",
    ):
        assert forbidden not in raw

    assert any(
        command[0].endswith("/bin/pip") and "--force-reinstall" in command
        for command in runner.commands
    )


def test_apply_refuses_source_root_not_at_exact_target(tmp_path: Path) -> None:
    source, app, venv, launcher = _tree(tmp_path)
    receipts = tmp_path / "receipts"

    class WrongSource(FakeRunner):
        def __call__(self, argv):
            args = tuple(str(item) for item in argv)
            if (
                args[:3] == ("git", "-C", str(self.source_root))
                and args[3:] == ("rev-parse", "HEAD")
            ):
                return bootstrap.CommandResult(0, PREVIOUS + "\n", "")
            return super().__call__(argv)

    with pytest.raises(
        bootstrap.BootstrapError,
        match="source_root_not_exact_target",
    ):
        bootstrap.apply_from_target(
            source_root=source,
            target_sha=TARGET,
            profile="automation",
            app_root=app,
            venv_root=venv,
            launcher_path=launcher,
            receipt_dir=receipts,
            runner=WrongSource(source, app),
            require_root=False,
        )

    assert not launcher.exists()


def test_receipt_contract_has_no_freeform_command_output() -> None:
    receipt = bootstrap.BootstrapReceipt(
        previous_sha=PREVIOUS,
        target_sha=TARGET,
        profile="automation",
        launcher_sha256="c" * 64,
        package_install="passed",
        host_activation="not_attempted",
        result="success",
        failed_stage=None,
        completed_at="2026-09-19T00:00:00Z",
    )
    value = json.loads(receipt.to_json_bytes())
    assert set(value) == {
        "record_version",
        "bootstrap_contract",
        "stage",
        "previous_sha",
        "target_sha",
        "profile",
        "launcher_sha256",
        "package_install",
        "host_activation",
        "result",
        "failed_stage",
        "completed_at",
    }
