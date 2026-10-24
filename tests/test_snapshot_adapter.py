import os
import subprocess
from pathlib import Path

import pytest

from obsidian_automation.snapshot_adapter import Connections, SnapshotAdapter


def git(*args, cwd):
    return subprocess.run(["git", *map(str, args)], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def fixture(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    git("init", "--bare", remote, cwd=tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", seed, cwd=tmp_path)
    git("config", "user.name", "Tester", cwd=seed)
    git("config", "user.email", "test@local.invalid", cwd=seed)
    (seed / "note.md").write_text("old")
    git("add", "note.md", cwd=seed)
    git("commit", "-m", "initial", cwd=seed)
    baseline = git("rev-parse", "HEAD", cwd=seed)
    git("push", remote.as_uri(), "HEAD:refs/heads/main", cwd=seed)
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.md").write_text("old")
    policy = tmp_path / "policy.toml"
    policy.write_text("version = 1\nsettle_seconds = 0\nstability_attempts = 1\nexclude = []\nrepository_owned = []\n")
    config = tmp_path / "rclone.conf"
    config.write_text("fake only")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "rclone"
    stub.write_text("""#!/usr/bin/env python3
import os, shutil, sys
from pathlib import Path
action, remote, mirror = sys.argv[3:6]
if os.environ.get('FAIL_RCLONE') == action:
    sys.exit(1)
if action == 'sync' and os.environ.get('FAIL_SECOND_SYNC'):
    marker = Path(os.environ['FAIL_SECOND_SYNC'])
    if marker.exists():
        sys.exit(1)
    marker.touch()
source = Path(os.environ['FAKE_VAULT'])
if action == 'sync':
    shutil.copytree(source, mirror, dirs_exist_ok=True)
elif action == 'check':
    if sorted((x.name, x.read_bytes()) for x in source.iterdir()) != sorted((x.name, x.read_bytes()) for x in Path(mirror).iterdir()):
        sys.exit(1)
""")
    stub.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_VAULT", str(source))
    c = Connections(tmp_path / "state", "nextcloud:Vault", remote.as_uri(), "main", policy, config, True)
    return SnapshotAdapter(c), source, remote, baseline


def test_noop_changed_and_remote_verified(tmp_path, monkeypatch):
    adapter, source, remote, baseline = fixture(tmp_path, monkeypatch)
    assert adapter("a" * 32) == ("no_changes", "verify", baseline)
    (source / "note.md").write_text("new")
    status, stage, sha = adapter("b" * 32)
    assert (status, stage) == ("saved", "verify")
    assert sha != baseline
    assert git("--git-dir", remote, "rev-parse", "refs/heads/main", cwd=tmp_path) == sha
    assert (source / "note.md").read_text() == "new"


def test_sync_failure_empty_and_missing_branch_fail_closed(tmp_path, monkeypatch):
    adapter, source, remote, baseline = fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("FAIL_RCLONE", "check")
    assert adapter("c" * 32)[0] == "failed"
    assert not (tmp_path / "state/runs" / ("c" * 32) / "cache.git").exists()
    monkeypatch.delenv("FAIL_RCLONE")
    monkeypatch.setenv("FAIL_SECOND_SYNC", str(tmp_path / "second-sync-marker"))
    assert adapter("3" * 32) == ("failed", "sync", None)
    assert not (tmp_path / "state/runs" / ("3" * 32) / "cache.git").exists()
    monkeypatch.delenv("FAIL_SECOND_SYNC")
    (source / "note.md").unlink()
    assert adapter("d" * 32) == ("failed", "sync", None)
    assert git("--git-dir", remote, "rev-parse", "refs/heads/main", cwd=tmp_path) == baseline
    (source / "note.md").write_text("new")
    from dataclasses import replace
    other = SnapshotAdapter(replace(adapter.config, branch="nonexistent"))
    assert other("e" * 32) == ("failed", "fetch", None)


def test_push_race_keeps_local_commit(tmp_path, monkeypatch):
    adapter, source, remote, baseline = fixture(tmp_path, monkeypatch)
    (source / "note.md").write_text("new")
    import obsidian_automation.snapshot_adapter as module
    original = module._command

    def race(args, **kwargs):
        if args[:4] == ["git", "-C", str(tmp_path / "state/runs" / ("f" * 32) / "work"), "push"]:
            return False, ""
        return original(args, **kwargs)

    monkeypatch.setattr(module, "_command", race)
    assert adapter("f" * 32)[0] == "unconfirmed"
    work = tmp_path / "state/runs" / ("f" * 32) / "work"
    assert work.exists()
    assert git("rev-parse", "HEAD", cwd=work) != baseline
    assert git("--git-dir", remote, "rev-parse", "refs/heads/main", cwd=tmp_path) == baseline


def test_ambiguous_push_reply_reconciles_remote_sha(tmp_path, monkeypatch):
    adapter, source, remote, baseline = fixture(tmp_path, monkeypatch)
    (source / "note.md").write_text("new")
    import obsidian_automation.snapshot_adapter as module
    original = module._command

    def lost_reply(args, **kwargs):
        if args[0] == "git" and "push" in args:
            success, _ = original(args, **kwargs)
            assert success
            return False, ""  # remote applied push; transport reply was lost
        return original(args, **kwargs)

    monkeypatch.setattr(module, "_command", lost_reply)
    status, stage, sha = adapter("1" * 32)
    assert (status, stage) == ("saved", "verify")
    assert sha != baseline
    assert git("--git-dir", remote, "rev-parse", "refs/heads/main", cwd=tmp_path) == sha


def test_nonfastforward_preserves_evidence(tmp_path, monkeypatch):
    adapter, source, remote, baseline = fixture(tmp_path, monkeypatch)
    (source / "note.md").write_text("new")
    import obsidian_automation.snapshot_adapter as module
    original = module._command
    fork = tmp_path / "fork"
    git("clone", remote.as_uri(), fork, cwd=tmp_path)
    git("-C", fork, "checkout", "main", cwd=tmp_path)
    git("-C", fork, "config", "user.name", "Racer", cwd=tmp_path)
    git("-C", fork, "config", "user.email", "racer@local.invalid", cwd=tmp_path)

    def concurrent(args, **kwargs):
        if args[0] == "git" and "push" in args and "-C" in args:
            (fork / "other.md").write_text("concurrent")
            git("add", "other.md", cwd=fork)
            git("commit", "-m", "concurrent", cwd=fork)
            git("push", remote.as_uri(), "HEAD:refs/heads/main", cwd=fork)
        return original(args, **kwargs)

    monkeypatch.setattr(module, "_command", concurrent)
    assert adapter("2" * 32)[0] == "unconfirmed"
    work = tmp_path / "state/runs" / ("2" * 32) / "work"
    assert work.exists()
    assert git("rev-parse", "HEAD", cwd=work) != baseline
    assert git("--git-dir", remote, "rev-parse", "refs/heads/main", cwd=tmp_path) != baseline


@pytest.mark.parametrize("change_tracked", [False, True], ids=["ignored-only", "ignored-and-tracked"])
def test_managed_ignored_new_file_cannot_be_reported_backed_up(tmp_path, monkeypatch, change_tracked):
    adapter, source, remote, _ = fixture(tmp_path, monkeypatch)
    seed = tmp_path / "seed"
    (seed / ".gitignore").write_bytes(b"ignored.tmp\n")
    git("add", ".gitignore", cwd=seed)
    git("commit", "-m", "retain repository ignore", cwd=seed)
    baseline = git("rev-parse", "HEAD", cwd=seed)
    git("push", remote.as_uri(), "HEAD:refs/heads/main", cwd=seed)
    adapter.config.policy.write_text(
        "version = 1\nsettle_seconds = 0\nstability_attempts = 1\n"
        "exclude = []\nrepository_owned = ['.gitignore']\n"
    )
    (source / "ignored.tmp").write_bytes(b"must be archived despite Git ignore\n")
    if change_tracked:
        (source / "note.md").write_bytes(b"tracked edit\n")
    source_before = {file.name: file.read_bytes() for file in source.iterdir()}

    assert adapter(("5" if change_tracked else "4") * 32) == ("failed", "snapshot", None)
    assert git("--git-dir", remote, "rev-parse", "refs/heads/main", cwd=tmp_path) == baseline
    assert {file.name: file.read_bytes() for file in source.iterdir()} == source_before
