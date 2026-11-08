"""#303 V1: purely synthetic Vault-root candidate preview, no provider I/O."""
from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from obsidian_automation.snapshot_setup import validate_nextcloud
from obsidian_automation.vault_root_preview import (
    DirectoryEvidence, Resource, RootSelection, preview_root,
)


def choice(**overrides):
    values = dict(
        requested="Archive/Vault",
        active="Vault",
        active_resource_id="obj-old",
        expected_revision="rev-a",
        current_revision="rev-a",
        namespace="account-a",
        container_resource_id="obj-account-root",
        jobs_in_flight=False,
    )
    values.update(overrides)
    return RootSelection(**values)


def evidence(*, root=None, nested=None):
    return (
        DirectoryEvidence(
            parent="",
            parent_resource_id="obj-account-root",
            namespace="account-a",
            revision="rev-a",
            complete=True,
            entries=tuple(root) if root is not None else (
                Resource("Vault", "obj-old", "directory"),
                Resource("Archive", "obj-archive", "directory"),
            ),
        ),
        DirectoryEvidence(
            parent="Archive",
            parent_resource_id="obj-archive",
            namespace="account-a",
            revision="rev-a",
            complete=True,
            entries=tuple(nested) if nested is not None else (
                Resource("Vault", "obj-new", "directory"),
                Resource("Other", "obj-other", "directory"),
            ),
        ),
    )


def assert_blocked(result, reason):
    assert result.status == "blocked"
    assert result.reason == reason
    assert result.version == 1
    assert result.writes_vault is False
    assert result.activation_allowed is False
    assert result.requires_explicit_activation is True


def test_complete_synthetic_inventory_is_preview_only():
    result = preview_root(choice(), evidence())
    assert result.status == "candidate_valid"
    assert result.reason == "requires_revalidation_and_activation"
    assert result.writes_vault is False
    assert result.activation_allowed is False
    assert result.requires_explicit_activation is True


def test_existing_selection_is_no_op_not_connectivity_proof():
    result = preview_root(choice(requested="Vault"), ())
    assert result.status == "no_change"
    assert result.reason == "already_selected"
    assert not result.activation_allowed
    assert preview_root(choice(requested="Vault", jobs_in_flight=True), ()).status == "no_change"


@pytest.mark.parametrize(
    "bad",
    [
        "", "../Vault", "Vault/..", "/Vault", "Vault/",
        "Vault//Sub", "Vault/.", "Vault/.hidden", r"Vault\bad",
        "https://cloud.example/remote.php/dav/files/user/Vault",
        "Vault?key=secret", "Vault\nSecret", "Vault\x7f", "V" * 101,
        "A/" * 17 + "B", "日本語",  # not in legacy V1 source alphabet
    ],
)
def test_bad_root_inputs_are_rejected_before_any_inventory_use(bad):
    assert_blocked(preview_root(choice(requested=bad), evidence()), "invalid_path")


@pytest.mark.parametrize(
    "bad",
    [None, 0, ["Vault"], {"path": "Vault"}, True],
)
def test_untyped_root_input_fails_closed(bad):
    assert_blocked(preview_root(choice(requested=bad), evidence()), "invalid_path")


@pytest.mark.parametrize("legacy", [
    ".legacy",
    "Legacy/.hidden",
    "A/" * 17 + "B",  # legacy-valid depth, beyond new V1 candidate cap
])
def test_legacy_active_root_is_not_rejected_by_new_candidate_policy(legacy):
    nc = {
        "webdav_url": "https://example.invalid/remote.php/dav/files/user/",
        "vault_folder": legacy,
        "nextcloud_user": "synthetic",
        "nextcloud_password": "SYNTHETIC-PRIVATE-ONLY",
    }
    assert validate_nextcloud(nc)["vault_folder"] == legacy
    assert preview_root(choice(active=legacy), evidence()).status == "candidate_valid"
    same = preview_root(
        choice(active=legacy, requested=legacy, jobs_in_flight=True), ()
    )
    assert same.status == "no_change"
    assert same.activation_allowed is False
    # A *new* choice with legacy-only syntax is blocked, even if active was
    # validly saved under the older connection contract.
    assert_blocked(
        preview_root(choice(active="Vault", requested=legacy), evidence()),
        "invalid_path",
    )


