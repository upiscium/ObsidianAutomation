"""Inert privileged-reader admission for preprovisioned B3 evidence snapshots.

Only a future root installer may create the fixed files. A dedicated
non-Writer verifier role can inspect them; the HTTP caller selects nothing.
Local root-owned DAC provenance is NOT Nextcloud signature verification,
not proof of old-host revocation, not a durable replay checkpoint, and
never cutover authority. No file creation, credentials/network, or Writer.
"""
from __future__ import annotations

import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat

from .github_b3_authenticated_evidence import (
    B3DetachedIssuerKeys, B3DetachedSequenceFloor,
    assess_b3_authenticated_evidence,
)

ROOT = Path("/etc/obsidian-b3-retirement-evidence")
TRUST_ANCHOR = Path("/")
READER_ROLE = "obsidian-b3-evidence-verifier"
READER_GROUP = "obsidian-b3-evidence-verifier"
MANIFEST = "snapshot.json"
KEYS = "issuer-keys.json"
FLOOR = "replay-floor.json"
PROVIDER = "provider-envelope.json"
HOST = "old-host-envelope.json"
_SOURCE_NAMES = (KEYS, FLOOR, PROVIDER, HOST)
_LIMITS = {MANIFEST: 2048, KEYS: 1024, FLOOR: 512,
           PROVIDER: 12 * 1024, HOST: 12 * 1024}
VERSION = "github-b3-pinned-retirement-source/v1"
_KEYS_VERSION = "github-b3-detached-keys/v1"
_FLOOR_VERSION = "github-b3-detached-floor/v1"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC


class B3PinnedEvidenceUnavailable(RuntimeError):
    """Fixed diagnostic; never print paths, keys or privileged data."""

    def __init__(self) -> None:
        super().__init__("pinned_evidence_source_unavailable")


def _owner_uid() -> int:
    return 0


def _verifier_ids() -> tuple[int, int]:
    uid = pwd.getpwnam(READER_ROLE).pw_uid
    gid = grp.getgrnam(READER_GROUP).gr_gid
    if uid <= 0 or gid <= 0 or uid == _owner_uid() or os.geteuid() != uid:
        raise B3PinnedEvidenceUnavailable()
    return uid, gid


def _identity(st: os.stat_result) -> tuple[int, ...]:
    return (
        st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_uid,
        st.st_gid, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
    )


def _open_root(*, owner: int, group: int) -> int:
    if (
        type(ROOT) is not type(Path("/"))
        or type(TRUST_ANCHOR) is not type(Path("/"))
        or not ROOT.is_absolute() or not TRUST_ANCHOR.is_absolute()
        or ROOT == TRUST_ANCHOR
        or not ROOT.is_relative_to(TRUST_ANCHOR)
        or ".." in ROOT.parts or ".." in TRUST_ANCHOR.parts
    ):
        raise B3PinnedEvidenceUnavailable()
    fd = os.open(TRUST_ANCHOR, _DIR_FLAGS)
    try:
        start = os.fstat(fd)
        if (
            not stat.S_ISDIR(start.st_mode)
            or start.st_uid != owner
            or stat.S_IMODE(start.st_mode) & 0o022
        ):
            raise B3PinnedEvidenceUnavailable()
        for component in ROOT.relative_to(TRUST_ANCHOR).parts:
            next_fd = os.open(component, _DIR_FLAGS, dir_fd=fd)
            try:
                info = os.fstat(next_fd)
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != owner
                    or stat.S_IMODE(info.st_mode) & 0o022
                ):
                    raise B3PinnedEvidenceUnavailable()
            except BaseException:
                os.close(next_fd)
                raise
            os.close(fd)
            fd = next_fd
        final = os.fstat(fd)
        if (final.st_uid != owner or final.st_gid != group
                or stat.S_IMODE(final.st_mode) != 0o750):
            raise B3PinnedEvidenceUnavailable()
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def _read_fixed(fd: int, name: str, *, owner: int, group: int) -> bytes:
    # All names are fixed literals. No path fragment may be caller-selected.
    if name not in _LIMITS or type(name) is not str:
        raise B3PinnedEvidenceUnavailable()
    file_fd = os.open(name, _FILE_FLAGS, dir_fd=fd)
    try:
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != owner or before.st_gid != group
            or stat.S_IMODE(before.st_mode) != 0o640
            or not 0 < before.st_size <= _LIMITS[name]
        ):
            raise B3PinnedEvidenceUnavailable()
        raw = os.read(file_fd, _LIMITS[name] + 1)
        after = os.fstat(file_fd)
        path = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if (
            len(raw) != before.st_size
            or _identity(before) != _identity(after)
            or _identity(before) != _identity(path)
        ):
            raise B3PinnedEvidenceUnavailable()
        return raw
    finally:
        os.close(file_fd)


