"""Opt-in, nonwired Collector-owned Daily v2 source CAS transaction (#336).

This module does not collect remote data and has no CLI, scheduler, model,
Nextcloud, Vault or Writer integration. Hashes alone never prove producer
identity. A local reader additionally checks a trusted host UID/GID and ACL
policy on preprovisioned CAS storage; this does not authenticate GitHub data
against a compromised Collector service identity.
"""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import stat
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import github_daily_source_route as source
from . import github_daily_route_capture_index as index
from .artifact_lifecycle import (
    _decode_json_object,
    ArtifactLifecycleError,
)
from .github_daily_cas_admission import (
    DailyCASAdmissionError,
    _acl_from_fd,
    USER_OBJ, USER, GROUP_OBJ, MASK, OTHER,
)

COLLECTOR_ACCOUNT = "obsidian-github-collector"
READER_ACCOUNT = "obsidian-github-summarizer"
# Administrator-preprovisioned; never selected by a model, client or CLI.
ROOT = Path("/var/lib/obsidian-github-pipeline/daily-progress/15-VerifiedSource")
TRUST_ANCHOR = Path("/")  # Tests replace this only for an isolated fixture.
STAGES = {"evidence": "evidence", "receipt": "source-route", "capture": "capture-index"}
FILE_LIMITS = {"evidence": 32 * 1024 * 1024, "receipt": 4 * 1024 * 1024,
               "capture": index.MAX_CAPTURE_INDEX_BYTES}
# This is a bounded wall-clock policy, not a cryptographic same-invocation proof.
MAX_INITIAL_CAPTURE_AGE = timedelta(hours=1)
MAX_FUTURE_SKEW = timedelta(seconds=60)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DIRECTORY_FLAGS = (os.O_RDONLY | os.O_DIRECTORY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0))
_FILE_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
               | getattr(os, "O_NONBLOCK", 0)
               | getattr(os, "O_CLOEXEC", 0))


class DailyRouteCASError(ValueError):
    """Source CAS publication or reader attestation could not be accepted."""


@dataclass(frozen=True)
class CollectorRoles:
    owner_uid: int
    owner_gid: int
    reader_uid: int


@dataclass(frozen=True)
class PublishedCapture:
    index_sha256: str
    evidence_sha256: str
    receipt_sha256: str
    # Publication here means an immutable *local* CAS transaction, not a Daily.
    daily_publication_authorized: bool = False


@dataclass(frozen=True)
class VerifiedCapture:
    index_sha256: str
    evidence_bytes: bytes
    receipt_bytes: bytes
    capture_index: index.CaptureIndex
    routes: tuple[dict[str, object], ...]
    # Conditional on trusted host service identities, filesystem and ACL.
    collector_storage_attested: bool = True
    daily_publication_authorized: bool = False


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _roles() -> CollectorRoles:
    try:
        writer = pwd.getpwnam(COLLECTOR_ACCOUNT)
        reader = pwd.getpwnam(READER_ACCOUNT)
    except (KeyError, TypeError):
        raise DailyRouteCASError("required Collector or reader identity is unavailable") from None
    if (writer.pw_uid == 0 or writer.pw_gid == 0 or reader.pw_uid == 0
            or writer.pw_uid == reader.pw_uid):
        raise DailyRouteCASError("Collector and reader identities are not isolated")
    return CollectorRoles(writer.pw_uid, writer.pw_gid, reader.pw_uid)


def _require_role(roles: CollectorRoles, *, writer: bool) -> None:
    if writer:
        if (os.geteuid(), os.getegid()) != (roles.owner_uid, roles.owner_gid):
            raise DailyRouteCASError("only the configured Collector may publish source CAS")
    elif os.geteuid() not in (roles.reader_uid, roles.owner_uid):
        raise DailyRouteCASError("source CAS reader identity not admitted")


