"""B3-lite step 2a: read Project notes from the Vault and change one status line.

Design: docs/b3-status-lite-design.md. This is the only place the feature touches the
Vault, and it is deliberately small:

* reads and writes are confined to ``<project role folder>/**/*.md`` (a path guard, not
  an ACL); nothing is created, renamed or deleted;
* a note is updated with ``PUT`` + ``If-Match`` on the strong ETag of the exact bytes the
  decision was made from, so an edit by the owner in the meantime makes the write fail
  (``note_changed``) and the note is retried on a later run;
* the new bytes are produced by ``apply_status_line`` (one line changes), and a result is
  reported only after the bytes were read back and match exactly;
* the credential is the shared saved connection; the pinned Vault root must still verify.

Errors are fixed codes: never a URL, credential, note path or text.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from .core_promotion_transport import (
    HTTPResponse, HTTPTransport, MAX_MUTATION_RESPONSE_BYTES, MAX_REMOTE_RESPONSE_BYTES,
    PromotionTransportNetworkError, _real_http_request,
)
from .knowledge_proposal_publish import _bounded
from .project_status import StatusEditError, apply_status_line
from .snapshot_setup import SetupStore, validate_nextcloud
from .vault_layout import role_folder
from .vault_root_activation import RootPinRefused
from .webdav_connection import vault_collection_url
from .webdav_create import WebDAVCreateError, build_target_url

MAX_NOTE_BYTES = 256 * 1024
MAX_NOTES = 500
MAX_DEPTH = 4
_STRONG_ETAG = re.compile(r'"[^"\r\n]{1,200}"\Z')
_LIST_BODY = (b'<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:">'
              b"<d:prop><d:resourcetype/></d:prop></d:propfind>")


class ProjectVaultError(RuntimeError):
    """Fixed error codes only."""


@dataclass(frozen=True)
class NoteRead:
    path: str
    content: bytes
    etag: str


@dataclass(frozen=True)
class _Connection:
    base: str
    username: str
    password: str
    original: tuple
    request: HTTPTransport
    timeout: float


def _check_path(path: object) -> str:
    folder = role_folder("project")
    if type(path) is not str:
        raise ProjectVaultError("invalid_target")
    pure = PurePosixPath(path)
    if (pure.suffix != ".md" or ".." in pure.parts or path.startswith("/") or "\\" in path
            or "\x00" in path or any(part.startswith(".") for part in pure.parts)
            or not (pure.parts and pure.parts[0] == folder.split("/")[0])
            or not path.startswith(folder + "/")):
        raise ProjectVaultError("invalid_target")
    return path


def _connect(shared: SetupStore, guard: Callable[[], object] | None,
             transport: HTTPTransport | None, timeout: float) -> _Connection:
    source = shared.load_nextcloud()
    if type(source) is not dict:
        raise ProjectVaultError("source_not_configured")
    try:
        source = validate_nextcloud(source)
        base = vault_collection_url(source["webdav_url"], source["vault_folder"])
    except (ValueError, WebDAVCreateError):
        raise ProjectVaultError("invalid_vault_root") from None
    if guard is not None:
        try:
            guard()
        except RootPinRefused:
            raise ProjectVaultError("vault_root_refused") from None
    original = tuple(source[k] for k in ("webdav_url", "vault_folder", "nextcloud_user",
                                         "nextcloud_password"))
    return _Connection(base, source["nextcloud_user"], source["nextcloud_password"], original,
                       transport or _real_http_request, timeout)


def _url(connection: _Connection, path: str, *, collection: bool = False) -> str:
    try:
        url = build_target_url(connection.base, path)
    except (ValueError, WebDAVCreateError):
        raise ProjectVaultError("invalid_target") from None
    return url + "/" if collection else url


def _call(connection: _Connection, **kwargs) -> HTTPResponse:
    return _bounded(connection.request, username=connection.username,
                    password=connection.password, timeout=connection.timeout, **kwargs)


def list_project_notes(*, shared: SetupStore, guard: Callable[[], object] | None = None,
                       transport: HTTPTransport | None = None,
                       timeout: float = 30.0) -> list[str]:
    """Relative paths of ``*.md`` below the project folder (read-only, bounded, sorted).

    An absent folder is an empty list. Any other trouble raises, so a caller fails closed.
    """
    connection = _connect(shared, guard, transport, timeout)
    base_path = urlsplit(connection.base).path
    base_path = base_path if base_path.endswith("/") else base_path + "/"
    folders = [(role_folder("project"), 0)]
    found: list[str] = []
    while folders:
        folder, depth = folders.pop(0)
        try:
            response = _call(connection, method="PROPFIND", target_url=_url(connection, folder, collection=True),
                             headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
                             body=_LIST_BODY, response_limit=MAX_REMOTE_RESPONSE_BYTES)
        except PromotionTransportNetworkError:
            raise ProjectVaultError("listing_unavailable") from None
        if response.status == 404:
            if depth == 0:
                return []
            continue
        if response.status != 207 or type(response.body) is not bytes:
            raise ProjectVaultError("listing_unavailable")
        try:
            tree = ElementTree.fromstring(response.body)
        except ElementTree.ParseError:
            raise ProjectVaultError("listing_unavailable") from None
        for item in tree.iter("{DAV:}response"):
            href = item.find("{DAV:}href")
            if href is None or not href.text:
                continue
            path = unquote(urlsplit(href.text.strip()).path)
            if not path.startswith(base_path):
                raise ProjectVaultError("listing_unexpected")
            relative = path[len(base_path):]
            is_collection = item.find(".//{DAV:}resourcetype/{DAV:}collection") is not None
            if is_collection:
                if relative.rstrip("/") != folder and depth < MAX_DEPTH:
                    folders.append((relative.rstrip("/"), depth + 1))
                continue
            try:
                found.append(_check_path(relative))
            except ProjectVaultError:
                continue  # hidden or odd names are simply not Project notes
            if len(found) > MAX_NOTES:
                raise ProjectVaultError("too_many_notes")
    return sorted(set(found))


def read_note(path: str, *, shared: SetupStore, guard: Callable[[], object] | None = None,
              transport: HTTPTransport | None = None, timeout: float = 30.0) -> NoteRead:
    """The bytes and strong ETag of one Project note. A weak or missing ETag is refused:
    the update must be conditional on exactly these bytes."""
    connection = _connect(shared, guard, transport, timeout)
    return _read(connection, _check_path(path))


def _read(connection: _Connection, path: str) -> NoteRead:
    try:
        response = _call(connection, method="GET", target_url=_url(connection, path),
                         headers={"Accept": "text/markdown"}, body=None,
                         response_limit=MAX_REMOTE_RESPONSE_BYTES)
    except PromotionTransportNetworkError:
        raise ProjectVaultError("read_unavailable") from None
    if response.status == 404:
        raise ProjectVaultError("note_missing")
    if response.status != 200 or type(response.body) is not bytes or len(response.body) > MAX_NOTE_BYTES:
        raise ProjectVaultError("read_unavailable")
    if type(response.etag) is not str or not _STRONG_ETAG.fullmatch(response.etag):
        raise ProjectVaultError("etag_not_strong")
    return NoteRead(path, response.body, response.etag)


def set_status(path: str, old: str, new: str, *, shared: SetupStore,
               guard: Callable[[], object] | None = None,
               transport: HTTPTransport | None = None, timeout: float = 30.0,
               expected: NoteRead | None = None) -> dict[str, object]:
    """Change the status of one note from ``old`` to ``new``; only that line changes.

    Reads the note fresh (or checks ``expected`` against the fresh read), edits one line,
    writes with ``If-Match`` and verifies by reading back. Returns
    ``{"outcome": "updated" | "recovered", "sha256": ...}``. Raises ``ProjectVaultError``:
    ``note_changed`` (the bytes differ from what the decision used, or the server refused
    the condition: nothing was written), ``status_edit_refused`` (not safely editable),
    ``outcome_unconfirmed`` (a transport failure after the write: state unknown),
    ``readback_mismatch``.
    """
    path = _check_path(path)
    connection = _connect(shared, guard, transport, timeout)
    current = _read(connection, path)
    if expected is not None and (expected.content != current.content or expected.etag != current.etag):
        raise ProjectVaultError("note_changed")
    try:
        text = current.content.decode("utf-8")
        edited = apply_status_line(text, old, new).encode("utf-8")
    except (UnicodeDecodeError, StatusEditError):
        raise ProjectVaultError("status_edit_refused") from None
    if len(edited) > MAX_NOTE_BYTES:
        raise ProjectVaultError("status_edit_refused")
    ambiguous, status = False, None
    try:
        response = _call(connection, method="PUT", target_url=_url(connection, path),
                         headers={"If-Match": current.etag, "Content-Type": "text/markdown; charset=utf-8"},
                         body=edited, response_limit=MAX_MUTATION_RESPONSE_BYTES)
        status = response.status
        if status == 412:
            raise ProjectVaultError("note_changed")
        if not 200 <= status < 300:
            ambiguous = True
    except PromotionTransportNetworkError:
        ambiguous = True
    try:
        after = _call(connection, method="GET", target_url=_url(connection, path),
                      headers={"Accept": "text/markdown"}, body=None,
                      response_limit=MAX_REMOTE_RESPONSE_BYTES)
    except PromotionTransportNetworkError:
        raise ProjectVaultError("outcome_unconfirmed") from None
    if after.status == 200 and type(after.body) is bytes and after.body == edited:
        _assert_source_unchanged(shared, connection.original)
        return {"outcome": "recovered" if ambiguous else "updated",
                "sha256": hashlib.sha256(edited).hexdigest()}
    if ambiguous and after.status == 200 and after.body == current.content:
        raise ProjectVaultError("note_changed")  # the write did not take effect
    if ambiguous:
        raise ProjectVaultError("outcome_unconfirmed")
    raise ProjectVaultError("readback_mismatch")


def _assert_source_unchanged(shared: SetupStore, original: tuple) -> None:
    source = shared.load_nextcloud()
    if type(source) is not dict or tuple(source.get(k) for k in (
            "webdav_url", "vault_folder", "nextcloud_user", "nextcloud_password")) != original:
        raise ProjectVaultError("source_changed")
