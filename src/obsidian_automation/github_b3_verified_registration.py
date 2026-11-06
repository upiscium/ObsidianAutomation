"""Unattached fixed-host, root-owned B3 registration and secret admission.

The only authority is a future privileged installer creating a fixed root-owned
config directory with a group-readable registration and credential. The HTTP
request cannot choose any path, connection or secret; no WebDAV request, Writer
guard, host retirement claim, or service registration exists in this module.

Source-only: importing it never opens a file; production locations have not
been provisioned. This is local Unix DAC identity, not remote attestation.
"""
from __future__ import annotations

import grp
import json
import math
import os
from pathlib import Path
import pwd
import re
import stat
from urllib.parse import unquote, urlsplit

from .github_b3_job import B3JobConfig
from .github_b3_request_binding import B3RegisteredScope
from .webdav_create import build_target_url

ROOT = Path("/etc/obsidian-b3-registered")
TRUST_ANCHOR = Path("/")
READER_ROLE = "obsidian-github-writer"
READER_GROUP = "obsidian-github-writer"
CONFIG_FILE = "registered-scope.json"
SECRET_FILE = "webdav-password"
CONFIG_LIMIT = 4096
SECRET_LIMIT = 1024
_ID = re.compile(r"[0-9a-f]{32}\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class B3RegistrationUnavailable(RuntimeError):
    """No private bytes, paths, user names or provider errors in failures."""

    def __init__(self) -> None:
        super().__init__("registration_source_unavailable")


def _root_uid() -> int:
    return 0


def _reader_ids() -> tuple[int, int]:
    uid = pwd.getpwnam(READER_ROLE).pw_uid
    gid = grp.getgrnam(READER_GROUP).gr_gid
    if uid <= 0 or gid <= 0 or os.geteuid() != uid:
        raise B3RegistrationUnavailable()
    return uid, gid


def _identity(st: os.stat_result) -> tuple[int, ...]:
    return (
        st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_uid,
        st.st_gid, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
    )


def _open_trusted_root(*, owner_uid: int, reader_gid: int) -> int:
    if (
        type(ROOT) is not type(Path("/"))
        or type(TRUST_ANCHOR) is not type(Path("/"))
        or not ROOT.is_absolute() or not TRUST_ANCHOR.is_absolute()
        or ROOT == TRUST_ANCHOR
        or not ROOT.is_relative_to(TRUST_ANCHOR)
        or ".." in ROOT.parts or ".." in TRUST_ANCHOR.parts
    ):
        raise B3RegistrationUnavailable()
    fd = os.open(TRUST_ANCHOR, _DIR_FLAGS)
    try:
        anchor = os.fstat(fd)
        if (not stat.S_ISDIR(anchor.st_mode)
                or anchor.st_uid != owner_uid
                or stat.S_IMODE(anchor.st_mode) & 0o022):
            raise B3RegistrationUnavailable()
        for part in ROOT.relative_to(TRUST_ANCHOR).parts:
            newfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            try:
                st = os.fstat(newfd)
                if (not stat.S_ISDIR(st.st_mode)
                        or st.st_uid != owner_uid
                        or stat.S_IMODE(st.st_mode) & 0o022):
                    raise B3RegistrationUnavailable()
            except BaseException:
                os.close(newfd)
                raise
            os.close(fd)
            fd = newfd
        last = os.fstat(fd)
        if (last.st_gid != reader_gid
                or stat.S_IMODE(last.st_mode) != 0o750):
            raise B3RegistrationUnavailable()
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def _read_fixed_file(
    dirfd: int, name: str, *, owner_uid: int, reader_gid: int, limit: int,
) -> bytes:
    fd = os.open(name, _FILE_FLAGS, dir_fd=dirfd)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != owner_uid
                or before.st_gid != reader_gid
                or stat.S_IMODE(before.st_mode) != 0o640
                or not 0 < before.st_size <= limit):
            raise B3RegistrationUnavailable()
        data = os.read(fd, limit + 1)
        after = os.fstat(fd)
        at_name = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
        if (len(data) != before.st_size
                or _identity(after) != _identity(before)
                or _identity(at_name) != _identity(before)):
            raise B3RegistrationUnavailable()
        return data
    finally:
        os.close(fd)


