from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import obsidian_automation.public_publish as public_publish
from obsidian_automation.public_publish import PublishError, publish_projection


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


def _commit_all(path: Path, message: str = "initial") -> str:
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
    assert _git(destination, "status", "--porcelain") == ""
