#!/usr/bin/env python3
"""Fresh Debian LXC installer for the Obsidian Snapshot application.

The default invocation only prints a generated plan.  Host changes require the
explicit ``--apply`` option and a root process.  The installer is intentionally
stdlib-only so the root-side bootstrap does not depend on the application being
installed first.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
from typing import Callable, Sequence


ACCOUNT = "obsidian-snapshot"
APP_ROOT = "/opt/obsidian-snapshot"
VENV_ROOT = f"{APP_ROOT}/venv"
STATE_ROOT = "/var/lib/obsidian-snapshot"
UNIT_NAME = "obsidian-snapshot.service"
UNIT_PATH = f"/etc/systemd/system/{UNIT_NAME}"
WHEELHOUSE = "/usr/share/python-wheels"
MINIMUM_PYTHON = (3, 11)


class InstallerError(RuntimeError):
    """Raised when a fresh install cannot proceed safely."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], CommandResult]


@dataclass(frozen=True)
class Step:
    kind: str
    label: str
    argv: tuple[str, ...] = ()
    path: str | None = None
    content: str | None = None
    mode: int | None = None

    def as_json(self) -> dict[str, object]:
        if self.kind == "command":
            return {"kind": self.kind, "label": self.label, "argv": list(self.argv)}
        return {
            "kind": self.kind,
            "label": self.label,
            "path": self.path,
            "mode": None if self.mode is None else f"{self.mode:04o}",
            "content": self.content,
        }


