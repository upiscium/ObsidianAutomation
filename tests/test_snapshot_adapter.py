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
    print('SECRET_TOKEN_CANARY private-vault/secret-note', file=sys.stderr)
    sys.exit(int(os.environ.get('FAIL_RCLONE_CODE', '1')))
if action == 'sync' and os.environ.get('TRANSIENT_SYNC_MARKER'):
    marker = Path(os.environ['TRANSIENT_SYNC_MARKER'])
    if not marker.exists():
        marker.touch()
        print('SECRET_TOKEN_CANARY private-vault/secret-note', file=sys.stderr)
        sys.exit(5)
if action == 'sync' and os.environ.get('FAIL_SECOND_SYNC'):
    marker = Path(os.environ['FAIL_SECOND_SYNC'])
    if marker.exists():
        sys.exit(1)
    marker.touch()
source = Path(os.environ['FAKE_VAULT'])
if action == 'sync':
    shutil.copytree(source, mirror, dirs_exist_ok=True)
elif action == 'check':
    if os.environ.get('SOURCE_CHURN_ONCE_MARKER'):
        marker = Path(os.environ['SOURCE_CHURN_ONCE_MARKER'])
        if not marker.exists():
            marker.touch()
            (source / 'note.md').write_text('source updated during check')
            (source / 'new-note.md').write_text('new during check')
    if sorted((x.name, x.read_bytes()) for x in source.iterdir()) != sorted((x.name, x.read_bytes()) for x in Path(mirror).iterdir()):
        sys.exit(1)
