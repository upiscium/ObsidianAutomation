from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import obsidian_automation.vault_mirror as mirror


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    ai_root = tmp_path / "state"
    (ai_root / "24-Locks" / "read-view").mkdir(parents=True)
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    config = tmp_path / "rclone.conf"
    config.write_text("[nextcloud-ai]\ntype = webdav\n", encoding="utf-8")
    filters = tmp_path / "vault-pull.filters"
    filters.write_text("+ **\n", encoding="utf-8")
    return ai_root, vault_root, config, filters


def _assert_bounded_timeout(timeout: float) -> None:
    assert 0 < timeout <= mirror._RCLONE_COMMAND_TIMEOUT_SECONDS


def test_pull_mirror_uses_fixed_sync_and_download_check_commands(tmp_path: Path) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)
    captured = []

    def fake_runner(command, *, check, timeout):
        _assert_bounded_timeout(timeout)
        captured.append((command, check, timeout))
        return SimpleNamespace(returncode=0)

    result = mirror.refresh_pull_only_mirror(
        ai_root,
        vault_root,
        remote="nextcloud-ai:ObsidianVault",
        rclone_config=config,
        filter_file=filters,
        runner=fake_runner,
    )

    assert result.remote == "nextcloud-ai:ObsidianVault"
    assert result.vault_root == vault_root.absolute()
    assert [command for command, _check, _timeout in captured] == [
        [
            "rclone",
            "sync",
            "nextcloud-ai:ObsidianVault",
            str(vault_root.absolute()),
            "--config",
            str(config.absolute()),
            "--filter-from",
            str(filters.absolute()),
            "--ignore-times",
            "--delete-after",
            "--delete-excluded",
        ],
        [
            "rclone",
            "check",
            "nextcloud-ai:ObsidianVault",
            str(vault_root.absolute()),
            "--config",
            str(config.absolute()),
            "--filter-from",
            str(filters.absolute()),
            "--download",
        ],
    ]
    assert all(check is False for _command, check, _timeout in captured)
    assert all(
        0 < timeout <= mirror._RCLONE_COMMAND_TIMEOUT_SECONDS
        for _command, _check, timeout in captured
    )
    assert (ai_root / "24-Locks" / "canonical-io.lock").is_file()
    assert (ai_root / "24-Locks" / "read-view" / "mirror-read.lock").is_file()


def test_pull_mirror_holds_canonical_and_read_view_locks_while_rclone_runs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)
    held: list[str] = []

    @contextmanager
    def fake_canonical_lock(observed_ai_root):
        assert observed_ai_root == ai_root
        held.append("canonical")
        try:
            yield
        finally:
            assert held[-1] == "canonical"
            held.pop()

    @contextmanager
    def fake_read_view_lock(observed_ai_root):
        assert observed_ai_root == ai_root
        assert held == ["canonical"]
        held.append("read-view")
        try:
            yield
        finally:
            assert held[-1] == "read-view"
            held.pop()

    clock_calls = []

    def fake_monotonic():
        assert held == ["canonical", "read-view"]
        clock_calls.append(None)
        return 100.0

    commands = []

    def fake_runner(command, *, check, timeout):
        _assert_bounded_timeout(timeout)
        assert check is False
        assert held == ["canonical", "read-view"]
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mirror, "canonical_io_lock", fake_canonical_lock)
    monkeypatch.setattr(mirror, "mirror_read_lock", fake_read_view_lock)
    monkeypatch.setattr(mirror, "time", SimpleNamespace(monotonic=fake_monotonic))

    mirror.refresh_pull_only_mirror(
        ai_root,
        vault_root,
        remote="nextcloud-ai:ObsidianVault",
        rclone_config=config,
        filter_file=filters,
        runner=fake_runner,
    )

    assert held == []
    assert [command[1] for command in commands] == ["sync", "check"]
    assert len(clock_calls) == 3