@dataclass(frozen=True)
class InstallPlan:
    source_root: Path
    target_root: Path
    steps: tuple[Step, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "installer": "obsidian_snapshot_fresh_lxc",
                "mode": "plan_only",
                "source_root": str(self.source_root),
                "target_root": str(self.target_root),
                "account": ACCOUNT,
                "application_root": APP_ROOT,
                "venv_root": VENV_ROOT,
                "state_root": STATE_ROOT,
                "service": UNIT_NAME,
                "preflight": {
                    "account_absence_check": ["getent", "passwd", ACCOUNT],
                    "destinations_must_not_exist": [APP_ROOT, STATE_ROOT, UNIT_PATH],
                },
                "steps": [step.as_json() for step in self.steps],
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"


def _service_unit() -> str:
    return """[Unit]
Description=Obsidian Snapshot application
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=obsidian-snapshot
Group=obsidian-snapshot
WorkingDirectory=/var/lib/obsidian-snapshot
Environment=HOME=/var/lib/obsidian-snapshot
ExecStart=/opt/obsidian-snapshot/venv/bin/obsidian-snapshot-app --state /var/lib/obsidian-snapshot
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
"""


def _validate_source_root(source_root: Path) -> Path:
    if source_root.is_symlink():
        raise InstallerError("source_root_must_not_be_symlink")
    try:
        resolved = source_root.resolve(strict=True)
    except OSError as exc:
        raise InstallerError("source_root_missing") from exc
    if not resolved.is_dir():
        raise InstallerError("source_root_must_be_directory")
    pyproject = resolved / "pyproject.toml"
    if pyproject.is_symlink() or not pyproject.is_file():
        raise InstallerError("source_pyproject_missing_or_unsafe")
    return resolved


def _validate_target_root(target_root: Path) -> Path:
    if target_root.is_symlink():
        raise InstallerError("target_root_must_not_be_symlink")
    try:
        resolved = target_root.resolve(strict=True)
    except OSError as exc:
        raise InstallerError("target_root_missing") from exc
    if not resolved.is_dir():
        raise InstallerError("target_root_must_be_directory")
    return resolved


def build_plan(
    source_root: Path,
    *,
    target_root: Path = Path("/"),
) -> InstallPlan:
    """Build the install actions without invoking a command or writing files.

    ``target_root`` is an API seam for disposable-root tests.  The command line
    intentionally does not expose it: ``--apply`` always targets the real LXC.
    """
    source = _validate_source_root(Path(source_root))
    target = _validate_target_root(Path(target_root))
    pip = f"{VENV_ROOT}/bin/pip"
    steps = (
        Step("command", "refresh Debian package metadata", ("apt-get", "update")),
        Step(
            "command",
            "install Python runtime and offline build wheels",
            (
                "apt-get", "install", "--yes", "python3", "python3-venv",
                "python3-setuptools-whl", "python3-wheel-whl",
                "git", "rclone", "ca-certificates",
            ),
        ),
        Step(
            "command",
            "require Python 3.11 or newer",
            (
                "python3", "-c",
                "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 2)",
            ),
        ),
        Step(
            "command",
            "create the dedicated non-login service account",
            (
                "useradd", "--system", "--user-group", "--home-dir", STATE_ROOT,
                "--no-create-home", "--shell", "/usr/sbin/nologin", ACCOUNT,
            ),
        ),
        Step(
            "command", "create root-owned application directory",
            ("install", "-d", "-o", "root", "-g", "root", "-m", "0755", APP_ROOT),
        ),
        Step(
            "command", "create service-owned persistent state directory",
            (
                "install", "-d", "-o", ACCOUNT, "-g", ACCOUNT,
                "-m", "0700", STATE_ROOT,
            ),
        ),
        Step("command", "create the application virtual environment", (
            "python3", "-m", "venv", VENV_ROOT,
        )),
        Step(
            "command",
            "install build backend from Debian's local wheelhouse",
            (
                pip, "install", "--no-index", "--find-links", WHEELHOUSE,
                "setuptools>=75", "wheel",
            ),
        ),
        Step(
            "command", "install the reviewed local application source",
            (
                pip, "install", "--no-index", "--no-build-isolation",
                "--no-deps", str(source),
            ),
        ),
        Step(
            "write_file", "install the single Snapshot systemd service",
            path=UNIT_PATH, content=_service_unit(), mode=0o644,
        ),
        Step(
            "command", "reload systemd unit inventory",
            ("systemctl", "daemon-reload"),
        ),
        Step(
            "command", "enable and start the Snapshot service",
            ("systemctl", "enable", "--now", UNIT_NAME),
        ),
    )
    return InstallPlan(source_root=source, target_root=target, steps=steps)


def _target_path(root: Path, absolute_path: str) -> Path:
    logical = PurePosixPath(absolute_path)
    if not logical.is_absolute() or ".." in logical.parts:
        raise InstallerError("unsafe_install_destination")
    result = root
    for part in logical.parts[1:]:
        result = result / part
        if result.is_symlink():
            raise InstallerError("install_destination_contains_symlink")
    return result


def _check_fresh_destinations(plan: InstallPlan) -> None:
    for logical in (APP_ROOT, STATE_ROOT, UNIT_PATH):
        destination = _target_path(plan.target_root, logical)
        if os.path.lexists(destination):
            raise InstallerError("fresh_install_destination_already_exists")


def _write_file_atomically(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or os.path.lexists(path):
        raise InstallerError("refusing_to_replace_existing_install_file")
    temporary = path.with_name(f".{path.name}.install-tmp")
    if os.path.lexists(temporary):
        raise InstallerError("temporary_install_file_already_exists")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _subprocess_runner(argv: Sequence[str]) -> CommandResult:
    completed = subprocess.run(
        [str(part) for part in argv],
        check=False,
        capture_output=True,
        text=True,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def execute_plan(
    plan: InstallPlan,
    runner: Runner,
    *,
    require_root: bool = True,
) -> None:
    """Execute an explicitly supplied plan through the supplied command runner.

    Tests pass a fake runner and a disposable target root.  There is deliberately
    no default runner here, so importing this module or building a plan cannot
    accidentally mutate the host.
    """
    if plan.target_root != Path("/") and require_root:
        raise InstallerError("non_host_target_requires_test_mode")
    if require_root and os.geteuid() != 0:
        raise InstallerError("installer_requires_root")
    _check_fresh_destinations(plan)

    account_result = runner(("getent", "passwd", ACCOUNT))
    if account_result.returncode == 0:
        raise InstallerError("service_account_already_exists")
    if account_result.returncode != 2:
        raise InstallerError("cannot_safely_check_service_account")

    for step in plan.steps:
        if step.kind == "command":
            result = runner(step.argv)
            if result.returncode != 0:
                raise InstallerError(f"{step.label.replace(' ', '_')}_failed")
            continue
        if step.kind != "write_file" or step.path is None or step.content is None or step.mode is None:
            raise InstallerError("invalid_install_plan_step")
        destination = _target_path(plan.target_root, step.path)
        _write_file_atomically(destination, step.content, step.mode)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="install_snapshot_lxc",
        description="Plan or explicitly apply a fresh Debian Snapshot LXC install.",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="reviewed source checkout to install (default: this repository)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the host mutations described by the generated plan",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        plan = build_plan(args.source_root)
        if not args.apply:
            sys.stdout.write(plan.to_json())
            return 0
        execute_plan(plan, _subprocess_runner)
        print(json.dumps({"installer": "obsidian_snapshot_fresh_lxc", "status": "completed"}))
        return 0
    except InstallerError as exc:
        print(
            json.dumps({"installer": "obsidian_snapshot_fresh_lxc", "status": "failed", "error": str(exc)}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
