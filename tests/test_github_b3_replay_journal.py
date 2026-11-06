"""Disposable Unix filesystem tests; no installed ledger, credentials or Writer."""
from __future__ import annotations

from dataclasses import replace
import fcntl
import hashlib
import json
import os

import pytest

from obsidian_automation import github_b3_replay_journal as j

EPOCH = "e" * 32
BIND = "b" * 32
REV = "c" * 32
GEN = "d" * 32
PKID = "1" * 32
HKID = "2" * 32
BUNDLE_A = hashlib.sha256(b"synthetic-bundle-a").hexdigest()
BUNDLE_B = hashlib.sha256(b"synthetic-bundle-b").hexdigest()


def diagnostic(*, provider=11, host=13, bundle=BUNDLE_A):
    return j.B3ReplayDiagnostic(
        epoch=EPOCH, binding_ref=BIND, revision=REV,
        source_generation=GEN, provider_key_id=PKID,
        host_key_id=HKID, provider_sequence=provider,
        host_sequence=host, bundle_sha256=bundle,
    )


def genesis(*, provider=10, host=12, ctx=None):
    ctx = diagnostic() if ctx is None else ctx
    values = {
        "schema_version": j.VERSION, "kind": "genesis",
        **{key: getattr(ctx, key) for key in j._CONTEXT},
        "provider_sequence": provider, "host_sequence": host,
        "bundle_sha256": j._ZERO, "previous_sha256": j._ZERO,
    }
    return j._sealed_fields(values)


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "root-owned"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    tmp_path.chmod(0o700)
    filename = root / j.JOURNAL_NAME
    filename.write_bytes(genesis())
    filename.chmod(0o600)
    monkeypatch.setattr(j, "TRUST_ANCHOR", tmp_path)
    monkeypatch.setattr(j, "JOURNAL_ROOT", root)
    monkeypatch.setattr(j, "_owner_uid", lambda: os.geteuid())
    monkeypatch.setattr(j, "_owner_gid", lambda: os.getegid())
    return root, filename


def check(observation):
    result = j.append_b3_replay_diagnostic(observation)
    assert type(result) is dict
    assert set(result) == {
        "schema_version", "state", "reason", "cutover_authorized",
    }
    assert result["schema_version"] == j.VERSION
    assert result["cutover_authorized"] is False
    return result


def inspect(path):
    return j._verify_journal(path.read_bytes())


def test_first_append_creates_exactly_one_valid_chain_record(source):
    root, filename = source
    assert check(diagnostic()) == {
        "schema_version": j.VERSION, "state": "recorded",
        "reason": "local_sequence_checkpoint_only",
        "cutover_authorized": False,
    }
    last, count, seen = inspect(filename)
    assert count == 2
    assert last["provider_sequence"] == 11
    assert last["host_sequence"] == 13
    assert seen == frozenset({BUNDLE_A})
    assert sorted(x.name for x in root.iterdir()) == [j.JOURNAL_NAME]


def test_second_append_chains_and_persists_to_fresh_open(source):
    _, file = source
    assert check(diagnostic())["state"] == "recorded"
    assert check(diagnostic(provider=12, host=14, bundle=BUNDLE_B))[
        "state"] == "recorded"
    records = file.read_bytes().splitlines(keepends=True)
    assert len(records) == 3
    assert json.loads(records[2])["previous_sha256"] == json.loads(records[1])["sha256"]
    assert inspect(file)[1] == 3


def test_repeat_same_bundle_is_blocked_without_any_new_write(source):
    _, file = source
    assert check(diagnostic())["state"] == "recorded"
    before = file.read_bytes()
    result = check(diagnostic())
    assert result["state"] == "blocked"
    assert file.read_bytes() == before


@pytest.mark.parametrize("provider,host", (
    (10, 14), (12, 12), (0, 20), (11, 12), (10, 13),
))
def test_one_issuer_not_advanced_is_blocked(source, provider, host):
    _, file = source
    before = file.read_bytes()
    assert check(diagnostic(provider=provider, host=host))[
        "reason"] == "stale_or_reused_replay_bundle"
    assert file.read_bytes() == before


@pytest.mark.parametrize("key", j._CONTEXT)
def test_different_epoch_or_binding_is_blocked(source, key):
    _, file = source
    before = file.read_bytes()
    fake = replace(diagnostic(), **{key: "a" * 32})
    assert check(fake)["state"] == "blocked"
    assert file.read_bytes() == before


