"""R2/A0 synthetic-only tests: no WebDAV, credentials or live effects."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import copy
import json
import uuid

import pytest

from obsidian_automation.vault_formatter import create_plan
from obsidian_automation.vault_format_writer import FormatWriteError
from obsidian_automation.vault_format_recurring_admission import (
    RecurringAdmission, RecurringPolicy, _policy_digest,
    prepare_recurring_admission,
)

RAW = b"# Test\nline 1\n  \nline 2\n"


def fixture(tmp_path, count=2):
    vault = tmp_path / "synthetic-vault"
    root = vault / "98-System"
    root.mkdir(parents=True)
    for i in range(count):
        (root / f"note-{i}.md").write_bytes(RAW)
    plan = create_plan(vault)
    assert plan["counts"].get("would_change", 0) == count
    policy = RecurringPolicy(
        source_generation="a" * 64, account_binding_sha256="b" * 64,
        provider_proof_sha256="c" * 64,
        max_targets=3, max_selected_bytes=64 * 1024,
    )
    return plan, policy, vault


def admit(plan, policy, *, run_id=None, current_generation=None,
          verifier=None):
    expected = _policy_digest(policy)
    def verify(policy_digest, generation, binding, proof):
        return (policy_digest == expected
                and generation == policy.source_generation
                and binding == policy.account_binding_sha256
                and proof == policy.provider_proof_sha256)
    return prepare_recurring_admission(
        plan, policy, run_id or "d" * 32,
        current_generation=current_generation or (lambda: policy.source_generation),
        verify_operator_policy=verify if verifier is None else verifier,
    )


def test_selected_plan_and_replay_key_are_stable_and_no_effect(tmp_path):
    plan, policy, vault = fixture(tmp_path)
    before = {p.name: p.read_bytes() for p in vault.rglob("*.md")}
    one = admit(plan, policy)
    assert isinstance(one, RecurringAdmission)
    assert one.selected_paths == ("98-System/note-0.md", "98-System/note-1.md")
    assert one.selected_bytes == 2 * len(RAW)
    assert one.plan_sha256 == plan["plan_sha256"]
    assert len(one.operation_id) == 32
    assert admit(plan, policy) == one
    assert admit(plan, policy, run_id="e" * 32).operation_id != one.operation_id
    assert {p.name: p.read_bytes() for p in vault.rglob("*.md")} == before
    assert not list(tmp_path.rglob("*.vault-formatter-receipt.json"))


@pytest.mark.parametrize("change,reason", [
    ("missing_verifier", "recurring_authority_missing"),
    ("false_verifier", "recurring_policy_unverified"),
    ("wrong_generation", "recurring_source_generation_changed"),
    ("missing_generation", "recurring_source_generation_changed"),
    ("verifier_throws", "recurring_policy_unverified"),
])
def test_untrusted_or_absent_authority_fails_closed(tmp_path, change, reason):
    plan, policy, _ = fixture(tmp_path)
    overrides = {
        "missing_verifier": {"verifier": None},
        "false_verifier": {"verifier": lambda *a: False},
        "wrong_generation": {"current_generation": lambda: "d" * 64},
        "missing_generation": {"current_generation": lambda: None},
        "verifier_throws": {"verifier": lambda *a: (_ for _ in ()).throw(ValueError("SECRET"))},
    }
    kwargs = overrides[change]
    with pytest.raises(FormatWriteError, match="^" + reason + "$") as exc:
        if change == "missing_verifier":
            prepare_recurring_admission(
                plan, policy, "d" * 32,
                current_generation=lambda: policy.source_generation,
                verify_operator_policy=None,
            )
        else:
            admit(plan, policy, **kwargs)
    assert "SECRET" not in str(exc.value)


@pytest.mark.parametrize("mutate", [
    lambda p: replace(p, max_targets=1),
    lambda p: replace(p, max_selected_bytes=1),
])
def test_excess_budget_never_silently_selects_subset(tmp_path, mutate):
    plan, policy, vault = fixture(tmp_path)
    constrained = mutate(policy)
    with pytest.raises(FormatWriteError, match="recurring_(target_budget|selected_byte_budget)"):
        admit(plan, constrained)
    assert len(list(vault.rglob("*.md"))) == 2


@pytest.mark.parametrize("mutation", [
    {"source_generation": "d" * 63},
    {"account_binding_sha256": "g" * 64},
    {"provider_proof_sha256": 42},
    {"max_targets": 33},
    {"max_targets": True},
    {"max_targets": 0},
    {"max_selected_bytes": 0},
    {"max_selected_bytes": 2**40},
    {"max_selected_bytes": True},
    {"scope": "**/*"},
    {"policy_version": "wrong"},
])
def test_bad_policy_fails_without_verifier_call(tmp_path, mutation):
    plan, policy, _ = fixture(tmp_path)
    calls = []
    invalid = replace(policy, **mutation)
    with pytest.raises(FormatWriteError, match="invalid_recurring_policy"):
        prepare_recurring_admission(
            plan, invalid, "d" * 32,
            current_generation=lambda: policy.source_generation,
            verify_operator_policy=lambda *a: calls.append(a) or True,
        )
    assert not calls


@pytest.mark.parametrize("id_", ["", "wrong", "0" * 31, "0" * 33, "g" * 32, None, 45, True])
def test_invalid_run_id_rejected(tmp_path, id_):
    plan, policy, _ = fixture(tmp_path)
    with pytest.raises(FormatWriteError, match="invalid_recurring_run_id"):
        prepare_recurring_admission(
            plan, policy, id_,
            current_generation=lambda: policy.source_generation,
            verify_operator_policy=lambda *a: True,
        )


def test_verifier_cannot_mutate_plan_snapshot(tmp_path):
    plan, policy, _ = fixture(tmp_path)
    original = copy.deepcopy(plan)
    def mutate_in_verifier(*_):
        plan["results"].clear()
        plan["counts"]["would_change"] = 0
        return True
    result = admit(plan, policy, verifier=mutate_in_verifier)
    assert result.plan_sha256 == original["plan_sha256"]
    assert len(result.selected_paths) == 2
    assert plan["results"] == []


def test_zero_change_has_no_admission_and_no_effect(tmp_path):
    plan, policy, vault = fixture(tmp_path, count=0)
    assert admit(plan, policy) is None
    assert not list(vault.rglob("*.md"))


def test_generation_callback_failure_is_redacted(tmp_path):
    plan, policy, _ = fixture(tmp_path)
    def failure():
        raise RuntimeError("PRIVATE_SECRET")
    with pytest.raises(FormatWriteError, match="^recurring_source_unavailable$") as e:
        admit(plan, policy, current_generation=failure)
    assert "PRIVATE" not in str(e.value)


def test_tampered_plan_digest_is_rejected(tmp_path):
    plan, policy, _ = fixture(tmp_path)
    plan["counts"]["would_change"] = 0
    with pytest.raises(FormatWriteError, match="invalid_plan"):
        admit(plan, policy)


def test_plan_never_selects_unchanged_or_skipped(tmp_path):
    plan, policy, vault = fixture(tmp_path)
    (vault / "98-System/unchanged.md").write_bytes(b"# Correct\n\ntext\n")
    (vault / "other").mkdir()
    (vault / "other/private.md").write_bytes(RAW)
    safe = create_plan(vault)
    proposal = admit(safe, policy)
    assert proposal.selected_paths == ("98-System/note-0.md", "98-System/note-1.md")
    assert all(path.startswith("98-System/") for path in proposal.selected_paths)


def test_abandoned_run_cannot_be_reissued_under_same_policy_after_plan_change(tmp_path):
    plan, policy, vault = fixture(tmp_path)
    first = admit(plan, policy, run_id="f" * 32)
    (vault / "98-System/note-0.md").write_bytes(b"# Human edit\n  \ncontent\n")
    fresh = create_plan(vault)
    second = admit(fresh, policy, run_id="f" * 32)
    assert first.operation_id == second.operation_id
    # Same run_id cannot mint another replay key even if Plan changed.
    assert first.plan_sha256 != second.plan_sha256
    rotated = replace(policy, source_generation="e" * 64)
    third = admit(fresh, rotated, run_id="f" * 32)
    assert third.operation_id == first.operation_id
    # The effect-time Writer must still fence replays with durable markers.


def test_generation_changed_by_verifier_is_rejected(tmp_path):
    plan, policy, _ = fixture(tmp_path)
    generation = [policy.source_generation]
    def verifier(*_):
        generation[0] = "f" * 64
        return True
    with pytest.raises(FormatWriteError, match="recurring_source_generation_changed"):
        admit(plan, policy, verifier=verifier,
              current_generation=lambda: generation[0])


def test_true_boolean_verifier_only_and_exact_policy_digest(tmp_path):
    plan, policy, _ = fixture(tmp_path)
    observed = []
    def verifier(*args):
        observed.append(args)
        return 1  # truthy integer is not explicit operator verification
    with pytest.raises(FormatWriteError, match="recurring_policy_unverified"):
        admit(plan, policy, verifier=verifier)
    assert observed == [(
        _policy_digest(policy), policy.source_generation,
        policy.account_binding_sha256, policy.provider_proof_sha256,
    )]


def test_writer_immutable_marker_blocks_replayed_run_prior_to_get_or_put(tmp_path):
    """Actual guarded CAS Writer, but only a synthetic in-memory WebDAV."""
    from obsidian_automation.core_promotion_transport import HTTPResponse
    from obsidian_automation.vault_format_writer import apply_plan

    plan, policy, vault = fixture(tmp_path, count=1)
    first = admit(plan, policy, run_id="f" * 32)
    assert isinstance(first, RecurringAdmission)
    target = first.selected_paths[0]
    remote = {"bytes": (vault / target).read_bytes(), "etag": '"v0"',
              "gets": 0, "puts": 0}
    receipts = tmp_path / "receipts"
    receipts.mkdir(mode=0o700)
    base = "https://synthetic.invalid/remote.php/dav/files/user/ObsidianVault"

    def dav(*, method, target_url, username, password, headers, body,
            timeout, response_limit):
        assert target_url.endswith("/" + target)
        assert username == "user" and password == "SYNTHETIC"
        if method == "GET":
            remote["gets"] += 1
            return HTTPResponse(200, remote["bytes"], remote["etag"])
        assert method == "PUT"
        assert headers.get("If-Match") == remote["etag"]
        remote["puts"] += 1
        remote["bytes"] = body
        remote["etag"] = '"v1"'
        return HTTPResponse(204, b"", remote["etag"])

    def effect(proposal):
        return apply_plan(
            plan, selected_paths=proposal.selected_paths,
            enabled=True, cas_server_verified=True,
            receipt_dir=receipts, base_url=base,
            username="user", password="SYNTHETIC",
            transport=dav, operation_id=proposal.operation_id,
        )

    written = effect(first)
    assert written["status"] == "complete"
    assert written["vault_modified"] is True
    assert remote["puts"] == 1
    assert remote["gets"] >= 2
    captured = dict(remote)
    same_run = admit(plan, policy, run_id="f" * 32)
    assert same_run.operation_id == first.operation_id
    with pytest.raises(FormatWriteError, match="operation_replay_blocked"):
        effect(same_run)
    assert remote == captured
    assert len(list(receipts.glob("operation-*.marker"))) == 1
    # The Plan preparation itself did not mutate the original source bytes.
    assert (vault / target).read_bytes() == RAW



def test_mutating_caller_owned_frozen_policy_cannot_expand_approved_budget(tmp_path):
    plan, base, _ = fixture(tmp_path, count=2)
    policy = replace(base, max_targets=1)
    original_digest = _policy_digest(policy)
    generation = policy.source_generation

    def verifier(received, *_):
        assert received == original_digest
        object.__setattr__(policy, "max_targets", 3)
        object.__setattr__(policy, "max_selected_bytes", 8 * 1024 * 1024)
        return True

    with pytest.raises(FormatWriteError, match="recurring_target_budget"):
        prepare_recurring_admission(
            plan, policy, "d" * 32,
            current_generation=lambda: generation,
            verify_operator_policy=verifier,
        )
    # The original caller policy was mutated, but the internal admission used
    # the private copy of the earlier verified count=1 budget.
    assert policy.max_targets == 3


def test_mutating_caller_policy_identity_cannot_alter_result_after_verification(tmp_path):
    plan, policy, _ = fixture(tmp_path, count=1)
    expected_generation = policy.source_generation
    expected_binding = policy.account_binding_sha256
    expected_digest = _policy_digest(policy)

    def verifier(received, *_):
        assert received == expected_digest
        object.__setattr__(policy, "source_generation", "f" * 64)
        object.__setattr__(policy, "account_binding_sha256", "e" * 64)
        return True

    admission = prepare_recurring_admission(
        plan, policy, "d" * 32,
        current_generation=lambda: expected_generation,
        verify_operator_policy=verifier,
    )
    assert admission is not None
    assert admission.source_generation == expected_generation
    assert admission.account_binding_sha256 == expected_binding
    assert admission.policy_sha256 == expected_digest


def test_large_metadata_is_rejected_before_json_decode(tmp_path, monkeypatch):
    from obsidian_automation import vault_format_recurring_admission as module
    plan, policy, _ = fixture(tmp_path)
    # This extra field must be treated as part of the bounded source metadata
    # before _validated_plan can even validate its own digest.
    plan["untrusted_filler"] = "X" * (module.MAX_PLAN_BYTES + 1)

    def must_not_decode(*_args, **_kwargs):
        pytest.fail("oversized Plan must be refused before JSON decoding")

    monkeypatch.setattr(module.json, "loads", must_not_decode)
    with pytest.raises(FormatWriteError, match="recurring_plan_too_large"):
        admit(plan, policy)


def test_large_diff_is_not_copied_into_admission(tmp_path):
    plan, policy, _ = fixture(tmp_path)
    # 'diffs' is non-authoritative display data, omitted from the Plan SHA in
    # the existing writer. No need to duplicate it in the trust-bound snapshot.
    plan["diffs"] = {"long-preview": "X" * (9 * 1024 * 1024)}
    proposal = admit(plan, policy)
    assert proposal is not None
    assert len(proposal.selected_paths) == 2


def test_budget_limits_generated_bytes_as_well_as_source_bytes(tmp_path):
    import hashlib
    from obsidian_automation import vault_format_recurring_admission as module
    plan, policy, _ = fixture(tmp_path, count=1)
    path = next(row for row in plan["results"] if row["status"] == "would_change")
    before = path["before_size"]
    path["after_size"] = before + 10
    fixed = {key: value for key, value in plan.items()
             if key not in ("plan_sha256", "diffs")}
    plan["plan_sha256"] = hashlib.sha256(json.dumps(
        fixed, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    bounded = replace(policy, max_selected_bytes=before + 1)
    with pytest.raises(FormatWriteError, match="recurring_selected_byte_budget"):
        admit(plan, bounded)



@pytest.mark.parametrize("changed", [
    ("scope", "**/*"),
    ("policy_version", "unsafe-future-version"),
])
def test_callback_cannot_spoof_verified_scope_or_policy_version(tmp_path, changed):
    plan, policy, _ = fixture(tmp_path, count=1)
    original_scope = policy.scope
    original_version = policy.policy_version
    original_sha = _policy_digest(policy)

    def verifier(received, *_):
        assert received == original_sha
        object.__setattr__(policy, changed[0], changed[1])
        return True

    admission = prepare_recurring_admission(
        plan, policy, "d" * 32,
        current_generation=lambda: "a" * 64,
        verify_operator_policy=verifier,
    )
    assert admission is not None
    assert admission.policy_sha256 == original_sha
    assert admission.scope == original_scope
    assert admission.policy_version == original_version


def test_metadata_string_size_rejected_before_json_encoder(tmp_path, monkeypatch):
    from obsidian_automation import vault_format_recurring_admission as module
    plan, policy, _ = fixture(tmp_path, count=1)
    plan["very_large"] = "X" * (module.MAX_PLAN_BYTES + 1)
    # Policy digest uses JSON too; pin that already checked small policy
    # before spying on the Plan-specific encoder.
    policy_digest = module._policy_digest(policy)
    monkeypatch.setattr(module, "_policy_digest", lambda _: policy_digest)
    original_encode = module.json.JSONEncoder.iterencode

    def dont_encode(*args, **kwargs):
        pytest.fail("oversized metadata must fail before starting JSON encoding")

    monkeypatch.setattr(module.json.JSONEncoder, "iterencode", dont_encode)
    with pytest.raises(FormatWriteError, match="recurring_plan_too_large"):
        # Avoid admit() here: its helper computes _policy_digest using json.dumps.
        module.prepare_recurring_admission(
            plan, policy, "d" * 32,
            current_generation=lambda: "a" * 64,
            verify_operator_policy=lambda *args: True,
        )
    monkeypatch.setattr(module.json.JSONEncoder, "iterencode", original_encode)


def test_oversized_metadata_collection_rejected_before_encoding(tmp_path):
    from obsidian_automation import vault_format_recurring_admission as module
    plan, policy, _ = fixture(tmp_path, count=1)
    plan["huge_collection"] = list(range(module.MAX_PLAN_BYTES // 8 + 1))
    with pytest.raises(FormatWriteError, match="recurring_plan_too_large"):
        admit(plan, policy)


def test_metadata_cycle_rejected_without_recurse_crash(tmp_path):
    plan, policy, _ = fixture(tmp_path, count=1)
    alias = []
    alias.append(alias)
    plan["cycle"] = alias
    with pytest.raises(FormatWriteError, match="recurring_plan_snapshot_invalid"):
        admit(plan, policy)



def test_caller_mutation_after_bounded_copy_cannot_change_selected_plan(
        tmp_path, monkeypatch):
    from obsidian_automation import vault_format_recurring_admission as module
    plan, policy, _ = fixture(tmp_path, count=2)
    expected_plan_sha = plan["plan_sha256"]
    original = module._bounded_metadata

    def copy_then_mutate(original_plan):
        copied = original(original_plan)
        original_plan["results"].clear()
        original_plan["counts"]["would_change"] = 0
        original_plan["plan_sha256"] = "0" * 64
        return copied

    monkeypatch.setattr(module, "_bounded_metadata", copy_then_mutate)
    proposal = admit(plan, policy)
    assert proposal is not None
    assert proposal.plan_sha256 == expected_plan_sha
    assert len(proposal.selected_paths) == 2
    assert not plan["results"]



@pytest.mark.parametrize("field", ["scope", "policy_version"])
def test_str_subclass_cannot_spoof_policy_version_or_scope(tmp_path, field):
    plan, policy, _ = fixture(tmp_path, count=1)

    class Spoof(str):
        def __ne__(self, other):
            return False

        def __eq__(self, other):
            return True

    spoofed = replace(policy, **{field: Spoof("**/*" if field == "scope" else "bad-policy")})
    calls = []
    with pytest.raises(FormatWriteError, match="invalid_recurring_policy"):
        prepare_recurring_admission(
            plan, spoofed, "d" * 32,
            current_generation=lambda: "a" * 64,
            verify_operator_policy=lambda *args: calls.append(args) or True,
        )
    assert not calls


def test_very_large_int_refused_before_json_encoder(tmp_path, monkeypatch):
    from obsidian_automation import vault_format_recurring_admission as module
    plan, policy, _ = fixture(tmp_path, count=1)
    plan["huge_integer"] = 1 << 1_000_000
    # Only spy on the Plan encoding; policy_digest first uses the JSON module.
    original_digest = module._policy_digest(policy)
    monkeypatch.setattr(module, "_policy_digest", lambda _: original_digest)

    def deny_encoding(*args, **kwargs):
        pytest.fail("huge int must be rejected before JSON decimal conversion")

    monkeypatch.setattr(module.json.JSONEncoder, "iterencode", deny_encoding)
    with pytest.raises(FormatWriteError, match="recurring_plan_too_large"):
        module.prepare_recurring_admission(
            plan, policy, "d" * 32,
            current_generation=lambda: "a" * 64,
            verify_operator_policy=lambda *args: True,
        )



@pytest.mark.parametrize("observed_at", [0, 1])
def test_source_generation_str_subclass_cannot_spoof_epoch(
        tmp_path, observed_at):
    plan, policy, _ = fixture(tmp_path, count=1)

    class Spoof(str):
        def __ne__(self, other):
            return False

        def __eq__(self, other):
            return True

    calls = []
    epochs = iter((
        (Spoof("f" * 64), "a" * 64)
        if observed_at == 0 else
        ("a" * 64, Spoof("f" * 64))
    ))
    with pytest.raises(FormatWriteError, match="recurring_source_generation_changed"):
        prepare_recurring_admission(
            plan, policy, "d" * 32,
            current_generation=lambda: next(epochs),
            verify_operator_policy=lambda *args: calls.append(args) or True,
        )
    assert len(calls) == observed_at



def test_control_character_json_escape_growth_is_preflight_bounded(
        tmp_path, monkeypatch):
    from obsidian_automation import vault_format_recurring_admission as module
    plan, policy, _ = fixture(tmp_path, count=1)
    plan["control_data"] = "\u0001" * (module.MAX_PLAN_BYTES // 3)
    expected_policy = module._policy_digest(policy)
    monkeypatch.setattr(module, "_policy_digest", lambda _: expected_policy)

    def forbidden_encoder(*_args, **_kwargs):
        pytest.fail("escaped string must be refused before JSON encoding")

    monkeypatch.setattr(module.json.JSONEncoder, "iterencode", forbidden_encoder)
    with pytest.raises(FormatWriteError, match="recurring_plan_too_large"):
        prepare_recurring_admission(
            plan, policy, "d" * 32,
            current_generation=lambda: "a" * 64,
            verify_operator_policy=lambda *args: True,
        )
