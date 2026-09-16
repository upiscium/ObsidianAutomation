from __future__ import annotations

import os
from pathlib import Path

import pytest

from obsidian_automation.production_io import (
    CANONICAL_IO_LOCK_NAME,
    ProductionIOError,
    canonical_io_lock,
)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    (root / "24-Locks").mkdir(parents=True)
    return root


def test_canonical_io_lock_creates_shared_regular_lock_file(tmp_path: Path) -> None:
    root = _root(tmp_path)

    with canonical_io_lock(root) as path:
        assert path == root / "24-Locks" / CANONICAL_IO_LOCK_NAME
        assert path.is_file()

    mode = os.stat(root / "24-Locks" / CANONICAL_IO_LOCK_NAME).st_mode & 0o777
    assert mode & 0o660 == 0o660


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
