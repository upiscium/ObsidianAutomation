#!/usr/bin/env python3
"""Source-side bootstrap/update launcher for ObsidianAutomation production.

This file is intentionally standalone and stdlib-only. The installed launcher
never needs to understand the target package version: it fetches and verifies an
explicit target commit, creates a detached worktree at that target, then hands
control to the target commit's copy of this file.

The target-owned apply stage updates the production checkout/package and replaces
this launcher atomically. Service/profile provisioning is layered on top in later
host-lifecycle stages; this foundation does not enable recurring automation.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Iterable, Sequence


BOOTSTRAP_CONTRACT = 2
DEFAULT_REPOSITORY_URL = "https://github.com/upiscium/ObsidianAutomation.git"
DEFAULT_APP_ROOT = Path("/opt/obsidian-automation/app")
DEFAULT_VENV_ROOT = Path("/opt/obsidian-automation/venv")
DEFAULT_LAUNCHER_PATH = Path("/usr/local/sbin/obsidian-automation-update")
DEFAULT_RECEIPT_DIR = Path("/var/lib/obsidian-automation/deployments")
DEFAULT_WHEELHOUSE = Path("/usr/share/python-wheels")
SUPPORTED_PROFILES = ("automation",)
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


class BootstrapError(RuntimeError):
    """Raised when a source-side bootstrap stage fails closed."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], CommandResult]


