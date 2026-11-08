"""#303 V1/B: synthetic-only assembler for trusted Nextcloud DAV evidence.

No HTTP client, credential access, XML parser, persistence, or provider calls.
The *future* authenticated DAV verifier must produce these internal records;
booleans, revision labels and resource IDs here do NOT prove their provenance.
Never deserialize these records from a browser request. No production importer.
"""
from __future__ import annotations

from dataclasses import dataclass

from .vault_root_preview import (
    MAX_COMPONENTS, MAX_SIBLINGS, MAX_SIBLING_NAME_CHARS,
    DirectoryEvidence, Resource, RootPreview, RootSelection,
    _identifier, _parts, preview_root,
)


MAX_PAGES_PER_DIRECTORY = 4
MAX_PAGE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class VerifiedDavEntry:
    """Provider row after a trusted verifier has checked canonical identity."""
    name: str
    resource_id: str
    kind: str
    is_alias: bool
    canonical_identity_verified: bool
    alias_classification_verified: bool


@dataclass(frozen=True)
class VerifiedDavPage:
    """Transport-neutral, server-only page, NOT an untrusted HTTP payload.

    The future verifier must guarantee that parent_id is provider canonical,
    identity_scope_verified has actually been demonstrated, and snapshot_token
    describes one coherent directory snapshot, not merely a connection ETag.
    """
    parent: str
    parent_resource_id: str
    namespace: str
    source_revision: str
    snapshot_token: str
    cursor: str | None
    next_cursor: str | None
    http_status: int
    redirected: bool
    content_bytes: int
    complete: bool
    identity_scope_verified: bool
    coherent_snapshot_verified: bool
    rows: tuple[VerifiedDavEntry, ...]


def _blocked(reason: str) -> RootPreview:
    # Keep the wire-facing result redacted; never return evidence or raw names.
    return RootPreview(version=1, status="blocked", reason=reason)


