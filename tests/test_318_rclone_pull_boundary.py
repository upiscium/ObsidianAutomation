"""Issue #318: rclone may copy canonical remote bytes *toward local staging only*.

All invocations are intercepted. These tests never connect to Nextcloud or GitHub.
"""
from __future__ import annotations

import ast
from collections import Counter
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from obsidian_automation import github_daily_setup, snapshot_adapter, vault_format_source, vault_mirror


_SRC = Path(__file__).resolve().parents[1] / "src" / "obsidian_automation"
_TOOLS = Path(__file__).resolve().parents[1] / "tools"
_FORBIDDEN_RCLONE_ACTIONS = {"copyto", "moveto", "purge", "delete", "deletefile", "move", "rcat", "sync:"}


def _pull_only(command: list[str], *, remote: str, destination: Path | None) -> None:
    """Assert an actual argv's rclone direction, not a mocked success message."""
    assert command[0] == "rclone"
    assert not any(token in _FORBIDDEN_RCLONE_ACTIONS for token in command)
    assert "--header-upload" not in command
    assert not any(token.startswith("--header-upload=") for token in command)
    assert not any(token in {"If-Match", "If-None-Match"} for token in command)
    operations = [i for i, token in enumerate(command) if token in {"copy", "sync", "check", "lsjson"}]
    assert len(operations) == 1, command
    verb_at = operations[0]
    assert command[verb_at + 1] == remote
    if destination is not None:
        assert command[verb_at + 2] == str(destination)
        assert ":" not in command[verb_at + 2], "rclone destination cannot be remote"
    assert command[verb_at] in {"copy", "sync", "check", "lsjson"}


def test_canonical_mirror_sync_is_remote_to_local_only(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "24-Locks" / "read-view").mkdir(parents=True)
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    config = tmp_path / "rclone.conf"
    config.write_text("fake fixture only")
    filters = tmp_path / "allow.filters"
    filters.write_text("+ **\n")
    calls: list[list[str]] = []

    def fake_run(argv, *, check):
        assert check is False
        calls.append(list(argv))
        return SimpleNamespace(returncode=0)

    result = vault_mirror.refresh_pull_only_mirror(
        state, mirror, remote="nextcloud:Vault",
        rclone_config=config, filter_file=filters, runner=fake_run,
    )
    assert result.vault_root == mirror.absolute()
    assert len(calls) == 1
    _pull_only(calls[0], remote="nextcloud:Vault", destination=mirror.absolute())
    assert "--delete-after" in calls[0]  # local mirror deletion remains legitimate


def test_snapshot_adapter_three_rclone_operations_never_upload(tmp_path: Path, monkeypatch) -> None:
    policy = tmp_path / "policy.toml"
    policy.write_text("version = 1\nexclude = []\nrepository_owned = []\n")
    config = tmp_path / "rclone.conf"
    config.write_text("fake fixture only")
    connections = snapshot_adapter.Connections(
        state=tmp_path / "run-state", rclone_remote="nextcloud:Vault",
        git_remote="https://git.example.invalid/private.git",
        branch="main", policy=policy, rclone_config=config,
    )
    argv: list[list[str]] = []

    def fake_command(command, **_kwargs):
        if command[0] != "rclone":
            raise AssertionError("no Git command should run without a verified snapshot")
        argv.append(list(command))
        # No bytes are downloaded into mirror. The snapshot must refuse an
        # empty/misaddressed source before any Git operation or remote mutation.
        return True, ""

    monkeypatch.setattr(snapshot_adapter, "_command", fake_command)
    assert snapshot_adapter.SnapshotAdapter(connections)("a" * 32) == ("failed", "sync", None)
    assert len(argv) == 3
    mirror = connections.state / "runs" / ("a" * 32) / "mirror"
    for expected, actual in zip(["sync", "sync", "check"], argv):
        _pull_only(actual, remote="nextcloud:Vault", destination=mirror)
        assert expected in actual
    assert all("--delete-after" in cmd for cmd in argv[:2])
    assert "--delete-after" not in argv[2]


