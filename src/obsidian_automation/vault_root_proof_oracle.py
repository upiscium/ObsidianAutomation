"""#303 V1/B1 R1: synthetic fixture oracle and coherence *non-proof*.

This pure module has NO transport, file/provider/credential access, UI, or
activation authority. Its dataclasses are caller-constructed assertions,
NOT attested Nextcloud evidence. In particular, neither a complete-looking
inventory nor repeated matching observations certify provider completeness
or cross-level atomicity. Nothing here may be wired to a production route.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from .vault_root_preview import _fold, _legacy_fold

MAX_FIXTURE_MEMBERS = 2048
MAX_TIMELINE_FRAMES = 64
MAX_NAME_CHARS = 255
_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z", re.ASCII)


@dataclass(frozen=True)
class SyntheticMember:
    name: str
    resource_id: str


@dataclass(frozen=True)
class SyntheticAccountView:
    """Fabricated expected/observed account-visible immediate children.

    'expected' is a fixture owner's independently preseeded account-visible
    oracle, not a proof that the remote service returns every needed object.
    'hidden_children_uncertain' is a hard block, even if the visible rows match.
    """
    namespace: str
    source_revision: str
    parent_resource_id: str
    entries: tuple[SyntheticMember, ...]
    hidden_children_uncertain: bool = False
    complete_claim: bool = True


@dataclass(frozen=True)
class SyntheticTraceFrame:
    """One fabricated observation or independently known mutation event."""
    tick: int
    source_revision: str
    view_fingerprint: str
    mutation_event: bool = False


@dataclass(frozen=True)
class SyntheticProofResult:
    status: str
    reason: str
    provider_verified: bool = False
    writes_vault: bool = False
    activation_allowed: bool = False
    requires_explicit_activation: bool = True


def _deny(reason: str) -> SyntheticProofResult:
    return SyntheticProofResult("blocked", reason)


def _id(value: object) -> bool:
    return type(value) is str and _ID.fullmatch(value) is not None


def _valid_view(view: object) -> str | None:
    if type(view) is not SyntheticAccountView:
        return "invalid_fixture"
    if not all(_id(v) for v in (
        view.namespace, view.source_revision, view.parent_resource_id,
    )):
        return "invalid_fixture"
    if (type(view.hidden_children_uncertain) is not bool
            or type(view.complete_claim) is not bool
            or type(view.entries) is not tuple):
        return "invalid_fixture"
    if not 1 <= len(view.entries) <= MAX_FIXTURE_MEMBERS:
        return "invalid_fixture"
    names: set[str] = set()
    ids: set[str] = {view.parent_resource_id}
    folded_names: set[str] = set()
    legacy_folded_names: set[str] = set()
    for member in view.entries:
        if (type(member) is not SyntheticMember
                or type(member.name) is not str
                or not 1 <= len(member.name) <= MAX_NAME_CHARS
                or member.name in (".", "..")
                or "/" in member.name or "\x00" in member.name
                or not _id(member.resource_id)):
            return "invalid_fixture"
        # Follow V1/A's exact canonical/legacy caseless collision policy
        # on ALL fabricated siblings, including unselected rows.
        folded = _fold(member.name)
        legacy = _legacy_fold(member.name)
        if (
            member.name in names
            or member.resource_id in ids
            or folded in folded_names
            or legacy in legacy_folded_names
        ):
            return "duplicate_fixture_identity"
        names.add(member.name)
        ids.add(member.resource_id)
        folded_names.add(folded)
        legacy_folded_names.add(legacy)
    return None


def compare_synthetic_account_view(
    expected: SyntheticAccountView,
    observed: SyntheticAccountView,
) -> SyntheticProofResult:
    """Compare *finite fabricated* expected and observed authorized views.

    A match is ONLY finite_fixture_match, never real provider proof.
    Missing, extra, aliased or hidden-uncertain rows are fail-closed.
    """
    if (issue := _valid_view(expected)) is not None:
        return _deny(issue)
    if (issue := _valid_view(observed)) is not None:
        return _deny(issue)
    if (
        expected.namespace != observed.namespace
        or expected.source_revision != observed.source_revision
        or expected.parent_resource_id != observed.parent_resource_id
    ):
        return _deny("scope_or_revision_changed")
    if expected.hidden_children_uncertain or observed.hidden_children_uncertain:
        return _deny("hidden_scope_unproved")
    if not expected.complete_claim or not observed.complete_claim:
        return _deny("incomplete_claim")
    # Exact (name, identity) pairs are a fixture equality check. Do not use
    # this as global alias, mount, symlink, ACL or snapshot proof.
    expected_rows = {(m.name, m.resource_id) for m in expected.entries}
    observed_rows = {(m.name, m.resource_id) for m in observed.entries}
    if expected_rows != observed_rows:
        return _deny("fixture_inventory_mismatch")
    return SyntheticProofResult("finite_fixture_match", "provider_unverified")


def assess_synthetic_coherence(
    frames: tuple[SyntheticTraceFrame, ...],
) -> SyntheticProofResult:
    """Reject known mutations/drift; unchanged samples remain UNKNOWN.

    A-B-A may be invisible between reads. Even no known mutation cannot
    prove the preview-to-save interval is fenced by the remote provider.
    """
    if type(frames) is not tuple or not 2 <= len(frames) <= MAX_TIMELINE_FRAMES:
        return _deny("invalid_trace")
    prior_tick = -1
    first_revision = None
    first_fingerprint = None
    mutation_seen = False
    drift_seen = False
    for frame in frames:
        if (type(frame) is not SyntheticTraceFrame
                or type(frame.tick) is not int or frame.tick <= prior_tick
                or frame.tick < 0 or not _id(frame.source_revision)
                or not _id(frame.view_fingerprint)
                or type(frame.mutation_event) is not bool):
            return _deny("invalid_trace")
        prior_tick = frame.tick
        if first_revision is None:
            first_revision = frame.source_revision
            first_fingerprint = frame.view_fingerprint
        else:
            if frame.source_revision != first_revision:
                return _deny("stale_source")
            if frame.view_fingerprint != first_fingerprint:
                drift_seen = True
        mutation_seen = mutation_seen or frame.mutation_event
    if mutation_seen or drift_seen:
        return _deny("known_tree_mutation")
    # This NEVER upgrades an unchanging sample sequence to provider proof.
    return SyntheticProofResult("unknown", "provider_atomicity_unproved")