def _canonical_record(data: bytes) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        record: dict[str, object] = {}
        for key, value in pairs:
            if key in record:
                raise B3RegistrationUnavailable()
            record[key] = value
        return record

    value = json.loads(data, object_pairs_hook=unique)
    if type(value) is not dict:
        raise B3RegistrationUnavailable()
    expected = (
        json.dumps(value, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")
    if expected != data:
        raise B3RegistrationUnavailable()
    return value


def _registration(record: dict[str, object], secret: bytes) -> B3RegisteredScope:
    if (set(record) != {
        "record_version", "source_generation", "binding_ref", "revision",
        "project_path", "repository", "registered_base_url", "username",
        "timeout",
    } or type(record["record_version"]) is not int
            or record["record_version"] != 1
            or any(type(record[k]) is not str or not _ID.fullmatch(record[k])
                   for k in ("source_generation", "binding_ref", "revision"))
            or type(record["project_path"]) is not str
            or len(record["project_path"]) > 512
            or not record["project_path"].startswith("10-Project/")
            or not record["project_path"].endswith(".md")
            or type(record["repository"]) is not str
            or not _REPO.fullmatch(record["repository"])
            or type(record["username"]) is not str
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", record["username"])
            or record["username"] in (".", "..")
            or type(record["registered_base_url"]) is not str
            or not 1 <= len(record["registered_base_url"]) <= 1024
            or any(ord(c) <= 32 or ord(c) == 127
                   for c in record["registered_base_url"])
            or any(ord(c) < 32 or ord(c) == 127
                   for c in record["project_path"])
            or type(record["timeout"]) not in (int, float)
            or not math.isfinite(record["timeout"])
            or not 0 < record["timeout"] <= 120):
        raise B3RegistrationUnavailable()

    # The future privileged root owner, not any request, chooses a fixed
    # HTTPS DAV account and canonical Vault root.
    try:
        url = urlsplit(record["registered_base_url"])
        username = record["username"]
        segments = url.path.split("/")
        if (url.scheme != "https" or not url.hostname
                or url.username is not None or url.password is not None
                or url.query or url.fragment
                or len(segments) < 6
                or segments[1:5] != ["remote.php", "dav", "files", username]
                or any(p in ("", ".", "..") for p in segments[5:])
                or any(unquote(p) != p for p in segments)
                or record["registered_base_url"].strip()
                   != record["registered_base_url"]):
            raise ValueError("noncanonical-dav-scope")
        build_target_url(record["registered_base_url"], record["project_path"])
    except Exception:
        raise B3RegistrationUnavailable() from None

    if secret.endswith(b"\n"):
        secret = secret[:-1]
    if (not secret or len(secret) > SECRET_LIMIT
            or any(c < 0x20 or c == 0x7f for c in secret)):
        raise B3RegistrationUnavailable()
    try:
        private_value = secret.decode("utf-8")
    except UnicodeDecodeError:
        raise B3RegistrationUnavailable() from None
    return B3RegisteredScope(
        config=B3JobConfig(
            binding_ref=record["binding_ref"],
            revision=record["revision"],
            project_path=record["project_path"],
            repository=record["repository"],
            registered_base_url=record["registered_base_url"],
            username=record["username"],
            password=private_value,
            timeout=record["timeout"],
        ),
        source_generation=record["source_generation"],
    )


def load_verified_b3_registered_scope() -> B3RegisteredScope:
    """Load fixed root-owned server config and a separate private credential.

    No request argument or override path is accepted. Future service owner
    must prove old Writer exclusion BEFORE calling any provider that reads
    this credential and must recheck all registered inputs under its guard.
    """
    try:
        _, gid = _reader_ids()
        root_uid = _root_uid()
        fd = _open_trusted_root(owner_uid=root_uid, reader_gid=gid)
        try:
            config = _read_fixed_file(
                fd, CONFIG_FILE, owner_uid=root_uid, reader_gid=gid,
                limit=CONFIG_LIMIT,
            )
            secret = _read_fixed_file(
                fd, SECRET_FILE, owner_uid=root_uid, reader_gid=gid,
                limit=SECRET_LIMIT,
            )
            # Detect a config/secret rotation during this bounded read.
            if (config != _read_fixed_file(
                fd, CONFIG_FILE, owner_uid=root_uid, reader_gid=gid,
                limit=CONFIG_LIMIT,
            ) or secret != _read_fixed_file(
                fd, SECRET_FILE, owner_uid=root_uid, reader_gid=gid,
                limit=SECRET_LIMIT,
            )):
                raise B3RegistrationUnavailable()
            return _registration(_canonical_record(config), secret)
        finally:
            os.close(fd)
    except Exception:
        raise B3RegistrationUnavailable() from None
