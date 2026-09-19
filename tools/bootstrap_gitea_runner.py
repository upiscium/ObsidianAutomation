#!/usr/bin/env python3
"""Stage a pinned host-mode Gitea runner on a separate, non-serving LXC.

No network downloads, registration, token handling, job execution, or activation.
Private YAML is operator-supplied and never printed. Existing .runner is retained.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Sequence

USER = "gitea-runner"
HOME = Path("/var/lib/gitea-runner")
CONFIG = Path("/etc/gitea-runner/config.yaml")
BINARY = Path("/usr/local/bin/runner")
UNIT = "gitea-runner.service"
UNIT_PATH = Path("/etc/systemd/system") / UNIT
EXCLUDED_ROOTS = (
    "/etc/obsidian-ai", "/etc/obsidian-core-promotion",
    "/etc/obsidian-github-sync", "/etc/obsidian-github-writer",
    "/etc/obsidian-github-mirror", "/etc/obsidian-snapshot",
    "/opt/obsidian-automation/app",
)
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class RunnerBootstrapError(RuntimeError):
    """Only fixed diagnostic codes are emitted by the CLI."""


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def _checked(runner: Runner, argv: Sequence[str], code: str) -> str:
    result = runner(tuple(str(arg) for arg in argv))
    if result.returncode:
        raise RunnerBootstrapError(code)
    return result.stdout.strip()


def _safe_chain(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise RunnerBootstrapError("unsafe_path")
    for parent in reversed((path, *path.parents)):
        if os.path.lexists(parent) and parent.is_symlink():
            raise RunnerBootstrapError("symlink_path")


def _read_regular(path: Path, limit: int) -> bytes:
    _safe_chain(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise RunnerBootstrapError("unsafe_or_oversized_source")
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read(limit + 1)
        after = os.fstat(fd)
        if len(data) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise RunnerBootstrapError("source_changed")
        return data
    finally:
        os.close(fd)


def _atomic(path: Path, data: bytes, mode: int, owner: tuple[int, int] | None) -> None:
    _safe_chain(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not path.is_file():
        raise RunnerBootstrapError("unsafe_destination")
    fd, name = tempfile.mkstemp(prefix=".runner-stage-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            if owner is not None:
                os.fchown(handle.fileno(), *owner)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _inert(runner: Runner) -> None:
    result = runner((
        "systemctl", "show", UNIT, "--property=LoadState", "--property=ActiveState",
        "--property=UnitFileState", "--property=MainPID", "--property=ControlPID", "--property=Job",
    ))
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode and not (result.returncode == 1 and state.get("LoadState") == "not-found"
                                 and state.get("ActiveState") == "inactive"):
        raise RunnerBootstrapError("cannot_inspect_runner")
    if state.get("LoadState") not in {"loaded", "not-found"}:
        raise RunnerBootstrapError("unknown_runner_load_state")
    if state.get("ActiveState") not in {"inactive", "failed"}:
        raise RunnerBootstrapError("runner_must_be_inactive")
    if state.get("UnitFileState", "") not in {"", "disabled"}:
        raise RunnerBootstrapError("runner_must_be_disabled")
    if any(state.get(key, "0") not in {"0", ""} for key in ("MainPID", "ControlPID", "Job")):
        raise RunnerBootstrapError("runner_has_pending_work")


def stage(
    *, source_root: Path, target_sha: str, binary_source: Path,
    binary_sha256: str, config_source: Path, root: Path = Path("/"),
    runner: Runner = _run, require_root: bool = True,
) -> dict[str, object]:
    if require_root and os.geteuid() != 0:
        raise RunnerBootstrapError("root_required")
    if re.fullmatch(r"[0-9a-f]{40,64}", target_sha) is None:
        raise RunnerBootstrapError("full_target_sha_required")
    if re.fullmatch(r"[0-9a-f]{64}", binary_sha256) is None:
        raise RunnerBootstrapError("binary_sha256_required")
    _safe_chain(source_root)
    if _checked(runner, ("git", "-C", str(source_root), "rev-parse", "HEAD"), "cannot_read_source") != target_sha:
        raise RunnerBootstrapError("source_not_exact_target")
    if _checked(runner, ("git", "-C", str(source_root), "status", "--porcelain"), "cannot_read_source"):
        raise RunnerBootstrapError("source_not_clean")
    _safe_chain(root)
    for value in EXCLUDED_ROOTS:
        if os.path.lexists(root / value.lstrip("/")):
            raise RunnerBootstrapError("runner_requires_separate_trust_domain")
    _inert(runner)
    binary = _read_regular(binary_source, 128 * 1024 * 1024)
    if hashlib.sha256(binary).hexdigest() != binary_sha256:
        raise RunnerBootstrapError("runner_binary_digest_mismatch")
    config = _read_regular(config_source, 1024 * 1024)
    if not config.strip():
        raise RunnerBootstrapError("empty_runner_config")
    unit = _read_regular(source_root / "examples/gitea-runner/gitea-runner.service", 16384)
    if b"User=gitea-runner\n" not in unit or b"/usr/local/bin/runner daemon" not in unit:
        raise RunnerBootstrapError("invalid_runner_unit")
    registration = root / HOME.relative_to("/") / ".runner"
    _safe_chain(registration)
    if registration.exists() and not registration.is_file():
        raise RunnerBootstrapError("unsafe_registration_state")
    for value in (BINARY, CONFIG, HOME, UNIT_PATH):
        _safe_chain(root / value.relative_to("/"))

    owner: tuple[int, int] | None = None
    root_owner: tuple[int, int] | None = None
    config_owner: tuple[int, int] | None = None
    if require_root:
        if runner(("getent", "group", USER)).returncode:
            _checked(runner, ("groupadd", "--system", USER), "cannot_create_group")
        if runner(("id", "-u", USER)).returncode:
            _checked(runner, (
                "useradd", "--system", "--gid", USER, "--home-dir", str(HOME),
                "--no-create-home", "--shell", "/usr/sbin/nologin", USER,
            ), "cannot_create_user")
        account = pwd.getpwnam(USER)
        if account.pw_dir != str(HOME) or account.pw_shell != "/usr/sbin/nologin":
            raise RunnerBootstrapError("runner_account_contract_mismatch")
        groups = set(_checked(runner, ("id", "-nG", USER), "cannot_inspect_groups").split())
        if groups != {USER}:
            raise RunnerBootstrapError("runner_has_unexpected_groups")
        owner = account.pw_uid, account.pw_gid
        root_owner = 0, 0
        config_owner = 0, account.pw_gid
    for value, mode, identity in ((HOME, 0o750, owner), (CONFIG.parent, 0o750, config_owner)):
        directory = root / value.relative_to("/")
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, mode)
        if identity is not None:
            os.chown(directory, *identity)
    _atomic(root / BINARY.relative_to("/"), binary, 0o755, root_owner)
    _atomic(root / CONFIG.relative_to("/"), config, 0o640, config_owner)
    _atomic(root / UNIT_PATH.relative_to("/"), unit, 0o644, root_owner)
    if registration.exists():
        os.chmod(registration, 0o600)
        if owner is not None:
            os.chown(registration, *owner)
    _checked(runner, ("systemctl", "daemon-reload"), "daemon_reload_failed")
    _checked(runner, ("systemctl", "disable", UNIT), "cannot_disable_runner")
    _inert(runner)
    return {
        "record_version": 1, "profile": "runner-execution-only",
        "target_sha": target_sha, "runner_binary_sha256": binary_sha256,
        "registration_preserved": registration.exists(), "registration_performed": False,
        "service_activated": False, "values_printed": False, "result": "staged",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--runner-binary", type=Path, required=True)
    parser.add_argument("--runner-sha256", required=True)
    parser.add_argument("--config-source", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = stage(source_root=args.source_root, target_sha=args.target_sha,
                       binary_source=args.runner_binary, binary_sha256=args.runner_sha256,
                       config_source=args.config_source)
    except (RunnerBootstrapError, OSError, KeyError):
        print(json.dumps({"event": "runner-bootstrap", "status": "failed", "values_printed": False}), file=sys.stderr)
        return 1
    print(json.dumps({"event": "runner-bootstrap", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
