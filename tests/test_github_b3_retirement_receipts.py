"""Provider/host retirement receipts: bounded cross-check, no real probes."""
from __future__ import annotations

from dataclasses import replace

import pytest

from obsidian_automation.github_b3_retirement_receipts import (
    B3NextcloudProviderReceipt, B3OldHostFenceReceipt,
    assess_b3_retirement_receipts,
)

BIND = "b" * 32
REV = "c" * 32
GEN = "d" * 32
FENCE = "e" * 32
TOKENS = ("a" * 64, "f" * 64)
PRINCIPALS = ("legacy-mirror", "legacy-writer")
NOW = 1_800_000_000


def receipts():
    provider = B3NextcloudProviderReceipt(
        binding_ref=BIND, revision=REV, source_generation=GEN,
        fencing_generation=FENCE, tenant="nextcloud",
        observed_at=NOW - 1,
        post_restart_denial_at=NOW - 5,
        inventory_complete=True,
        principals=PRINCIPALS,
        legacy_token_ids=TOKENS,
        provider_revoked_token_ids=TOKENS,
        alternate_write_authority_absent=True,
        old_write_denied_after_restart=True,
        audit_cursor="1" * 64,
    )
    host = B3OldHostFenceReceipt(
        binding_ref=BIND, revision=REV, source_generation=GEN,
        fencing_generation=FENCE, host="obsidian-automation",
        fenced_at=NOW - 300, restarted_at=NOW - 20,
        observed_at=NOW - 10, boot_id="2" * 32,
        principals=PRINCIPALS,
        old_writer_units_inert=True,
        old_host_egress_denied=True,
    )
    return provider, host


def assess(provider, host, *, now=NOW, binding_ref=BIND,
           revision=REV, expected_generation=GEN):
    result = assess_b3_retirement_receipts(
        provider, host, binding_ref=binding_ref, revision=revision,
        expected_generation=expected_generation, now=now,
    )
    assert result["schema_version"] == "github-b3-retirement-receipt-crosscheck/v1"
    assert result["cutover_authorized"] is False
    return result


def test_perfect_synthetic_receipts_are_not_cutover_authority():
    provider, host = receipts()
    assert assess(provider, host) == {
        "schema_version": "github-b3-retirement-receipt-crosscheck/v1",
        "state": "candidate",
        "reason": "independent_source_authentication_missing",
        "cutover_authorized": False,
    }


@pytest.mark.parametrize("field,value", (
    ("binding_ref", "a" * 32),
    ("revision", "f" * 32),
    ("source_generation", "0" * 32),
    ("fencing_generation", "1" * 32),
    ("tenant", "arbitrary-provider"),
    ("audit_cursor", "x" * 64),
    ("audit_cursor", 123),
    ("observed_at", NOW + 1),
    ("observed_at", NOW - 61),
    ("post_restart_denial_at", NOW - 61),
    ("post_restart_denial_at", NOW - 30),
    ("post_restart_denial_at", "yesterday"),
    ("principals", ("legacy-writer", "legacy-mirror")),
    ("principals", ("legacy-writer", "legacy-writer")),
    ("principals", ["legacy-writer"]),
    ("principals", ("Legacy-WRITER",)),
    ("legacy_token_ids", ()),
    ("legacy_token_ids", (TOKENS[0], TOKENS[0])),
    ("legacy_token_ids", ("not-hex",)),
    ("legacy_token_ids", [TOKENS[0]]),
    ("provider_revoked_token_ids", [TOKENS[0]]),
    ("provider_revoked_token_ids", (TOKENS[0], TOKENS[0])),
    ("inventory_complete", 1),
    ("alternate_write_authority_absent", 1),
    ("old_write_denied_after_restart", "yes"),
))
def test_invalid_provider_receipt_fail_closed(field, value):
    provider, host = receipts()
    assert assess(replace(provider, **{field: value}), host)["state"] == "unknown"


@pytest.mark.parametrize("field,value", (
    ("binding_ref", "a" * 32),
    ("revision", "f" * 32),
    ("source_generation", "0" * 32),
    ("fencing_generation", "1" * 32),
    ("host", "other-host"),
    ("fenced_at", NOW - 1),
    ("restarted_at", NOW - 400),
    ("restarted_at", NOW + 1),
    ("restarted_at", 10**1000),
    ("observed_at", NOW - 61),
    ("observed_at", NOW - 100),
    ("observed_at", NOW + 1),
    ("boot_id", "g" * 32),
    ("principals", ("legacy-writer",)),
    ("principals", ("legacy-writer", "legacy-writer")),
    ("old_writer_units_inert", 1),
    ("old_host_egress_denied", "yes"),
))
def test_invalid_old_host_receipt_fail_closed(field, value):
    provider, host = receipts()
    assert assess(provider, replace(host, **{field: value}))["state"] == "unknown"


@pytest.mark.parametrize("field,value,reason", (
    ("inventory_complete", False, "legacy_principal_inventory_incomplete"),
    ("provider_revoked_token_ids", (TOKENS[0],),
     "legacy_tokens_not_fully_revoked"),
    ("provider_revoked_token_ids", ("e" * 64,),
     "legacy_tokens_not_fully_revoked"),
    ("alternate_write_authority_absent", False,
     "alternate_legacy_write_authority_unknown"),
    ("old_write_denied_after_restart", False,
     "old_provider_write_not_denied"),
))
def test_missing_provider_denial_blocks(field, value, reason):
    provider, host = receipts()
    result = assess(replace(provider, **{field: value}), host)
    assert result == {
        "schema_version": "github-b3-retirement-receipt-crosscheck/v1",
        "state": "blocked",
        "reason": reason,
        "cutover_authorized": False,
    }


@pytest.mark.parametrize("field", ("old_writer_units_inert", "old_host_egress_denied"))
def test_missing_host_fencing_blocks(field):
    provider, host = receipts()
    assert assess(provider, replace(host, **{field: False}))["state"] == "blocked"


@pytest.mark.parametrize("argument,value", (
    ("binding_ref", "a" * 32),
    ("revision", "f" * 32),
    ("expected_generation", "e" * 32),
    ("now", -1),
    ("now", float("nan")),
    ("now", 10**1000),
    ("now", "now"),
))
def test_mismatched_requested_registration_or_now_refused(argument, value):
    provider, host = receipts()
    assert assess(provider, host, **{argument: value})["state"] == "unknown"


def test_two_forged_objects_still_cannot_mint_permission():
    provider, host = receipts()
    # Both are caller-generated Python dataclasses, not signed or issued by
    # a connected Nextcloud or trusted old-host transport.
    assert assess(replace(provider), replace(host))["cutover_authorized"] is False


def test_no_approved_cutover_objects_or_live_provider_calls():
    from obsidian_automation import github_b3_retirement_receipts as module
    assert not hasattr(module, "B3CutoverObservation")
    assert not hasattr(module, "verify_nextcloud_live")
    assert not hasattr(module, "revoke_credentials")
    assert not hasattr(module, "enable_writer")


def test_unexpected_types_fail_closed():
    provider, host = receipts()
    assert assess(None, host)["state"] == "unknown"
    assert assess(provider, None)["state"] == "unknown"
    assert assess({"provider": provider}, host)["state"] == "unknown"
