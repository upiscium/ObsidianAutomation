"""#303 V1/B2: read-only Nextcloud DAV verifier over an injected transport.

This module issues only ``PROPFIND`` through a caller-supplied transport.
It has no HTTP client, credential loader, persistence or activation path.
Provider capability is NOT assumed. The full contract needs every
A/I/X/L/C/R gate VERIFIED; the owner-approved restricted contract needs every
gate except C. Otherwise the verifier refuses before any transport call.
``CURRENT_PROVIDER_PROOF`` records the real register state
(docs/oa303-p3-coherence-feasibility-adr.md): A/I/X/L/R are VERIFIED for
the owner-accepted scope (V1-S1, extended to received shares as V1-S2) only and C is not, so only the restricted contract admits reads.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import re
import time
from typing import Callable, Protocol
from urllib.parse import quote
from xml.etree import ElementTree as ET

from .vault_root_dav_adapter import (
    MAX_PAGE_BYTES, VerifiedDavEntry, VerifiedDavPage,
    preview_from_verified_dav_pages,
)
from .vault_root_dav_fixture import (
    DAV, OC, _ACCOUNT, _Blocked, _canonical_href, _expected_parent_href, _parse_xml,
)
from .vault_root_preview import (
    MAX_COMPONENTS, MAX_SIBLING_NAME_CHARS, DirectoryEvidence, Resource,
    RootPreview, RootSelection, _identifier, _parts, preview_root,
)

NC = "{http://nextcloud.org/ns}"
PER_CALL_TIMEOUT_S = 5.0
TOTAL_BUDGET_S = 15.0
_ORIGIN = re.compile(r"https://[a-z0-9.-]{1,253}(?::[0-9]{1,5})?\Z", re.ASCII)
_FILEID = re.compile(r"[0-9]{1,20}\Z", re.ASCII)
_INSTANCE = re.compile(r"[a-z0-9]{3,64}\Z", re.ASCII)
_CONTENT_TYPES = (
    "application/xml", "text/xml",
    "application/xml; charset=utf-8", "text/xml; charset=utf-8",
)
PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"'
    b' xmlns:nc="http://nextcloud.org/ns"><d:prop>'
    b"<d:resourcetype/><d:getetag/><oc:fileid/><oc:id/><nc:mount-type/>"
    b"<nc:is-mount-root/><oc:permissions/></d:prop></d:propfind>"
)
_REQUIRED = (
    DAV + "resourcetype", DAV + "getetag", OC + "fileid", OC + "id",
    NC + "mount-type", NC + "is-mount-root", OC + "permissions",
)
# Scope V1-S2 (owner-accepted 2026-11-13, P3-SR): a received share
# (mount-type "shared") may hold the Vault root. Every other mount type stays
# an alias. With False the verifier behaves exactly as under V1-S1.
SHARE_ROOTS_ACCEPTED = True
_PERMS = re.compile(r"[A-Z]{0,16}\Z", re.ASCII)


@dataclass(frozen=True)
class ProviderProof:
    """Provider gate register. Only the literal ``VERIFIED`` admits a gate.

    L is scoped to Nextcloud's index (owner decision 2026-11-12, option A):
    a DAV listing is complete relative to the file cache, not to storage.
    Outside the guarantee: files written directly on the server, external
    storage that has not been rescanned, and case twins not yet indexed."""
    A: str
    I: str
    X: str
    L: str
    C: str
    R: str

    def all_verified(self) -> bool:
        return all(
            type(getattr(self, f.name)) is str and getattr(self, f.name) == "VERIFIED"
            for f in fields(self)
        )

    def admits(self, *, restricted_coherence: bool) -> bool:
        """``restricted_coherence`` is the owner-approved limited contract
        (2026-11-12): C may stay unproven, every other gate must be VERIFIED.
        It never turns C into VERIFIED and never sets a coherence flag."""
        if restricted_coherence is not True:
            return self.all_verified()
        return all(
            type(getattr(self, f.name)) is str and getattr(self, f.name) == "VERIFIED"
            for f in fields(self) if f.name != "C"
        )


# Disposable Nextcloud 35.0.2 observations P3-D1..D5 (2026-11-12). A gate is
# promoted only under the owner criterion in
# docs/oa303-a-account-binding-and-verified-criterion.md (scoped evidence,
# independent review, explicit owner acceptance of that gate).
# Owner accepted I/X/L/R (2026-11-13) and A after P3-A (2026-11-13) as
# VERIFIED for scope V1-S1 only. C stays unproven: only the owner-approved
# restricted contract (identity pin, no coherence claim) admits provider reads.
# Production still performs none until a picker/enrollment/activation is wired
# or a root is pinned.
CURRENT_PROVIDER_PROOF = ProviderProof(
    A="VERIFIED", I="VERIFIED", X="VERIFIED",
    L="VERIFIED", C="UNKNOWN", R="VERIFIED",
)


@dataclass(frozen=True)
class TrustedDavAccount:
    """Server-derived account binding. Never built from a browser payload."""
    origin: str
    account_prefix: str
    instance_id: str


@dataclass(frozen=True)
class DavResponse:
    status: int
    content_type: str
    content_encoding: str
    redirected: bool
    body: bytes


class DavTransportError(Exception):
    """Raised by a transport for timeout, oversize or connection failure."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class DavTransport(Protocol):
    def propfind(
        self, origin: str, href: str, depth: str, body: bytes, *,
        timeout: float, max_bytes: int,
    ) -> DavResponse: ...


