"""Synthetic private on-demand Reviewer evidence; never an approval route."""

from __future__ import annotations

import json
import os
import zipfile
from dataclasses import replace

import pytest
from test_chatgpt_export_review_preview import input_export
from test_chatgpt_external_worker_e2e import run, setup

from obsidian_automation.chatgpt_export_intake import publish_archive
from obsidian_automation.chatgpt_external_review_evidence import (
    PACKET_VERSION,
    ExternalReviewEvidenceError,
    read_private_review_evidence,
)
from obsidian_automation.pre_review_job import job_status


def _after_e2e(tmp_path):
    ai, private, preview, staged, vault = setup(tmp_path)
    result = run(ai, private, preview, staged, vault)
    assert result.awaiting_human_review
    return ai, private, preview, staged, vault, result


def test_private_reviewer_gets_exact_quote_role_and_explicit_uncertainty(tmp_path):
    ai, private, preview, staged, _, receipt = _after_e2e(tmp_path)
    before = set((ai / "20-Review").iterdir())
    packet = read_private_review_evidence(
        ai,
        private_root=private,
        preview=preview,
        job_id=staged["job_id"],
    )
    assert packet.version == PACKET_VERSION
    assert packet.review_state == "awaiting_human_review"
    assert packet.semantic_status == "unknown"
    assert packet.evaluation_sha256 == receipt.evaluation_sha256
    assert packet.source_revision_sha256 == preview.revision_sha256
    assert len(packet.packet_sha256) == 64
    assert len(packet.quotations) == 1
    view = packet.quotations[0]
    source = preview.candidate_batch.candidates[0]
    assert view.exact_quote == source.excerpt
    assert view.exact_quote_sha256 == source.excerpt_sha256
    assert view.source_anchor_sha256 == source.evidence_anchor_sha256
    assert view.message_sha256 == source.source_message_sha256
    assert view.message_index == source.message_index
    assert view.quote_start_codepoint == source.quote_start_codepoint
    assert view.quote_end_codepoint == source.quote_end_codepoint
    assert view.speaker_role == "assistant"
    assert view.attribution == "assistant-claim-unverified"
    assert "semantic_review_required" in view.required_checks
    assert not view.semantic_truth_verified
    assert not view.user_confirmation_verified
    assert not view.correction_chronology_verified
    assert view.source_quote_verified
    assert packet.human_approval_granted is False
    assert packet.canonical_vault_mutated is False
    assert packet.context_complete
    assert packet.source_hmac not in repr(packet)
    assert view.exact_quote not in repr(view)
    assert set((ai / "20-Review").iterdir()) == before
    assert not list((ai / "20-Review").glob("*.approval.json"))


def test_pending_review_requires_exact_current_source_revision(tmp_path):
    ai, private, preview, staged, _, _ = _after_e2e(tmp_path)
    newer = tmp_path / "correction.zip"
    with zipfile.ZipFile(newer, "w") as archive:
        archive.writestr(
            "conversations.json",
            json.dumps(
                input_export(assistant_text="Later correction not accepted yet")
            ),
        )
    os.chmod(newer, 0o600)
    publish_archive(private, newer)
    with pytest.raises(
        ExternalReviewEvidenceError,
        match="private_review_source_unavailable",
    ):
        read_private_review_evidence(
            ai,
            private_root=private,
            preview=preview,
            job_id=staged["job_id"],
        )
    assert job_status(ai, staged["job_id"])["current_generation"]["state"] == (
        "awaiting_human_review"
    )


def test_forged_quote_or_summary_never_becomes_displayable_evidence(tmp_path):
    ai, private, preview, staged, _, _ = _after_e2e(tmp_path)
    base = preview.candidate_batch.candidates[0]
    altered = replace(base, excerpt="Invented quotation not in Source")
    altered_batch = replace(preview.candidate_batch, candidates=(altered,))
    with pytest.raises(ExternalReviewEvidenceError):
        read_private_review_evidence(
            ai,
            private_root=private,
            preview=replace(preview, candidate_batch=altered_batch),
            job_id=staged["job_id"],
        )
    altered = replace(
        base, proposed_summary="A user has permanently approved production"
    )
    altered_batch = replace(preview.candidate_batch, candidates=(altered,))
    with pytest.raises(ExternalReviewEvidenceError):
        read_private_review_evidence(
            ai,
            private_root=private,
            preview=replace(preview, candidate_batch=altered_batch),
            job_id=staged["job_id"],
        )


def test_wrong_job_identity_and_no_approval_record_rejected(tmp_path):
    ai, private, preview, _, _, _ = _after_e2e(tmp_path)
    with pytest.raises(ExternalReviewEvidenceError):
        read_private_review_evidence(
            ai,
            private_root=private,
            preview=preview,
            job_id="0" * 64,
        )
    assert not list((ai / "20-Review").glob("*.approval.json"))


@pytest.mark.parametrize("limit", [0, True, -1, 17, 18])
def test_reader_refuses_invalid_excerpt_bundle_limits(tmp_path, limit):
    ai, private, preview, staged, _, _ = _after_e2e(tmp_path)
    with pytest.raises(
        ExternalReviewEvidenceError,
        match="private_review_limit_invalid",
    ):
        read_private_review_evidence(
            ai,
            private_root=private,
            preview=preview,
            job_id=staged["job_id"],
            max_candidates=limit,
        )


def test_incomplete_selected_page_cannot_claim_chronology_verified(tmp_path):
    ai, private, preview, staged, _, _ = _after_e2e(tmp_path)
    packet = read_private_review_evidence(
        ai,
        private_root=private,
        preview=preview,
        job_id=staged["job_id"],
    )
    assert packet.context_complete
    # Even when the bounded source page is complete, later corrections in
    # other conversations or outside the Export can still invalidate it.
    assert all(not q.correction_chronology_verified for q in packet.quotations)


def test_private_reviewer_entrypoint_rejects_unauthorized_production_scope(tmp_path):
    ai, private, preview, staged, _, _ = _after_e2e(tmp_path)
    forged = replace(preview, scope="production-private")
    with pytest.raises(
        ExternalReviewEvidenceError,
        match="private_review_scope_not_authorized",
    ):
        read_private_review_evidence(
            ai,
            private_root=private,
            preview=forged,
            job_id=staged["job_id"],
        )
    assert not list((ai / "20-Review").glob("*.approval.json"))


def test_review_evidence_is_stable_for_same_committed_source(tmp_path):
    ai, private, preview, staged, _, _ = _after_e2e(tmp_path)
    first = read_private_review_evidence(
        ai,
        private_root=private,
        preview=preview,
        job_id=staged["job_id"],
    )
    second = read_private_review_evidence(
        ai,
        private_root=private,
        preview=preview,
        job_id=staged["job_id"],
    )
    assert first == second
    assert first.packet_sha256 == second.packet_sha256
    assert first.human_approval_granted is False
