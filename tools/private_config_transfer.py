#!/usr/bin/env python3
"""Narrow private-config transfer for consolidated ObsidianAutomation migration.

The exporter reads only explicitly declared regular files and writes a small
length-framed binary bundle to stdout. The importer accepts only the declared
logical IDs and atomically installs them with target-owned owner/group/mode.
No secret value, hash, endpoint, or token fragment is printed.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import grp
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import stat
import sys
import tempfile
from typing import BinaryIO, Iterable, Sequence


MAGIC = b"OAPCFG1\n"
RECORD_VERSION = 1
MAX_HEADER = 64 * 1024
MAX_FILE_SIZE = 1024 * 1024
MAX_BUNDLE_SIZE = 8 * 1024 * 1024


class PrivateConfigTransferError(RuntimeError):
    """Fixed/bounded private-config migration failure."""


@dataclass(frozen=True)
class PrivateFile:
    logical_id: str
    path: str
    owner: str
    group: str
    mode: int
    required: bool = True
    conditional_group: str | None = None


ROLE_FILES: dict[str, tuple[PrivateFile, ...]] = {
    "publisher": (
        PrivateFile(
            "core_promotion_env",
            "/etc/obsidian-core-promotion/promotion.env",
            "root",
            "obsidian-core-promoter",
            0o640,
            required=False,
            conditional_group="core_promotion",
        ),
        PrivateFile(
            "core_promotion_policy",
            "/etc/obsidian-core-promotion/public-export.toml",
            "root",
            "obsidian-core-promoter",
            0o640,
            required=False,
            conditional_group="core_promotion",
        ),
        PrivateFile(
            "core_promotion_password",
            "/etc/obsidian-core-promotion/nextcloud.password",
            "root",
            "obsidian-core-promoter",
            0o640,
            required=False,
            conditional_group="core_promotion",
        ),
    ),
    "ai": (
        PrivateFile(
            "ai_pull_rclone",
            "/etc/obsidian-ai/rclone.conf",
            "obsidian-ai-sync",
            "obsidian-ai-sync",
            0o600,
        ),
        PrivateFile(
            "ai_pull_filters",
            "/etc/obsidian-ai/vault-pull.filters",
            "root",
            "obsidian-ai-sync",
            0o640,
        ),
        PrivateFile(
            "ai_writer_webdav_password",
            "/etc/obsidian-ai/webdav-password",
            "obsidian-ai-sync",
            "obsidian-ai-sync",
            0o400,
            required=False,
        ),
        PrivateFile(
            "ai_generator_env",
            "/etc/obsidian-ai/pre-review-generator.env",
            "root",
            "root",
            0o600,
        ),
        PrivateFile(
            "ai_evaluator_env",
            "/etc/obsidian-ai/pre-review-evaluator.env",
            "root",
            "root",
            0o600,
        ),
    ),
    "github-sync": (
        PrivateFile(
            "github_sync_config",
            "/etc/obsidian-github-sync/config.toml",
            "root",
            "obsidian-github-sync",
            0o640,
        ),
        PrivateFile(
            "github_read_token_env",
            "/etc/obsidian-github-sync/credentials.env",
            "root",
            "obsidian-github-sync",
            0o640,
            required=False,
        ),
        PrivateFile(
            "github_mirror_rclone",
            "/etc/obsidian-github-mirror/rclone.conf",
            "root",
            "obsidian-github-mirror",
            0o640,
        ),
        PrivateFile(
            "github_mirror_filters",
            "/etc/obsidian-github-mirror/vault-pull.filters",
            "root",
            "obsidian-github-mirror",
            0o640,
        ),
        PrivateFile(
            "github_writer_config",
            "/etc/obsidian-github-writer/config.env",
            "root",
            "obsidian-github-writer",
            0o640,
        ),
        PrivateFile(
            "github_writer_password",
            "/etc/obsidian-github-writer/webdav-password",
            "root",
            "obsidian-github-writer",
            0o640,
        ),
    ),
}


READERS: dict[str, dict[str, tuple[str, ...]]] = {
    "publisher": {
        "core_promotion_env": ("obsidian-core-promoter",),
        "core_promotion_policy": ("obsidian-core-promoter",),
        "core_promotion_password": ("obsidian-core-promoter",),
    },
    "ai": {
        "ai_pull_rclone": ("obsidian-ai-sync",),
        "ai_pull_filters": ("obsidian-ai-sync",),
        "ai_writer_webdav_password": ("obsidian-ai-sync",),
        "ai_generator_env": (),
        "ai_evaluator_env": (),
    },
    "github-sync": {
        "github_sync_config": ("obsidian-github-sync",),
        "github_read_token_env": ("obsidian-github-sync",),
        "github_mirror_rclone": ("obsidian-github-mirror",),
        "github_mirror_filters": ("obsidian-github-mirror",),
        "github_writer_config": ("obsidian-github-writer",),
        "github_writer_password": ("obsidian-github-writer",),
    },
}


ROLE_USERS: dict[str, tuple[str, ...]] = {
    "publisher": (
        "gitea-runner",
        "obsidian-core-promoter",
        "obsidian-ai-sync",
        "obsidian-github-sync",
        "obsidian-github-writer",
    ),
    "ai": (
        "obsidian-ai-sync",
        "obsidian-ai-reader",
        "obsidian-ai-generator",
        "obsidian-ai-validator",
        "obsidian-ai-evaluator",
        "obsidian-ai-status",
        "obsidian-ai-reviewer",
        "obsidian-ai-executor",
        "obsidian-core-promoter",
        "obsidian-github-sync",
        "obsidian-github-writer",
    ),
    "github-sync": (
        "obsidian-github-mirror",
        "obsidian-github-sync",
        "obsidian-github-writer",
        "obsidian-github-compactor",
        "gitea-runner",
        "obsidian-core-promoter",
        "obsidian-ai-sync",
    ),
}


def _manifest(role: str) -> dict[str, PrivateFile]:
    try:
        values = ROLE_FILES[role]
    except KeyError as exc:
        raise PrivateConfigTransferError("unsupported_role") from exc
    return {entry.logical_id: entry for entry in values}


def _safe_absolute(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise PrivateConfigTransferError("invalid_manifest_path")
    return path


def _rooted(root: Path, absolute: str) -> Path:
    path = _safe_absolute(absolute)
    return root.joinpath(*path.parts[1:])


def _require_root() -> None:
    if os.geteuid() != 0:
        raise PrivateConfigTransferError("operation_requires_root")


def _read_private_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise PrivateConfigTransferError("declared_file_missing") from exc
    except OSError as exc:
        raise PrivateConfigTransferError("declared_file_unreadable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PrivateConfigTransferError("declared_path_not_regular_file")
        if info.st_size > MAX_FILE_SIZE:
            raise PrivateConfigTransferError("declared_file_too_large")
        chunks: list[bytes] = []
        remaining = MAX_FILE_SIZE + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_FILE_SIZE:
            raise PrivateConfigTransferError("declared_file_too_large")
        return data
    finally:
        os.close(fd)


def _validate_presence(
    role: str,
    present: set[str],
) -> None:
    entries = ROLE_FILES[role]
    for entry in entries:
        if entry.required and entry.logical_id not in present:
            raise PrivateConfigTransferError(
                f"required_file_missing:{entry.logical_id}"
            )

    conditional: dict[str, set[str]] = {}
    for entry in entries:
        if entry.conditional_group:
            conditional.setdefault(entry.conditional_group, set()).add(
                entry.logical_id
            )
    for name, expected in conditional.items():
        found = expected & present
        if found and found != expected:
            raise PrivateConfigTransferError(
                f"conditional_group_incomplete:{name}"
            )


def collect_files(
    role: str,
    *,
    source_root: Path = Path("/"),
) -> list[tuple[PrivateFile, bytes]]:
    manifest = _manifest(role)
    if not source_root.is_absolute():
        raise PrivateConfigTransferError("source_root_must_be_absolute")

    collected: list[tuple[PrivateFile, bytes]] = []
    present: set[str] = set()
    total = 0

    for logical_id, entry in manifest.items():
        path = _rooted(source_root, entry.path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise PrivateConfigTransferError(
                f"unsafe_source_type:{logical_id}"
            )
        data = _read_private_file(path)
        total += len(data)
        if total > MAX_BUNDLE_SIZE:
            raise PrivateConfigTransferError("bundle_too_large")
        collected.append((entry, data))
        present.add(logical_id)

    _validate_presence(role, present)
    return collected


def write_bundle(
    role: str,
    stream: BinaryIO,
    *,
    source_root: Path = Path("/"),
) -> dict[str, object]:
    collected = collect_files(role, source_root=source_root)
    header = {
        "record_version": RECORD_VERSION,
        "role": role,
        "entries": [
            {"logical_id": entry.logical_id, "size": len(data)}
            for entry, data in collected
        ],
    }
    encoded = json.dumps(
        header,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_HEADER:
        raise PrivateConfigTransferError("bundle_header_too_large")

    stream.write(MAGIC)
    stream.write(encoded + b"\n")
    for _entry, data in collected:
        stream.write(data)
    stream.flush()

    return {
        "record_version": RECORD_VERSION,
        "role": role,
        "file_count": len(collected),
        "values_printed": False,
    }


def _readline_limited(stream: BinaryIO, limit: int) -> bytes:
    line = stream.readline(limit + 1)
    if len(line) > limit or not line.endswith(b"\n"):
        raise PrivateConfigTransferError("invalid_bundle_header")
    return line


def read_bundle(
    stream: BinaryIO,
    *,
    expected_role: str,
) -> list[tuple[PrivateFile, bytes]]:
    if _readline_limited(stream, len(MAGIC)) != MAGIC:
        raise PrivateConfigTransferError("invalid_bundle_magic")

    raw = _readline_limited(stream, MAX_HEADER)
    try:
        header = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PrivateConfigTransferError("invalid_bundle_json") from exc

    if not isinstance(header, dict):
        raise PrivateConfigTransferError("invalid_bundle_json")
    if header.get("record_version") != RECORD_VERSION:
        raise PrivateConfigTransferError("unsupported_bundle_version")
    if header.get("role") != expected_role:
        raise PrivateConfigTransferError("bundle_role_mismatch")

    raw_entries = header.get("entries")
    if not isinstance(raw_entries, list):
        raise PrivateConfigTransferError("invalid_bundle_entries")

    manifest = _manifest(expected_role)
    seen: set[str] = set()
    result: list[tuple[PrivateFile, bytes]] = []
    total = 0

    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise PrivateConfigTransferError("invalid_bundle_entry")
        logical_id = raw_entry.get("logical_id")
        size = raw_entry.get("size")
        if not isinstance(logical_id, str) or logical_id not in manifest:
            raise PrivateConfigTransferError("unknown_bundle_entry")
        if logical_id in seen:
            raise PrivateConfigTransferError("duplicate_bundle_entry")
        if not isinstance(size, int) or size < 0 or size > MAX_FILE_SIZE:
            raise PrivateConfigTransferError("invalid_bundle_size")

        data = stream.read(size)
        if len(data) != size:
            raise PrivateConfigTransferError("truncated_bundle")
        total += size
        if total > MAX_BUNDLE_SIZE:
            raise PrivateConfigTransferError("bundle_too_large")

        seen.add(logical_id)
        result.append((manifest[logical_id], data))

    if stream.read(1) != b"":
        raise PrivateConfigTransferError("trailing_bundle_data")

    _validate_presence(expected_role, seen)
    return result


def _reject_symlink_components(path: Path) -> None:
    if not path.is_absolute():
        raise PrivateConfigTransferError("destination_must_be_absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode):
            raise PrivateConfigTransferError("destination_symlink_component")


def _identity_ids(entry: PrivateFile) -> tuple[int, int]:
    try:
        uid = pwd.getpwnam(entry.owner).pw_uid
        gid = grp.getgrnam(entry.group).gr_gid
    except KeyError as exc:
        raise PrivateConfigTransferError("destination_identity_missing") from exc
    return uid, gid


def _existing_bytes(path: Path) -> bytes:
    return _read_private_file(path)


def _install_one(
    entry: PrivateFile,
    data: bytes,
    *,
    destination_root: Path = Path("/"),
) -> str:
    destination = _rooted(destination_root, entry.path)
    _reject_symlink_components(destination)

    parent = destination.parent
    try:
        parent_info = parent.lstat()
    except FileNotFoundError as exc:
        raise PrivateConfigTransferError("destination_parent_missing") from exc
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise PrivateConfigTransferError("destination_parent_unsafe")

    uid, gid = _identity_ids(entry)

    try:
        existing = destination.lstat()
    except FileNotFoundError:
        existing = None

    if existing is not None:
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
            raise PrivateConfigTransferError(
                f"destination_conflict_type:{entry.logical_id}"
            )
        if _existing_bytes(destination) != data:
            raise PrivateConfigTransferError(
                f"destination_content_conflict:{entry.logical_id}"
            )
        os.chown(destination, uid, gid)
        os.chmod(destination, entry.mode)
        return "unchanged"

    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=parent,
    )
    temporary_path = Path(temporary)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, entry.mode)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise PrivateConfigTransferError("short_private_write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)

    try:
        os.replace(temporary_path, destination)
        dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return "installed"


def install_bundle(
    role: str,
    stream: BinaryIO,
    *,
    destination_root: Path = Path("/"),
) -> dict[str, object]:
    entries = read_bundle(stream, expected_role=role)
    outcomes: dict[str, str] = {}
    for entry, data in entries:
        outcomes[entry.logical_id] = _install_one(
            entry,
            data,
            destination_root=destination_root,
        )
    return {
        "record_version": RECORD_VERSION,
        "role": role,
        "result": "installed",
        "file_count": len(entries),
        "outcomes": outcomes,
        "values_printed": False,
    }


def _can_read(user: str, path: str) -> bool:
    completed = os.spawnvp(
        os.P_WAIT,
        "runuser",
        ("runuser", "-u", user, "--", "test", "-r", path),
    )
    return completed == 0


def verify_installed(role: str) -> dict[str, object]:
    manifest = _manifest(role)
    present = {
        logical_id
        for logical_id, entry in manifest.items()
        if Path(entry.path).exists()
    }
    _validate_presence(role, present)

    readers = READERS[role]
    users = ROLE_USERS[role]
    checked = 0

    for logical_id in sorted(present):
        entry = manifest[logical_id]
        path = Path(entry.path)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise PrivateConfigTransferError(
                f"unsafe_destination_type:{logical_id}"
            )
        uid, gid = _identity_ids(entry)
        if info.st_uid != uid or info.st_gid != gid:
            raise PrivateConfigTransferError(
                f"destination_owner_mismatch:{logical_id}"
            )
        if stat.S_IMODE(info.st_mode) != entry.mode:
            raise PrivateConfigTransferError(
                f"destination_mode_mismatch:{logical_id}"
            )

        allowed = set(readers[logical_id])
        for user in users:
            actual = _can_read(user, entry.path)
            expected = user in allowed
            if actual != expected:
                raise PrivateConfigTransferError(
                    f"readability_gate_failed:{logical_id}:{user}"
                )
            checked += 1

    return {
        "record_version": RECORD_VERSION,
        "role": role,
        "result": "passed",
        "file_count": len(present),
        "readability_checks": checked,
        "values_read_for_verification": False,
        "values_printed": False,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-private-config-transfer",
        description=(
            "Stream only declared private migration files and install them with "
            "fixed destination authority. Never pass secret values as arguments."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export")
    export.add_argument("--role", choices=tuple(sorted(ROLE_FILES)), required=True)
    export.add_argument(
        "--source-root",
        type=Path,
        default=Path("/"),
        help=argparse.SUPPRESS,
    )

    install = subparsers.add_parser("install")
    install.add_argument("--role", choices=tuple(sorted(ROLE_FILES)), required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--role", choices=tuple(sorted(ROLE_FILES)), required=True)

    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        _require_root()
        if args.command == "export":
            result = write_bundle(
                args.role,
                sys.stdout.buffer,
                source_root=args.source_root,
            )
            print(
                json.dumps(
                    {
                        "event": "obsidian-private-config-export",
                        "status": "completed",
                        **result,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 0

        if args.command == "install":
            result = install_bundle(args.role, sys.stdin.buffer)
        else:
            result = verify_installed(args.role)

        print(
            json.dumps(
                {
                    "event": f"obsidian-private-config-{args.command}",
                    "status": "completed",
                    **result,
                },
                sort_keys=True,
            )
        )
        return 0
    except PrivateConfigTransferError as exc:
        target = sys.stderr
        print(
            json.dumps(
                {
                    "event": "obsidian-private-config-transfer",
                    "status": "failed",
                    "message": str(exc),
                },
                sort_keys=True,
            ),
            file=target,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
