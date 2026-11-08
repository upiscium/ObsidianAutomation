"""Offline, fail-closed Vault root preview contract (#303 V1).

This module never lists a provider, opens a connection, saves credentials,
changes a binding, or authorizes canonical writes. A future *trusted* server
adapter must produce complete, scoped and revision-bound listing evidence.
Objects received directly from a browser must not be treated as evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


MAX_COMPONENTS = 16
MAX_SIBLINGS = 2048
MAX_SIBLING_NAME_CHARS = 255
_ROOT = re.compile(r"[A-Za-z0-9_./ -]{1,100}\Z", re.ASCII)
_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z", re.ASCII)


@dataclass(frozen=True)
class RootSelection:
    requested: str
    active: str
    active_resource_id: str
    expected_revision: str
    current_revision: str
    namespace: str
    container_resource_id: str
    jobs_in_flight: bool = False


@dataclass(frozen=True)
class Resource:
    name: str
    resource_id: str
    kind: str
    is_alias: bool = False


@dataclass(frozen=True)
class DirectoryEvidence:
    """Complete immediate children, gathered by a trusted read-only adapter.

    parent and parent_resource_id form an exact canonical chain from the
    dedicated account root; the revision and namespace bind all observations.
    Provider redirects/symlinks/aliases must be classified, never assumed safe.
    """
    parent: str
    parent_resource_id: str
    namespace: str
    revision: str
    complete: bool
    entries: tuple[Resource, ...]


@dataclass(frozen=True)
class RootPreview:
    version: int
    status: str
    reason: str
    writes_vault: bool = False
    activation_allowed: bool = False
    requires_explicit_activation: bool = True


def _result(status: str, reason: str) -> RootPreview:
    return RootPreview(version=1, status=status, reason=reason)


def _identifier(value: object) -> bool:
    return type(value) is str and _ID.fullmatch(value) is not None


def _parts(value: object, *, new_candidate: bool) -> tuple[str, ...] | None:
    # The active root obeys the *legacy* saved-root grammar, including
    # dot-prefixed names and >16 components. Only new candidates are stricter.
    if type(value) is not str or _ROOT.fullmatch(value) is None:
        return None
    parts = tuple(value.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        return None
    if new_candidate and (
        len(parts) > MAX_COMPONENTS or any(part.startswith(".") for part in parts)
    ):
        return None
    return parts


def _fold(name: str) -> str:
    # Unicode compatibility-caseless matching: decompose canonically BEFORE
    # the first fold, then apply compatibility decomposition and a second fold.
    # This combines canonical and compatibility aliases without depending on
    # the order of two independent key comparisons.
    folded = unicodedata.normalize("NFD", name).casefold()
    folded = unicodedata.normalize("NFKD", folded).casefold()
    return unicodedata.normalize("NFKD", folded)


def _legacy_fold(name: str) -> str:
    # Preserve all collisions rejected by the already-published V1/A key.
    folded = unicodedata.normalize("NFKC", name).casefold()
    return unicodedata.normalize("NFKC", folded)


def preview_root(
    request: RootSelection,
    inventory: tuple[DirectoryEvidence, ...],
) -> RootPreview:
    """Assess an offline root candidate using trusted provider evidence only.

    Never returns raw names/paths, URLs, credentials or resource IDs. Even a
    valid preview cannot be used as an activation or write authorization.
    """
    if type(request) is not RootSelection or type(inventory) is not tuple:
        return _result("blocked", "invalid_request")
    # Dataclass annotations do not enforce exact runtime types. In particular
    # a str subclass may override != and escape the new/legacy path gate.
    if type(request.requested) is not str or type(request.active) is not str:
        return _result("blocked", "invalid_path")
    if not all(
        _identifier(v)
        for v in (
            request.expected_revision, request.current_revision, request.namespace,
            request.active_resource_id, request.container_resource_id,
        )
    ) or type(request.jobs_in_flight) is not bool:
        return _result("blocked", "invalid_request")

    active = _parts(request.active, new_candidate=False)
    if active is None:
        return _result("blocked", "invalid_path")
    if request.expected_revision != request.current_revision:
        return _result("blocked", "stale_source")
    selected = _parts(
        request.requested,
        new_candidate=request.requested != request.active,
    )
    if selected is None:
        return _result("blocked", "invalid_path")
    if request.requested == request.active:
        # No read needed for a no-op; this is not live provider verification.
        return _result("no_change", "already_selected")
    if request.jobs_in_flight:
        return _result("blocked", "jobs_in_flight")
    if len(inventory) != len(selected):
        return _result("blocked", "incomplete_inventory")

    parent = ""
    expected_parent_id = request.container_resource_id
    seen_ids = {expected_parent_id}
    for component, snapshot in zip(selected, inventory):
        if type(snapshot) is not DirectoryEvidence:
            return _result("blocked", "invalid_inventory")
        # Every evidence identity/scope field must be an ordinary bounded
        # string BEFORE any equality comparison with the request. Otherwise
        # hostile __ne__ methods may spoof namespace/parent/revision checks.
        if (
            type(snapshot.parent) is not str or len(snapshot.parent) > 100
            or not all(_identifier(v) for v in (
                snapshot.parent_resource_id, snapshot.namespace,
                snapshot.revision,
            ))
        ):
            return _result("blocked", "invalid_inventory")
        if (
            snapshot.parent != parent
            or snapshot.parent_resource_id != expected_parent_id
            or snapshot.namespace != request.namespace
            or snapshot.revision != request.current_revision
        ):
            return _result("blocked", "out_of_scope")
        if snapshot.complete is not True or type(snapshot.entries) is not tuple:
            return _result("blocked", "incomplete_inventory")
        if not 1 <= len(snapshot.entries) <= MAX_SIBLINGS:
            return _result("blocked", "incomplete_inventory")

        siblings = set()
        legacy_siblings = set()
        selected_resource = None
        for entry in snapshot.entries:
            if type(entry) is not Resource or type(entry.name) is not str:
                return _result("blocked", "invalid_inventory")
            # Bound untrusted inventory processing BEFORE Unicode normalization.
            if not 1 <= len(entry.name) <= MAX_SIBLING_NAME_CHARS:
                return _result("blocked", "invalid_inventory")
            # A directory inventory contains immediate *single-component*
            # names, including unselected siblings. Do not confuse malformed
            # child names with nested paths or special path components.
            # Do not apply the new-root ASCII grammar to Unicode siblings.
            if entry.name in (".", "..") or "/" in entry.name or "\x00" in entry.name:
                return _result("blocked", "invalid_inventory")
            if not _identifier(entry.resource_id) or type(entry.is_alias) is not bool:
                return _result("blocked", "invalid_inventory")
            if (
                type(entry.kind) is not str
                or entry.kind not in ("directory", "file", "symlink", "unknown")
            ):
                return _result("blocked", "invalid_inventory")
            folded = _fold(entry.name)
            legacy_folded = _legacy_fold(entry.name)
            if (
                not folded or not legacy_folded
                or folded in siblings or legacy_folded in legacy_siblings
            ):
                return _result("blocked", "casefold_collision")
            if entry.resource_id in seen_ids:
                # A duplicate canonical ID *anywhere* in the supplied chain
                # indicates an alias/loop, including unselected sibling rows.
                return _result("blocked", "identity_alias")
            siblings.add(folded)
            legacy_siblings.add(legacy_folded)
            seen_ids.add(entry.resource_id)
            if entry.name == component:
                selected_resource = entry

        if selected_resource is None:
            return _result("blocked", "candidate_missing")
        if selected_resource.kind != "directory" or selected_resource.is_alias:
            return _result("blocked", "unsafe_target")
        expected_parent_id = selected_resource.resource_id
        parent = component if not parent else parent + "/" + component

    if expected_parent_id == request.active_resource_id:
        # Different path resolving to the old root is not a new target.
        return _result("blocked", "identity_alias")
    return _result("candidate_valid", "requires_revalidation_and_activation")