""")
    stub.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_VAULT", str(source))
    # Keep synthetic fail/retry fixtures fast; production uses 2/4s delays.
    import obsidian_automation.snapshot_adapter as module
    monkeypatch.setattr(module, "_RCLONE_RETRY_DELAYS_SECONDS", (0, 0))
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


def test_rclone_only_uses_longer_bounded_timeout(tmp_path, monkeypatch):
    adapter, _, remote, baseline = fixture(tmp_path, monkeypatch)
    import obsidian_automation.snapshot_adapter as module

    real_command = module._command
    observed = []

    def capture(args, **kwargs):
        if args[0] == "rclone":
            observed.append((args[3], kwargs.get("timeout")))
        else:
            # Git operations retain the independent, unchanged default.
            assert kwargs.get("timeout", 120) == 120
        return real_command(args, **kwargs)

    monkeypatch.setattr(module, "_command", capture)
    assert adapter("9" * 32) == ("no_changes", "verify", baseline)
    assert observed == [
        ("sync", module._RCLONE_IO_TIMEOUT_SECONDS),
        ("sync", module._RCLONE_IO_TIMEOUT_SECONDS),
        ("check", module._RCLONE_IO_TIMEOUT_SECONDS),
    ]
    assert module._RCLONE_IO_TIMEOUT_SECONDS == 600
    assert git("--git-dir", remote, "rev-parse", "refs/heads/main", cwd=tmp_path) == baseline



def _sync_receipt(adapter: SnapshotAdapter, run_id: str) -> dict:
    import json
    root = adapter.config.state / "runs" / run_id
    receipt = root / "rclone-receipt.json"
    assert receipt.is_file()
    assert receipt.stat().st_mode & 0o777 == 0o600
    payload = receipt.read_text()
    assert "SECRET_TOKEN_CANARY" not in payload
    assert "private-vault" not in payload
    assert "nextcloud:" not in payload
    assert str(root) not in payload
    data = json.loads(payload)
    assert set(data) == {"version", "attempts"} and data["version"] == 1
    assert 1 <= len(data["attempts"]) <= 9
    assert all(set(item) == {"phase", "attempt", "reason", "exit_code"}
               for item in data["attempts"])
    return data


def test_transient_rclone_error_retries_from_first_sync_and_saves(tmp_path, monkeypatch):
    adapter, source, remote, baseline = fixture(tmp_path, monkeypatch)
    marker = tmp_path / "temporary-http-error"
    monkeypatch.setenv("TRANSIENT_SYNC_MARKER", str(marker))
    source_before = (source / "note.md").read_bytes()
    run_id = "6" * 32

    assert adapter(run_id) == ("no_changes", "verify", baseline)
    receipt = _sync_receipt(adapter, run_id)
    assert [(r["phase"], r["attempt"], r["reason"]) for r in receipt["attempts"]] == [
        ("sync_initial", 1, "temporary_error"),
        ("sync_initial", 2, "ok"),
        ("sync_settled", 2, "ok"),
        ("check", 2, "ok"),
    ]
    assert receipt["attempts"][0]["exit_code"] == 5
    assert marker.is_file()
    assert (source / "note.md").read_bytes() == source_before
    assert git("--git-dir", remote, "rev-parse", "refs/heads/main", cwd=tmp_path) == baseline


def test_source_churn_during_check_restarts_entire_sync_cycle(tmp_path, monkeypatch):
    adapter, source, remote, baseline = fixture(tmp_path, monkeypatch)
    marker = tmp_path / "source-moved-during-check"
    monkeypatch.setenv("SOURCE_CHURN_ONCE_MARKER", str(marker))
    run_id = "7" * 32

    status, stage, commit = adapter(run_id)
    assert (status, stage) == ("saved", "verify")
    assert commit != baseline and marker.is_file()
    assert git("--git-dir", remote, "rev-parse", "refs/heads/main", cwd=tmp_path) == commit
    assert git("--git-dir", remote, "show", "refs/heads/main:new-note.md",
               cwd=tmp_path) == "new during check"
    receipt = _sync_receipt(adapter, run_id)
    attempts = receipt["attempts"]
    assert [(x["phase"], x["attempt"], x["reason"]) for x in attempts] == [
        ("sync_initial", 1, "ok"), ("sync_settled", 1, "ok"),
        ("check", 1, "code_1_ambiguous"), ("sync_initial", 2, "ok"),
        ("sync_settled", 2, "ok"), ("check", 2, "ok"),
    ]


def test_fatal_rclone_exit_is_not_retried_or_pushed(tmp_path, monkeypatch):
    adapter, source, remote, baseline = fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("FAIL_RCLONE", "sync")
    monkeypatch.setenv("FAIL_RCLONE_CODE", "7")
    run_id = "a" * 32
    assert adapter(run_id) == ("failed", "sync", None)
    receipt = _sync_receipt(adapter, run_id)
    assert receipt["attempts"] == [
        {"phase": "sync_initial", "attempt": 1, "reason": "fatal_error", "exit_code": 7}
    ]
    assert not (adapter.config.state / "runs" / run_id / "cache.git").exists()
    assert git("--git-dir", remote, "rev-parse", "refs/heads/main", cwd=tmp_path) == baseline


@pytest.mark.parametrize("exit_code", [1, 2])
def test_bounded_ambiguous_rclone_errors_fail_without_git(tmp_path, monkeypatch, exit_code):
    adapter, source, remote, baseline = fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("FAIL_RCLONE", "sync")
    monkeypatch.setenv("FAIL_RCLONE_CODE", str(exit_code))
    run_id = "b" * 32
    assert adapter(run_id) == ("failed", "sync", None)
    receipt = _sync_receipt(adapter, run_id)
    assert [x["attempt"] for x in receipt["attempts"]] == [1, 2, 3]
    assert all(x["reason"] == f"code_{exit_code}_ambiguous" and x["exit_code"] == exit_code
               for x in receipt["attempts"])
    assert not (adapter.config.state / "runs" / run_id / "cache.git").exists()
    assert git("--git-dir", remote, "rev-parse", "refs/heads/main", cwd=tmp_path) == baseline


def test_timeout_and_exhausted_budget_fail_closed(tmp_path, monkeypatch):
    adapter, source, remote, baseline = fixture(tmp_path, monkeypatch)
    import obsidian_automation.snapshot_adapter as module
    original = module.subprocess.run

    def expire_only_rclone(args, **kwargs):
        if args[0] == "rclone":
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
        return original(args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", expire_only_rclone)
    run_id = "c" * 32
    assert adapter(run_id) == ("failed", "sync", None)
    assert _sync_receipt(adapter, run_id)["attempts"] == [
        {"phase": "sync_initial", "attempt": 1, "reason": "timeout", "exit_code": None}
    ]
    monkeypatch.setattr(module, "_RCLONE_TOTAL_BUDGET_SECONDS", 0)
    run_id = "d" * 32
    assert adapter(run_id) == ("failed", "sync", None)
    assert _sync_receipt(adapter, run_id)["attempts"] == [
        {"phase": "sync_initial", "attempt": 1, "reason": "budget_exhausted", "exit_code": None}
    ]
    assert git("--git-dir", remote, "rev-parse", "refs/heads/main", cwd=tmp_path) == baseline


def test_rclone_retries_never_expand_git_operation_budget(tmp_path, monkeypatch):
    adapter, source, remote, baseline = fixture(tmp_path, monkeypatch)
    import obsidian_automation.snapshot_adapter as module
    original = module._command
    git_timeouts = []
    rclone_timeouts = []

    def capture(args, **kwargs):
        if args[0] == "git":
            assert kwargs.get("diagnostics") is None
            git_timeouts.append(kwargs.get("timeout", 120))
        elif args[0] == "rclone":
            assert kwargs.get("diagnostics") is not None
            rclone_timeouts.append(kwargs.get("timeout"))
        return original(args, **kwargs)

    monkeypatch.setattr(module, "_command", capture)
    assert adapter("e" * 32) == ("no_changes", "verify", baseline)
    assert len(rclone_timeouts) == 3
    assert all(0 < timeout <= 600 for timeout in rclone_timeouts)
    assert git_timeouts and all(timeout == 120 for timeout in git_timeouts)
    assert module._RCLONE_MAX_ATTEMPTS == 3
    assert module._RCLONE_TOTAL_BUDGET_SECONDS == 900
