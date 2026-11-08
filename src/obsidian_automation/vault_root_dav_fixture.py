"""#303 V1/B: pure parser for synthetic, already-captured DAV evidence.

No HTTP client, credential loader, network/provider access, server endpoint,
disk persistence, job effects, or activation exists in this module.  Its
caller-provided classifications and revision strings are NOT verified
authentication or identity proof.  The only admitted origin is a fabricated
synthetic fixture; production must have a separately approved authenticated
transport/identity adapter that establishes those properties before adoption.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import quote, unquote_to_bytes, urlsplit
from xml.etree import ElementTree as ET

from .vault_root_preview import (
    DirectoryEvidence, Resource, RootPreview, RootSelection,
    MAX_SIBLINGS, MAX_SIBLING_NAME_CHARS, preview_root,
)

DAV = "{DAV:}"
OC = "{http://owncloud.org/ns}"
_MAX_XML_BYTES = 1_048_576
_MAX_XML_NODES = 18_000
_MAX_HREF_CHARS = 1024
_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z", re.ASCII)
_ACCOUNT = re.compile(r"/remote\.php/dav/files/(?!\.{1,2}/)[A-Za-z0-9_.-]+/\Z", re.ASCII)
_OK_STATUS = "HTTP/1.1 200 OK"


@dataclass(frozen=True)
class SyntheticDavSource:
    """Fixture-supplied context; NEVER an authentication attestation.

    A future real transport must independently establish account binding,
    globally stable canonical IDs, redirect/alias classification and coherent
    snapshots. No runtime code should construct this from a browser payload.
    """

    account_prefix: str
    namespace: str
    root_resource_id: str
    source_revision: str
    listing_revision: str
    origin: str = "synthetic_fixture"


@dataclass(frozen=True)
class SyntheticClassification:
    """One fabricated trusted-classifier result, bound to href AND file ID."""

    href: str
    resource_id: str
    kind: str
    is_alias: bool


@dataclass(frozen=True)
class SyntheticDavCapture:
    """One synthetic Depth:1 PROPFIND response, not an HTTP transport."""

    parent: str
    parent_resource_id: str
    namespace: str
    source_revision: str
    listing_revision: str
    http_status: int
    depth: str
    content_type: str
    redirect_count: int
    complete: bool
    has_more: bool
    content_encoding: str
    body: bytes
    classifications: tuple[SyntheticClassification, ...]
    parent_alias: bool = False
    origin: str = "synthetic_fixture"


class _Blocked(Exception):
    def __init__(self, code: str):
        self.code = code


def _deny(reason: str) -> RootPreview:
    return RootPreview(version=1, status="blocked", reason=reason)


def _valid_id(value: object) -> bool:
    return type(value) is str and _ID.fullmatch(value) is not None


def _canonical_href(href: object) -> str:
    # Canonical, path-only, UTF-8 encoded hrefs. No host, query, fragment,
    # encoded slash/backslash, dot segments or double-decoding ambiguity.
    if type(href) is not str or not 1 <= len(href) <= _MAX_HREF_CHARS:
        raise _Blocked("unsafe_href")
    if not href.isascii() or not href.startswith("/") or href.startswith("//"):
        raise _Blocked("unsafe_href")
    if re.search(r"%(?:2f|5c)", href, re.IGNORECASE):
        raise _Blocked("unsafe_href")
    try:
        parts = urlsplit(href)
        decoded = unquote_to_bytes(href).decode("utf-8", "strict")
    except (ValueError, UnicodeError):
        raise _Blocked("unsafe_href") from None
    if parts.scheme or parts.netloc or parts.query or parts.fragment:
        raise _Blocked("unsafe_href")
    if decoded.startswith("//") or "\\" in decoded or "?" in decoded or "#" in decoded:
        raise _Blocked("unsafe_href")
    if any(ord(c) < 32 or ord(c) == 127 for c in decoded):
        raise _Blocked("unsafe_href")
    if any(p in ("", ".", "..") for p in decoded.split("/")[1:-1]):
        raise _Blocked("unsafe_href")
    if quote(decoded, safe="/") != href:
        raise _Blocked("unsafe_href")
    return decoded


def _expected_parent_href(account_prefix: str, parent: str) -> str:
    if not parent:
        return _canonical_href(account_prefix)
    encoded = account_prefix + "/".join(
        quote(part, safe="") for part in parent.split("/")
    ) + "/"
    # All response and classification hrefs are compared as DECODED paths.
    return _canonical_href(encoded)


def _required_text(node: ET.Element, tag: str) -> str:
    found = node.findall(tag)
    if len(found) != 1 or len(found[0]) or found[0].text is None:
        raise _Blocked("invalid_dav_response")
    value = found[0].text
    if not value or len(value) > _MAX_HREF_CHARS:
        raise _Blocked("invalid_dav_response")
    return value


def _parse_xml(body: bytes) -> list[ET.Element]:
    if type(body) is not bytes or not 1 <= len(body) <= _MAX_XML_BYTES:
        raise _Blocked("invalid_dav_response")
    # Decode the complete bounded body BEFORE scanning. A byte-only scan
    # would miss DTD/entity declarations encoded as UTF-16 or UTF-32.
    try:
        decoded = body.decode("utf-8", "strict")
    except UnicodeError:
        raise _Blocked("invalid_dav_response") from None
    if "\x00" in decoded or decoded.startswith("\ufeff"):
        raise _Blocked("invalid_dav_response")
    # The body was already capped at 1 MiB: inspect the WHOLE declaration.
    # A 256-character prefix silently missed long non-UTF-8 declarations.
    decl = re.match(r"\s*<\?xml\b[^?]*\?>", decoded, re.IGNORECASE)
    if decl is not None:
        encoding = re.search(
            r"""\bencoding\s*=\s*['"]([^'"]+)['"]""",
            decl.group(0), re.IGNORECASE,
        )
        if encoding is not None and encoding.group(1).lower() != "utf-8":
            raise _Blocked("invalid_dav_response")
    upper = decoded.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper or "<?XML-STYLESHEET" in upper:
        raise _Blocked("invalid_dav_response")
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError, UnicodeError, LookupError):
        raise _Blocked("invalid_dav_response") from None
    if root.tag != DAV + "multistatus":
        raise _Blocked("invalid_dav_response")
    # Guard large synthetic node graphs even when the total body is bounded.
    if sum(1 for _ in root.iter()) > _MAX_XML_NODES:
        raise _Blocked("invalid_dav_response")
    rows = list(root)
    if not 1 <= len(rows) <= MAX_SIBLINGS + 1:
        raise _Blocked("incomplete_inventory")
    if any(row.tag != DAV + "response" for row in rows):
        raise _Blocked("invalid_dav_response")
    return rows


def _row_properties(row: ET.Element) -> tuple[str, str, bool]:
    if any(item.tag not in (DAV + "href", DAV + "propstat") for item in row):
        raise _Blocked("invalid_dav_response")
    href = _required_text(row, DAV + "href")
    propstat = row.findall(DAV + "propstat")
    if len(propstat) != 1 or len(propstat[0]) != 2:
        raise _Blocked("invalid_dav_response")
    status = _required_text(propstat[0], DAV + "status")
    if status != _OK_STATUS:
        raise _Blocked("provider_unavailable")
    props = propstat[0].findall(DAV + "prop")
    if len(props) != 1:
        raise _Blocked("invalid_dav_response")
    prop = props[0]
    fileid = _required_text(prop, OC + "fileid")
    if not _valid_id(fileid):
        raise _Blocked("identity_unverified")
    types = prop.findall(DAV + "resourcetype")
    if len(types) != 1:
        raise _Blocked("identity_unverified")
    resource_type = types[0]
    # A bare DAV:collection is an EMPTY type marker. Merely checking its tag
    # discards nested redirect/alias-like content and mixed text. Reject it
    # before conversion to the small Resource representation consumed by V1/A.
    if resource_type.attrib or (resource_type.text or "").strip():
        raise _Blocked("invalid_dav_response")
    markers = list(resource_type)
    type_tags = [element.tag for element in markers]
    if type_tags not in ([], [DAV + "collection"]):
        raise _Blocked("identity_unverified")
    if any(
        marker.attrib or len(marker)
        or (marker.text or "").strip() or (marker.tail or "").strip()
        for marker in markers
    ):
        raise _Blocked("invalid_dav_response")
    return href, fileid, type_tags == [DAV + "collection"]


def _parse_listing(
    source: SyntheticDavSource,
    capture: SyntheticDavCapture,
    parent: str,
    parent_id: str,
) -> DirectoryEvidence:
    if (type(capture) is not SyntheticDavCapture
            or type(capture.origin) is not str
            or capture.origin != "synthetic_fixture"):
        raise _Blocked("unverified_origin")
    # Check exact types before equality: a synthetic caller can construct
    # str subclasses with hostile __ne__/__eq__ behavior. Never silently
    # rebuild trusted scope using an unvalidated capture's fields.
    if (type(capture.parent) is not str or len(capture.parent) > 100
            or not all(_valid_id(v) for v in (
                capture.parent_resource_id, capture.namespace,
                capture.source_revision, capture.listing_revision,
            ))):
        raise _Blocked("invalid_dav_response")
    if (capture.parent != parent or capture.parent_resource_id != parent_id
            or capture.namespace != source.namespace):
        raise _Blocked("out_of_scope")
    if capture.source_revision != source.source_revision:
        raise _Blocked("stale_source")
    if capture.listing_revision != source.listing_revision:
        raise _Blocked("stale_inventory")
    if type(capture.http_status) is not int or capture.http_status != 207:
        raise _Blocked("provider_unavailable")
    if type(capture.redirect_count) is not int or capture.redirect_count != 0:
        raise _Blocked("redirect_disallowed")
    if (type(capture.depth) is not str or capture.depth != "1"
            or type(capture.content_encoding) is not str
            or capture.content_encoding != "identity"):
        raise _Blocked("invalid_dav_response")
    if capture.complete is not True or capture.has_more is not False:
        raise _Blocked("incomplete_inventory")
    if capture.parent_alias is not False:
        raise _Blocked("identity_unverified")
    if (type(capture.content_type) is not str
            or not 1 <= len(capture.content_type) <= 31
            or capture.content_type.lower() not in (
                "application/xml", "text/xml", "application/xml; charset=utf-8",
                "text/xml; charset=utf-8",
            )):
        raise _Blocked("invalid_dav_response")
    if type(capture.classifications) is not tuple:
        raise _Blocked("identity_unverified")
    if len(capture.classifications) > MAX_SIBLINGS:
        raise _Blocked("incomplete_inventory")
    rows = _parse_xml(capture.body)
    parent_href = _expected_parent_href(source.account_prefix, parent)
    # Pre-validate and bind exact classifier hrefs/IDs. No path-based ID
    # synthesis, no alias inference from resource type or ETag.
    classifier: dict[str, SyntheticClassification] = {}
    for claim in capture.classifications:
        if (type(claim) is not SyntheticClassification
                or type(claim.is_alias) is not bool
                or type(claim.kind) is not str
                or claim.kind not in ("directory", "file", "symlink", "unknown")
                or not _valid_id(claim.resource_id)):
            raise _Blocked("identity_unverified")
        href = _canonical_href(claim.href)
        if href in classifier or not href.startswith(parent_href):
            raise _Blocked("identity_unverified")
        classifier[href] = claim

    seen_hrefs: set[str] = set()
    resources: list[Resource] = []
    saw_self = False
    for row in rows:
        href_raw, fileid, collection = _row_properties(row)
        href = _canonical_href(href_raw)
        if href in seen_hrefs:
            raise _Blocked("invalid_dav_response")
        seen_hrefs.add(href)
        if href == parent_href:
            if saw_self or not collection or fileid != parent_id:
                raise _Blocked("identity_unverified")
            saw_self = True
            continue
        if not href.startswith(parent_href):
            raise _Blocked("out_of_scope")
        remainder = href[len(parent_href):]
        directory_href = remainder.endswith("/")
        leaf = remainder[:-1] if directory_href else remainder
        if (not leaf or "/" in leaf or leaf in (".", "..")
                or "\\" in leaf or "%" in leaf or "?" in leaf or "#" in leaf):
            raise _Blocked("unsafe_href")
        if len(leaf) > MAX_SIBLING_NAME_CHARS:
            raise _Blocked("invalid_dav_response")
        claim = classifier.pop(href, None)
        if claim is None or claim.resource_id != fileid:
            raise _Blocked("identity_unverified")
        if collection != directory_href:
            raise _Blocked("invalid_dav_response")
        if claim.kind == "directory" and not collection:
            raise _Blocked("identity_unverified")
        if claim.kind == "file" and collection:
            raise _Blocked("identity_unverified")
        resources.append(Resource(leaf, fileid, claim.kind, claim.is_alias))
    if not saw_self or len(resources) != len(capture.classifications) or classifier:
        raise _Blocked("incomplete_inventory")
    if not 1 <= len(resources) <= MAX_SIBLINGS:
        raise _Blocked("incomplete_inventory")
    return DirectoryEvidence(
        parent=parent, parent_resource_id=parent_id,
        namespace=source.namespace, revision=source.source_revision,
        complete=True, entries=tuple(resources),
    )


def preview_synthetic_dav_root(
    selection: RootSelection,
    source: SyntheticDavSource,
    captures: tuple[SyntheticDavCapture, ...],
) -> RootPreview:
    """Offline synthetic-only adapter-to-validator exercise.

    NEVER use this output to persist a new root. Neither a fixture's claims
    nor a candidate_valid result prove DAV transport, ACLs, global file-ID
    scope, completeness, or snapshot coherence in an actual Nextcloud server.
    """
    if type(selection) is not RootSelection:
        return _deny("invalid_request")
    # V1/A uses request path comparisons while choosing legacy vs new
    # grammar. Validate exact strings before invoking any comparison hooks.
    if type(selection.requested) is not str or type(selection.active) is not str:
        return _deny("invalid_path")
    preflight = preview_root(selection, ())
    if preflight.reason != "incomplete_inventory":
        return preflight  # malformed/stale/no-op/in-flight are handled by V1/A
    if (type(source) is not SyntheticDavSource
            or type(source.origin) is not str
            or source.origin != "synthetic_fixture"
            or type(captures) is not tuple):
        return _deny("unverified_origin")
    if (type(source.account_prefix) is not str
            or not 1 <= len(source.account_prefix) <= _MAX_HREF_CHARS
            or _ACCOUNT.fullmatch(source.account_prefix) is None
            or not all(_valid_id(v) for v in (
                source.namespace, source.root_resource_id,
                source.source_revision, source.listing_revision,
            ))):
        return _deny("unverified_origin")
    if (selection.namespace != source.namespace
            or selection.container_resource_id != source.root_resource_id):
        return _deny("out_of_scope")
    if selection.current_revision != source.source_revision:
        return _deny("stale_source")
    components = selection.requested.split("/")
    if len(captures) != len(components):
        return _deny("incomplete_inventory")
    parent = ""
    parent_id = source.root_resource_id
    evidence: list[DirectoryEvidence] = []
    for component, capture in zip(components, captures):
        try:
            listing = _parse_listing(source, capture, parent, parent_id)
        except _Blocked as err:
            return _deny(err.code)
        evidence.append(listing)
        selected = next((r for r in listing.entries if r.name == component), None)
        if selected is None:
            return _deny("candidate_missing")
        if selected.kind != "directory" or selected.is_alias:
            return _deny("unsafe_target")
        parent_id = selected.resource_id
        parent = component if not parent else parent + "/" + component
    return preview_root(selection, tuple(evidence))
