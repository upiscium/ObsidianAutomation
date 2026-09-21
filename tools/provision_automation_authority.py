#!/usr/bin/env python3
"""Provision the consolidated ObsidianAutomation Unix authority boundary.

This is a source-side, stdlib-only host provisioner. It creates only local
users/groups, empty directory roots and POSIX ACLs. It never installs credentials,
systemd units, timers, or application-specific state contents.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Callable, Iterable, Sequence


PROFILE = "automation-authority-v1"
NOLOGIN = "/usr/sbin/nologin"


class AuthorityProvisionError(RuntimeError):
    """Raised when the local authority boundary cannot be established safely."""


class CommandResult:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


Runner = Callable[[Sequence[str]], CommandResult]


def _default_runner(argv: Sequence[str]) -> CommandResult:
    completed = subprocess.run(
        [str(item) for item in argv],
        check=False,
        capture_output=True,
        text=True,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


PRIMARY_USERS: tuple[tuple[str, str, str], ...] = (
    ("gitea-runner", "gitea-runner", "/var/lib/gitea-runner"),
    ("obsidian-core-promoter", "obsidian-core-promoter", "/var/lib/obsidian-core-promotion"),
    ("obsidian-ai-sync", "obsidian-ai-sync", "/nonexistent"),
    ("obsidian-ai-reader", "obsidian-ai-reader", "/nonexistent"),
    ("obsidian-ai-generator", "obsidian-ai-generator", "/nonexistent"),
    ("obsidian-ai-validator", "obsidian-ai-validator", "/nonexistent"),
    ("obsidian-ai-evaluator", "obsidian-ai-evaluator", "/nonexistent"),
    ("obsidian-ai-status", "obsidian-ai-status", "/nonexistent"),
    ("obsidian-ai-reviewer", "obsidian-ai-reviewer", "/nonexistent"),
    ("obsidian-ai-executor", "obsidian-ai-executor", "/nonexistent"),
    ("obsidian-github-mirror", "obsidian-github-mirror", "/nonexistent"),
    ("obsidian-github-sync", "obsidian-github-sync", "/nonexistent"),
    ("obsidian-github-writer", "obsidian-github-writer", "/nonexistent"),
    ("obsidian-github-compactor", "obsidian-github-compactor", "/nonexistent"),
)

SHARED_GROUPS = (
    "obsidian-github-vault",
    "obsidian-github-pipeline",
)

SUPPLEMENTARY_GROUPS: dict[str, tuple[str, ...]] = {
    "obsidian-github-mirror": ("obsidian-github-vault",),
    "obsidian-github-sync": ("obsidian-github-vault", "obsidian-github-pipeline"),
    "obsidian-github-writer": ("obsidian-github-pipeline",),
    "obsidian-github-compactor": ("obsidian-github-pipeline",),
}

DIRECTORIES: tuple[tuple[str, str, str, int], ...] = (
    ("/var/lib/gitea-runner", "gitea-runner", "gitea-runner", 0o750),
    ("/etc/obsidian-core-promotion", "root", "obsidian-core-promoter", 0o750),
    ("/var/lib/obsidian-core-promotion", "obsidian-core-promoter", "obsidian-core-promoter", 0o700),
    ("/etc/obsidian-ai", "root", "obsidian-ai-sync", 0o750),
    ("/var/lib/obsidian-ai", "root", "root", 0o755),
    ("/var/lib/obsidian-ai/vault", "obsidian-ai-sync", "obsidian-ai-sync", 0o700),
    ("/var/lib/obsidian-ai/vault/11-Knowledge", "obsidian-ai-sync", "obsidian-ai-sync", 0o700),
    ("/var/lib/obsidian-ai/vault/10-Project", "obsidian-ai-sync", "obsidian-ai-sync", 0o700),
    ("/var/lib/obsidian-ai/state", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/00-Untrusted", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/02-Orchestration", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/02-Orchestration/recipes", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/02-Orchestration/status", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/04-Index", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/05-Context", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/10-Validation", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/12-Evaluation-Request", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/14-Evaluation-Context", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/15-Evaluation", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/16-Human-Projection", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/16-Human-Projection/reader", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/16-Human-Projection/generator", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/16-Human-Projection/validator", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/16-Human-Projection/evaluator", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/16-Human-Projection/reviewer", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/16-Human-Projection/executor", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/16-Human-Projection/sync", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/17-Human-Projection-Result", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/20-Review", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/24-Locks", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/24-Locks/read-view", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/25-Execution", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/27-Transport", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/state/30-Receipts", "root", "root", 0o700),
    ("/var/lib/obsidian-ai/deployments", "root", "root", 0o750),
    ("/etc/obsidian-github-sync", "root", "obsidian-github-sync", 0o750),
    ("/etc/obsidian-github-mirror", "root", "obsidian-github-mirror", 0o750),
    ("/etc/obsidian-github-writer", "root", "obsidian-github-writer", 0o750),
    ("/var/lib/obsidian-github-sync", "obsidian-github-sync", "obsidian-github-sync", 0o750),
    ("/var/lib/obsidian-github-pipeline", "root", "obsidian-github-pipeline", 0o750),
    ("/var/lib/obsidian-github-pipeline/24-Locks", "obsidian-github-writer", "obsidian-github-writer", 0o750),
    ("/var/lib/obsidian-github-pipeline/25-Execution", "obsidian-github-sync", "obsidian-github-pipeline", 0o2750),
    ("/var/lib/obsidian-github-pipeline/27-Transport", "obsidian-github-writer", "obsidian-github-pipeline", 0o2750),
    ("/var/lib/obsidian-github-mirror", "obsidian-github-mirror", "obsidian-github-mirror", 0o700),
    ("/var/lib/obsidian-github-mirror/state", "obsidian-github-mirror", "obsidian-github-mirror", 0o700),
    ("/var/lib/obsidian-github-mirror/state/24-Locks", "obsidian-github-mirror", "obsidian-github-mirror", 0o700),
    ("/var/lib/obsidian-github-mirror/state/24-Locks/read-view", "obsidian-github-mirror", "obsidian-github-mirror", 0o700),
    ("/srv/obsidian-github-sync", "root", "obsidian-github-vault", 0o750),
    ("/srv/obsidian-github-sync/vault", "obsidian-github-mirror", "obsidian-github-vault", 0o2750),
)

AI_ACLS: dict[str, tuple[str, ...]] = {
    "/etc/obsidian-ai": (
        "u:obsidian-ai-reviewer:--x",
    ),
    "/var/lib/obsidian-ai/vault": (
        "u:obsidian-ai-reader:--x",
        "u:obsidian-ai-validator:r-x",
        "u:obsidian-ai-executor:r-x",
    ),
    "/var/lib/obsidian-ai/vault/11-Knowledge": (
        "u:obsidian-ai-reader:r-x",
        "u:obsidian-ai-validator:r-x",
        "u:obsidian-ai-executor:r-x",
    ),
    "/var/lib/obsidian-ai/vault/10-Project": (
        "u:obsidian-ai-reader:r-x",
    ),
    "/var/lib/obsidian-ai/state": (
        "u:obsidian-ai-sync:r-x",
        "u:obsidian-ai-reader:--x",
        "u:obsidian-ai-generator:--x",
        "u:obsidian-ai-validator:--x",
        "u:obsidian-ai-evaluator:--x",
        "u:obsidian-ai-status:--x",
        "u:obsidian-ai-reviewer:r-x",
        "u:obsidian-ai-executor:r-x",
    ),
    "/var/lib/obsidian-ai/state/00-Untrusted": (
        "u:obsidian-ai-generator:rwx",
        "u:obsidian-ai-validator:r-x",
        "u:obsidian-ai-evaluator:r-x",
    ),
    "/var/lib/obsidian-ai/state/02-Orchestration": (
        "u:obsidian-ai-reader:rwx",
        "u:obsidian-ai-generator:rwx",
        "u:obsidian-ai-validator:rwx",
        "u:obsidian-ai-evaluator:rwx",
        "u:obsidian-ai-status:r-x",
        "u:obsidian-ai-reviewer:--x",
    ),
    "/var/lib/obsidian-ai/state/02-Orchestration/recipes": (
        "u:obsidian-ai-reader:rwx",
        "u:obsidian-ai-generator:r-x",
        "u:obsidian-ai-validator:r-x",
        "u:obsidian-ai-evaluator:r-x",
    ),
    "/var/lib/obsidian-ai/state/02-Orchestration/status": (
        "u:obsidian-ai-status:rwx",
        "u:obsidian-ai-reviewer:r-x",
    ),
    "/var/lib/obsidian-ai/state/04-Index": (
        "u:obsidian-ai-reader:rwx",
    ),
    "/var/lib/obsidian-ai/state/05-Context": (
        "u:obsidian-ai-reader:rwx",
        "u:obsidian-ai-generator:r-x",
        "u:obsidian-ai-evaluator:r-x",
    ),
    "/var/lib/obsidian-ai/state/10-Validation": (
        "u:obsidian-ai-sync:r-x",
        "u:obsidian-ai-validator:rwx",
        "u:obsidian-ai-evaluator:r-x",
        "u:obsidian-ai-reviewer:r-x",
        "u:obsidian-ai-executor:r-x",
    ),
    "/var/lib/obsidian-ai/state/12-Evaluation-Request": (
        "u:obsidian-ai-validator:rwx",
        "u:obsidian-ai-reader:r-x",
    ),
    "/var/lib/obsidian-ai/state/14-Evaluation-Context": (
        "u:obsidian-ai-reader:rwx",
        "u:obsidian-ai-evaluator:r-x",
    ),
    "/var/lib/obsidian-ai/state/15-Evaluation": (
        "u:obsidian-ai-evaluator:rwx",
        "u:obsidian-ai-reviewer:r-x",
    ),
    "/var/lib/obsidian-ai/state/16-Human-Projection": (
        "u:obsidian-ai-reader:--x",
        "u:obsidian-ai-generator:--x",
        "u:obsidian-ai-validator:--x",
        "u:obsidian-ai-evaluator:--x",
        "u:obsidian-ai-reviewer:--x",
        "u:obsidian-ai-executor:--x",
        "u:obsidian-ai-sync:r-x",
    ),
    "/var/lib/obsidian-ai/state/16-Human-Projection/reader": (
        "u:obsidian-ai-reader:rwx",
        "u:obsidian-ai-sync:r-x",
    ),
    "/var/lib/obsidian-ai/state/16-Human-Projection/generator": (
        "u:obsidian-ai-generator:rwx",
        "u:obsidian-ai-sync:r-x",
    ),
    "/var/lib/obsidian-ai/state/16-Human-Projection/validator": (
        "u:obsidian-ai-validator:rwx",
        "u:obsidian-ai-sync:r-x",
    ),
    "/var/lib/obsidian-ai/state/16-Human-Projection/evaluator": (
        "u:obsidian-ai-evaluator:rwx",
        "u:obsidian-ai-reviewer:r-x",
        "u:obsidian-ai-sync:r-x",
    ),
    "/var/lib/obsidian-ai/state/16-Human-Projection/reviewer": (
        "u:obsidian-ai-reviewer:rwx",
        "u:obsidian-ai-sync:r-x",
    ),
    "/var/lib/obsidian-ai/state/16-Human-Projection/executor": (
        "u:obsidian-ai-executor:rwx",
        "u:obsidian-ai-sync:r-x",
    ),
    "/var/lib/obsidian-ai/state/16-Human-Projection/sync": (
        "u:obsidian-ai-sync:rwx",
    ),
    "/var/lib/obsidian-ai/state/17-Human-Projection-Result": (
        "u:obsidian-ai-sync:rwx",
        "u:obsidian-ai-reviewer:r-x",
    ),
    "/var/lib/obsidian-ai/state/20-Review": (
        "u:obsidian-ai-sync:r-x",
        "u:obsidian-ai-reader:r-x",
        "u:obsidian-ai-reviewer:rwx",
        "u:obsidian-ai-executor:r-x",
    ),
    "/var/lib/obsidian-ai/state/24-Locks": (
        "u:obsidian-ai-sync:rwx",
        "u:obsidian-ai-reader:--x",
        "u:obsidian-ai-reviewer:rwx",
        "u:obsidian-ai-executor:rwx",
    ),
    "/var/lib/obsidian-ai/state/24-Locks/read-view": (
        "u:obsidian-ai-sync:rwx",
        "u:obsidian-ai-reader:rwx",
    ),
    "/var/lib/obsidian-ai/state/25-Execution": (
        "u:obsidian-ai-sync:r-x",
        "u:obsidian-ai-reviewer:r-x",
        "u:obsidian-ai-executor:rwx",
    ),
    "/var/lib/obsidian-ai/state/27-Transport": (
        "u:obsidian-ai-sync:rwx",
        "u:obsidian-ai-reviewer:r-x",
        "u:obsidian-ai-executor:r-x",
    ),
    "/var/lib/obsidian-ai/state/30-Receipts": (
        "u:obsidian-ai-reader:r-x",
        "u:obsidian-ai-reviewer:r-x",
        "u:obsidian-ai-executor:rwx",
    ),
}


def _run(
    runner: Runner,
    argv: Sequence[str],
    *,
    label: str,
    ok: tuple[int, ...] = (0,),
) -> CommandResult:
    result = runner(tuple(str(item) for item in argv))
    if result.returncode not in ok:
        raise AuthorityProvisionError(
            f"{label} failed with exit status {result.returncode}"
        )
    return result


def _command_exists(runner: Runner, name: str) -> None:
    _run(runner, ("sh", "-c", f"command -v {name} >/dev/null 2>&1"), label=f"require {name}")


def _group_exists(runner: Runner, name: str) -> bool:
    return runner(("getent", "group", name)).returncode == 0


def _user_exists(runner: Runner, name: str) -> bool:
    return runner(("id", "-u", name)).returncode == 0


def _ensure_group(runner: Runner, name: str) -> None:
    if not _group_exists(runner, name):
        _run(runner, ("groupadd", "--system", name), label=f"create group {name}")


def _ensure_user(
    runner: Runner,
    *,
    user: str,
    group: str,
    home: str,
) -> None:
    if not _user_exists(runner, user):
        _run(
            runner,
            (
                "useradd",
                "--system",
                "--gid",
                group,
                "--home-dir",
                home,
                "--no-create-home",
                "--shell",
                NOLOGIN,
                user,
            ),
            label=f"create user {user}",
        )
    primary = _run(
        runner,
        ("id", "-gn", user),
        label=f"read primary group {user}",
    ).stdout.strip()
    if primary != group:
        raise AuthorityProvisionError(f"{user} primary group mismatch")

    passwd = _run(
        runner,
        ("getent", "passwd", user),
        label=f"read passwd entry {user}",
    ).stdout.strip()
    parts = passwd.split(":")
    if len(parts) < 7 or parts[-1] != NOLOGIN or parts[-2] != home:
        raise AuthorityProvisionError(f"{user} account contract mismatch")


def _ensure_supplementary_groups(runner: Runner, user: str, groups: Sequence[str]) -> None:
    if not groups:
        return
    _run(
        runner,
        ("usermod", "-aG", ",".join(groups), user),
        label=f"set supplementary groups {user}",
    )


def _refuse_symlink(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise AuthorityProvisionError(f"refusing symlink path: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise AuthorityProvisionError(f"required directory path is not a directory: {path}")


def _ensure_dir(
    runner: Runner,
    *,
    path: str,
    owner: str,
    group: str,
    mode: int,
) -> None:
    target = Path(path)
    _refuse_symlink(target)
    _run(
        runner,
        (
            "install",
            "-d",
            "-o",
            owner,
            "-g",
            group,
            "-m",
            format(mode, "04o"),
            path,
        ),
        label=f"install directory {path}",
    )


def _reset_acl_dir(
    runner: Runner,
    path: str,
    entries: Sequence[str],
    *,
    defaults: bool = True,
) -> None:
    _run(runner, ("setfacl", "-b", path), label=f"clear ACL {path}")
    runner(("setfacl", "-k", path))
    _run(
        runner,
        ("setfacl", "-m", "u::rwx,g::---,o::---,m::rwx", path),
        label=f"set base ACL {path}",
    )
    if defaults:
        _run(
            runner,
            ("setfacl", "-m", "d:u::rwx,d:g::---,d:o::---,d:m::rwx", path),
            label=f"set default base ACL {path}",
        )
    for entry in entries:
        _run(runner, ("setfacl", "-m", entry, path), label=f"set ACL {path}")
        if defaults:
            _run(
                runner,
                ("setfacl", "-m", f"d:{entry}", path),
                label=f"set default ACL {path}",
            )


def _can(runner: Runner, user: str, flag: str, path: str) -> bool:
    return runner(("runuser", "-u", user, "--", "test", flag, path)).returncode == 0


def _require_access(
    runner: Runner,
    *,
    user: str,
    flag: str,
    path: str,
    expected: bool,
    label: str,
) -> None:
    actual = _can(runner, user, flag, path)
    if actual != expected:
        raise AuthorityProvisionError(f"authority gate failed: {label}")


def _apply_ai_acls(runner: Runner) -> None:
    no_default_acl = {
        "/var/lib/obsidian-ai/vault",
        "/var/lib/obsidian-ai/state",
    }
    for path, entries in AI_ACLS.items():
        _reset_acl_dir(
            runner,
            path,
            entries,
            defaults=path not in no_default_acl,
        )

    _require_access(
        runner,
        user="obsidian-ai-generator",
        flag="-r",
        path="/var/lib/obsidian-ai/state/05-Context",
        expected=True,
        label="generator reads Context",
    )
    _require_access(
        runner,
        user="obsidian-ai-generator",
        flag="-r",
        path="/var/lib/obsidian-ai/vault/11-Knowledge",
        expected=False,
        label="generator cannot read Knowledge",
    )
    _require_access(
        runner,
        user="obsidian-ai-generator",
        flag="-r",
        path="/var/lib/obsidian-ai/state/04-Index",
        expected=False,
        label="generator cannot read Index",
    )
    _require_access(
        runner,
        user="obsidian-ai-reader",
        flag="-w",
        path="/var/lib/obsidian-ai/state/04-Index",
        expected=True,
        label="reader writes Index",
    )
    _require_access(
        runner,
        user="obsidian-ai-reader",
        flag="-w",
        path="/var/lib/obsidian-ai/vault",
        expected=False,
        label="reader cannot write Vault",
    )
    _require_access(
        runner,
        user="obsidian-ai-status",
        flag="-w",
        path="/var/lib/obsidian-ai/state/02-Orchestration/status",
        expected=True,
        label="status writes status projection",
    )
    _require_access(
        runner,
        user="obsidian-ai-status",
        flag="-r",
        path="/var/lib/obsidian-ai/state/05-Context",
        expected=False,
        label="status cannot read Context",
    )
    _require_access(
        runner,
        user="obsidian-ai-executor",
        flag="-w",
        path="/var/lib/obsidian-ai/state/25-Execution",
        expected=True,
        label="executor writes Execution",
    )
    _require_access(
        runner,
        user="obsidian-ai-executor",
        flag="-w",
        path="/var/lib/obsidian-ai/state/27-Transport",
        expected=False,
        label="executor cannot write Transport",
    )
    _require_access(
        runner,
        user="obsidian-ai-sync",
        flag="-w",
        path="/var/lib/obsidian-ai/state/27-Transport",
        expected=True,
        label="sync writes Transport",
    )
    _require_access(
        runner,
        user="obsidian-ai-sync",
        flag="-w",
        path="/var/lib/obsidian-ai/state/30-Receipts",
        expected=False,
        label="sync cannot write Receipts",
    )
    for role in ("reader", "generator", "validator", "evaluator", "reviewer", "executor"):
        user = f"obsidian-ai-{role}"
        own = f"/var/lib/obsidian-ai/state/16-Human-Projection/{role}"
        _require_access(
            runner,
            user=user,
            flag="-w",
            path=own,
            expected=True,
            label=f"{role} writes own human projection queue",
        )
    _require_access(
        runner,
        user="obsidian-ai-generator",
        flag="-w",
        path="/var/lib/obsidian-ai/state/16-Human-Projection/validator",
        expected=False,
        label="generator cannot write validator human projection queue",
    )
    _require_access(
        runner,
        user="obsidian-ai-sync",
        flag="-r",
        path="/var/lib/obsidian-ai/state/16-Human-Projection/generator",
        expected=True,
        label="sync reads human projection requests",
    )
    _require_access(
        runner,
        user="obsidian-ai-sync",
        flag="-w",
        path="/var/lib/obsidian-ai/state/16-Human-Projection/generator",
        expected=False,
        label="sync cannot forge generator human projection requests",
    )
    _require_access(
        runner,
        user="obsidian-ai-sync",
        flag="-w",
        path="/var/lib/obsidian-ai/state/17-Human-Projection-Result",
        expected=True,
        label="sync writes human projection results",
    )


def _apply_github_acl(runner: Runner) -> None:
    _run(
        runner,
        (
            "setfacl",
            "-m",
            "u:obsidian-github-compactor:rwx",
            "/var/lib/obsidian-github-pipeline/25-Execution",
        ),
        label="grant compactor request cleanup",
    )

    _require_access(
        runner,
        user="obsidian-github-sync",
        flag="-r",
        path="/srv/obsidian-github-sync/vault",
        expected=True,
        label="GitHub watcher reads mirror",
    )
    _require_access(
        runner,
        user="obsidian-github-sync",
        flag="-w",
        path="/var/lib/obsidian-github-pipeline/25-Execution",
        expected=True,
        label="GitHub watcher writes requests",
    )
    _require_access(
        runner,
        user="obsidian-github-sync",
        flag="-w",
        path="/var/lib/obsidian-github-pipeline/27-Transport",
        expected=False,
        label="GitHub watcher cannot write results",
    )
    _require_access(
        runner,
        user="obsidian-github-writer",
        flag="-r",
        path="/etc/obsidian-github-sync",
        expected=False,
        label="GitHub writer cannot read watcher config",
    )
    _require_access(
        runner,
        user="obsidian-github-writer",
        flag="-r",
        path="/etc/obsidian-github-mirror",
        expected=False,
        label="GitHub writer cannot read mirror config",
    )
    _require_access(
        runner,
        user="obsidian-github-compactor",
        flag="-w",
        path="/var/lib/obsidian-github-pipeline/25-Execution",
        expected=True,
        label="compactor can remove requests",
    )
    _require_access(
        runner,
        user="obsidian-github-compactor",
        flag="-w",
        path="/var/lib/obsidian-github-pipeline/27-Transport",
        expected=False,
        label="compactor cannot write results",
    )
    _require_access(
        runner,
        user="obsidian-github-compactor",
        flag="-r",
        path="/etc/obsidian-github-writer",
        expected=False,
        label="compactor cannot read writer config",
    )
    _require_access(
        runner,
        user="obsidian-github-mirror",
        flag="-r",
        path="/var/lib/obsidian-github-pipeline",
        expected=False,
        label="mirror cannot read pipeline state",
    )


def _apply_cross_domain_gates(runner: Runner) -> None:
    for user in ("gitea-runner", "obsidian-core-promoter"):
        _require_access(
            runner,
            user=user,
            flag="-r",
            path="/etc/obsidian-ai",
            expected=False,
            label=f"{user} cannot read AI config",
        )
        _require_access(
            runner,
            user=user,
            flag="-r",
            path="/etc/obsidian-github-writer",
            expected=False,
            label=f"{user} cannot read GitHub writer config",
        )


def provision(
    *,
    runner: Runner = _default_runner,
    require_root: bool = True,
) -> dict[str, object]:
    if require_root and os.geteuid() != 0:
        raise AuthorityProvisionError("authority provisioning requires root")

    for command in (
        "getent",
        "groupadd",
        "id",
        "install",
        "runuser",
        "setfacl",
        "useradd",
        "usermod",
    ):
        _command_exists(runner, command)

    for _, group, _ in PRIMARY_USERS:
        _ensure_group(runner, group)
    for group in SHARED_GROUPS:
        _ensure_group(runner, group)

    for user, group, home in PRIMARY_USERS:
        _ensure_user(runner, user=user, group=group, home=home)

    for user, groups in SUPPLEMENTARY_GROUPS.items():
        _ensure_supplementary_groups(runner, user, groups)

    for path, owner, group, mode in DIRECTORIES:
        _ensure_dir(
            runner,
            path=path,
            owner=owner,
            group=group,
            mode=mode,
        )

    _apply_ai_acls(runner)
    _apply_github_acl(runner)
    _apply_cross_domain_gates(runner)

    return {
        "record_version": 1,
        "profile": PROFILE,
        "result": "passed",
        "identity_count": len(PRIMARY_USERS),
        "shared_group_count": len(SHARED_GROUPS),
        "directory_count": len(DIRECTORIES),
        "credentials_installed": False,
        "systemd_units_installed": False,
        "recurring_services_activated": False,
    }


def _build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="obsidian-automation-authority-provision",
        description=(
            "Provision consolidated local Unix identities/directories/ACLs only. "
            "Credentials and systemd production units are intentionally excluded."
        ),
    )


def main(argv: Iterable[str] | None = None) -> int:
    _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        result = provision()
    except AuthorityProvisionError as exc:
        print(
            json.dumps(
                {
                    "event": "obsidian-automation-authority-provision",
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
                "event": "obsidian-automation-authority-provision",
                "status": "completed",
                **result,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