def test_exact_legacy_vault_folder_contract_is_not_widened():
    nc = {
        "webdav_url": "https://example.invalid/remote.php/dav/files/user/",
        "vault_folder": "Archive/Vault",
        "nextcloud_user": "synthetic",
        "nextcloud_password": "SYNTHETIC-PRIVATE-ONLY",
    }
    assert validate_nextcloud(nc)["vault_folder"] == choice().requested
    assert preview_root(choice(requested="Archive/Vault"), evidence()).status == "candidate_valid"


def test_source_revision_mismatch_rejected_even_with_valid_evidence():
    assert_blocked(
        preview_root(choice(current_revision="rev-b"), evidence()), "stale_source"
    )


@pytest.mark.parametrize("field", ["expected_revision", "current_revision", "namespace"])
def test_missing_source_binding_fails_closed(field):
    assert_blocked(
        preview_root(choice(**{field: ""}), evidence()), "invalid_request"
    )


def test_active_jobs_block_root_selection_but_do_not_alter_jobs():
    result = preview_root(choice(jobs_in_flight=True), evidence())
    assert_blocked(result, "jobs_in_flight")


@pytest.mark.parametrize("replacement,reason", [
    (lambda d: replace(d, complete=False), "incomplete_inventory"),
    (lambda d: replace(d, revision="rev-b"), "out_of_scope"),
    (lambda d: replace(d, namespace="account-b"), "out_of_scope"),
    (lambda d: replace(d, parent="Secret"), "out_of_scope"),
    (lambda d: replace(d, parent_resource_id="other-root"), "out_of_scope"),
    (lambda d: replace(d, entries=()), "incomplete_inventory"),
])
def test_fail_closed_on_stale_incomplete_or_cross_scope_evidence(replacement, reason):
    a, b = evidence()
    assert_blocked(preview_root(choice(), (a, replacement(b))), reason)


def test_exact_number_of_component_listings_is_required():
    a, b = evidence()
    assert_blocked(preview_root(choice(), (a,)), "incomplete_inventory")
    assert_blocked(preview_root(choice(), (a, b, b)), "incomplete_inventory")
    assert_blocked(preview_root(choice(), ()), "incomplete_inventory")


@pytest.mark.parametrize("replacement", [
    (
        Resource("Vault", "obj-old", "directory"),
        Resource("Archive", "obj-archive", "directory"),
        Resource("archive", "other-archive", "directory"),
    ),
    (
        Resource("Vault", "obj-old", "directory"),
        Resource("Archive", "obj-archive", "directory"),
        Resource("Ａrchive", "other-archive", "directory"),
    ),
])
def test_casefold_and_nfkc_sibling_collisions(replacement):
    root, nested = evidence()
    assert_blocked(preview_root(choice(), (replace(root, entries=replacement), nested)),
                   "casefold_collision")


@pytest.mark.parametrize("first_name,second_name", [
    ("\u0390", "\u03AA\u0301"),  # Greek iota/diaeresis/acute; fold decomposes
    ("\u0390", "\u0399\u0344"),  # alternate canonically equivalent spelling
    ("\u1FB3\u0308", "\u03B1\u0308\u03B9"),  # decompose ypogegrammeni *before* fold
    ("\u03B1\u0345\uFF9E", "\u03B1\u03B9\uFF9E"),  # canonical/compat mark reordering
    ("\u03B1\u0345\uFF9E", "\u03B1\u03B9\u3099"),  # canonical-first compatibility closure
    ("\u03B1\u0345\uFF9E", "\u03B1\u0345\u3099"),  # retain legacy NFKC alias collision
])
def test_unicode_caseless_and_legacy_sibling_collisions(first_name, second_name):
    root, nested = evidence()
    # Unique server IDs, complete inventory: only the Unicode-equivalent
    # sibling labels should cause this otherwise valid preview to be refused.
    rows = nested.entries + (
        Resource(first_name, "obj-greek-1", "directory"),
        Resource(second_name, "obj-greek-2", "directory"),
    )
    assert_blocked(
        preview_root(choice(), (root, replace(nested, entries=rows))),
        "casefold_collision",
    )


