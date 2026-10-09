"""Fault-injection and concurrency acceptance for atomic CAS publication."""
from __future__ import annotations

import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from obsidian_automation import artifact_lifecycle as cas


def test_partial_staged_write_is_not_a_visible_final_path(tmp_path: Path, monkeypatch):
    parent = tmp_path / "cas"
    parent.mkdir()
    final = parent / "target.json"
    payload = b'{"complete": true}\n'

    def fail_mid_write(fd, data):
        os.write(fd, data[:7])
        raise RuntimeError("fault after 7 bytes")

    with monkeypatch.context() as m:
        m.setattr(cas, "_write_all", fail_mid_write)
        with pytest.raises(RuntimeError):
            cas._store_immutable(final, payload)

    assert not final.exists()
    assert not list(parent.glob(".obsidian-cas-*.tmp"))
    assert cas._store_immutable(final, payload) == final
    assert final.read_bytes() == payload


def _hard_kill_mid_write(final: str, payload: bytes):
    def injected(fd, content):
        os.write(fd, content[:7])
        os._exit(73)

    cas._write_all = injected
    cas._store_immutable(Path(final), payload)


def _hard_kill_after_publish(final: str, payload: bytes):
    original = cas._rename_noreplace

    def injected(parent_fd, staged_name, target_name):
        original(parent_fd, staged_name, target_name)
        os._exit(74)

    cas._rename_noreplace = injected
    cas._store_immutable(Path(final), payload)


@pytest.mark.parametrize(
    "target,expected_code,final_exists",
    [(_hard_kill_mid_write, 73, False), (_hard_kill_after_publish, 74, True)],
)
def test_sigkill_cutpoints_never_expose_partial_final(
    tmp_path: Path, target, expected_code, final_exists
):
    parent = tmp_path / "cas"
    parent.mkdir()
    final = parent / "target.json"
    payload = b"fully durable content"
    process = multiprocessing.get_context("fork").Process(
        target=target, args=(str(final), payload)
    )
    process.start()
    process.join(timeout=5)
    assert process.exitcode == expected_code
    assert final.exists() is final_exists
    if final_exists:
        assert final.read_bytes() == payload
    assert cas._store_immutable(final, payload) == final
    assert final.read_bytes() == payload


def test_existing_identical_content_is_idempotent_and_different_is_fatal(tmp_path: Path):
    final = tmp_path / "target.json"
    assert cas._store_immutable(final, b"exact") == final
    assert cas._store_immutable(final, b"exact") == final
    with pytest.raises(cas.ArtifactLifecycleError):
        cas._store_immutable(final, b"wrong")
    assert final.read_bytes() == b"exact"


def test_symlink_target_collision_never_redirects_write(tmp_path: Path):
    outsider = tmp_path / "outsider"
    outsider.write_bytes(b"secret")
    directory = tmp_path / "cas"
    directory.mkdir()
    target = directory / "target.json"
    target.symlink_to(outsider)
    with pytest.raises(cas.ArtifactLifecycleError):
        cas._store_immutable(target, b"secret")
    assert outsider.read_bytes() == b"secret"


def test_concurrent_identical_writers_are_idempotent(tmp_path: Path):
    target = tmp_path / "cas.json"
    payload = b"same-content" * 100
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: cas._store_immutable(target, payload), range(32)))
    assert results == [target] * 32
    assert target.read_bytes() == payload


def test_concurrent_conflicting_writers_preserve_first_complete_object(tmp_path: Path):
    target = tmp_path / "cas.json"
    payloads = [b"alpha", b"beta"] * 10

    def try_store(payload):
        try:
            cas._store_immutable(target, payload)
            return "ok"
        except cas.ArtifactLifecycleError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(try_store, payloads))
    assert outcomes.count("ok") >= 1
    assert "conflict" in outcomes
    assert target.read_bytes() in {b"alpha", b"beta"}


def test_missing_atomic_rename_fails_closed_before_publication(tmp_path: Path, monkeypatch):
    target = tmp_path / "cas.json"
    with monkeypatch.context() as m:
        def unavailable(*args):
            raise cas.ArtifactLifecycleError("atomic primitive unavailable")
        m.setattr(cas, "_rename_noreplace", unavailable)
        with pytest.raises(cas.ArtifactLifecycleError):
            cas._store_immutable(target, b"new")
    assert not target.exists()
    assert not list(tmp_path.glob(".obsidian-cas-*.tmp"))
    assert cas._store_immutable(target, b"new") == target


def _write_with_production_umask(final: str):
    os.umask(0o027)
    cas._store_immutable(Path(final), b"production-mode-sample")


def test_production_umask_027_preserves_group_read_mode(tmp_path: Path):
    """Production summarizer uses UMask=0027; publication must preserve mode 0640."""
    target = tmp_path / "cas.json"
    proc = multiprocessing.get_context("fork").Process(
        target=_write_with_production_umask, args=(str(target),)
    )
    proc.start()
    proc.join(timeout=5)
    assert proc.exitcode == 0
    assert (target.stat().st_mode & 0o777) == 0o640
    assert target.read_bytes() == b"production-mode-sample"


def _attempt_fifo_collision(final: str):
    try:
        cas._store_immutable(Path(final), b"data")
    except cas.ArtifactLifecycleError:
        os._exit(0)
    os._exit(76)


def test_fifo_target_collision_fails_closed_without_blocking(tmp_path: Path):
    """A non-regular final pathname must not hang opening a FIFO for read."""
    final = tmp_path / "target.json"
    os.mkfifo(final)
    proc = multiprocessing.get_context("fork").Process(
        target=_attempt_fifo_collision, args=(str(final),)
    )
    proc.start()
    proc.join(timeout=3)
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=2)
        pytest.fail("FIFO target caused a blocking collision read")
    assert proc.exitcode == 0
    assert final.is_fifo()


def test_failed_staging_fsync_never_publishes_final(tmp_path: Path, monkeypatch):
    target = tmp_path / "target.json"
    original = os.fsync

    def fail_regular_file_sync(fd):
        import stat
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("injected staging fsync failure")
        original(fd)

    with monkeypatch.context() as m:
        m.setattr(cas.os, "fsync", fail_regular_file_sync)
        with pytest.raises(OSError, match="injected staging"):
            cas._store_immutable(target, b"complete")

    assert not target.exists()
    assert not list(tmp_path.glob(".obsidian-cas-*.tmp"))
    assert cas._store_immutable(target, b"complete") == target


def test_failed_directory_fsync_has_complete_final_and_idempotent_retry(
    tmp_path: Path, monkeypatch
):
    target = tmp_path / "target.json"
    original = os.fsync

    def fail_directory_sync(fd):
        import stat
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("injected directory fsync failure")
        original(fd)

    with monkeypatch.context() as m:
        m.setattr(cas.os, "fsync", fail_directory_sync)
        with pytest.raises(OSError, match="injected directory"):
            cas._store_immutable(target, b"complete")

    assert target.read_bytes() == b"complete"
    assert not list(tmp_path.glob(".obsidian-cas-*.tmp"))
    assert cas._store_immutable(target, b"complete") == target
