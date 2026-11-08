"""#303 V1/B1 P1: offline, non-executable readiness contract assertions.

No service instance, credentials, private network, or provider is accessed.
This validates an inert artifact and must not be used as operational consent.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

import pytest

HERE = Path(__file__).parent
CATALOG = HERE / "fixtures" / "oa303_v1b1_p1_environment_catalog.json"
SOURCE = HERE / "fixtures" / "oa303_v1b1_proof_matrix.json"
SHA = "192aaf050cd7510b785e3b4c29138efa4715b0cf"
STAGES = ("p1a", "p1b", "p2", "p3")
AUTH = {
    "p1a_provisioning", "p1b_fixture_setup", "p2_authenticated_read",
    "p3_mutation", "second_instance", "destruction",
    "git_publication", "deploy", "activation",
}
REQUIRED_TOP = {
    "schema_version", "artifact_type", "issue_number", "project",
    "source_main_sha", "real_provider_proof", "allowed_actions_now",
    "authority", "blueprint", "required_stage_evidence",
    "admission", "provider_gates", "source_matrix_file", "case_coverage",
}
BLUEPRINT = {
    "target_class": "UNSELECTED_DISPOSABLE_ONLY",
    "preferred_isolation": "UNPRIVILEGED_VM",
    "alternate": "UNPRIVILEGED_LXC_REQUIRES_EXTRA_REVIEW",
    "host": "UNSELECTED",
    "server_release": "UNPINNED",
    "image_digest": "UNPINNED",
    "database_version": "UNPINNED",
    "resource_allowance_proposal": {"vcpu": 2, "memory_gib": 4, "storage_gib": 32},
    "disposable_storage_only": True,
    "production_mounts_allowed": False,
    "production_credentials_allowed": False,
    "unapproved_outbound_allowed": False,
    "one_approved_observer_route_later": True,
    "existing_vault_data_allowed": False,
}
REQUIRED_GATES = ("A", "I", "X", "L", "C", "R")
CASE_FIELDS = {
    "case_id", "gate", "phase", "fixture_stage",
    "future_observation_stage", "special_constraint", "status",
}

# Independently authored, closed admission requirements; catalog values
# must not be able to silently replace their own acceptance criteria.
EVIDENCE_BY_STAGE = {
    "p1a": [
        "owner_selected_host",
        "operator_identity_and_lease",
        "pinned_runtime_and_app_digests",
        "isolated_route_and_egress_plan",
        "separate_test_storage_and_database",
        "no_production_mount_or_credential",
        "explicit_provisioning_permission",
        "teardown_identity_and_retention_plan",
    ],
    "p1b": [
        "p1a_completed_and_readback",
        "explicit_fixture_write_permission",
        "test_only_admin_and_viewer_accounts",
        "independently_produced_visible_child_oracle",
        "acl_hidden_child_uncertainty_recorded",
        "share_mount_alias_support_matrix",
        "no_production_data",
    ],
    "p2": [
        "p1b_completed_and_readback",
        "explicit_provider_read_permission",
        "fixed_disposable_origin_and_paths",
        "fixed_test_only_credential_principal",
        "read_only_depth_0_or_1",
        "transport_byte_and_time_limits",
        "private_redacted_receipts",
        "no_cross_origin_redirect",
    ],
    "p3": [
        "explicit_disposable_writer_permission",
        "recorded_exact_test_target",
        "approved_mutation_case_ids",
        "separate_test_origin_if_cross_instance",
        "reversible_disposable_mutations_only",
    ],
}

# Stable case_id -> conditional requirement; unspecified cases must remain
# ordinary_test_only. Changes require a distinct explicit design review.
SPECIAL_BY_CASE = {
    "A05": "redirect_negative_offline_only",
    "A06": "invalid_207_negative_offline_only",
    "I03": "second_instance_offline_only",
    "X03": "group_folder_app_conditional",
    "X06": "backend_symlink_conditional",
    "X07": "backend_symlink_conditional",
    "X08": "backend_symlink_conditional",
    "X09": "backend_symlink_conditional",
    "X10": "external_storage_app_conditional",
    "X11": "external_storage_app_conditional",
    "L02": "malformed_207_negative_offline_only",
    "L05": "malformed_href_negative_offline_only",
    "L07": "silent_omission_needs_independent_oracle",
    "L08": "acl_hidden_needs_independent_oracle",
    "L09": "conflicting_propstat_negative_offline_only",
    "C05": "aba_requires_separate_writer",
    "C06": "post_final_check_requires_separate_writer",
    "R02": "credential_rotation_requires_separate_writer",
}

# Independent pinned case-to-phase contract of the merged V1/B1 matrix.
# Source and catalog cannot collaboratively downgrade a mutation to P2.
CASES_BY_PHASE = {
    "read_only": (
        "A01", "A02", "A04", "I01", "I02", "X01", "L01",
        "L06", "C01", "C04",
    ),
    "seed_required": (
        "A03", "I05", "X02", "X03", "X04", "X05", "X06",
        "X07", "X08", "X09", "X10", "X11", "L03", "L04", "L07", "L08",
    ),
    "offline_only": (
        "A05", "A06", "I03", "L02", "L05", "L09",
        "R01", "R03", "R04",
    ),
    "mutation_required": (
        "I04", "C02", "C03", "C05", "C06", "R02",
    ),
}
PHASE_BY_CASE = {
    case_id: phase for phase, ids in CASES_BY_PHASE.items()
    for case_id in ids
}


@pytest.fixture(scope="module")
def matrix():
    return json.loads(SOURCE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def verify(catalog, matrix):
    assert type(catalog) is dict and set(catalog) == REQUIRED_TOP
    assert type(catalog["schema_version"]) is int and catalog["schema_version"] == 1
    assert catalog["artifact_type"] == "NON_EXECUTABLE_DISPOSABLE_READINESS_PLAN"
    assert type(catalog["issue_number"]) is int and catalog["issue_number"] == 303
    assert catalog["project"] == "ObsidianAutomation"
    assert type(catalog["source_main_sha"]) is str and catalog["source_main_sha"] == SHA
    assert catalog["real_provider_proof"] is False
    assert catalog["allowed_actions_now"] == []
    assert type(catalog["authority"]) is dict
    assert set(catalog["authority"]) == AUTH
    assert all(val is False for val in catalog["authority"].values())

    blueprint = catalog["blueprint"]
    assert type(blueprint) is dict and set(blueprint) == set(BLUEPRINT)
    for key, value in BLUEPRINT.items():
        assert type(blueprint[key]) is type(value) and blueprint[key] == value
    assert type(blueprint["resource_allowance_proposal"]) is dict
    assert all(
        type(v) is int and v > 0
        for v in blueprint["resource_allowance_proposal"].values()
    )

    assert catalog["source_matrix_file"] == (
        "tests/fixtures/oa303_v1b1_proof_matrix.json"
    )
    assert type(catalog["required_stage_evidence"]) is dict
    assert set(catalog["required_stage_evidence"]) == set(EVIDENCE_BY_STAGE)
    for stage in STAGES:
        evidence = catalog["required_stage_evidence"][stage]
        assert type(evidence) is list
        assert evidence == EVIDENCE_BY_STAGE[stage]
        assert len(evidence) == len(set(evidence))
        assert all(type(item) is str for item in evidence)
    assert catalog["admission"] == {stage: "BLOCKED" for stage in STAGES}
    assert catalog["provider_gates"] == {
        gate: "UNKNOWN" for gate in REQUIRED_GATES
    }

    old_cases = matrix["cases"]
    assert type(old_cases) is list and len(old_cases) == 41
    assert all(type(case) is dict and type(case.get("id")) is str
               for case in old_cases)
    source_ids = [case["id"] for case in old_cases]
    assert len(source_ids) == len(set(source_ids)), "duplicate source ID"
    by_id = {case["id"]: case for case in old_cases}
    assert len(by_id) == 41
    assert len(PHASE_BY_CASE) == 41
    assert set(by_id) == set(PHASE_BY_CASE)
    assert all(
        by_id[case_id]["phase"] == PHASE_BY_CASE[case_id]
        for case_id in PHASE_BY_CASE
    )
    cases = catalog["case_coverage"]
    assert type(cases) is list and len(cases) == len(by_id)
    assert len({case["case_id"] for case in cases}) == len(cases)
    assert {case["case_id"] for case in cases} == set(by_id)
    for entry in cases:
        assert type(entry) is dict and set(entry) == CASE_FIELDS
        case = by_id[entry["case_id"]]
        assert entry["gate"] == case["gate"]
        assert entry["phase"] == case["phase"]
        assert entry["status"] == "NOT_RUN"
        assert type(entry["special_constraint"]) is str
        assert entry["special_constraint"] == SPECIAL_BY_CASE.get(
            entry["case_id"], "ordinary_test_only"
        )
        future, fixture = {
            "offline_only": ("NONE", "NONE"),
            "read_only": ("P2", "P1-B"),
            "seed_required": ("P2", "P1-B"),
            "mutation_required": ("P3", "P3"),
        }[case["phase"]]
        assert entry["future_observation_stage"] == future
        assert entry["fixture_stage"] == fixture

    # These are strict named scenario constraints, NOT provider proof.
    assert set(SPECIAL_BY_CASE) <= set(by_id)
    assert all(key[:1] in REQUIRED_GATES for key in SPECIAL_BY_CASE)

    # Lexical hygiene supplements closed-schema validation, not substitutes
    # for real secret scanning or network-enforced access controls.
    text = json.dumps(catalog, ensure_ascii=False, sort_keys=True)
    for forbidden in (
        r"https?://", r"password\s*[:=]", r"authorization\s*[:=]",
        r"token\s*[:=]", r"bearer\s+[a-z0-9]", r"/remote\.php/",
        r"@(?:[a-z0-9-]+\.)+[a-z]{2,}",
    ):
        assert re.search(forbidden, text, re.IGNORECASE) is None


def test_catalog_is_inert_and_all_gates_stay_blocked(catalog, matrix):
    verify(catalog, matrix)


def test_every_original_case_is_accounted_for_without_provider_execution(catalog, matrix):
    verify(catalog, matrix)
    phases = {p: [c for c in catalog["case_coverage"] if c["phase"] == p]
              for p in ("read_only", "seed_required", "offline_only", "mutation_required")}
    assert {key: len(val) for key, val in phases.items()} == {
        "read_only": 10, "seed_required": 16,
        "offline_only": 9, "mutation_required": 6,
    }
    assert all(c["fixture_stage"] == "P1-B" for c in phases["read_only"])
    assert all(c["fixture_stage"] == "P1-B" for c in phases["seed_required"])
    assert all(c["fixture_stage"] == "NONE" for c in phases["offline_only"])
    assert all(c["fixture_stage"] == "P3" for c in phases["mutation_required"])


@pytest.mark.parametrize("mutate", [
    lambda d: d.__setitem__("credential", "placeholder"),
    lambda d: d.__setitem__("real_provider_proof", True),
    lambda d: d["authority"].__setitem__("p2_authenticated_read", True),
    lambda d: d["authority"].__setitem__("creation", False),
    lambda d: d["blueprint"].__setitem__("host", "unknown-production-host"),
    lambda d: d["blueprint"].__setitem__("production_mounts_allowed", 0),
    lambda d: d["blueprint"].__setitem__("resource_allowance_proposal", {"vcpu": 4}),
    lambda d: d["blueprint"].__setitem__("database_version", "current"),
    lambda d: d["admission"].__setitem__("p1b", "READY"),
    lambda d: d["provider_gates"].__setitem__("C", "VERIFIED"),
    lambda d: d["required_stage_evidence"].pop("p1a"),
    lambda d: d["allowed_actions_now"].append("PROPFIND"),
    lambda d: d["case_coverage"].pop(),
    lambda d: d["case_coverage"][0].__setitem__("status", "PASS"),
    lambda d: d["case_coverage"][0].__setitem__("case_id", "X06"),
    lambda d: d["case_coverage"][0].__setitem__("future_observation_stage", "P1-A"),
    lambda d: next(c for c in d["case_coverage"] if c["case_id"] == "L07")
              .__setitem__("fixture_stage", "P2"),
    lambda d: next(c for c in d["case_coverage"] if c["case_id"] == "C06")
              .__setitem__("future_observation_stage", "P2"),
])
def test_unapproved_scope_or_semantic_change_is_rejected(catalog, matrix, mutate):
    modified = deepcopy(catalog)
    mutate(modified)
    with pytest.raises((AssertionError, KeyError, TypeError)):
        verify(modified, matrix)


def test_docs_must_explicitly_separate_provisioning_reads_writes_and_teardown():
    p = HERE.parent / "docs" / "oa303-v1b1-p1-disposable-environment-readiness.md"
    prose = p.read_text(encoding="utf-8")
    for token in ("P1-A", "P1-B", "P2", "P3", "BLOCKED/NYI",
                  "production", "independent", "teardown", "separately"):
        assert token.lower() in prose.lower()
    assert "No P1-A receipt can change" in prose


def test_swapping_setup_and_provider_read_evidence_lists_is_rejected(catalog, matrix):
    changed = deepcopy(catalog)
    e = changed["required_stage_evidence"]
    e["p1b"], e["p2"] = e["p2"], e["p1b"]
    with pytest.raises(AssertionError):
        verify(changed, matrix)


@pytest.mark.parametrize("stage", STAGES)
def test_unrelated_syntactically_valid_evidence_labels_fail(stage, catalog, matrix):
    changed = deepcopy(catalog)
    n = len(changed["required_stage_evidence"][stage])
    changed["required_stage_evidence"][stage] = [
        f"unrelated_{i}" for i in range(n)
    ]
    with pytest.raises(AssertionError):
        verify(changed, matrix)


@pytest.mark.parametrize("stage,required_label", [
    (stage, label)
    for stage, labels in EVIDENCE_BY_STAGE.items()
    for label in labels
])
def test_removing_any_one_required_stage_evidence_label_fails(
    stage, required_label, catalog, matrix
):
    changed = deepcopy(catalog)
    changed["required_stage_evidence"][stage].remove(required_label)
    with pytest.raises(AssertionError):
        verify(changed, matrix)


@pytest.mark.parametrize("case_id,incorrect_constraint", [
    ("X03", "ordinary_test_only"),
    ("X03", "external_storage_app_conditional"),
    ("X10", "group_folder_app_conditional"),
    ("R02", "ordinary_test_only"),
    ("C05", "backend_symlink_conditional"),
    ("C06", "aba_requires_separate_writer"),
    ("L07", "acl_hidden_needs_independent_oracle"),
    ("A05", "ordinary_test_only"),
])
def test_case_specific_constraint_substitutions_fail(
    case_id, incorrect_constraint, catalog, matrix
):
    changed = deepcopy(catalog)
    entry = next(c for c in changed["case_coverage"] if c["case_id"] == case_id)
    entry["special_constraint"] = incorrect_constraint
    with pytest.raises(AssertionError):
        verify(changed, matrix)


def test_two_valid_specific_constraint_labels_cannot_be_swapped(catalog, matrix):
    changed = deepcopy(catalog)
    x03 = next(c for c in changed["case_coverage"] if c["case_id"] == "X03")
    x10 = next(c for c in changed["case_coverage"] if c["case_id"] == "X10")
    x03["special_constraint"], x10["special_constraint"] = (
        x10["special_constraint"], x03["special_constraint"]
    )
    with pytest.raises(AssertionError):
        verify(changed, matrix)


@pytest.mark.parametrize("conflicting", [False, True])
def test_duplicate_source_matrix_id_must_not_be_silently_collapsed(
    conflicting, catalog, matrix
):
    source = deepcopy(matrix)
    extra = deepcopy(source["cases"][0])
    if conflicting:
        extra["phase"] = "offline_only"
    source["cases"].append(extra)
    assert len(source["cases"]) == 42
    with pytest.raises(AssertionError):
        verify(catalog, source)


def test_replaced_source_case_id_repetition_must_not_erase_case(catalog, matrix):
    source = deepcopy(matrix)
    source["cases"][-1]["id"] = source["cases"][0]["id"]
    assert len(source["cases"]) == 41
    with pytest.raises(AssertionError):
        verify(catalog, source)


@pytest.mark.parametrize("left,right", [
    ("C05", "A01"),  # known A-B-A writer scenario downgraded to read-only
    ("A05", "A01"),  # offline redirect negative test changed into provider read
])
def test_count_preserving_coordinated_source_and_catalog_phase_swap_is_rejected(
    left, right, catalog, matrix
):
    changed_source = deepcopy(matrix)
    changed_catalog = deepcopy(catalog)
    sleft = next(c for c in changed_source["cases"] if c["id"] == left)
    sright = next(c for c in changed_source["cases"] if c["id"] == right)
    sleft["phase"], sright["phase"] = sright["phase"], sleft["phase"]

    mapping = {
        "offline_only": ("NONE", "NONE"),
        "read_only": ("P1-B", "P2"),
        "seed_required": ("P1-B", "P2"),
        "mutation_required": ("P3", "P3"),
    }
    for case_id in (left, right):
        entry = next(c for c in changed_catalog["case_coverage"]
                     if c["case_id"] == case_id)
        source = next(c for c in changed_source["cases"]
                      if c["id"] == case_id)
        entry["phase"] = source["phase"]
        entry["fixture_stage"], entry["future_observation_stage"] = mapping[
            source["phase"]
        ]

    # Even after coordinated change and preservation of phase totals, the
    # independent immutable case/phase contract must fail closed.
    from collections import Counter
    assert Counter(c["phase"] for c in changed_source["cases"]) == Counter(
        c["phase"] for c in matrix["cases"]
    )
    with pytest.raises(AssertionError):
        verify(changed_catalog, changed_source)