def _record(raw: bytes) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for name, value in pairs:
            if name in result:
                raise B3PinnedEvidenceUnavailable()
            result[name] = value
        return result

    def bad_constant(_: str) -> object:
        raise B3PinnedEvidenceUnavailable()

    data = json.loads(raw, object_pairs_hook=unique,
                      parse_constant=bad_constant)
    if type(data) is not dict:
        raise B3PinnedEvidenceUnavailable()
    canonical = (
        json.dumps(data, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")
    if raw != canonical:
        raise B3PinnedEvidenceUnavailable()
    return data


def _id(value: object, pattern: re.Pattern[str] = _ID) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _source_spec(manifest: bytes, source: dict[str, bytes]) -> tuple[
    B3DetachedIssuerKeys, B3DetachedSequenceFloor,
]:
    index = _record(manifest)
    if (set(index) != {"schema_version", "generation", "sha256"}
            or index["schema_version"] != VERSION
            or not _id(index["generation"])
            or type(index["sha256"]) is not dict
            or set(index["sha256"]) != set(_SOURCE_NAMES)):
        raise B3PinnedEvidenceUnavailable()

    for name in _SOURCE_NAMES:
        checksum = index["sha256"][name]
        if (not _id(checksum, _SHA)
                or hashlib.sha256(source[name]).hexdigest() != checksum):
            raise B3PinnedEvidenceUnavailable()

    keys = _record(source[KEYS])
    floor = _record(source[FLOOR])
    if (
        set(keys) != {
            "schema_version", "generation", "provider_key_id", "host_key_id",
            "provider_key_hex", "host_key_hex",
        }
        or keys["schema_version"] != _KEYS_VERSION
        or keys["generation"] != index["generation"]
        or not _id(keys["provider_key_id"])
        or not _id(keys["host_key_id"])
        or not _id(keys["provider_key_hex"], _SHA)
        or not _id(keys["host_key_hex"], _SHA)
        or set(floor) != {
            "schema_version", "generation",
            "provider_sequence", "host_sequence",
        }
        or floor["schema_version"] != _FLOOR_VERSION
        or floor["generation"] != index["generation"]
        or any(type(floor[k]) is not int or not 0 <= floor[k] < (1 << 63)
               for k in ("provider_sequence", "host_sequence"))
    ):
        raise B3PinnedEvidenceUnavailable()

    return (
        B3DetachedIssuerKeys(
            provider_key_id=keys["provider_key_id"],
            host_key_id=keys["host_key_id"],
            provider_key=bytes.fromhex(keys["provider_key_hex"]),
            host_key=bytes.fromhex(keys["host_key_hex"]),
        ),
        B3DetachedSequenceFloor(
            provider_sequence=floor["provider_sequence"],
            host_sequence=floor["host_sequence"],
        ),
    )


def assess_pinned_b3_retirement_evidence(
    *, binding_ref: str, revision: str, expected_generation: str,
    now: float,
) -> dict[str, object]:
    """Read fixed owner-provisioned sources and return redacted verification.

    No path, key, snapshot or checkpoint comes from WebUI input. Neither
    a positive local DAC check nor HMAC proves who generated a receipt, and
    the floor is NOT advanced. This function never authorizes any effect.
    """
    def unavailable() -> dict[str, object]:
        return {
            "schema_version": VERSION, "state": "unknown",
            "reason": "pinned_evidence_source_unavailable",
            "cutover_authorized": False,
        }

    fd = None
    try:
        _, group = _verifier_ids()
        owner = _owner_uid()
        fd = _open_root(owner=owner, group=group)
        root = _identity(os.fstat(fd))
        names = (MANIFEST, *_SOURCE_NAMES)
        before = {
            name: _identity(os.stat(
                name, dir_fd=fd, follow_symlinks=False,
            )) for name in names
        }
        manifest = _read_fixed(fd, MANIFEST, owner=owner, group=group)
        source = {
            name: _read_fixed(fd, name, owner=owner, group=group)
            for name in _SOURCE_NAMES
        }

        # Verify the same *inodes*, modes, and bytes are still pinned.
        # A rename+replacement with identical bytes is NOT a stable read.
        # Only an external privileged installer can guarantee the root is
        # not rotated just after these checks; no effect is authorized.
        if manifest != _read_fixed(fd, MANIFEST, owner=owner, group=group):
            raise B3PinnedEvidenceUnavailable()
        for name in _SOURCE_NAMES:
            if source[name] != _read_fixed(fd, name, owner=owner, group=group):
                raise B3PinnedEvidenceUnavailable()
        after = {
            name: _identity(os.stat(
                name, dir_fd=fd, follow_symlinks=False,
            )) for name in names
        }
        if before != after:
            raise B3PinnedEvidenceUnavailable()
        again = _open_root(owner=owner, group=group)
        try:
            if _identity(os.fstat(again)) != root:
                raise B3PinnedEvidenceUnavailable()
        finally:
            os.close(again)

        keys, floor = _source_spec(manifest, source)
        result = assess_b3_authenticated_evidence(
            source[PROVIDER], source[HOST],
            trusted_issuer_keys=keys, trusted_sequence_floor=floor,
            binding_ref=binding_ref, revision=revision,
            expected_generation=expected_generation, now=now,
        )
        if (
            type(result) is not dict
            or result.get("cutover_authorized") is not False
            or result.get("state") not in ("unknown", "blocked", "candidate")
            or type(result.get("reason")) is not str
        ):
            raise B3PinnedEvidenceUnavailable()
        # Preserve one public, bounded schema for success and refusal.
        return {
            "schema_version": VERSION, "state": result["state"],
            "reason": result["reason"], "cutover_authorized": False,
        }
    except Exception:
        return unavailable()
    finally:
        if fd is not None:
            os.close(fd)
