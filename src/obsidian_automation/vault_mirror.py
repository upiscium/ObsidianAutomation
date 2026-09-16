from __future__ import annotations

import argparse
import json
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .production_io import ProductionIOError, canonical_io_lock


class MirrorRefreshError(RuntimeError):
    """Raised when the pull-only production mirror cannot be refreshed safely."""


@dataclass(frozen=True)
class MirrorRefreshResult:
    remote: str
    vault_root: Path


def _safe_existing_directory(path: Path, *, label: str) -> Path:
    absolute = path.absolute()
    try:
        info = absolute.lstat()
    except FileNotFoundError as exc:
        raise MirrorRefreshError(f"{label} does not exist: {absolute}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise MirrorRefreshError(f"{label} is not a safe directory: {absolute}")
    return absolute


def _safe_regular_file(path: Path, *, label: str) -> Path:
    absolute = path.absolute()
    try:
        info = absolute.lstat()
    except FileNotFoundError as exc:
        raise MirrorRefreshError(f"{label} does not exist: {absolute}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise MirrorRefreshError(f"{label} is not a regular non-symlink file: {absolute}")
    return absolute


def _validated_remote(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MirrorRefreshError("rclone remote must be a non-empty trimmed string")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise MirrorRefreshError("rclone remote must not contain control characters")
    name, separator, _path = value.partition(":")
    if separator != ":" or not name:
        raise MirrorRefreshError("rclone source must be a named remote, not a local path")
    return value


def refresh_pull_only_mirror(
    ai_root: Path,
    vault_root: Path,
    *,
    remote: str,
    rclone_config: Path,
    filter_file: Path,
    runner: Callable[..., subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]] = subprocess.run,
) -> MirrorRefreshResult:
    destination = _safe_existing_directory(vault_root, label="local Vault mirror")
    config = _safe_regular_file(rclone_config, label="rclone config")
    filters = _safe_regular_file(filter_file, label="rclone filter file")
    source = _validated_remote(remote)

    command = [
        "rclone",
        "sync",
        source,
        str(destination),
        "--config",
        str(config),
        "--filter-from",
        str(filters),
        "--delete-after",
    ]

    with canonical_io_lock(ai_root):
        try:
            completed = runner(command, check=False)
        except OSError as exc:
            raise MirrorRefreshError(f"cannot execute rclone: {exc}") from exc
        if completed.returncode != 0:
            raise MirrorRefreshError(f"rclone pull-only mirror refresh failed with exit code {completed.returncode}")

    return MirrorRefreshResult(remote=source, vault_root=destination)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-production-vault-pull")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--rclone-config", type=Path, required=True)
    parser.add_argument("--filter-file", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        result = refresh_pull_only_mirror(
            args.ai_root,
            args.vault_root,
            remote=args.remote,
            rclone_config=args.rclone_config,
            filter_file=args.filter_file,
        )
    except (MirrorRefreshError, ProductionIOError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "status": "refreshed",
                "direction": "remote_to_local",
                "remote": result.remote,
                "vault_root": str(result.vault_root),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0
