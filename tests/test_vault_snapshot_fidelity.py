from __future__ import annotations

import subprocess
from pathlib import Path

from obsidian_automation.vault_snapshot import snapshot_vault


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


def test_snapshot_preserves_trailing_whitespace_in_canonical_vault(tmp_path: Path) -> None:
    source = tmp_path / "live"
    source.mkdir()
    source_note = source / "note.md"
    source_note.write_text("---\nproject: \n---\nbody\n")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / ".gitignore").write_text("ignored.tmp\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "initial")

    config = tmp_path / "snapshot.toml"
    config.write_text(
        '''version = 1
settle_seconds = 0
stability_attempts = 1
exclude = []
repository_owned = [".gitignore"]
'''
    )

    result = snapshot_vault(
        source=source,
        destination=repo,
        config_path=config,
        commit_message="Snapshot ObsidianVault",
        author_name="Obsidian Snapshot",
        author_email="snapshot@example.invalid",
    )

    assert result.changed is True
    assert result.commit_sha is not None
    assert (repo / "note.md").read_bytes() == source_note.read_bytes()
    assert _git(repo, "status", "--porcelain") == ""
    assert "note.md" in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
