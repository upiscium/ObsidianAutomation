"""B3 new-host cooperating lock tests: disposable files and fork only."""
from __future__ import annotations

import fcntl
import multiprocessing
import os
from pathlib import Path
import threading

import pytest

from obsidian_automation import github_b3_canonical_writer_guard as mod
from obsidian_automation.github_b3_canonical_writer_guard import (
    B3CanonicalWriterGuard,
    B3CanonicalWriterGuardUnavailable,
)


@pytest.fixture
def source(tmp_path, monkeypatch):
    directory = tmp_path / "canonical"
    directory.mkdir()
    directory.chmod(0o750)
    f = directory / mod.LOCK_NAME
    f.write_bytes(b"")
    f.chmod(0o640)
    monkeypatch.setattr(mod, "TRUST_ANCHOR", tmp_path)
    monkeypatch.setattr(mod, "LOCK_ROOT", directory)
    monkeypatch.setattr(mod, "_owner_uid", lambda: os.geteuid())
    monkeypatch.setattr(mod, "_shared_gid", lambda: os.getegid())
    monkeypatch.setattr(mod, "_require_reader_group", lambda _: None)
    return directory, f


def refuses():
    with pytest.raises(
        B3CanonicalWriterGuardUnavailable,
        match="canonical_writer_guard_unavailable",
    ):
        with B3CanonicalWriterGuard()():
            pytest.fail("admitted unsafe canonical writer")


def test_existing_file_can_be_locked_without_creation(source):
    directory, f = source
    with B3CanonicalWriterGuard()():
        assert f.exists()
        assert f.read_bytes() == b""
    assert sorted(p.name for p in directory.iterdir()) == [mod.LOCK_NAME]


def test_releases_and_reacquires_cleanly(source):
    for _ in range(3):
        with B3CanonicalWriterGuard()():
            pass


def test_same_process_two_instances_cannot_overlap(source):
    with B3CanonicalWriterGuard()():
        refuses()
    with B3CanonicalWriterGuard()():
        pass


def test_writer_body_exception_is_preserved_and_lock_released(source):
    with pytest.raises(ValueError, match="original-writer-error"):
        with B3CanonicalWriterGuard()():
            raise ValueError("original-writer-error")
    with B3CanonicalWriterGuard()():
        pass


def test_thread_contention_denied_without_waiting(source):
    ready = threading.Event()
    release = threading.Event()
    outcome = []

    def holder():
        with B3CanonicalWriterGuard()():
            ready.set()
            release.wait(2)

    thread = threading.Thread(target=holder)
    thread.start()
    assert ready.wait(2)
    try:
        try:
            with B3CanonicalWriterGuard()():
                outcome.append("unsafe")
        except B3CanonicalWriterGuardUnavailable:
            outcome.append("busy")
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert outcome == ["busy"]


def _child_flholder(path, ready, release):
    # Independent PID holds same existing kernel lock inode, no fixture
    # patch state required and no network/production effects.
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        ready.set()
        release.wait(5)
    finally:
        os.close(fd)


def test_cross_process_os_flock_contention_denied(source):
    directory, f = source
    ctx = multiprocessing.get_context("fork")
    ready, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_child_flholder, args=(str(f), ready, release))
    child.start()
    try:
        assert ready.wait(5)
        refuses()
    finally:
        release.set()
        child.join(5)
        if child.is_alive():
            child.terminate()
            child.join(3)
    assert child.exitcode == 0
    with B3CanonicalWriterGuard()():
        pass


def test_missing_preprovisioned_lock_refused(source):
    directory, f = source
    f.unlink()
    refuses()
    assert not f.exists()


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o660, 0o666])
def test_bad_lock_permissions_refused(source, mode):
    directory, f = source
    f.chmod(mode)
    refuses()


@pytest.mark.parametrize("mode", [0o700, 0o755, 0o770, 0o777])
def test_bad_root_directory_permissions_refused(source, mode):
    directory, f = source
    directory.chmod(mode)
    refuses()


def test_symlinked_file_refused(source):
    directory, f = source
    other = directory / "alternate"
    other.write_bytes(b"")
    other.chmod(0o640)
    f.unlink()
    f.symlink_to(other)
    refuses()


def test_symlinked_directory_refused(source, monkeypatch):
    directory, f = source
    alias = directory.parent / "linked"
    alias.symlink_to(directory, target_is_directory=True)
    monkeypatch.setattr(mod, "LOCK_ROOT", alias)
    refuses()


def test_hardlinked_lock_refused(source):
    directory, f = source
    os.link(f, directory / "alternate")
    refuses()


def test_nonempty_lock_refused(source):
    directory, f = source
    f.write_bytes(b"x")
    refuses()


def test_group_identity_mismatch_refused(source, monkeypatch):
    monkeypatch.setattr(mod, "_shared_gid", lambda: os.getegid() + 10000)
    refuses()


def test_owner_mismatch_refused(source, monkeypatch):
    monkeypatch.setattr(mod, "_owner_uid", lambda: os.geteuid() + 10000)
    refuses()


def test_reader_not_in_cooperative_group_refused(source, monkeypatch):
    monkeypatch.setattr(
        mod, "_require_reader_group",
        lambda _: (_ for _ in ()).throw(B3CanonicalWriterGuardUnavailable()),
    )
    refuses()


def test_lock_rotated_during_guard_is_uncertain(source):
    directory, f = source
    with pytest.raises(B3CanonicalWriterGuardUnavailable):
        with B3CanonicalWriterGuard()():
            previous = directory / "moved"
            f.rename(previous)
            f.write_bytes(b"")
            f.chmod(0o640)
    with B3CanonicalWriterGuard()():
        pass


def test_privileged_root_directory_rotation_during_effect_refused(source):
    directory, f = source
    with pytest.raises(B3CanonicalWriterGuardUnavailable):
        with B3CanonicalWriterGuard()():
            former = directory.parent / "retired-directory"
            directory.rename(former)
            directory.mkdir()
            directory.chmod(0o750)
            replacement = directory / mod.LOCK_NAME
            replacement.write_bytes(b"")
            replacement.chmod(0o640)
    # A new lock in a new directory is not proof the original Writer was
    # excluded. The previous Writer must be treated as effect-unknown.


def test_parent_mode_group_writable_refused(source):
    directory, f = source
    directory.parent.chmod(0o770)
    refuses()


def test_a_read_only_role_cannot_create_or_authorize(source):
    assert callable(B3CanonicalWriterGuard())
    assert not hasattr(mod, "deploy")
    assert not hasattr(mod, "cutover_authorized")
    assert not hasattr(mod, "provision_root")