@pytest.mark.parametrize("resource", [
    Resource("Vault", "obj-new", "file"),
    Resource("Vault", "obj-new", "symlink"),
    Resource("Vault", "obj-new", "unknown"),
    Resource("Vault", "obj-new", "directory", is_alias=True),
])
def test_selected_non_directory_symlink_unknown_or_alias_rejected(resource):
    first, second = evidence()
    changed = replace(second, entries=(resource,))
    assert_blocked(preview_root(choice(), (first, changed)), "unsafe_target")


def test_cross_root_or_duplicate_resource_identity_rejected():
    first, second = evidence()
    assert_blocked(preview_root(
        choice(), (first, replace(second, entries=(Resource("Vault", "obj-account-root", "directory"),)))
    ), "identity_alias")
    assert_blocked(preview_root(
        choice(), (first, replace(second, entries=(Resource("Vault", "obj-old", "directory"),)))
    ), "identity_alias")


def test_sibling_alias_to_same_canonical_resource_rejected():
    first, second = evidence()
    same_identity = (
        Resource("Vault", "obj-new", "directory"),
        Resource("AliasVault", "obj-new", "directory"),
    )
    assert_blocked(
        preview_root(choice(), (first, replace(second, entries=same_identity))),
        "identity_alias",
    )


def test_canonical_ids_must_be_unique_across_all_listings():
    first, second = evidence()
    # Previously an unselected root sibling could alias the selected nested
    # target, because ID checks covered only ancestors and one listing.
    first_with_alias = replace(first, entries=first.entries + (
        Resource("Elsewhere", "obj-new", "directory"),
    ))
    assert_blocked(preview_root(choice(), (first_with_alias, second)), "identity_alias")

    # Unselected child must not reuse even the account-root resource.
    second_with_ancestor = replace(second, entries=second.entries + (
        Resource("BackToRoot", "obj-account-root", "directory"),
    ))
    assert_blocked(preview_root(choice(), (first, second_with_ancestor)), "identity_alias")

    # Duplicate *unselected* IDs across different parent listings also fail.
    first_other = replace(first, entries=first.entries + (
        Resource("Elsewhere", "obj-mirror", "directory"),
    ))
    second_other = replace(second, entries=second.entries + (
        Resource("Unrelated", "obj-mirror", "directory"),
    ))
    assert_blocked(preview_root(choice(), (first_other, second_other)), "identity_alias")


def test_no_implicit_fallback_for_case_or_missing_candidate():
    first, second = evidence()
    assert_blocked(preview_root(
        choice(), (first, replace(second, entries=(Resource("vault", "obj-new", "directory"),)))
    ), "candidate_missing")


def test_rejects_malformed_untrusted_evidence_shape_and_inventory_root():
    a, b = evidence()
    assert_blocked(preview_root(choice(), [a, b]), "invalid_request")
    assert_blocked(preview_root(choice(), (a, {"parent": "Archive"})), "invalid_inventory")
    assert_blocked(preview_root(choice(), (
        a, replace(b, entries=(Resource("Vault", "", "directory"),))
    )), "invalid_inventory")
    assert_blocked(preview_root(choice(), (
        a, replace(b, entries=(Resource("Vault", "obj-new", "executable"),))
    )), "invalid_inventory")


