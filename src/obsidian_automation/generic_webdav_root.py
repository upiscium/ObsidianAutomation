"""Explicit HTTPS path-based authority for non-Nextcloud WebDAV roots.

Generic DAV does not offer Nextcloud's stable OC-ID/OCS identity. Instead an
operator must explicitly enroll the exact root after a read-only PROPFIND.
Each Job must re-verify the collection at the same authenticated HTTPS origin
and path before any effect. This offers URL/path binding, *not* inode or
server-instance continuity. Without an explicit enrollment: fail closed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import stat
from typing import Callable

from .snapshot_setup import SetupStore, _atomic
from .webdav_connection import probe_webdav_collection, rclone_vendor

FILE = "generic-webdav-root.json"
MAX_PROOF_BYTES = 2048
_EPOCH = re.compile(r"[a-f0-9]{32}\Z", re.ASCII)


def _current_epoch(store: SetupStore) -> str | None:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(store.webdav_epoch, flags)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as stream:
            meta = os.fstat(stream.fileno())
            if (not stat.S_ISREG(meta.st_mode)
                    or stat.S_IMODE(meta.st_mode) != 0o600
                    or meta.st_size != 32):
                return None
            data = stream.read(33)
        token = data.decode("ascii")
        return token if _EPOCH.fullmatch(token) else None
    except (OSError, UnicodeError):
        return None


def _source_digest(saved: dict) -> str:
    # Do not hash the password/token into a potentially brute-forceable
    # exposed display value. The account + exact origin/path and Vault root
    # are the scope of the operator's explicit path-based trust decision.
    identity = [saved["webdav_url"], saved["nextcloud_user"], saved["vault_folder"]]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _read_proof(path: Path) -> dict | None:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as stream:
            meta=os.fstat(stream.fileno())
            if (not stat.S_ISREG(meta.st_mode)
                    or stat.S_IMODE(meta.st_mode) != 0o600
                    or not 0 < meta.st_size <= MAX_PROOF_BYTES):
                return None
            raw = stream.read(MAX_PROOF_BYTES + 1)
        if len(raw) > MAX_PROOF_BYTES:
            return None
        data = json.loads(raw)
        return data if type(data) is dict else None
    except (OSError, UnicodeError, ValueError):
        return None


def _pin_compatible(store: SetupStore) -> bool:
    """Never downgrade an unreadable or identity-pinned OC-ID root."""
    from .vault_root_activation import (
        BINDING_FILE, PINNING_MARKER, _pin, _read_binding,
        _source_digest as existing_source_digest,
    )
    binding=store.state / BINDING_FILE
    marker=store.state / PINNING_MARKER
    if not any((binding.exists(), binding.is_symlink(),
                marker.exists(), marker.is_symlink())):
        return True
    # Prior Nextcloud activation must have passed an explicit connection
    # change fence and become an unpinned 'connection_saved' candidate.
    data=_read_binding(store.state)
    if (type(data) is not dict
            or set(data) != {"version", "current", "previous", "outcome",
                             "recorded_at", "source_sha256"}
            or data.get("outcome") != "connection_saved"
            or data.get("previous") is not None
            or binding.is_symlink() or marker.is_symlink()):
        return False
    # No marker means an ordinary connection_saved record has never begun
    # Nextcloud OC-ID pinning, and is a legitimate first generic setup.
    # Once a marker exists, it must still be the exact protected pinning
    # marker; an unreadable or malformed marker is never downgraded.
    if marker.exists():
        try:
            if (not marker.is_file()
                    or stat.S_IMODE(marker.stat().st_mode) != 0o600
                    or marker.read_bytes() != b"1\n"):
                return False
        except OSError:
            return False
    pin=_pin(data.get("current"))
    saved=store.load_nextcloud()
    return bool(pin and pin.chain is None and saved is not None
                and pin.path==saved["vault_folder"]
                and data.get("source_sha256")==existing_source_digest(saved))


def is_enrolled(store: SetupStore, saved: dict | None = None) -> bool:
    saved=saved if saved is not None else store.load_nextcloud()
    if saved is None or rclone_vendor(saved["webdav_url"]) != "other":
        return False
    data=_read_proof(store.state / FILE)
    epoch = _current_epoch(store)
    return bool(data is not None and epoch is not None
                and set(data) == {"version","authority","source_sha256","source_epoch"}
                and type(data["version"]) is int and data["version"]==1
                and data["authority"]=="https_path"
                and type(data["source_sha256"]) is str
                and data["source_sha256"]==_source_digest(saved)
                and data["source_epoch"]==epoch
                and _pin_compatible(store))


def enroll(
    store: SetupStore,
    *,
    probe: Callable[[dict], bool] = probe_webdav_collection,
) -> str:
    """Explicit operator action; one read-only provider probe, local 0600 write."""
    saved=store.load_nextcloud()
    if saved is None:
        return "binding_unavailable"
    if rclone_vendor(saved["webdav_url"])!="other":
        return "provider_unsupported"
    if not _pin_compatible(store):
        return "prior_root_pin_requires_reset"
    epoch = _current_epoch(store)
    if epoch is None:
        return "source_epoch_unavailable"
    try:
        if probe(saved) is not True:
            return "provider_unavailable"
    except Exception:
        return "provider_unavailable"
    # Credential/source mutation during the provider read must not bind a
    # stale endpoint. The store lock pairs the last comparison with the write.
    with store._lock:
        if (store.load_nextcloud()!=saved or not _pin_compatible(store)
                or _current_epoch(store)!=epoch):
            return "source_changed"
        try:
            _atomic(store.state / FILE,json.dumps({
                "version":1,"authority":"https_path",
                "source_sha256":_source_digest(saved),"source_epoch":epoch,
            },sort_keys=True))
        except OSError:
            return "enrollment_unrecorded"
    return "enrolled"


def check(
    store: SetupStore,
    *,
    probe: Callable[[dict], bool] = probe_webdav_collection,
) -> str:
    """Recheck exact HTTPS DAV collection on every canonical Job admission."""
    saved=store.load_nextcloud()
    if saved is None:
        return "binding_unavailable"
    if not is_enrolled(store,saved):
        return "provider_unverified"
    try:
        if probe(saved) is not True:
            return "provider_unavailable"
    except Exception:
        return "provider_unavailable"
    if store.load_nextcloud()!=saved or not is_enrolled(store):
        return "source_changed"
    return "ok"