def _validate_directory(fd: int, roles: CollectorRoles) -> None:
    meta = os.fstat(fd)
    if (not stat.S_ISDIR(meta.st_mode)
            or meta.st_uid != roles.owner_uid or meta.st_gid != roles.owner_gid
            or stat.S_IMODE(meta.st_mode) != 0o750):
        raise DailyRouteCASError("source CAS directory owner/mode is untrusted")
    required = {
        (USER_OBJ, None), (GROUP_OBJ, None),
        (MASK, None), (OTHER, None), (USER, roles.reader_uid),
    }
    for attr, mask in (("system.posix_acl_access", 0o5),
                       ("system.posix_acl_default", 0o7)):
        try:
            acl = _acl_from_fd(fd, attr=attr)
        except DailyCASAdmissionError:
            raise DailyRouteCASError("source CAS directory ACL unavailable") from None
        if (set(acl) != required or acl[(USER_OBJ, None)] != 0o7
                or acl[(GROUP_OBJ, None)] != 0
                or acl[(OTHER, None)] != 0
                or acl[(MASK, None)] != mask
                or acl[(USER, roles.reader_uid)] != 0o5):
            raise DailyRouteCASError("source CAS directory ACL mismatch")


def _directory_handles(roles: CollectorRoles) -> dict[str, int]:
    """Pin each no-follow component from /; never trust callers' path strings.

    Nonroot ancestors must be owned by the designated Collector and cannot be
    group/world writable. Its same-UID compromise and system root are outside
    the adversarial storage model. All handles are returned only after checks.
    """
    if (not ROOT.is_absolute() or ".." in ROOT.parts or ROOT == Path("/")
            or not TRUST_ANCHOR.is_absolute() or not ROOT.is_relative_to(TRUST_ANCHOR)
            or ROOT == TRUST_ANCHOR):
        raise DailyRouteCASError("source CAS root is not a trusted absolute path")
    handles: dict[str, int] = {}
    anchor: int | None = None
    try:
        anchor = os.open(TRUST_ANCHOR, _DIRECTORY_FLAGS)
        pieces = ROOT.relative_to(TRUST_ANCHOR).parts
        if not pieces:
            raise DailyRouteCASError("invalid Collector storage location")
        for i, piece in enumerate(pieces):
            child = os.open(piece, _DIRECTORY_FLAGS, dir_fd=anchor)
            os.close(anchor)
            anchor = child
            mode = os.fstat(anchor)
            if i < len(pieces) - 1:
                if (not stat.S_ISDIR(mode.st_mode)
                        or mode.st_uid not in (0, roles.owner_uid)
                        or (stat.S_IMODE(mode.st_mode) & 0o022) != 0):
                    raise DailyRouteCASError("untrusted Collector CAS ancestor")
        _validate_directory(anchor, roles)
        handles["root"] = anchor
        anchor = None
        for key in STAGES:
            stage_fd = os.open(key, _DIRECTORY_FLAGS, dir_fd=handles["root"])
            try:
                _validate_directory(stage_fd, roles)
            except BaseException:
                os.close(stage_fd)
                raise
            handles[key] = stage_fd
        return handles
    except (OSError, DailyCASAdmissionError, ArtifactLifecycleError):
        raise DailyRouteCASError("source CAS path/ACL inspection failed") from None
    finally:
        if anchor is not None:
            os.close(anchor)
        # On error, close all already-open handles.
        if len(handles) != 4:
            for fd in handles.values():
                os.close(fd)
            handles.clear()


def _close_handles(handles: dict[str, int]) -> None:
    for fd in handles.values():
        os.close(fd)
    handles.clear()


def _filename(stage: str, sha256: str) -> str:
    if stage not in STAGES or type(sha256) is not str or not _SHA256_RE.fullmatch(sha256):
        raise DailyRouteCASError("invalid source CAS object identity")
    return f"{sha256}.github-daily-{STAGES[stage]}.json"


def _admit_inode(fd: int, roles: CollectorRoles, *, limit: int) -> None:
    meta = os.fstat(fd)
    if (not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1
            or meta.st_uid != roles.owner_uid or meta.st_gid != roles.owner_gid
            or stat.S_IMODE(meta.st_mode) != 0o640
            or not 0 < meta.st_size <= limit):
        raise DailyRouteCASError("source CAS inode metadata does not meet policy")
    try:
        acl = _acl_from_fd(fd)
    except DailyCASAdmissionError:
        raise DailyRouteCASError("source CAS object ACL unavailable") from None
    wanted = {(USER_OBJ, None), (GROUP_OBJ, None), (MASK, None),
              (OTHER, None), (USER, roles.reader_uid)}
    if (set(acl) != wanted or acl[(USER_OBJ, None)] != 0o6
            or acl[(GROUP_OBJ, None)] != 0
            or acl[(MASK, None)] != 0o4 or acl[(OTHER, None)] != 0
            or (acl[(USER, roles.reader_uid)] & acl[(MASK, None)]) != 0o4):
        raise DailyRouteCASError("source CAS object ACL mismatch")


