"""#300 standalone main release: legacy Stage/Activate + stable lock regression.

All tests are synthetic and operate only on per-test disposable directories.
Never touch actual systemd, product containers, remote APIs or Vault.
"""
from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

from obsidian_automation import github_production_update as update
from obsidian_automation import github_production_staging as staging
from obsidian_automation.pre_review_production_update import (
    RefreshUpdateInhibit, PreReviewProductionUpdateError,
)

SHA = "b" * 40


def test_existing_main_stage_activate_inspect_dispatch_preserved():
    source = inspect.getsource(update.main)
    assert 'arguments[0] in {"stage", "activate", "inspect"}' in source
    assert "from .github_production_staging import main as staged_main" in source


def test_root_stage_requires_existing_host_update_inode():
    source = inspect.getsource(staging._lock)
    assert "require_existing_lock=require_root" in source


def test_legacy_root_cli_requires_two_distinct_authorizations(monkeypatch, capsys):
    monkeypatch.setattr(update.os, "geteuid", lambda: 0)
    assert update.main(["--target-sha", SHA]) == 2
    assert "legacy_live_effects_not_approved" in capsys.readouterr().err
    assert update.main(["--target-sha", SHA, "--approve-legacy-live-effects"]) == 2
    assert "single_lxc_coordination_required" in capsys.readouterr().err


def test_direct_root_python_api_also_requires_explicit_live_consent(monkeypatch):
    monkeypatch.setattr(update.os, "geteuid", lambda: 0)
    with pytest.raises(update.ProductionUpdateError, match="legacy_live_effects_not_approved"):
        update.execute_update(target_sha=SHA, require_root=True)
    with pytest.raises(update.ProductionUpdateError, match="single_lxc_coordination_required"):
        update.execute_update(
            target_sha=SHA, require_root=True,
            approve_legacy_live_effects=True,
        )


def test_no_replacement_host_lock_inode(tmp_path):
    root = tmp_path / "host"
    root.mkdir(mode=0o700)
    path = root / "host-update.lock"
    with pytest.raises(update.ProductionUpdateError, match="untrusted host-wide update lock"):
        with update._host_update_guard(
            root, require_root=False, require_existing_lock=True,
        ):
            pass
    assert not path.exists()


def test_semantic_lock_never_automatically_created(tmp_path):
    root = tmp_path / "semantic"
    root.mkdir(mode=0o700)
    target = root / "semantic-refresh-inhibited.json"
    lock = target.with_suffix(".lock")
    inhibit = RefreshUpdateInhibit(target, SHA, "github-updater", require_root=False)
    with pytest.raises(PreReviewProductionUpdateError, match="semantic refresh lock unavailable"):
        inhibit.acquire(publish=False, require_existing_lock=True)
    assert not lock.exists()
    assert not target.exists()


def test_semantic_lock_existing_inode_is_reused_and_not_replaced(tmp_path):
    root = tmp_path / "semantic"
    root.mkdir(mode=0o700)
    target = root / "semantic-refresh-inhibited.json"
    lock = target.with_suffix(".lock")
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    original = lock.stat().st_ino
    inhibit = RefreshUpdateInhibit(target, SHA, "github-updater", require_root=False)
    inhibit.acquire(publish=False, require_existing_lock=True)
    try:
        assert lock.stat().st_ino == original
        assert inhibit.lock_fd is not None
        assert not target.exists()
    finally:
        inhibit.close()


def test_effectful_root_paths_cannot_be_redirected(monkeypatch, tmp_path):
    monkeypatch.setattr(update.os, "geteuid", lambda: 0)
    with pytest.raises(update.ProductionUpdateError, match="production legacy effect paths must be fixed"):
        update.execute_update(
            target_sha=SHA, require_root=True,
            single_lxc_coordinated=True,
            approve_legacy_live_effects=True,
            app_root=tmp_path / "unreviewed-app",
        )
    assert not (tmp_path / "unreviewed-app").exists()


def test_src_documentation_refuses_implicitly_releasing_dashboard_subsystems():
    doc = Path(__file__).resolve().parents[1] / "docs/releases/300-standalone-20261018.md"
    content = doc.read_text(encoding="utf-8")
    assert "Connections (#301)" in content
    assert "Wizard (#304)" in content
    assert "#311 remains Draft" in content
    assert "enabled=false" in content
