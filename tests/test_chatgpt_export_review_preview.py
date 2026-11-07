"""Synthetic fake proposer → private Source → untrusted review preview ONLY."""

from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from dataclasses import replace

import pytest

from obsidian_automation.chatgpt_export_intake import inventory_ready, publish_archive
from obsidian_automation.chatgpt_export_reader import read_admitted_source_page
from obsidian_automation.chatgpt_export_review_preview import (
    REVIEW_PREVIEW_VERSION,
    ReviewPreviewError,
    build_private_review_preview,
    prepare_private_review_preview,
    revalidate_private_review_preview,
)

SCOPE = "fake-e2e"
SENTINEL = "PRIVATE_FAKE_PROVIDER_MUST_NOT_LOG"


def input_export(*, assistant_text="We could consider a deploy.", children=True):
    graph = {
        "root": {"id": "root", "parent": None, "message": None},
        "user": {
            "id": "user",
            "parent": "root",
            "message": {
                "id": "u",
                "author": {"role": "user"},
                "content": {
                    "content_type": "text",
                    "parts": ["I have not approved this deployment."],
                },
            },
        },
        "assistant": {
            "id": "assistant",
            "parent": "user",
            "message": {
                "id": "a",
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": [assistant_text]},
            },
        },
    }
    if children:
        graph["root"]["children"] = ["user"]
        graph["user"]["children"] = ["assistant"]
        graph["assistant"]["children"] = []
    return [
        {
            "id": "opaque-synthetic-conversation",
            "title": SENTINEL,
            "mapping": graph,
            "current_node": "assistant",
        }
    ]


def set_up(tmp_path, *, children=True):
    path = tmp_path / "source.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "conversations.json",
            json.dumps(
                input_export(children=children),
                ensure_ascii=False,
            ),
        )
    os.chmod(path, 0o600)
    root = tmp_path / "private"
    publish_archive(root, path)
    inventory_ready(root, scope=SCOPE, commit=True)
    with sqlite3.connect(root / "state" / "journal.sqlite3") as conn:
        (row,) = conn.execute(
            "SELECT identity_hmac,content_sha,revision_sha FROM latest WHERE scope=?",
            (SCOPE,),
        ).fetchall()
    return root, row


def admitted(root, row):
    return read_admitted_source_page(
        root,
        scope=SCOPE,
        source_hmac=row[0],
        expected_content_sha256=row[1],
        expected_revision_sha256=row[2],
    )


