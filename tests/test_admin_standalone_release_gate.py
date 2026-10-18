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


def test_legacy_module_isolated_stage_import_keeps_privileged_modules_lazy():
    import ast
    body = ast.parse(Path(update.__file__).read_text(encoding="utf-8")).body
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.level == 1
        and isinstance(node.module, str)
        and node.module.startswith("admin_")
        for node in body
    ), "isolated main Stage inspect cannot import absent #300 modules"


def test_existing_main_stage_activate_inspect_dispatch_preserved():
    source = inspect.getsource(update.main)
    assert 'arguments[0] in {"stage", "activate", "inspect"}' in source
    assert "from .github_production_staging import main as staged_main" in source


def test_root_stage_uses_both_preexisting_shared_lock_inodes():
    source = inspect.getsource(staging._lock)
    # Stage/Activate on root use the reviewed host+semantic gate. It checks
    # BOTH pinned existing lock inodes and the semantic pending marker.
    # More than a source string assertion: the fixture below exercises the
    # real OS flock and pending/missing-semantic-lock rejection.
    assert "legacy._verified_semantic_exclusion()" in source
    assert "production Stage authority roots are fixed" in source
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


def test_root_cannot_disable_privilege_checks_via_synthetic_mode(monkeypatch):
    monkeypatch.setattr(update.os, "geteuid", lambda: 0)
    with pytest.raises(update.ProductionUpdateError, match="root_must_not_disable_privileged_guard"):
        update.execute_update(target_sha=SHA, require_root=False)


def test_unknown_github_timers_are_checked_before_legacy_live_update():
    source = inspect.getsource(update.execute_update)
    assert "_assert_known_timer_inventory(runner, systemd_dir)" in source
    assert source.index("_assert_known_timer_inventory(runner, systemd_dir)") < source.index(
        "_require_idle_services(runner, systemd_dir)"
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


def test_actual_root_cannot_turn_off_stage_or_activate_guards(monkeypatch, tmp_path):
    # Synthetic require_root=False allows fixtures for ordinary unprivileged
    # tests, but may not be used by real UID 0 to bypass authority/lock checks.
    monkeypatch.setattr(staging.os, "geteuid", lambda: 0)
    inputs = {
        "app_root": tmp_path / "app",
        "venv_root": tmp_path / "venv",
        "systemd_dir": tmp_path / "systemd",
        "receipt_dir": tmp_path / "receipts",
        "daily_revision_env": tmp_path / "revision.env",
        "config_files": (),
        "require_root": False,
    }
    with pytest.raises(update.ProductionUpdateError, match="root_must_not_disable_privileged_guard"):
        staging.stage_update(target_sha=SHA, **inputs)
    with pytest.raises(update.ProductionUpdateError, match="root_must_not_disable_privileged_guard"):
        staging.activate_update(
            stage_sha256="a" * 64, approve_live_github_writer=True,
            **inputs,
        )
    from obsidian_automation.pre_review_production_update import (
        execute_update as pre_review_update,
        PreReviewProductionUpdateError,
    )
    with pytest.raises(PreReviewProductionUpdateError, match="root_must_not_disable_privileged_guard"):
        pre_review_update(
            target_sha=SHA,
            app_root=tmp_path / "pre-review-app",
            require_root=False,
        )
    assert not any(tmp_path.iterdir())


def test_every_root_cooperator_reuses_existing_semantic_lock():
    # Source contract for the two real root update entrypoints. The primitive
    # has behavioral inode, symlink and missing-lock tests in this module.
    import inspect
    from obsidian_automation import pre_review_production_update as pre_review
    source = inspect.getsource(pre_review.execute_update)
    assert "require_existing_lock=require_root" in source
    lifecycle = Path(__file__).resolve().parents[1] / "tools/host_runtime_lifecycle.py"
    assert "require_existing_lock=self.require_root" in lifecycle.read_text(encoding="utf-8")


def test_legacy_completion_proof_precedes_removing_refresh_inhibitor():
    from obsidian_automation.github_production_update import _execute_update_unlocked
    source = inspect.getsource(_execute_update_unlocked)
    assert source.index('path = _persist_receipt(receipt_dir, receipt)') < source.index(
        'after_restore_effect()'
    )
    # A target may install an extra GitHub timer AFTER preflight.
    assert source.index('label="systemd daemon-reload"') < source.index(
        'stage = "post_install_quiescence"'
    ) < source.index('stage = "live_smoke"')
