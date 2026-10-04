from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence


DEFAULT_APP_ROOT = Path("/opt/obsidian-ai/ObsidianAutomation")
DEFAULT_VENV_ROOT = Path("/opt/obsidian-ai/venv")
DEFAULT_SYSTEMD_DIR = Path("/etc/systemd/system")
DEFAULT_RECEIPT_DIR = Path("/var/lib/obsidian-ai/deployments")
DEFAULT_REVISION_ENV = Path("/etc/obsidian-ai/pre-review-revision.env")
DEFAULT_REFRESH_INHIBIT = Path(
    "/run/obsidian-automation/semantic-refresh-inhibited.json"
)
TIMER_UNIT = "obsidian-pre-review.timer"
MIRROR_TIMER_UNIT = "obsidian-ai-vault-pull.timer"
MIRROR_SERVICE_UNIT = "obsidian-ai-vault-pull.service"
SEMANTIC_REFRESH_SERVICES = (
    "obsidian-semantic-index-refresh-prepare.service",
    "obsidian-semantic-index-refresh-embed.service",
    "obsidian-semantic-index-refresh-finalize.service",
)
PRE_REVIEW_SERVICES = (
    *SEMANTIC_REFRESH_SERVICES,
    "obsidian-pre-review-status.service",
    "obsidian-ai-input-planner.service",
    "obsidian-ai-human-projection-sync.service",
    "obsidian-ai-human-projection-cleanup-sync.service",
    "obsidian-ai-review-intake.service",
    "obsidian-ai-post-review-executor-prepare.service",
    "obsidian-ai-post-review-transport.service",
    "obsidian-ai-post-review-executor-finalize.service",
    "obsidian-ai-post-review-projection-sync.service",
    "obsidian-ai-post-review-reconcile.service",
    "obsidian-pre-review-evaluator.service",
    "obsidian-pre-review-reader.service",
    "obsidian-pre-review-validator.service",
    "obsidian-pre-review-generator.service",
)
OBSOLETE_UNITS = ("obsidian-pre-review-evaluator.timer",)
REQUIRED_UNITS = frozenset(
    {
        MIRROR_SERVICE_UNIT,
        *SEMANTIC_REFRESH_SERVICES,
        "obsidian-ai-input-planner.service",
        "obsidian-ai-human-projection-sync.service",
        "obsidian-ai-human-projection-cleanup-sync.service",
        "obsidian-ai-review-intake.service",
        "obsidian-ai-post-review-executor-prepare.service",
        "obsidian-ai-post-review-transport.service",
        "obsidian-ai-post-review-executor-finalize.service",
        "obsidian-ai-post-review-projection-sync.service",
        "obsidian-ai-post-review-reconcile.service",
        "obsidian-pre-review-generator.service",
        "obsidian-pre-review-validator.service",
        "obsidian-pre-review-reader.service",
        "obsidian-pre-review-evaluator.service",
        "obsidian-pre-review-status.service",
        "obsidian-pre-review.timer",
    }
)
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


class PreReviewProductionUpdateError(RuntimeError):
    """Raised when exact-revision pre-review deployment cannot complete safely."""