def test_oversized_sibling_name_is_rejected_before_unicode_work():
    first, second = evidence()
    expanded = replace(first, entries=first.entries + (
        Resource("x" * 256, "obj-oversize", "file"),
    ))
    assert_blocked(preview_root(choice(), (expanded, second)), "invalid_inventory")
    huge = replace(first, entries=first.entries + (
        Resource("x" * 2_000_000, "obj-huge", "file"),
    ))
    assert_blocked(preview_root(choice(), (huge, second)), "invalid_inventory")


def test_bounded_listing_size_and_identity_limit():
    first, second = evidence()
    oversized = tuple(Resource(f"Child-{i}", f"obj-{i}", "directory") for i in range(2049))
    assert_blocked(preview_root(choice(), (
        replace(first, entries=oversized), second
    )), "incomplete_inventory")
    assert_blocked(preview_root(choice(container_resource_id="!" * 128), evidence()),
                   "invalid_request")


def test_sensitive_values_cannot_be_observed_in_preview_response():
    private_path = "SecretRoot/Hidden"
    # Deliberately use sensitive-looking values as untrusted request fields.
    result = preview_root(choice(requested=private_path), evidence())
    wire = json.dumps(asdict(result), sort_keys=True)
    for forbidden in (
        "SecretRoot", "Hidden", "Vault", "Archive", "obj-old", "obj-archive",
        "account-a", "rev-a", "webdav_url", "nextcloud_password",
    ):
        assert forbidden not in wire
    assert set(asdict(result)) == {
        "version", "status", "reason", "writes_vault",
        "activation_allowed", "requires_explicit_activation",
    }


def test_preview_is_pure_no_filesystem_network_or_process(monkeypatch):
    import socket
    import subprocess
    import urllib.request

    def denied(*_a, **_kw):
        raise AssertionError("preview attempted provider/FS/process I/O")

    monkeypatch.setattr(Path, "open", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(urllib.request, "urlopen", denied)
    assert preview_root(choice(), evidence()).status == "candidate_valid"


# #303 V1/B0 provenance boundary: typed dataclasses still accept fields
# of arbitrary runtime types, including hostile str subclasses.
class _SpoofedEvidenceString(str):
    def __eq__(self, _other):
        return True

    def __ne__(self, _other):
        return False


class _RaisingEvidenceString(str):
    def __eq__(self, _other):
        raise AssertionError("raw identity must not reach comparison")

    def __ne__(self, _other):
        raise AssertionError("raw identity must not reach comparison")


@pytest.mark.parametrize("field", [
    "parent", "parent_resource_id", "namespace", "revision",
])
@pytest.mark.parametrize("bad", [
    _SpoofedEvidenceString("wrong-scope"),
    _RaisingEvidenceString("secret-scope"),
    None,
    42,
])
def test_directory_evidence_scope_fields_require_exact_str(field, bad):
    a, b = evidence()
    changed = replace(b, **{field: bad})
    assert_blocked(preview_root(choice(), (a, changed)), "invalid_inventory")


def test_selected_resource_kind_rejects_spoofed_enum_string():
    a, b = evidence()
    changed = replace(b, entries=(
        Resource("Vault", "obj-new", _SpoofedEvidenceString("not-a-kind")),
    ))
    assert_blocked(preview_root(choice(), (a, changed)), "invalid_inventory")


@pytest.mark.parametrize("field", ["requested", "active"])
def test_request_path_subclass_cannot_trigger_comparison_hook(field):
    bogus = choice(**{field: _RaisingEvidenceString("sensitive-path")})
    assert_blocked(preview_root(bogus, evidence()), "invalid_path")


@pytest.mark.parametrize("bad_name", [
    ".", "..", "Nested/Leaf", "/absolute", "contains\x00nul",
])
def test_immediate_sibling_names_must_be_structurally_valid(bad_name):
    first, second = evidence()
    bad_sibling = Resource(bad_name, "obj-malformed", "file")
    changed = replace(second, entries=second.entries + (bad_sibling,))
    assert_blocked(
        preview_root(choice(), (first, changed)), "invalid_inventory",
    )
