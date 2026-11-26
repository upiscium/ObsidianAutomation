"""B1-lite phase 2: create one proposal note in the Vault, create-only, with readback.

Design: docs/knowledge-lite-design.md. This is the only place the knowledge
feature writes to the Vault, and it is deliberately small:

* the target is fixed to ``04-AI/Proposals/<name>.md`` and checked here (a path
  guard, not an ACL); nothing else is ever written;
* the file is created with ``If-None-Match: *``; an existing file is never
  overwritten, merged, renamed or deleted;
* the note is checked again for executable Obsidian content right before the
  write, and the pinned Vault root must still verify (same guard Daily uses);
* a result is only reported after the bytes were read back and match exactly
  (the ETag is not needed: nothing is updated afterwards, and Apache answers a
  fresh write with a weak ETag unless it is configured with FileETag Digest);
* the credential is the one the other Jobs already use (the shared saved
  connection); the feature adds no account.

Nothing runs unless a caller invokes :func:`publish_proposal`.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import unquote

from .core_promotion_transport import (
    HTTPResponse, HTTPTransport, MAX_MUTATION_RESPONSE_BYTES,
    MAX_REMOTE_RESPONSE_BYTES, PromotionTransportError,
    PromotionTransportNetworkError, _real_http_request,
    ensure_parent_collections,
)
from .knowledge_proposal import (
    IMPLEMENTATION_REVISION, PROPOSAL_EMBED, PROPOSAL_ROOT, QUERY, Proposal, ProposalError,
    check_active_content,
)
from .snapshot_setup import SetupStore, validate_nextcloud
from .vault_root_activation import RootPinRefused
from .webdav_connection import vault_collection_url
from .webdav_create import WebDAVCreateError, build_target_url

MAX_PROPOSAL_BYTES = 256 * 1024
_NAME = re.compile(r"[^/\x00-\x1f\\]{1,200}\.md\Z")


class PublishError(RuntimeError):
    """Fixed error codes only; never a URL, credential, note text or exception text."""


def _validate(proposal: Proposal) -> str:
    """Admit only one well-formed proposal under the fixed folder."""
    if type(proposal.target_path) is not str:
        raise PublishError("invalid_target")
    path = PurePosixPath(proposal.target_path)
    if (path.parent.as_posix() != PROPOSAL_ROOT or not _NAME.fullmatch(path.name)
            or ".." in path.name or path.name.startswith(".")):
        raise PublishError("invalid_target")
    content = proposal.content
    if type(content) is not bytes or not content or len(content) > MAX_PROPOSAL_BYTES:
        raise PublishError("invalid_content")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise PublishError("invalid_content") from None
    if not text.startswith("---\n") or "type: knowledge-proposal" not in text.split("\n---\n", 1)[0]:
        raise PublishError("not_a_proposal")
    # The one managed embed is the approval button the proposal needs; it must be
    # present exactly once and is the only active block allowed.
    if PROPOSAL_EMBED not in text:
        raise PublishError("not_a_proposal")
    try:
        check_active_content(text.replace(PROPOSAL_EMBED, "", 1))
    except ProposalError:
        raise PublishError("active_content") from None
    return path.name


def _bounded(request: HTTPTransport, **kwargs) -> HTTPResponse:
    try:
        response = request(**kwargs)
    except PromotionTransportNetworkError:
        raise
    except Exception:
        # Custom providers can put URLs or credentials in exceptions.
        raise PromotionTransportNetworkError("WebDAV request returned no trustworthy result") from None
    if type(response) is not HTTPResponse or type(response.status) is not int:
        raise PromotionTransportNetworkError("WebDAV request returned invalid metadata")
    return response


def _observe(request: HTTPTransport, *, url: str, username: str, password: str,
             timeout: float) -> HTTPResponse:
    return _bounded(
        request, method="GET", target_url=url, username=username, password=password,
        headers={"Accept": "text/markdown"}, body=None, timeout=timeout,
        response_limit=MAX_REMOTE_RESPONSE_BYTES,
    )


def publish_proposal(
    proposal: Proposal,
    *,
    shared: SetupStore,
    guard: Callable[[], object] | None = None,
    transport: HTTPTransport | None = None,
    timeout: float = 30.0,
) -> dict[str, object]:
    """Create the proposal note in the Vault. Never overwrites.

    Returns ``{"outcome": "created" | "recovered" | "already_present", ...}`` only
    after an exact-byte readback. Raises :class:`PublishError`
    with a fixed code otherwise.
    """
    name = _validate(proposal)
    target_path = f"{PROPOSAL_ROOT}/{name}"
    source = shared.load_nextcloud()
    if type(source) is not dict:
        raise PublishError("source_not_configured")
    try:
        source = validate_nextcloud(source)
        base = vault_collection_url(source["webdav_url"], source["vault_folder"])
        url = build_target_url(base, target_path)  # strict HTTPS/host/relative checks
    except (ValueError, WebDAVCreateError):
        raise PublishError("invalid_vault_root") from None
    username, password = source["nextcloud_user"], source["nextcloud_password"]
    original_source = tuple(source[k] for k in (
        "webdav_url", "vault_folder", "nextcloud_user", "nextcloud_password"))

    if guard is not None:
        try:
            guard()
        except RootPinRefused:
            raise PublishError("vault_root_refused") from None

    request = transport or _real_http_request
    content = proposal.content
    digest = hashlib.sha256(content).hexdigest()
    try:
        ensure_parent_collections(
            base_url=base, path=target_path, username=username, password=password,
            timeout=timeout, transport=transport,
        )
    except PromotionTransportError:
        raise PublishError("parent_collections_unavailable") from None

    ambiguous = False
    status: int | None = None
    try:
        response = _bounded(
            request, method="PUT", target_url=url, username=username, password=password,
            headers={"If-None-Match": "*", "Content-Type": "text/markdown; charset=utf-8"},
            body=content, timeout=timeout, response_limit=MAX_MUTATION_RESPONSE_BYTES,
        )
        status = response.status
        if not 200 <= status < 300 and status != 412:
            ambiguous = True
    except PromotionTransportNetworkError:
        ambiguous = True

    try:
        observed = _observe(request, url=url, username=username, password=password,
                            timeout=timeout)
    except PromotionTransportNetworkError:
        raise PublishError("outcome_unconfirmed") from None
    matches = (observed.status == 200 and type(observed.body) is bytes
               and observed.body == content)
    if status == 412:
        # Something already exists at this exact name. Identical bytes are fine
        # (nothing to do); anything else is never touched.
        if matches:
            return _receipt("already_present", target_path, digest, len(content))
        raise PublishError("proposal_exists")
    if matches:
        _assert_source_unchanged(shared, original_source)
        return _receipt("recovered" if ambiguous else "created", target_path, digest, len(content))
    if ambiguous and observed.status == 404:
        raise PublishError("outcome_unconfirmed")
    raise PublishError("readback_mismatch")


def _assert_source_unchanged(shared: SetupStore, original: tuple) -> None:
    """A source swap during a write must not leave a green result for the new source."""
    updated = shared.load_nextcloud()
    if type(updated) is not dict or tuple(updated.get(k) for k in (
            "webdav_url", "vault_folder", "nextcloud_user", "nextcloud_password")) != original:
        raise PublishError("source_changed")


def _receipt(outcome: str, target_path: str, digest: str, size: int) -> dict[str, object]:
    return {"status": "published", "outcome": outcome, "target_path": target_path,
            "content_sha256": digest, "bytes": size, "vault_write": outcome != "already_present"}


# ------------------------------------------------------------------ journal

def recipe_key(source_sha256: str, model: str) -> str:
    """Identity of "this source made the same way": same input, model, pipeline, prompt."""
    material = "\0".join((source_sha256, model, IMPLEMENTATION_REVISION,
                          hashlib.sha256(QUERY.encode("utf-8")).hexdigest()))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ProposalJournal:
    """Small private record so one input is not generated and written twice."""

    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "journal.sqlite3")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS proposals (key TEXT PRIMARY KEY, target_path TEXT NOT NULL,"
            " content_sha256 TEXT NOT NULL, created REAL NOT NULL)")
        self.db.commit()

    def lookup(self, key: str) -> dict | None:
        row = self.db.execute(
            "SELECT target_path,content_sha256,created FROM proposals WHERE key=?", (key,)).fetchone()
        return None if row is None else dict(
            target_path=row[0], content_sha256=row[1], created=row[2])

    def record(self, key: str, receipt: dict) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO proposals VALUES (?,?,?,?)", (
                key, receipt["target_path"], receipt["content_sha256"], time.time()))

    def close(self) -> None:
        self.db.close()


# ------------------------------------------------- generate + publish (shared)

def generate_and_publish(
    source,
    *,
    shared: SetupStore,
    guard: Callable[[], object] | None,
    journal: ProposalJournal,
    ai_root: Path,
    base_url: str,
    model: str,
    timeout: float,
    api_key: str | None = None,
    transport: HTTPTransport | None = None,
    json_transport=None,
) -> dict[str, object]:
    """One source text -> one proposal in the Vault. The CLI and the service share this.

    Returns ``already_proposed`` (no generation, no write) when the same source was
    already proposed the same way; otherwise generates, creates and records it.
    ``ProposalError`` / ``PublishError`` carry fixed codes; a failed attempt is not
    recorded, so it can be retried.
    """
    from . import knowledge_proposal as _kp  # late: that module imports this one lazily

    key = recipe_key(hashlib.sha256(source.text.encode("utf-8")).hexdigest(), model)
    previous = journal.lookup(key)
    if previous is not None:
        return {"status": "already_proposed",
                "file": PurePosixPath(previous["target_path"]).name, "vault_write": False}
    proposal = _kp.propose(ai_root, source, base_url=base_url, model=model, timeout=timeout,
                           api_key=api_key, transport=json_transport)
    receipt = publish_proposal(proposal, shared=shared, guard=guard, transport=transport)
    journal.record(key, receipt)
    return {"status": "proposed", "outcome": receipt["outcome"], "bytes": receipt["bytes"],
            "file": PurePosixPath(receipt["target_path"]).name,
            "vault_write": receipt["vault_write"]}


def count_pending_proposals(
    *,
    shared: SetupStore,
    guard: Callable[[], object] | None = None,
    transport: HTTPTransport | None = None,
    timeout: float = 30.0,
) -> int:
    """How many ``*.md`` proposals still wait in ``04-AI/Proposals/`` (read-only).

    An absent folder is zero. Any other trouble raises ``PublishError`` so a caller
    that uses the count as a brake fails closed.
    """
    from xml.etree import ElementTree

    source = shared.load_nextcloud()
    if type(source) is not dict:
        raise PublishError("source_not_configured")
    try:
        source = validate_nextcloud(source)
        base = vault_collection_url(source["webdav_url"], source["vault_folder"])
        url = build_target_url(base, PROPOSAL_ROOT) + "/"
    except (ValueError, WebDAVCreateError):
        raise PublishError("invalid_vault_root") from None
    if guard is not None:
        try:
            guard()
        except RootPinRefused:
            raise PublishError("vault_root_refused") from None
    try:
        response = _bounded(
            transport or _real_http_request, method="PROPFIND", target_url=url,
            username=source["nextcloud_user"], password=source["nextcloud_password"],
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            body=_LIST_BODY, timeout=timeout, response_limit=MAX_REMOTE_RESPONSE_BYTES,
        )
    except PromotionTransportNetworkError:
        raise PublishError("pending_count_unavailable") from None
    if response.status == 404:
        return 0
    if response.status != 207 or type(response.body) is not bytes:
        raise PublishError("pending_count_unavailable")
    try:
        root = ElementTree.fromstring(response.body)
    except ElementTree.ParseError:
        raise PublishError("pending_count_unavailable") from None
    count = 0
    for href in root.iter("{DAV:}href"):
        path = unquote((href.text or "").strip())
        if path.endswith(".md") and not path.endswith("/"):
            count += 1
    return count


_LIST_BODY = (b'<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:">'
              b"<d:prop><d:resourcetype/></d:prop></d:propfind>")