class RefreshUpdateInhibit:
    """Prevent queued success handlers from starting during either updater.

    PID1 checks the marker before activating each refresh stage. A separate,
    stable inode serializes both updater paths; an interrupted owner must retry
    its exact target before another updater may remove the marker.
    """

    def __init__(self, path: Path, target_sha: str, owner: str, *, require_root: bool = True):
        self.path = path.absolute()
        self.require_root = require_root
        self.value = {"record_version": 1, "target_sha": target_sha, "owner": owner}
        self.lock_fd: int | None = None
        if _SHA_RE.fullmatch(target_sha) is None or owner not in {"host-runtime", "pre-review"}:
            raise PreReviewProductionUpdateError("invalid semantic refresh update owner")

    def _safe_path(self, path: Path) -> None:
        if ".." in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
            raise PreReviewProductionUpdateError("unsafe semantic refresh inhibit path")

    def _private_regular(self, path: Path) -> None:
        self._safe_path(path)
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
            or info.st_uid != (0 if self.require_root else os.geteuid())):
            raise PreReviewProductionUpdateError("unsafe semantic refresh inhibit file")

    def _read(self) -> dict:
        self._private_regular(self.path)
        if self.path.stat().st_size > 4096:
            raise PreReviewProductionUpdateError("invalid semantic refresh update intent")
        try:
            value = json.loads(self.path.read_bytes())
        except (ValueError, UnicodeError) as exc:
            raise PreReviewProductionUpdateError("invalid semantic refresh update intent") from exc
        if value != self.value:
            raise PreReviewProductionUpdateError("semantic refresh update intent belongs to another target or updater")
        return value

    def acquire(self, *, publish: bool = True) -> None:
        self._safe_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        self._safe_path(self.path.parent)
        info = self.path.parent.stat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022
            or info.st_uid != (0 if self.require_root else os.geteuid())):
            raise PreReviewProductionUpdateError("unsafe semantic refresh inhibit directory")
        lock_path = self.path.with_suffix(".lock")
        self._safe_path(lock_path)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            self._private_regular(lock_path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PreReviewProductionUpdateError("semantic refresh update already in progress") from exc
            if os.path.lexists(self.path):
                self._read()
            self.lock_fd = fd
            if publish:
                self.publish()
        except BaseException:
            self.lock_fd = None
            os.close(fd)
            raise

    def publish(self) -> None:
        if self.lock_fd is None:
            raise PreReviewProductionUpdateError("semantic refresh inhibit is not owned")
        if os.path.lexists(self.path):
            self._read()
        else:
            _atomic_install_bytes(
                (json.dumps(self.value, sort_keys=True) + "\n").encode(),
                self.path,
                mode=0o600,
            )

    def release(self) -> None:
        if self.lock_fd is None:
            raise PreReviewProductionUpdateError("semantic refresh inhibit is not owned")
        self._read()
        self.path.unlink()
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def close(self) -> None:
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None


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
    timer_existed: bool | None
    timer_was_enabled: bool | None
    timer_was_active: bool | None
    mirror_timer_was_enabled: bool | None
    mirror_timer_was_active: bool | None
    bootstrap_mirror_pre_disabled: bool
    first_install_left_disabled: bool
    safe_smoke: str
    disposable_canary: str
    result: str
    failed_stage: str | None
    completed_at: str

    def to_json_bytes(self) -> bytes:
        return (
            json.dumps(
                {
                    "record_version": 1,
                    "stage": "obsidian_pre_review_production_update",
                    "previous_sha": self.previous_sha,
                    "target_sha": self.target_sha,
                    "timer_existed": self.timer_existed,
                    "timer_was_enabled": self.timer_was_enabled,
                    "timer_was_active": self.timer_was_active,
                    "mirror_timer_was_enabled": self.mirror_timer_was_enabled,
                    "mirror_timer_was_active": self.mirror_timer_was_active,
                    "bootstrap_mirror_pre_disabled": self.bootstrap_mirror_pre_disabled,
                    "first_install_left_disabled": self.first_install_left_disabled,
                    "safe_smoke": self.safe_smoke,
                    "disposable_canary": self.disposable_canary,
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
        raise PreReviewProductionUpdateError(f"{label} does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PreReviewProductionUpdateError(
            f"{label} must be a non-symlink directory"
        )


def _run(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    label: str,
) -> CommandResult:
    result = runner(tuple(str(item) for item in argv))
    if result.returncode != 0:
        raise PreReviewProductionUpdateError(
            f"{label} failed with exit status {result.returncode}"
        )
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


def _validate_target(
    target_sha: str,
    *,
    app_root: Path,
    runner: CommandRunner,
) -> str:
    if _SHA_RE.fullmatch(target_sha) is None:
        raise PreReviewProductionUpdateError(
            "target SHA must be a lowercase full Git digest"
        )

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
        raise PreReviewProductionUpdateError(
            "target SHA is not an exact fetched commit"
        )

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
        raise PreReviewProductionUpdateError(
            "target SHA is not reachable from origin/main"
        )
    if ancestor.returncode != 0:
        raise PreReviewProductionUpdateError(
            "cannot validate target SHA against origin/main"
        )
    return target_sha


def _starting_revision(runner: CommandRunner, app_root: Path) -> str:
    branch = _git_output(runner, app_root, "branch", "--show-current", label="read current branch")
    if branch != "main":
        raise PreReviewProductionUpdateError("production checkout must be on main")
    if _git_output(runner, app_root, "status", "--porcelain", label="read working tree status"):
        raise PreReviewProductionUpdateError("production checkout is not clean")
    previous = _git_output(runner, app_root, "rev-parse", "HEAD", label="read current production SHA")
    if _SHA_RE.fullmatch(previous) is None:
        raise PreReviewProductionUpdateError("current production HEAD is not a supported Git digest")
    return previous


def _timer_state(
    runner: CommandRunner,
    unit: str,
    *,
    allow_missing: bool,
) -> tuple[bool, bool, bool]:
    enabled = runner(("systemctl", "is-enabled", unit))
    enabled_state = enabled.stdout.strip()
    if enabled_state == "enabled":
        exists = True
        was_enabled = True
    elif enabled_state == "disabled":
        exists = True
        was_enabled = False
    elif allow_missing and enabled_state in {"not-found", ""} and enabled.returncode != 0:
        return False, False, False
    else:
        raise PreReviewProductionUpdateError(
            f"{unit} enablement state is not enabled/disabled"
        )

    active = runner(("systemctl", "is-active", unit))
    active_state = active.stdout.strip()
    if active_state == "active":
        was_active = True
    elif active_state == "inactive":
        was_active = False
    else:
        raise PreReviewProductionUpdateError(
            f"{unit} activity state is not active/inactive"
        )
    return exists, was_enabled, was_active


def _update_timer_snapshot(
    runner: CommandRunner, *, bootstrap_mirror_pre_disabled: bool,
) -> tuple[bool, bool, bool, bool, bool]:
    timer_existed, enabled, active = _timer_state(runner, TIMER_UNIT, allow_missing=True)
    mirror_exists, mirror_enabled, mirror_active = _timer_state(
        runner, MIRROR_TIMER_UNIT, allow_missing=False,
    )
    if not mirror_exists:
        raise PreReviewProductionUpdateError("mirror timer must already exist")
    if bootstrap_mirror_pre_disabled:
        if mirror_enabled or mirror_active:
            raise PreReviewProductionUpdateError(
                "bootstrap mirror pre-disabled mode requires disabled/inactive mirror timer"
            )
        mirror_enabled, mirror_active = True, True
    return timer_existed, enabled, active, mirror_enabled, mirror_active


def _managed_unit_sources(app_root: Path) -> tuple[Path, ...]:
    source_dir = app_root / "examples" / "ai"
    _require_directory(source_dir, label="AI unit source directory")
    units = tuple(source_dir / name for name in sorted(REQUIRED_UNITS))
    missing = [source.name for source in units if not source.exists()]
    if missing:
        raise PreReviewProductionUpdateError(
            f"managed pre-review systemd units are incomplete: {missing}"
        )
    for source in units:
        info = source.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise PreReviewProductionUpdateError(
                "managed pre-review unit source must be a regular file"
            )
    return units


def _atomic_install_bytes(
    data: bytes,
    destination: Path,
    *,
    mode: int,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _require_directory(destination.parent, label="install destination directory")
    if os.path.lexists(destination) and destination.is_symlink():
        raise PreReviewProductionUpdateError(
            "refusing to replace symlinked deployment file"
        )

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
                raise PreReviewProductionUpdateError(
                    "short write while installing deployment file"
                )
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


def _install_managed_units(
    app_root: Path,
    systemd_dir: Path,
) -> tuple[str, ...]:
    _require_directory(systemd_dir, label="systemd unit directory")
    installed: list[str] = []
    for source in _managed_unit_sources(app_root):
        destination = systemd_dir / source.name
        _atomic_install_bytes(source.read_bytes(), destination, mode=0o644)
        installed.append(source.name)
    for name in OBSOLETE_UNITS:
        path = systemd_dir / name
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise PreReviewProductionUpdateError(
                    "obsolete pre-review unit path is unsafe"
                )
            path.unlink()
    return tuple(installed)


def _stop_refresh_services(runner: CommandRunner) -> None:
    """Stop existing stages, accepting absent units on the first upgrade."""
    loaded: list[str] = []
    for unit in SEMANTIC_REFRESH_SERVICES:
        result = runner((
            "systemctl", "show", unit, "--property=LoadState", "--value",
        ))
        state = result.stdout.strip()
        if state == "not-found" and result.returncode in {0, 1}:
            continue
        if result.returncode != 0 or state != "loaded":
            raise PreReviewProductionUpdateError("cannot determine semantic refresh service state")
        loaded.append(unit)
    # One transaction prevents a stopped predecessor from replacing another
    # stage's pending stop; the inhibit marker also blocks late success jobs.
    if loaded:
        _run(runner, ("systemctl", "stop", *loaded), label="stop semantic refresh services")
    for unit in loaded:
        result = _run(runner, (
            "systemctl", "show", unit, "--property=ActiveState",
            "--property=MainPID", "--property=ControlPID", "--property=Job",
        ), label="verify semantic refresh stopped")
        state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if (state.get("ActiveState") not in {"inactive", "failed"}
            or any(state.get(key) not in {"0", ""} for key in ("MainPID", "ControlPID", "Job"))):
            raise PreReviewProductionUpdateError("semantic refresh service did not become inert")


def _write_revision_env(path: Path, target_sha: str) -> None:
    if _SHA_RE.fullmatch(target_sha) is None:
        raise PreReviewProductionUpdateError("cannot write invalid revision env")
    _atomic_install_bytes(
        f"OBSIDIAN_AUTOMATION_REVISION={target_sha}\n".encode("utf-8"),
        path,
        mode=0o644,
    )


def _restore_timer(
    runner: CommandRunner,
    unit: str,
    *,
    was_enabled: bool,
    was_active: bool,
) -> None:
    if was_enabled and was_active:
        _run(
            runner,
            ("systemctl", "enable", "--now", unit),
            label=f"{unit} restore",
        )
        return
    if was_enabled:
        _run(
            runner,
            ("systemctl", "enable", unit),
            label=f"{unit} enabled-state restore",
        )
    else:
        _run(
            runner,
            ("systemctl", "disable", unit),
            label=f"{unit} disabled-state restore",
        )
    if was_active:
        _run(
            runner,
            ("systemctl", "start", unit),
            label=f"{unit} active-state restore",
        )
    else:
        _run(
            runner,
            ("systemctl", "stop", unit),
            label=f"{unit} inactive-state restore",
        )


def _receipt_path(
    receipt_dir: Path,
    target_sha: str,
    *,
    completed_at: str,
) -> Path:
    stamp = (
        completed_at.replace("-", "")
        .replace(":", "")
        .replace(".", "")
        .replace("+", "")
    )
    label = (
        target_sha[:12]
        if _SHA_RE.fullmatch(target_sha)
        else hashlib.sha256(target_sha.encode("utf-8")).hexdigest()[:12]
    )
    return receipt_dir / f"{stamp}-{label}.pre-review.json"


def _persist_receipt(
    receipt_dir: Path,
    receipt: DeploymentReceipt,
) -> Path:
    receipt_dir.mkdir(parents=True, exist_ok=True)
    _require_directory(receipt_dir, label="deployment receipt directory")
    target = _receipt_path(
        receipt_dir,
        receipt.target_sha,
        completed_at=receipt.completed_at,
    )
    data = receipt.to_json_bytes()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(target, flags, 0o640)
    except FileExistsError as exc:
        raise PreReviewProductionUpdateError(
            "deployment receipt path already exists"
        ) from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise PreReviewProductionUpdateError(
                    "short write while persisting deployment receipt"
                )
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
    revision_env: Path = DEFAULT_REVISION_ENV,
    runner: CommandRunner = _default_runner,
    require_root: bool = True,
    bootstrap_mirror_pre_disabled: bool = False,
    refresh_inhibit_path: Path = DEFAULT_REFRESH_INHIBIT,
) -> tuple[DeploymentReceipt, Path]:
    stage = "preflight"
    previous_sha: str | None = None
    timer_existed: bool | None = None
    timer_was_enabled: bool | None = None
    timer_was_active: bool | None = None
    mirror_timer_was_enabled: bool | None = None
    mirror_timer_was_active: bool | None = None
    safe_smoke = "not_run"
    timer_controlled = False
    mirror_timer_controlled = False
    units_installed = False
    refresh_inhibit: RefreshUpdateInhibit | None = None

    try:
        if require_root and os.geteuid() != 0:
            raise PreReviewProductionUpdateError(
                "pre-review production update must run as root"
            )
        _require_directory(app_root, label="production app root")
        _require_directory(venv_root, label="production venv root")
        receipt_dir.mkdir(parents=True, exist_ok=True)
        _require_directory(receipt_dir, label="deployment receipt directory")
        revision_env.parent.mkdir(parents=True, exist_ok=True)
        _require_directory(
            revision_env.parent,
            label="pre-review configuration directory",
        )

        previous_sha = _starting_revision(runner, app_root)
        _validate_target(target_sha, app_root=app_root, runner=runner)
        (
            timer_existed, timer_was_enabled, timer_was_active,
            mirror_timer_was_enabled, mirror_timer_was_active,
        ) = _update_timer_snapshot(
            runner, bootstrap_mirror_pre_disabled=bootstrap_mirror_pre_disabled,
        )

        stage = "stop_recurring_services"
        refresh_inhibit = RefreshUpdateInhibit(
            refresh_inhibit_path, target_sha, "pre-review", require_root=require_root,
        )
        refresh_inhibit.acquire(publish=False)
        # Another updater may have restored timers or advanced HEAD after our
        # preflight. Capture restoration state and receipt provenance only once
        # both updater paths are excluded by the shared lock.
        previous_sha = _starting_revision(runner, app_root)
        (
            timer_existed, timer_was_enabled, timer_was_active,
            mirror_timer_was_enabled, mirror_timer_was_active,
        ) = _update_timer_snapshot(
            runner, bootstrap_mirror_pre_disabled=bootstrap_mirror_pre_disabled,
        )
        refresh_inhibit.publish()
        if timer_existed:
            _run(
                runner,
                ("systemctl", "disable", "--now", TIMER_UNIT),
                label="pre-review timer stop",
            )
            timer_controlled = True

        _run(
            runner,
            ("systemctl", "disable", "--now", MIRROR_TIMER_UNIT),
            label="mirror timer stop",
        )
        mirror_timer_controlled = True
        _run(
            runner,
            ("systemctl", "stop", MIRROR_SERVICE_UNIT),
            label="mirror service stop",
        )
        _stop_refresh_services(runner)
        if timer_existed:
            for unit in PRE_REVIEW_SERVICES:
                if unit not in SEMANTIC_REFRESH_SERVICES:
                    _run(runner, ("systemctl", "stop", unit), label=f"stop {unit}")

        stage = "checkout_target"
        _git(
            runner,
            app_root,
            "reset",
            "--hard",
            target_sha,
            label="reset production checkout",
        )
        deployed = _git_output(
            runner,
            app_root,
            "rev-parse",
            "HEAD",
            label="verify deployed SHA",
        )
        if deployed != target_sha:
            raise PreReviewProductionUpdateError(
                "production checkout did not reach exact target SHA"
            )

        stage = "install_package"
        pip = venv_root / "bin" / "pip"
        if not pip.is_file():
            raise PreReviewProductionUpdateError(
                "production pip entrypoint is missing"
            )
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

        stage = "install_revision"
        _write_revision_env(revision_env, target_sha)

        stage = "install_units"
        _install_managed_units(app_root, systemd_dir)
        units_installed = True
        _run(
            runner,
            ("systemctl", "daemon-reload"),
            label="systemd daemon-reload",
        )

        # First rollout is intentionally inert until the manual disposable
        # production canary passes. For later deployments this also creates a
        # known disabled baseline before safe smoke.
        stage = "disable_installed_timer"
        _run(
            runner,
            ("systemctl", "disable", "--now", TIMER_UNIT),
            label="disable installed pre-review timer",
        )
        timer_controlled = True

        # Existing installations need file-level handoff ACLs as well as the
        # newly shipped units before any refresh worker may restart.
        stage = "apply_authority"
        _run(
            runner,
            (str(venv_root / "bin/python"),
             str(app_root / "tools/provision_automation_authority.py"),
             "--semantic-refresh-only"),
            label="semantic refresh authority provisioning",
        )
        _run(
            runner,
            ("sh", str(app_root / "examples/ai/bootstrap-pre-review-authority.sh")),
            label="pre-review authority bootstrap",
        )

        smoke = venv_root / "bin" / "obsidian-pre-review-production-smoke"
        if not smoke.is_file():
            raise PreReviewProductionUpdateError(
                "pre-review production smoke entrypoint is missing after install"
            )

        stage = "safe_smoke"
        _run(
            runner,
            (
                str(smoke),
                "--profile",
                "safe",
                "--expected-revision",
                target_sha,
                "--revision-env",
                str(revision_env),
                "--systemd-dir",
                str(systemd_dir),
            ),
            label="safe pre-review production smoke",
        )
        safe_smoke = "passed"

        first_install = not bool(timer_existed)

        stage = "restore_mirror_timer"
        _restore_timer(
            runner,
            MIRROR_TIMER_UNIT,
            was_enabled=bool(mirror_timer_was_enabled),
            was_active=bool(mirror_timer_was_active),
        )

        if not first_install:
            stage = "restore_pre_review_timer"
            _restore_timer(
                runner,
                TIMER_UNIT,
                was_enabled=bool(timer_was_enabled),
                was_active=bool(timer_was_active),
            )

        stage = "persist_receipt"
        completed_at = _utc_now()
        receipt = DeploymentReceipt(
            previous_sha=previous_sha,
            target_sha=target_sha,
            timer_existed=timer_existed,
            timer_was_enabled=timer_was_enabled,
            timer_was_active=timer_was_active,
            mirror_timer_was_enabled=mirror_timer_was_enabled,
            mirror_timer_was_active=mirror_timer_was_active,
            bootstrap_mirror_pre_disabled=bootstrap_mirror_pre_disabled,
            first_install_left_disabled=first_install,
            safe_smoke=safe_smoke,
            disposable_canary="pending_manual_acceptance",
            result="success",
            failed_stage=None,
            completed_at=completed_at,
        )
        path = _persist_receipt(receipt_dir, receipt)
        refresh_inhibit.release()
        return receipt, path

    except BaseException as exc:
        if timer_controlled or units_installed:
            runner(("systemctl", "disable", "--now", TIMER_UNIT))
        if mirror_timer_controlled:
            runner(("systemctl", "disable", "--now", MIRROR_TIMER_UNIT))
        completed_at = _utc_now()
        receipt = DeploymentReceipt(
            previous_sha=previous_sha,
            target_sha=target_sha,
            timer_existed=timer_existed,
            timer_was_enabled=timer_was_enabled,
            timer_was_active=timer_was_active,
            mirror_timer_was_enabled=mirror_timer_was_enabled,
            mirror_timer_was_active=mirror_timer_was_active,
            bootstrap_mirror_pre_disabled=bootstrap_mirror_pre_disabled,
            first_install_left_disabled=not bool(timer_existed),
            safe_smoke=safe_smoke,
            disposable_canary="not_run",
            result="failed",
            failed_stage=stage,
            completed_at=completed_at,
        )
        try:
            _persist_receipt(receipt_dir, receipt)
        except Exception:
            pass
        if isinstance(exc, PreReviewProductionUpdateError):
            raise PreReviewProductionUpdateError(f"{stage}: {exc}") from exc
        raise PreReviewProductionUpdateError(
            f"{stage}: unexpected production update failure"
        ) from exc
    finally:
        if refresh_inhibit is not None:
            refresh_inhibit.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-pre-review-production-update",
        description=(
            "Deploy one explicit reviewed ObsidianAutomation commit to the AI "
            "Writer checkout without enabling first-rollout automation."
        ),
    )
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--app-root", type=Path, default=DEFAULT_APP_ROOT)
    parser.add_argument("--venv-root", type=Path, default=DEFAULT_VENV_ROOT)
    parser.add_argument("--systemd-dir", type=Path, default=DEFAULT_SYSTEMD_DIR)
    parser.add_argument("--receipt-dir", type=Path, default=DEFAULT_RECEIPT_DIR)
    parser.add_argument("--revision-env", type=Path, default=DEFAULT_REVISION_ENV)
    parser.add_argument(
        "--bootstrap-mirror-pre-disabled",
        action="store_true",
        help=(
            "First updater bootstrap only: the operator already disabled/stopped "
            "an originally enabled+active mirror timer before replacing the shared venv."
        ),
    )
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
            revision_env=args.revision_env,
            bootstrap_mirror_pre_disabled=args.bootstrap_mirror_pre_disabled,
        )
    except PreReviewProductionUpdateError as exc:
        print(
            json.dumps(
                {
                    "event": "pre-review-production-update",
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
                "event": "pre-review-production-update",
                "status": "completed",
                "target_sha": receipt.target_sha,
                "first_install_left_disabled": receipt.first_install_left_disabled,
                "receipt": str(path),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