def test_pull_mirror_fails_closed_on_rclone_error(tmp_path: Path) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)
    commands = []

    def fake_runner(command, *, check, timeout):
        _assert_bounded_timeout(timeout)
        assert check is False
        commands.append(command)
        return SimpleNamespace(returncode=23)

    with pytest.raises(mirror.MirrorRefreshError, match="exit code 23"):
        mirror.refresh_pull_only_mirror(
            ai_root,
            vault_root,
            remote="nextcloud-ai:ObsidianVault",
            rclone_config=config,
            filter_file=filters,
            runner=fake_runner,
        )
    assert len(commands) == 1


def test_pull_mirror_sync_spawn_failure_prevents_verification(tmp_path: Path) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)
    commands = []

    def fake_runner(command, *, check, timeout):
        _assert_bounded_timeout(timeout)
        assert check is False
        commands.append(command)
        raise OSError("permission denied")

    with pytest.raises(mirror.MirrorRefreshError, match="cannot execute rclone: permission denied"):
        mirror.refresh_pull_only_mirror(
            ai_root,
            vault_root,
            remote="nextcloud-ai:ObsidianVault",
            rclone_config=config,
            filter_file=filters,
            runner=fake_runner,
        )
    assert len(commands) == 1


def test_pull_mirror_sync_timeout_fails_closed_and_releases_locks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)
    held: list[str] = []
    commands = []

    @contextmanager
    def fake_canonical_lock(observed_ai_root):
        assert observed_ai_root == ai_root
        held.append("canonical")
        try:
            yield
        finally:
            assert held[-1] == "canonical"
            held.pop()

    @contextmanager
    def fake_read_view_lock(observed_ai_root):
        assert observed_ai_root == ai_root
        assert held == ["canonical"]
        held.append("read-view")
        try:
            yield
        finally:
            assert held[-1] == "read-view"
            held.pop()

    def fake_runner(command, *, check, timeout):
        _assert_bounded_timeout(timeout)
        assert check is False
        assert held == ["canonical", "read-view"]
        commands.append(command)
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(mirror, "canonical_io_lock", fake_canonical_lock)
    monkeypatch.setattr(mirror, "mirror_read_lock", fake_read_view_lock)

    with pytest.raises(mirror.MirrorRefreshError) as error:
        mirror.refresh_pull_only_mirror(
            ai_root,
            vault_root,
            remote="nextcloud-ai:ObsidianVault",
            rclone_config=config,
            filter_file=filters,
            runner=fake_runner,
        )

    assert str(error.value) == "rclone pull-only mirror sync timed out"
    assert [command[1] for command in commands] == ["sync"]
    assert held == []


def test_pull_mirror_fails_closed_on_verification_error(tmp_path: Path) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)
    commands = []

    def fake_runner(command, *, check, timeout):
        _assert_bounded_timeout(timeout)
        assert check is False
        commands.append(command)
        return SimpleNamespace(returncode=0 if len(commands) == 1 else 9)

    with pytest.raises(mirror.MirrorRefreshError, match="verification failed with exit code 9"):
        mirror.refresh_pull_only_mirror(
            ai_root,
            vault_root,
            remote="nextcloud-ai:ObsidianVault",
            rclone_config=config,
            filter_file=filters,
            runner=fake_runner,
        )
    assert [command[1] for command in commands] == ["sync", "check"]