@dataclass(frozen=True)
class _Row:
    href: str
    collection: bool
    kind: str
    etag: str
    fileid: str
    resource_id: str
    is_alias: bool
    share: str = "local"      # local | share_root | in_share | other
    writable: bool = False    # oc:permissions has C (create) and K (mkdir)


def _share_class(mount_type: str, mount_root: str) -> str:
    if not mount_type.strip():
        return "local"
    if mount_type == "shared" and mount_root in ("true", "false"):
        return "share_root" if mount_root == "true" else "in_share"
    return "other"


def _deny(reason: str) -> RootPreview:
    return RootPreview(version=1, status="blocked", reason=reason)


def _account_ok(account: object) -> bool:
    return (type(account) is TrustedDavAccount
            and type(account.origin) is str and _ORIGIN.fullmatch(account.origin) is not None
            and type(account.account_prefix) is str
            and _ACCOUNT.fullmatch(account.account_prefix) is not None
            and type(account.instance_id) is str
            and _INSTANCE.fullmatch(account.instance_id) is not None)


def _encoded_href(account_prefix: str, parent: str) -> str:
    if not parent:
        return account_prefix
    return account_prefix + "/".join(quote(p, safe="") for p in parent.split("/")) + "/"


def _text(prop: ET.Element, tag: str, *, allow_empty: bool = False) -> str:
    found = prop.findall(tag)
    if len(found) != 1 or len(found[0]):
        raise _Blocked("invalid_dav_response")
    value = found[0].text or ""
    if (not value and not allow_empty) or len(value) > 256:
        raise _Blocked("invalid_dav_response")
    return value