@pytest.mark.parametrize("bad", (
    {"provider_key_id": HKID},
    {"host_key_id": PKID},
    {"bundle_sha256": j._ZERO},
    {"bundle_sha256": "A" * 64},
    {"bundle_sha256": "no-hash"},
    {"epoch": "not-an-epoch"},
    {"provider_sequence": -1},
    {"host_sequence": True},
    {"provider_sequence": 2**63},
    {"host_sequence": "13"},
))
def test_invalid_observation_never_opens_or_mutates(source, bad):
    _, file = source
    original = file.read_bytes()
    assert check(replace(diagnostic(), **bad))["state"] == "unknown"
    assert file.read_bytes() == original


def test_untyped_caller_refused(source):
    _, file = source
    before = file.read_bytes()
    assert check({"epoch": EPOCH})["state"] == "unknown"
    assert file.read_bytes() == before


@pytest.mark.parametrize("mode", (0o640, 0o644, 0o660, 0o400, 0o666))
def test_bad_file_modes_refused(source, mode):
    _, file = source
    file.chmod(mode)
    assert check(diagnostic())["state"] == "unknown"


@pytest.mark.parametrize("mode", (0o750, 0o755, 0o770, 0o777, 0o500))
def test_bad_root_modes_refused(source, mode):
    root, _ = source
    root.chmod(mode)
    assert check(diagnostic())["state"] == "unknown"


def test_writable_ancestor_refused(source):
    root, _ = source
    root.parent.chmod(0o770)
    assert check(diagnostic())["state"] == "unknown"


def test_missing_file_refused_without_recreating(source):
    _, file = source
    file.unlink()
    assert check(diagnostic())["state"] == "unknown"
    assert not file.exists()


def test_missing_fixed_directory_refused(source, monkeypatch):
    root, _ = source
    missing = root.parent / "not-provisioned"
    monkeypatch.setattr(j, "JOURNAL_ROOT", missing)
    assert check(diagnostic())["state"] == "unknown"
    assert not missing.exists()


def test_root_symlink_refused(source, monkeypatch):
    root, _ = source
    link = root.parent / "alias"
    link.symlink_to(root, target_is_directory=True)
    monkeypatch.setattr(j, "JOURNAL_ROOT", link)
    assert check(diagnostic())["state"] == "unknown"


def test_file_symlink_refused(source):
    root, file = source
    original = root / "regular"
    file.rename(original)
    file.symlink_to(original)
    assert check(diagnostic())["state"] == "unknown"


def test_hardlink_refused(source):
    root, file = source
    os.link(file, root / "extra")
    assert check(diagnostic())["state"] == "unknown"


def test_fifo_does_not_block(source):
    _, file = source
    file.unlink()
    os.mkfifo(file, 0o600)
    assert check(diagnostic())["state"] == "unknown"


def test_file_directory_refused(source):
    _, file = source
    file.unlink()
    file.mkdir()
    assert check(diagnostic())["state"] == "unknown"


def test_wrong_owner_refused(source, monkeypatch):
    _, file = source
    monkeypatch.setattr(j, "_owner_uid", lambda: os.geteuid() + 3000)
    assert check(diagnostic())["state"] == "unknown"


def test_wrong_group_refused(source, monkeypatch):
    _, file = source
    monkeypatch.setattr(j, "_owner_gid", lambda: os.getegid() + 3000)
    assert check(diagnostic())["state"] == "unknown"


def test_invalid_trust_anchor_refused(source, monkeypatch):
    monkeypatch.setattr(j, "JOURNAL_ROOT", j.TRUST_ANCHOR)
    assert check(diagnostic())["state"] == "unknown"


@pytest.mark.parametrize("bad", (
    b"", b"[]\n", b"{}\n", b"not json\n",
    b'{"kind":"genesis","kind":"advance"}\n',
    b"\xff", b"[" * 2000, b"x" * (1 << 20) + b"y",
))
def test_corrupted_genesis_refused(source, bad):
    _, file = source
    file.write_bytes(bad)
    assert check(diagnostic())["state"] == "unknown"


def test_incomplete_newline_at_end_is_unrecoverable_without_owner(source):
    _, file = source
    file.write_bytes(file.read_bytes() + b'{"kind":"adv')
    assert check(diagnostic())["state"] == "unknown"


def test_tampered_preceding_digest_is_refused(source):
    _, file = source
    assert check(diagnostic())["state"] == "recorded"
    lines = file.read_bytes().splitlines(keepends=True)
    second = json.loads(lines[1])
    second["previous_sha256"] = "a" * 64
    second.pop("sha256")
    lines[1] = j._sealed_fields(second)
    file.write_bytes(b"".join(lines))
    assert check(diagnostic(provider=12, host=14, bundle=BUNDLE_B))[
        "state"] == "unknown"


