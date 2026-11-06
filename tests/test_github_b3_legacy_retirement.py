"""Source-only old Writer retirement claims: never grant cutover authority."""
from __future__ import annotations

from dataclasses import replace

import pytest

from obsidian_automation.github_b3_legacy_retirement import (
    B3LegacyRetirementClaims, assess_b3_legacy_retirement,
)

BINDING = "b" * 32
REVISION = "c" * 32
NOW = 1_800_000_000
TOKENS = ("a" * 64, "d" * 64)


def complete_claim():
    return B3LegacyRetirementClaims(
        binding_ref=BINDING, revision=REVISION,
        old_host="obsidian-automation", new_host="obsidian-control",
        captured_at=NOW - 1,
        provider_captured_at=NOW - 2,
        host_captured_at=NOW - 3,
        legacy_principals=("legacy-writer", "legacy-mirror"),
        known_token_ids=TOKENS,
        provider_revoked_token_ids=TOKENS,
        all_principals_inventory_complete=True,
        provider_denies_old_write=True,
        old_host_restarted_since_fencing=True,
        old_writer_units_inert_after_restart=True,
        old_host_network_isolated=True,
        alternative_old_tokens_absent=True,
    )


def assess(claim, *, now=NOW, binding_ref=BINDING, revision=REVISION):
    result = assess_b3_legacy_retirement(
        claim, now=now, binding_ref=binding_ref, revision=revision,
    )
    assert result["schema_version"] == "github-b3-legacy-retirement/v1"
    assert result["cutover_authorized"] is False
    return result


def test_complete_caller_claim_only_candidates_never_approves():
    assert assess(complete_claim()) == {
        "schema_version": "github-b3-legacy-retirement/v1",
        "state": "candidate",
        "reason": "source_authenticity_unverified",
        "cutover_authorized": False,
    }


@pytest.mark.parametrize("field,value,reason", [
    ("all_principals_inventory_complete", False,
     "legacy_principal_inventory_incomplete"),
    ("alternative_old_tokens_absent", False,
     "alternate_legacy_write_authority_unknown"),
    ("provider_revoked_token_ids", (TOKENS[0],),
     "legacy_tokens_not_fully_revoked"),
    ("provider_revoked_token_ids", (TOKENS[0], "e" * 64),
     "legacy_tokens_not_fully_revoked"),
    ("provider_denies_old_write", False,
     "old_provider_write_not_denied"),
    ("old_host_restarted_since_fencing", False,
     "restart_persistent_host_exclusion_unverified"),
    ("old_writer_units_inert_after_restart", False,
     "restart_persistent_host_exclusion_unverified"),
    ("old_host_network_isolated", False,
     "restart_persistent_host_exclusion_unverified"),
])
def test_incomplete_authority_claims_blocked(field, value, reason):
    result = assess(replace(complete_claim(), **{field: value}))
    assert result["state"] == "blocked"
    assert result["reason"] == reason


@pytest.mark.parametrize("field,value", [
    ("binding_ref", "a" * 32),
    ("revision", "e" * 32),
    ("binding_ref", "b" * 31),
    ("revision", 4),
    ("old_host", "arbitrary-old-host"),
    ("new_host", "arbitrary-new-host"),
    ("captured_at", NOW + 1),
    ("provider_captured_at", NOW - 61),
    ("host_captured_at", -1),
    ("captured_at", "1"),
    ("legacy_principals", ()),
    ("legacy_principals", ("legacy-writer", "legacy-writer")),
    ("legacy_principals", ["legacy-writer"]),
    ("legacy_principals", ("NOT_A_USERNAME",)),
    ("known_token_ids", ()),
    ("known_token_ids", (TOKENS[0], TOKENS[0])),
    ("known_token_ids", ["a" * 64]),
    ("provider_revoked_token_ids", (TOKENS[0], TOKENS[0])),
    ("provider_revoked_token_ids", ("q" * 64,)),
    ("all_principals_inventory_complete", 1),
    ("provider_denies_old_write", 1),
    ("old_host_restarted_since_fencing", "true"),
    ("old_writer_units_inert_after_restart", 1),
    ("old_host_network_isolated", 1),
    ("alternative_old_tokens_absent", 1),
])
def test_malformed_claims_cannot_approve(field, value):
    result = assess(replace(complete_claim(), **{field: value}))
    assert result["state"] == "unknown"
    assert result["reason"] == "invalid_observation"


@pytest.mark.parametrize("now", (
    -1, "now", None, True, NOW - 5, float("nan"),
    float("inf"), 1e15, 10**1000,
))
def test_invalid_clock_cannot_approve(now):
    result = assess(complete_claim(), now=now)
    assert result["state"] == "unknown"


@pytest.mark.parametrize("ref", (
    "z" * 32, "a", 1, None, True,
))
def test_wrong_request_binding_refused(ref):
    result = assess(complete_claim(), binding_ref=ref)
    assert result["state"] == "unknown"


def test_mutation_proof_is_not_a_security_signature():
    # In-process claims are forgeable. A caller could construct a complete
    # object, but no pure claim-based API can elevate it into a port's
    # B3CutoverObservation. The runtime must use an independent verifier.
    assert assess(complete_claim())["cutover_authorized"] is False


def test_module_never_issues_operator_approved_cutover():
    from obsidian_automation import github_b3_legacy_retirement as module

    assert not hasattr(module, "B3CutoverObservation")
    assert not hasattr(module, "approve_cutover")
    assert not hasattr(module, "revoke_credentials")
    assert not hasattr(module, "enable_writer")


def test_explicit_wrong_type_fails_closed():
    assert assess({"state": "ready"})["state"] == "unknown"
    assert assess(None)["state"] == "unknown"
