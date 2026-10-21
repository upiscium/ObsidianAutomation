"""Shared Validator/Reader artifact operations for Vault and External Source.

All inputs come from synthetic ZIP, Fake Provider and an isolated Vault.
These functions never decide Source admission, Human approval or canonical
Vault changes; Source-specific checks stay in their existing adapters.
"""
from __future__ import annotations

import pytest
from test_chatgpt_external_worker_e2e import run, setup

from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError
from obsidian_automation.evaluation_artifact import load_evaluation_context
from obsidian_automation.pre_review_job import (
    PreReviewJobError,
    stage_output,
)
from obsidian_automation.pre_review_shared_artifacts import (
    build_bound_fresh_retrieval,
    create_bound_evaluation_request,
)


def test_same_existing_validation_request_contract_is_used_for_external_source(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)
    before = {
        p.name: p.read_bytes() for p in (vault / "11-Knowledge").iterdir()
    }
    outcome = run(ai, private, preview, staged, vault)
    assert outcome.awaiting_human_review
    selected = stage_output(ai, staged["generation_id"], "external_validation")
    assert selected
    request_sha = create_bound_evaluation_request(
        ai,
        proposal_sha256=selected["proposal_sha256"],
        mutation_sha256=selected["mutation_sha256"],
    )
    assert request_sha == selected["request_sha256"]

    index_sha, context_sha = build_bound_fresh_retrieval(
        ai, vault,
        proposal_sha256=selected["proposal_sha256"],
        mutation_sha256=selected["mutation_sha256"],
        request_sha256=request_sha,
        top_k=5,
    )
    assert len(index_sha) == 64
    loaded = load_evaluation_context(ai, context_sha)
    assert loaded.proposal_sha256 == selected["proposal_sha256"]
    assert loaded.mutation_sha256 == selected["mutation_sha256"]
    assert loaded.request_sha256 == request_sha
    assert {p.name: p.read_bytes() for p in (vault / "11-Knowledge").iterdir()} == before


def test_mismatched_validated_mutation_never_becomes_eval_request(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)
    receipt = run(ai, private, preview, staged, vault)
    assert receipt.awaiting_human_review
    selected = stage_output(ai, staged["generation_id"], "external_validation")
    assert selected
    wrong = "0" * 64
    assert wrong != selected["mutation_sha256"]
    with pytest.raises(
        PreReviewJobError, match="evaluation request does not match validation"
    ):
        create_bound_evaluation_request(
            ai,
            proposal_sha256=selected["proposal_sha256"],
            mutation_sha256=wrong,
        )


def test_fresh_retrieval_rejects_cross_proposal_evaluation_request(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)
    receipt = run(ai, private, preview, staged, vault)
    assert receipt.awaiting_human_review
    selected = stage_output(ai, staged["generation_id"], "external_validation")
    assert selected
    with pytest.raises(
        PreReviewJobError, match="evaluation context binding mismatch"
    ):
        build_bound_fresh_retrieval(
            ai, vault,
            proposal_sha256="f" * 64,
            mutation_sha256=selected["mutation_sha256"],
            request_sha256=selected["request_sha256"],
            top_k=5,
        )


def test_fake_evaluation_request_is_not_a_source_authority_token(tmp_path):
    ai, _, _, _, _ = setup(tmp_path)
    with pytest.raises(ArtifactLifecycleError):
        create_bound_evaluation_request(
            ai,
            proposal_sha256="e" * 64,
            mutation_sha256="d" * 64,
        )
    assert not list((ai / "20-Review").glob("*.approval.json"))
    assert not list((ai / "30-Receipts").glob("*.json"))