def test_github_daily_source_copy_stays_local(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "rclone.conf"
    config.write_text("fixture only")
    target = tmp_path / "project-notes"
    target.mkdir()
    connections = SimpleNamespace(
        validate=lambda: None, rclone_remote="nextcloud:Vault",
        rclone_config=config,
    )
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(github_daily_setup.subprocess, "run", fake_run)
    assert github_daily_setup.pull_project_notes(connections, target)
    assert len(calls) == 1
    _pull_only(calls[0], remote="nextcloud:Vault/10-Project", destination=target)


def test_vault_formatter_copy_and_check_are_remote_to_local(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "system-capture"
    target.mkdir()
    config = tmp_path / "rclone.conf"
    config.write_text("fixture only")
    source = SimpleNamespace(validate=lambda: None, rclone_remote="nextcloud:Vault",
                             rclone_config=config)
    inventory = json.dumps([{"Path": "note.md", "Size": 4, "IsDir": False}]).encode()
    observed: list[list[str]] = []
    downloaded: list[list[str]] = []

    def fake_listing(command, _cwd):
        observed.append(list(command))
        return inventory

    def fake_run(command, **_kwargs):
        downloaded.append(list(command))
        if "copy" in command:
            (target / "note.md").write_bytes(b"note")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(vault_format_source, "_bounded_listing", fake_listing)
    monkeypatch.setattr(vault_format_source.subprocess, "run", fake_run)
    assert vault_format_source.pull_system_markdown(source, target)
    assert len(observed) == 2
    assert len(downloaded) == 2
    for command in observed:
        _pull_only(command, remote="nextcloud:Vault/98-System", destination=None)
        assert "lsjson" in command
    for command in downloaded:
        _pull_only(command, remote="nextcloud:Vault/98-System", destination=target)
    assert [verb for command in downloaded for verb in ("copy", "check") if verb in command] == ["copy", "check"]


# Review one finite set of rclone command-construction sites. Every new
# Python command source (including new files) must be audited and explicitly
# admitted here before the default test can pass. Calls from these functions
# are also covered by argv-capturing behavior tests above. The installer
# build_plan merely includes rclone as a package; obscure_password is local.
_APPROVED_RCLONE_OWNER_CALLS = {
    ("src/obsidian_automation/vault_mirror.py", "refresh_pull_only_mirror"):
        Counter({"runner": 1}),
    ("src/obsidian_automation/snapshot_adapter.py", "__call__"):
        Counter({"_command": 10}),
    ("src/obsidian_automation/github_daily_setup.py", "pull_project_notes"):
        Counter({"subprocess.run": 1}),
    ("src/obsidian_automation/vault_format_source.py", "pull_system_markdown"):
        Counter({"_bounded_listing": 2, "subprocess.run": 1}),
    ("src/obsidian_automation/snapshot_setup.py", "obscure_password"):
        Counter({"subprocess.run": 1}),
    ("tools/install_snapshot_lxc.py", "build_plan"): Counter(),
}
_EXECUTION_BOUNDARIES = {
    "runner", "_command", "_bounded_listing", "subprocess.run",
    "subprocess.Popen", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "os.system",
}
_BANNED_RCLONE_LITERALS = {
    "copyto", "moveto", "deletefile", "purge", "rcat", "--header-upload",
}
# Service/shell direct rclone commands are deliberately not yet an approved
# route. Existing services invoke typed Python entrypoints instead. New
# services must be reviewed, even if they appear to be pull-only.
_RCLONE_SERVICE_COMMAND = re.compile(
    r"(?<![A-Za-z0-9_-])['\"]?(?:[^\s\"']*/)?rclone['\"]?[ \t]+[^\s\\]+",
    re.IGNORECASE,
)
_RCLONE_SHELL_COMMAND = re.compile(
    r"(?<![A-Za-z0-9_-])['\"]?(?:[^\s\"']*/)?rclone['\"]?[ \t]+"
    r"(?:copy|sync|copyto|moveto|move|delete|deletefile|purge|rcat)\b",
    re.IGNORECASE,
)


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        if isinstance(call.func.value, ast.Name):
            return f"{call.func.value.id}.{call.func.attr}"
        return call.func.attr
    return None


def _rclone_executable_literal(node: ast.AST) -> bool:
    """Find direct rclone names, absolute paths and literal shell command lines."""
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return False
    value = node.value
    return (value.rsplit("/", 1)[-1] == "rclone"
            or bool(_RCLONE_SHELL_COMMAND.search(value)))


def _source_rclone_violations(root: Path) -> list[tuple[str, str]]:
    """Fail closed for new direct rclone command sources and script effects.

    Behavioral tests verify direction of approved current command sites.
    This static inventory catches added Python callsites and service scripts,
    not arbitrary runtime-generated strings or private deployed units.
    """
    violations: list[tuple[str, str]] = []
    for directory in ("src/obsidian_automation", "tools", "examples"):
        folder = root / directory
        if not folder.is_dir():
            continue
        for file in sorted(folder.rglob("*.py")):
            rel = file.relative_to(root).as_posix()
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=rel)
            seen_literals: set[int] = set()
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                literals = [n for n in ast.walk(function)
                            if _rclone_executable_literal(n)]
                if not literals:
                    continue
                seen_literals.update(id(n) for n in literals)
                key = (rel, function.name)
                expected = _APPROVED_RCLONE_OWNER_CALLS.get(key)
                if expected is None:
                    violations.append((rel, f"unreviewed rclone Python entrypoint: {function.name}"))
                    continue
                calls = Counter(
                    name for call in ast.walk(function)
                    if isinstance(call, ast.Call)
                    for name in [_call_name(call)]
                    if name in _EXECUTION_BOUNDARIES
                )
                if calls != expected:
                    violations.append((rel, f"rclone execution callsites changed: {function.name}"))
                strings = [n.value for n in ast.walk(function)
                           if isinstance(n, ast.Constant) and isinstance(n.value, str)]
                if any(s in _BANNED_RCLONE_LITERALS
                       or s.startswith("--header-upload=") for s in strings):
                    violations.append((rel, f"unsafe rclone command token: {function.name}"))
            # A module-level command bypasses the reviewed function boundary.
            for literal in ast.walk(tree):
                if (_rclone_executable_literal(literal)
                        and id(literal) not in seen_literals):
                    violations.append((rel, "unreviewed module-level rclone command"))
    # Include systemd services, drop-ins, scripts and CI entrypoints.
    # A direct service-level rclone command is not approved by our current
    # source contract, even if it appears read-only.
    for folder_name in ("examples", "configs", "tools", "src", ".github"):
        folder = root / folder_name
        if not folder.is_dir():
            continue
        for file in folder.rglob("*"):
            if not file.is_file():
                continue
            if file.suffix not in {
                ".service", ".timer", ".sh", ".bash", ".nix", ".conf",
                ".yml", ".yaml",
            } and not (file.suffix == "" and file.stat().st_mode & 0o111):
                continue
            rel = file.relative_to(root).as_posix()
            try:
                text = file.read_text(encoding="utf-8")
            except UnicodeError:
                violations.append((rel, "unreadable executable/service source"))
                continue
            active = "\n".join(
                line for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
            active = active.replace("\\\n", " ")
            if _RCLONE_SERVICE_COMMAND.search(active) or "--header-upload" in active:
                violations.append((rel, "unreviewed direct rclone/service upload command"))
    return violations


def test_rclone_source_and_service_invocation_inventory_is_allowlisted() -> None:
    root = _SRC.parents[1]
    assert _source_rclone_violations(root) == []


@pytest.mark.parametrize(("relative", "content"), [
    ("src/obsidian_automation/new_upload.py",
     'import subprocess\ndef upload(local, remote):\n'
     '    subprocess.run(["rclone", "copy", local, remote])\n'),
    ("tools/cas_upload.py",
     'from subprocess import run\ndef put(local, remote):\n'
     '    run(["rclone", "sync", local, remote])\n'),
    ("src/obsidian_automation/absolute_rclone_upload.py",
     'import subprocess\ndef upload(local, remote):\n'
     '    subprocess.run(["/usr/bin/rclone", "copy", local, remote])\n'),
    ("src/obsidian_automation/string_shell_upload.py",
     'import subprocess\ndef upload():\n'
     '    subprocess.run("rclone copy /tmp/loc nextcloud:Vault", shell=True)\n'),
    ("examples/ai/unsafe-upload.service",
     '[Service]\nExecStart=/usr/bin/rclone copy /tmp/loc nextcloud:Vault\n'),
    ("examples/ai/quoted-upload.service",
     '[Service]\nExecStart="/usr/bin/rclone" copy /tmp/loc nextcloud:Vault\n'),
    ("examples/ai/obsidian-pull.service.d/override.conf",
     '[Service]\nExecStart=/usr/bin/rclone copy /tmp/loc nextcloud:Vault\n'),
    ("tools/unsafe-upload.sh",
     '#!/bin/sh\nrclone sync /tmp/loc nextcloud:Vault\n'),
    ("tools/unsafe-upload",
     '#!/bin/sh\nrclone copy /tmp/loc nextcloud:Vault\n'),
    (".github/workflows/unsafe-upload.yml",
     'steps:\n  - run: rclone copy /tmp/loc nextcloud:Vault\n'),
], ids=[
    "new-python-copy", "new-python-sync", "absolute-python-copy",
    "shell-string-python", "new-systemd", "quoted-systemd", "systemd-override",
    "new-shell", "extensionless-shell", "github-workflow",
])
def test_rclone_inventory_detects_new_upload_entrypoints(
    tmp_path: Path, relative: str, content: str,
) -> None:
    file = tmp_path / relative
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")
    if file.suffix == "":
        file.chmod(0o755)  # executable entrypoint, without actually executing it
    assert any(found == relative for found, _ in _source_rclone_violations(tmp_path))


def test_rclone_inventory_detects_an_extra_upload_in_approved_python_function(
    tmp_path: Path,
) -> None:
    original = (_SRC / "github_daily_setup.py").read_text(encoding="utf-8")
    marker = '    argv = ["rclone", "--config",'
    assert original.count(marker) == 1
    modified = original.replace(
        marker,
        '    subprocess.run(["rclone", "copy", "/tmp/new", "nextcloud:Vault"])\n'
        + marker,
        1,
    )
    path = tmp_path / "src/obsidian_automation/github_daily_setup.py"
    path.parent.mkdir(parents=True)
    path.write_text(modified, encoding="utf-8")
    violations = _source_rclone_violations(tmp_path)
    assert any(found == path.relative_to(tmp_path).as_posix()
               and "callsites changed" in reason for found, reason in violations)
