"""Root production shared updater gates, verified using disposable test inodes.

No real systemd, production directories, provider/credential paths or Vault
mutation. Pinned production path rejection remains real; an injected
nonroot-owned throwaway flock context qualifies the order and marker checks.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path

import pytest

from obsidian_automation import github_production_staging as staged
from obsidian_automation import github_production_update as legacy
from obsidian_automation import pre_review_production_update as pre_review
from obsidian_automation import admin_local_maintenance_gate as gates
from obsidian_automation.admin_operation_broker import BrokerRejected

ROOT_SHA = "c" * 40


def test_root_stage_refuses_redirected_shared_authority_paths(tmp_path):
    with pytest.raises(legacy.ProductionUpdateError, match="authority roots are fixed"):
        with staged._lock(
            tmp_path / "staged",
            require_root=True,
            host_receipt_dir=legacy.HOST_RECEIPT_DIR,
        ):
            pytest.fail("must_not_reach_mutation")
    with pytest.raises(legacy.ProductionUpdateError, match="authority roots are fixed"):
        with staged._lock(
            legacy.DEFAULT_RECEIPT_DIR,
            require_root=True,
            host_receipt_dir=tmp_path,
        ):
            pytest.fail("must_not_reach_mutation")


def test_root_stage_obtains_both_existing_kernel_locks_and_rejects_pending_marker(
    tmp_path, monkeypatch,
):
    host, refresh, state = (
        tmp_path / "host", tmp_path / "refresh", tmp_path / "state"
    )
    for root in (host, refresh, state):
        root.mkdir(mode=0o700)
    for root, name in (
        (host, gates.HOST_LOCK), (refresh, gates.REFRESH_LOCK)
    ):
        path = root / name
        path.touch(mode=0o600)
        path.chmod(0o600)

    real_host_guard = legacy._host_update_guard
    real_semantic_guard = legacy._verified_semantic_exclusion

    @contextmanager
    def disposable_host_guard(root, *, require_root, require_existing_lock):
        # Fixed root caller must never fall back to O_CREAT for the host lock.
        assert root == legacy.HOST_RECEIPT_DIR
        assert require_root is True and require_existing_lock is True
        with real_host_guard(
            host, require_root=False, require_existing_lock=True,
        ):
            yield

    @contextmanager
    def disposable_semantic_guard():
        # The production function itself performs a real no-create inode
        # acquire. Only the fixed directory/root requirement is substituted
        # for this synthetic test; root Stage imports no #300 modules.
        with real_semantic_guard(
            path=refresh/gates.REFRESH_PENDING, require_root=False,
        ):
            yield

    def disposable_staging_dir(root, *, create):
        assert root == legacy.DEFAULT_RECEIPT_DIR
        assert create is True
        return state

    monkeypatch.setattr(legacy, "_host_update_guard", disposable_host_guard)
    monkeypatch.setattr(legacy, "_verified_semantic_exclusion", disposable_semantic_guard)
    monkeypatch.setattr(staged, "_state_dir", disposable_staging_dir)

    with staged._lock(legacy.DEFAULT_RECEIPT_DIR, require_root=True):
        for root, name in ((host,gates.HOST_LOCK),(refresh,gates.REFRESH_LOCK)):
            fd = os.open(root/name, os.O_RDWR | os.O_NOFOLLOW)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
    assert (state/"transaction.lock").is_file()

    (refresh/gates.REFRESH_PENDING).write_text("DISPOSABLE_ONLY\n")
    with pytest.raises(legacy.ProductionUpdateError, match="semantic update reconciliation required"):
        with staged._lock(legacy.DEFAULT_RECEIPT_DIR, require_root=True):
            pytest.fail("missing_pending_block")
    (refresh/gates.REFRESH_PENDING).unlink()

    # Missing immutable semantic lock inode cannot be recreated by Stage.
    (refresh/gates.REFRESH_LOCK).unlink()
    with pytest.raises(legacy.ProductionUpdateError, match="semantic update lock unavailable"):
        with staged._lock(legacy.DEFAULT_RECEIPT_DIR, require_root=True):
            pytest.fail("missing_lock_not_blocked")
    assert not (refresh/gates.REFRESH_LOCK).exists()


def test_root_pre_review_cannot_select_a_different_semantic_lock(tmp_path,monkeypatch):
    with monkeypatch.context() as ctx:
        ctx.setattr(pre_review.os, "geteuid", lambda: 0)
        with pytest.raises(
            pre_review.PreReviewProductionUpdateError,
            match="production_semantic_lock_path_is_fixed",
        ):
            pre_review.execute_update(
                target_sha=ROOT_SHA, require_root=True,
                refresh_inhibit_path=tmp_path/"semantic-refresh-inhibited.json",
            )
    assert not list(tmp_path.iterdir())


def test_root_host_runtime_cannot_relocate_receipt_or_semantic_lock(tmp_path):
    from tools import host_runtime_lifecycle as life
    args=dict(
        source_root=tmp_path,
        target_sha=ROOT_SHA,
        venv_root=tmp_path/"venv",
        receipt_dir=tmp_path/"wrong_host",
        runner=lambda args: None,
        require_root=True,
    )
    with pytest.raises(life.LifecycleError, match="production_host_update_paths_are_fixed"):
        life.RuntimeTransaction(**args)
    args["receipt_dir"] = Path("/var/lib/obsidian-automation/deployments")
    args["refresh_inhibit_path"] = tmp_path/"wrong_semantic.json"
    with pytest.raises(life.LifecycleError, match="production_host_update_paths_are_fixed"):
        life.RuntimeTransaction(**args)
    assert not list(tmp_path.iterdir())


def test_root_source_bootstrap_rejects_redirected_host_lock_before_git(tmp_path,monkeypatch):
    from tools import production_bootstrap as bootstrap
    with monkeypatch.context() as ctx:
        ctx.setattr(bootstrap.os, "geteuid", lambda: 0)
        with pytest.raises(
            bootstrap.BootstrapError,
            match="production_host_receipt_root_is_fixed",
        ):
            bootstrap.apply_from_target(
                source_root=tmp_path, target_sha=ROOT_SHA,
                profile="automation", receipt_dir=tmp_path/"fake_host",
            )
    assert not list(tmp_path.iterdir())

def test_real_three_file_first_stage_import_has_no_extra_module_dependency(tmp_path):
    """The very first approved Stage cannot import installed #300 code."""
    import subprocess
    import sys
    import shutil
    from tools.bootstrap_github_stage import REQUIRED_MODULES
    assert len(REQUIRED_MODULES) == 3
    root = Path(__file__).resolve().parents[1]
    package = tmp_path/"obsidian_automation"
    package.mkdir()
    for relpath in REQUIRED_MODULES:
        destination = package/Path(relpath).name
        shutil.copyfile(root/relpath, destination)
        destination.chmod(0o600)
    snippet = (
        "import sys;sys.path.insert(0,sys.argv[1]);"
        "from obsidian_automation import github_production_staging as stage;"
        "from obsidian_automation import github_production_update as legacy;"
        "assert stage.legacy is legacy;"
        "assert hasattr(legacy,'_verified_semantic_exclusion');"
        "print('ISOLATED_THREE_MODULE_BOOTSTRAP_PASS')"
    )
    result=subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c",snippet,str(tmp_path)],
        check=False,capture_output=True,text=True,timeout=8,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ISOLATED_THREE_MODULE_BOOTSTRAP_PASS"


def test_root_bootstrap_handoff_rejects_redirect_before_git_or_temp(
    tmp_path,monkeypatch,
):
    from tools import production_bootstrap as bootstrap
    executed=[]
    def fake_run(cmd):
        executed.append(cmd)
        raise AssertionError("root_bootstrap_git_before_approval")
    with monkeypatch.context() as ctx:
        ctx.setattr(bootstrap.os, "geteuid", lambda: 0)
        with pytest.raises(
            bootstrap.BootstrapError,
            match="production_host_receipt_root_is_fixed",
        ):
            bootstrap.handoff_to_target(
                target_sha=ROOT_SHA, profile="automation",
                receipt_dir=tmp_path/"other_host_receipts",
                runner=fake_run,
            )
    assert executed == []
    assert not list(tmp_path.iterdir())
