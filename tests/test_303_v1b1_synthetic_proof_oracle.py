"""#303 V1/B1 R1: offline-only finite fixture comparison and coherence non-proof."""
from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
import json

import pytest

from obsidian_automation.vault_root_proof_oracle import (
    MAX_FIXTURE_MEMBERS,
    SyntheticAccountView,
    SyntheticMember,
    SyntheticTraceFrame,
    assess_synthetic_coherence,
    compare_synthetic_account_view,
)


def members():
    return (
        SyntheticMember("Vault", "id-vault"),
        SyntheticMember("Archive", "id-archive"),
        SyntheticMember("Notes", "id-notes"),
    )


def view(**overrides):
    values = dict(
        namespace="account-fixture",
        source_revision="rev-one",
        parent_resource_id="id-root",
        entries=members(),
        hidden_children_uncertain=False,
        complete_claim=True,
    )
    values.update(overrides)
    return SyntheticAccountView(**values)


def trace(*frames):
    return tuple(
        SyntheticTraceFrame(tick=t, source_revision=r,
                            view_fingerprint=f, mutation_event=event)
        for t, r, f, event in frames
    )


def blocked(result, reason):
    assert result.status == "blocked"
    assert result.reason == reason
    assert result.provider_verified is False
    assert result.writes_vault is False
    assert result.activation_allowed is False
    assert result.requires_explicit_activation is True


def test_exact_synthetic_visible_inventory_is_finite_match_not_provider_proof():
    result = compare_synthetic_account_view(
        view(), view(entries=tuple(reversed(members())))
    )
    assert result.status == "finite_fixture_match"
    assert result.reason == "provider_unverified"
    assert result.provider_verified is False
    assert result.activation_allowed is False
    assert result.writes_vault is False
    assert result.requires_explicit_activation is True


@pytest.mark.parametrize("changed", [
    (SyntheticMember("Vault", "id-vault"), SyntheticMember("Archive", "id-archive")),
    members() + (SyntheticMember("Extra", "id-extra"),),
    (SyntheticMember("Vault", "id-different"), *members()[1:]),
])
def test_omitted_extra_or_rebound_member_fails_closed(changed):
    blocked(compare_synthetic_account_view(view(), view(entries=changed)),
            "fixture_inventory_mismatch")


@pytest.mark.parametrize("field", [
    "namespace", "source_revision", "parent_resource_id",
])
def test_cross_scope_or_stale_source_is_not_accepted(field):
    blocked(compare_synthetic_account_view(view(), view(**{field: "other"})),
            "scope_or_revision_changed")


@pytest.mark.parametrize("field", ["hidden_children_uncertain", "complete_claim"])
def test_unobservable_or_incomplete_claim_never_becomes_completeness_proof(field):
    if field == "hidden_children_uncertain":
        result = compare_synthetic_account_view(view(**{field: True}), view())
        blocked(result, "hidden_scope_unproved")
    else:
        result = compare_synthetic_account_view(view(), view(**{field: False}))
        blocked(result, "incomplete_claim")


def test_unreported_acl_hidden_child_cannot_be_proven_absent_by_matching_rows():
    # A lying synthetic fixture can set hidden_children_uncertain=False on
    # both sides. Even then, only finite_fixture_match, NEVER provider proof.
    result = compare_synthetic_account_view(view(), view())
    assert result.status == "finite_fixture_match"
    assert result.provider_verified is False


@pytest.mark.parametrize("rows,why", [
    ((SyntheticMember("Same", "id-one"), SyntheticMember("Same", "id-two")),
     "duplicate_fixture_identity"),
    ((SyntheticMember("Same", "id-one"), SyntheticMember("Other", "id-one")),
     "duplicate_fixture_identity"),
    ((SyntheticMember(".", "id-one"),), "invalid_fixture"),
    ((SyntheticMember("..", "id-one"),), "invalid_fixture"),
    ((SyntheticMember("Nested/Child", "id-one"),), "invalid_fixture"),
    ((SyntheticMember("bad\x00name", "id-one"),), "invalid_fixture"),
    ((SyntheticMember("too-large" * 100, "id-one"),), "invalid_fixture"),
    ((SyntheticMember("ParentAlias", "id-root"),), "duplicate_fixture_identity"),
    ((SyntheticMember("É", "id-one"), SyntheticMember("e\u0301", "id-two")),
     "duplicate_fixture_identity"),
])
def test_malformed_or_aliased_synthetic_fixture_denied(rows, why):
    blocked(compare_synthetic_account_view(view(entries=rows), view()), why)


