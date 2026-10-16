"""Local Core role mutex regression — disposable paths, no remote effects."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import stat

import pytest

from obsidian_automation.promotion_deployment import (
    _production_lock,
    PromotionDeploymentError,
)


@pytest.fixture
def lock_path(tmp_path: Path):
    root = tmp_path / "core-promotion"
    root.mkdir(mode=0o700)
    return root / "promotion.lock"


def test_first_lock_is_private_and_reusable(lock_path):
    with _production_lock(lock_path):
        assert lock_path.is_file()
        info = lock_path.stat()
        assert info.st_nlink == 1
        assert info.st_uid == os.geteuid()
        assert stat.S_IMODE(info.st_mode) == 0o600
    with _production_lock(lock_path):
        assert lock_path.is_file()


def test_second_instance_fails_immediately_rather_than_queue(lock_path):
    with _production_lock(lock_path):
        with pytest.raises(PromotionDeploymentError, match="promotion_already_running"):
            with _production_lock(lock_path):
                raise AssertionError("should be blocked")
    # No stale artificial failure after first run finishes.
    with _production_lock(lock_path):
        pass


def test_external_flock_blocks_promotion(lock_path):
    with _production_lock(lock_path):
        fd = os.open(lock_path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


def test_failure_during_body_releases_mutex(lock_path):
    with pytest.raises(RuntimeError, match="canary-private"):
        with _production_lock(lock_path):
            raise RuntimeError("canary-private")
    with _production_lock(lock_path):
        pass


def test_symlink_is_never_followed(lock_path):
    target = lock_path.parent / "internal"
    target.write_text("private")
    lock_path.symlink_to(target)
    with pytest.raises(PromotionDeploymentError, match="promotion_lock_unavailable"):
        with _production_lock(lock_path):
            pass
    assert target.read_text() == "private"


def test_hardlink_is_rejected(lock_path):
    lock_path.touch(mode=0o600)
    lock_path.chmod(0o600)
    os.link(lock_path, lock_path.parent / "alias")
    with pytest.raises(PromotionDeploymentError, match="unsafe_promotion_lock"):
        with _production_lock(lock_path):
            pass


@pytest.mark.parametrize("bad_mode,expected", [
    (0o644, "unsafe_promotion_lock"),
    (0o660, "unsafe_promotion_lock"),
    (0o400, "promotion_lock_unavailable"),
    (0o666, "unsafe_promotion_lock"),
])
def test_nonprivate_modes_rejected(lock_path, bad_mode, expected):
    lock_path.touch(mode=0o600)
    lock_path.chmod(bad_mode)
    with pytest.raises(PromotionDeploymentError, match=expected):
        with _production_lock(lock_path):
            pass


def test_existing_directory_at_lock_path_rejected(lock_path):
    lock_path.mkdir(mode=0o700)
    with pytest.raises(PromotionDeploymentError, match="promotion_lock_unavailable"):
        with _production_lock(lock_path):
            pass


def test_lock_inode_owner_mismatch_rejected_independently_of_parent(
    lock_path, monkeypatch,
):
    with _production_lock(lock_path):
        pass
    original = os.fstat
    expected_ino = lock_path.stat().st_ino

    def mismatched_lock_owner(fd):
        info = original(fd)
        if stat.S_ISREG(info.st_mode) and info.st_ino == expected_ino:
            # Simulate a pre-existing file owned by a different role without
            # altering the directory UID. This reaches the intended per-file
            # identity check, not the earlier safe-parent guard.
            members = list(info)
            members[4] = info.st_uid + 1
            return os.stat_result(members)
        return info

    monkeypatch.setattr(os, "fstat", mismatched_lock_owner)
    with pytest.raises(
        PromotionDeploymentError, match="^unsafe_promotion_lock$",
    ):
        with _production_lock(lock_path):
            pass


def test_failed_or_busy_path_never_renames_or_unlinks_journal(lock_path):
    with _production_lock(lock_path):
        before = lock_path.stat()
        with pytest.raises(PromotionDeploymentError, match="promotion_already_running"):
            with _production_lock(lock_path):
                pass
        after = lock_path.stat()
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


@pytest.mark.parametrize("mode", [0o777, 0o770, 0o733])
def test_core_promotion_lock_parent_must_not_be_writable_by_others(lock_path, mode):
    lock_path.parent.chmod(mode)
    with pytest.raises(PromotionDeploymentError, match="unsafe_promotion_lock_directory"):
        with _production_lock(lock_path):
            pass
    assert not lock_path.exists()


def test_core_promotion_lock_parent_must_be_owned_by_current_uid(
    lock_path, monkeypatch,
):
    real_uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real_uid + 1)
    with pytest.raises(PromotionDeploymentError, match="unsafe_promotion_lock_directory"):
        with _production_lock(lock_path):
            pass
    assert not lock_path.exists()