def _parse_row(row: ET.Element, instance_id: str) -> _Row:
    if any(item.tag not in (DAV + "href", DAV + "propstat") for item in row):
        raise _Blocked("invalid_dav_response")
    hrefs = row.findall(DAV + "href")
    if len(hrefs) != 1 or len(hrefs[0]) or not hrefs[0].text:
        raise _Blocked("invalid_dav_response")
    ok: ET.Element | None = None
    missing: set[str] = set()
    for propstat in row.findall(DAV + "propstat"):
        statuses = propstat.findall(DAV + "status")
        props = propstat.findall(DAV + "prop")
        if len(statuses) != 1 or len(props) != 1 or len(propstat) != 2:
            raise _Blocked("invalid_dav_response")
        if statuses[0].text == "HTTP/1.1 200 OK":
            if ok is not None:
                raise _Blocked("invalid_dav_response")
            ok = props[0]
        else:
            missing.update(child.tag for child in props[0])
    if ok is None or any(child.tag not in _REQUIRED for child in ok):
        raise _Blocked("invalid_dav_response")
    present = {child.tag for child in ok}
    if missing & present:
        raise _Blocked("invalid_dav_response")
    absent = set(_REQUIRED) - present
    if {NC + "mount-type", NC + "is-mount-root"} & absent:
        raise _Blocked("alias_unverified")
    if absent:
        raise _Blocked("identity_unverified")

    rtypes = ok.findall(DAV + "resourcetype")
    if len(rtypes) != 1 or rtypes[0].attrib or (rtypes[0].text or "").strip():
        raise _Blocked("invalid_dav_response")
    markers = list(rtypes[0])
    if any(m.attrib or len(m) or (m.text or "").strip() or (m.tail or "").strip()
           for m in markers):
        raise _Blocked("invalid_dav_response")
    tags = [m.tag for m in markers]
    collection = tags == [DAV + "collection"]
    kind = "directory" if collection else ("file" if not tags else "unknown")

    fileid = _text(ok, OC + "fileid")
    resource_id = _text(ok, OC + "id")
    if _FILEID.fullmatch(fileid) is None:
        raise _Blocked("identity_unverified")
    # Documented oc:id layout: zero-padded file ID followed by instance ID.
    if resource_id != fileid.zfill(8) + instance_id or not _identifier(resource_id):
        raise _Blocked("identity_unverified")
    mount_type = _text(ok, NC + "mount-type", allow_empty=True)
    mount_root = _text(ok, NC + "is-mount-root", allow_empty=True)
    permissions = _text(ok, OC + "permissions", allow_empty=True)
    if _PERMS.fullmatch(permissions) is None:
        raise _Blocked("invalid_dav_response")
    etag = _text(ok, DAV + "getetag")
    share = _share_class(mount_type, mount_root)
    if not SHARE_ROOTS_ACCEPTED and share != "local":
        share = "other"
    return _Row(hrefs[0].text, collection, kind, etag, fileid, resource_id,
                share == "other", share, "C" in permissions and "K" in permissions)


def _step_allowed(inside_share: bool, row: _Row) -> bool:
    """May the selected path step into ``row``? Enter one share at its root;
    below it, stay inside that share (no nested share root)."""
    if row.share == "other":
        return False
    if inside_share:
        return row.share == "in_share"
    return row.share in ("local", "share_root")


def _etag_token(etag: str) -> str:
    return "e" + hashlib.sha256(etag.encode("utf-8")).hexdigest()[:40]


class _Walk:
    def __init__(self, account: TrustedDavAccount, transport: DavTransport,
                 clock: Callable[[], float]):
        self.account = account
        self.transport = transport
        self.clock = clock
        self.deadline = clock() + TOTAL_BUDGET_S

    def propfind(self, parent: str, depth: str) -> tuple[list[_Row], int]:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise _Blocked("provider_timeout")
        try:
            response = self.transport.propfind(
                self.account.origin,
                _encoded_href(self.account.account_prefix, parent), depth,
                PROPFIND_BODY, timeout=min(PER_CALL_TIMEOUT_S, remaining),
                max_bytes=MAX_PAGE_BYTES,
            )
        except DavTransportError as err:
            raise _Blocked("limit_exceeded" if err.kind == "oversize" else
                           "provider_timeout" if err.kind == "timeout" else
                           "provider_unavailable") from None
        if type(response) is not DavResponse:
            raise _Blocked("provider_unavailable")
        if response.redirected is not False or (
                type(response.status) is int and 300 <= response.status < 400):
            raise _Blocked("provider_redirect")
        if type(response.status) is not int or response.status != 207:
            raise _Blocked("provider_unavailable")
        if (type(response.content_encoding) is not str
                or response.content_encoding not in ("", "identity")
                or type(response.content_type) is not str
                or response.content_type.lower() not in _CONTENT_TYPES):
            raise _Blocked("invalid_dav_response")
        if type(response.body) is not bytes or len(response.body) > MAX_PAGE_BYTES:
            raise _Blocked("limit_exceeded")
        rows = [_parse_row(r, self.account.instance_id) for r in _parse_xml(response.body)]
        return rows, len(response.body)


