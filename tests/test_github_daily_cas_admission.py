"""Linux-only, metadata-only permission policy regressions for #292."""
from __future__ import annotations

import ctypes
import os
import pwd
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from obsidian_automation import artifact_lifecycle as cas
from obsidian_automation import github_daily_cas_admission as audit


def _set_acl(path: Path, text: str, *, default: bool) -> None:
    try:
        lib = ctypes.CDLL("libacl.so.1", use_errno=True)
    except OSError:
        pytest.skip("Linux libacl unavailable")
    lib.acl_from_text.argtypes = [ctypes.c_char_p]
    lib.acl_from_text.restype = ctypes.c_void_p
    lib.acl_set_file.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    lib.acl_set_file.restype = ctypes.c_int
    lib.acl_free.argtypes = [ctypes.c_void_p]
    lib.acl_free.restype = ctypes.c_int
    handle = lib.acl_from_text(text.encode())
    if not handle:
        pytest.skip("requested ACL fixture cannot be parsed")
    try:
        result = lib.acl_set_file(os.fsencode(path), 0x4000 if default else 0x8000, handle)
        if result != 0:
            pytest.skip("filesystem does not admit POSIX ACL fixture")
    finally:
        lib.acl_free(handle)


def _fixture(tmp_path, monkeypatch):
    root = tmp_path / "cas"
    root.mkdir()
    root.chmod(0o750)
    stage = root / "output"
    stage.mkdir()
    stage.chmod(0o750)
    uid = os.getuid()
    gid = os.getgid()
    reader = uid + 22017
    monkeypatch.setattr(audit.pwd, "getpwnam",
                        lambda name: SimpleNamespace(pw_uid=uid, pw_gid=gid)
                        if name == "summarizer" else
                        SimpleNamespace(pw_uid=reader, pw_gid=gid)
                        if name == "renderer" else (_ for _ in ()).throw(KeyError(name)))
    _set_acl(
        stage,
        f"u::rwx,u:{uid}:rwx,u:{reader}:r-x,g::---,m::rwx,o::---",
        default=True,
    )
    final = stage / ("a" * 64 + ".github-daily-partial-output.json")
    original_mask = os.umask(0o027)
    try:
        cas._store_immutable(final, b'{"verified":true}\n')
    finally:
        os.umask(original_mask)
    return root, final, uid, reader


def _audit(root):
    return audit.audit_daily_summary_cas(root, owner_name="summarizer", reader_name="renderer")


def test_valid_existing_daily_cas_acl_is_accepted_without_reading_payload(tmp_path, monkeypatch):
    root, final, uid, reader = _fixture(tmp_path, monkeypatch)
    before = final.stat()
    result = _audit(root)
    after = final.stat()
    assert result == {
        "record_version": 1,
        "audit": "daily-cas-metadata-v1",
        "status": "admitted_at_inspection",
        "published_files": 1,
        "stage_directories": 1,
        "files_read": 0,
        "files_changed": 0,
        "publication_authorized": False,
    }
    assert before.st_size == after.st_size
    assert before.st_mtime_ns == after.st_mtime_ns
    assert stat.S_IMODE(after.st_mode) == 0o640


@pytest.mark.parametrize("mutate,reason", [
    ("world_read", "inode metadata"),
    ("group_write", "inode metadata"),
    ("hardlink", "inode metadata"),
    ("symlink", "cannot be safely opened"),
    ("extra_reader", "named-reader ACL"),
    ("missing_acl", "named-user ACL"),
    ("wrong_owner", "inode metadata"),
    ("orphan_temp", "unknown or uncommitted"),
    ("unknown_stage", "unexpected stage"),
    ("wrong_artifact_class", "unknown or uncommitted"),
])
def test_unsafe_existing_cas_objects_are_blocked(tmp_path, monkeypatch, mutate, reason):
    root, final, uid, reader = _fixture(tmp_path, monkeypatch)
    if mutate == "world_read":
        final.chmod(0o644)
    elif mutate == "group_write":
        final.chmod(0o660)
    elif mutate == "hardlink":
        os.link(final, root / "output" / ("b" * 64 + ".github-daily-partial-output.json"))
    elif mutate == "symlink":
        target = root / "output" / "target.txt"
        target.write_text("untrusted")
        final.unlink()
        final.symlink_to(target)
        target.unlink()  # avoid additional unknown-file precedence
    elif mutate == "extra_reader":
        extra = reader + 1
        _set_acl(final,
            f"u::rw-,u:{uid}:rw-,u:{reader}:r--,u:{extra}:r--,g::---,m::r--,o::---",
            default=False,
        )
        final.chmod(0o640)
    elif mutate == "missing_acl":
        # Replace the target with a regular file without inherited named ACL.
        # The owning stage has a default ACL; strip this file's access ACL.
        os.removexattr(final, "system.posix_acl_access")
    elif mutate == "wrong_owner":
        monkeypatch.setattr(audit.pwd, "getpwnam",
                            lambda name: SimpleNamespace(pw_uid=uid + 1, pw_gid=os.getgid())
                            if name == "summarizer" else
                            SimpleNamespace(pw_uid=reader, pw_gid=os.getgid()))
        reason = "root ownership"
    elif mutate == "orphan_temp":
        (root / "output" / ".obsidian-cas-deadbeef.tmp").write_bytes(b"uncommitted")
    elif mutate == "unknown_stage":
        (root / "unknown").mkdir()
    elif mutate == "wrong_artifact_class":
        wrong = root / "output" / ("b" * 64 + ".github-daily-ground-context.json")
        wrong.write_bytes(b"unreviewed class")
    with pytest.raises(audit.DailyCASAdmissionError, match=reason):
        _audit(root)


def test_empty_root_and_bounded_files_fail_closed(tmp_path, monkeypatch):
    root = tmp_path / "empty"
    root.mkdir()
    root.chmod(0o750)
    monkeypatch.setattr(audit.pwd, "getpwnam",
                        lambda name: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
                        if name == "summarizer" else SimpleNamespace(pw_uid=os.getuid() + 1))
    with pytest.raises(audit.DailyCASAdmissionError, match="no published"):
        _audit(root)
    with pytest.raises(audit.DailyCASAdmissionError, match="limit invalid"):
        audit.audit_daily_summary_cas(
            root, owner_name="summarizer", reader_name="renderer", max_files=0
        )