def test_genesis_cannot_be_replayed_as_subsequent_advance(source):
    _, file = source
    file.write_bytes(file.read_bytes() + genesis())
    assert check(diagnostic())["state"] == "unknown"


def test_sha_reuse_with_higher_seq_is_refused(source):
    _, file = source
    assert check(diagnostic())["state"] == "recorded"
    before = file.read_bytes()
    r = check(diagnostic(provider=12, host=14, bundle=BUNDLE_A))
    assert r["state"] == "blocked"
    assert file.read_bytes() == before


def test_old_checkpoint_rollback_by_privileged_actor_is_not_detectable(source):
    _, file = source
    original = file.read_bytes()
    assert check(diagnostic())["state"] == "recorded"
    file.write_bytes(original)
    # This cannot be claimed as tamper-resistant anti-rollback, because
    # root can replace old bytes without an external sealed anchor.
    assert check(diagnostic())["state"] == "recorded"


def test_same_process_second_open_exclusive_lock_refused(source):
    _, file = source
    fd = os.open(file, os.O_RDONLY | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = file.read_bytes()
        assert check(diagnostic())["state"] == "unknown"
        assert file.read_bytes() == before
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert check(diagnostic())["state"] == "recorded"


def test_short_append_leaves_corrupt_tail_and_fails_closed(source, monkeypatch):
    _, file = source
    real_write = j.os.write
    def short_write(fd, data):
        return real_write(fd, data[:-1])
    monkeypatch.setattr(j.os, "write", short_write)
    assert check(diagnostic())["state"] == "unknown"
    assert check(diagnostic(provider=12, host=14, bundle=BUNDLE_B))[
        "state"] == "unknown"
    assert not file.read_bytes().endswith(b"\n")


def test_fsync_error_is_fail_closed_and_retry_does_not_duplicate(source, monkeypatch):
    _, file = source
    real_fsync = j.os.fsync
    def error_after_fsync(fd):
        real_fsync(fd)
        raise OSError("synthetic fsync uncertainty")
    with monkeypatch.context() as m:
        m.setattr(j.os, "fsync", error_after_fsync)
        assert check(diagnostic())["state"] == "unknown"
    assert inspect(file)[1] == 2
    assert check(diagnostic())["state"] == "blocked"


def test_fixed_file_inode_replacement_detected(source, monkeypatch):
    root, file = source
    original = j._check_file
    calls = {"n": 0}
    def swapped(fd, dir_fd, uid, gid):
        result = original(fd, dir_fd, uid, gid)
        calls["n"] += 1
        if calls["n"] == 2:
            old = root / "stale"
            file.rename(old)
            file.write_bytes(old.read_bytes())
            file.chmod(0o600)
        return result
    monkeypatch.setattr(j, "_check_file", swapped)
    assert check(diagnostic())["state"] == "unknown"


def test_fixed_root_inode_replacement_detected(source, monkeypatch):
    root, _ = source
    original = j._check_root
    calls = {"n": 0}
    def swapped(fd, uid, gid):
        calls["n"] += 1
        if calls["n"] == 1:
            original_data = (root / j.JOURNAL_NAME).read_bytes()
            root.rename(root.parent / "old-root")
            root.mkdir(mode=0o700)
            new_file = root / j.JOURNAL_NAME
            new_file.write_bytes(original_data)
            new_file.chmod(0o600)
        return original(fd, uid, gid)
    monkeypatch.setattr(j, "_check_root", swapped)
    assert check(diagnostic())["state"] == "unknown"


def test_capacity_limit_refuses_without_appending(source, monkeypatch):
    _, file = source
    monkeypatch.setattr(j, "_MAX_RECORDS", 1)
    before = file.read_bytes()
    assert check(diagnostic())["state"] == "unknown"
    assert file.read_bytes() == before


def test_no_live_host_or_writer_exports_and_keyless_journal(source):
    from obsidian_automation import github_b3_replay_journal as m
    assert not hasattr(m, "deploy")
    assert not hasattr(m, "revoke_credentials")
    assert not hasattr(m, "register_b3_writer")
    assert not hasattr(m, "load_nextcloud_credential")
    assert not hasattr(m, "init_root")
    assert check(diagnostic())["cutover_authorized"] is False
    _, file = source
    assert b"password" not in file.read_bytes()
    assert b"secret" not in file.read_bytes()