def preview_from_verified_dav_pages(
    request: RootSelection, pages: tuple[VerifiedDavPage, ...],
) -> RootPreview:
    """Compose offline verified pages and delegate final decisions to V1/A.

    This does not prove provider assertions. There is intentionally no network
    interface or default credentials. It cannot grant activation authority.
    """
    if type(request) is not RootSelection or type(pages) is not tuple:
        return _blocked("invalid_request")
    # V1/A compares requested to active while deciding the new-path grammar.
    # Reject malformed string subclasses before any user comparison hooks.
    if type(request.requested) is not str or type(request.active) is not str:
        return _blocked("invalid_path")

    # Reuse V1/A for validation of source, new/legacy grammar, no-op, jobs.
    # A valid changed-root path with no inventory must be incomplete.
    initial = preview_root(request, ())
    if initial.reason != "incomplete_inventory":
        return initial

    selected = _parts(request.requested, new_candidate=True)
    if selected is None or not 1 <= len(selected) <= MAX_COMPONENTS:
        return _blocked("invalid_path")
    if not 1 <= len(pages) <= len(selected) * MAX_PAGES_PER_DIRECTORY:
        return _blocked("incomplete_inventory")

    snapshots: list[DirectoryEvidence] = []
    page_index = 0
    parent = ""
    expected_parent_id = request.container_resource_id
    shared_token: str | None = None
    total_bytes = 0

    for component in selected:
        entries: list[Resource] = []
        seen_cursors: set[str] = set()
        expected_cursor: str | None = None
        this_parent_token: str | None = None
        finished = False

        for page_number in range(MAX_PAGES_PER_DIRECTORY):
            if page_index >= len(pages):
                return _blocked("incomplete_inventory")
            page = pages[page_index]
            page_index += 1
            if type(page) is not VerifiedDavPage:
                return _blocked("invalid_inventory")
            if type(page.http_status) is not int or page.http_status != 207:
                return _blocked("provider_unavailable")
            if page.redirected is not False:
                return _blocked("provider_redirect")
            if page.identity_scope_verified is not True:
                return _blocked("identity_unverified")
            if page.coherent_snapshot_verified is not True:
                return _blocked("snapshot_unverified")
            if not all(_identifier(value) for value in (
                page.parent_resource_id, page.namespace, page.source_revision,
            )):
                # Exact-string types before equality: no hostile __ne__ hooks.
                return _blocked("invalid_inventory")
            if (
                type(page.parent) is not str or page.parent != parent
                or page.parent_resource_id != expected_parent_id
                or page.namespace != request.namespace
                or page.source_revision != request.current_revision
            ):
                return _blocked("out_of_scope")
            if not _identifier(page.snapshot_token):
                return _blocked("snapshot_unverified")
            if shared_token is None:
                shared_token = page.snapshot_token
            if page.snapshot_token != shared_token:
                return _blocked("snapshot_unverified")
            if this_parent_token is None:
                this_parent_token = page.snapshot_token
            if page.snapshot_token != this_parent_token:
                return _blocked("snapshot_unverified")
            if page.cursor is not None and not _identifier(page.cursor):
                return _blocked("invalid_inventory")
            if page.cursor != expected_cursor:
                return _blocked("incomplete_inventory")
            if page.next_cursor is not None and not _identifier(page.next_cursor):
                return _blocked("invalid_inventory")
            if page.next_cursor is not None:
                if not _identifier(page.next_cursor) or page.next_cursor in seen_cursors:
                    return _blocked("incomplete_inventory")
                seen_cursors.add(page.next_cursor)
            if (
                type(page.content_bytes) is not int
                or not 0 <= page.content_bytes <= MAX_PAGE_BYTES
            ):
                return _blocked("limit_exceeded")
            total_bytes += page.content_bytes
            if total_bytes > MAX_TOTAL_BYTES:
                return _blocked("limit_exceeded")
            if type(page.rows) is not tuple:
                return _blocked("invalid_inventory")
            if not 1 <= len(page.rows) <= MAX_SIBLINGS - len(entries):
                return _blocked("limit_exceeded")

            for item in page.rows:
                if type(item) is not VerifiedDavEntry:
                    return _blocked("invalid_inventory")
                if item.canonical_identity_verified is not True:
                    return _blocked("identity_unverified")
                if item.alias_classification_verified is not True:
                    return _blocked("alias_unverified")
                if (
                    type(item.name) is not str
                    or not 1 <= len(item.name) <= MAX_SIBLING_NAME_CHARS
                    or not _identifier(item.resource_id)
                    or type(item.kind) is not str
                    or item.kind not in ("directory", "file", "symlink", "unknown")
                    or type(item.is_alias) is not bool
                ):
                    return _blocked("invalid_inventory")
                entries.append(Resource(
                    name=item.name, resource_id=item.resource_id,
                    kind=item.kind, is_alias=item.is_alias,
                ))

            if type(page.complete) is not bool:
                return _blocked("incomplete_inventory")
            if page.complete:
                if page.next_cursor is not None:
                    return _blocked("incomplete_inventory")
                finished = True
                break
            if page.next_cursor is None:
                return _blocked("incomplete_inventory")
            expected_cursor = page.next_cursor

        if not finished:
            return _blocked("incomplete_inventory")

        # Delegate all canonical/sibling collision and identity-loop
        # decisions to V1/A, rather than silently duplicating policy.
        snapshots.append(DirectoryEvidence(
            parent=parent, parent_resource_id=expected_parent_id,
            namespace=request.namespace, revision=request.current_revision,
            complete=True, entries=tuple(entries),
        ))
        selected_entry = next((e for e in entries if e.name == component), None)
        if selected_entry is None:
            return _blocked("candidate_missing")
        expected_parent_id = selected_entry.resource_id
        parent = component if not parent else parent + "/" + component

    if page_index != len(pages):
        return _blocked("incomplete_inventory")
    return preview_root(request, tuple(snapshots))
