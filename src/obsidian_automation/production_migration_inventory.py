"""Value-free inventory for writer-side ObsidianAutomation LXC migration.

This tool intentionally never opens credential/config files. It only lstat(2)s
explicit manifest paths and reports bounded filesystem metadata plus manifest
expectations. Secret values, hashes, endpoints, usernames, token prefixes and
key material are never emitted.
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
from typing import Iterable, Sequence


RECORD_VERSION = 1


class MigrationInventoryError(RuntimeError):
    """Fixed-code inventory failure."""


@dataclass(frozen=True)
class ManifestEntry:
    logical_id: str
    path: str
    category: str
    migration_action: str
    expected_fields: tuple[str, ...] = ()
    required: bool = False


ROLE_MANIFESTS: dict[str, tuple[ManifestEntry, ...]] = {
    "publisher": (
        ManifestEntry(
            "gitea_runner_state",
            "/var/lib/gitea-runner",
            "opaque_runtime_state",
            "reregister_preferred",
        ),
        ManifestEntry(
            "gitea_runner_unit",
            "/etc/systemd/system/act_runner.service",
            "deployment_config",
            "recreate_from_reviewed_config",
        ),
    ),
    "ai": (
        ManifestEntry(
            "ai_pull_rclone",
            "/etc/obsidian-ai/rclone.conf",
            "credential",
            "copy_narrow_private",
            ("type", "url", "vendor", "user", "pass"),
            True,
        ),
        ManifestEntry(
            "ai_pull_filters",
            "/etc/obsidian-ai/vault-pull.filters",
            "deployment_config",
            "copy_or_recreate",
            required=True,
        ),
        ManifestEntry(
            "ai_writer_webdav_password",
            "/etc/obsidian-ai/webdav-password",
            "credential",
            "copy_narrow_private_if_deployed",
        ),
        ManifestEntry(
            "ai_generator_env",
            "/etc/obsidian-ai/pre-review-generator.env",
            "credential_config",
            "copy_narrow_private",
            ("OPENAI_BASE_URL", "OPENAI_API_KEY"),
            True,
        ),
        ManifestEntry(
            "ai_evaluator_env",
            "/etc/obsidian-ai/pre-review-evaluator.env",
            "credential_config",
            "copy_narrow_private",
            ("OPENAI_BASE_URL", "OPENAI_API_KEY"),
            True,
        ),
        ManifestEntry(
            "ai_revision_env",
            "/etc/obsidian-ai/pre-review-revision.env",
            "derived_config",
            "recreate_from_target_revision",
            ("OBSIDIAN_AUTOMATION_REVISION",),
        ),
        ManifestEntry(
            "ai_durable_state",
            "/var/lib/obsidian-ai/state",
            "durable_state",
            "migrate_with_quiesced_copy",
            required=True,
        ),
        ManifestEntry(
            "ai_deployment_receipts",
            "/var/lib/obsidian-ai/deployments",
            "audit_state",
            "archive_or_copy_read_only",
        ),
        ManifestEntry(
            "ai_pull_mirror",
            "/var/lib/obsidian-ai/vault",
            "rebuildable_replica",
            "rebuild_preferred",
        ),
    ),
    "github-sync": (
        ManifestEntry(
            "github_sync_config",
            "/etc/obsidian-github-sync/config.toml",
            "deployment_config",
            "copy_or_recreate",
            required=True,
        ),
        ManifestEntry(
            "github_read_token_env",
            "/etc/obsidian-github-sync/credentials.env",
            "credential",
            "copy_narrow_private_if_required",
            ("GITHUB_TOKEN",),
        ),
        ManifestEntry(
            "github_mirror_rclone",
            "/etc/obsidian-github-mirror/rclone.conf",
            "credential",
            "copy_narrow_private",
            ("type", "url", "vendor", "user", "pass"),
            True,
        ),
        ManifestEntry(
            "github_mirror_filters",
            "/etc/obsidian-github-mirror/vault-pull.filters",
            "deployment_config",
            "copy_or_recreate",
            required=True,
        ),
        ManifestEntry(
            "github_writer_password",
            "/etc/obsidian-github-writer/webdav-password",
            "credential",
            "copy_narrow_private",
            required=True,
        ),
        ManifestEntry(
            "github_sync_state",
            "/var/lib/obsidian-github-sync",
            "durable_state",
            "migrate_with_quiesced_copy",
            required=True,
        ),
        ManifestEntry(
            "github_pipeline_state",
            "/var/lib/obsidian-github-pipeline",
            "durable_state",
            "migrate_with_quiesced_copy",
            required=True,
        ),
        ManifestEntry(
            "github_mirror_state",
            "/var/lib/obsidian-github-mirror",
            "rebuildable_replica",
            "rebuild_preferred",
        ),
        ManifestEntry(
            "github_vault_mirror",
            "/srv/obsidian-github-sync/vault",
            "rebuildable_replica",
            "rebuild_preferred",
        ),
    ),
}


def _safe_manifest_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise MigrationInventoryError("invalid_manifest_path")
    return path


def _rooted(root: Path, absolute: str) -> Path:
    path = _safe_manifest_path(absolute)
    return root.joinpath(*path.parts[1:])


def _name_for_uid(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid:{uid}"


def _name_for_gid(gid: int) -> str:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return f"gid:{gid}"


def _file_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "regular"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "char_device"
    if stat.S_ISBLK(mode):
        return "block_device"
    return "other"


def _size_class(info: os.stat_result) -> str:
    if not stat.S_ISREG(info.st_mode):
        return "not_regular"
    size = info.st_size
    if size == 0:
        return "empty"
    if size <= 1024:
        return "lte_1k"
    if size <= 16 * 1024:
        return "lte_16k"
    if size <= 1024 * 1024:
        return "lte_1m"
    return "gt_1m"


def _inspect_entry(root: Path, entry: ManifestEntry) -> dict[str, object]:
    path = _rooted(root, entry.path)
    result: dict[str, object] = {
        "logical_id": entry.logical_id,
        "path": entry.path,
        "category": entry.category,
        "migration_action": entry.migration_action,
        "required": entry.required,
        "expected_fields": list(entry.expected_fields),
    }
    try:
        info = path.lstat()
    except FileNotFoundError:
        result.update(
            {
                "exists": False,
                "file_type": "missing",
                "owner": None,
                "group": None,
                "mode": None,
                "size_class": None,
            }
        )
        return result
    except OSError as exc:
        raise MigrationInventoryError("metadata_unreadable") from exc

    result.update(
        {
            "exists": True,
            "file_type": _file_type(info.st_mode),
            "owner": _name_for_uid(info.st_uid),
            "group": _name_for_gid(info.st_gid),
            "mode": format(stat.S_IMODE(info.st_mode), "04o"),
            "size_class": _size_class(info),
        }
    )
    return result


def inspect_role(role: str, *, root: Path = Path("/")) -> dict[str, object]:
    if role not in ROLE_MANIFESTS:
        raise MigrationInventoryError("unsupported_role")
    if not root.is_absolute():
        raise MigrationInventoryError("root_must_be_absolute")
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise MigrationInventoryError("root_unreadable") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise MigrationInventoryError("root_must_be_directory")

    items = [_inspect_entry(root, entry) for entry in ROLE_MANIFESTS[role]]
    missing_required = [
        item["logical_id"]
        for item in items
        if item["required"] and not item["exists"]
    ]
    unsafe_types = [
        item["logical_id"]
        for item in items
        if item["exists"] and item["file_type"] not in {"regular", "directory"}
    ]
    return {
        "record_version": RECORD_VERSION,
        "inspection_only": True,
        "values_read": False,
        "role": role,
        "items": items,
        "summary": {
            "declared": len(items),
            "present": sum(bool(item["exists"]) for item in items),
            "missing_required": missing_required,
            "unsafe_types": unsafe_types,
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obsidian-production-migration-inventory",
        description=(
            "Inspect only filesystem metadata for explicitly declared migration "
            "credential/config/state paths. File contents are never opened."
        ),
    )
    parser.add_argument("--role", choices=tuple(sorted(ROLE_MANIFESTS)), required=True)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/"),
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        report = inspect_role(args.role, root=args.root)
    except MigrationInventoryError as exc:
        print(
            json.dumps(
                {
                    "record_version": RECORD_VERSION,
                    "inspection_only": True,
                    "values_read": False,
                    "status": "error",
                    "error_code": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