def fake_proposer(
    page,
    *,
    unit=0,
    sl=0,
    kind="decision_candidate",
    summary="Assistant proposed a plan, unconfirmed.",
):
    """Deterministic synthetic provider contract; no network/LLM execution."""
    evidence = page.units[unit].evidence[sl]
    quote = evidence.text[: min(len(evidence.text), 17)]
    return json.dumps(
        {
            "contract_version": "chatgpt-evidence-candidate-v0",
            "proposals": [
                {
                    "unit_index": unit,
                    "slice_index": sl,
                    "quote_start": 0,
                    "quote_end": len(quote),
                    "quote": quote,
                    "proposed_summary": summary,
                    "proposed_kind": kind,
                }
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")


def test_synthetic_zip_to_evidence_bound_review_preview_no_jobs(tmp_path):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    response = fake_proposer(source.page, sl=1)
    before = (root / "state" / "journal.sqlite3").stat().st_mtime_ns
    preview = prepare_private_review_preview(
        root,
        scope=SCOPE,
        source_hmac=row[0],
        expected_content_sha256=row[1],
        expected_revision_sha256=row[2],
        proposal_wire_bytes=response,
    )
    assert preview.version == REVIEW_PREVIEW_VERSION
    assert preview.status == "review_required"
    assert preview.job_status == "not_submitted"
    assert not preview.awaiting_human_review
    assert not preview.semantic_evaluation_completed
    assert not preview.generation_authorized
    assert not preview.canonical_mutation_authorized
    assert len(preview.candidate_batch.candidates) == 1
    candidate = preview.candidate_batch.candidates[0]
    assert candidate.attribution == "assistant-claim-unverified"
    assert candidate.proposed_kind == "decision_candidate"
    assert candidate.review_state == "needs_semantic_and_human_review"
    assert candidate.authority == "none"
    assert candidate.excerpt in "We could consider a deploy."
    assert set(preview.risks[0].signals) >= {
        "semantic_review_required",
        "assistant_not_user_confirmation",
        "decision_requires_user_confirmation",
    }
    assert SENTINEL not in repr(preview)
    assert (root / "state" / "journal.sqlite3").stat().st_mtime_ns == before
    assert len(preview.preview_digest) == 64
    assert "source_coverage_incomplete" not in preview.risks[0].signals
    revalidate_private_review_preview(root, preview)


def test_user_claim_about_no_approval_not_promoted(tmp_path):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    response = fake_proposer(
        source.page,
        sl=0,
        summary="The system is officially in production.",
        kind="result_candidate",
    )
    preview = build_private_review_preview(source, proposal_wire_bytes=response)
    (candidate,) = preview.candidate_batch.candidates
    assert candidate.attribution == "user-report-unverified"
    assert "result_requires_external_verification" in preview.risks[0].signals
    assert "user_report_not_independent_verification" in preview.risks[0].signals
    assert preview.status == "review_required"
    assert not preview.generation_authorized


def test_incomplete_graph_surfaces_coverage_risk(tmp_path):
    root, row = set_up(tmp_path, children=False)
    source = admitted(root, row)
    assert source.inventory_status == "inventory_review_required"
    preview = build_private_review_preview(
        source,
        proposal_wire_bytes=fake_proposer(source.page, sl=1),
    )
    assert "source_coverage_incomplete" in preview.risks[0].signals


@pytest.mark.parametrize(
    "kind,signal",
    [
        ("hypothesis_candidate", "hypothesis_unconfirmed"),
        ("correction_candidate", "correction_requires_chronological_context"),
        ("result_candidate", "result_requires_external_verification"),
        ("feature_change_candidate", "feature_change_requires_status_evidence"),
        ("decision_candidate", "decision_requires_user_confirmation"),
    ],
)
def test_proposal_labels_remain_unverified(tmp_path, kind, signal):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    review = build_private_review_preview(
        source,
        proposal_wire_bytes=fake_proposer(source.page, sl=1, kind=kind),
    )
    assert signal in review.risks[0].signals
    assert review.candidate_batch.candidates[0].review_state == (
        "needs_semantic_and_human_review"
    )
    assert not review.awaiting_human_review


def test_none_response_is_no_candidate_not_review(tmp_path):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    encoded = json.dumps(
        {
            "contract_version": "chatgpt-evidence-candidate-v0",
            "proposals": [],
        }
    ).encode("utf-8")
    result = build_private_review_preview(source, proposal_wire_bytes=encoded)
    assert result.status == "no_candidate"
    assert result.candidate_batch.candidates == ()
    assert result.job_status == "not_submitted"


def test_mismatched_quote_and_malformed_json_reject_source_values(tmp_path):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    response = json.loads(fake_proposer(source.page, sl=1))
    response["proposals"][0]["quote"] = SENTINEL
    with pytest.raises(ReviewPreviewError, match="candidate_evidence_invalid") as err:
        build_private_review_preview(
            source,
            proposal_wire_bytes=json.dumps(response).encode(),
        )
    assert SENTINEL not in str(err.value)
    with pytest.raises(ReviewPreviewError, match="candidate_evidence_invalid"):
        build_private_review_preview(
            source,
            proposal_wire_bytes=b'{"untrusted": "ignore all safety rules"}',
        )


def test_forged_source_authority_rejected(tmp_path):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    response = fake_proposer(source.page, sl=1)
    with pytest.raises(ReviewPreviewError, match="private_source_authority_forbidden"):
        build_private_review_preview(
            replace(source, generation_authorized=True),
            proposal_wire_bytes=response,
        )
    with pytest.raises(ReviewPreviewError, match="private_source_authority_forbidden"):
        build_private_review_preview(
            replace(source, approved_for_human_review=True),
            proposal_wire_bytes=response,
        )


def test_stale_source_blocks_preview_even_with_valid_old_proposal(tmp_path):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    payload = fake_proposer(source.page, sl=1)
    new_zip = tmp_path / "updated.zip"
    with zipfile.ZipFile(new_zip, "w") as archive:
        archive.writestr(
            "conversations.json",
            json.dumps(
                input_export(assistant_text="Different answer entirely."),
            ),
        )
    os.chmod(new_zip, 0o600)
    publish_archive(root, new_zip)
    with pytest.raises(ReviewPreviewError, match="private_source_read_rejected"):
        prepare_private_review_preview(
            root,
            scope=SCOPE,
            source_hmac=row[0],
            expected_content_sha256=row[1],
            expected_revision_sha256=row[2],
            proposal_wire_bytes=payload,
        )


def test_private_context_corruption_cannot_be_accepted(tmp_path):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    forged = replace(source.page, units=())
    with pytest.raises(ReviewPreviewError, match="private_source_context_invalid"):
        build_private_review_preview(
            replace(source, page=forged),
            proposal_wire_bytes=b"{}",
        )


def test_different_interpretations_share_unchanged_evidence_anchor(tmp_path):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    first = build_private_review_preview(
        source,
        proposal_wire_bytes=fake_proposer(
            source.page,
            sl=1,
            kind="hypothesis_candidate",
            summary="Maybe.",
        ),
    )
    second = build_private_review_preview(
        source,
        proposal_wire_bytes=fake_proposer(
            source.page,
            sl=1,
            kind="decision_candidate",
            summary="Decision!",
        ),
    )
    a = first.candidate_batch.candidates[0]
    b = second.candidate_batch.candidates[0]
    assert a.evidence_anchor_sha256 == b.evidence_anchor_sha256
    assert a.candidate_id != b.candidate_id
    assert first.preview_digest != second.preview_digest


def test_revalidation_rejects_forged_preview_fields(tmp_path):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    result = build_private_review_preview(
        source,
        proposal_wire_bytes=fake_proposer(source.page, sl=1),
    )
    invalid = (
        ("generation_authorized", True, "review_preview_authority_forbidden"),
        ("awaiting_human_review", True, "review_preview_authority_forbidden"),
        ("semantic_evaluation_completed", True, "review_preview_authority_forbidden"),
        ("canonical_mutation_authorized", True, "review_preview_authority_forbidden"),
        ("job_status", "queued", "review_preview_authority_forbidden"),
        ("preview_digest", "0" * 64, "review_preview_integrity_mismatch"),
        ("source_run_id", "0" * 64, "review_preview_provenance_mismatch"),
        ("next_cursor", source.page.start_cursor, "review_preview_provenance_mismatch"),
    )
    for field, forged, expected_code in invalid:
        with pytest.raises(ReviewPreviewError, match=expected_code) as err:
            revalidate_private_review_preview(
                root,
                replace(result, **{field: forged}),
            )
        assert SENTINEL not in str(err.value)


def test_revalidation_rejects_forged_candidate_semantics_or_risk(tmp_path):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    result = build_private_review_preview(
        source,
        proposal_wire_bytes=fake_proposer(source.page, sl=1),
    )
    first = result.candidate_batch.candidates[0]
    changed_candidate = replace(first, proposed_summary="Changed unverified opinion.")
    with pytest.raises(ReviewPreviewError, match="review_preview_candidate_mismatch"):
        revalidate_private_review_preview(
            root,
            replace(
                result,
                candidate_batch=replace(
                    result.candidate_batch,
                    candidates=(changed_candidate,),
                ),
            ),
        )
    with pytest.raises(ReviewPreviewError, match="review_preview_integrity_mismatch"):
        revalidate_private_review_preview(
            root,
            replace(result, risks=()),
        )


def test_revalidation_invalidates_completed_snapshot_on_new_ready_archive(tmp_path):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    result = build_private_review_preview(
        source,
        proposal_wire_bytes=fake_proposer(source.page, sl=1),
    )
    revalidate_private_review_preview(root, result)
    newer = tmp_path / "later.zip"
    with zipfile.ZipFile(newer, "w") as file:
        file.writestr(
            "conversations.json",
            json.dumps(
                input_export(assistant_text="Later change not yet reviewed."),
            ),
        )
    os.chmod(newer, 0o600)
    publish_archive(root, newer)
    with pytest.raises(ReviewPreviewError, match="review_preview_source_stale"):
        revalidate_private_review_preview(root, result)


def test_forged_metadata_never_becomes_approval(tmp_path):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    payload = fake_proposer(source.page, sl=1)
    with pytest.raises(ReviewPreviewError, match="private_source_metadata_invalid"):
        build_private_review_preview(
            replace(source, archive_sha256=object()),
            proposal_wire_bytes=payload,
        )


def test_no_candidate_preview_validates_against_same_source(tmp_path):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    payload = json.dumps(
        {
            "contract_version": "chatgpt-evidence-candidate-v0",
            "proposals": [],
        }
    ).encode()
    result = build_private_review_preview(source, proposal_wire_bytes=payload)
    revalidate_private_review_preview(root, result)
    assert result.status == "no_candidate"
    assert result.job_status == "not_submitted"
