from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path

import pytest

import importlib.util
import sys


def _load_transfer():
    path = Path("tools/private_config_transfer.py")
    spec = importlib.util.spec_from_file_location("private_config_transfer_tool", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


transfer = _load_transfer()
SECRET = b"PRIVATE-MIGRATION-CANARY-never-print-this"


def _write(root: Path, absolute: str, data: bytes = SECRET) -> Path:
    target = root.joinpath(*Path(absolute).parts[1:])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def _populate_required(root: Path, role: str) -> None:
    for entry in transfer.ROLE_FILES[role]:
        if entry.required:
            _write(root, entry.path)


def test_ai_bundle_round_trip_uses_only_declared_logical_ids(tmp_path: Path) -> None:
    _populate_required(tmp_path, "ai")
    _write(tmp_path, "/etc/obsidian-ai/webdav-password")
    _write(tmp_path, "/etc/obsidian-ai/projection-cleanup.env")
    _write(tmp_path, "/etc/obsidian-ai/projection-cleanup-password")

    stream = BytesIO()
    status = transfer.write_bundle("ai", stream, source_root=tmp_path)

    assert status == {
        "record_version": 1,
        "role": "ai",
        "file_count": 7,
        "values_printed": False,
    }

    stream.seek(0)
    values = transfer.read_bundle(stream, expected_role="ai")
    assert {entry.logical_id for entry, _data in values} == {
        "ai_pull_rclone",
        "ai_pull_filters",
        "ai_writer_webdav_password",
        "ai_projection_cleanup_env",
        "ai_projection_cleanup_password",
        "ai_generator_env",
        "ai_evaluator_env",
    }
    assert all(data == SECRET for _entry, data in values)


def test_status_metadata_does_not_contain_secret(tmp_path: Path) -> None:
    _populate_required(tmp_path, "github-sync")

    stream = BytesIO()
    status = transfer.write_bundle(
        "github-sync",
        stream,
        source_root=tmp_path,
    )

    encoded = json.dumps(status, sort_keys=True)
    assert SECRET.decode() not in encoded
    assert "values_printed" in encoded


def test_missing_required_source_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(
        transfer.PrivateConfigTransferError,
        match="required_file_missing:ai_pull_rclone",
    ):
        transfer.collect_files("ai", source_root=tmp_path)


def test_partial_core_promotion_boundary_is_rejected(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "/etc/obsidian-core-promotion/promotion.env",
    )

    with pytest.raises(
        transfer.PrivateConfigTransferError,
        match="conditional_group_incomplete:core_promotion",
    ):
        transfer.collect_files("publisher", source_root=tmp_path)


def test_complete_core_promotion_boundary_is_accepted(tmp_path: Path) -> None:
    for entry in transfer.ROLE_FILES["publisher"]:
        _write(tmp_path, entry.path)

    stream = BytesIO()
    status = transfer.write_bundle(
        "publisher",
        stream,
        source_root=tmp_path,
    )
    assert status["file_count"] == 3

    stream.seek(0)
    values = transfer.read_bundle(stream, expected_role="publisher")
    assert len(values) == 3


def test_symlink_source_is_rejected_without_following_target(tmp_path: Path) -> None:
    _populate_required(tmp_path, "ai")

    target = tmp_path / "private-target"
    target.write_bytes(SECRET)

    link = tmp_path / "etc/obsidian-ai/rclone.conf"
    link.unlink()
    link.symlink_to(target)

    with pytest.raises(
        transfer.PrivateConfigTransferError,
        match="unsafe_source_type:ai_pull_rclone",
    ):
        transfer.collect_files("ai", source_root=tmp_path)


def test_unknown_bundle_entry_is_rejected() -> None:
    header = {
        "record_version": 1,
        "role": "ai",
        "entries": [{"logical_id": "not-declared", "size": 0}],
    }
    stream = BytesIO(
        transfer.MAGIC
        + json.dumps(header, separators=(",", ":")).encode()
        + b"\n"
    )

    with pytest.raises(
        transfer.PrivateConfigTransferError,
        match="unknown_bundle_entry",
    ):
        transfer.read_bundle(stream, expected_role="ai")


def test_trailing_bundle_data_is_rejected(tmp_path: Path) -> None:
    _populate_required(tmp_path, "ai")

    stream = BytesIO()
    transfer.write_bundle("ai", stream, source_root=tmp_path)
    stream.write(b"unexpected")

    stream.seek(0)
    with pytest.raises(
        transfer.PrivateConfigTransferError,
        match="trailing_bundle_data",
    ):
        transfer.read_bundle(stream, expected_role="ai")


def test_target_manifest_preserves_observed_owner_group_modes() -> None:
    by_role = {
        role: {entry.logical_id: entry for entry in entries}
        for role, entries in transfer.ROLE_FILES.items()
    }

    assert (
        by_role["publisher"]["core_promotion_password"].owner,
        by_role["publisher"]["core_promotion_password"].group,
        by_role["publisher"]["core_promotion_password"].mode,
    ) == ("root", "obsidian-core-promoter", 0o640)

    assert (
        by_role["ai"]["ai_pull_rclone"].owner,
        by_role["ai"]["ai_pull_rclone"].group,
        by_role["ai"]["ai_pull_rclone"].mode,
    ) == ("obsidian-ai-sync", "obsidian-ai-sync", 0o600)

    assert (
        by_role["ai"]["ai_writer_webdav_password"].owner,
        by_role["ai"]["ai_writer_webdav_password"].group,
        by_role["ai"]["ai_writer_webdav_password"].mode,
    ) == ("obsidian-ai-sync", "obsidian-ai-sync", 0o400)

    assert (
        by_role["ai"]["ai_projection_cleanup_env"].owner,
        by_role["ai"]["ai_projection_cleanup_env"].group,
        by_role["ai"]["ai_projection_cleanup_env"].mode,
    ) == ("root", "obsidian-ai-sync", 0o640)

    assert (
        by_role["ai"]["ai_projection_cleanup_password"].owner,
        by_role["ai"]["ai_projection_cleanup_password"].group,
        by_role["ai"]["ai_projection_cleanup_password"].mode,
    ) == ("obsidian-ai-sync", "obsidian-ai-sync", 0o400)

    assert (
        by_role["ai"]["ai_generator_env"].owner,
        by_role["ai"]["ai_generator_env"].group,
        by_role["ai"]["ai_generator_env"].mode,
    ) == ("root", "root", 0o600)

    assert (
        by_role["github-sync"]["github_writer_password"].owner,
        by_role["github-sync"]["github_writer_password"].group,
        by_role["github-sync"]["github_writer_password"].mode,
    ) == ("root", "obsidian-github-writer", 0o640)


def test_revision_env_and_rebuildable_state_are_not_private_bundle_members() -> None:
    rendered = repr(transfer.ROLE_FILES)
    assert "pre-review-revision.env" not in rendered
    assert "/var/lib/obsidian-ai/state" not in rendered
    assert "/var/lib/obsidian-ai/vault" not in rendered
    assert "/var/lib/obsidian-github-pipeline" not in rendered
    assert "/srv/obsidian-github-sync/vault" not in rendered
    assert "/var/lib/obsidian-core-promotion" not in rendered


def test_export_source_root_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(
        transfer.PrivateConfigTransferError,
        match="source_root_must_be_absolute",
    ):
        transfer.collect_files("ai", source_root=Path("relative"))
