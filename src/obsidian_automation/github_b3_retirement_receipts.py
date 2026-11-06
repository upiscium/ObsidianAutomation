"""Unattached B3 provider/old-host retirement receipt cross-check (pure).

This validates a contract that future *independently authenticated* read-only
provider/host probes must produce. A caller can forge both dataclasses today;
even coherent receipts are NEVER an authority token or old-Writer exclusion.
No provider API, network, credentials, old host or service is accessed here.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re

from .github_b3_legacy_retirement import (
    B3LegacyRetirementClaims, assess_b3_legacy_retirement,
)

VERSION = "github-b3-retirement-receipt-crosscheck/v1"
_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_PRINCIPAL = re.compile(r"[a-z0-9_.-]{1,64}\Z")
_MAX_TIME = 253402300799
_FRESH_SECONDS = 60


@dataclass(frozen=True, slots=True)
class B3NextcloudProviderReceipt:
    """Future authenticated provider-issued inventory and *denial* evidence.

    Token IDs are provider audit IDs only, never password material or hashed
    bearer credentials. The issuing/probe account and its authority are not
    authenticated by this in-process dataclass or its fields.
    """

    binding_ref: str
    revision: str
    source_generation: str
    fencing_generation: str
    tenant: str
    observed_at: float
    post_restart_denial_at: float
    inventory_complete: bool
    principals: tuple[str, ...]
    legacy_token_ids: tuple[str, ...]
    provider_revoked_token_ids: tuple[str, ...]
    alternate_write_authority_absent: bool
    old_write_denied_after_restart: bool
    audit_cursor: str


@dataclass(frozen=True, slots=True)
class B3OldHostFenceReceipt:
    """Future independently captured old LXC post-restart observations."""

    binding_ref: str
    revision: str
    source_generation: str
    fencing_generation: str
    host: str
    fenced_at: float
    restarted_at: float
    observed_at: float
    boot_id: str
    principals: tuple[str, ...]
    old_writer_units_inert: bool
    old_host_egress_denied: bool


def _ok_time(value: object, now: object, *, fresh: bool) -> bool:
    try:
        return (
            type(now) in (int, float)
            and math.isfinite(now)
            and 0 <= now <= _MAX_TIME
            and type(value) in (int, float)
            and math.isfinite(value)
            and 0 <= value <= now
            and (not fresh or now - value <= _FRESH_SECONDS)
        )
    except (OverflowError, ValueError, TypeError):
        return False


def _hex(value: object, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def _principals(value: object) -> bool:
    return (
        type(value) is tuple
        and 1 <= len(value) <= 32
        and all(type(p) is str and _PRINCIPAL.fullmatch(p) is not None
                for p in value)
        and tuple(sorted(set(value))) == value
    )


def _tokens(value: object, *, empty_ok: bool = False) -> bool:
    return (
        type(value) is tuple
        and (0 if empty_ok else 1) <= len(value) <= 128
        and all(_hex(x, _SHA) for x in value)
        and tuple(sorted(set(value))) == value
    )


def _result(state: str, reason: str) -> dict[str, object]:
    return {
        "schema_version": VERSION,
        "state": state,
        "reason": reason,
        "cutover_authorized": False,
    }


def assess_b3_retirement_receipts(
    provider: B3NextcloudProviderReceipt,
    old_host: B3OldHostFenceReceipt,
    *,
    binding_ref: str,
    revision: str,
    expected_generation: str,
    now: float,
) -> dict[str, object]:
    """Cross-correlate distinct proposed attestations; never approve cutover.

    The future production verifier must authenticate the independent provider
    and host *before* constructing receipts, and enforce durable audit cursor
    monotonicity, replay/rotation detection and time-of-effect revocation.
    This module cannot and does not make that source trust decision.
    """
    if (
        type(provider) is not B3NextcloudProviderReceipt
        or type(old_host) is not B3OldHostFenceReceipt
        or any(not _hex(x, _ID) for x in (
            binding_ref, revision, expected_generation,
        ))
        or not _ok_time(now, now, fresh=False)
    ):
        return _result("unknown", "invalid_receipts")

    scope = (binding_ref, revision, expected_generation)
    if (
        any(not _hex(x, _ID) for x in (
            provider.binding_ref, provider.revision, provider.source_generation,
            provider.fencing_generation, old_host.binding_ref,
            old_host.revision, old_host.source_generation,
            old_host.fencing_generation, old_host.boot_id,
        ))
        or (
            provider.binding_ref, provider.revision,
            provider.source_generation,
        ) != scope
        or (old_host.binding_ref, old_host.revision,
            old_host.source_generation) != scope
        or provider.fencing_generation != old_host.fencing_generation
        or provider.tenant != "nextcloud"
        or old_host.host != "obsidian-automation"
        or not _hex(provider.audit_cursor, _SHA)
        or not _principals(provider.principals)
        or not _principals(old_host.principals)
        or provider.principals != old_host.principals
        or not _tokens(provider.legacy_token_ids)
        or not _tokens(provider.provider_revoked_token_ids, empty_ok=True)
        or any(type(x) is not bool for x in (
            provider.inventory_complete,
            provider.alternate_write_authority_absent,
            provider.old_write_denied_after_restart,
            old_host.old_writer_units_inert,
            old_host.old_host_egress_denied,
        ))
        or not _ok_time(provider.observed_at, now, fresh=True)
        or not _ok_time(provider.post_restart_denial_at, now, fresh=True)
        or not _ok_time(old_host.observed_at, now, fresh=True)
        or not _ok_time(old_host.restarted_at, now, fresh=False)
        or not _ok_time(old_host.fenced_at, now, fresh=False)
        or not (
            old_host.fenced_at < old_host.restarted_at
            <= old_host.observed_at <= provider.post_restart_denial_at
            <= provider.observed_at
        )
    ):
        return _result("unknown", "invalid_receipts")

    claim = B3LegacyRetirementClaims(
        binding_ref=binding_ref,
        revision=revision,
        old_host="obsidian-automation",
        new_host="obsidian-control",
        captured_at=max(provider.observed_at, old_host.observed_at),
        provider_captured_at=provider.observed_at,
        host_captured_at=old_host.observed_at,
        legacy_principals=provider.principals,
        known_token_ids=provider.legacy_token_ids,
        provider_revoked_token_ids=provider.provider_revoked_token_ids,
        all_principals_inventory_complete=provider.inventory_complete,
        provider_denies_old_write=provider.old_write_denied_after_restart,
        old_host_restarted_since_fencing=True,  # enforced by ordering above
        old_writer_units_inert_after_restart=old_host.old_writer_units_inert,
        old_host_network_isolated=old_host.old_host_egress_denied,
        alternative_old_tokens_absent=provider.alternate_write_authority_absent,
    )
    decision = assess_b3_legacy_retirement(
        claim, binding_ref=binding_ref, revision=revision, now=now,
    )
    if decision["state"] == "blocked":
        return _result("blocked", str(decision["reason"]))
    if decision["state"] != "candidate":
        return _result("unknown", "invalid_receipts")
    return _result("candidate", "independent_source_authentication_missing")
