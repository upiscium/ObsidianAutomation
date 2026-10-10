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


def test_existing_immutable_artifact_skips_staging_and_rewrite(
    tmp_path: Path, monkeypatch
):
    """Resume must not need extra free space/writable directories for CAS hits."""
    target = tmp_path / "existing.json"
    payload = b"already-published"
    assert cas._store_immutable(target, payload) == target

    def unexpected_write(fd, data):
        raise AssertionError("duplicate CAS store attempted redundant staging")

    with monkeypatch.context() as m:
        m.setattr(cas, "_write_all", unexpected_write)
        m.setattr(cas, "_rename_noreplace", unexpected_write)
        assert cas._store_immutable(target, payload) == target
    assert target.read_bytes() == payload
    assert not list(tmp_path.glob(".obsidian-cas-*.tmp"))


def test_staged_payload_is_owner_only_until_bytes_are_complete(tmp_path: Path, monkeypatch):
    """No observer with group/other read authority can see partial bytes."""
    target = tmp_path / "staged.json"
    captured = []
    write_original = cas._write_all

    def inspected_write(fd, data):
        import stat
        captured.append(stat.S_IMODE(os.fstat(fd).st_mode))
        write_original(fd, data)

    with monkeypatch.context() as m:
        m.setattr(cas, "_write_all", inspected_write)
        cas._store_immutable(target, b"private until complete")
    assert captured == [0o600]
    assert target.read_bytes() == b"private until complete"
    # A production equivalent UMask=0027 regression is already above.


def test_default_acl_reader_masked_while_staged_then_restored(
    tmp_path: Path, monkeypatch
):
    """Simulate named Renderer ACL inherited by a 0640 Summary CAS object."""
    import ctypes
    import stat

    try:
        acl = ctypes.CDLL("libacl.so.1", use_errno=True)
    except OSError:
        pytest.skip("Linux POSIX ACL library is unavailable")
    acl.acl_from_text.argtypes = [ctypes.c_char_p]
    acl.acl_from_text.restype = ctypes.c_void_p
    acl.acl_set_file.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    acl.acl_set_file.restype = ctypes.c_int
    acl.acl_get_file.argtypes = [ctypes.c_char_p, ctypes.c_int]
    acl.acl_get_file.restype = ctypes.c_void_p
    acl.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ssize_t)]
    acl.acl_to_text.restype = ctypes.c_void_p
    acl.acl_free.argtypes = [ctypes.c_void_p]
    acl.acl_free.restype = ctypes.c_int

    default_acl = acl.acl_from_text(b"u::rwx,u:1001:r-x,g::---,m::rwx,o::---")
    if not default_acl:
        pytest.skip("cannot construct POSIX default ACL")
    try:
        if acl.acl_set_file(os.fsencode(tmp_path), 0x4000, default_acl) != 0:
            pytest.skip("filesystem does not support default ACL")
    finally:
        acl.acl_free(default_acl)

    def access_acl_text(path: Path) -> str:
        handle = acl.acl_get_file(os.fsencode(path), 0x8000)
        assert handle
        try:
            n = ctypes.c_ssize_t()
            ptr = acl.acl_to_text(handle, ctypes.byref(n))
            assert ptr
            try:
                return ctypes.string_at(ptr, n.value).decode("utf-8")
            finally:
                acl.acl_free(ptr)
        finally:
            acl.acl_free(handle)

    target = tmp_path / "summary-cas.json"
    snapshots = []
    original_write = cas._write_all

    def observe(fd, data):
        snapshots.append((stat.S_IMODE(os.fstat(fd).st_mode), access_acl_text(target.parent / staging[0])))
        original_write(fd, data)

    staging = []
    at_creation = []
    original_open = cas.os.open
    def observe_new_open(file, flags, *args, **kwargs):
        result = original_open(file, flags, *args, **kwargs)
        if isinstance(file, str) and file.startswith(".obsidian-cas-"):
            staging.append(file)
            at_creation.append((stat.S_IMODE(os.fstat(result).st_mode),
                                access_acl_text(target.parent / file)))
        return result

    with monkeypatch.context() as m:
        m.setattr(cas, "_write_all", observe)
        m.setattr(cas.os, "open", observe_new_open)
        cas._store_immutable(target, b"fully committed private content")

    assert len(snapshots) == 1
    mode, acl_during_write = snapshots[0]
    assert mode == 0o600
    # libacl prints a name (runner) when uid=1001 resolves on CI, but the
    # numeric UID on minimal systems. Compare the effective ACL, not spelling.
    def named_user_effective(text):
        rows = [line for line in text.splitlines()
                if line.startswith("user:") and not line.startswith("user::")]
        assert len(rows) == 1
        assert ":r-x" in rows[0]
        return rows[0].split("#effective:", 1)[1].strip()

    assert len(at_creation) == 1
    assert at_creation[0][0] == 0o600
    assert named_user_effective(at_creation[0][1]) == "---"
    assert named_user_effective(acl_during_write) == "---"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    acl_final = access_acl_text(target)
    assert named_user_effective(acl_final) == "r--"