@dataclass(frozen=True)
class CandidateListing:
    """Server-internal listing. ``directories`` holds (name, resource_id);
    resource IDs must never be returned to a browser."""
    status: str
    reason: str
    directories: tuple[tuple[str, str], ...] = ()


def list_candidate_directories(
    account: TrustedDavAccount,
    transport: DavTransport,
    *,
    parent: str,
    parent_resource_id: str,
    expected_revision: str,
    source_revision: Callable[[], object],
    proof: ProviderProof = CURRENT_PROVIDER_PROOF,
    clock: Callable[[], float] = time.monotonic,
    restricted_coherence: bool = False,
) -> CandidateListing:
    """One read-only Depth:1 listing of selectable child directories.

    Aliases/mounts, files, unknown types and names outside the new-root
    grammar are omitted. A listing is a hint for the picker only; the
    preview re-walks every level before reporting ``candidate_valid``.
    """
    def deny(reason: str) -> CandidateListing:
        return CandidateListing("blocked", reason)

    if (type(proof) is not ProviderProof
            or not proof.admits(restricted_coherence=restricted_coherence)):
        return deny("provider_unproven")
    if not _account_ok(account):
        return deny("account_unverified")
    if type(parent) is not str or (parent and _parts(parent, new_candidate=True) is None):
        return deny("invalid_path")
    if not _identifier(parent_resource_id) or not _identifier(expected_revision):
        return deny("invalid_request")

    def revision_matches() -> bool:
        current = source_revision()
        return type(current) is str and current == expected_revision

    if not revision_matches():
        return deny("stale_source")
    try:
        rows, _ = _Walk(account, transport, clock).propfind(parent, "1")
        parent_href = _expected_parent_href(account.account_prefix, parent)
        selves = [r for r in rows if _canonical_href(r.href) == parent_href]
        if (len(selves) != 1 or not selves[0].collection or selves[0].is_alias
                or selves[0].resource_id != parent_resource_id):
            raise _Blocked("identity_unverified")
        inside_share = selves[0].share in ("share_root", "in_share")
        found = []
        for row in rows:
            if row is selves[0]:
                continue
            href = _canonical_href(row.href)
            if not href.startswith(parent_href):
                raise _Blocked("out_of_scope")
            leaf = href[len(parent_href):].rstrip("/")
            if not leaf or "/" in leaf:
                raise _Blocked("unsafe_href")
            if row.kind != "directory" or not _step_allowed(inside_share, row):
                continue
            if _parts(leaf, new_candidate=True) is None:
                continue
            found.append((leaf, row.resource_id))
    except _Blocked as err:
        return deny(err.code)
    if not revision_matches():
        return deny("stale_source")
    return CandidateListing("ok", "listed", tuple(sorted(found)))


def verify_root_candidate(
    selection: RootSelection,
    account: TrustedDavAccount,
    transport: DavTransport,
    *,
    source_revision: Callable[[], object],
    proof: ProviderProof = CURRENT_PROVIDER_PROOF,
    clock: Callable[[], float] = time.monotonic,
    expected_resource_id: str | None = None,
    restricted_coherence: bool = False,
) -> RootPreview:
    """Walk the selected root read-only and delegate the decision to V1/A.

    ``expected_resource_id`` pins the target to the folder the operator was
    shown, so a delete/recreate under the same name is not accepted.
    Returns the same redacted ``RootPreview`` as V1/A; never names, IDs,
    URLs or ETags. ``candidate_valid`` is advisory and grants no activation.
    """
    return verify_root_chain(
        selection, account, transport, source_revision=source_revision,
        proof=proof, clock=clock, expected_resource_id=expected_resource_id,
        restricted_coherence=restricted_coherence,
    )[0]


