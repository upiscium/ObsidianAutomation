"""Detached, fail-closed B3 MAC-to-local-replay-journal handoff.

This unattached source-only module binds the *exact immutable bytes* of two
locally MAC-validated envelopes to their recorded issuer sequences. It does
not authenticate real Nextcloud/legacy-host issuers: the supplied HMAC keys,
sequence floor and epoch are not independently trusted. It never grants B3
cutover authority and does not operate a Writer or provider.
"""
from __future__ import annotations

import hashlib
import re

from .github_b3_authenticated_evidence import (
    B3DetachedIssuerKeys,
    B3DetachedSequenceFloor,
    VERSION as MAC_VERSION,
    _HOST,
    _MAX_ENVELOPE,
    _PROVIDER,
    _loads_exact,
    assess_b3_authenticated_evidence,
)
from .github_b3_replay_journal import (
    B3ReplayDiagnostic,
    VERSION as JOURNAL_VERSION,
    append_b3_replay_diagnostic,
)

VERSION = "github-b3-mac-replay-binding/v1"
_MAC_CANDIDATE = "detached_mac_valid_external_authority_unverified"
_DOMAIN = b"obsidian-b3-mac-to-replay-journal/v1\x00"
_ID = re.compile(r"[0-9a-f]{32}\Z")


def _result(state: str, reason: str) -> dict[str, object]:
    return {
        "schema_version": VERSION, "state": state, "reason": reason,
        "cutover_authorized": False,
    }


def _bundle_digest(provider_raw: bytes, host_raw: bytes) -> str:
    """Domain + fixed ordering + length prefix: never caller-chosen digest."""
    return hashlib.sha256(
        _DOMAIN
        + len(provider_raw).to_bytes(4, "big") + provider_raw
        + len(host_raw).to_bytes(4, "big") + host_raw
    ).hexdigest()


def assess_and_record_b3_mac_bound_replay(
    provider_envelope: bytes,
    host_envelope: bytes,
    *,
    trusted_issuer_keys: B3DetachedIssuerKeys,
    trusted_sequence_floor: B3DetachedSequenceFloor,
    epoch: str,
    binding_ref: str,
    revision: str,
    expected_generation: str,
    now: float,
) -> dict[str, object]:
    """MAC-validate then append the *same* bundle under the journal lock.

    No journal I/O occurs before both MACs and the receipt cross-check pass.
    Only a synthetically valid detached-MAC candidate is admitted to the
    *local* record step; a changed or stale bundle is always refused.

    Under this contract, root must independently control the caller,
    key provenance, epoch and durable floor; none of that is provided here.
    Even a successful local fsync is NOT external issuer authentication,
    replay rollback-resistance, or a B3CutoverObservation.
    """
    if (
        type(epoch) is not str or _ID.fullmatch(epoch) is None
        or type(provider_envelope) is not bytes
        or not 0 < len(provider_envelope) <= _MAX_ENVELOPE
        or type(host_envelope) is not bytes
        or not 0 < len(host_envelope) <= _MAX_ENVELOPE
    ):
        return _result("unknown", "invalid_mac_replay_input")

    try:
        assessment = assess_b3_authenticated_evidence(
            provider_envelope, host_envelope,
            trusted_issuer_keys=trusted_issuer_keys,
            trusted_sequence_floor=trusted_sequence_floor,
            binding_ref=binding_ref, revision=revision,
            expected_generation=expected_generation, now=now,
        )
        if (
            type(assessment) is not dict
            or assessment.get("schema_version") != MAC_VERSION
            or assessment.get("cutover_authorized") is not False
            or assessment.get("state") not in ("candidate", "unknown", "blocked")
        ):
            return _result("unknown", "mac_verifier_unavailable")
        if assessment["state"] == "blocked":
            return _result("blocked", "mac_evidence_refused")
        if assessment["state"] != "candidate":
            return _result("unknown", "mac_evidence_unavailable")
        if assessment.get("reason") != _MAC_CANDIDATE:
            return _result("unknown", "mac_verifier_unavailable")

        # These exact bytes have *already* passed the dual-issuer MAC and
        # cross-receipt checks. Canonical parsing reuses the same bytes; no
        # caller-supplied digest, sequence, issuer, key ID or claim is read.
        provider = _loads_exact(provider_envelope)
        host = _loads_exact(host_envelope)
        if (
            type(trusted_issuer_keys) is not B3DetachedIssuerKeys
            or type(trusted_sequence_floor) is not B3DetachedSequenceFloor
            or provider.get("issuer") != _PROVIDER
            or host.get("issuer") != _HOST
            or provider.get("key_id") != trusted_issuer_keys.provider_key_id
            or host.get("key_id") != trusted_issuer_keys.host_key_id
            or type(provider.get("sequence")) is not int
            or type(host.get("sequence")) is not int
            or provider["sequence"] <= trusted_sequence_floor.provider_sequence
            or host["sequence"] <= trusted_sequence_floor.host_sequence
        ):
            return _result("unknown", "mac_binding_unavailable")

        observation = B3ReplayDiagnostic(
            epoch=epoch, binding_ref=binding_ref, revision=revision,
            source_generation=expected_generation,
            provider_key_id=trusted_issuer_keys.provider_key_id,
            host_key_id=trusted_issuer_keys.host_key_id,
            provider_sequence=provider["sequence"],
            host_sequence=host["sequence"],
            bundle_sha256=_bundle_digest(provider_envelope, host_envelope),
        )
        # Journal synchronizes competing cooperative processes on the same
        # locked inode. The ledger checks its own *latest* sequence pair;
        # the caller's injected floor is NOT authoritative for replay.
        journal = append_b3_replay_diagnostic(observation)
        if (
            type(journal) is not dict
            or journal.get("schema_version") != JOURNAL_VERSION
            or journal.get("cutover_authorized") is not False
        ):
            return _result("unknown", "local_journal_unavailable")
        if (
            journal.get("state") == "recorded"
            and journal.get("reason") == "local_sequence_checkpoint_only"
        ):
            return _result(
                "candidate",
                "mac_bound_local_checkpoint_external_authority_unverified",
            )
        if journal.get("state") == "blocked":
            return _result("blocked", "local_replay_journal_refused")
        return _result("unknown", "local_journal_unavailable")
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError,
            UnicodeError, RecursionError, OSError):
        return _result("unknown", "mac_binding_unavailable")