def test_pull_mirror_verification_timeout_fails_closed(monkeypatch, tmp_path: Path) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)
    held: list[str] = []
    commands = []

    @contextmanager
    def fake_canonical_lock(observed_ai_root):
        assert observed_ai_root == ai_root
        held.append("canonical")
        try:
            yield
        finally:
            assert held[-1] == "canonical"
            held.pop()

    @contextmanager
    def fake_read_view_lock(observed_ai_root):
        assert observed_ai_root == ai_root
        assert held == ["canonical"]
        held.append("read-view")
        try:
            yield
        finally:
            assert held[-1] == "read-view"
            held.pop()

    def fake_runner(command, *, check, timeout):
        _assert_bounded_timeout(timeout)
        assert check is False
        assert held == ["canonical", "read-view"]
        commands.append(command)
        if command[1] == "sync":
            return SimpleNamespace(returncode=0)
        raise subprocess.TimeoutExpired(
            ["private", "verification", "args"], timeout
        )

    monkeypatch.setattr(mirror, "canonical_io_lock", fake_canonical_lock)
    monkeypatch.setattr(mirror, "mirror_read_lock", fake_read_view_lock)

    with pytest.raises(mirror.MirrorRefreshError) as error:
        mirror.refresh_pull_only_mirror(
            ai_root,
            vault_root,
            remote="nextcloud-ai:ObsidianVault",
            rclone_config=config,
            filter_file=filters,
            runner=fake_runner,
        )

    assert str(error.value) == "rclone pull-only mirror verification timed out"
    assert "private" not in str(error.value)
    assert [command[1] for command in commands] == ["sync", "check"]
    assert held == []


def test_pull_mirror_verification_spawn_failure_is_distinct(tmp_path: Path) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)
    commands = []

    def fake_runner(command, *, check, timeout):
        _assert_bounded_timeout(timeout)
        assert check is False
        commands.append(command)
        if len(commands) == 1:
            return SimpleNamespace(returncode=0)
        raise OSError("exec denied")

    with pytest.raises(mirror.MirrorRefreshError, match="cannot execute rclone check: exec denied"):
        mirror.refresh_pull_only_mirror(
            ai_root,
            vault_root,
            remote="nextcloud-ai:ObsidianVault",
            rclone_config=config,
            filter_file=filters,
            runner=fake_runner,
        )
    assert [command[1] for command in commands] == ["sync", "check"]


def test_pull_mirror_subprocess_timeout_shrinks_to_shared_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)
    clock_values = iter((50.0, 50.0, 800.0))
    monkeypatch.setattr(
        mirror,
        "time",
        SimpleNamespace(monotonic=lambda: next(clock_values)),
    )
    timeouts = []

    def fake_runner(command, *, check, timeout):
        _assert_bounded_timeout(timeout)
        assert check is False
        timeouts.append(timeout)
        return SimpleNamespace(returncode=0)

    mirror.refresh_pull_only_mirror(
        ai_root,
        vault_root,
        remote="nextcloud-ai:ObsidianVault",
        rclone_config=config,
        filter_file=filters,
        runner=fake_runner,
    )

    assert timeouts == [600.0, 150.0]


def test_pull_mirror_exhausted_shared_budget_does_not_start_check(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)
    clock_values = iter((10.0, 10.0, 911.0))
    monkeypatch.setattr(
        mirror,
        "time",
        SimpleNamespace(monotonic=lambda: next(clock_values)),
    )
    commands = []

    def fake_runner(command, *, check, timeout):
        _assert_bounded_timeout(timeout)
        assert check is False
        commands.append(command)
        return SimpleNamespace(returncode=0)

    with pytest.raises(mirror.MirrorRefreshError, match="budget exhausted before verification"):
        mirror.refresh_pull_only_mirror(
            ai_root,
            vault_root,
            remote="nextcloud-ai:ObsidianVault",
            rclone_config=config,
            filter_file=filters,
            runner=fake_runner,
        )

    assert [command[1] for command in commands] == ["sync"]


