from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


DEFAULT_APP_ROOT = Path("/opt/obsidian-github-sync/app")
DEFAULT_VENV_ROOT = Path("/opt/obsidian-github-sync/venv")
DEFAULT_SYSTEMD_DIR = Path("/etc/systemd/system")
DEFAULT_RECEIPT_DIR = Path("/var/lib/obsidian-github-sync/deployments")
TIMER_UNIT = "obsidian-github-sync.timer"
_REQUIRED_UNITS = frozenset(
    {
        "obsidian-github-sync-vault-pull.service",
        "obsidian-github-sync.service",
        "obsidian-github-writer.service",
        "obsidian-github-sync.timer",
    }
)
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


class ProductionUpdateError(RuntimeError):
    """Raised when a production update stage cannot complete safely."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str]], CommandResult]


@dataclass(frozen=True)
class DeploymentReceipt:
    previous_sha: str | None
    target_sha: str
    timer_was_enabled: bool | None
    timer_was_active: bool | None
    safe_smoke: str
    live_smoke: str
    result: str
    failed_stage: str | None
    completed_at: str

    def to_json_bytes(self) -> bytes:
        return (
            json.dumps(
                {
                    "record_version": 1,
                    "stage": "obsidian_github_production_update",
                    "previous_sha": self.previous_sha,
                    "target_sha": self.target_sha,
                    "timer_was_enabled": self.timer_was_enabled,
                    "timer_was_active": self.timer_was_active,
                    "safe_smoke": self.safe_smoke,
                    "live_smoke": self.live_smoke,
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
        list(argv),
        check=False,
        capture_output=True,
        text=True,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _require_directory(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ProductionUpdateError(f"{label} does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProductionUpdateError(f"{label} must be a non-symlink directory")


def _run(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    label: str,
) -> CommandResult:
    result = runner(tuple(str(item) for item in argv))
    if result.returncode != 0:
        raise ProductionUpdateError(f"{label} failed with exit status {result.returncode}")
    return result


def _git(
    runner: CommandRunner,
    app_root: Path,
    *args: str,
    label: str,
) -> CommandResult:
    return _run(
        runner,
        ("git", "-C", str(app_root), *args),
        label=label,
    )


def _git_output(
    runner: CommandRunner,
    app_root: Path,
    *args: str,
    label: str,
) -> str:
    return _git(runner, app_root, *args, label=label).stdout.strip()


def _timer_state(runner: CommandRunner) -> tuple[bool, bool]:
    enabled = runner(("systemctl", "is-enabled", TIMER_UNIT))
    enabled_state = enabled.stdout.strip()
    if enabled_state == "enabled":
        was_enabled = True
    elif enabled_state == "disabled":
        was_enabled = False
    else:
        raise ProductionUpdateError("timer enablement state is not enabled/disabled")

    active = runner(("systemctl", "is-active", TIMER_UNIT))
    active_state = active.stdout.strip()
    if active_state == "active":
        was_active = True
    elif active_state == "inactive":
        was_active = False
    else:
        raise ProductionUpdateError("timer activity state is not active/inactive")
    return was_enabled, was_active


def _validate_target(
    target_sha: str,
    *,
    app_root: Path,
    runner: CommandRunner,
) -> str:
    if _SHA_RE.fullmatch(target_sha) is None:
        raise ProductionUpdateError("target SHA must be a lowercase full Git digest")

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
        raise ProductionUpdateError("target SHA is not an exact fetched commit")

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
        raise ProductionUpdateError("target SHA is not reachable from origin/main")
    if ancestor.returncode != 0:
        raise ProductionUpdateError("cannot validate target SHA against origin/main")
    return target_sha


def _managed_unit_sources(app_root: Path) -> tuple[Path, ...]:
    source_dir = app_root / "examples" / "github-sync"
    _require_directory(source_dir, label="GitHub sync unit source directory")
    units = sorted(
        {
            *source_dir.glob("obsidian-github-*.service"),
            *source_dir.glob("obsidian-github-*.timer"),
        },
        key=lambda item: item.name,
    )
    names = {item.name for item in units}
    missing = sorted(_REQUIRED_UNITS - names)
    if missing:
        raise ProductionUpdateError(f"managed systemd units are incomplete: {missing}")
    for unit in units:
        info = unit.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ProductionUpdateError("managed systemd unit source must be a regular file")
    return tuple(units)


def _atomic_install_file(source: Path, destination: Path, *, mode: int = 0o644) -> None:
    data = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
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
                raise ProductionUpdateError("short write while installing systemd unit")
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


def _install_managed_units(app_root: Path, systemd_dir: Path) -> tuple[str, ...]:
    _require_directory(systemd_dir, label="systemd unit directory")
    installed: list[str] = []
    for source in _managed_unit_sources(app_root):
        destination = systemd_dir / source.name
        if destination.exists() and destination.is_symlink():
            raise ProductionUpdateError("refusing to replace symlinked systemd unit")
        _atomic_install_file(source, destination)
        installed.append(source.name)
    return tuple(installed)


def _restore_timer(
    runner: CommandRunner,
    *,
    was_enabled: bool,
    was_active: bool,
) -> None:
    if was_enabled and was_active:
        _run(
            runner,
            ("systemctl", "enable", "--now", TIMER_UNIT),
            label="timer restore",
        )
        return
    if was_enabled:
        _run(runner, ("systemctl", "enable", TIMER_UNIT), label="timer enable restore")
    if was_active:
        _run(runner, ("systemctl", "start", TIMER_UNIT), label="timer active restore")


def _receipt_path(receipt_dir: Path, target_sha: str, *, completed_at: str) -> Path:
    stamp = (
        completed_at.replace("-", "")
        .replace(":", "")
        .replace(".", "")
        .replace("+", "")
    )
    if _SHA_RE.fullmatch(target_sha) is not None:
        target_label = target_sha[:12]
    else:
        target_label = hashlib.sha256(target_sha.encode("utf-8")).hexdigest()[:12]
    return receipt_dir / f"{stamp}-{target_label}.json"


def _persist_receipt(receipt_dir: Path, receipt: DeploymentReceipt) -> Path:
    receipt_dir.mkdir(parents=True, exist_ok=True)
    info = receipt_dir.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProductionUpdateError("deployment receipt directory must be a non-symlink directory")

    target = _receipt_path(receipt_dir, receipt.target_sha, completed_at=receipt.completed_at)
    data = receipt.to_json_bytes()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(target, flags, 0o640)
    except FileExistsError as exc:
        raise ProductionUpdateError("deployment receipt path already exists") from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise ProductionUpdateError("short write while persisting deployment receipt")
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


def execute_update(
    *,
    target_sha: str,
    app_root: Path = DEFAULT_APP_ROOT,
    venv_root: Path = DEFAULT_VENV_ROOT,
    systemd_dir: Path = DEFAULT_SYSTEMD_DIR,
    receipt_dir: Path = DEFAULT_RECEIPT_DIR,
    runner: CommandRunner = _default_runner,
    require_root: bool = True,
) -> tuple[DeploymentReceipt, Path]:
    stage = "preflight"
    previous_sha: str | None = None
    timer_was_enabled: bool | None = None
    timer_was_active: bool | None = None
    safe_smoke = "not_run"
    live_smoke = "not_run"
    timer_stopped = False

    try:
        if require_root and os.geteuid() != 0:
            raise ProductionUpdateError("production update must run as root")
        _require_directory(app_root, label="production app root")
        _require_directory(venv_root, label="production venv root")
        receipt_dir.mkdir(parents=True, exist_ok=True)
        _require_directory(receipt_dir, label="deployment receipt directory")

        branch = _git_output(
            runner,
            app_root,
            "branch",
            "--show-current",
            label="read current branch",
        )
        if branch != "main":
            raise ProductionUpdateError("production checkout must be on main")

        dirty = _git_output(
            runner,
            app_root,
            "status",
            "--porcelain",
            label="read working tree status",
        )
        if dirty:
            raise ProductionUpdateError("production checkout is not clean")

        previous_sha = _git_output(
            runner,
            app_root,
            "rev-parse",
            "HEAD",
            label="read current production SHA",
        )
        if _SHA_RE.fullmatch(previous_sha) is None:
            raise ProductionUpdateError("current production HEAD is not a supported Git digest")

        _validate_target(target_sha, app_root=app_root, runner=runner)
        timer_was_enabled, timer_was_active = _timer_state(runner)

        stage = "stop_timer"
        _run(
            runner,
            ("systemctl", "disable", "--now", TIMER_UNIT),
            label="timer stop",
        )
        timer_stopped = True

        stage = "checkout_target"
        _git(
            runner,
            app_root,
            "reset",
            "--hard",
            target_sha,
            label="reset production checkout",
        )
        deployed_sha = _git_output(
            runner,
            app_root,
            "rev-parse",
            "HEAD",
            label="verify deployed SHA",
        )
        if deployed_sha != target_sha:
            raise ProductionUpdateError("production checkout did not reach exact target SHA")

        stage = "install_package"
        pip = venv_root / "bin" / "pip"
        if not pip.is_file():
            raise ProductionUpdateError("production pip entrypoint is missing")
        _run(
            runner,
            (
                str(pip),
                "install",
                "--no-deps",
                "--force-reinstall",
                str(app_root),
            ),
            label="production package install",
        )

        stage = "install_units"
        _install_managed_units(app_root, systemd_dir)
        _run(
            runner,
            ("systemctl", "daemon-reload"),
            label="systemd daemon-reload",
        )

        smoke = venv_root / "bin" / "obsidian-github-production-smoke"
        if not smoke.is_file():
            raise ProductionUpdateError("production smoke entrypoint is missing after install")

        stage = "safe_smoke"
        _run(
            runner,
            (str(smoke), "--profile", "safe"),
            label="safe production smoke",
        )
        safe_smoke = "passed"

        stage = "live_smoke"
        _run(
            runner,
            (str(smoke), "--profile", "live"),
            label="live production smoke",
        )
        live_smoke = "passed"

        stage = "restore_timer"
        _restore_timer(
            runner,
            was_enabled=bool(timer_was_enabled),
            was_active=bool(timer_was_active),
        )

        stage = "persist_receipt"
        completed_at = _utc_now()
        receipt = DeploymentReceipt(
            previous_sha=previous_sha,
            target_sha=target_sha,
            timer_was_enabled=timer_was_enabled,
            timer_was_active=timer_was_active,
            safe_smoke=safe_smoke,
            live_smoke=live_smoke,
            result="success",
            failed_stage=None,
            completed_at=completed_at,
        )
        path = _persist_receipt(receipt_dir, receipt)
        return receipt, path

    except Exception as exc:
        if timer_stopped:
            runner(("systemctl", "disable", "--now", TIMER_UNIT))
        completed_at = _utc_now()
        receipt = DeploymentReceipt(
            previous_sha=previous_sha,
            target_sha=target_sha,
            timer_was_enabled=timer_was_enabled,
            timer_was_active=timer_was_active,
            safe_smoke=safe_smoke,
            live_smoke=live_smoke,
            result="failed",
            failed_stage=stage,
            completed_at=completed_at,
        )
        try:
            path = _persist_receipt(receipt_dir, receipt)
        except Exception:
            path = receipt_dir / "unpersisted"
        if isinstance(exc, ProductionUpdateError):
            raise ProductionUpdateError(f"{stage}: {exc}") from exc
        raise ProductionUpdateError(f"{stage}: unexpected update failure") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-github-production-update",
        description=(
            "Deploy one explicit reviewed ObsidianAutomation commit to the GitHub "
            "integration production checkout and run registered smoke checks."
        ),
    )
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--app-root", type=Path, default=DEFAULT_APP_ROOT)
    parser.add_argument("--venv-root", type=Path, default=DEFAULT_VENV_ROOT)
    parser.add_argument("--systemd-dir", type=Path, default=DEFAULT_SYSTEMD_DIR)
    parser.add_argument("--receipt-dir", type=Path, default=DEFAULT_RECEIPT_DIR)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        receipt, path = execute_update(
            target_sha=args.target_sha,
            app_root=args.app_root,
            venv_root=args.venv_root,
            systemd_dir=args.systemd_dir,
            receipt_dir=args.receipt_dir,
        )
    except ProductionUpdateError as exc:
        print(
            json.dumps(
                {
                    "event": "obsidian-github-production-update",
                    "status": "failed",
                    "message": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "event": "obsidian-github-production-update",
                "status": "completed",
                "target_sha": receipt.target_sha,
                "receipt": str(path),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