def _read_object(stage_fd: int, stage: str, sha256: str, roles: CollectorRoles) -> bytes:
    name = _filename(stage, sha256)
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=stage_fd)
    except OSError:
        raise DailyRouteCASError("source CAS object unavailable") from None
    try:
        limit = FILE_LIMITS[stage]
        _admit_inode(fd, roles, limit=limit)
        chunks: list[bytes] = []
        read_size = 0
        while True:
            data = os.read(fd, min(65536, limit + 1 - read_size))
            if not data:
                break
            chunks.append(data)
            read_size += len(data)
            if read_size > limit:
                raise DailyRouteCASError("source CAS read exceeded object bound")
        result = b"".join(chunks)
        if _digest(result) != sha256:
            raise DailyRouteCASError("source CAS content hash does not match name")
        # Detect an authorized same-UID mutation during an open read, though
        # concurrent same-UID replacement remains outside the threat model.
        _admit_inode(fd, roles, limit=limit)
        if os.fstat(fd).st_size != len(result):
            raise DailyRouteCASError("source CAS inode changed during read")
        return result
    except OSError:
        raise DailyRouteCASError("source CAS object read failed") from None
    finally:
        os.close(fd)


def _utc_now() -> datetime:
    """Trusted host wall clock for first-commit freshness admission."""
    return datetime.now(timezone.utc)


