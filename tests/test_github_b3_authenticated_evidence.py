"""Offline dual-MAC issuer envelope tests: no real keys, provider or host I/O.

The synthetic test-side signer is intentionally NOT exported to production.
An in-process HMAC cannot prove a genuine Nextcloud/old-host observation.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import hmac
import json

import pytest

from obsidian_automation.github_b3_authenticated_evidence import (
    B3DetachedIssuerKeys, B3DetachedSequenceFloor,
    assess_b3_authenticated_evidence,
)
from obsidian_automation.github_b3_retirement_receipts import (
    B3NextcloudProviderReceipt, B3OldHostFenceReceipt,
)

BIND = "b" * 32
REV = "c" * 32
GEN = "d" * 32
FENCE = "e" * 32
PKID = "1" * 32
HKID = "2" * 32
PKEY = b"P" * 32  # unmistakably synthetic bytes; no real credential
HKEY = b"H" * 32
NOW = 1_800_000_000
TOKENS = ("a" * 64, "f" * 64)
PRINCIPALS = ("legacy-mirror", "legacy-writer")
SCHEMA = "github-b3-mac-envelope/v1"
CONTEXT = b"obsidian-b3-retirement-issuer-evidence/v1\x00"


def canonical(value) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ) + "\n").encode("utf-8")


def fake_sources():
    p = B3NextcloudProviderReceipt(
        binding_ref=BIND, revision=REV, source_generation=GEN,
        fencing_generation=FENCE, tenant="nextcloud",
        observed_at=NOW - 1, post_restart_denial_at=NOW - 5,
        inventory_complete=True, principals=PRINCIPALS,
        legacy_token_ids=TOKENS, provider_revoked_token_ids=TOKENS,
        alternate_write_authority_absent=True,
        old_write_denied_after_restart=True, audit_cursor="3" * 64,
    )
    h = B3OldHostFenceReceipt(
        binding_ref=BIND, revision=REV, source_generation=GEN,
        fencing_generation=FENCE, host="obsidian-automation",
        fenced_at=NOW - 300, restarted_at=NOW - 20,
        observed_at=NOW - 10, boot_id="4" * 32,
        principals=PRINCIPALS, old_writer_units_inert=True,
        old_host_egress_denied=True,
    )
    return p, h


def sign_fixture(payload, issuer, key_id, secret, *, sequence=11):
    unsigned = {
        "schema_version": SCHEMA, "issuer": issuer, "key_id": key_id,
        "sequence": sequence, "payload": payload,
    }
    mac = hmac.new(
        secret, CONTEXT + issuer.encode("ascii") + b"\x00"
        + canonical(unsigned), hashlib.sha256,
    ).hexdigest()
    return canonical({**unsigned, "mac_sha256": mac})


def inputs(p=None, h=None, *, provider_sequence=11, host_sequence=13):
    if p is None or h is None:
        pp, hh = fake_sources()
        p = pp if p is None else p
        h = hh if h is None else h
    return (
        sign_fixture(
            asdict(p), "nextcloud-provider-probe", PKID, PKEY,
            sequence=provider_sequence,
        ),
        sign_fixture(
            asdict(h), "legacy-host-probe", HKID, HKEY,
            sequence=host_sequence,
        ),
    )


def keyring():
    return B3DetachedIssuerKeys(
        provider_key_id=PKID, host_key_id=HKID,
        provider_key=PKEY, host_key=HKEY,
    )


def floor():
    return B3DetachedSequenceFloor(provider_sequence=10, host_sequence=12)


def check(p, h, *, keys=None, checkpoint=None, binding_ref=BIND,
          revision=REV, expected_generation=GEN, now=NOW):
    result = assess_b3_authenticated_evidence(
        p, h,
        trusted_issuer_keys=keyring() if keys is None else keys,
        trusted_sequence_floor=floor() if checkpoint is None else checkpoint,
        binding_ref=binding_ref, revision=revision,
        expected_generation=expected_generation, now=now,
    )
    assert result["schema_version"] == "github-b3-mac-evidence/v1"
    assert result["cutover_authorized"] is False
    assert set(result) == {
        "schema_version", "state", "reason", "cutover_authorized",
    }
    return result


def test_genuine_synthetic_mac_proof_is_still_not_cutover_authority():
    p, h = inputs()
    assert check(p, h) == {
        "schema_version": "github-b3-mac-evidence/v1",
        "state": "candidate",
        "reason": "detached_mac_valid_external_authority_unverified",
        "cutover_authorized": False,
    }


def test_pure_checker_never_reads_any_keys_or_starts_any_services():
    from obsidian_automation import github_b3_authenticated_evidence as m
    assert not hasattr(m, "sign")
    assert not hasattr(m, "load_credentials")
    assert not hasattr(m, "write_checkpoint")
    assert not hasattr(m, "register_b3_writer")
    assert b"hello" not in repr(keyring()).encode()
    assert "PPPP" not in repr(keyring())
    assert "HHHH" not in repr(keyring())


@pytest.mark.parametrize("which", ("provider", "host"))
def test_one_modified_mac_cannot_pass(which):
    p, h = inputs()
    if which == "provider":
        doc = json.loads(p)
        doc["mac_sha256"] = "0" * 64
        p = canonical(doc)
    else:
        doc = json.loads(h)
        doc["mac_sha256"] = "0" * 64
        h = canonical(doc)
    assert check(p, h)["state"] == "unknown"


@pytest.mark.parametrize("which", ("provider", "host"))
def test_unsigned_payload_mutations_denied(which):
    p, h = inputs()
    if which == "provider":
        doc = json.loads(p)
        doc["payload"]["source_generation"] = "1" * 32
        p = canonical(doc)
    else:
        doc = json.loads(h)
        doc["payload"]["fencing_generation"] = "2" * 32
        h = canonical(doc)
    assert check(p, h)["reason"] == "evidence_authentication_unavailable"


def test_provider_and_old_host_are_domain_separated():
    p, h = inputs()
    assert check(h, p)["state"] == "unknown"


@pytest.mark.parametrize("which", ("provider", "host"))
def test_wrong_trusted_issuer_key_denied(which):
    p, h = inputs()
    keys = keyring()
    keys = replace(keys, **{
        ("provider_key" if which == "provider" else "host_key"): b"X" * 32,
    })
    assert check(p, h, keys=keys)["state"] == "unknown"


@pytest.mark.parametrize("which", ("provider", "host"))
def test_wrong_trusted_key_id_denied(which):
    p, h = inputs()
    keys = keyring()
    keys = replace(keys, **{
        ("provider_key_id" if which == "provider" else "host_key_id"): "9" * 32,
    })
    assert check(p, h, keys=keys)["state"] == "unknown"


@pytest.mark.parametrize("change", (
    {"provider_key_id": HKID},
    {"host_key_id": PKID},
    {"provider_key": HKEY},
    {"host_key": PKEY},
    {"provider_key": b"x"},
    {"host_key": b"x" * 31},
    {"provider_key": "notbytes"},
    {"provider_key_id": "Q" * 32},
))
def test_invalid_trusted_key_separation_denied(change):
    p, h = inputs()
    assert check(p, h, keys=replace(keyring(), **change))["state"] == "unknown"


@pytest.mark.parametrize("checkpoint", (
    B3DetachedSequenceFloor(11, 12),
    B3DetachedSequenceFloor(10, 13),
    B3DetachedSequenceFloor(20, 12),
    B3DetachedSequenceFloor(-1, 12),
    B3DetachedSequenceFloor(True, 12),
    B3DetachedSequenceFloor(0, 10**1000),
    {"provider_sequence": 10, "host_sequence": 12},
))
def test_stale_or_invalid_sequence_floor_denied(checkpoint):
    p, h = inputs()
    assert check(p, h, checkpoint=checkpoint)["state"] == "unknown"


@pytest.mark.parametrize("which", ("provider", "host"))
@pytest.mark.parametrize("sequence", (0, True, -1, 2**63, 10**1000))
def test_sequence_type_or_range_refused(which, sequence):
    p, h = inputs()
    doc = json.loads(p if which == "provider" else h)
    doc["sequence"] = sequence
    if which == "provider":
        p = canonical(doc)
    else:
        h = canonical(doc)
    assert check(p, h)["state"] == "unknown"


@pytest.mark.parametrize("raw", (
    b"",
    b"\xef\xbb\xbf{}",
    b"not json",
    b'{"foo":"bar"}',
    b'{"a":1,"a":1}\n',
    b"{}\n\n",
    b"{}",
    b"{ }\n",
    b"[]" + b"\n",
    b"\x00" + b"{}\n",
    b'{"mac_sha256":NaN}\n',
    b"a" * (12 * 1024 + 1),
    b"[" * 2000,
))
def test_bad_canonical_envelope_refused(raw):
    p, h = inputs()
    assert check(raw, h)["state"] == "unknown"
    assert check(p, raw)["state"] == "unknown"


@pytest.mark.parametrize("field,value", (
    ("schema_version", "other-version"),
    ("issuer", "legacy-host-probe"),
    ("issuer", "provider-forged"),
    ("key_id", HKID),
    ("key_id", 1),
    ("sequence", 9),
    ("mac_sha256", "A" * 64),
    ("mac_sha256", "0" * 63),
))
def test_metadata_substitution_refused(field, value):
    p, h = inputs()
    doc = json.loads(p)
    doc[field] = value
    assert check(canonical(doc), h)["state"] == "unknown"


@pytest.mark.parametrize("k,v", (
    ("revision", "1" * 32),
    ("source_generation", "2" * 32),
    ("fencing_generation", "3" * 32),
    ("provider_revoked_token_ids", [TOKENS[0]]),
    ("inventory_complete", False),
    ("old_write_denied_after_restart", False),
))
def test_mac_valid_but_incomplete_provider_claims_are_not_approved(k, v):
    p, h = fake_sources()
    p = replace(p, **{k:v})
    pdoc, hdoc = inputs(p, h)
    result = check(pdoc, hdoc)
    assert result["state"] in ("unknown", "blocked")


@pytest.mark.parametrize("k,v", (
    ("revision", "1" * 32),
    ("source_generation", "2" * 32),
    ("fencing_generation", "3" * 32),
    ("restarted_at", NOW - 301),
    ("old_writer_units_inert", False),
    ("old_host_egress_denied", False),
))
def test_mac_valid_but_incomplete_old_host_claims_not_approved(k, v):
    p, h = fake_sources()
    h = replace(h, **{k:v})
    pdoc, hdoc = inputs(p, h)
    result = check(pdoc, hdoc)
    assert result["state"] in ("unknown", "blocked")


@pytest.mark.parametrize("bad", (
    "b" * 31, "z" * 32, None, True, 1,
))
def test_bad_requested_binding_ref_refused(bad):
    p, h = inputs()
    assert check(p, h, binding_ref=bad)["state"] == "unknown"


@pytest.mark.parametrize("now", (
    -1, True, "now", None, float("nan"), float("inf"),
    10**1000,
))
def test_invalid_clock_denied(now):
    p, h = inputs()
    assert check(p, h, now=now)["state"] == "unknown"


def test_external_provider_and_host_keys_are_just_injected_untrusted_values():
    # No key provenance, source-root installation, provider probe, privileged
    # atomic ledger or issuer enrollment exists in this slice.
    p, h = inputs()
    assert check(p, h)["reason"].endswith("_unverified")
