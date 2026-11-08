"""#303 V1/B1 P1-A: offline candidate stack and authority-boundary contract.

This module is test-only and neither provisions a VM/LXC nor accesses Nextcloud,
Proxmox APIs, credentials, production resources or networks.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

import pytest

BASE = Path(__file__).parent
SOURCE = BASE / "fixtures" / "oa303_v1b1_p1a_stack_admission_candidate.json"
P1 = BASE / "fixtures" / "oa303_v1b1_p1_environment_catalog.json"
DOC = BASE.parent / "docs" / "oa303-v1b1-p1a-disposable-stack-and-isolation-admission.md"
TOP = {
    "schema_version", "kind", "issue_number", "base_main_sha",
    "verification_date", "status", "source_of_truth", "allowed_actions_now",
    "authorizations", "admission", "provider_gates", "candidate_stack",
    "resource_proposal", "isolation", "inspection_preflight",
    "p1a_p1b_boundary", "p1a_missing_proof", "conclusion",
}
PERMISSIONS = {
    "read_only_host_capacity_check", "read_only_host_network_check",
    "p1a_vm_provision", "p1a_bootstrap_download", "p1b_initial_app_install",
    "p1b_account_and_fixture_seeding", "p2_authenticated_dav_reads",
    "p3_mutation_or_fault_injection", "second_instance", "delete_or_teardown",
    "commit_push_pr", "deploy", "activation",
}
GATES = ("A", "I", "X", "L", "C", "R")
ADMISSIONS = ("p1a", "p1b", "p2", "p3")
STACK = {
    "server_name": "Nextcloud Server",
    "server_release_candidate": "35.0.2",
    "server_release_status": "PUBLIC_STABLE_AS_OF_2026_11_08",
    "server_archive_name": "nextcloud-35.0.2.tar.bz2",
    "server_archive_sha256": "UNVERIFIED",
    "server_archive_signature": "UNVERIFIED",
    "guest_os_candidate": "Ubuntu Server 24.04 LTS x86_64",
    "guest_os_iso_digest": "UNVERIFIED",
    "php_major_minor_candidate": "8.3",
    "php_patch_packages": "UNPINNED",
    "database_candidate": "PostgreSQL 16",
    "database_patch_packages": "UNPINNED",
    "webserver_candidate": "Apache 2.4 with php-fpm",
    "webserver_packages": "UNPINNED",
    "nextcloud_optional_apps": "NONE_PRESELECTED",
    "package_installation_method": "SOURCE_ARCHIVE_STAGED_NOT_INITIALIZED",
    "effective_installation_is_authorized": False,
}
RESOURCES = {
    "vcpu": 2, "memory_gib": 4, "storage_gib": 32,
    "assumption": "PROPOSAL_NOT_MEASURED",
    "cpu_ram_disk_headroom": "UNOBSERVED",
    "selected_proxmox_host": "UNSELECTED",
    "selected_vmid": "UNSELECTED",
    "selected_guest_storage": "UNSELECTED",
}
ISOLATION = {
    "vm_only_candidate": True,
    "unprivileged_guest_accounts": True,
    "host_network": "UNSELECTED",
    "guest_network": "UNSELECTED",
    "guest_ipv4": "UNSELECTED",
    "guest_ipv6": "UNSELECTED",
    "guest_dns": "UNSELECTED",
    "public_ingress": "DENY",
    "default_egress": "DENY",
    "bootstrap_egress": "DENY_UNTIL_SEPARATE_AUTHORIZATION",
    "provider_observer_ingress": "DENY_UNTIL_P2",
    "production_routes": "DENY",
    "hypervisor_management_routes": "DENY",
    "production_storage_mounts": False,
    "production_database_connection": False,
    "production_credentials": False,
    "production_vault_files": False,
    "shared_host_socket_mounts": False,
    "auto_update_enabled": False,
    "teardown_authorized": False,
}
INSPECTION = {
    "owner_nominated_exact_host": "UNSELECTED",
    "owner_nominated_allowed_read_only_fields": [],
    "read_only_target_approval": False,
    "cluster_wide_discovery_authorized": False,
    "unapproved_node_enumeration_authorized": False,
    "inspection_performed": False,
    "vmid_reservation_authorized": False,
    "final_placement_selected": False,
}
PARENT_PERMISSIONS = {
    "p1a_provisioning", "p1b_fixture_setup", "p2_authenticated_read",
    "p3_mutation", "second_instance", "destruction", "git_publication",
    "deploy", "activation",
}
PARENT_CASE_IDS = {
    *(f"A{i:02d}" for i in range(1, 7)),
    *(f"I{i:02d}" for i in range(1, 6)),
    *(f"X{i:02d}" for i in range(1, 12)),
    *(f"L{i:02d}" for i in range(1, 10)),
    *(f"C{i:02d}" for i in range(1, 7)),
    *(f"R{i:02d}" for i in range(1, 5)),
}
PARENT_PHASE_GROUPS = {
    "read_only": (
        "A01", "A02", "A04", "I01", "I02", "X01", "L01",
        "L06", "C01", "C04",
    ),
    "seed_required": (
        "A03", "I05", "X02", "X03", "X04", "X05",
        "X06", "X07", "X08", "X09", "X10", "X11", "L03", "L04",
        "L07", "L08",
    ),
    "offline_only": (
        "A05", "A06", "I03", "L02", "L05", "L09", "R01", "R03", "R04",
    ),
    "mutation_required": ("I04", "C02", "C03", "C05", "C06", "R02"),
}
PARENT_CASE_PHASE = {
    id_: phase for phase, ids in PARENT_PHASE_GROUPS.items() for id_ in ids
}

BOUNDARY = {
    "p1a_effect": "STAGE_OS_RUNTIME_DB_ARCHIVE_NO_APP_INITIALIZATION",
    "p1a_create_initial_nextcloud_admin": False,
    "p1a_create_nextcloud_users": False,
    "p1a_perform_occ_maintenance_install": False,
    "p1b_initial_nextcloud_admin_creation_requires_separate_consent": True,
    "p1b_auth_and_seed_requires_separate_consent": True,
    "p2_provider_reads_requires_separate_consent": True,
    "p3_mutations_requires_separate_consent": True,
    "destruction_requires_separate_consent": True,
}
MISSING = {
    "host_placement_headroom", "network_isolation_for_ipv4_ipv6_dns_management",
    "iso_and_archive_signature_digest", "package_versions",
    "bootstrap_egress_scope_and_lease", "operator_and_disposable_lease",
    "exact_provisioning_authority", "separate_teardown_identity",
    "hypervisor_backup_snapshot_replication_exclusion",
    "auto_started_service_listener_bind_scope",
    "bootstrap_dns_redirect_and_reboot_deny_persistence",
}


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_member:" + key)
        result[key] = value
    return result


def _load_strict_json(text):
    return json.loads(text, object_pairs_hook=_strict_pairs)


@pytest.fixture(scope="module")
def candidate():
    return _load_strict_json(SOURCE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def parent():
    return _load_strict_json(P1.read_text(encoding="utf-8"))


def validate(m, parent):
    assert type(m) is dict and set(m) == TOP, "top_contract"
    assert type(m["schema_version"]) is int and m["schema_version"] == 1, "schema_version"
    assert m["kind"] == "NON_EXECUTABLE_P1A_STACK_AND_ISOLATION_ADMISSION_CANDIDATE"
    assert type(m["issue_number"]) is int and m["issue_number"] == 303
    assert m["base_main_sha"] == "5932a0982d86e12407f9c18e31b6226391818224"
    assert m["verification_date"] == "2026-11-08"
    assert m["status"] == "DESIGN_ONLY_NOT_PROVISIONED", "status"
    assert m["source_of_truth"] == (
        "docs/oa303-v1b1-p1a-disposable-stack-and-isolation-admission.md"
    )
    assert type(m["allowed_actions_now"]) is list and m["allowed_actions_now"] == [], "allowed_actions_now"
    assert type(m["authorizations"]) is dict, "authorizations"
    assert set(m["authorizations"]) == PERMISSIONS, "authorizations"
    assert all(value is False for value in m["authorizations"].values()), "authorizations"
    assert type(m["admission"]) is dict and m["admission"] == {
        stage: "BLOCKED" for stage in ADMISSIONS
    }, "admission"
    assert type(m["provider_gates"]) is dict and m["provider_gates"] == {
        gate: "UNKNOWN" for gate in GATES
    }, "provider_gates"
    for key, expected in (
        ("candidate_stack", STACK),
        ("resource_proposal", RESOURCES),
        ("isolation", ISOLATION),
        ("inspection_preflight", INSPECTION),
        ("p1a_p1b_boundary", BOUNDARY),
    ):
        actual = m[key]
        assert type(actual) is dict and set(actual) == set(expected), key
        assert all(
            type(actual[k]) is type(v) and actual[k] == v
            for k, v in expected.items()
        ), key
    assert type(m["p1a_missing_proof"]) is dict, "p1a_missing_proof"
    assert m["p1a_missing_proof"] == {
        key: "UNKNOWN" for key in MISSING
    }, "p1a_missing_proof"
    assert m["conclusion"] == "P1A_ADMISSION_BLOCKED", "conclusion"

    # Parent integrity is a separate requirement; an empty authority map
    # MUST NOT satisfy a vacuous all(False) claim. This pins the complete
    # canonical case universe independently from the parent JSON.
    assert type(parent) is dict, "parent_structure"
    assert type(parent.get("issue_number")) is int, "parent_issue"
    assert parent["issue_number"] == 303, "parent_issue"
    assert parent.get("real_provider_proof") is False, "parent_provider_proof"
    assert type(parent.get("allowed_actions_now")) is list, "parent_actions"
    assert parent["allowed_actions_now"] == [], "parent_actions"
    assert type(parent.get("authority")) is dict, "parent_authority_keyset"
    assert set(parent["authority"]) == PARENT_PERMISSIONS, "parent_authority_keyset"
    assert all(v is False for v in parent["authority"].values()), "parent_authority_values"
    assert type(parent.get("admission")) is dict, "parent_admission"
    assert parent["admission"] == {
        stage: "BLOCKED" for stage in ADMISSIONS
    }, "parent_admission"
    assert type(parent.get("provider_gates")) is dict, "parent_provider_gates"
    assert parent["provider_gates"] == {
        gate: "UNKNOWN" for gate in GATES
    }, "parent_provider_gates"
    cases = parent.get("case_coverage")
    assert type(cases) is list and len(cases) == 41, "parent_case_cardinality"
    assert len(PARENT_CASE_IDS) == 41, "parent_fixed_case_count"
    assert set(PARENT_CASE_PHASE) == PARENT_CASE_IDS, "parent_fixed_case_set"
    assert all(type(c) is dict and type(c.get("case_id")) is str
               for c in cases), "parent_case_identity"
    ids = [c["case_id"] for c in cases]
    assert len(set(ids)) == len(ids) and set(ids) == PARENT_CASE_IDS, "parent_case_identity"
    mapping = {
        "read_only": ("P1-B", "P2"),
        "seed_required": ("P1-B", "P2"),
        "offline_only": ("NONE", "NONE"),
        "mutation_required": ("P3", "P3"),
    }
    for case in cases:
        case_id = case["case_id"]
        assert type(case.get("gate")) is str, "parent_case_gate"
        assert case["gate"] == case_id[0] and case["gate"] in GATES, "parent_case_gate"
        assert case.get("phase") == PARENT_CASE_PHASE[case_id], "parent_case_phase"
        assert (
            case.get("fixture_stage"), case.get("future_observation_stage")
        ) == mapping[case["phase"]], "parent_case_stages"
        assert case.get("status") == "NOT_RUN", "parent_case_status"

    # Simple lexical review helps catch inadvertent private endpoints/tokens.
    # It does not guarantee complete secret detection or network isolation.
    text = json.dumps(m, sort_keys=True, ensure_ascii=False)
    for pattern in (
        r"https?://", r"(?i)bearer\s+[a-z0-9]",
        r"(?i)password\s*[:=]", r"(?i)token\s*[:=]",
        r"/remote\.php/", r"@(?:[a-z0-9-]+\.)+[a-z]{2,}",
    ):
        assert not re.search(pattern, text)


def test_inert_candidate_is_blocked(candidate, parent):
    validate(candidate, parent)


def test_initial_admin_creation_is_explicitly_p1b_not_p1a(candidate, parent):
    validate(candidate, parent)
    assert candidate["p1a_p1b_boundary"]["p1a_create_initial_nextcloud_admin"] is False
    assert candidate["p1a_p1b_boundary"][
        "p1b_initial_nextcloud_admin_creation_requires_separate_consent"
    ] is True


@pytest.mark.parametrize("path,bad", [
    (("allowed_actions_now",), ["PROPFIND"]),
    (("status",), "PROVISIONED"),
    (("conclusion",), "P1A_ADMISSION_READY"),
    (("authorizations", "p1a_vm_provision"), True),
    (("authorizations", "p1a_bootstrap_download"), True),
    (("authorizations", "p1b_initial_app_install"), True),
    (("authorizations", "p2_authenticated_dav_reads"), True),
    (("authorizations", "delete_or_teardown"), True),
    (("authorizations", "activation"), True),
    (("admission", "p1a"), "READY"),
    (("admission", "p1b"), "READY"),
    (("provider_gates", "A"), "VERIFIED"),
    (("provider_gates", "C"), "VERIFIED"),
    (("candidate_stack", "server_release_candidate"), "35.0.3rc1"),
    (("candidate_stack", "server_archive_sha256"), "FAKE_PINNED"),
    (("candidate_stack", "guest_os_iso_digest"), "FAKE_PINNED"),
    (("candidate_stack", "effective_installation_is_authorized"), True),
    (("candidate_stack", "effective_installation_is_authorized"), 0),
    (("resource_proposal", "selected_proxmox_host"), "some-production-host"),
    (("resource_proposal", "selected_vmid"), "900"),
    (("resource_proposal", "vcpu"), True),
    (("resource_proposal", "memory_gib"), 0),
    (("isolation", "production_routes"), "ALLOW"),
    (("isolation", "hypervisor_management_routes"), "ALLOW"),
    (("isolation", "public_ingress"), "ALLOW"),
    (("isolation", "guest_dns"), "production-dns.local"),
    (("isolation", "bootstrap_egress"), "ALLOW"),
    (("isolation", "provider_observer_ingress"), "ALLOW"),
    (("isolation", "auto_update_enabled"), True),
    (("isolation", "production_storage_mounts"), True),
    (("isolation", "teardown_authorized"), True),
    (("inspection_preflight", "owner_nominated_exact_host"), "unapproved-node"),
    (("inspection_preflight", "owner_nominated_allowed_read_only_fields"), ["cpu"]),
    (("inspection_preflight", "read_only_target_approval"), True),
    (("inspection_preflight", "cluster_wide_discovery_authorized"), True),
    (("inspection_preflight", "unapproved_node_enumeration_authorized"), True),
    (("inspection_preflight", "inspection_performed"), True),
    (("inspection_preflight", "vmid_reservation_authorized"), True),
    (("inspection_preflight", "final_placement_selected"), True),
    (("p1a_p1b_boundary", "p1a_create_initial_nextcloud_admin"), True),
    (("p1a_p1b_boundary", "p1a_perform_occ_maintenance_install"), True),
    (("p1a_p1b_boundary", "p1b_initial_nextcloud_admin_creation_requires_separate_consent"), False),
    (("p1a_missing_proof", "host_placement_headroom"), "VERIFIED"),
    (("p1a_missing_proof", "iso_and_archive_signature_digest"), "VERIFIED"),
])
def test_attempted_authority_upgrade_or_unverified_proof_fails(
    candidate, parent, path, bad
):
    validate(candidate, parent)  # ensure a defective baseline cannot mask the target
    copy = deepcopy(candidate)
    target = copy
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = bad
    with pytest.raises(AssertionError, match=path[0]):
        validate(copy, parent)


@pytest.mark.parametrize("change,guard", [
    (lambda x: x.pop("candidate_stack"), "top_contract"),
    (lambda x: x["authorizations"].pop("delete_or_teardown"), "authorizations"),
    (lambda x: x["candidate_stack"].__setitem__("provider_auth_url", "placeholder"), "candidate_stack"),
    (lambda x: x["resource_proposal"].pop("cpu_ram_disk_headroom"), "resource_proposal"),
    (lambda x: x["isolation"].pop("hypervisor_management_routes"), "isolation"),
    (lambda x: x["inspection_preflight"].pop("read_only_target_approval"), "inspection_preflight"),
    (lambda x: x["p1a_missing_proof"].pop("separate_teardown_identity"), "p1a_missing_proof"),
    (lambda x: x["p1a_p1b_boundary"].__setitem__("provision_from_p1a", True), "p1a_p1b_boundary"),
])
def test_schema_remove_or_extra_fields_fail(candidate, parent, change, guard):
    validate(candidate, parent)  # prevent unrelated baseline failures
    copy = deepcopy(candidate)
    change(copy)
    with pytest.raises(AssertionError, match=guard):
        validate(copy, parent)


def test_document_requires_strict_gate_and_admin_boundary():
    body = DOC.read_text(encoding="utf8")
    for token in (
        "35.0.2", "UNVERIFIED", "UNSELECTED", "P1-A", "P1-B", "P2",
        "P3", "BLOCKED", "UNKNOWN", "Proxmox", "IPv6", "admin",
        "teardown", "separate", "signature", "source-only",
    ):
        assert token.lower() in body.lower()
    assert "P1-A provisioning approval must follow" in body


def test_owner_nomination_precedes_read_only_host_inspection(candidate, parent):
    validate(candidate, parent)
    scope = candidate["inspection_preflight"]
    assert scope["owner_nominated_exact_host"] == "UNSELECTED"
    assert scope["owner_nominated_allowed_read_only_fields"] == []
    assert scope["read_only_target_approval"] is False
    assert scope["cluster_wide_discovery_authorized"] is False
    assert scope["unapproved_node_enumeration_authorized"] is False
    assert scope["inspection_performed"] is False
    assert scope["vmid_reservation_authorized"] is False


def _parent_case(parent, case_id="A01"):
    matches = [row for row in parent["case_coverage"] if row["case_id"] == case_id]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.parametrize("edit,guard", [
    (lambda p: p.__setitem__("issue_number", True), "parent_issue"),
    (lambda p: p.__setitem__("real_provider_proof", True), "parent_provider_proof"),
    (lambda p: p.__setitem__("authority", {}), "parent_authority_keyset"),
    (lambda p: p["authority"].pop("destruction"), "parent_authority_keyset"),
    (lambda p: p["authority"].__setitem__("unknown_permit", False), "parent_authority_keyset"),
    (lambda p: p["authority"].__setitem__("p1a_provisioning", True), "parent_authority_values"),
    (lambda p: p["admission"].__setitem__("p1a", "READY"), "parent_admission"),
    (lambda p: p["provider_gates"].__setitem__("C", "VERIFIED"), "parent_provider_gates"),
    (lambda p: p["case_coverage"].pop(), "parent_case_cardinality"),
    (lambda p: p["case_coverage"].__setitem__(
        1, deepcopy(p["case_coverage"][0])
    ), "parent_case_identity"),
    (lambda p: _parent_case(p).__setitem__("gate", "C"), "parent_case_gate"),
    (lambda p: _parent_case(p).__setitem__("gate", {"unexpected": "synthetic"}), "parent_case_gate"),
    (lambda p: _parent_case(p).__setitem__("phase", "offline_only"), "parent_case_phase"),
    (lambda p: _parent_case(p).__setitem__("fixture_stage", "P3"), "parent_case_stages"),
    (lambda p: _parent_case(p).__setitem__("status", "PASS"), "parent_case_status"),
])
def test_untrusted_parent_cannot_degrade_readiness_contract(
    candidate, parent, edit, guard
):
    validate(candidate, parent)
    altered = deepcopy(parent)
    edit(altered)
    with pytest.raises(AssertionError, match=guard):
        validate(candidate, altered)


@pytest.mark.parametrize("original,replacement,duplicate_key", [
    (
        '"allowed_actions_now": []',
        '"allowed_actions_now": ["PROVISION"], "allowed_actions_now": []',
        "allowed_actions_now",
    ),
    (
        '"p1a_vm_provision": false',
        '"p1a_vm_provision": true, "p1a_vm_provision": false',
        "p1a_vm_provision",
    ),
    (
        '"p1a_vm_provision": false',
        '"p1a_vm_provision": false, "p1a_vm_provision": true',
        "p1a_vm_provision",
    ),
    (
        '"production_routes": "DENY"',
        '"production_routes": "ALLOW", "production_routes": "DENY"',
        "production_routes",
    ),
])
def test_raw_json_duplicate_members_rejected_even_with_safe_last_winner(
    candidate, original, replacement, duplicate_key
):
    raw = json.dumps(candidate)
    assert original in raw
    modified = raw.replace(original, replacement, 1)
    assert modified != raw
    with pytest.raises(ValueError, match="duplicate_json_member:" + duplicate_key):
        _load_strict_json(modified)


def test_nested_parent_duplicate_json_key_rejected(parent):
    raw = json.dumps(parent)
    original = '"p1a_provisioning": false'
    assert original in raw
    modified = raw.replace(
        original,
        '"p1a_provisioning": true, "p1a_provisioning": false',
        1,
    )
    with pytest.raises(ValueError, match="duplicate_json_member:p1a_provisioning"):
        _load_strict_json(modified)


def test_parent_reordering_is_valid_and_does_not_change_authority(candidate, parent):
    reordered = deepcopy(parent)
    reordered["case_coverage"].reverse()
    validate(candidate, reordered)
    assert _parent_case(reordered)["gate"] == "A"


@pytest.mark.parametrize("key,bad,guard", [
    ("gate", "C", "parent_case_gate"),
    ("phase", "offline_only", "parent_case_phase"),
    ("fixture_stage", "P3", "parent_case_stages"),
    ("status", "PASS", "parent_case_status"),
])
def test_parent_negative_mutations_target_a01_after_valid_reordering(
    candidate, parent, key, bad, guard
):
    reordered = deepcopy(parent)
    reordered["case_coverage"].reverse()
    validate(candidate, reordered)
    case = _parent_case(reordered)
    assert case[key] != bad
    case[key] = bad
    with pytest.raises(AssertionError, match=guard):
        validate(candidate, reordered)


def test_explicit_a01_first_and_last_parent_orderings_are_valid(candidate, parent):
    reordered = deepcopy(parent)
    target = _parent_case(reordered)
    reordered["case_coverage"].remove(target)
    reordered["case_coverage"].append(target)
    assert reordered["case_coverage"][-1]["case_id"] == "A01"
    validate(candidate, reordered)

    reordered["case_coverage"].reverse()
    assert reordered["case_coverage"][0]["case_id"] == "A01"
    validate(candidate, reordered)
