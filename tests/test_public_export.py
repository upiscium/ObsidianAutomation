from pathlib import Path

import pytest

from obsidian_automation.public_export import (
    ExportConfig,
    ExportError,
    apply_plan,
    build_plan,
    load_config,
)


def _config(*include: str, exclude: tuple[str, ...] = ()) -> ExportConfig:
    return ExportConfig(
        include=tuple(include),
        repository_owned=(".github/**", ".gitignore", "README.md", "LICENSE"),
        strict_missing=True,
        exclude=exclude,
    )


def test_exports_only_allowlisted_files_and_preserves_repository_owned(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    destination = tmp_path / "public"
    (vault / "98-System/nested").mkdir(parents=True)
    (vault / "98-System/nested/view.js").write_text("view")
    (vault / "11-Knowledge").mkdir()
    (vault / "11-Knowledge/private.md").write_text("private")
    (vault / "Dashboard.md").write_text("dashboard")

    (destination / ".github").mkdir(parents=True)
    (destination / ".github/workflow.yml").write_text("keep")
    (destination / "README.md").write_text("keep")

    changes = apply_plan(vault, destination, _config("98-System/**", "Dashboard.md"))

    assert (destination / "98-System/nested/view.js").read_text() == "view"
    assert (destination / "Dashboard.md").read_text() == "dashboard"
    assert not (destination / "11-Knowledge/private.md").exists()
    assert (destination / ".github/workflow.yml").read_text() == "keep"
    assert (destination / "README.md").read_text() == "keep"
    assert {item.action for item in changes} == {"ADD"}


def test_exclude_removes_health_marker_from_projection(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    destination = tmp_path / "public"
    (vault / "98-System/.rclone-bisync").mkdir(parents=True)
    (vault / "98-System/view.js").write_text("view")
    (vault / "98-System/.rclone-bisync/RCLONE_TEST").write_text("health")

    (destination / "98-System/.rclone-bisync").mkdir(parents=True)
    (destination / "98-System/.rclone-bisync/RCLONE_TEST").write_text("stale-public-health")

    changes = apply_plan(
        vault,
        destination,
        _config("98-System/**", exclude=("98-System/.rclone-bisync/**",)),
    )

    assert (destination / "98-System/view.js").read_text() == "view"
    assert not (destination / "98-System/.rclone-bisync/RCLONE_TEST").exists()
    assert ("DELETE", "98-System/.rclone-bisync/RCLONE_TEST") in {
        (item.action, item.path) for item in changes
    }
    assert all(
        item.path != "98-System/.rclone-bisync/RCLONE_TEST" or item.action == "DELETE"
        for item in changes
    )


def test_load_config_reads_exclude_patterns(tmp_path: Path) -> None:
    config_path = tmp_path / "public-export.toml"
    config_path.write_text(
        'version = 1\n'
        'include = ["98-System/**"]\n'
        'exclude = ["98-System/.rclone-bisync/**"]\n'
        'repository_owned = []\n'
    )

    config = load_config(config_path)

    assert config.exclude == ("98-System/.rclone-bisync/**",)


def test_rejects_path_traversal_pattern(tmp_path: Path) -> None:
    config = ExportConfig(
        include=("../secret.md",),
        repository_owned=(),
        strict_missing=True,
    )
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(ExportError, match="path traversal"):
        build_plan(vault, tmp_path / "dest", config)


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret")
    vault.mkdir()
    (vault / "98-System").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ExportError, match="symlink"):
        build_plan(vault, tmp_path / "dest", _config("98-System/**"))


def test_dry_plan_reports_add_update_delete_without_mutating(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    destination = tmp_path / "public"
    (vault / "98-System").mkdir(parents=True)
    (destination / "98-System").mkdir(parents=True)

    (vault / "98-System/new.md").write_text("new")
    (vault / "98-System/update.md").write_text("new-content")
    (destination / "98-System/update.md").write_text("old-content")
    (destination / "98-System/stale.md").write_text("stale")

    changes, _ = build_plan(vault, destination, _config("98-System/**"))

    assert {(item.action, item.path) for item in changes} == {
        ("ADD", "98-System/new.md"),
        ("UPDATE", "98-System/update.md"),
        ("DELETE", "98-System/stale.md"),
    }
    assert (destination / "98-System/update.md").read_text() == "old-content"
    assert (destination / "98-System/stale.md").exists()


def test_rejects_repository_owned_collision(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / ".github").mkdir(parents=True)
    (vault / ".github/workflow.yml").write_text("malicious")

    with pytest.raises(ExportError, match="repository-owned"):
        build_plan(vault, tmp_path / "dest", _config(".github/**"))


def test_missing_include_is_error_by_default(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(ExportError, match="matched no files"):
        build_plan(vault, tmp_path / "dest", _config("98-System/**"))


def test_removes_non_owned_files_that_are_no_longer_allowlisted(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    destination = tmp_path / "public"
    (vault / "98-System").mkdir(parents=True)
    (vault / "98-System/view.js").write_text("view")
    (destination / ".obsidian/themes/OldTheme").mkdir(parents=True)
    (destination / ".obsidian/themes/OldTheme/theme.css").write_text("old")

    changes = apply_plan(vault, destination, _config("98-System/**"))

    assert ("DELETE", ".obsidian/themes/OldTheme/theme.css") in {
        (item.action, item.path) for item in changes
    }
    assert not (destination / ".obsidian/themes/OldTheme/theme.css").exists()


def test_never_manages_git_metadata(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    destination = tmp_path / "public"
    (vault / "98-System").mkdir(parents=True)
    (vault / "98-System/view.js").write_text("view")
    (destination / ".git/objects").mkdir(parents=True)
    (destination / ".git/HEAD").write_text("ref: refs/heads/main")
    (destination / ".git/objects/object").write_text("object")

    changes = apply_plan(vault, destination, _config("98-System/**"))

    assert (destination / ".git/HEAD").exists()
    assert (destination / ".git/objects/object").exists()
    assert all(not item.path.startswith(".git") for item in changes)


def test_rejects_symlink_directory_in_destination(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    destination = tmp_path / "public"
    outside = tmp_path / "outside"
    (vault / "98-System").mkdir(parents=True)
    (vault / "98-System/view.js").write_text("view")
    destination.mkdir()
    outside.mkdir()
    (outside / "file.md").write_text("outside")
    (destination / "legacy").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ExportError, match="symlink"):
        build_plan(vault, destination, _config("98-System/**"))