def test_row_cap_is_enforced_before_expensive_comparisons():
    rows = tuple(SyntheticMember(f"Child-{i}", f"id-{i}")
                 for i in range(MAX_FIXTURE_MEMBERS + 1))
    blocked(compare_synthetic_account_view(view(entries=rows), view()),
            "invalid_fixture")


class MaliciousString(str):
    def __eq__(self, _other):
        raise AssertionError("untrusted equality invoked")

    def __ne__(self, _other):
        raise AssertionError("untrusted inequality invoked")

    __hash__ = str.__hash__


@pytest.mark.parametrize("field,bad", [
    ("namespace", MaliciousString("other")),
    ("source_revision", MaliciousString("other")),
    ("parent_resource_id", MaliciousString("other")),
    ("hidden_children_uncertain", 1),
    ("complete_claim", 1),
    ("entries", [SyntheticMember("Vault", "id-vault")]),
])
def test_invalid_or_comparison_spoofed_contract_values_denied(field, bad):
    blocked(compare_synthetic_account_view(view(**{field: bad}), view()),
            "invalid_fixture")


def test_class_subclass_and_malformed_members_are_not_trusted():
    class SubView(SyntheticAccountView):
        pass

    blocked(compare_synthetic_account_view(SubView(**view().__dict__), view()),
            "invalid_fixture")
    blocked(compare_synthetic_account_view(
        view(entries=(SyntheticMember(MaliciousString("Vault"), "id-vault"),)),
        view(),
    ), "invalid_fixture")


def test_unchanged_synthetic_samples_never_prove_atomic_tree_snapshot():
    result = assess_synthetic_coherence(trace(
        (1, "rev-one", "tree-a", False),
        (2, "rev-one", "tree-a", False),
    ))
    assert result.status == "unknown"
    assert result.reason == "provider_atomicity_unproved"
    assert result.provider_verified is False
    assert result.activation_allowed is False


def test_aba_mutation_between_identical_observed_endpoints_is_blocked_if_known():
    blocked(assess_synthetic_coherence(trace(
        (1, "rev-one", "tree-a", False),
        (2, "rev-one", "tree-b", True),
        (3, "rev-one", "tree-a", True),
    )), "known_tree_mutation")


def test_unobserved_aba_race_must_still_remain_unknown_not_verified():
    # No trace event means no evidence that nothing happened between polls.
    result = assess_synthetic_coherence(trace(
        (1, "rev-one", "tree-a", False),
        (3, "rev-one", "tree-a", False),
    ))
    assert result.status == "unknown"
    assert result.provider_verified is False


def test_mutation_after_final_recheck_is_denied_when_independently_known():
    blocked(assess_synthetic_coherence(trace(
        (1, "rev-one", "tree-a", False),
        (2, "rev-one", "tree-a", False),
        (3, "rev-one", "tree-a", True),
    )), "known_tree_mutation")


def test_uncertainty_of_post_final_recheck_mutation_never_enables_activation():
    result = assess_synthetic_coherence(trace(
        (1, "rev-one", "tree-a", False),
        (2, "rev-one", "tree-a", False),
    ))
    assert result.status == "unknown"
    assert result.activation_allowed is False


@pytest.mark.parametrize("frames,why", [
    (trace((1, "rev-a", "tree-a", False),
           (2, "rev-b", "tree-a", False)), "stale_source"),
    (trace((1, "rev-a", "tree-a", False),
           (2, "rev-a", "tree-b", False)), "known_tree_mutation"),
    (trace((2, "rev-a", "tree-a", False),
           (1, "rev-a", "tree-a", False)), "invalid_trace"),
    (trace((1, "rev-a", "tree-a", False),), "invalid_trace"),
    ((SyntheticTraceFrame(True, "rev-a", "tree-a"), *trace(
        (2, "rev-a", "tree-a", False))), "invalid_trace"),
    ((SyntheticTraceFrame(1, MaliciousString("rev-a"), "tree-a"), *trace(
        (2, "rev-a", "tree-a", False))), "invalid_trace"),
])
def test_malformed_stale_or_changed_trace_is_blocked(frames, why):
    blocked(assess_synthetic_coherence(frames), why)


