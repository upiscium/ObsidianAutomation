from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.production_migration_inventory import (
    MigrationInventoryError,
    ROLE_MANIFESTS,
    inspect_role,
    main,
)


SECRET = "PRIVATE-CREDENTIAL-CANARY-never-print-this"


def _write(root: Path, absolute: str, data: str, mode: int = 0o600) -> Path:
    target = root.joinpath(*Path(absolute).parts[1:])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(data, encoding="utf-8")
    target.chmod(mode)
    return target


def test_ai_inventory_reports_metadata_without_reading_values(tmp_path: Path) -> None:
    _write(tmp_path, "/etc/obsidian-ai/rclone.conf", SECRET)
    _write(tmp_path, "/etc/obsidian-ai/vault-pull.filters", "filter")
    _write(tmp_path, "/etc/obsidian-ai/pre-review-generator.env", SECRET)
    _write(tmp_path, "/etc/obsidian-ai/pre-review-evaluator.env", SECRET)
    state = tmp_path / "var/lib/obsidian-ai/state"
    state.mkdir(parents=True)

    report = inspect_role("ai", root=tmp_path)
    encoded = json.dumps(report, sort_keys=True)

    assert report["inspection_only"] is True
    assert report["values_read"] is False
    assert SECRET not in encoded
    assert "https://" not in encoded
    assert "PRIVATE-CREDENTIAL" not in encoded
    assert report["summary"]["missing_required"] == []

    by_id = {item["logical_id"]: item for item in report["items"]}
    assert by_id["ai_pull_rclone"]["exists"] is True
    assert by_id["ai_pull_rclone"]["mode"] == "0600"
    assert by_id["ai_pull_rclone"]["file_type"] == "regular"
    assert by_id["ai_durable_state"]["file_type"] == "directory"
    assert by_id["ai_generator_env"]["expected_fields"] == [
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
    ]
    assert by_id["ai_projection_cleanup_env"]["expected_fields"] == [
        "PROJECTION_CLEANUP_BASE_URL",
        "PROJECTION_CLEANUP_USERNAME",
    ]
    assert by_id["ai_projection_cleanup_password"]["category"] == "credential"


def test_inventory_does_not_follow_symlink_or_emit_target(tmp_path: Path) -> None:
    outside = tmp_path / "private-target"
    outside.write_text(SECRET, encoding="utf-8")
    link = tmp_path / "etc/obsidian-ai/rclone.conf"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    report = inspect_role("ai", root=tmp_path)
    item = next(
        value for value in report["items"]
        if value["logical_id"] == "ai_pull_rclone"
    )
    assert item["file_type"] == "symlink"
    assert "ai_pull_rclone" in report["summary"]["unsafe_types"]
    assert str(outside) not in json.dumps(report)
    assert SECRET not in json.dumps(report)


def test_github_inventory_marks_required_missing_without_contents(tmp_path: Path) -> None:
    report = inspect_role("github-sync", root=tmp_path)
    missing = set(report["summary"]["missing_required"])
    assert {
        "github_sync_config",
        "github_mirror_rclone",
        "github_mirror_filters",
        "github_writer_config",
        "github_writer_password",
        "github_sync_state",
        "github_pipeline_state",
    }.issubset(missing)


def test_publisher_manifest_does_not_claim_gitea_repository_secrets() -> None:
    text = json.dumps(
        [entry.__dict__ for entry in ROLE_MANIFESTS["publisher"]],
        sort_keys=True,
    )
    assert "OBSIDIAN_CORE_DEPLOY_KEY" not in text
    assert "OBSIDIAN_CORE_KNOWN_HOSTS" not in text
    assert "OBSIDIAN_AUTOMATION_REF" not in text


def test_relative_root_is_rejected() -> None:
    with pytest.raises(MigrationInventoryError, match="root_must_be_absolute"):
        inspect_role("ai", root=Path("relative"))


def test_cli_output_is_value_free(tmp_path: Path, capsys) -> None:
    _write(tmp_path, "/etc/obsidian-github-sync/credentials.env", SECRET)
    rc = main(["--role", "github-sync", "--root", str(tmp_path)])
    captured = capsys.readouterr()

    assert rc == 0
    assert SECRET not in captured.out
    value = json.loads(captured.out)
    assert value["values_read"] is False
    assert value["role"] == "github-sync"


def test_publisher_manifest_uses_production_runner_unit_name() -> None:
    by_id = {entry.logical_id: entry for entry in ROLE_MANIFESTS["publisher"]}
    assert by_id["gitea_runner_unit"].path == "/etc/systemd/system/gitea-runner.service"


def test_publisher_manifest_includes_core_promotion_private_boundary() -> None:
    by_id = {entry.logical_id: entry for entry in ROLE_MANIFESTS["publisher"]}

    assert by_id["core_promotion_env"].path == (
        "/etc/obsidian-core-promotion/promotion.env"
    )
    assert by_id["core_promotion_policy"].path == (
        "/etc/obsidian-core-promotion/public-export.toml"
    )
    assert by_id["core_promotion_password"].path == (
        "/etc/obsidian-core-promotion/nextcloud.password"
    )
    assert by_id["core_promotion_state"].path == (
        "/var/lib/obsidian-core-promotion"
    )
    assert by_id["core_promotion_service"].path == (
        "/etc/systemd/system/obsidian-core-promotion.service"
    )
    assert by_id["core_promotion_timer"].path == (
        "/etc/systemd/system/obsidian-core-promotion.timer"
    )


def test_publisher_core_promotion_inventory_is_value_free(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "/etc/obsidian-core-promotion/promotion.env",
        SECRET,
    )
    _write(
        tmp_path,
        "/etc/obsidian-core-promotion/public-export.toml",
        SECRET,
    )
    _write(
        tmp_path,
        "/etc/obsidian-core-promotion/nextcloud.password",
        SECRET,
    )
    state = tmp_path / "var/lib/obsidian-core-promotion"
    state.mkdir(parents=True)

    report = inspect_role("publisher", root=tmp_path)
    encoded = json.dumps(report, sort_keys=True)

    assert SECRET not in encoded
    assert "PRIVATE-CREDENTIAL" not in encoded

    by_id = {item["logical_id"]: item for item in report["items"]}
    assert by_id["core_promotion_env"]["exists"] is True
    assert by_id["core_promotion_policy"]["exists"] is True
    assert by_id["core_promotion_password"]["exists"] is True
    assert by_id["core_promotion_state"]["file_type"] == "directory"


def test_github_writer_config_is_required_and_value_free(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "/etc/obsidian-github-writer/config.env",
        SECRET,
    )

    report = inspect_role("github-sync", root=tmp_path)
    encoded = json.dumps(report, sort_keys=True)
    by_id = {item["logical_id"]: item for item in report["items"]}

    assert SECRET not in encoded
    assert by_id["github_writer_config"]["exists"] is True
    assert by_id["github_writer_config"]["required"] is True
    assert by_id["github_writer_config"]["expected_fields"] == [
        "NEXTCLOUD_BASE_URL",
        "NEXTCLOUD_USERNAME",
    ]