def verify_root_chain(
    selection: RootSelection,
    account: TrustedDavAccount,
    transport: DavTransport,
    *,
    source_revision: Callable[[], object],
    proof: ProviderProof = CURRENT_PROVIDER_PROOF,
    clock: Callable[[], float] = time.monotonic,
    expected_resource_id: str | None = None,
    restricted_coherence: bool = False,
) -> tuple[RootPreview, tuple[str, ...] | None]:
    """As ``verify_root_candidate``; also returns the server-internal
    ``oc:id`` chain of the selected root when the result is candidate_valid."""
    result, chain = _verify(selection, account, transport, source_revision, proof,
                            clock, expected_resource_id, restricted_coherence)
    return result, chain if result.status == "candidate_valid" else None


def _verify(selection, account, transport, source_revision, proof, clock,
            expected_resource_id, restricted_coherence):
    if (type(proof) is not ProviderProof
            or not proof.admits(restricted_coherence=restricted_coherence)):
        return _deny("provider_unproven"), None
    if type(selection) is not RootSelection:
        return _deny("invalid_request"), None
    if type(selection.requested) is not str or type(selection.active) is not str:
        return _deny("invalid_path"), None
    initial = preview_root(selection, ())
    if initial.reason != "incomplete_inventory":
        return initial, None
    if not _account_ok(account):
        return _deny("account_unverified"), None
    if selection.namespace != account.instance_id:
        return _deny("out_of_scope"), None
    selected = _parts(selection.requested, new_candidate=True)
    if selected is None or not 1 <= len(selected) <= MAX_COMPONENTS:
        return _deny("invalid_path"), None
    if expected_resource_id is not None and not _identifier(expected_resource_id):
        return _deny("invalid_request"), None

    def revision_matches() -> bool:
        current = source_revision()
        return type(current) is str and current == selection.current_revision

    if not revision_matches():
        return _deny("stale_source"), None

    walk = _Walk(account, transport, clock)
    pages: list[VerifiedDavPage] = []
    parent = ""
    parent_id = selection.container_resource_id
    root_etag: str | None = None
    page_chain: list[str] = []
    inside_share = False
    target_row: _Row | None = None
    expected_self = "local"   # the account root describes itself as local
    try:
        for component in selected:
            rows, size = walk.propfind(parent, "1")
            parent_href = _expected_parent_href(account.account_prefix, parent)
            selves = [r for r in rows if _canonical_href(r.href) == parent_href]
            if len(selves) != 1 or not selves[0].collection:
                raise _Blocked("identity_unverified")
            if selves[0].resource_id != parent_id or selves[0].is_alias:
                raise _Blocked("identity_unverified")
            # Each folder must describe itself as the parent listing did.
            if selves[0].share != expected_self:
                raise _Blocked("identity_unverified")
            if root_etag is None:
                root_etag = selves[0].etag
            entries = []
            by_name: dict[str, _Row] = {}
            for row in rows:
                if row is selves[0]:
                    continue
                href = _canonical_href(row.href)
                if not href.startswith(parent_href):
                    raise _Blocked("out_of_scope")
                remainder = href[len(parent_href):]
                leaf = remainder[:-1] if remainder.endswith("/") else remainder
                if (not leaf or "/" in leaf or leaf in (".", "..")
                        or len(leaf) > MAX_SIBLING_NAME_CHARS
                        or remainder.endswith("/") != row.collection):
                    raise _Blocked("unsafe_href")
                by_name[leaf] = row
                entries.append(VerifiedDavEntry(
                    name=leaf, resource_id=row.resource_id, kind=row.kind,
                    is_alias=row.is_alias, canonical_identity_verified=True,
                    alias_classification_verified=True,
                ))
            if not entries:
                raise _Blocked("candidate_missing")
            pages.append(VerifiedDavPage(
                parent=parent, parent_resource_id=parent_id,
                namespace=account.instance_id,
                source_revision=selection.current_revision,
                snapshot_token=_etag_token(root_etag), cursor=None, next_cursor=None,
                http_status=207, redirected=False, content_bytes=size,
                complete=True, identity_scope_verified=True,
                coherent_snapshot_verified=proof.all_verified(), rows=tuple(entries),
            ))
            chosen = next((e for e in entries if e.name == component), None)
            if chosen is None:
                raise _Blocked("candidate_missing")
            if chosen.kind != "directory" or chosen.is_alias:
                raise _Blocked("unsafe_target")
            target_row = by_name[component]
            if not _step_allowed(inside_share, target_row):
                raise _Blocked("unsafe_target")
            inside_share = inside_share or target_row.share == "share_root"
            expected_self = target_row.share
            parent_id = chosen.resource_id
            page_chain.append(parent_id)
            parent = component if not parent else parent + "/" + component
        if expected_resource_id is not None and parent_id != expected_resource_id:
            raise _Blocked("identity_unverified")
        if inside_share and (target_row is None or not target_row.writable):
            raise _Blocked("unsafe_target")  # a share the account cannot write to
        # Change signal only (C is a provider gate, not proven by this check):
        # the account root ETag propagates on descendant change.
        after, _ = walk.propfind("", "0")
        if (len(after) != 1
                or _canonical_href(after[0].href) != _canonical_href(account.account_prefix)
                or after[0].resource_id != selection.container_resource_id
                or not after[0].collection or after[0].is_alias):
            raise _Blocked("identity_unverified")
        if after[0].etag != root_etag:
            raise _Blocked("snapshot_unverified")
    except _Blocked as err:
        return _deny(err.code), None
    if not revision_matches():
        return _deny("stale_source"), None
    chain = tuple(page_chain)
    if proof.all_verified():
        return preview_from_verified_dav_pages(selection, tuple(pages)), chain
    # Restricted contract: V1/A decides on the same complete evidence without
    # any coherence claim. Activation must still revalidate the ID chain.
    evidence = tuple(DirectoryEvidence(
        parent=page.parent, parent_resource_id=page.parent_resource_id,
        namespace=page.namespace, revision=page.source_revision, complete=True,
        entries=tuple(Resource(e.name, e.resource_id, e.kind, e.is_alias)
                      for e in page.rows),
    ) for page in pages)
    return preview_root(selection, evidence), chain


