"""One optional, shared Nextcloud connection without any Gitea dependency.

The full B2 Snapshot source remains supported. A B4-only installation may
save only the four WebDAV fields, using the same app identity and local state.
If B2 is subsequently configured, its source becomes the single active source.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

from .snapshot_setup import SetupStore, _atomic, obscure_password

FIELDS = ("webdav_url", "vault_folder", "nextcloud_user", "nextcloud_password")
MARKER = "_rclone_sha256"


class NextcloudInUse(ValueError):
    """The active B2 source must be changed through B2 Connections."""


def validate_nextcloud(payload: object) -> dict[str, str]:
    if type(payload) is not dict or set(payload) != set(FIELDS):
        raise ValueError("invalid_nextcloud")
    if any(type(v) is not str or not v or len(v) > 512 or any(ord(c) < 32 for c in v)
           for v in payload.values()):
        raise ValueError("invalid_nextcloud")
    url = urlsplit(payload["webdav_url"])
    if (url.scheme != "https" or not url.hostname
            or not url.path.startswith("/remote.php/dav/files/")
            or not url.path.endswith("/") or url.username or url.password
            or url.query or url.fragment):
        raise ValueError("invalid_nextcloud")
    if (not re.fullmatch(r"[A-Za-z0-9_./ -]{1,100}", payload["vault_folder"])
            or payload["vault_folder"].startswith(("/", "."))
            or any(part in ("", ".", "..")
                   for part in payload["vault_folder"].split("/"))):
        raise ValueError("invalid_nextcloud")
    if ":" in payload["nextcloud_user"]:
        raise ValueError("invalid_nextcloud")
    return dict(payload)


def probe_nextcloud(source: dict[str, str]) -> bool:
    """Read-only WebDAV folder metadata. No Gitea probe or remote mutation."""
    url = source["webdav_url"] + "/".join(
        quote(part) for part in source["vault_folder"].split("/")) + "/"
    basic = base64.b64encode((source["nextcloud_user"] + ":" +
                              source["nextcloud_password"]).encode()).decode()
    request = urllib.request.Request(
        url, method="PROPFIND",
        headers={"Depth": "0", "Authorization": "Basic " + basic},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status == 207
    except (OSError, urllib.error.URLError, ValueError):
        return False


@dataclass(frozen=True)
class NextcloudSource:
    """Fixed rclone download source; deliberately has no Git destination."""
    state: Path
    rclone_remote: str
    rclone_config: Path

    def validate(self) -> None:
        if (not self.state.is_absolute() or not self.rclone_config.is_file()
                or self.rclone_config.is_symlink()
                or stat.S_IMODE(self.rclone_config.stat().st_mode) != 0o600):
            raise ValueError("missing_private_webdav_configuration")
        if not re.fullmatch(r"nextcloud:[A-Za-z0-9_./ -]+", self.rclone_remote):
            raise ValueError("invalid_webdav_folder")
        folder = self.rclone_remote.split(":", 1)[1]
        if folder.startswith(("/", ".")) or any(
            item in ("", ".", "..") for item in folder.split("/")
        ):
            raise ValueError("invalid_webdav_folder")


class NextcloudSetup:
    """Reuse B2's account when present, otherwise persist only one WebDAV source."""

    def __init__(self, full: SetupStore, *, probe=None, obscure=None):
        self.full = full
        self.state = full.state
        self.path = self.state / "nextcloud-only.json"
        self.rclone = self.state / "nextcloud-only-rclone.conf"
        self._lock = full._lock
        self.probe = probe or probe_nextcloud
        self.obscure = obscure or obscure_password

    def _standalone(self) -> dict[str, str] | None:
        try:
            info = self.path.lstat()
            config_info = self.rclone.lstat()
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or not stat.S_ISREG(config_info.st_mode)
                    or stat.S_IMODE(config_info.st_mode) != 0o600):
                return None
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if type(payload) is not dict or set(payload) != set(FIELDS) | {MARKER}:
                return None
            digest = payload.pop(MARKER)
            if (type(digest) is not str or not re.fullmatch(r"[a-f0-9]{64}", digest)
                    or hashlib.sha256(self.rclone.read_bytes()).hexdigest() != digest):
                return None
            return validate_nextcloud(payload)
        except (OSError, UnicodeError, ValueError, TypeError):
            return None

    def load(self) -> dict[str, str] | None:
        with self._lock:
            full = self.full.load()
            if full is not None:
                return {key: full[key] for key in FIELDS}
            return self._standalone()

    def connections(self) -> NextcloudSource | None:
        with self._lock:
            full = self.full.load()
            if full is not None:
                source = NextcloudSource(self.state, "nextcloud:" + full["vault_folder"],
                                         self.full.rclone)
            else:
                value = self._standalone()
                if value is None:
                    return None
                source = NextcloudSource(self.state, "nextcloud:" + value["vault_folder"],
                                         self.rclone)
            try:
                source.validate()
                return source
            except ValueError:
                return None

    def status(self) -> dict[str, object]:
        configured = self.connections() is not None
        return {"version": 1, "configured": configured,
                "nextcloud": "configured" if configured else "missing",
                "gitea_required": False}

    def save(self, payload: object, *, before_commit: Callable[[], None] = lambda: None) -> bool:
        value = validate_nextcloud(payload)
        with self._lock:
            full = self.full.load()
            if full is not None:
                # Prevent a second active Nextcloud account beside B2's source.
                if any(full[key] != value[key] for key in FIELDS):
                    raise NextcloudInUse("nextcloud_managed_by_snapshot")
                return bool(self.probe(value))
            if not self.probe(value):
                return False
            obscured = self.obscure(value["nextcloud_password"])
            if type(obscured) is not str or not re.fullmatch(r"[A-Za-z0-9+/=]+", obscured):
                return False
            config = ("[nextcloud]\ntype = webdav\nurl = " + value["webdav_url"] +
                      "\nvendor = nextcloud\nuser = " + value["nextcloud_user"] +
                      "\npass = " + obscured + "\n")
            old_values = self._standalone()
            if old_values == value:
                return True
            # Caller stops an existing B4 schedule *before* replacing a
            # source. A failed DB pause cannot write new credentials.
            before_commit()
            old_config = self.rclone.read_text() if self.rclone.exists() else None
            try:
                _atomic(self.rclone, config)
                _atomic(self.path, json.dumps({
                    **value, MARKER: hashlib.sha256(config.encode()).hexdigest()
                }))
            except OSError:
                if old_config is None:
                    self.rclone.unlink(missing_ok=True)
                else:
                    _atomic(self.rclone, old_config)
                raise
            return True

    def discard_standalone_if_full(self) -> None:
        """After a successful B2 setup, leave just the new authoritative B2 source."""
        with self._lock:
            if self.full.load() is not None:
                self.path.unlink(missing_ok=True)
                self.rclone.unlink(missing_ok=True)