def test_pull_mirror_refreshes_same_size_changed_content(tmp_path: Path) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)
    remote_content = b"new!"
    local_file = vault_root / "note.md"
    local_file.write_bytes(b"old!")

    def fake_runner(command, *, check, timeout):
        _assert_bounded_timeout(timeout)
        assert check is False
        if command[1] == "sync":
            # Model rclone's size-only default: same-size files are skipped
            # unless the explicit option forces transfer.
            if "--ignore-times" in command or local_file.stat().st_size != len(remote_content):
                local_file.write_bytes(remote_content)
            return SimpleNamespace(returncode=0)

        assert command[1] == "check"
        # Model a default size-only check unless download comparison is asked
        # for; with --download, compare the content itself.
        if "--download" in command:
            matches = local_file.read_bytes() == remote_content
        else:
            matches = local_file.stat().st_size == len(remote_content)
        return SimpleNamespace(returncode=0 if matches else 1)

    mirror.refresh_pull_only_mirror(
        ai_root,
        vault_root,
        remote="nextcloud-ai:ObsidianVault",
        rclone_config=config,
        filter_file=filters,
        runner=fake_runner,
    )

    assert local_file.read_bytes() == remote_content


def test_pull_mirror_verification_mismatch_fails_closed(tmp_path: Path) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)
    local_file = vault_root / "note.md"
    local_file.write_bytes(b"stale")
    commands = []

    def fake_runner(command, *, check, timeout):
        _assert_bounded_timeout(timeout)
        assert check is False
        commands.append(command)
        if command[1] == "sync":
            # Simulate a completed sync that did not leave the expected bytes.
            return SimpleNamespace(returncode=0)
        assert command[1] == "check"
        assert "--download" in command
        return SimpleNamespace(returncode=0 if local_file.read_bytes() == b"fresh" else 1)

    with pytest.raises(mirror.MirrorRefreshError, match="verification failed with exit code 1"):
        mirror.refresh_pull_only_mirror(
            ai_root,
            vault_root,
            remote="nextcloud-ai:ObsidianVault",
            rclone_config=config,
            filter_file=filters,
            runner=fake_runner,
        )
    assert [command[1] for command in commands] == ["sync", "check"]


def test_pull_mirror_rejects_local_source(tmp_path: Path) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)

    with pytest.raises(mirror.MirrorRefreshError, match="named remote"):
        mirror.refresh_pull_only_mirror(
            ai_root,
            vault_root,
            remote="/srv/ObsidianVault",
            rclone_config=config,
            filter_file=filters,
            runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
        )


def test_pull_mirror_rejects_symlink_filter_file(tmp_path: Path) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)
    linked = tmp_path / "linked.filters"
    linked.symlink_to(filters)

    with pytest.raises(mirror.MirrorRefreshError, match="non-symlink"):
        mirror.refresh_pull_only_mirror(
            ai_root,
            vault_root,
            remote="nextcloud-ai:ObsidianVault",
            rclone_config=config,
            filter_file=linked,
        )


def test_pull_mirror_cli_reports_remote_to_local(monkeypatch, tmp_path: Path, capsys) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)

    monkeypatch.setattr(
        mirror,
        "refresh_pull_only_mirror",
        lambda *_args, **_kwargs: mirror.MirrorRefreshResult(
            remote="nextcloud-ai:ObsidianVault",
            vault_root=vault_root,
        ),
    )

    rc = mirror.main(
        [
            "--ai-root",
            str(ai_root),
            "--vault-root",
            str(vault_root),
            "--remote",
            "nextcloud-ai:ObsidianVault",
            "--rclone-config",
            str(config),
            "--filter-file",
            str(filters),
        ]
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert '"direction": "remote_to_local"' in output
    assert '"status": "refreshed"' in output


def test_pull_mirror_cli_does_not_report_success_on_verification_failure(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)

    def failed_refresh(*_args, **_kwargs):
        raise mirror.MirrorRefreshError("rclone pull-only mirror verification failed with exit code 1")

    monkeypatch.setattr(mirror, "refresh_pull_only_mirror", failed_refresh)

    rc = mirror.main(
        [
            "--ai-root",
            str(ai_root),
            "--vault-root",
            str(vault_root),
            "--remote",
            "nextcloud-ai:ObsidianVault",
            "--rclone-config",
            str(config),
            "--filter-file",
            str(filters),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 2
    assert "refreshed" not in captured.out
    assert "verification failed" in captured.err
