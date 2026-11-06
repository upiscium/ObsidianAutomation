"""Pure, fail-closed B3 legacy Writer authority retirement assessment.

This is NOT a source of trusted cutover authority: observations below may be
forged by a caller, and only a later independent privileged provider can
attest a remote Nextcloud revocation or host isolation after a restart.
Even an ideal candidate is NOT B3CutoverObservation(operator_approved=True).
No credentials, hosts, Nextcloud, systemd, journal, or Writer are accessed.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

VERSION = "github-b3-legacy-retirement/v1"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MAX_TIME = 253402300799
_MAX_AGE = 60


@dataclass(frozen=True, slots=True)
class B3LegacyRetirementClaims:
    """Diagnostic *claims* only; NOT trusted provider/host proof.

    Token fingerprints must be stable administrative IDs, not plaintext or
    a raw credential hash used as a login verifier. A credential inventory
    that omits alternate principals is not complete.
    """

    binding_ref: str
    revision: str
    old_host: str
    new_host: str
    captured_at: float
    provider_captured_at: float
    host_captured_at: float
    legacy_principals: tuple[str, ...]
    known_token_ids: tuple[str, ...]
    provider_revoked_token_ids: tuple[str, ...]
    all_principals_inventory_complete: bool
    provider_denies_old_write: bool
    old_host_restarted_since_fencing: bool
    old_writer_units_inert_after_restart: bool
    old_host_network_isolated: bool
    alternative_old_tokens_absent: bool


def _time(t: object, now: float) -> bool:
    try:
        return (
            type(t) in (int, float)
            and math.isfinite(t)
            and 0 <= t <= min(now, _MAX_TIME)
            and now - t <= _MAX_AGE
        )
    except (ValueError, TypeError, OverflowError):
        return False


def _valid_now(now: object) -> bool:
    try:
        return (
            type(now) in (int, float)
            and math.isfinite(now)
            and 0 <= now <= _MAX_TIME
        )
    except (ValueError, TypeError, OverflowError):
        return False


def _result(state: str, reason: str) -> dict[str, object]:
    return {
        "schema_version": VERSION,
        "state": state,
        "reason": reason,
        "cutover_authorized": False,
    }


def assess_b3_legacy_retirement(
    claim: B3LegacyRetirementClaims,
    *,
    binding_ref: str,
    revision: str,
    now: float,
) -> dict[str, object]:
    """Assess completeness; NEVER mint a trusted B3 cutover observation.

    All-host inventory, provider-confirmed credential revocation, provider
    denial of a write and after-restart host exclusion are independent
    requirements. Neither source authentication nor lifetime locking can be
    proven by these in-process claim fields.
    """
    if (
        type(claim) is not B3LegacyRetirementClaims
        or type(binding_ref) is not str
        or type(revision) is not str
        or not _ID.fullmatch(binding_ref)
        or not _ID.fullmatch(revision)
        or not _valid_now(now)
    ):
        return _result("unknown", "invalid_observation")

    ids = (claim.binding_ref, claim.revision)
    if (
        any(type(x) is not str or not _ID.fullmatch(x) for x in ids)
        or ids != (binding_ref, revision)
        or claim.old_host != "obsidian-automation"
        or claim.new_host != "obsidian-control"
        or not all(_time(t, now) for t in (
            claim.captured_at, claim.provider_captured_at,
            claim.host_captured_at,
        ))
        or type(claim.legacy_principals) is not tuple
        or not 1 <= len(claim.legacy_principals) <= 32
        or any(type(x) is not str
               or not re.fullmatch(r"[a-z0-9_.-]{1,64}", x)
               for x in claim.legacy_principals)
        or len(set(claim.legacy_principals)) != len(claim.legacy_principals)
        or type(claim.known_token_ids) is not tuple
        or type(claim.provider_revoked_token_ids) is not tuple
        or not 0 <= len(claim.provider_revoked_token_ids) <= 128
        or not 1 <= len(claim.known_token_ids) <= 128
        or any(type(x) is not str or not _SHA.fullmatch(x)
               for x in (
                   *claim.known_token_ids,
                   *claim.provider_revoked_token_ids,
               ))
        or len(set(claim.known_token_ids)) != len(claim.known_token_ids)
        or len(set(claim.provider_revoked_token_ids))
           != len(claim.provider_revoked_token_ids)
        or any(type(x) is not bool for x in (
            claim.all_principals_inventory_complete,
            claim.provider_denies_old_write,
            claim.old_host_restarted_since_fencing,
            claim.old_writer_units_inert_after_restart,
            claim.old_host_network_isolated,
            claim.alternative_old_tokens_absent,
        ))
    ):
        return _result("unknown", "invalid_observation")

    if not claim.all_principals_inventory_complete:
        return _result("blocked", "legacy_principal_inventory_incomplete")
    if not claim.alternative_old_tokens_absent:
        return _result("blocked", "alternate_legacy_write_authority_unknown")
    if set(claim.provider_revoked_token_ids) != set(claim.known_token_ids):
        return _result("blocked", "legacy_tokens_not_fully_revoked")
    if not claim.provider_denies_old_write:
        return _result("blocked", "old_provider_write_not_denied")
    if not (
        claim.old_host_restarted_since_fencing
        and claim.old_writer_units_inert_after_restart
        and claim.old_host_network_isolated
    ):
        return _result("blocked", "restart_persistent_host_exclusion_unverified")
    # Local claims can establish a *candidate* only. The privileged
    # verifier must obtain provider/host source-backed receipts and make its
    # own decision. Never expose an operator-approved token from this module.
    return _result("candidate", "source_authenticity_unverified")
