"""Unattached Unix-role-backed B3 producer admission (read-only, no Writer).

Trust is a privileged-provisioned, NOFOLLOW, independent `obsidian-github-sync`
owned directory and its exact root/role-owned ancestry. Unlike caller-supplied
hashes, the ordinary B3 WebUI request cannot create these files or substitute
their role. This is LOCAL Unix DAC provenance, NOT a remote cryptographic
signature, Vault freshness claim, or proof of old-LXC Writer retirement.

The service owner must separately provision the directory and producer/reader
permissions, install a trusted registration/secret reader and a real shared
Writer guard, then prove durable old-Writer exclusion before connecting this
reader to the existing unattached B3 port. This module provisions nothing.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import grp
import re
import stat

from .github_b3_request_binding import B3PreparedProposalPair
from .webui_b3_adapter import ProjectSyncRequest

ROOT = Path("/var/lib/obsidian-github-pipeline/29-B3VerifiedSource")
TRUST_ANCHOR = Path("/")
PRODUCER_ROLE = "obsidian-github-sync"
READER_ROLE = "obsidian-github-writer"
PIPELINE_GROUP = "obsidian-github-pipeline"
VERSION = 1
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_PAYLOAD_LIMIT = 4 * 1024 * 1024
_MANIFEST_LIMIT = 2048


class B3SourceUnavailable(RuntimeError):
    """Redacted refusal; never include raw file paths, bytes or credentials."""

    def __init__(self) -> None:
        super().__init__("producer_source_unavailable")


def _producer_uid() -> int:
    return pwd.getpwnam(PRODUCER_ROLE).pw_uid


def _pipeline_gid() -> int:
    return grp.getgrnam(PIPELINE_GROUP).gr_gid


def _verify_reader(producer_uid: int) -> None:
    reader_uid = pwd.getpwnam(READER_ROLE).pw_uid
    if (
        producer_uid <= 0 or reader_uid <= 0
        or producer_uid == reader_uid
        or os.geteuid() != reader_uid
    ):
        raise B3SourceUnavailable()


def _identity(st: os.stat_result) -> tuple[int, ...]:
    return (
        st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_uid,
        st.st_gid, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
    )


def _object(raw: bytes) -> dict[str, object]:
    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise B3SourceUnavailable()
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=no_duplicates)
    if type(value) is not dict:
        raise B3SourceUnavailable()
    canonical = (
        json.dumps(value, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")
    if canonical != raw:
        raise B3SourceUnavailable()
    return value


def _read_file(
    parent_fd: int, name: str, *, owner: int, group: int, max_bytes: int,
) -> bytes:
    fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
            or st.st_uid != owner or st.st_gid != group
            or stat.S_IMODE(st.st_mode) != 0o640
            or not 0 < st.st_size <= max_bytes
        ):
            raise B3SourceUnavailable()
        raw = os.read(fd, max_bytes + 1)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            len(raw) != st.st_size
            or _identity(st) != _identity(after)
            or _identity(st) != _identity(current)
        ):
            raise B3SourceUnavailable()
        return raw
    finally:
        os.close(fd)


def _trusted_dir(*, producer_uid: int, pipeline_gid: int) -> int:
    if (
        type(ROOT) is not type(Path("/"))
        or type(TRUST_ANCHOR) is not type(Path("/"))
        or not ROOT.is_absolute() or not TRUST_ANCHOR.is_absolute()
        or ROOT == TRUST_ANCHOR or not ROOT.is_relative_to(TRUST_ANCHOR)
        or ".." in ROOT.parts or ".." in TRUST_ANCHOR.parts
    ):
        raise B3SourceUnavailable()
    fd = os.open(TRUST_ANCHOR, _DIRECTORY_FLAGS)
    try:
        anchor = os.fstat(fd)
        if (
            not stat.S_ISDIR(anchor.st_mode)
            or (TRUST_ANCHOR == Path("/") and anchor.st_uid != 0)
            or stat.S_IMODE(anchor.st_mode) & 0o022
        ):
            raise B3SourceUnavailable()
        parts = ROOT.relative_to(TRUST_ANCHOR).parts
        for index, part in enumerate(parts):
            next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            try:
                st = os.fstat(next_fd)
                if (
                    not stat.S_ISDIR(st.st_mode)
                    or st.st_uid != (
                        producer_uid if index == len(parts) - 1
                        else (0 if TRUST_ANCHOR == Path("/") else producer_uid)
                    )
                    or stat.S_IMODE(st.st_mode) & 0o022
                ):
                    raise B3SourceUnavailable()
            except BaseException:
                os.close(next_fd)
                raise
            os.close(fd)
            fd = next_fd
        final = os.fstat(fd)
        if (
            final.st_uid != producer_uid
            or final.st_gid != pipeline_gid
            or stat.S_IMODE(final.st_mode) not in (0o700, 0o750)
        ):
            raise B3SourceUnavailable()
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def read_verified_b3_proposal_pair(request: ProjectSyncRequest) -> B3PreparedProposalPair:
    """Read exact-request role-protected manifest and SHA-addressed artifacts.

    Only the installed host identity/ACL is the provenance anchor; the returned
    dataclass has NO permission to authorize a B3 Job or retire an old Writer.
    """
    if type(request) is not ProjectSyncRequest:
        raise B3SourceUnavailable()
    try:
        request.__post_init__()
        if (
            type(request.requestId) is not str
            or _ID.fullmatch(request.requestId) is None
            or not all(
                type(x) is str and _ID.fullmatch(x) is not None
                for x in (request.bindingRef, request.expectedRevision)
            )
        ):
            raise B3SourceUnavailable()
        owner, group = _producer_uid(), _pipeline_gid()
        _verify_reader(owner)
        dirfd = _trusted_dir(producer_uid=owner, pipeline_gid=group)
        try:
            raw = _read_file(
                dirfd, f"{request.requestId}.b3-source.json",
                owner=owner, group=group, max_bytes=_MANIFEST_LIMIT,
            )
            m = _object(raw)
            if (
                set(m) != {
                    "record_version", "request_id", "binding_ref",
                    "expected_revision", "source_generation", "source_record_id",
                    "prepared_at", "status_sha256", "overview_sha256",
                }
                or type(m["record_version"]) is not int
                or m["record_version"] != VERSION
                or not all(
                    type(m[k]) is str and _ID.fullmatch(m[k]) is not None
                    for k in ("request_id", "binding_ref", "expected_revision",
                              "source_generation", "source_record_id")
                )
                or not all(
                    type(m[k]) is str and _SHA.fullmatch(m[k]) is not None
                    for k in ("status_sha256", "overview_sha256")
                )
                or type(m["prepared_at"]) not in (int, float)
                or not math.isfinite(m["prepared_at"])
                or not 0 <= m["prepared_at"] <= 253402300799
                or m["request_id"] != request.requestId
                or m["binding_ref"] != request.bindingRef
                or m["expected_revision"] != request.expectedRevision
            ):
                raise B3SourceUnavailable()
            status = _read_file(
                dirfd, f"{m['status_sha256']}.b3-status.json",
                owner=owner, group=group, max_bytes=_PAYLOAD_LIMIT,
            )
            overview = _read_file(
                dirfd, f"{m['overview_sha256']}.b3-overview.json",
                owner=owner, group=group, max_bytes=_PAYLOAD_LIMIT,
            )
            if (
                hashlib.sha256(status).hexdigest() != m["status_sha256"]
                or hashlib.sha256(overview).hexdigest() != m["overview_sha256"]
            ):
                raise B3SourceUnavailable()
            # Read the exact manifest a second time to catch races between
            # obtaining the pinned producer metadata and proposal bytes.
            again = _read_file(
                dirfd, f"{request.requestId}.b3-source.json",
                owner=owner, group=group, max_bytes=_MANIFEST_LIMIT,
            )
            if again != raw:
                raise B3SourceUnavailable()
            return B3PreparedProposalPair(
                request_id=m["request_id"],
                binding_ref=m["binding_ref"],
                expected_revision=m["expected_revision"],
                source_generation=m["source_generation"],
                source_record_id=m["source_record_id"],
                prepared_at=m["prepared_at"],
                status_bytes=status, overview_bytes=overview,
                status_sha256=m["status_sha256"],
                overview_sha256=m["overview_sha256"],
            )
        finally:
            os.close(dirfd)
    except Exception:
        raise B3SourceUnavailable() from None