def test_result_contains_no_resource_names_paths_or_secret_values():
    result = compare_synthetic_account_view(
        view(entries=(SyntheticMember("synthetic-secret", "id-secret"),)),
        view(entries=(SyntheticMember("Other", "id-other"),)),
    )
    wire = json.dumps(asdict(result), sort_keys=True)
    for secret in ("synthetic-secret", "id-secret", "account-fixture", "rev-one"):
        assert secret not in wire
    assert set(asdict(result)) == {
        "status", "reason", "provider_verified", "writes_vault",
        "activation_allowed", "requires_explicit_activation",
    }


def test_evaluators_avoid_selected_file_network_and_process_io(monkeypatch):
    # These are SELECTED runtime tripwires during evaluation only; the module
    # was already imported. This test alone cannot prove import-time purity
    # or intercept every possible I/O entry point. Check the import closure
    # and source call graph separately before claiming a broader property.
    import builtins
    import socket
    import subprocess
    import urllib.request

    def denied(*_args, **_kwargs):
        raise AssertionError("pure proof fixture attempted provider/FS/process I/O")

    monkeypatch.setattr(Path, "open", denied)
    monkeypatch.setattr(builtins, "open", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(urllib.request, "urlopen", denied)
    assert compare_synthetic_account_view(view(), view()).provider_verified is False
    assert assess_synthetic_coherence(trace(
        (1, "rev-a", "tree-a", False), (2, "rev-a", "tree-a", False),
    )).status == "unknown"


def test_exact_maximum_sibling_limit_is_a_finite_match_not_provider_proof():
    capped = tuple(SyntheticMember(f"Child-{i}", f"id-{i}")
                   for i in range(MAX_FIXTURE_MEMBERS))
    result = compare_synthetic_account_view(view(entries=capped),
                                            view(entries=tuple(reversed(capped))))
    assert result.status == "finite_fixture_match"
    assert result.provider_verified is False
    assert result.activation_allowed is False


def test_historical_unicode_compatibility_collision_fails_closed():
    # A pair previously blocked by V1/A's legacy NFKC-casefold rule.
    entries = (
        SyntheticMember("\u03b1\u0345\uff9e", "id-canonical"),
        SyntheticMember("\u03b1\u0345\u3099", "id-legacy"),
    )
    blocked(compare_synthetic_account_view(view(entries=entries), view()),
            "duplicate_fixture_identity")


def test_exact_timeline_cap_remains_unknown_and_one_extra_is_rejected():
    from obsidian_automation.vault_root_proof_oracle import MAX_TIMELINE_FRAMES

    within = tuple(
        SyntheticTraceFrame(i, "rev-one", "tree-a")
        for i in range(MAX_TIMELINE_FRAMES)
    )
    result = assess_synthetic_coherence(within)
    assert result.status == "unknown"
    assert result.provider_verified is False
    blocked(assess_synthetic_coherence(
        within + (SyntheticTraceFrame(MAX_TIMELINE_FRAMES, "rev-one", "tree-a"),)
    ), "invalid_trace")


@pytest.mark.parametrize("field,value", [
    ("view_fingerprint", MaliciousString("tree-a")),
    ("mutation_event", 1),
    ("tick", False),
    ("tick", -1),
    ("source_revision", ""),
])
def test_timeline_rejects_malformed_field_types_before_comparison(field, value):
    a = SyntheticTraceFrame(0, "rev-one", "tree-a")
    b = replace(a, tick=1)
    b = replace(b, **{field: value})
    blocked(assess_synthetic_coherence((a, b)), "invalid_trace")


def test_trace_objects_must_be_exact_dataclass_type():
    class UnsafeFrame(SyntheticTraceFrame):
        pass

    frames = (
        SyntheticTraceFrame(0, "rev-one", "tree-a"),
        UnsafeFrame(1, "rev-one", "tree-a"),
    )
    blocked(assess_synthetic_coherence(frames), "invalid_trace")


def test_untrusted_direct_result_construction_is_not_an_attestation():
    from obsidian_automation.vault_root_proof_oracle import SyntheticProofResult

    # These are merely Python objects; never accept a caller-created instance
    # as an authenticated receipt or use it to authorize Vault operations.
    forged = SyntheticProofResult("arbitrary", "fixture-secret",
                                  provider_verified=True,
                                  activation_allowed=True)
    assert forged.provider_verified is True
    valid = compare_synthetic_account_view(view(), view())
    assert valid.provider_verified is False
    assert valid.activation_allowed is False
