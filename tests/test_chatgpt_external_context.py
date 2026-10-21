"""Synthetic private Source -> strict non-authoritative external Context."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from test_chatgpt_export_review_preview import (
    admitted,
    fake_proposer,
    set_up,
)

from obsidian_automation.chatgpt_export_review_preview import (
    build_private_review_preview,
)
from obsidian_automation.chatgpt_external_context import (
    EXTERNAL_CONTEXT_KIND,
    ExternalContextError,
    build_external_context,
    parse_external_context,
    revalidate_external_context,
)


def context_fixture(tmp_path, *, no_candidate=False):
    root, row = set_up(tmp_path)
    source = admitted(root, row)
    response = (
        json.dumps(
            {
                "contract_version": "chatgpt-evidence-candidate-v0",
                "proposals": [],
            }
        ).encode()
        if no_candidate
        else fake_proposer(source.page, sl=1)
    )
    preview = build_private_review_preview(
        source,
        proposal_wire_bytes=response,
    )
    return root, preview, build_external_context(preview)


def test_private_external_context_exact_roundtrip_and_freshness(tmp_path):
    root, preview, context = context_fixture(tmp_path)
    assert context.context_kind == EXTERNAL_CONTEXT_KIND
    assert context.version == 1
    assert context.source_preview_sha256 == preview.preview_digest
    assert context.evidence[0].candidate_id == (
        preview.candidate_batch.candidates[0].candidate_id
    )
    assert "assistant_not_user_confirmation" in context.evidence[0].risks
    assert context.generation_authorized is False
    assert context.human_review_approved is False
    assert context.vault_mutation_authorized is False
    assert "PRIVATE_FAKE_PROVIDER_MUST_NOT_LOG" not in repr(context)
    assert parse_external_context(context.to_json_bytes()) == context
    revalidate_external_context(root, preview, context)


def test_no_candidate_is_not_an_approved_job(tmp_path):
    root, preview, context = context_fixture(tmp_path, no_candidate=True)
    assert context.evidence == ()
    assert context.generation_authorized is False
    revalidate_external_context(root, preview, context)


@pytest.mark.parametrize(
    "field",
    [
        "source_hmac",
        "source_archive_sha256",
        "source_content_sha256",
        "source_revision_sha256",
        "source_run_sha256",
        "source_preview_sha256",
    ],
)
def test_forged_provenance_digest_rejected(tmp_path, field):
    root, preview, context = context_fixture(tmp_path)
    with pytest.raises(
        ExternalContextError,
        match="external_context_provenance_mismatch",
    ):
        revalidate_external_context(
            root,
            preview,
            replace(context, **{field: "a" * 64}),
        )


@pytest.mark.parametrize(
    "field",
    [
        "generation_authorized",
        "human_review_approved",
        "vault_mutation_authorized",
    ],
)
def test_context_authority_flags_must_be_false(tmp_path, field):
    _, _, context = context_fixture(tmp_path)
    raw = context.payload()
    raw[field] = True
    with pytest.raises(
        ExternalContextError, match="external_context_authority_forbidden"
    ):
        parse_external_context(json.dumps(raw).encode())


def test_unknown_context_kind_is_not_vault_bundle(tmp_path):
    _, _, context = context_fixture(tmp_path)
    raw = context.payload()
    raw["context_kind"] = "context-bundle"
    with pytest.raises(ExternalContextError, match="external_context_kind_invalid"):
        parse_external_context(json.dumps(raw).encode())


def test_duplicate_properties_rejected(tmp_path):
    _, _, context = context_fixture(tmp_path)
    data = context.to_json_bytes().replace(
        b'"record_version":1',
        b'"record_version":1,"record_version":1',
    )
    with pytest.raises(
        ExternalContextError, match="duplicate_external_context_property"
    ):
        parse_external_context(data)


def test_no_unexpected_fields_and_no_vault_path(tmp_path):
    _, _, context = context_fixture(tmp_path)
    raw = context.payload()
    raw["target_path"] = "11-Knowledge/Forged.md"
    with pytest.raises(
        ExternalContextError, match="external_context_properties_invalid"
    ):
        parse_external_context(json.dumps(raw).encode())


def test_tampered_candidate_summary_rejected_at_private_revalidation(tmp_path):
    root, preview, context = context_fixture(tmp_path)
    altered = replace(
        context.evidence[0],
        proposed_summary="Assistant completed and approved production",
    )
    with pytest.raises(
        ExternalContextError, match="external_context_provenance_mismatch"
    ):
        revalidate_external_context(
            root,
            preview,
            replace(context, evidence=(altered,)),
        )


def test_wrong_evidence_count_and_duplicate_candidate_rejected(tmp_path):
    _, _, context = context_fixture(tmp_path)
    raw = context.payload()
    raw["evidence"] *= 2
    with pytest.raises(ExternalContextError, match="duplicate_external_candidate"):
        parse_external_context(json.dumps(raw).encode())
    raw["evidence"] = raw["evidence"] * 20
    with pytest.raises(ExternalContextError, match="external_evidence_limit"):
        parse_external_context(json.dumps(raw).encode())


@pytest.mark.parametrize("field", ["source_cursor", "next_cursor"])
def test_cursor_requires_nonnegative_bounded_ints(tmp_path, field):
    _, _, context = context_fixture(tmp_path)
    raw = context.payload()
    raw[field] = [True, -1]
    with pytest.raises(ExternalContextError, match="invalid_external_context_cursor"):
        parse_external_context(json.dumps(raw).encode())


def test_untrusted_summary_has_strict_byte_bound(tmp_path):
    _, _, context = context_fixture(tmp_path)
    raw = context.payload()
    raw["evidence"][0]["proposed_summary"] = "日本語" * 1600
    with pytest.raises(ExternalContextError, match="invalid_evidence_summary"):
        parse_external_context(json.dumps(raw, ensure_ascii=False).encode())


def test_non_json_values_and_oversized_payload_rejected():
    with pytest.raises(ExternalContextError, match="invalid_external_context_bytes"):
        parse_external_context(b"x" * (64 * 1024 + 1))
    with pytest.raises(ExternalContextError, match="invalid_external_context_number"):
        parse_external_context(b'{"untrusted": NaN}')
