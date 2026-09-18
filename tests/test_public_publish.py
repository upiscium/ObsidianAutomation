from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import obsidian_automation.public_publish as public_publish
from obsidian_automation.public_publish import (
    PROJECTION_COMMIT_MARKER,
    PROJECTION_COMMIT_SUBJECT,
    PublishError,
    publish_projection,
)


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _init_repository(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.invalid")


def _commit_all(path: Path, message: str = PROJECTION_COMMIT_SUBJECT) -> str:
    _git(path, "add", "-A")
    _git(path, "commit", "-m", message)
    return _git(path, "rev-parse", "HEAD")


def _write_config(path: Path) -> Path:
    config = path / "public-export.toml"
    config.write_text(
        """version = 1
strict_missing = true
include = ["98-System/**"]
repository_owned = [".git", ".git/**", ".github/**", ".gitignore", "README.md", "LICENSE"]
"""
    )
    return config



def _write_config_with_appearance(path: Path) -> Path:
    config = path / "public-export-with-appearance.toml"
    config.write_text(
        """version = 1
strict_missing = true
include = ["98-System/**", ".obsidian/appearance.json"]
repository_owned = [".git", ".git/**", ".github/**", ".gitignore", "README.md", "LICENSE"]
"""
    )
    return config


def _appearance(
    *,
    managed: list[str],
    private: list[str] | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "theme": "obsidian",
        "cssTheme": "Tokyo Night",
        "enabledCssSnippets": [*managed, *(private or [])],
    }
    value.update(extra or {})
    return value


def _write_json(path: Path, value: dict[str, object], *, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    else:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def test_noop_projection_creates_no_commit(tmp_path: Path) -> None:
    source = tmp_path / "vault"
    destination = tmp_path / "core"
    (source / "98-System").mkdir(parents=True)
    (source / "98-System/view.js").write_text("same\n")

    _init_repository(destination)
    (destination / "98-System").mkdir(parents=True)
    (destination / "98-System/view.js").write_text("same\n")
    initial = _commit_all(destination)

    result = publish_projection(
        source=source,
        destination=destination,
        config_path=_write_config(tmp_path),
        commit_message="projection",
        author_name="Automation",
        author_email="automation@example.invalid",
        validate_core=False,
    )

    assert result.changed is False
    assert result.commit_sha is None
    assert _git(destination, "rev-parse", "HEAD") == initial
    assert _git(destination, "status", "--porcelain") == ""


def test_validation_failure_does_not_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "vault"
    destination = tmp_path / "core"
    (source / "98-System").mkdir(parents=True)
    (source / "98-System/view.js").write_text("new\n")

    _init_repository(destination)
    (destination / "98-System").mkdir(parents=True)
    (destination / "98-System/view.js").write_text("old\n")
    initial = _commit_all(destination)

    def fail_validation(_: Path) -> None:
        raise PublishError("validation failed")

    monkeypatch.setattr(public_publish, "_validate_obsidian_core", fail_validation)

    with pytest.raises(PublishError, match="validation failed"):
        publish_projection(
            source=source,
            destination=destination,
            config_path=_write_config(tmp_path),
            commit_message="projection",
            author_name="Automation",
            author_email="automation@example.invalid",
        )

    assert _git(destination, "rev-parse", "HEAD") == initial
    assert _git(destination, "status", "--porcelain") != ""


def test_valid_projection_creates_single_local_commit(tmp_path: Path) -> None:
    source = tmp_path / "vault"
    destination = tmp_path / "core"
    (source / "98-System").mkdir(parents=True)
    (source / "98-System/view.js").write_text("new\n")

    _init_repository(destination)
    (destination / "98-System").mkdir(parents=True)
    (destination / "98-System/view.js").write_text("old\n")
    initial = _commit_all(destination)

    result = publish_projection(
        source=source,
        destination=destination,
        config_path=_write_config(tmp_path),
        commit_message="projection",
        author_name="Automation",
        author_email="automation@example.invalid",
        validate_core=False,
    )

    assert result.changed is True
    assert result.commit_sha == _git(destination, "rev-parse", "HEAD")
    assert _git(destination, "rev-list", "--count", f"{initial}..HEAD") == "1"
    assert (destination / "98-System/view.js").read_text() == "new\n"
    assert PROJECTION_COMMIT_MARKER in _git(destination, "log", "-1", "--format=%B")
    assert _git(destination, "status", "--porcelain") == ""


def test_pending_core_managed_change_blocks_projection_before_mutation(tmp_path: Path) -> None:
    source = tmp_path / "vault"
    destination = tmp_path / "core"
    (source / "98-System").mkdir(parents=True)
    (source / "98-System/view.js").write_text("vault-old\n")

    _init_repository(destination)
    (destination / "98-System").mkdir(parents=True)
    target = destination / "98-System/view.js"
    target.write_text("vault-old\n")
    _commit_all(destination)
    target.write_text("reviewed-core-new\n")
    manual = _commit_all(destination, "Reviewed system change (#1)")

    with pytest.raises(PublishError, match="have not yet converged"):
        publish_projection(
            source=source,
            destination=destination,
            config_path=_write_config(tmp_path),
            commit_message=PROJECTION_COMMIT_SUBJECT,
            author_name="Automation",
            author_email="automation@example.invalid",
            validate_core=False,
        )

    assert _git(destination, "rev-parse", "HEAD") == manual
    assert target.read_text() == "reviewed-core-new\n"
    assert _git(destination, "status", "--porcelain") == ""


def test_promoted_core_change_creates_empty_projection_acknowledgement(tmp_path: Path) -> None:
    source = tmp_path / "vault"
    destination = tmp_path / "core"
    (source / "98-System").mkdir(parents=True)

    _init_repository(destination)
    (destination / "98-System").mkdir(parents=True)
    target = destination / "98-System/view.js"
    target.write_text("old\n")
    _commit_all(destination)
    target.write_text("reviewed-new\n")
    manual = _commit_all(destination, "Reviewed system change (#2)")

    # Promotion has already converged the Live Vault to the reviewed Core bytes.
    (source / "98-System/view.js").write_text("reviewed-new\n")

    result = publish_projection(
        source=source,
        destination=destination,
        config_path=_write_config(tmp_path),
        commit_message=PROJECTION_COMMIT_SUBJECT,
        author_name="Automation",
        author_email="automation@example.invalid",
        validate_core=False,
    )

    assert result.changed is True
    assert result.changes == ()
    assert result.commit_sha == _git(destination, "rev-parse", "HEAD")
    assert _git(destination, "rev-list", "--count", f"{manual}..HEAD") == "1"
    assert _git(destination, "diff", "--name-only", "HEAD^", "HEAD") == ""
    assert PROJECTION_COMMIT_MARKER in _git(destination, "log", "-1", "--format=%B")

    # Once acknowledged, an ordinary later Vault edit is allowed to publish.
    (source / "98-System/view.js").write_text("later-vault-change\n")
    later = publish_projection(
        source=source,
        destination=destination,
        config_path=_write_config(tmp_path),
        commit_message=PROJECTION_COMMIT_SUBJECT,
        author_name="Automation",
        author_email="automation@example.invalid",
        validate_core=False,
    )
    assert later.changed is True
    assert (destination / "98-System/view.js").read_text() == "later-vault-change\n"


def test_repository_owned_core_change_does_not_block_vault_projection(tmp_path: Path) -> None:
    source = tmp_path / "vault"
    destination = tmp_path / "core"
    (source / "98-System").mkdir(parents=True)
    (source / "98-System/view.js").write_text("vault-new\n")

    _init_repository(destination)
    (destination / "98-System").mkdir(parents=True)
    (destination / "98-System/view.js").write_text("old\n")
    (destination / "README.md").write_text("old readme\n")
    _commit_all(destination)
    (destination / "README.md").write_text("reviewed readme\n")
    _commit_all(destination, "Update repository docs (#3)")

    result = publish_projection(
        source=source,
        destination=destination,
        config_path=_write_config(tmp_path),
        commit_message=PROJECTION_COMMIT_SUBJECT,
        author_name="Automation",
        author_email="automation@example.invalid",
        validate_core=False,
    )

    assert result.changed is True
    assert (destination / "98-System/view.js").read_text() == "vault-new\n"
    assert (destination / "README.md").read_text() == "reviewed readme\n"


def test_semantically_converged_appearance_allows_empty_acknowledgement(tmp_path: Path) -> None:
    source = tmp_path / "vault"
    destination = tmp_path / "core"
    (source / "98-System").mkdir(parents=True)
    (source / "98-System/view.js").write_text("same\n")

    _init_repository(destination)
    (destination / "98-System").mkdir(parents=True)
    (destination / "98-System/view.js").write_text("same\n")
    core_appearance = destination / ".obsidian/appearance.json"
    _write_json(core_appearance, _appearance(managed=["obsidian-core"]))
    _commit_all(destination)

    # Reviewed Core change enables the mobile managed snippet.
    _write_json(
        core_appearance,
        _appearance(managed=["obsidian-core", "obsidian-core-mobile"]),
    )
    manual = _commit_all(destination, "Reviewed appearance change (#84)")

    # Promotion converged managed state, but Live preserved a private snippet,
    # an unknown key and different JSON formatting.
    _write_json(
        source / ".obsidian/appearance.json",
        _appearance(
            managed=["obsidian-core", "obsidian-core-mobile"],
            private=["private-local-snippet"],
            extra={"accentColor": "#123456"},
        ),
        compact=True,
    )

    result = publish_projection(
        source=source,
        destination=destination,
        config_path=_write_config_with_appearance(tmp_path),
        commit_message=PROJECTION_COMMIT_SUBJECT,
        author_name="Automation",
        author_email="automation@example.invalid",
        validate_core=False,
    )

    assert result.changed is True
    assert result.changes == ()
    assert _git(destination, "rev-list", "--count", f"{manual}..HEAD") == "1"
    assert _git(destination, "diff", "--name-only", "HEAD^", "HEAD") == ""
    assert "private-local-snippet" not in core_appearance.read_text()
    assert "accentColor" not in core_appearance.read_text()


def test_publication_writes_only_managed_appearance_projection(tmp_path: Path) -> None:
    source = tmp_path / "vault"
    destination = tmp_path / "core"
    (source / "98-System").mkdir(parents=True)
    (source / "98-System/view.js").write_text("same\n")

    _init_repository(destination)
    (destination / "98-System").mkdir(parents=True)
    (destination / "98-System/view.js").write_text("same\n")
    core_appearance = destination / ".obsidian/appearance.json"
    _write_json(core_appearance, _appearance(managed=["obsidian-core"]))
    _commit_all(destination)

    _write_json(
        source / ".obsidian/appearance.json",
        _appearance(
            managed=["obsidian-core", "obsidian-core-mobile"],
            private=["private-local-snippet"],
            extra={"translucency": True},
        ),
    )

    result = publish_projection(
        source=source,
        destination=destination,
        config_path=_write_config_with_appearance(tmp_path),
        commit_message=PROJECTION_COMMIT_SUBJECT,
        author_name="Automation",
        author_email="automation@example.invalid",
        validate_core=False,
    )

    assert result.changed is True
    assert result.changes == (public_publish.Change("UPDATE", ".obsidian/appearance.json"),)
    published = json.loads(core_appearance.read_text())
    assert published == {
        "theme": "obsidian",
        "cssTheme": "Tokyo Night",
        "enabledCssSnippets": ["obsidian-core", "obsidian-core-mobile"],
    }
    assert set(published) == {"theme", "cssTheme", "enabledCssSnippets"}


def test_malformed_live_appearance_fails_before_repository_mutation(tmp_path: Path) -> None:
    source = tmp_path / "vault"
    destination = tmp_path / "core"
    (source / "98-System").mkdir(parents=True)
    (source / "98-System/view.js").write_text("vault-new\n")
    (source / ".obsidian").mkdir(parents=True)
    (source / ".obsidian/appearance.json").write_text("{not-json")

    _init_repository(destination)
    (destination / "98-System").mkdir(parents=True)
    (destination / "98-System/view.js").write_text("old\n")
    _write_json(
        destination / ".obsidian/appearance.json",
        _appearance(managed=["obsidian-core"]),
    )
    initial = _commit_all(destination)

    with pytest.raises(PublishError, match="Live Vault appearance"):
        publish_projection(
            source=source,
            destination=destination,
            config_path=_write_config_with_appearance(tmp_path),
            commit_message=PROJECTION_COMMIT_SUBJECT,
            author_name="Automation",
            author_email="automation@example.invalid",
            validate_core=False,
        )

    assert _git(destination, "rev-parse", "HEAD") == initial
    assert _git(destination, "status", "--porcelain") == ""
    assert (destination / "98-System/view.js").read_text() == "old\n"


def test_duplicate_live_appearance_snippet_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "vault"
    destination = tmp_path / "core"
    (source / "98-System").mkdir(parents=True)
    (source / "98-System/view.js").write_text("same\n")

    _init_repository(destination)
    (destination / "98-System").mkdir(parents=True)
    (destination / "98-System/view.js").write_text("same\n")
    _write_json(
        destination / ".obsidian/appearance.json",
        _appearance(managed=["obsidian-core"]),
    )
    initial = _commit_all(destination)

    _write_json(
        source / ".obsidian/appearance.json",
        _appearance(managed=["obsidian-core", "obsidian-core"]),
    )

    with pytest.raises(PublishError, match="must not contain duplicates"):
        publish_projection(
            source=source,
            destination=destination,
            config_path=_write_config_with_appearance(tmp_path),
            commit_message=PROJECTION_COMMIT_SUBJECT,
            author_name="Automation",
            author_email="automation@example.invalid",
            validate_core=False,
        )

    assert _git(destination, "rev-parse", "HEAD") == initial
    assert _git(destination, "status", "--porcelain") == ""
