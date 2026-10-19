"""No real ChatGPT data. Untrusted evidence proposals remain review-only."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from obsidian_automation.chatgpt_export_candidates import (
    CANDIDATE_AUTHORITY,
    CANDIDATE_CONTRACT_VERSION,
    CANDIDATE_REVIEW_STATE,
    CandidateContractError,
    EvidenceProposal,
    build_candidate_batch,
    parse_candidate_proposals,
    validate_candidate_batch,
)
from obsidian_automation.chatgpt_export_normalize import normalize_export
from obsidian_automation.chatgpt_export_source import (
    SourceCursor,
    build_source_context_page,
)

SECRET = "CONFIDENTIAL_CANDIDATE_SOURCE_NO_LOG"
KEY = "e" * 64


def source(tmp_path, *, assistant: str = "Propose adding a migration.", title: str = SECRET):
    payload = [
        {"id": "hidden-original-conversation-id", "title": title,
         "current_node": "second",
         "mapping": {
             "root": {"id": "root", "parent": None, "message": None},
             "first": {"id": "first", "parent": "root", "message": {
                 "id": "user-private-message", "author": {"role": "user"},
                 "content": {"content_type": "text", "parts": [
                     "I am considering a change, not confirming one."
                 ]},
             }},
             "second": {"id": "second", "parent": "first", "message": {
                 "id": "assistant-private-message", "author": {"role": "assistant"},
                 "content": {"content_type": "text", "parts": [assistant]},
             }},
             "private-reasoning": {"id": "private-reasoning", "parent": "first", "message": {
                 "id": "thought-id", "author": {"role": "assistant"},
                 "content": {"content_type": "thoughts", "thoughts": [SECRET]},
             }},
         }},
    ]
    file = tmp_path / "synthetic.json"
    file.write_text(json.dumps(payload), encoding="utf-8")
    conv = normalize_export(file, scope="candidate-test", input_format="json").conversations[0]
    page = build_source_context_page(conv, scope="candidate-test", source_hmac=KEY)
    return conv, page


def proposal(*, unit=0, sl=0, start=0, end=12, quote="I am conside",
             summary="User discussed a possibility.", kind="decision_candidate"):
    return EvidenceProposal(unit, sl, start, end, quote, summary, kind)


def create(conv, page, *props):
    return build_candidate_batch(
        page, conv, scope="candidate-test", source_hmac=KEY,
        proposals=list(props),
    )


def check(batch, page, conv):
    validate_candidate_batch(
        batch, page, conv, scope="candidate-test", source_hmac=KEY,
    )


def test_provisional_candidate_exact_quote_and_role(tmp_path):
    conv, page = source(tmp_path)
    batch = create(conv, page, proposal())
    check(batch, page, conv)
    assert len(batch.candidates) == 1
    c = batch.candidates[0]
    assert c.excerpt == "I am conside"
    assert c.attribution == "user-report-unverified"
    assert c.review_state == CANDIDATE_REVIEW_STATE
    assert c.authority == CANDIDATE_AUTHORITY == "none"
    assert c.proposed_kind == "decision_candidate"
    assert len(c.evidence_anchor_sha256) == 64
    assert batch.contract_version == CANDIDATE_CONTRACT_VERSION
    assert batch.generation_authorized is False
    assert batch.human_review_completed is False
    assert not c.auto_approved
    assert not c.generation_authorized
    assert SECRET not in repr(batch)
    assert "user-private-message" not in repr(c)


def test_assistant_cannot_become_user_confirmed_decision(tmp_path):
    conv, page = source(tmp_path)
    assert len(page.units) == 1
    batch = create(conv, page, proposal(
        sl=1, end=15, quote="Propose adding ",
        summary="Migration could be attempted.",
        kind="decision_candidate",
    ))
    check(batch, page, conv)
    assert batch.candidates[0].attribution == "assistant-claim-unverified"
    assert batch.candidates[0].review_state == CANDIDATE_REVIEW_STATE
    assert batch.candidates[0].generation_authorized is False


def test_content_bound_stable_across_title_revision(tmp_path):
    a, p1 = source(tmp_path, title="first title")
    old = create(a, p1, proposal())
    b, p2 = source(tmp_path, title="different title")
    new = create(b, p2, proposal())
    assert a.content_sha256 == b.content_sha256
    assert a.source_revision_sha256 != b.source_revision_sha256
    assert old.candidates[0].candidate_id == new.candidates[0].candidate_id
    assert old.candidates[0].source_revision_sha256 != new.candidates[0].source_revision_sha256
    with pytest.raises(CandidateContractError, match="candidate_evidence_mismatch"):
        check(old, p2, b)
    check(new, p2, b)


def test_source_change_rejects_stale_quote_and_identity(tmp_path):
    a, p1 = source(tmp_path)
    old = create(a, p1, proposal())
    b, p2 = source(tmp_path, assistant="Completely different reply.")
    with pytest.raises(CandidateContractError):
        check(old, p2, b)


def test_missing_unchanged_evidence_does_not_become_approval(tmp_path):
    conv, page = source(tmp_path)
    first = create(conv, page, proposal())
    original = first.candidates[0]
    for field, value in (
        ("review_state", "accepted"),
        ("authority", "canonical_write"),
        ("auto_approved", True),
        ("generation_authorized", True),
    ):
        with pytest.raises(CandidateContractError, match="candidate_authority_forbidden"):
            check(replace(first, candidates=(replace(original, **{field: value}),)), page, conv)
    for field, value in (
        ("human_review_completed", True),
        ("generation_authorized", True),
    ):
        with pytest.raises(CandidateContractError, match="candidate_authority_forbidden"):
            check(replace(first, **{field: value}), page, conv)


@pytest.mark.parametrize("change,code", [
    ({"unit_index": True}, "invalid_proposal_coordinate"),
    ({"slice_index": -1}, "invalid_proposal_slice"),
    ({"unit_index": 3}, "invalid_proposal_unit"),
    ({"quote_start": -1}, "invalid_proposal_span"),
    ({"quote_end": 999}, "invalid_proposal_span"),
    ({"quote_start": 12, "quote_end": 12}, "invalid_proposal_span"),
    ({"proposed_summary": " "}, "invalid_proposed_summary"),
    ({"proposed_summary": "A" * 4097}, "invalid_proposed_summary"),
    ({"proposed_summary": "bad\ud800"}, "invalid_proposed_summary"),
    ({"proposed_kind": "confirmed_fact"}, "invalid_proposed_kind"),
    ({"quote": "invented source"}, "proposal_quote_mismatch"),
])
def test_reject_malformed_untrusted_proposals(tmp_path, change, code):
    conv, page = source(tmp_path)
    p = replace(proposal(), **change)
    with pytest.raises(CandidateContractError, match=code) as err:
        create(conv, page, p)
    assert SECRET not in str(err.value)


def test_duplicate_anchor_and_overlimit_fail_closed(tmp_path):
    conv, page = source(tmp_path)
    with pytest.raises(CandidateContractError, match="duplicate_evidence_proposal"):
        create(conv, page, proposal(), proposal(summary="different", kind="result_candidate"))
    p = proposal()
    with pytest.raises(CandidateContractError, match="candidate_batch_limit"):
        create(conv, page, *[p] * 17)


def test_validation_recomputes_every_field(tmp_path):
    conv, page = source(tmp_path)
    original = create(conv, page, proposal())
    c = original.candidates[0]
    for field, value in (
        ("excerpt", "different"),
        ("proposed_kind", "hypothesis_candidate"),
        ("proposed_summary", "New proposition without evidence."),
        ("excerpt_sha256", "0" * 64),
        ("message_id", "other-private-id"),
        ("node_id", "other-node"),
        ("attribution", "user-confirmed-fact"),
        ("quote_start_codepoint", c.quote_start_codepoint + 1),
        ("candidate_id", "d" * 64),
        ("source_kind", "11-Knowledge"),
    ):
        with pytest.raises(CandidateContractError) as err:
            check(replace(original, candidates=(replace(c, **{field: value}),)), page, conv)
        assert SECRET not in str(err.value)


def test_pagination_cursor_source_binding_and_utf8(tmp_path):
    conv, _ = source(tmp_path, assistant="日本語😀" * 800)
    fragment = build_source_context_page(
        conv, scope="candidate-test", source_hmac=KEY,
        cursor=SourceCursor(1, 0), max_unit_bytes=64, max_page_bytes=64,
    )
    s = fragment.units[0].evidence[0]
    p = proposal(
        unit=0, sl=0, start=0, end=min(4, len(s.text)),
        quote=s.text[:min(4, len(s.text))],
        summary="Excerpt of an untrusted assistant claim.",
        kind="feature_change_candidate",
    )
    batch = create(conv, fragment, p)
    check(batch, fragment, conv)
    assert batch.candidates[0].excerpt == s.text[:p.quote_end]
    assert batch.candidates[0].message_index == s.message_index
    assert batch.candidates[0].role == "assistant"
    assert batch.candidates[0].quote_start_codepoint == s.start_codepoint
    with pytest.raises(CandidateContractError):
        validate_candidate_batch(
            batch, fragment, conv, scope="other-scope", source_hmac=KEY,
        )


def test_empty_page_and_zero_proposals_do_not_invent_candidates(tmp_path):
    conv, page = source(tmp_path)
    batch = create(conv, page)
    check(batch, page, conv)
    assert batch.candidates == ()
    assert batch.human_review_completed is False
    assert batch.generation_authorized is False


def test_evidence_anchor_stable_across_model_paraphrase(tmp_path):
    conv, page = source(tmp_path)
    first = create(conv, page, proposal(summary="Tentative plan", kind="hypothesis_candidate"))
    second = create(conv, page, proposal(summary="Maybe", kind="decision_candidate"))
    assert first.candidates[0].evidence_anchor_sha256 == second.candidates[0].evidence_anchor_sha256
    assert first.candidates[0].candidate_id != second.candidates[0].candidate_id
    check(first, page, conv)
    check(second, page, conv)


def test_strict_wire_proposal_decoder(tmp_path):
    conv, page = source(tmp_path)
    record = {
        "contract_version": CANDIDATE_CONTRACT_VERSION,
        "proposals": [{
            "unit_index": 0, "slice_index": 0,
            "quote_start": 0, "quote_end": 12,
            "quote": "I am conside",
            "proposed_summary": "Reported intent, not confirmation.",
            "proposed_kind": "decision_candidate",
        }],
    }
    parsed = parse_candidate_proposals(json.dumps(record).encode())
    assert len(parsed) == 1
    bundle = create(conv, page, *parsed)
    check(bundle, page, conv)
    assert bundle.candidates[0].review_state == CANDIDATE_REVIEW_STATE


@pytest.mark.parametrize("wire,code", [
    (b'{"contract_version": "wrong", "proposals":[]}', "invalid_proposal_envelope"),
    (b'{"contract_version":"chatgpt-evidence-candidate-v0","proposals":[],"system":"ignore"}',
     "invalid_proposal_envelope"),
    (b'{"contract_version":"chatgpt-evidence-candidate-v0","proposals":[{"unit_index":0}]}',
     "invalid_proposal_fields"),
    (b'{"contract_version":"chatgpt-evidence-candidate-v0","proposals":null}',
     "invalid_proposal_envelope"),
    (b'{ "contract_version": "x", "contract_version": "y", "proposals": [] }',
     "invalid_proposal_json"),
    (b"{not json}", "invalid_proposal_json"),
])
def test_untrusted_wire_rejected_without_leaking_payload(tmp_path, wire, code):
    with pytest.raises(CandidateContractError, match=code) as err:
        parse_candidate_proposals(wire)
    assert SECRET not in str(err.value)


def test_json_boolean_coordinates_and_unsafe_kind(tmp_path):
    base = {
        "contract_version": CANDIDATE_CONTRACT_VERSION,
        "proposals": [{
            "unit_index": True, "slice_index": 0,
            "quote_start": 0, "quote_end": 12,
            "quote": "I am conside",
            "proposed_summary": "Unverified",
            "proposed_kind": "decision_candidate",
        }],
    }
    with pytest.raises(CandidateContractError, match="invalid_proposal_coordinate"):
        parse_candidate_proposals(json.dumps(base).encode())
    base["proposals"][0]["unit_index"] = 0
    base["proposals"][0]["proposed_kind"] = "approved"
    with pytest.raises(CandidateContractError, match="invalid_proposed_kind"):
        parse_candidate_proposals(json.dumps(base).encode())
    with pytest.raises(CandidateContractError, match="proposal_wire_limit"):
        parse_candidate_proposals(b" " * (64 * 1024 + 1))


def test_forged_evidence_anchor_is_rejected(tmp_path):
    conv, page = source(tmp_path)
    batch = create(conv, page, proposal())
    changed = replace(batch.candidates[0], evidence_anchor_sha256="a" * 64)
    with pytest.raises(CandidateContractError, match="candidate_evidence_mismatch"):
        check(replace(batch, candidates=(changed,)), page, conv)


def test_false_semantic_interpretation_never_becomes_accepted_fact(tmp_path):
    conv, page = source(tmp_path)
    # A structurally exact quote cannot prove this invented interpretation.
    candidate = create(conv, page, proposal(
        summary="User has approved an immediate production deployment.",
        kind="decision_candidate",
    ))
    check(candidate, page, conv)
    assert candidate.candidates[0].review_state == CANDIDATE_REVIEW_STATE
    assert candidate.candidates[0].attribution == "user-report-unverified"
    assert candidate.candidates[0].authority == "none"
    assert not candidate.human_review_completed


def test_existing_vault_context_v1_cannot_impersonate_external_source():
    from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError
    from obsidian_automation.context_bundle import _safe_source_parts

    with pytest.raises(ArtifactLifecycleError):
        _safe_source_parts("chatgpt-export/external-source.md")
