from __future__ import annotations

from pathlib import Path

import pytest

from obsidian_automation.production_io import (
    CANONICAL_IO_LOCK_NAME,
    MIRROR_READ_LOCK_NAME,
    READ_VIEW_LOCK_DIR_NAME,
    ProductionIOError,
    canonical_io_lock,
    mirror_read_lock,
)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    (root / "24-Locks" / READ_VIEW_LOCK_DIR_NAME).mkdir(parents=True)
    return root


def test_canonical_io_lock_creates_shared_regular_lock_file(tmp_path: Path) -> None:
    root = _root(tmp_path)

    with canonical_io_lock(root) as path:
        assert path == root / "24-Locks" / CANONICAL_IO_LOCK_NAME
        assert path.is_file()

    assert (root / "24-Locks" / CANONICAL_IO_LOCK_NAME).is_file()


def test_canonical_io_lock_rejects_symlink_lock_file(tmp_path: Path) -> None:
    root = _root(tmp_path)
    target = tmp_path / "target"
    target.write_text("x", encoding="utf-8")
    (root / "24-Locks" / CANONICAL_IO_LOCK_NAME).symlink_to(target)

    with pytest.raises(ProductionIOError, match="cannot open canonical production I/O lock"):
        with canonical_io_lock(root):
            raise AssertionError("lock must not be acquired")


def test_canonical_io_lock_requires_existing_safe_lock_directory(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()

    with pytest.raises(ProductionIOError, match="production lock directory does not exist"):
        with canonical_io_lock(root):
            raise AssertionError("lock must not be acquired")


def test_mirror_read_lock_uses_narrow_subdirectory(tmp_path: Path) -> None:
    root = _root(tmp_path)

    with mirror_read_lock(root) as path:
        assert path == (
            root / "24-Locks" / READ_VIEW_LOCK_DIR_NAME / MIRROR_READ_LOCK_NAME
        )
        assert path.is_file()

    assert (
        root / "24-Locks" / READ_VIEW_LOCK_DIR_NAME / MIRROR_READ_LOCK_NAME
    ).is_file()


def test_mirror_read_lock_rejects_symlink_lock_file(tmp_path: Path) -> None:
    root = _root(tmp_path)
    target = tmp_path / "target-read-view"
    target.write_text("x", encoding="utf-8")
    lock = root / "24-Locks" / READ_VIEW_LOCK_DIR_NAME / MIRROR_READ_LOCK_NAME
    lock.symlink_to(target)

    with pytest.raises(ProductionIOError, match="cannot open mirror read-view lock"):
        with mirror_read_lock(root):
            raise AssertionError("lock must not be acquired")


def test_mirror_read_lock_requires_existing_narrow_directory(tmp_path: Path) -> None:
    root = tmp_path / "state"
    (root / "24-Locks").mkdir(parents=True)

    with pytest.raises(
        ProductionIOError,
        match="mirror read-view lock directory does not exist",
    ):
        with mirror_read_lock(root):
            raise AssertionError("lock must not be acquired")