@dataclass(frozen=True)
class BootstrapReceipt:
    previous_sha: str | None
    target_sha: str
    profile: str
    launcher_sha256: str
    package_install: str
    host_activation: str
    result: str
    failed_stage: str | None
    completed_at: str

    def to_json_bytes(self) -> bytes:
        return (
            json.dumps(
                {
                    "record_version": 1,
                    "bootstrap_contract": BOOTSTRAP_CONTRACT,
                    "stage": "obsidian_automation_source_bootstrap",
                    "previous_sha": self.previous_sha,
                    "target_sha": self.target_sha,
                    "profile": self.profile,
                    "launcher_sha256": self.launcher_sha256,
                    "package_install": self.package_install,
                    "host_activation": self.host_activation,
                    "result": self.result,
                    "failed_stage": self.failed_stage,
                    "completed_at": self.completed_at,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_runner(argv: Sequence[str]) -> CommandResult:
    completed = subprocess.run(
        [str(item) for item in argv],
        check=False,
        capture_output=True,
        text=True,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _run(runner: Runner, argv: Sequence[str], *, label: str) -> CommandResult:
    result = runner(tuple(str(item) for item in argv))
    if result.returncode != 0:
        raise BootstrapError(f"{label} failed with exit status {result.returncode}")
    return result


def _git(
    runner: Runner,
    root: Path,
    *args: str,
    label: str,
) -> CommandResult:
    return _run(runner, ("git", "-C", str(root), *args), label=label)


def _git_output(
    runner: Runner,
    root: Path,
    *args: str,
    label: str,
) -> str:
    return _git(runner, root, *args, label=label).stdout.strip()


def _validate_target_sha(value: str) -> str:
    if _SHA_RE.fullmatch(value) is None:
        raise BootstrapError("target_sha_must_be_full_lowercase_git_digest")
    return value


def _require_profile(value: str) -> str:
    if value not in SUPPORTED_PROFILES:
        raise BootstrapError("unsupported_profile")
    return value


def _require_directory(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise BootstrapError(f"{label}_missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise BootstrapError(f"{label}_must_be_non_symlink_directory")


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _require_directory(path.parent, label="destination_parent")


def _atomic_install_bytes(data: bytes, destination: Path, *, mode: int) -> None:
    _ensure_parent(destination)
    if os.path.lexists(destination) and destination.is_symlink():
        raise BootstrapError("refusing_to_replace_symlink")

    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise BootstrapError("short_write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temporary_path, destination)
        dir_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _persist_receipt(
    receipt_dir: Path,
    receipt: BootstrapReceipt,
) -> Path:
    receipt_dir.mkdir(parents=True, exist_ok=True)
    _require_directory(receipt_dir, label="receipt_dir")
    stamp = (
        receipt.completed_at.replace("-", "")
        .replace(":", "")
        .replace(".", "")
        .replace("+", "")
    )
    target = receipt_dir / f"{stamp}-{receipt.target_sha[:12]}.bootstrap.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(target, flags, 0o640)
    try:
        data = receipt.to_json_bytes()
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise BootstrapError("short_receipt_write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    dir_fd = os.open(receipt_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return target


def _ensure_production_checkout(
    *,
    app_root: Path,
    repository_url: str,
    runner: Runner,
) -> bool:
    if app_root.exists():
        _require_directory(app_root, label="app_root")
        branch = _git_output(
            runner,
            app_root,
            "branch",
            "--show-current",
            label="read production branch",
        )
        if branch != "main":
            raise BootstrapError("production_checkout_must_be_on_main")
        dirty = _git_output(
            runner,
            app_root,
            "status",
            "--porcelain",
            label="read production working tree status",
        )
        if dirty:
            raise BootstrapError("production_checkout_must_be_clean")
        return False

    _ensure_parent(app_root)
    _run(
        runner,
        (
            "git",
            "clone",
            "--branch",
            "main",
            "--single-branch",
            repository_url,
            str(app_root),
        ),
        label="fresh production clone",
    )
    _require_directory(app_root, label="app_root")
    return True


def _verify_fetched_target(
    *,
    app_root: Path,
    target_sha: str,
    runner: Runner,
) -> None:
    _validate_target_sha(target_sha)
    _git(runner, app_root, "fetch", "origin", "main", label="git fetch")
    verified = runner(
        (
            "git",
            "-C",
            str(app_root),
            "rev-parse",
            "--verify",
            f"{target_sha}^{{commit}}",
        )
    )
    if verified.returncode != 0 or verified.stdout.strip() != target_sha:
        raise BootstrapError("target_not_exact_fetched_commit")

    ancestor = runner(
        (
            "git",
            "-C",
            str(app_root),
            "merge-base",
            "--is-ancestor",
            target_sha,
            "origin/main",
        )
    )
    if ancestor.returncode == 1:
        raise BootstrapError("target_not_reachable_from_origin_main")
    if ancestor.returncode != 0:
        raise BootstrapError("target_reachability_check_failed")


def _target_command(
    *,
    python_executable: str,
    source_root: Path,
    target_sha: str,
    profile: str,
    repository_url: str,
    app_root: Path,
    venv_root: Path,
    launcher_path: Path,
    receipt_dir: Path,
    wheelhouse: Path,
) -> tuple[str, ...]:
    return (
        python_executable,
        str(source_root / "tools" / "production_bootstrap.py"),
        "--apply-from-target",
        "--source-root",
        str(source_root),
        "--target-sha",
        target_sha,
        "--profile",
        profile,
        "--repository-url",
        repository_url,
        "--app-root",
        str(app_root),
        "--venv-root",
        str(venv_root),
        "--launcher-path",
        str(launcher_path),
        "--receipt-dir",
        str(receipt_dir),
        "--wheelhouse",
        str(wheelhouse),
    )


def _validated_child_failure(stderr: str) -> str | None:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        value = json.loads(lines[-1])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("event") != "obsidian-automation-source-bootstrap":
        return None
    if value.get("status") != "failed":
        return None
    message = value.get("message")
    if not isinstance(message, str) or len(message) > 512:
        return None
    if re.fullmatch(r"[A-Za-z0-9_ .:/=-]+", message) is None:
        return None
    return message


def _run_target_apply(
    runner: Runner,
    argv: Sequence[str],
) -> CommandResult:
    result = runner(tuple(str(item) for item in argv))
    if result.returncode != 0:
        child = _validated_child_failure(result.stderr)
        if child is not None:
            raise BootstrapError(f"target_owned:{child}")
        raise BootstrapError(
            f"target-owned bootstrap apply failed with exit status {result.returncode}"
        )
    return result


def _prepare_offline_build_backend(
    *,
    pip: Path,
    wheelhouse: Path,
    runner: Runner,
) -> None:
    _require_directory(wheelhouse, label="build_wheelhouse")
    _run(
        runner,
        (
            str(pip),
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "setuptools>=75",
            "wheel",
        ),
        label="install offline build backend",
    )


def handoff_to_target(
    *,
    target_sha: str,
    profile: str,
    repository_url: str = DEFAULT_REPOSITORY_URL,
    app_root: Path = DEFAULT_APP_ROOT,
    venv_root: Path = DEFAULT_VENV_ROOT,
    launcher_path: Path = DEFAULT_LAUNCHER_PATH,
    receipt_dir: Path = DEFAULT_RECEIPT_DIR,
    wheelhouse: Path = DEFAULT_WHEELHOUSE,
    runner: Runner = _default_runner,
    python_executable: str = sys.executable,
    require_root: bool = True,
) -> None:
    if require_root and os.geteuid() != 0:
        raise BootstrapError("bootstrap_requires_root")
    target_sha = _validate_target_sha(target_sha)
    profile = _require_profile(profile)
    _ensure_production_checkout(
        app_root=app_root,
        repository_url=repository_url,
        runner=runner,
    )
    _verify_fetched_target(
        app_root=app_root,
        target_sha=target_sha,
        runner=runner,
    )

    temporary = Path(
        tempfile.mkdtemp(
            prefix="obsidian-automation-bootstrap.",
            dir="/var/tmp" if Path("/var/tmp").is_dir() else None,
        )
    )
    try:
        temporary.rmdir()
        _git(
            runner,
            app_root,
            "worktree",
            "add",
            "--detach",
            str(temporary),
            target_sha,
            label="create target bootstrap worktree",
        )
        source_script = temporary / "tools" / "production_bootstrap.py"
        if not source_script.is_file() or source_script.is_symlink():
            raise BootstrapError("target_bootstrap_script_missing_or_unsafe")
        _run_target_apply(
            runner,
            _target_command(
                python_executable=python_executable,
                source_root=temporary,
                target_sha=target_sha,
                profile=profile,
                repository_url=repository_url,
                app_root=app_root,
                venv_root=venv_root,
                launcher_path=launcher_path,
                receipt_dir=receipt_dir,
                wheelhouse=wheelhouse,
            ),
        )
    finally:
        if temporary.exists():
            runner(
                (
                    "git",
                    "-C",
                    str(app_root),
                    "worktree",
                    "remove",
                    "--force",
                    str(temporary),
                )
            )
            shutil.rmtree(temporary, ignore_errors=True)


def apply_from_target(
    *,
    source_root: Path,
    target_sha: str,
    profile: str,
    app_root: Path = DEFAULT_APP_ROOT,
    venv_root: Path = DEFAULT_VENV_ROOT,
    launcher_path: Path = DEFAULT_LAUNCHER_PATH,
    receipt_dir: Path = DEFAULT_RECEIPT_DIR,
    wheelhouse: Path = DEFAULT_WHEELHOUSE,
    runner: Runner = _default_runner,
    python_executable: str = sys.executable,
    require_root: bool = True,
) -> tuple[BootstrapReceipt, Path]:
    stage = "preflight"
    previous_sha: str | None = None
    package_install = "not_run"
    host_activation = "not_attempted"
    launcher_sha = "not_installed"

    try:
        if require_root and os.geteuid() != 0:
            raise BootstrapError("bootstrap_requires_root")
        target_sha = _validate_target_sha(target_sha)
        profile = _require_profile(profile)
        _require_directory(source_root, label="source_root")
        _require_directory(app_root, label="app_root")

        source_head = _git_output(
            runner,
            source_root,
            "rev-parse",
            "HEAD",
            label="read target source HEAD",
        )
        if source_head != target_sha:
            raise BootstrapError("source_root_not_exact_target")

        source_dirty = _git_output(
            runner,
            source_root,
            "status",
            "--porcelain",
            label="read target source status",
        )
        if source_dirty:
            raise BootstrapError("source_root_not_clean")

        branch = _git_output(
            runner,
            app_root,
            "branch",
            "--show-current",
            label="read production branch",
        )
        if branch != "main":
            raise BootstrapError("production_checkout_must_be_on_main")
        dirty = _git_output(
            runner,
            app_root,
            "status",
            "--porcelain",
            label="read production working tree status",
        )
        if dirty:
            raise BootstrapError("production_checkout_must_be_clean")

        previous_sha = _git_output(
            runner,
            app_root,
            "rev-parse",
            "HEAD",
            label="read previous production SHA",
        )

        stage = "checkout_target"
        _git(
            runner,
            app_root,
            "reset",
            "--hard",
            target_sha,
            label="reset production checkout to target",
        )
        deployed = _git_output(
            runner,
            app_root,
            "rev-parse",
            "HEAD",
            label="verify production target",
        )
        if deployed != target_sha:
            raise BootstrapError("production_checkout_target_mismatch")

        stage = "prepare_venv"
        if not venv_root.exists():
            _ensure_parent(venv_root)
            _run(
                runner,
                (python_executable, "-m", "venv", str(venv_root)),
                label="create production venv",
            )
        _require_directory(venv_root, label="venv_root")
        pip = venv_root / "bin" / "pip"
        if not pip.is_file() or pip.is_symlink():
            raise BootstrapError("production_pip_missing_or_unsafe")

        stage = "prepare_build_backend"
        _prepare_offline_build_backend(
            pip=pip,
            wheelhouse=wheelhouse,
            runner=runner,
        )

        stage = "install_package"
        _run(
            runner,
            (
                str(pip),
                "install",
                "--no-index",
                "--no-build-isolation",
                "--no-deps",
                "--force-reinstall",
                str(app_root),
            ),
            label="install production package",
        )
        package_install = "passed"

        stage = "install_launcher"
        source_launcher = source_root / "tools" / "production_bootstrap.py"
        if not source_launcher.is_file() or source_launcher.is_symlink():
            raise BootstrapError("target_launcher_source_missing_or_unsafe")
        launcher_bytes = source_launcher.read_bytes()
        launcher_sha = hashlib.sha256(launcher_bytes).hexdigest()
        _atomic_install_bytes(
            launcher_bytes,
            launcher_path,
            mode=0o755,
        )

        stage = "persist_receipt"
        completed_at = _utc_now()
        receipt = BootstrapReceipt(
            previous_sha=previous_sha,
            target_sha=target_sha,
            profile=profile,
            launcher_sha256=launcher_sha,
            package_install=package_install,
            host_activation=host_activation,
            result="success",
            failed_stage=None,
            completed_at=completed_at,
        )
        path = _persist_receipt(receipt_dir, receipt)
        return receipt, path

    except Exception as exc:
        completed_at = _utc_now()
        receipt = BootstrapReceipt(
            previous_sha=previous_sha,
            target_sha=target_sha,
            profile=profile,
            launcher_sha256=launcher_sha,
            package_install=package_install,
            host_activation=host_activation,
            result="failed",
            failed_stage=stage,
            completed_at=completed_at,
        )
        try:
            _persist_receipt(receipt_dir, receipt)
        except Exception:
            pass
        if isinstance(exc, BootstrapError):
            raise BootstrapError(f"{stage}:{exc}") from exc
        raise BootstrapError(f"{stage}:unexpected_bootstrap_failure") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-automation-update",
        description=(
            "Fetch one explicit reviewed target and hand control to that target's "
            "stdlib-only production bootstrap implementation."
        ),
    )
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--profile", choices=SUPPORTED_PROFILES, default="automation")
    parser.add_argument("--repository-url", default=DEFAULT_REPOSITORY_URL)
    parser.add_argument("--app-root", type=Path, default=DEFAULT_APP_ROOT)
    parser.add_argument("--venv-root", type=Path, default=DEFAULT_VENV_ROOT)
    parser.add_argument("--launcher-path", type=Path, default=DEFAULT_LAUNCHER_PATH)
    parser.add_argument("--receipt-dir", type=Path, default=DEFAULT_RECEIPT_DIR)
    parser.add_argument("--wheelhouse", type=Path, default=DEFAULT_WHEELHOUSE)
    parser.add_argument("--apply-from-target", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source-root", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.apply_from_target:
            if args.source_root is None:
                raise BootstrapError("source_root_required")
            receipt, path = apply_from_target(
                source_root=args.source_root,
                target_sha=args.target_sha,
                profile=args.profile,
                app_root=args.app_root,
                venv_root=args.venv_root,
                launcher_path=args.launcher_path,
                receipt_dir=args.receipt_dir,
                wheelhouse=args.wheelhouse,
            )
            print(
                json.dumps(
                    {
                        "event": "obsidian-automation-source-bootstrap",
                        "status": "completed",
                        "target_sha": receipt.target_sha,
                        "profile": receipt.profile,
                        "host_activation": receipt.host_activation,
                        "receipt": str(path),
                    },
                    sort_keys=True,
                )
            )
            return 0

        handoff_to_target(
            target_sha=args.target_sha,
            profile=args.profile,
            repository_url=args.repository_url,
            app_root=args.app_root,
            venv_root=args.venv_root,
            launcher_path=args.launcher_path,
            receipt_dir=args.receipt_dir,
            wheelhouse=args.wheelhouse,
        )
        return 0
    except BootstrapError as exc:
        print(
            json.dumps(
                {
                    "event": "obsidian-automation-source-bootstrap",
                    "status": "failed",
                    "message": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
