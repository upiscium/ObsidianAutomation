"""Local-only, read-only observation of the existing GitHub Writer receipt store.

This is NOT attached to an HTTP handler, new-LXC service or shared dashboard.
The public entrypoint binds an operator-independent fixed path/UID/GID; never
forward a request path, UID, secret, or caller-supplied permission override.

Existing writer-owned receipt files attest a *historical producer outcome* under
the trusted local OS identity, not current remote Vault contents or the entire
GitHub Sync execution chain. No remote calls, jobs or state writes occur.
"""
from __future__ import annotations

import grp
import os
from pathlib import Path
import pwd
import re
import stat
from datetime import datetime

from .github_pipeline_evidence import (
    MAX_RECEIPT_BYTES,
    MAX_RECEIPTS_PER_KIND,
    InvalidEvidence,
    _empty,
    parse_producer_receipt,
    summarize_candidate_receipts,
)

_WRITER_USER = "obsidian-github-writer"
_WRITER_GROUP = "obsidian-github-pipeline"
_RECEIPT_ROOT = Path("/var/lib/obsidian-github-pipeline/27-Transport")
_DIR_ENTRIES_LIMIT = 4096
_RE_NAME = {
    "status": re.compile(r"([0-9a-f]{64})\.github-status\.transport-result\.json\Z"),
    "overview": re.compile(r"([0-9a-f]{64})\.github-overview\.transport-result\.json\Z"),
}
_SUFFIX = {
    "status": ".github-status.transport-result.json",
    "overview": ".github-overview.transport-result.json",
}


class SourceRejected(Exception):
    """All details are deliberately private; browser output uses fixed reason codes."""


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
        and left.st_mode == right.st_mode
        and left.st_uid == right.st_uid
        and left.st_gid == right.st_gid
    )


def _open_directory(directory: Path, *, uid: int, gid: int) -> int:
    """Traverse each component without following symlinks, even ancestors."""
    if not isinstance(directory, Path) or not directory.is_absolute():
        raise SourceRejected("invalid_root")
    if not hasattr(os, "O_NOFOLLOW"):
        raise SourceRejected("unsupported_os")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for piece in directory.parts[1:]:
            if piece in ("", ".", ".."):
                raise SourceRejected("invalid_root")
            newfd = os.open(piece, flags, dir_fd=fd)
            os.close(fd)
            fd = newfd
        meta = os.fstat(fd)
        if (
            not stat.S_ISDIR(meta.st_mode)
            or meta.st_uid != uid or meta.st_gid != gid
            or bool(meta.st_mode & 0o027)
        ):
            raise SourceRejected("invalid_root")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_regular_file(
    dirfd: int, name: str, *, uid: int, gid: int,
) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    listed = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
    if (
        not stat.S_ISREG(listed.st_mode)
        or listed.st_uid != uid or listed.st_gid != gid
        or listed.st_nlink != 1
        or bool(listed.st_mode & 0o027)
        or not 1 <= listed.st_size <= MAX_RECEIPT_BYTES
    ):
        raise SourceRejected("invalid_file")
    filefd = os.open(name, flags, dir_fd=dirfd)
    try:
        initial = os.fstat(filefd)
        if not _same_identity(listed, initial):
            raise SourceRejected("file_changed")
        payload = os.read(filefd, MAX_RECEIPT_BYTES + 1)
        final = os.fstat(filefd)
        directory_entry = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
        if (
            len(payload) != initial.st_size
            or not _same_identity(initial, final)
            or not _same_identity(initial, directory_entry)
        ):
            raise SourceRejected("file_changed")
        return payload, final
    finally:
        os.close(filefd)


def _unavailable(reason: str, now: datetime) -> dict[str, object]:
    result = summarize_candidate_receipts(
        {"status": [], "overview": []}, now=now,
    )
    result["status"] = _empty(reason)
    result["overview"] = _empty(reason)
    return result


def _read_root(
    directory: Path, *, uid: int, gid: int, now: datetime,
) -> dict[str, object]:
    """Internal fixture entrypoint, NOT a path-valued HTTP/API interface."""
    if (
        type(uid) is not int or uid < 0 or type(gid) is not int or gid < 0
        or not isinstance(now, datetime) or now.tzinfo is None
    ):
        raise ValueError("invalid fixed reader configuration")
    try:
        dirfd = _open_directory(directory, uid=uid, gid=gid)
    except (OSError, SourceRejected):
        return _unavailable("source_unavailable", now)
    try:
        before = os.fstat(dirfd)
        names: list[str] = []
        with os.scandir(dirfd) as entries:
            for entry in entries:
                if len(names) >= _DIR_ENTRIES_LIMIT:
                    return _unavailable("capacity_exceeded", now)
                names.append(entry.name)
        records: dict[str, list[bytes]] = {"status": [], "overview": []}
        observed_files: dict[str, os.stat_result] = {}
        rejected: dict[str, bool] = {"status": False, "overview": False}
        counts: dict[str, int] = {"status": 0, "overview": 0}
        for name in sorted(names):
            for kind, suffix in _SUFFIX.items():
                if not name.endswith(suffix):
                    continue
                counts[kind] += 1
                if counts[kind] > MAX_RECEIPTS_PER_KIND:
                    rejected[kind] = True
                    break
                m = _RE_NAME[kind].fullmatch(name)
                if m is None:
                    rejected[kind] = True
                    break
                try:
                    raw, file_stat = _read_regular_file(dirfd, name, uid=uid, gid=gid)
                    parsed = parse_producer_receipt(raw, kind=kind, now=now)
                    if parsed.file_identity != m.group(1):
                        raise SourceRejected("result_filename_binding")
                    records[kind].append(raw)
                    observed_files[name] = file_stat
                except (OSError, SourceRejected, InvalidEvidence):
                    rejected[kind] = True
                break
        for filename, saved_stat in observed_files.items():
            try:
                current = os.stat(filename, dir_fd=dirfd, follow_symlinks=False)
            except OSError:
                return _unavailable("source_unavailable", now)
            if not _same_identity(saved_stat, current):
                return _unavailable("source_unavailable", now)
        after = os.fstat(dirfd)
        if not _same_identity(before, after):
            return _unavailable("source_unavailable", now)
        result = summarize_candidate_receipts(
            records, now=now, source_attested=True,
        )
        for kind in ("status", "overview"):
            if rejected[kind]:
                result[kind] = _empty(
                    "capacity_exceeded" if counts[kind] > MAX_RECEIPTS_PER_KIND
                    else "source_invalid"
                )
        return result
    except OSError:
        return _unavailable("source_unavailable", now)
    finally:
        os.close(dirfd)


def read_local_github_pipeline_evidence(*, now: datetime) -> dict[str, object]:
    """Read only the known same-host writer-owned transport directory.

    On a fresh single-LXC deployment this directory may be absent; return
    unknown instead of reaching across hosts or treating old-LXC status as B3.
    """
    try:
        uid = pwd.getpwnam(_WRITER_USER).pw_uid
        gid = grp.getgrnam(_WRITER_GROUP).gr_gid
    except KeyError:
        return _unavailable("source_unavailable", now)
    return _read_root(_RECEIPT_ROOT, uid=uid, gid=gid, now=now)