def test_permission_narrowing_failure_aborts_before_payload_write(
    tmp_path: Path, monkeypatch
):
    target = tmp_path / "new.json"
    original = cas.os.fchmod
    def reject_owner_only(fd, mode):
        if mode & 0o077 == 0:
            raise PermissionError("simulated owner-only chmod denied")
        return original(fd, mode)
    def unexpected_writer(fd, data):
        raise AssertionError("attempted payload write after chmod failure")
    with monkeypatch.context() as m:
        m.setattr(cas.os, "fchmod", reject_owner_only)
        m.setattr(cas, "_write_all", unexpected_writer)
        with pytest.raises(PermissionError, match="owner-only"):
            cas._store_immutable(target, b"do not expose me")
    assert not target.exists()
    assert not list(tmp_path.glob(".obsidian-cas-*.tmp"))


def test_permission_restore_failure_leaves_no_final_file(
    tmp_path: Path, monkeypatch
):
    target = tmp_path / "new.json"
    original = cas.os.fchmod
    counter = [0]
    def reject_restore(fd, mode):
        counter[0] += 1
        if counter[0] == 2:
            raise PermissionError("simulated restore failure")
        return original(fd, mode)
    with monkeypatch.context() as m:
        m.setattr(cas.os, "fchmod", reject_restore)
        with pytest.raises(PermissionError, match="restore"):
            cas._store_immutable(target, b"fully synced but unpublished")
    assert counter[0] == 2
    assert not target.exists()
    assert not list(tmp_path.glob(".obsidian-cas-*.tmp"))
    assert cas._store_immutable(target, b"fully synced but unpublished") == target


def test_data_staging_starts_owner_only_at_inode_creation(tmp_path: Path, monkeypatch):
    """chmod after O_CREAT cannot revoke an already-open observer FD."""
    import stat

    target = tmp_path / "cas.json"
    original_open = cas.os.open
    observed = []

    def inspect_create(file, flags, *args, **kwargs):
        fd = original_open(file, flags, *args, **kwargs)
        if (isinstance(file, str) and file.startswith(".obsidian-cas-")
                and flags & os.O_CREAT):
            observed.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fd

    with monkeypatch.context() as m:
        m.setattr(cas.os, "open", inspect_create)
        cas._store_immutable(target, b"private bytes must start owner-only")
    assert observed == [0o600]
    assert target.read_bytes() == b"private bytes must start owner-only"
    assert not list(tmp_path.glob(".obsidian-mode-probe-*.tmp"))
    assert not list(tmp_path.glob(".obsidian-cas-*.tmp"))


def test_first_use_layout_fsyncs_parent_before_artifact_publish(tmp_path, monkeypatch):
    """An fsynced child directory alone does not persist its parent's entry."""
    recorded = []
    real_fsync = cas.os.fsync

    def record_fsync(fd):
        recorded.append(os.readlink(f"/proc/self/fd/{fd}"))
        real_fsync(fd)

    with monkeypatch.context() as m:
        m.setattr(cas.os, "fsync", record_fsync)
        layout = cas.ensure_artifact_layout(tmp_path)
        assert set(recorded) == {str(tmp_path)}
        assert len(recorded) == 4
        recorded.clear()
        assert cas.ensure_artifact_layout(tmp_path) == layout
        assert recorded == []
        cas._store_immutable(layout.untrusted / "first.json", b"fully-committed")
    assert (layout.untrusted / "first.json").read_bytes() == b"fully-committed"


def test_new_directory_parent_fsync_failure_never_reports_success(tmp_path, monkeypatch):
    original = cas.os.fsync

    def deny_parent(fd):
        if os.readlink(f"/proc/self/fd/{fd}") == str(tmp_path):
            raise OSError("simulated layout parent fsync failure")
        return original(fd)

    with monkeypatch.context() as m:
        m.setattr(cas.os, "fsync", deny_parent)
        with pytest.raises(OSError, match="layout parent fsync failure"):
            cas.ensure_artifact_layout(tmp_path)
    assert not list(tmp_path.rglob("*.json"))
