from __future__ import annotations

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


def test_pull_mirror_uses_fixed_remote_to_local_sync_command(tmp_path: Path) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)
    captured = {}

    def fake_runner(command, *, check):
        captured["command"] = command
        captured["check"] = check
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
    assert captured["check"] is False
    assert captured["command"] == [
        "rclone",
        "sync",
        "nextcloud-ai:ObsidianVault",
        str(vault_root.absolute()),
        "--config",
        str(config.absolute()),
        "--filter-from",
        str(filters.absolute()),
        "--delete-after",
        "--delete-excluded",
    ]
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

    def fake_runner(_command, *, check):
        assert check is False
        assert held == ["canonical", "read-view"]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mirror, "canonical_io_lock", fake_canonical_lock)
    monkeypatch.setattr(mirror, "mirror_read_lock", fake_read_view_lock)

    mirror.refresh_pull_only_mirror(
        ai_root,
        vault_root,
        remote="nextcloud-ai:ObsidianVault",
        rclone_config=config,
        filter_file=filters,
        runner=fake_runner,
    )

    assert held == []


def test_pull_mirror_fails_closed_on_rclone_error(tmp_path: Path) -> None:
    ai_root, vault_root, config, filters = _fixture(tmp_path)

    def fake_runner(_command, *, check):
        assert check is False
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