def read_root_status(
    account: TrustedDavAccount,
    transport: DavTransport,
    path: str,
    *,
    proof: ProviderProof = CURRENT_PROVIDER_PROOF,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str | None, str]:
    """One Depth:0 ``PROPFIND`` of a root: ``(oc:id, "ok")`` or ``(None, reason)``.

    Identity only: a pinned root inside a share that later became read-only
    or nested still reads ``ok`` (same object; reads keep working, writes fail
    on the server). The walk, not this check, enforces those rules.

    Reasons: ``provider_unproven``, ``invalid_root`` (bad account/path),
    ``unreadable`` (transport, status or parse failure), ``unsupported_mount`` (a
    mount outside the accepted scope: under V1-S2 any mount type other than a
    received share), ``not_a_folder``.
    """
    if (type(proof) is not ProviderProof
            or not proof.admits(restricted_coherence=True)):
        return None, "provider_unproven"
    if (not _account_ok(account)
            or type(path) is not str or _parts(path, new_candidate=False) is None):
        return None, "invalid_root"
    try:
        rows, _ = _Walk(account, transport, clock).propfind(path, "0")
    except _Blocked:
        return None, "unreadable"
    if len(rows) != 1:
        return None, "unreadable"
    href = _canonical_href(rows[0].href)
    expected = _expected_parent_href(account.account_prefix, path)
    if not rows[0].collection:
        # Only to classify a file (its href has no trailing slash).
        return (None, "not_a_folder") if href == expected.rstrip("/") else (None, "unreadable")
    if href != expected:  # a folder must answer at its exact collection href
        return None, "unreadable"
    if rows[0].is_alias:
        return None, "unsupported_mount"
    return rows[0].resource_id, "ok"


def read_root_identity(
    account: TrustedDavAccount,
    transport: DavTransport,
    path: str,
    *,
    proof: ProviderProof = CURRENT_PROVIDER_PROOF,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """One Depth:0 ``PROPFIND`` of a pinned root for the Job pre-run check.

    Returns the root's ``oc:id``, or ``None`` when the provider is unproven,
    the account binding is invalid, or the root is not a plain directory of
    the bound instance. ``None`` must stop the Job (fail-closed).
    """
    return read_root_status(account, transport, path, proof=proof, clock=clock)[0]