def _require_initial_freshness(record: index.CaptureIndex) -> None:
    """Never first-commit an old capture as though source recheck just ran.

    A local clock bound supplements, but does not authenticate, the Collector
    invocation. Already fully committed identical captures may be replayed.
    """
    try:
        captured = datetime.fromisoformat(
            record.captured_at_utc.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        now = _utc_now().astimezone(timezone.utc)
        age = now - captured
        if age < -MAX_FUTURE_SKEW or age > MAX_INITIAL_CAPTURE_AGE:
            raise DailyRouteCASError("Collector source capture is too old or in the future")
        if record.collection_mode == "scheduled_recent_api_reverified":
            today = now.astimezone(ZoneInfo(source.activity.CANONICAL_TIMEZONE)).date()
            if date.fromisoformat(record.date) not in (today, today - timedelta(days=1)):
                raise DailyRouteCASError("scheduled source capture no longer targets a recent JST day")
    except DailyRouteCASError:
        raise
    except (TypeError, ValueError, OverflowError):
        raise DailyRouteCASError("Collector capture time could not be admitted") from None


def publish_fresh_capture(collection: source.FreshRouteCollection) -> PublishedCapture:
    """Collector-only. Publish exact Evidence + receipt first, index *last*.

    A crash before final-index publication may leave uncommitted CAS blobs.
    No consumer may treat those partial sets as a transaction.
    """
    roles = _roles()
    _require_role(roles, writer=True)
    try:
        record = index.build_capture_index(collection)
        index.parse_capture_index(
            record.canonical_bytes, collection.evidence.canonical_bytes,
            collection.receipt_bytes,
        )
    except (index.DailyCaptureIndexError, ValueError, TypeError):
        raise DailyRouteCASError("source capture failed prepublication verification") from None
    # Stage names/path pieces are constant; SHA names come only from validated
    # byte identities, never from remote API URLs or untrusted model text.
    handles = _directory_handles(roles)
    try:
        # A completed, hash-identical capture may be replayed much later.
        # But a first-time commit must use a recently reverified source.
        manifest_name = _filename("capture", record.sha256)
        try:
            os.stat(manifest_name, dir_fd=handles["capture"], follow_symlinks=False)
        except FileNotFoundError:
            _require_initial_freshness(record)
        except OSError:
            raise DailyRouteCASError("source CAS manifest lookup failed") from None
        else:
            existing = read_verified_capture(record.sha256)
            if (existing.capture_index != record
                    or existing.evidence_bytes != collection.evidence.canonical_bytes
                    or existing.receipt_bytes != collection.receipt_bytes):
                raise DailyRouteCASError("stored source capture does not match retry")
            return PublishedCapture(
                index_sha256=record.sha256, evidence_sha256=record.evidence_sha256,
                receipt_sha256=record.route_receipt_sha256,
            )
        for stage, sha256, data in (
            ("evidence", record.evidence_sha256, collection.evidence.canonical_bytes),
            ("receipt", record.route_receipt_sha256, collection.receipt_bytes),
            ("capture", record.sha256, record.canonical_bytes),
        ):
            if stage == "capture":
                # Source may become stale while the first two CAS objects are
                # fsynced. Check again immediately before final index write.
                _require_initial_freshness(record)
            # Reuse atomic, no-overwrite CAS from merged main #293. Refuse a
            # branch/installation still carrying the old direct-final O_EXCL.
            from . import artifact_lifecycle as lifecycle
            if not callable(getattr(lifecycle, "_rename_noreplace", None)):
                raise DailyRouteCASError("safe CAS publication primitive unavailable")
            path = ROOT / stage / _filename(stage, sha256)
            try:
                lifecycle._store_immutable(path, data)
            except (OSError, ArtifactLifecycleError):
                raise DailyRouteCASError("atomic source CAS publication failed") from None
            # Verify observed published inode and bytes, not just a returned
            # CAS path or a prior same-hash short-circuit.
            if _read_object(handles[stage], stage, sha256, roles) != data:
                raise DailyRouteCASError("published source CAS bytes changed")
        return PublishedCapture(
            index_sha256=record.sha256, evidence_sha256=record.evidence_sha256,
            receipt_sha256=record.route_receipt_sha256,
        )
    finally:
        _close_handles(handles)


def read_verified_capture(capture_sha256: str) -> VerifiedCapture:
    """Read one exact digest from the trusted Collector storage namespace.

    The digest is a local approved capture reference; this function does not
    authorize arbitrary caller-supplied historical promotion or Daily Writer.
    """
    _filename("capture", capture_sha256)
    roles = _roles()
    _require_role(roles, writer=False)
    handles = _directory_handles(roles)
    try:
        manifest_bytes = _read_object(handles["capture"], "capture", capture_sha256, roles)
        try:
            manifest = _decode_json_object(manifest_bytes, label="capture index")
        except (ArtifactLifecycleError, ValueError, TypeError, RecursionError):
            raise DailyRouteCASError("source CAS capture index is malformed") from None
        if set(manifest) != {
            "record_version", "source_receipt_version", "date", "evidence_sha256",
            "route_receipt_sha256", "route_count", "collection_mode",
            "captured_at_utc", "producer_attestation_verified",
            "publication_authorized",
        } or manifest["producer_attestation_verified"] is not False or manifest["publication_authorized"] is not False:
            raise DailyRouteCASError("source CAS capture index schema mismatch")
        evidence_digest, route_digest = manifest["evidence_sha256"], manifest["route_receipt_sha256"]
        _filename("evidence", evidence_digest)
        _filename("receipt", route_digest)
        evidence_bytes = _read_object(handles["evidence"], "evidence", evidence_digest, roles)
        receipt_bytes = _read_object(handles["receipt"], "receipt", route_digest, roles)
        try:
            record = index.parse_capture_index(manifest_bytes, evidence_bytes, receipt_bytes)
            routes = source.parse_verified_routes(receipt_bytes, evidence_bytes)
        except (index.DailyCaptureIndexError, source.DailySourceRouteError):
            raise DailyRouteCASError("source CAS capture route verification failed") from None
        if record.sha256 != capture_sha256:
            raise DailyRouteCASError("source CAS capture identity changed")
        return VerifiedCapture(
            index_sha256=capture_sha256, evidence_bytes=evidence_bytes,
            receipt_bytes=receipt_bytes, capture_index=record, routes=routes,
        )
    finally:
        _close_handles(handles)
