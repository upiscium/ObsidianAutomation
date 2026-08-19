from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from obsidian_automation import vault_snapshot
from obsidian_automation.vault_snapshot import ManifestEntry, SnapshotError, snapshot_vault


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")


def _commit_all(repo: Path, message: str = "initial") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _write_config(path: Path, *, settle_seconds: float = 0, stability_attempts: int = 1) -> None:
    path.write_text(
        f'''version = 1
settle_seconds = {settle_seconds}
stability_attempts = {stability_attempts}

exclude = [
  ".obsidian/workspace*",
  ".obsidian/themes/**",
  ".trash/**",
]

repository_owned = [
  ".gitea/**",
  ".gitignore",
]
'''
    )


def _snapshot(source: Path, repo: Path, config: Path, *, dry_run: bool = False):
    return snapshot_vault(
        source=source,
        destination=repo,
        config_path=config,
        commit_message="Snapshot ObsidianVault",
        author_name="Obsidian Snapshot",
        author_email="snapshot@example.invalid",
        dry_run=dry_run,
    )


def test_snapshot_mirrors_add_update_delete_and_preserves_repository_owned(tmp_path: Path) -> None:
    source = tmp_path / "live"
    source.mkdir()
    (source / "keep.md").write_text("new\n")
    (source / "added.md").write_text("added\n")
    (source / ".gitignore").write_text("source-owned-copy\n")
    (source / ".gitea/workflows").mkdir(parents=True)
    (source / ".gitea/workflows/publish.yml").write_text("source workflow\n")
    (source / ".obsidian/themes").mkdir(parents=True)
    (source / ".obsidian/themes/theme.css").write_text("live transient theme\n")

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "keep.md").write_text("old\n")
    (repo / "deleted.md").write_text("delete me\n")
    (repo / ".gitignore").write_text("repo policy\n")
    (repo / ".gitea/workflows").mkdir(parents=True)
    (repo / ".gitea/workflows/publish.yml").write_text("repo workflow\n")
    (repo / ".obsidian/themes").mkdir(parents=True)
    (repo / ".obsidian/themes/theme.css").write_text("tracked transient theme\n")
    before = _commit_all(repo)

    config = tmp_path / "snapshot.toml"
    _write_config(config)
    result = _snapshot(source, repo, config)

    assert result.changed is True
    assert result.commit_sha != before
    assert (repo / "keep.md").read_text() == "new\n"
    assert (repo / "added.md").read_text() == "added\n"
    assert not (repo / "deleted.md").exists()
    assert not (repo / ".obsidian/themes/theme.css").exists()
    assert (repo / ".gitignore").read_text() == "repo policy\n"
    assert (repo / ".gitea/workflows/publish.yml").read_text() == "repo workflow\n"

    actions = {(change.action, change.path) for change in result.changes}
    assert ("ADD", "added.md") in actions
    assert ("UPDATE", "keep.md") in actions
    assert ("DELETE", "deleted.md") in actions
    assert ("DELETE", ".obsidian/themes/theme.css") in actions

    message = _git(repo, "log", "-1", "--pretty=%B")
    assert f"Source-Manifest-SHA256: {result.manifest_sha256}" in message
    assert _git(repo, "status", "--porcelain") == ""


def test_noop_and_dry_run_create_no_commit(tmp_path: Path) -> None:
    source = tmp_path / "live"
    source.mkdir()
    (source / "note.md").write_text("same\n")

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "note.md").write_text("same\n")
    before = _commit_all(repo)

    config = tmp_path / "snapshot.toml"
    _write_config(config)

    dry = _snapshot(source, repo, config, dry_run=True)
    assert dry.changed is False
    assert dry.commit_sha is None
    assert _git(repo, "rev-parse", "HEAD") == before

    result = _snapshot(source, repo, config)
    assert result.changed is False
    assert result.commit_sha is None
    assert _git(repo, "rev-parse", "HEAD") == before
    assert _git(repo, "status", "--porcelain") == ""


def test_source_change_during_staging_leaves_destination_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "live"
    source.mkdir()
    live_file = source / "note.md"
    live_file.write_text("stable\n")

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "note.md").write_text("old\n")
    before = _commit_all(repo)

    config = tmp_path / "snapshot.toml"
    _write_config(config)

    real_copy2 = vault_snapshot.shutil.copy2
    mutated = False

    def copy_then_mutate(src: Path, dst: Path):
        nonlocal mutated
        result = real_copy2(src, dst)
        if not mutated:
            live_file.write_text("changed during copy\n")
            mutated = True
        return result

    monkeypatch.setattr(vault_snapshot.shutil, "copy2", copy_then_mutate)

    with pytest.raises(SnapshotError, match="changed while staging"):
        _snapshot(source, repo, config)

    assert (repo / "note.md").read_text() == "old\n"
    assert _git(repo, "rev-parse", "HEAD") == before
    assert _git(repo, "status", "--porcelain") == ""


def test_unstable_manifest_exhausts_attempts_without_touching_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "live"
    source.mkdir()
    (source / "note.md").write_text("live\n")

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "note.md").write_text("old\n")
    before = _commit_all(repo)

    config = tmp_path / "snapshot.toml"
    _write_config(config, stability_attempts=2)

    manifests = iter(
        [
            {"note.md": ManifestEntry("a" * 64, 1)},
            {"note.md": ManifestEntry("b" * 64, 1)},
            {"note.md": ManifestEntry("c" * 64, 1)},
        ]
    )
    monkeypatch.setattr(vault_snapshot, "build_manifest", lambda *_: next(manifests))

    with pytest.raises(SnapshotError, match="did not become stable"):
        _snapshot(source, repo, config)

    assert _git(repo, "rev-parse", "HEAD") == before
    assert _git(repo, "status", "--porcelain") == ""


def test_source_symlink_and_special_file_are_rejected(tmp_path: Path) -> None:
    config = tmp_path / "snapshot.toml"
    _write_config(config)

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "seed.md").write_text("seed\n")
    _commit_all(repo)

    source = tmp_path / "live-symlink"
    source.mkdir()
    target = source / "target.md"
    target.write_text("target\n")
    (source / "link.md").symlink_to(target)
    with pytest.raises(SnapshotError, match="symlink"):
        _snapshot(source, repo, config)

    fifo_source = tmp_path / "live-fifo"
    fifo_source.mkdir()
    os.mkfifo(fifo_source / "pipe")
    with pytest.raises(SnapshotError, match="special file"):
        _snapshot(fifo_source, repo, config)


def test_source_and_destination_must_not_be_nested(tmp_path: Path) -> None:
    source = tmp_path / "live"
    source.mkdir()
    repo = source / "repo"
    _init_repo(repo)
    (repo / "seed.md").write_text("seed\n")
    _commit_all(repo)
    config = tmp_path / "snapshot.toml"
    _write_config(config)

    with pytest.raises(SnapshotError, match="separate, non-nested roots"):
        _snapshot(source, repo, config)
