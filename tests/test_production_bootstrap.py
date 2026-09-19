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


def _tree(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    source = tmp_path / "source"
    app = tmp_path / "app"
    venv = tmp_path / "venv"
    launcher = tmp_path / "bin/obsidian-automation-update"
    wheelhouse = tmp_path / "wheelhouse"

    (source / "tools").mkdir(parents=True)
    (source / "tools/production_bootstrap.py").write_text(
        "#!/usr/bin/env python3\nprint('target bootstrap')\n",
        encoding="utf-8",
    )
    (source / "tools/provision_automation_authority.py").write_text(
        "#!/usr/bin/env python3\nprint('authority provisioned')\n",
        encoding="utf-8",
    )
    app.mkdir()
    (venv / "bin").mkdir(parents=True)
    wheelhouse.mkdir()
    pip = venv / "bin/pip"
    pip.write_text("#!/bin/sh\n", encoding="utf-8")
    pip.chmod(0o755)
    return source, app, venv, launcher, wheelhouse


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
        wheelhouse=Path("/usr/share/python-wheels"),
    )

    assert command[0] == "/usr/bin/python3"
    assert command[1] == str(source / "tools/production_bootstrap.py")
    assert "--apply-from-target" in command
    assert command[command.index("--target-sha") + 1] == TARGET
    assert command[command.index("--wheelhouse") + 1] == "/usr/share/python-wheels"
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
    source, app, venv, launcher, wheelhouse = _tree(tmp_path)
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
        wheelhouse=wheelhouse,
        runner=runner,
        python_executable="/usr/bin/python3",
        require_root=False,
    )

    assert receipt.result == "success"
    assert receipt.previous_sha == PREVIOUS
    assert receipt.target_sha == TARGET
    assert receipt.package_install == "passed"
    assert receipt.authority_provisioning == "passed"
    assert receipt.host_activation == "not_attempted"
    assert launcher.read_bytes() == (
        source / "tools/production_bootstrap.py"
    ).read_bytes()
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o755

    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["bootstrap_contract"] == 3
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

    pip_commands = [
        command
        for command in runner.commands
        if command and command[0].endswith("/bin/pip")
    ]
    assert len(pip_commands) == 2

    build_backend, project_install = pip_commands
    assert "--no-index" in build_backend
    assert "--find-links" in build_backend
    assert str(wheelhouse) in build_backend
    assert "setuptools>=75" in build_backend
    assert "wheel" in build_backend

    assert "--no-index" in project_install
    assert "--no-build-isolation" in project_install
    assert "--no-deps" in project_install
    assert "--force-reinstall" in project_install

    assert any(
        command[:2] == (
            "/usr/bin/python3",
            str(source / "tools/provision_automation_authority.py"),
        )
        for command in runner.commands
    )


def test_apply_refuses_source_root_not_at_exact_target(tmp_path: Path) -> None:
    source, app, venv, launcher, wheelhouse = _tree(tmp_path)
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
            wheelhouse=wheelhouse,
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
        authority_provisioning="passed",
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
        "authority_provisioning",
        "host_activation",
        "result",
        "failed_stage",
        "completed_at",
    }


def test_validated_child_failure_propagates_only_fixed_json() -> None:
    message = "install_package:install production package failed with exit status 1"
    stderr = json.dumps(
        {
            "event": "obsidian-automation-source-bootstrap",
            "status": "failed",
            "message": message,
        }
    )

    assert bootstrap._validated_child_failure(stderr) == message
    assert bootstrap._validated_child_failure("secret=abc") is None


def test_target_apply_exposes_validated_child_stage() -> None:
    message = "prepare_build_backend:install offline build backend failed with exit status 1"

    def runner(_argv):
        return bootstrap.CommandResult(
            1,
            "",
            json.dumps(
                {
                    "event": "obsidian-automation-source-bootstrap",
                    "status": "failed",
                    "message": message,
                }
            ),
        )

    with pytest.raises(bootstrap.BootstrapError, match="target_owned:"):
        bootstrap._run_target_apply(runner, ("python3", "target-bootstrap.py"))


def test_offline_build_backend_never_uses_index(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(argv):
        command = tuple(str(item) for item in argv)
        commands.append(command)
        return bootstrap.CommandResult(0, "", "")

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()

    bootstrap._prepare_offline_build_backend(
        pip=Path("/venv/bin/pip"),
        wheelhouse=wheelhouse,
        runner=runner,
    )

    assert len(commands) == 1
    command = commands[0]
    assert "--no-index" in command
    assert "--find-links" in command
    assert str(wheelhouse) in command
    assert "setuptools>=75" in command
    assert "wheel" in command
