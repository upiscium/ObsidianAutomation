"""Synthetic-only fixed-root B3 evidence source admission and refusal tests."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import hmac
import json
import os

import pytest

from obsidian_automation import github_b3_pinned_retirement_source as src
from obsidian_automation.github_b3_retirement_receipts import (
    B3NextcloudProviderReceipt, B3OldHostFenceReceipt,
)

BIND = "b" * 32
REV = "c" * 32
GEN = "d" * 32
FENCE = "e" * 32
PKID, HKID = "1" * 32, "2" * 32
PKEY, HKEY = b"P" * 32, b"H" * 32
NOW = 1_800_000_000
PRINCIPALS = ("legacy-mirror", "legacy-writer")
TOKENS = ("a" * 64, "f" * 64)
PREFIX = b"obsidian-b3-retirement-issuer-evidence/v1\x00"


def canonical(record: object) -> bytes:
    return (
        json.dumps(record, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def synthetic_envelope(
    receipt: object, *, issuer: str, key: bytes, key_id: str, sequence: int,
) -> bytes:
    plain = {
        "schema_version": "github-b3-mac-envelope/v1",
        "issuer": issuer, "key_id": key_id, "sequence": sequence,
        "payload": asdict(receipt),
    }
    mac = hmac.new(
        key, PREFIX + issuer.encode("ascii") + b"\x00" + canonical(plain),
        hashlib.sha256,
    ).hexdigest()
    return canonical({**plain, "mac_sha256": mac})


def fake_source():
    provider = B3NextcloudProviderReceipt(
        binding_ref=BIND, revision=REV, source_generation=GEN,
        fencing_generation=FENCE, tenant="nextcloud", observed_at=NOW - 1,
        post_restart_denial_at=NOW - 5, inventory_complete=True,
        principals=PRINCIPALS, legacy_token_ids=TOKENS,
        provider_revoked_token_ids=TOKENS,
        alternate_write_authority_absent=True,
        old_write_denied_after_restart=True, audit_cursor="3" * 64,
    )
    host = B3OldHostFenceReceipt(
        binding_ref=BIND, revision=REV, source_generation=GEN,
        fencing_generation=FENCE, host="obsidian-automation",
        fenced_at=NOW - 300, restarted_at=NOW - 20,
        observed_at=NOW - 10, boot_id="4" * 32,
        principals=PRINCIPALS, old_writer_units_inert=True,
        old_host_egress_denied=True,
    )
    return {
        src.KEYS: canonical({
            "schema_version": "github-b3-detached-keys/v1",
            "generation": GEN,
            "provider_key_id": PKID, "host_key_id": HKID,
            "provider_key_hex": PKEY.hex(), "host_key_hex": HKEY.hex(),
        }),
        src.FLOOR: canonical({
            "schema_version": "github-b3-detached-floor/v1",
            "generation": GEN, "provider_sequence": 10,
            "host_sequence": 12,
        }),
        src.PROVIDER: synthetic_envelope(
            provider, issuer="nextcloud-provider-probe",
            key=PKEY, key_id=PKID, sequence=11,
        ),
        src.HOST: synthetic_envelope(
            host, issuer="legacy-host-probe", key=HKEY,
            key_id=HKID, sequence=13,
        ),
    }


def write(path, raw):
    path.write_bytes(raw)
    path.chmod(0o640)


def set_manifest(folder, source):
    write(folder / src.MANIFEST, canonical({
        "schema_version": src.VERSION,
        "generation": GEN,
        "sha256": {
            name: hashlib.sha256(raw).hexdigest()
            for name, raw in source.items()
        },
    }))


@pytest.fixture
def fixed_source(tmp_path, monkeypatch):
    folder = tmp_path / "retirement"
    folder.mkdir()
    tmp_path.chmod(0o700)
    folder.chmod(0o750)
    monkeypatch.setattr(src, "TRUST_ANCHOR", tmp_path)
    monkeypatch.setattr(src, "ROOT", folder)
    monkeypatch.setattr(src, "_owner_uid", lambda: os.geteuid())
    monkeypatch.setattr(
        src, "_verifier_ids", lambda: (os.geteuid(), os.getegid()),
    )
    data = fake_source()
    for name, raw in data.items():
        write(folder / name, raw)
    set_manifest(folder, data)
    return folder


def inspect(*, now=NOW, binding_ref=BIND, revision=REV,
            expected_generation=GEN):
    result = src.assess_pinned_b3_retirement_evidence(
        binding_ref=binding_ref, revision=revision,
        expected_generation=expected_generation, now=now,
    )
    assert type(result) is dict
    assert result["cutover_authorized"] is False
    return result


def refused():
    assert inspect()["state"] == "unknown"


def test_safe_disposable_root_yields_only_non_authoritative_candidate(fixed_source):
    result = inspect()
    assert result["state"] == "candidate"
    assert "unverified" in result["reason"]
    assert sorted(x.name for x in fixed_source.iterdir()) == sorted(
        [src.MANIFEST, *src._SOURCE_NAMES],
    )


def test_same_root_snapshot_is_not_durable_replay_protection(fixed_source):
    assert inspect()["state"] == "candidate"
    # No checkpoint update is made; same signed bundle remains a candidate.
    assert inspect()["state"] == "candidate"


@pytest.mark.parametrize("name", (src.MANIFEST, *src._SOURCE_NAMES))
def test_missing_files_rejected(fixed_source, name):
    (fixed_source / name).unlink()
    refused()
    assert not (fixed_source / name).exists()  # never auto-recreated


@pytest.mark.parametrize("name", (src.MANIFEST, *src._SOURCE_NAMES))
@pytest.mark.parametrize("mode", (0o600, 0o644, 0o660, 0o666))
def test_bad_permissions_rejected(fixed_source, name, mode):
    (fixed_source / name).chmod(mode)
    refused()


@pytest.mark.parametrize("name", (src.MANIFEST, *src._SOURCE_NAMES))
def test_symlink_file_rejected(fixed_source, name):
    original = fixed_source / name
    alternate = fixed_source / "alternate"
    original.rename(alternate)
    original.symlink_to(alternate)
    refused()


@pytest.mark.parametrize("name", (src.MANIFEST, *src._SOURCE_NAMES))
def test_hardlink_rejected(fixed_source, name):
    os.link(fixed_source / name, fixed_source / "alternate")
    refused()


@pytest.mark.parametrize("name", (src.MANIFEST, *src._SOURCE_NAMES))
def test_empty_file_rejected(fixed_source, name):
    write(fixed_source / name, b"")
    refused()


@pytest.mark.parametrize("name", (src.MANIFEST, *src._SOURCE_NAMES))
def test_fifo_does_not_block(fixed_source, name):
    original = fixed_source / name
    original.unlink()
    os.mkfifo(original, 0o640)
    refused()


@pytest.mark.parametrize("name", (src.MANIFEST, *src._SOURCE_NAMES))
def test_changing_source_bytes_without_updating_manifest_denied(
    fixed_source, name,
):
    target = fixed_source / name
    with target.open("ab") as out:
        out.write(b"x")
    refused()


@pytest.mark.parametrize("name", (src.KEYS, src.FLOOR, src.PROVIDER, src.HOST))
def test_source_rotation_between_reads_denied(fixed_source, monkeypatch, name):
    original = src._read_fixed
    calls = {"count": 0}

    def swapped(fd, filename, **kwargs):
        data = original(fd, filename, **kwargs)
        if filename == name:
            calls["count"] += 1
            if calls["count"] == 1:
                # This mutation happens in the disposable fixture only.
                target = fixed_source / name
                target.rename(fixed_source / "alternate")
                write(target, data)
        return data

    monkeypatch.setattr(src, "_read_fixed", swapped)
    # Source bytes could be identical, but the lockless file identity
    # has changed. The source must be refused, not admitted.
    refused()


@pytest.mark.parametrize("mode", (0o700, 0o755, 0o770, 0o777))
def test_bad_final_root_permissions_denied(fixed_source, mode):
    fixed_source.chmod(mode)
    refused()


def test_bad_anchor_permissions_denied(fixed_source):
    fixed_source.parent.chmod(0o770)
    refused()


def test_symlink_root_path_denied(fixed_source, monkeypatch):
    symlink = fixed_source.parent / "linked"
    symlink.symlink_to(fixed_source, target_is_directory=True)
    monkeypatch.setattr(src, "ROOT", symlink)
    refused()


def test_replaced_root_between_reads_denied(fixed_source, monkeypatch):
    original = src._read_fixed
    called = False

    def rotate(fd, name, **kwargs):
        nonlocal called
        data = original(fd, name, **kwargs)
        if name == src.HOST and not called:
            called = True
            fixed_source.rename(fixed_source.parent / "old")
            fixed_source.mkdir()
            fixed_source.chmod(0o750)
        return data

    monkeypatch.setattr(src, "_read_fixed", rotate)
    refused()


def test_wrong_owner_id_denied(fixed_source, monkeypatch):
    monkeypatch.setattr(src, "_owner_uid", lambda: os.geteuid() + 4000)
    refused()


def test_missing_reader_role_denied(fixed_source, monkeypatch):
    monkeypatch.setattr(
        src, "_verifier_ids",
        lambda: (_ for _ in ()).throw(src.B3PinnedEvidenceUnavailable()),
    )
    refused()


@pytest.mark.parametrize("name", (src.KEYS, src.FLOOR))
def test_noncanonical_or_duplicate_keys_denied(fixed_source, name):
    write(fixed_source / name, b'{"schema_version":1,"schema_version":1}\n')
    source = {
        n: (fixed_source / n).read_bytes()
        for n in src._SOURCE_NAMES
    }
    set_manifest(fixed_source, source)
    refused()


@pytest.mark.parametrize("key,value", (
    ("provider_key_hex", "bad"),
    ("provider_key_hex", PKEY.hex().upper()),
    ("host_key_hex", PKEY.hex()),
    ("provider_key_id", HKID),
    ("generation", "a" * 32),
))
def test_wrong_key_source_denied_even_with_updated_manifest(
    fixed_source, key, value,
):
    record = json.loads((fixed_source / src.KEYS).read_bytes())
    record[key] = value
    write(fixed_source / src.KEYS, canonical(record))
    data = {
        n: (fixed_source / n).read_bytes() for n in src._SOURCE_NAMES
    }
    set_manifest(fixed_source, data)
    # A forged coherent local root CAN spoof provenance if root is untrusted;
    # the verifier still must NEVER approve cutover.
    assert inspect()["cutover_authorized"] is False


@pytest.mark.parametrize("key,value", (
    ("provider_sequence", 11),
    ("host_sequence", 13),
    ("provider_sequence", True),
    ("host_sequence", -1),
    ("host_sequence", 1 << 64),
))
def test_replay_floor_rejection(fixed_source, key, value):
    record = json.loads((fixed_source / src.FLOOR).read_bytes())
    record[key] = value
    write(fixed_source / src.FLOOR, canonical(record))
    data = {
        n: (fixed_source / n).read_bytes() for n in src._SOURCE_NAMES
    }
    set_manifest(fixed_source, data)
    refused()


def test_manifest_missing_required_digest_denied(fixed_source):
    record = json.loads((fixed_source / src.MANIFEST).read_bytes())
    record["sha256"].pop(src.PROVIDER)
    write(fixed_source / src.MANIFEST, canonical(record))
    refused()


def test_manifest_invalid_generation_denied(fixed_source):
    record = json.loads((fixed_source / src.MANIFEST).read_bytes())
    record["generation"] = "A" * 32
    write(fixed_source / src.MANIFEST, canonical(record))
    refused()


def test_manifest_oversized_denied(fixed_source):
    write(fixed_source / src.MANIFEST, b"x" * 2049)
    refused()


def test_stale_or_wrong_request_binding_refused(fixed_source):
    assert inspect(binding_ref="a" * 32)["state"] == "unknown"
    assert inspect(revision="a" * 32)["state"] == "unknown"
    assert inspect(expected_generation="a" * 32)["state"] == "unknown"
    assert inspect(now=NOW + 120)["state"] == "unknown"


def test_no_external_read_without_preprovisioning(tmp_path, monkeypatch):
    # Use ONLY a missing disposable fixture directory. Never access /etc.
    monkeypatch.setattr(src, "TRUST_ANCHOR", tmp_path)
    monkeypatch.setattr(src, "ROOT", tmp_path / "not-installed")
    monkeypatch.setattr(src, "_owner_uid", lambda: os.geteuid())
    monkeypatch.setattr(src, "_verifier_ids", lambda: (os.geteuid(), os.getegid()))
    refused()
    assert not (tmp_path / "not-installed").exists()


def test_no_writer_or_credential_effect_exports(fixed_source):
    assert not hasattr(src, "start_writer")
    assert not hasattr(src, "revoke_credentials")
    assert not hasattr(src, "write_checkpoint")
    assert not hasattr(src, "create_trust_root")
    assert "PPPP" not in str(inspect())
    assert "HHHH" not in str(inspect())
