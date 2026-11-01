"""Source-only, synthetic acceptance for a single-file #471 Writer binding.

No actual Nextcloud, host credentials, canonical Vault writes, service activation
or production mode changes. The synthetic remote transport lives in memory.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from obsidian_automation.core_promotion_transport import HTTPResponse
from obsidian_automation.vault_format_canary_binding import (
    BoundedCanaryWriter, CanaryGrant, _generation,
)
from obsidian_automation.vault_format_job import FormatterJob, FormatterInvalid
from obsidian_automation.vault_format_writer import FormatWriteError, _binding
from obsidian_automation.vault_formatter import create_plan, POLICY_VERSION

RAW = b"# Synthetic\nbefore\n  \nafter\n"
DESIRED = b"# Synthetic\nbefore\n\nafter\n"
TARGET = "98-System/Disposable-One.md"
OTHER = "98-System/Other.md"
ACCOUNT_ROOT = "https://dav.invalid/remote.php/dav/files/user/"
VAULT_ROOT = ACCOUNT_ROOT + "ObsidianVault"


class FakeShared:
    def __init__(self, details):
        self.details = dict(details)
        self.loads = 0
        self._lock = threading.RLock()

    def load_nextcloud(self):
        self.loads += 1
        return dict(self.details)


class FakeDAV:
    def __init__(self, body=RAW):
        self.body = body
        self.etag = '"v1"'
        self.calls = []
        self.concurrent = False
        self.weak = False

    def __call__(
        self, *, method, target_url, username, password, headers,
        body, timeout, response_limit,
    ):
        assert target_url.startswith(VAULT_ROOT + "/98-System/")
        assert urlsplit(target_url).scheme == "https"
        assert username == "user" and password == "SYNTHETIC-ONLY"
        assert 0 < timeout <= 60
        path = unquote(urlsplit(target_url).path.split("ObsidianVault/", 1)[-1])
        self.calls.append((method, path, dict(headers)))
        if method == "GET":
            if path == TARGET:
                return HTTPResponse(
                    status=200, body=self.body,
                    etag='W/"weak"' if self.weak else self.etag)
            return HTTPResponse(status=404, body=b"", etag=None)
        assert method == "PUT"
        if path != TARGET:
            raise AssertionError("Writer attempted a non-approved remote target")
        assert headers.get("Content-Type") == "text/markdown; charset=utf-8"
        assert headers.get("If-Match")
        if self.concurrent:
            self.body = b"concurrent edit\n"
            self.etag = '"concurrent"'
            self.concurrent = False
        if self.etag != headers.get("If-Match"):
            return HTTPResponse(status=412, body=b"", etag=self.etag)
        self.body = body
        self.etag = '"v2"'
        return HTTPResponse(status=204, body=b"", etag=self.etag)

    @property
    def puts(self):
        return [x for x in self.calls if x[0] == "PUT"]


@pytest.fixture
def setup(tmp_path):
    vault = tmp_path / "synthetic-vault"
    target = vault / TARGET
    target.parent.mkdir(parents=True)
    target.write_bytes(RAW)
    other = vault / OTHER
    other.write_bytes(b"outside\n  \nscope\n")
    plan = create_plan(vault)
    assert POLICY_VERSION == "system-blank-lines-v3"
    assert plan["counts"]["would_change"] == 2
    source = FakeShared({
        "webdav_url": ACCOUNT_ROOT,
        "vault_folder": "ObsidianVault",
        "nextcloud_user": "user",
        "nextcloud_password": "SYNTHETIC-ONLY",
    })
    generation = _generation(source.details)
    receipt = tmp_path / "private-receipts"
    receipt.mkdir(mode=0o700)
    selected = next(x for x in plan["results"] if x["path"] == TARGET)
    grant = CanaryGrant(
        target_path=TARGET,
        plan_sha256=plan["plan_sha256"],
        before_sha256=selected["before_sha256"],
        after_sha256=selected["after_sha256"],
        source_generation=generation,
        account_binding_sha256=_binding(VAULT_ROOT, "user"),
        provider_proof_sha256="a" * 64,  # synthetic metadata only
        operation_id="b" * 32,
        receipt_root=receipt,
    )
    return vault, plan, source, generation, grant


def make(setup, *, dav=None, generation=None, grant=None, proof_verifier=None):
    _, _, source, configured_generation, configured_grant = setup
    return BoundedCanaryWriter(
        source,
        source_generation=lambda: configured_generation if generation is None else generation,
        grant=grant or configured_grant,
        proof_verifier=(
            proof_verifier if proof_verifier is not None
            else lambda proof, gen, binding: (
                proof == configured_grant.provider_proof_sha256
                and gen == configured_grant.source_generation
                and binding == configured_grant.account_binding_sha256
            )
        ),
        transport=dav,
    )


def test_unverified_provider_proof_cannot_authorize_remote_write(setup):
    _, plan, shared, generation, grant = setup
    dav = FakeDAV()
    unverified = BoundedCanaryWriter(
        shared, lambda: generation, grant, transport=dav,
    )
    with pytest.raises(FormatWriteError, match="canary_provider_proof_unverified"):
        unverified(plan)
    assert not dav.calls


def test_rejected_or_crashing_proof_verifier_never_connects(setup):
    _, plan, _, _, _ = setup
    for verifier in (lambda *args: False,
                     lambda *args: "true",
                     lambda *args: (_ for _ in ()).throw(RuntimeError("dummy"))):
        dav = FakeDAV()
        writer = make(setup, dav=dav, proof_verifier=verifier)
        with pytest.raises(FormatWriteError, match="canary_provider_proof_unverified"):
            writer(plan)
        assert not dav.calls


def test_exact_single_file_applies_and_never_touches_other_candidate(setup):
    vault, plan, shared, generation, grant = setup
    dav = FakeDAV()
    writer = make(setup, dav=dav)
    result = writer(plan)
    assert result["status"] == "complete"
    assert result["vault_modified"] is True
    assert result["outcomes"][0]["path"] == TARGET
    assert result["outcomes"][0]["result"] == "applied"
    assert result["plan_sha256"] == plan["plan_sha256"]
    assert dav.body == DESIRED
    assert [item[1] for item in dav.puts] == [TARGET]
    assert len([call for call in dav.calls if call[0] == "GET"]) == 2
    assert (vault / TARGET).read_bytes() == RAW
    assert (vault / OTHER).read_bytes() == b"outside\n  \nscope\n"
    artifacts = list(grant.receipt_root.glob("*.vault-formatter-receipt.json"))
    assert len(artifacts) == 3
    combined = b"".join(p.read_bytes() for p in artifacts)
    assert b"SYNTHETIC-ONLY" not in combined
    assert ACCOUNT_ROOT.encode() not in combined
    assert RAW not in combined and DESIRED not in combined
    assert shared.loads == 5  # initial and every GET/PUT generation fence


def test_same_operation_cannot_replay_a_put(setup):
    _, plan, _, _, _ = setup
    dav = FakeDAV()
    writer = make(setup, dav=dav)
    assert writer(plan)["status"] == "complete"
    with pytest.raises(FormatWriteError, match="operation_replay_blocked"):
        writer(plan)
    assert len(dav.puts) == 1


def test_weak_and_stale_validator_fail_closed(setup):
    _, plan, _, _, _ = setup
    for modifier, expected in (
        ("weak", "weak_or_missing_etag"),
        ("concurrent", "cas_conflict"),
    ):
        dav = FakeDAV()
        setattr(dav, modifier, True)
        grant = copy.deepcopy(setup[-1])
        grant = CanaryGrant(**{**grant.__dict__,
                              "operation_id": ("c" if modifier == "weak" else "d") * 32})
        writer = make(setup, dav=dav, grant=grant)
        res = writer(plan)
        assert res["status"] == "blocked"
        assert res["outcomes"][0]["result"] == expected
        if modifier == "weak":
            assert not dav.puts
        else:
            assert len(dav.puts) == 1 and dav.body == b"concurrent edit\n"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("target_path", OTHER),
        ("target_path", "../secret.md"),
        ("target_path", "98-System/file.bin"),
        ("target_path", "98-System/.private.md"),
        ("target_path", "98-System/../outside.md"),
        ("plan_sha256", "0" * 64),
        ("before_sha256", "0" * 64),
        ("after_sha256", "0" * 64),
        ("account_binding_sha256", "0" * 64),
        ("source_generation", "0" * 64),
        ("operation_id", "z" * 32),
        ("provider_proof_sha256", "bad"),
        ("receipt_root", Path("relative")),
    ],
)
def test_invalid_grants_never_reach_provider(setup, field, replacement):
    _, plan, shared, _, grant = setup
    options = {**grant.__dict__, field: replacement}
    fake_grant = CanaryGrant(**options)
    fake = FakeDAV()
    try:
        writer = make(setup, dav=fake, grant=fake_grant)
    except FormatWriteError:
        pass
    else:
        with pytest.raises(FormatWriteError):
            writer(plan)
    assert not fake.calls


def test_source_rotation_and_account_path_mismatch_block_pre_network(setup):
    _, plan, shared, _, grant = setup
    for mutation in (
        {"nextcloud_user": "another"},
        {"webdav_url": "https://dav.invalid/remote.php/dav/files/other/"},
        {"vault_folder": "OtherVault"},
        {"nextcloud_password": "ROTATED"},
    ):
        fake = FakeDAV()
        shared.details.update(mutation)
        with pytest.raises(FormatWriteError):
            make(setup, dav=fake)(plan)
        assert not fake.calls
        shared.details.clear()
        shared.details.update({
            "webdav_url": ACCOUNT_ROOT,
            "vault_folder": "ObsidianVault",
            "nextcloud_user": "user",
            "nextcloud_password": "SYNTHETIC-ONLY",
        })


def test_generation_callback_drift_blocks_before_loading_credentials(setup):
    _, plan, shared, _, _ = setup
    fake = FakeDAV()
    with pytest.raises(FormatWriteError, match="canary_source_generation_changed"):
        make(setup, dav=fake, generation="0" * 64)(plan)
    assert shared.loads == 0 and not fake.calls


def test_plan_digest_and_policy_drift_block_before_connection_read(setup):
    _, plan, shared, _, _ = setup
    invalid = copy.deepcopy(plan)
    invalid["plan_sha256"] = "0" * 64
    fake = FakeDAV()
    with pytest.raises(FormatWriteError):
        make(setup, dav=fake)(invalid)
    assert shared.loads == 0 and not fake.calls


def test_normal_formatter_job_keeps_writer_off_with_adapter_source_present(tmp_path):
    from obsidian_automation.vault_format_job import FormatterJob
    state = tmp_path / "unwired-job"
    with FormatterJob(state, lambda: {}, configured=lambda: True,
                      source_generation=lambda: "a" * 64) as job:
        assert job.settings()["write_capable"] is False
        with pytest.raises(FormatterInvalid, match="formatter_writer_not_authorized"):
            job.update({"mode":"auto-apply","interval_seconds":86400})


def test_missing_shared_configuration_mutex_fails_closed(setup):
    _, plan, shared, _, _ = setup
    fake = FakeDAV()
    del shared._lock
    writer = make(setup, dav=fake)
    with pytest.raises(FormatWriteError, match="canary_configuration_lock_missing"):
        writer(plan)
    assert not fake.calls


@pytest.mark.parametrize(
    ("name", "url"),
    [
        ("..", "https://dav.invalid/remote.php/dav/files/../"),
        (".", "https://dav.invalid/remote.php/dav/files/./"),
        ("u/s", "https://dav.invalid/remote.php/dav/files/u%2Fs/"),
        ("u%s", "https://dav.invalid/remote.php/dav/files/u%25s/"),
        ("u" + chr(92) + "s", "https://dav.invalid/remote.php/dav/files/u%5Cs/"),
        ("user", "https://dav.invalid/remote.php/dav/files//user/"),
        ("user", "https://dav.invalid/remote.php/dav/files/us%65r/"),
        ("user", ACCOUNT_ROOT + "?"),
        ("user", ACCOUNT_ROOT + "#"),
    ],
)
def test_ambiguous_account_root_spelling_is_rejected_before_network(setup, name, url):
    _, plan, shared, _, _ = setup
    shared.details["nextcloud_user"] = name
    shared.details["webdav_url"] = url
    dav = FakeDAV()
    with pytest.raises(FormatWriteError, match="canary_scope_mismatch"):
        make(setup, dav=dav)(plan)
    assert not dav.calls


def test_mutating_caller_plan_during_proof_cannot_replace_approved_bytes(setup):
    vault, plan, _, _, grant = setup
    other_before = b"# Synthetic\nUNAPPROVED ALTERED\n  \nafter\n"
    (vault / TARGET).write_bytes(other_before)
    replacement = create_plan(vault)
    assert replacement["plan_sha256"] != grant.plan_sha256
    dav = FakeDAV(body=other_before)

    def swap_plan_after_admission(*_):
        # Simulates a racing caller retaining its mutable Plan reference.
        plan.clear()
        plan.update(replacement)
        return True

    result = make(setup, dav=dav, proof_verifier=swap_plan_after_admission)(plan)
    assert plan["plan_sha256"] == replacement["plan_sha256"]
    assert result["status"] == "blocked"
    assert result["outcomes"][0]["result"] == "source_changed"
    assert result["outcomes"][0]["after_sha256"] == grant.after_sha256
    assert not dav.puts
    assert dav.body == other_before


def test_nested_mutation_of_callers_plan_cannot_affect_frozen_write(setup):
    _, plan, _, _, grant = setup
    dav = FakeDAV()

    def mutate_nested_entry(*_):
        item = next(item for item in plan["results"] if item["path"] == TARGET)
        item["after_sha256"] = "0" * 64
        item["changed_lines"] = [999]
        return True

    result = make(setup, dav=dav, proof_verifier=mutate_nested_entry)(plan)
    assert result["status"] == "complete"
    assert result["outcomes"][0]["result"] == "applied"
    assert result["outcomes"][0]["after_sha256"] == grant.after_sha256
    assert len(dav.puts) == 1
    assert dav.body == DESIRED


def test_non_json_plan_snapshot_fails_before_credentials_or_network(setup):
    _, plan, shared, _, _ = setup
    plan["non_json_value"] = object()
    dav = FakeDAV()
    with pytest.raises(FormatWriteError, match="canary_plan_snapshot_invalid"):
        make(setup, dav=dav)(plan)
    assert shared.loads == 0 and not dav.calls


def test_proof_verifier_cannot_rotate_connection_before_first_get(setup):
    _, plan, shared, _, _ = setup
    dav = FakeDAV()

    def rotated_after_approval(*_):
        shared.details["nextcloud_password"] = "ROTATED"
        return True

    writer = make(setup, dav=dav, proof_verifier=rotated_after_approval)
    with pytest.raises(FormatWriteError, match="canary_source_generation_changed"):
        writer(plan)
    assert not dav.calls


def test_other_process_epoch_change_during_get_preflight_blocks_put(setup):
    _, plan, shared, _, _ = setup
    dav = FakeDAV()

    def change_after_first_get(**kwargs):
        response = dav(**kwargs)
        if kwargs.get("method") == "GET":
            # Simulate a separate credential-rotation process which does not
            # observe the in-process mutex. The next guarded HTTP request
            # must detect the changed epoch before sending any PUT.
            shared.details["nextcloud_password"] = "ROTATED"
        return response

    result = make(setup, dav=change_after_first_get)(plan)
    assert result["status"] in {"blocked", "indeterminate"}
    assert not dav.puts
    assert dav.body == RAW


def test_shared_configuration_save_waits_for_writer_lock(setup):
    _, plan, shared, _, _ = setup
    entered = threading.Event()
    release = threading.Event()
    write_complete = threading.Event()
    mutation_done = threading.Event()
    dav = FakeDAV()

    def blocked_put(**kwargs):
        if kwargs["method"] == "PUT":
            entered.set()
            assert release.wait(3), "test must release the PUT barrier"
        return dav(**kwargs)

    writer = make(setup, dav=blocked_put)

    def write():
        try:
            assert writer(plan)["status"] == "complete"
        finally:
            write_complete.set()

    def rotate_using_normal_shared_lock():
        with shared._lock:
            shared.details["nextcloud_password"] = "ROTATED"
        mutation_done.set()

    owner = threading.Thread(target=write, daemon=True)
    owner.start()
    assert entered.wait(3)
    rotating = threading.Thread(target=rotate_using_normal_shared_lock, daemon=True)
    rotating.start()
    try:
        assert not mutation_done.wait(0.08)
    finally:
        release.set()
    owner.join(timeout=3)
    rotating.join(timeout=3)
    assert write_complete.is_set() and mutation_done.is_set()
    assert len(dav.puts) == 1 and dav.body == DESIRED
