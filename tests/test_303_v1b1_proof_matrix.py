"""#303 V1/B1: strict, synthetic-only *plan* schema tests, not DAV proof.

This module never opens a network connection, reads secrets, or grants
authorization. All real-provider proof conditions remain UNKNOWN.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET

import pytest

ROOT = Path(__file__).parent
FIXTURE = ROOT / "fixtures" / "oa303_v1b1_proof_matrix.json"
BASE = "bc61c8803cfc92b3ad51fc2ad0b877a8e17b2cf4"
GATES = ("A", "I", "X", "L", "C", "R")
PHASES = {"read_only", "seed_required", "mutation_required", "offline_only"}
# Depth:0 describes only the addressed resource; everything involving
# immediate siblings must be Depth:1. Offline placeholders have no depth.
SELF_ONLY_IDS = {"A01", "A02", "A03", "A04"}

# Stable proof questions, phase and expected response; labels MUST not drift
# while the matrix still reports structural green.
CRITICAL = {
    "A01": ("modern_account_self", "read_only", "observe"),
    "A02": ("auth_reject", "read_only", "fail_closed"),
    "A03": ("cross_account_forbidden", "seed_required", "fail_closed"),
    "A04": ("missing_path", "read_only", "fail_closed"),
    "A05": ("redirect_denial", "offline_only", "fail_closed"),
    "A06": ("legacy_false_207", "offline_only", "fail_closed"),
    "I01": ("global_and_local_ids", "read_only", "observe"),
    "I02": ("repeat_read_ids", "read_only", "observe"),
    "I03": ("cross_instance_offline", "offline_only", "observe"),
    "I04": ("rename_move_identity", "mutation_required", "unknown"),
    "I05": ("duplicate_ids", "seed_required", "fail_closed"),
    "X01": ("local_mount_classification", "read_only", "observe"),
    "X02": ("shared_mount", "seed_required", "fail_closed"),
    "X03": ("group_mount", "seed_required", "fail_closed"),
    "X04": ("missing_mount_props", "seed_required", "fail_closed"),
    "X05": ("nested_share_alias", "seed_required", "fail_closed"),
    "X06": ("symlink_in_root", "seed_required", "fail_closed"),
    "X07": ("symlink_out_of_root", "seed_required", "fail_closed"),
    "X08": ("symlink_cycle", "seed_required", "fail_closed"),
    "X09": ("symlink_unknown", "seed_required", "fail_closed"),
    "X10": ("external_mount", "seed_required", "fail_closed"),
    "X11": ("external_session_mount", "seed_required", "fail_closed"),
    "L01": ("authorized_visible_siblings", "read_only", "observe"),
    "L02": ("malformed_multistatus", "offline_only", "fail_closed"),
    "L03": ("resource_limit", "seed_required", "fail_closed"),
    "L04": ("unicode_alias", "seed_required", "fail_closed"),
    "L05": ("unsafe_href", "offline_only", "fail_closed"),
    "L06": ("partial_propstat", "read_only", "fail_closed"),
    "L07": ("silent_member_omission", "seed_required", "fail_closed"),
    "L08": ("acl_hidden_child", "seed_required", "unknown"),
    "L09": ("contradictory_propstat", "offline_only", "fail_closed"),
    "C01": ("etag_not_atomic", "read_only", "unknown"),
    "C02": ("concurrent_mutation", "mutation_required", "unknown"),
    "C03": ("cross_level_mutation", "mutation_required", "unknown"),
    "C04": ("provider_coherence_capability", "read_only", "unknown"),
    "C05": ("aba_restoration", "mutation_required", "unknown"),
    "C06": ("post_recheck_mutation", "mutation_required", "unknown"),
    "R01": ("source_contract_offline", "offline_only", "observe"),
    "R02": ("credential_rotation", "mutation_required", "unknown"),
    "R03": ("no_change_not_connectivity", "offline_only", "fail_closed"),
    "R04": ("stale_evidence_replay", "offline_only", "fail_closed"),
}
TOP = {
    "schema_version", "project", "issue", "phase", "snapshot", "base_commit",
    "non_executable", "authority_grant", "scope",
    "provider_io_authorized", "credential_access_authorized",
    "setup_mutation_authorized", "activation_authorized",
    "future_authorizations", "proof_status", "allowed_future_read_methods",
    "approved_operations_now", "gate_order", "gate_descriptions",
    "phase_descriptions", "cases",
}
CASE_KEYS = {
    "id", "gate", "phase", "probe", "depth", "expected",
    "description", "coverage", "scope_label", "prerequisites",
}
SCOPE = {
    "target_class": "disposable_only",
    "endpoint_class": "authenticated_modern_dav_only",
    "origin_binding": "future_owner_pinned_allowlist",
    "path_binding": "future_owner_pinned_fixture_paths",
    "redirects": "denied",
    "production_eligible": False,
    "second_instance_access": False,
}
AUTH_FLAGS = {
    "provider_read_only", "fixture_seeding", "fixture_mutation",
    "credential_use", "second_instance_observation", "fault_injection",
}


@pytest.fixture(scope="module")
def manifest():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def assert_inert_matrix(m):
    assert type(m) is dict and set(m) == TOP
    assert type(m["schema_version"]) is int and m["schema_version"] == 1
    assert type(m["project"]) is str and m["project"] == "ObsidianAutomation"
    assert type(m["issue"]) is int and m["issue"] == 303
    assert type(m["phase"]) is str and m["phase"] == "V1/B1"
    assert type(m["snapshot"]) is str and m["snapshot"] == "synthetic_plan_only"
    assert type(m["base_commit"]) is str and m["base_commit"] == BASE
    assert m["non_executable"] is True and m["authority_grant"] == "NONE"
    for name in (
        "provider_io_authorized", "credential_access_authorized",
        "setup_mutation_authorized", "activation_authorized",
    ):
        assert m[name] is False
    assert type(m["scope"]) is dict and set(m["scope"]) == set(SCOPE)
    assert all(
        type(m["scope"][key]) is type(value) and m["scope"][key] == value
        for key, value in SCOPE.items()
    )
    assert type(m["future_authorizations"]) is dict
    assert set(m["future_authorizations"]) == AUTH_FLAGS
    assert all(v is False for v in m["future_authorizations"].values())
    assert m["allowed_future_read_methods"] == ["PROPFIND"]
    assert m["approved_operations_now"] == []
    assert m["gate_order"] == list(GATES)
    for key, keys in (
        ("gate_descriptions", set(GATES)),
        ("phase_descriptions", PHASES),
    ):
        descriptions = m[key]
        assert type(descriptions) is dict
        assert set(descriptions) == keys
        assert all(
            type(value) is str and 12 <= len(value) <= 180
            for value in descriptions.values()
        )
    assert m["proof_status"] == {
        **{gate: "UNKNOWN" for gate in GATES}, "O": "SYNTHETIC_SOURCE_ONLY",
    }

    cases = m["cases"]
    assert type(cases) is list and len(cases) == len(CRITICAL)
    assert set(c["id"] for c in cases) == set(CRITICAL)
    assert len({c["coverage"] for c in cases}) == len(cases)
    for c in cases:
        assert type(c) is dict and set(c) == CASE_KEYS
        assert re.fullmatch(r"[A-Z][0-9]{2}", c["id"])
        assert c["gate"] == c["id"][0] and c["gate"] in GATES
        assert (c["coverage"], c["phase"], c["expected"]) == CRITICAL[c["id"]]
        assert type(c["description"]) is str
        assert 12 <= len(c["description"]) <= 180
        assert c["phase"] in PHASES
        if c["phase"] in {"offline_only", "mutation_required"}:
            assert c["probe"] == "OFFLINE_ONLY" and c["depth"] is None
        else:
            assert c["probe"] == "PROPFIND" and type(c["depth"]) is int
            expected_depth = 0 if c["id"] in SELF_ONLY_IDS else 1
            assert c["depth"] == expected_depth
        scope = ("offline_synthetic" if c["phase"] == "offline_only"
                 else "future_approved_disposable_only")
        assert c["scope_label"] == scope

        if c["phase"] == "offline_only":
            required = ["offline_synthetic"]
        elif c["phase"] == "read_only":
            required = ["owner_read_only_disposable_approval", "preseeded_fixture"]
        elif c["phase"] == "seed_required":
            required = [
                "owner_read_only_disposable_approval",
                "independently_seeded_fixture",
            ]
        else:
            required = [
                "owner_read_only_disposable_approval",
                "owner_disposable_mutation_approval",
                "independently_seeded_fixture",
            ]
        if c["id"] in ("L07", "L08"):
            required.append("independent_inventory_oracle")
        if c["id"] == "I03":
            required.append("separate_second_instance_approval_for_real_comparison")
        assert c["prerequisites"] == required

    # Supplementary hygiene only. The closed key schema does the substantive
    # rejection of unapproved executable / credential / proof fields.
    wire = json.dumps(m, sort_keys=True, ensure_ascii=False)
    for pattern in (
        r"https?://", r"@[A-Za-z0-9.-]+", r"authorization\s*:",
        r"cookie\s*:", r"password\s*=", r"secret\s*=",
        r"api[_-]?key\s*=", r"bearer\s+[A-Za-z0-9]",
    ):
        assert re.search(pattern, wire, re.IGNORECASE) is None


def test_inert_manifest_is_well_formed_without_providing_authority(manifest):
    assert_inert_matrix(manifest)


def test_plan_has_provably_separate_setup_and_read_only_phases(manifest):
    by_phase = {phase: [c for c in manifest["cases"] if c["phase"] == phase]
                for phase in PHASES}
    assert len(by_phase["read_only"]) >= 8
    assert len(by_phase["seed_required"]) >= 10
    assert len(by_phase["mutation_required"]) >= 6
    assert len(by_phase["offline_only"]) >= 8
    assert {c["expected"] for c in by_phase["mutation_required"]} == {"unknown"}
    assert all(c["probe"] == "OFFLINE_ONLY" for c in by_phase["offline_only"])


def _case(m, case_id):
    return next(c for c in m["cases"] if c["id"] == case_id)


@pytest.mark.parametrize("mutate", [
    lambda m: m.__setitem__("password", "placeholder"),
    lambda m: m.__setitem__("source_verified", True),
    lambda m: m["scope"].__setitem__("production_eligible", True),
    lambda m: m["scope"].__setitem__("host", "unknown"),
    lambda m: m["future_authorizations"].__setitem__("provider_read_only", True),
    lambda m: m["future_authorizations"].__setitem__("unsafe_override", False),
    lambda m: m.__setitem__("base_commit", "a" * 40),
    lambda m: _case(m, "X06").__setitem__("coverage", "shared_mount"),
    lambda m: _case(m, "L07").__setitem__("phase", "read_only"),
    lambda m: _case(m, "L07").__setitem__("expected", "observe"),
    lambda m: _case(m, "L07").__setitem__("prerequisites", [
        "owner_read_only_disposable_approval", "independently_seeded_fixture",
    ]),
    lambda m: _case(m, "I03").__setitem__("probe", "PROPFIND"),
    lambda m: _case(m, "A05").__setitem__("scope_label", "future_approved_disposable_only"),
    lambda m: _case(m, "C05").__setitem__("expected", "verified"),
    lambda m: _case(m, "X09").__setitem__("resource_id", "made-up-id"),
    lambda m: m.__setitem__("approved_operations_now", ["PROPFIND"]),
    lambda m: m.__setitem__("schema_version", True),
    lambda m: m["scope"].__setitem__("second_instance_access", 0),
    lambda m: m["gate_descriptions"].__setitem__(
        "A", {"source_verified": True, "password": "placeholder"}
    ),
    lambda m: m.__setitem__("gate_descriptions", list(GATES)),
    lambda m: m["phase_descriptions"].__setitem__("offline_only", ["unsafe"]),
    lambda m: _case(m, "L01").__setitem__("depth", 0),
    lambda m: _case(m, "L07").__setitem__("depth", 0),
    lambda m: _case(m, "A01").__setitem__("depth", 1),
])
def test_unsafe_or_semantically_changed_manifest_is_rejected(manifest, mutate):
    edited = deepcopy(manifest)
    mutate(edited)
    with pytest.raises(AssertionError):
        assert_inert_matrix(edited)


def test_offline_propfind_template_is_typed_and_has_only_intended_props():
    plan = ROOT.parent / "docs" / (
        "oa303-v1b1-disposable-nextcloud-identity-provenance-proof-plan.md"
    )
    text = plan.read_text(encoding="utf-8")
    match = re.search(r"```xml\n(.*?)\n```", text, re.DOTALL)
    assert match is not None
    xml = match.group(1)
    assert "password" not in xml.lower() and "https://" not in xml.lower()
    _assert_exact_propfind_shape(xml)


def _assert_exact_propfind_shape(xml):
    tree = ET.fromstring(xml.encode("utf-8"))
    assert tree.tag == "{DAV:}propfind"
    assert not tree.attrib
    assert len(tree) == 1 and tree[0].tag == "{DAV:}prop"
    props = tree[0]
    assert not props.attrib
    # Ordered, exact multiplicity prevents duplicate property requests.
    expected = [
        "{DAV:}resourcetype", "{DAV:}getetag",
        "{http://owncloud.org/ns}id", "{http://owncloud.org/ns}fileid",
        "{http://owncloud.org/ns}permissions",
        "{http://owncloud.org/ns}share-types",
        "{http://nextcloud.org/ns}mount-type",
        "{http://nextcloud.org/ns}is-mount-root",
    ]
    assert [node.tag for node in props] == expected
    for node in tree.iter():
        assert not node.attrib
        assert not (node.text or "").strip()
        assert not (node.tail or "").strip()
        if node is not tree and node is not props:
            assert not tuple(node)


@pytest.mark.parametrize("modify_xml", [
    lambda s: s.replace("<oc:id/>", "<oc:id/><oc:id/>", 1),
    lambda s: s.replace("<oc:id/>", "<oc:id>secret</oc:id>", 1),
    lambda s: s.replace("<oc:id/>", "<oc:id/>unexpected", 1),
    lambda s: s.replace("<oc:id/>", '<oc:id fake="x"/>', 1),
    lambda s: s.replace('<d:propfind ', '<d:propfind extra="x" ', 1),
    lambda s: s.replace("<d:prop>", '<d:prop extra="x">', 1),
    lambda s: s.replace("<d:prop>", "<d:prop>unexpected", 1),
])
def test_request_template_rejects_duplicate_text_or_container_attributes(modify_xml):
    plan = ROOT.parent / "docs" / (
        "oa303-v1b1-disposable-nextcloud-identity-provenance-proof-plan.md"
    )
    text = plan.read_text(encoding="utf-8")
    match = re.search(r"```xml\n(.*?)\n```", text, re.DOTALL)
    assert match is not None
    original = match.group(1)
    changed = modify_xml(original)
    assert changed != original
    with pytest.raises(AssertionError):
        _assert_exact_propfind_shape(changed)
