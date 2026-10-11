"""Read-only, fail-closed permission/ACL admission for Daily Summary CAS (#292).

This is a point-in-time operational diagnostic, not a lock, writer, CAS repair,
or a substitute for the trusted ancestor/ACL-stability contract.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import pwd
import re
import stat
import struct
from pathlib import Path

VERSION = 1
MAX_FILES = 65536
MAX_FILE_BYTES = 2 * 1024 * 1024
SHA_FILENAME = re.compile(r"^[0-9a-f]{64}\.github-daily-[a-z0-9-]+\.json$")
ALLOWED_STAGE_SUFFIXES = {
    "context": frozenset({"partial-context", "reduce-context", "ground-context"}),
    "output": frozenset({"partial-output", "reduce-output", "ground-output"}),
    "provenance": frozenset({"inference"}),
    "final": frozenset({"grounded-summary"}),
    "resume": frozenset({"resume"}),
}
ALLOWED_STAGE_DIRS = frozenset(ALLOWED_STAGE_SUFFIXES)
# Linux posix_acl_xattr_entry tags. All requested permissions are *effective*
# (i.e., named ACL entries are constrained by the ACL mask).
USER_OBJ, USER, GROUP_OBJ, GROUP, MASK, OTHER = 1, 2, 4, 8, 16, 32
ACL_VERSION = 2


class DailyCASAdmissionError(ValueError):
    """The metadata does not establish the configured CAS admission policy."""


def _acl_from_fd(fd: int) -> dict[tuple[int, int | None], int]:
    try:
        raw = os.getxattr(fd, "system.posix_acl_access")
    except OSError as exc:
        if exc.errno == errno.ENODATA:
            raise DailyCASAdmissionError("required named-user ACL missing") from exc
        raise DailyCASAdmissionError("CAS ACL metadata unreadable") from exc
    if (len(raw) < 4 or (len(raw) - 4) % 8
            or struct.unpack_from("<I", raw, 0)[0] != ACL_VERSION):
        raise DailyCASAdmissionError("CAS ACL xattr is invalid")
    entries: dict[tuple[int, int | None], int] = {}
    for offset in range(4, len(raw), 8):
        tag, perms, uid = struct.unpack_from("<HHI", raw, offset)
        if tag not in (USER_OBJ, USER, GROUP_OBJ, GROUP, MASK, OTHER) or perms & ~7:
            raise DailyCASAdmissionError("CAS ACL contains an unsupported entry")
        if tag in (USER, GROUP):
            if uid == 0xFFFFFFFF:
                raise DailyCASAdmissionError("named ACL identity missing")
            key = (tag, uid)
        else:
            if uid != 0xFFFFFFFF:
                raise DailyCASAdmissionError("base ACL entry has an unexpected ID")
            key = (tag, None)
        if key in entries:
            raise DailyCASAdmissionError("CAS ACL contains duplicate entries")
        entries[key] = perms
    return entries


def _verify_acl(fd: int, *, owner_uid: int, reader_uid: int) -> None:
    entries = _acl_from_fd(fd)
    expected_keys = {
        (USER_OBJ, None), (GROUP_OBJ, None), (MASK, None),
        (OTHER, None), (USER, owner_uid), (USER, reader_uid),
    }
    if owner_uid == reader_uid or set(entries) != expected_keys:
        raise DailyCASAdmissionError("CAS named-reader ACL differs from approved policy")
    mask = entries[(MASK, None)]
    if (entries[(USER_OBJ, None)] != 0o6 or entries[(GROUP_OBJ, None)] != 0
            or mask != 0o4 or entries[(OTHER, None)] != 0):
        raise DailyCASAdmissionError("CAS ACL base permissions differ from policy")
    if any(entries[(USER, uid)] & mask != 0o4 for uid in (owner_uid, reader_uid)):
        raise DailyCASAdmissionError("CAS named-reader effective rights differ from policy")


def audit_daily_summary_cas(
    root: Path, *, owner_name: str, reader_name: str,
    max_files: int = MAX_FILES,
) -> dict[str, object]:
    """Attest only local inode metadata; never read source/model/claim bytes.

    Owner and reader are *literal* system role names, not numeric IDs that
    survive host migration. The expected 0640/named-reader ACL is intentionally
    specific to the reviewed production Daily Summary service.
    """
    if type(max_files) is not int or not 1 <= max_files <= MAX_FILES:
        raise DailyCASAdmissionError("CAS audit file limit invalid")
    try:
        owner = pwd.getpwnam(owner_name)
        reader = pwd.getpwnam(reader_name)
    except (KeyError, TypeError) as exc:
        raise DailyCASAdmissionError("required service identity unavailable") from exc
    if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
        raise DailyCASAdmissionError("CAS root path invalid")
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise DailyCASAdmissionError("CAS root cannot be inspected") from exc
    if (not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != owner.pw_uid or metadata.st_gid != owner.pw_gid
            or stat.S_IMODE(metadata.st_mode) & 0o007
            or stat.S_IMODE(metadata.st_mode) & 0o020):
        raise DailyCASAdmissionError("CAS root ownership or mode not admitted")

    files = dirs = 0
    with os.scandir(root) as stages:
        stage_entries = list(stages)
    for stage in stage_entries:
        if stage.name not in ALLOWED_STAGE_DIRS or not stage.is_dir(follow_symlinks=False):
            raise DailyCASAdmissionError("CAS contains an unexpected stage directory/entry")
        st = stage.stat(follow_symlinks=False)
        if (st.st_uid != owner.pw_uid or st.st_gid != owner.pw_gid
                or stat.S_IMODE(st.st_mode) & 0o027):
            raise DailyCASAdmissionError("CAS stage directory mode/owner untrusted")
        dirs += 1
        with os.scandir(stage.path) as entries:
            for entry in entries:
                if (not SHA_FILENAME.fullmatch(entry.name)
                        or not any(
                            entry.name.endswith(f".github-daily-{suffix}.json")
                            for suffix in ALLOWED_STAGE_SUFFIXES[stage.name]
                        )):
                    raise DailyCASAdmissionError("CAS has unknown or uncommitted artifact name")
                flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0)
                         | getattr(os, "O_CLOEXEC", 0))
                try:
                    fd = os.open(entry.path, flags)
                except OSError as exc:
                    raise DailyCASAdmissionError("CAS file cannot be safely opened") from exc
                try:
                    info = os.fstat(fd)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_uid != owner.pw_uid
                            or info.st_gid != owner.pw_gid
                            or stat.S_IMODE(info.st_mode) != 0o640
                            or not 0 <= info.st_size <= MAX_FILE_BYTES):
                        raise DailyCASAdmissionError("CAS published inode metadata violates policy")
                    _verify_acl(fd, owner_uid=owner.pw_uid, reader_uid=reader.pw_uid)
                    files += 1
                    if files > max_files:
                        raise DailyCASAdmissionError("CAS audit size limit exceeded")
                finally:
                    os.close(fd)
    if files == 0:
        raise DailyCASAdmissionError("CAS namespace has no published artifacts")
    # This is deliberately a point-in-time report. Files can change *after*
    # scanning; the operator must independently bind an approved rollout window.
    return {
        "record_version": VERSION, "audit": "daily-cas-metadata-v1",
        "status": "admitted_at_inspection", "published_files": files,
        "stage_directories": dirs, "files_read": 0, "files_changed": 0,
        "publication_authorized": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Daily CAS metadata admission")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--owner-name", required=True)
    parser.add_argument("--reader-name", required=True)
    parser.add_argument("--max-files", type=int, default=MAX_FILES)
    args = parser.parse_args(argv)
    try:
        result = audit_daily_summary_cas(
            args.root, owner_name=args.owner_name, reader_name=args.reader_name,
            max_files=args.max_files,
        )
    except DailyCASAdmissionError as exc:
        print(json.dumps({"record_version": VERSION, "audit": "daily-cas-metadata-v1",
                          "status": "blocked", "reason": str(exc),
                          "publication_authorized": False}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
