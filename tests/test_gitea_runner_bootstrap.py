from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

spec = importlib.util.spec_from_file_location("runner_bootstrap_tool", "tools/bootstrap_gitea_runner.py")
assert spec and spec.loader
tool = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = tool
spec.loader.exec_module(tool)
TARGET = "a" * 40


class Commands:
    def __init__(self):
        self.calls = []
        self.active = "inactive"
        self.enabled = "disabled"
        self.job = ""

    def __call__(self, args):
        args = tuple(args)
        self.calls.append(args)
        stdout = ""
        if args[:1] == ("git",):
            stdout = TARGET if args[-2:] == ("rev-parse", "HEAD") else ""
        if args[:2] == ("systemctl", "show"):
            stdout = (f"LoadState=loaded\nActiveState={self.active}\n"
                      f"UnitFileState={self.enabled}\nMainPID=0\nControlPID=0\nJob={self.job}\n")
        return subprocess.CompletedProcess(args, 0, stdout, "")


@pytest.fixture
def fixture(tmp_path):
    source = tmp_path / "source"
    (source / "examples/gitea-runner").mkdir(parents=True)
    (source / "examples/gitea-runner/gitea-runner.service").write_bytes(
        Path("examples/gitea-runner/gitea-runner.service").read_bytes())
    binary = tmp_path / "runner"
    binary.write_bytes(b"test-reviewed-binary")
    config = tmp_path / "config.yaml"
    config.write_bytes(b"runner:\n  envs:\n    CANARY: private-config-value\n")
    root = tmp_path / "root"
    root.mkdir()
    commands = Commands()
    args = dict(source_root=source, target_sha=TARGET, binary_source=binary,
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                config_source=config, root=root, runner=commands, require_root=False)
    return args, commands


def test_staging_is_inert_replayable_and_private(fixture):
    args, commands = fixture
    first = tool.stage(**args)
    root = args["root"]
    registration = root / "var/lib/gitea-runner/.runner"
    assert not registration.exists()
    registration.write_bytes(b"PRIVATE-REGISTRATION-CANARY")
    registration.chmod(0o600)
    second = tool.stage(**args)
    assert registration.read_bytes() == b"PRIVATE-REGISTRATION-CANARY"
    assert second["registration_preserved"] is True
    assert first["service_activated"] is False
    assert first["registration_performed"] is False
    assert "private-config-value" not in json.dumps(first)
    assert (root / "etc/gitea-runner/config.yaml").read_bytes() == args["config_source"].read_bytes()
    assert (root / "etc/gitea-runner/config.yaml").stat().st_mode & 0o777 == 0o640
    assert (root / "usr/local/bin/runner").stat().st_mode & 0o777 == 0o755
    assert not any("start" in call or "enable" in call for call in commands.calls)


@pytest.mark.parametrize("entry", tool.EXCLUDED_ROOTS)
def test_writer_or_snapshot_state_refuses_colocation(fixture, entry):
    args, commands = fixture
    (args["root"] / entry.lstrip("/")).mkdir(parents=True)
    with pytest.raises(tool.RunnerBootstrapError, match="separate_trust_domain"):
        tool.stage(**args)
    assert not any(call[:2] == ("systemctl", "daemon-reload") for call in commands.calls)


@pytest.mark.parametrize("state", ["active", "activating", "deactivating", "unknown"])
def test_running_or_unknown_runner_is_not_mutated(fixture, state):
    args, commands = fixture
    commands.active = state
    with pytest.raises(tool.RunnerBootstrapError):
        tool.stage(**args)
    assert not (args["root"] / "usr/local/bin/runner").exists()


def test_pending_job_is_not_interrupted(fixture):
    args, commands = fixture
    commands.job = "123"
    with pytest.raises(tool.RunnerBootstrapError, match="pending_work"):
        tool.stage(**args)


def test_bad_digest_fails_before_host_changes(fixture):
    args, _ = fixture
    args["binary_sha256"] = "b" * 64
    with pytest.raises(tool.RunnerBootstrapError, match="digest_mismatch"):
        tool.stage(**args)
    assert not (args["root"] / "var/lib/gitea-runner").exists()


def test_source_and_destination_symlinks_are_rejected(fixture, tmp_path):
    args, _ = fixture
    actual = args["binary_source"]
    symlink = tmp_path / "binary-link"
    symlink.symlink_to(actual)
    args["binary_source"] = symlink
    with pytest.raises(tool.RunnerBootstrapError, match="symlink"):
        tool.stage(**args)
    args["binary_source"] = actual
    directory = args["root"] / "usr/local"
    directory.mkdir(parents=True)
    (directory / "bin").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(tool.RunnerBootstrapError, match="symlink"):
        tool.stage(**args)


def test_source_unit_contains_no_automation_or_snapshot_authority():
    unit = Path("examples/gitea-runner/gitea-runner.service").read_text()
    assert "User=gitea-runner\n" in unit
    assert "UMask=0077\n" in unit
    assert "ProtectSystem=strict\n" in unit
    assert "ReadWritePaths=/var/lib/gitea-runner\n" in unit
    assert "obsidian-ai" not in unit
    assert "obsidian-core-promotion" not in unit
    assert "obsidian-snapshot" not in unit
