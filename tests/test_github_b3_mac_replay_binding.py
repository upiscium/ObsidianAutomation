"""Synthetic-only MAC-to-root-journal diagnostic handoff, never cutover.

All journal and key fixtures are disposable: no production /var/lib,
Nextcloud/old LXC access, secrets or service activation.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import hashlib
import hmac
import json
import os
import threading

import pytest

from obsidian_automation import github_b3_mac_replay_binding as bind
from obsidian_automation import github_b3_replay_journal as journal
from obsidian_automation.github_b3_authenticated_evidence import (
    B3DetachedIssuerKeys, B3DetachedSequenceFloor,
)
from obsidian_automation.github_b3_retirement_receipts import (
    B3NextcloudProviderReceipt, B3OldHostFenceReceipt,
)

BIND = "b" * 32
REV = "c" * 32
GEN = "d" * 32
FENCE = "e" * 32
EPOCH = "f" * 32
PKID = "1" * 32
HKID = "2" * 32
PKEY = b"P" * 32
HKEY = b"H" * 32
NOW = 1_800_000_000
TOKENS = ("a" * 64, "f" * 64)
PRINCIPALS = ("legacy-mirror", "legacy-writer")
PROVIDER_ISSUER = "nextcloud-provider-probe"
HOST_ISSUER = "legacy-host-probe"
SCHEMA = "github-b3-mac-envelope/v1"
CONTEXT = b"obsidian-b3-retirement-issuer-evidence/v1\x00"


def canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def fake_receipts():
    provider = B3NextcloudProviderReceipt(
        binding_ref=BIND, revision=REV, source_generation=GEN,
        fencing_generation=FENCE, tenant="nextcloud",
        observed_at=NOW - 1, post_restart_denial_at=NOW - 5,
        inventory_complete=True, principals=PRINCIPALS,
        legacy_token_ids=TOKENS, provider_revoked_token_ids=TOKENS,
        alternate_write_authority_absent=True,
        old_write_denied_after_restart=True, audit_cursor="3" * 64,
    )
    host = B3OldHostFenceReceipt(
        binding_ref=BIND, revision=REV, source_generation=GEN,
        fencing_generation=FENCE, host="obsidian-automation",
        fenced_at=NOW - 300, restarted_at=NOW - 20,
        observed_at=NOW - 10, boot_id="4" * 32,
        principals=PRINCIPALS,
        old_writer_units_inert=True, old_host_egress_denied=True,
    )
    return provider, host


def synth_sign(payload: object, *, issuer: str, key_id: str,
               key: bytes, sequence: int) -> bytes:
    unsigned = {
        "schema_version": SCHEMA, "issuer": issuer,
        "key_id": key_id, "sequence": sequence,
        "payload": asdict(payload),
    }
    mac = hmac.new(
        key,
        CONTEXT + issuer.encode("ascii") + b"\x00" + canonical(unsigned),
        hashlib.sha256,
    ).hexdigest()
    return canonical({**unsigned, "mac_sha256": mac})


def envelopes(*, p_sequence=11, h_sequence=13, provider=None, host=None):
    default_p, default_h = fake_receipts()
    return (
        synth_sign(
            default_p if provider is None else provider,
            issuer=PROVIDER_ISSUER, key_id=PKID,
            key=PKEY, sequence=p_sequence,
        ),
        synth_sign(
            default_h if host is None else host,
            issuer=HOST_ISSUER, key_id=HKID,
            key=HKEY, sequence=h_sequence,
        ),
    )


def keys():
    return B3DetachedIssuerKeys(
        provider_key_id=PKID, host_key_id=HKID,
        provider_key=PKEY, host_key=HKEY,
    )


def floor():
    return B3DetachedSequenceFloor(
        provider_sequence=10, host_sequence=12,
    )


def genesis():
    fields = {
        "schema_version": journal.VERSION, "kind": "genesis",
        "epoch": EPOCH, "binding_ref": BIND, "revision": REV,
        "source_generation": GEN, "provider_key_id": PKID,
        "host_key_id": HKID,
        "provider_sequence": 10, "host_sequence": 12,
        "bundle_sha256": journal._ZERO,
        "previous_sha256": journal._ZERO,
    }
    return journal._sealed_fields(fields)


@pytest.fixture
def root(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    root_dir = tmp_path / "replay-root"
    root_dir.mkdir()
    root_dir.chmod(0o700)
    journal_file = root_dir / journal.JOURNAL_NAME
    journal_file.write_bytes(genesis())
    journal_file.chmod(0o600)
    monkeypatch.setattr(journal, "TRUST_ANCHOR", tmp_path)
    monkeypatch.setattr(journal, "JOURNAL_ROOT", root_dir)
    monkeypatch.setattr(journal, "_owner_uid", lambda: os.geteuid())
    monkeypatch.setattr(journal, "_owner_gid", lambda: os.getegid())
    return root_dir, journal_file


def run(envelope_pair=None, *, keystore=None, checkpoint=None, **overrides):
    p, h = envelopes() if envelope_pair is None else envelope_pair
    options = {
        "trusted_issuer_keys": keys() if keystore is None else keystore,
        "trusted_sequence_floor": floor() if checkpoint is None else checkpoint,
        "epoch": EPOCH, "binding_ref": BIND, "revision": REV,
        "expected_generation": GEN, "now": NOW,
    }
    options.update(overrides)
    result = bind.assess_and_record_b3_mac_bound_replay(p, h, **options)
    assert type(result) is dict
    assert set(result) == {
        "schema_version", "state", "reason", "cutover_authorized",
    }
    assert result["schema_version"] == bind.VERSION
    assert result["cutover_authorized"] is False
    return result


def assert_genesis_unchanged(file):
    last, count, digests = journal._verify_journal(file.read_bytes())
    assert count == 1
    assert last["kind"] == "genesis"
    assert digests == frozenset()


def test_two_valid_synthetic_macs_bind_exact_bytes_to_one_local_record(root):
    root_dir, file = root
    pair = envelopes()
    result = run(pair)
    assert result == {
        "schema_version": bind.VERSION,
        "state": "candidate",
        "reason": "mac_bound_local_checkpoint_external_authority_unverified",
        "cutover_authorized": False,
    }
    last, count, digests = journal._verify_journal(file.read_bytes())
    assert count == 2
    assert (last["provider_sequence"], last["host_sequence"]) == (11, 13)
    assert last["bundle_sha256"] == bind._bundle_digest(*pair)
    assert digests == frozenset({bind._bundle_digest(*pair)})
    assert [x.name for x in root_dir.iterdir()] == [journal.JOURNAL_NAME]


def test_second_pair_appends_only_verified_new_sequences(root):
    _, file = root
    assert run()["state"] == "candidate"
    second = envelopes(p_sequence=12, h_sequence=14)
    assert run(second)["state"] == "candidate"
    last, count, seen = journal._verify_journal(file.read_bytes())
    assert count == 3
    assert last["bundle_sha256"] == bind._bundle_digest(*second)
    assert len(seen) == 2


def test_replay_same_valid_pair_is_blocked_without_additional_writes(root):
    _, file = root
    assert run()["state"] == "candidate"
    before = file.read_bytes()
    assert run()["reason"] == "local_replay_journal_refused"
    assert file.read_bytes() == before


@pytest.mark.parametrize("p_seq,h_seq", (
    (11, 14), (12, 13), (11, 13),
))
def test_one_counter_not_advanced_from_journal_blocks_under_lock(
    root, p_seq, h_seq,
):
    _, file = root
    assert run()["state"] == "candidate"
    before = file.read_bytes()
    result = run(envelopes(p_sequence=p_seq, h_sequence=h_seq))
    assert result["state"] == "blocked"
    assert file.read_bytes() == before


@pytest.mark.parametrize("which", ("provider", "host"))
def test_tampered_mac_rejected_before_journal_io(root, which, monkeypatch):
    _, file = root
    original = file.read_bytes()
    pair = list(envelopes())
    current = json.loads(pair[0 if which == "provider" else 1])
    current["mac_sha256"] = "0" * 64
    pair[0 if which == "provider" else 1] = canonical(current)
    def must_not_record(_):
        raise AssertionError("journal I/O must not happen on invalid MAC")
    monkeypatch.setattr(bind, "append_b3_replay_diagnostic", must_not_record)
    assert run(tuple(pair))["state"] == "unknown"
    assert file.read_bytes() == original


def test_valid_mac_incomplete_provider_claims_block_without_journal_io(root, monkeypatch):
    _, file = root
    p, h = fake_receipts()
    pair = envelopes(provider=replace(p, inventory_complete=False), host=h)
    original = file.read_bytes()
    def must_not_record(_):
        raise AssertionError("blocked receipt must not be journaled")
    monkeypatch.setattr(bind, "append_b3_replay_diagnostic", must_not_record)
    assert run(pair)["state"] == "blocked"
    assert file.read_bytes() == original


def test_valid_mac_mismatched_cross_receipt_binding_refuses_io(root, monkeypatch):
    _, file = root
    _, h = fake_receipts()
    pair = envelopes(host=replace(h, source_generation="a" * 32))
    original = file.read_bytes()
    def must_not_record(_):
        raise AssertionError("unmatched receipts must not be journaled")
    monkeypatch.setattr(bind, "append_b3_replay_diagnostic", must_not_record)
    assert run(pair)["state"] == "unknown"
    assert file.read_bytes() == original


@pytest.mark.parametrize("name,value", (
    ("epoch", "BAD"),
    ("epoch", "a" * 31),
    ("epoch", "A" * 32),
    ("epoch", 1),
    ("epoch", None),
    ("binding_ref", "a" * 32),
    ("revision", "a" * 32),
    ("expected_generation", "a" * 32),
    ("now", float("nan")),
    ("now", -1),
    ("now", 10**1000),
))
def test_bad_context_cannot_record(root, name, value):
    _, file = root
    original = file.read_bytes()
    result = run(**{name: value})
    assert result["cutover_authorized"] is False
    assert result["state"] != "candidate"
    assert file.read_bytes() == original


def test_mismatched_epoch_valid_macs_refused_by_journal_context(root):
    _, file = root
    before = file.read_bytes()
    assert run(epoch="a" * 32)["state"] == "blocked"
    assert file.read_bytes() == before


@pytest.mark.parametrize("mutation", (
    {"provider_key_id": "a" * 32},
    {"host_key_id": "a" * 32},
    {"provider_key": b"X" * 32},
    {"host_key": b"X" * 32},
    {"provider_key": b"Q" * 12},
))
def test_key_and_issuer_identity_mutations_fail_without_journal_write(
    root, mutation,
):
    _, file = root
    before = file.read_bytes()
    result = run(keystore=replace(keys(), **mutation))
    assert result["state"] == "unknown"
    assert file.read_bytes() == before


@pytest.mark.parametrize("which", (0, 1))
@pytest.mark.parametrize("raw", (
    b"", b"not json", b"{}", b'{"a":1,"a":1}\n',
    b"x" * (12 * 1024 + 1), b"[" * 1800, b"\xff",
))
def test_malformed_envelopes_never_update_journal(root, which, raw):
    _, file = root
    before = file.read_bytes()
    pair = list(envelopes())
    pair[which] = raw
    assert run(tuple(pair))["state"] == "unknown"
    assert file.read_bytes() == before


def test_swapped_issuer_messages_fail_without_append(root):
    _, file = root
    original = file.read_bytes()
    p, h = envelopes()
    assert run((h, p))["state"] == "unknown"
    assert file.read_bytes() == original


@pytest.mark.parametrize("checkpoint", (
    B3DetachedSequenceFloor(11, 12),
    B3DetachedSequenceFloor(10, 13),
    B3DetachedSequenceFloor(-1, 12),
    B3DetachedSequenceFloor(True, 12),
))
def test_invalid_caller_floor_refused_even_if_local_journal_is_empty(
    root, checkpoint,
):
    _, file = root
    old = file.read_bytes()
    assert run(checkpoint=checkpoint)["state"] == "unknown"
    assert file.read_bytes() == old


def test_direct_forged_diagnostic_not_accepted_as_external_proof(root):
    _, file = root
    # A malicious *privileged* owner holding injected symmetric keys can
    # forge a locally valid HMAC, demonstrating remaining external trust gap.
    assert run()["state"] == "candidate"
    assert file.read_bytes() != genesis()
    assert run()["cutover_authorized"] is False


def test_caller_cannot_select_or_override_opaque_digest(root, monkeypatch):
    _, file = root
    pair = envelopes()
    expected = bind._bundle_digest(*pair)
    recorded = []
    real = bind.append_b3_replay_diagnostic
    def observe(v):
        recorded.append(v)
        return real(v)
    monkeypatch.setattr(bind, "append_b3_replay_diagnostic", observe)
    assert run(pair)["state"] == "candidate"
    assert len(recorded) == 1
    assert recorded[0].bundle_sha256 == expected
    assert recorded[0].bundle_sha256 != hashlib.sha256(pair[0]).hexdigest()
    assert "password" not in str(recorded[0])
    assert "PPPP" not in str(recorded[0])
    assert journal._verify_journal(file.read_bytes())[1] == 2


def test_changed_valid_payload_same_sequences_cannot_bypass_journal(root):
    _, file = root
    p, h = fake_receipts()
    assert run(envelopes(provider=p, host=h))["state"] == "candidate"
    before = file.read_bytes()
    changed = replace(p, audit_cursor="5" * 64)
    alternative = envelopes(provider=changed, host=h)
    assert bind._bundle_digest(*alternative) != bind._bundle_digest(*envelopes())
    assert run(alternative)["state"] == "blocked"
    assert file.read_bytes() == before


def test_corrupt_existing_journal_blocks_even_if_both_macs_are_valid(root):
    _, file = root
    file.write_bytes(file.read_bytes() + b"not a completed line")
    before = file.read_bytes()
    assert run()["state"] == "unknown"
    assert file.read_bytes() == before


def test_missing_journal_or_root_does_not_get_created(root):
    _, file = root
    file.unlink()
    assert run()["state"] == "unknown"
    assert not file.exists()


def test_fsynchronized_append_with_unknown_ack_is_not_retried(root, monkeypatch):
    _, file = root
    real_fsync = journal.os.fsync
    def ambiguous(fd):
        real_fsync(fd)
        raise OSError("synthetic fsync response lost")
    with monkeypatch.context() as m:
        m.setattr(journal.os, "fsync", ambiguous)
        assert run()["state"] == "unknown"
    assert journal._verify_journal(file.read_bytes())[1] == 2
    assert run()["state"] == "blocked"


def test_competing_same_bundle_never_both_record(root):
    _, file = root
    gate = threading.Barrier(4)
    def parallel(_):
        gate.wait(timeout=5)
        return run()
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(parallel, range(4)))
    assert sum(result["state"] == "candidate" for result in outcomes) == 1
    assert all(result["cutover_authorized"] is False for result in outcomes)
    assert journal._verify_journal(file.read_bytes())[1] == 2


def test_recorded_without_journal_proof_is_rejected(root, monkeypatch):
    _, file = root
    before = file.read_bytes()
    def untrusted_response(_):
        return {"schema_version": journal.VERSION, "state": "recorded",
                "reason": "not-a-proof", "cutover_authorized": False}
    monkeypatch.setattr(bind, "append_b3_replay_diagnostic", untrusted_response)
    assert run()["state"] == "unknown"
    assert file.read_bytes() == before


def test_cannot_convert_untrusted_journal_cutover_true_to_authority(
    root, monkeypatch,
):
    _, file = root
    before = file.read_bytes()
    def forged_response(_):
        return {"schema_version": journal.VERSION, "state": "recorded",
                "reason": "local_sequence_checkpoint_only",
                "cutover_authorized": True}
    monkeypatch.setattr(bind, "append_b3_replay_diagnostic", forged_response)
    assert run()["state"] == "unknown"
    assert file.read_bytes() == before


def test_journal_io_error_fails_closed_without_leaking_details(
    root, monkeypatch,
):
    _, file = root
    before = file.read_bytes()
    def unavailable(_):
        raise OSError("synthetic-secret-path-must-not-escape")
    monkeypatch.setattr(bind, "append_b3_replay_diagnostic", unavailable)
    result = run()
    assert result["state"] == "unknown"
    assert "synthetic-secret" not in str(result)
    assert file.read_bytes() == before


def test_no_runtime_or_network_activators_exposed():
    for item in (
        "start_writer", "enable_cutover", "read_provider",
        "run_systemctl", "write_webdav", "revoke_token",
        "build_owner_service", "load_real_issuer_keys",
    ):
        assert not hasattr(bind, item)
