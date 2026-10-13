"""C1b: v3 model replies remain source-bound and never grant publication."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from obsidian_automation import github_daily_witnessed_entity_context as c1a
from obsidian_automation import github_daily_witnessed_entity_candidate as c1b
from obsidian_automation import github_daily_route_cas as cas
from obsidian_automation.github_daily_issue_pr import EntityKey

from test_github_daily_route_cas import safe_layout
from test_github_daily_witnessed_entity_context import _publish


def _fixture(tmp_path):
    collected, saved = _publish(tmp_path)
    contexts = c1a.build_witnessed_entity_contexts(saved.index_sha256)
    assert contexts.contexts
    return collected, saved, contexts, contexts.contexts[0]


def _reply(candidates):
    return (
        json.dumps({"candidates": candidates}, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")) + "\n"
    ).encode("utf-8")


def test_candidate_schema_is_one_entity_and_has_no_identity_or_effect_choice(
    tmp_path, safe_layout
):
    _, saved, _, ctx = _fixture(tmp_path)
    schema = c1b.witnessed_model_candidate_schema(
        ctx, capture_sha256=saved.index_sha256,
    )
    assert set(schema["properties"]) == {"candidates"}
    assert set(schema["properties"]["candidates"]["items"]["properties"]) == {
        "summary", "source_refs",
    }
    assert schema["additionalProperties"] is False
    assert schema["properties"]["candidates"]["items"]["additionalProperties"] is False
    assert schema["properties"]["candidates"]["items"]["properties"]["source_refs"]["items"]["enum"] == list(range(len(ctx.events)))
    assert all(term not in json.dumps(schema) for term in (
        "repository", "number", "url", "publication_authorized", "merged", "deployed",
    ))


def test_c1b_accepts_valid_one_entity_candidate_and_preserves_all_source_digests(
    tmp_path, safe_layout
):
    _, saved, sources, ctx = _fixture(tmp_path)
    model = _reply([{"summary": "Verified progress was discussed", "source_refs": [0]}])
    a = c1b.normalize_witnessed_entity_candidates(
        ctx, model, capture_sha256=saved.index_sha256,
    )
    b = c1b.normalize_witnessed_entity_candidates(
        ctx, model, capture_sha256=saved.index_sha256,
    )
    assert a == b
    assert a.capture_sha256 == saved.index_sha256
    assert a.route_receipt_sha256 == sources.route_receipt_sha256
    assert a.evidence_sha256 == sources.evidence_sha256
    assert a.closure_sha256 == sources.closure_sha256
    assert a.context_sha256 == ctx.sha256
    assert a.response_sha256 == hashlib.sha256(model).hexdigest()
    assert (a.repository, a.entity_type, a.number) == (
        ctx.key.repository, ctx.key.entity_type, ctx.key.number,
    )
    assert a.candidates[0].evidence_ids == (ctx.evidence_ids[0],)
    assert len(a.candidates[0].candidate_sha256) == 64
    assert a.uncited_evidence_ids == ctx.evidence_ids[1:]
    report = a.report()
    assert report["publication_authorized"] is False
    assert report["grounding_required"] is True
    assert report["semantic_coverage_review_required"] is True
    assert report["requires_human_quality_review"] is True
    assert report["record_version"] == 3
    assert report["stage"] == c1a.CANDIDATE_STAGE
    assert report["closure_sha256"] == sources.closure_sha256
    assert a.to_json_bytes().endswith(b"\n")


def test_empty_model_candidates_never_certify_empty_progress(tmp_path, safe_layout):
    _, saved, _, ctx = _fixture(tmp_path)
    batch = c1b.normalize_witnessed_entity_candidates(
        ctx, _reply([]), capture_sha256=saved.index_sha256,
    )
    assert not batch.candidates
    assert batch.uncited_evidence_ids == ctx.evidence_ids
    assert batch.report()["semantic_coverage_review_required"] is True
    assert batch.report()["publication_authorized"] is False


def test_model_candidate_exact_CAS_recheck_rejects_forged_contexts(
    tmp_path, safe_layout
):
    _, saved, _, ctx = _fixture(tmp_path)
    first = ctx.events[0]
    forged_event = replace(first, original=dict(first.original, actor="evil"))
    changed = replace(ctx, events=(forged_event,) + ctx.events[1:])
    for invalid in (
        changed,
        replace(ctx, capture_sha256="a"*64),
        replace(ctx, route_receipt_sha256="a"*64),
        replace(ctx, evidence_sha256="a"*64),
        replace(ctx, closure_sha256="a"*64),
        replace(ctx, key=EntityKey(ctx.key.repository, "pull_request"
                                  if ctx.key.entity_type=="issue" else "issue", ctx.key.number)),
        replace(ctx, batch_count=99),
        replace(ctx, batch_index=100),
    ):
        with pytest.raises(c1b.DailyWitnessedCandidateError):
            c1b.witnessed_model_candidate_schema(
                invalid, capture_sha256=saved.index_sha256,
            )
        with pytest.raises(c1b.DailyWitnessedCandidateError):
            c1b.normalize_witnessed_entity_candidates(
                invalid, _reply([]), capture_sha256=saved.index_sha256,
            )


def test_swapped_source_proof_or_context_type_cannot_be_trusted(
    tmp_path, safe_layout
):
    _, saved, _, ctx = _fixture(tmp_path)
    proof = ctx.events[0].route_proof
    if proof is not None:
        fake = replace(proof, entity_type="issue" if proof.entity_type == "pull_request" else "pull_request")
        altered = replace(ctx, events=(replace(ctx.events[0], route_proof=fake),) + ctx.events[1:])
        with pytest.raises(c1b.DailyWitnessedCandidateError):
            c1b.witnessed_model_candidate_schema(
                altered, capture_sha256=saved.index_sha256,
            )
    for foreign in (None, True, dict(capture_sha256=saved.index_sha256), ctx.sha256):
        with pytest.raises(c1b.DailyWitnessedCandidateError):
            c1b.normalize_witnessed_entity_candidates(
                foreign, _reply([]), capture_sha256=saved.index_sha256,
            )


@pytest.mark.parametrize("bad_digest", [
    None, True, "A"*64, "a"*63, "b"*64, "../"+"a"*64,
])
def test_wrong_capture_is_never_a_source_authority(
    tmp_path, safe_layout, bad_digest
):
    _, saved, _, ctx = _fixture(tmp_path)
    with pytest.raises(c1b.DailyWitnessedCandidateError):
        c1b.normalize_witnessed_entity_candidates(
            ctx, _reply([]), capture_sha256=bad_digest,
        )


@pytest.mark.parametrize("model", [
    b'{"candidates":[{"summary":"ok","source_refs":[true]}]}',
    b'{"candidates":[{"summary":"ok","source_refs":[-1]}]}',
    b'{"candidates":[{"summary":"ok","source_refs":[999]}]}',
    b'{"candidates":[{"summary":"ok","source_refs":[0,0]}]}',
    b'{"candidates":[{"summary":"ok","source_refs":[]}]}',
    b'{"candidates":[{"summary":"ok","source_refs":[0],"url":"https://evil"}]}',
    b'{"candidates":[{"summary":"ok","source_refs":[0]}],"publication_authorized":true}',
    b'{"candidates":[{"summary":"ok","summary":"changed","source_refs":[0]}]}',
    b'{"candidates":[{"summary":"ok\\nmerged","source_refs":[0]}]}',
    b'{"candidates":[{"summary":"ok\\u2028merged","source_refs":[0]}]}',
    b'{"candidates":[{"summary":"ok","source_refs":[0]},{"summary":"ok","source_refs":[0]}]}',
    b'{"candidates":{}}',
    b'{"candidates":',
])
def test_model_cannot_inject_fields_refs_duplicates_or_multiline(
    tmp_path, safe_layout, model
):
    _, saved, _, ctx = _fixture(tmp_path)
    with pytest.raises(c1b.DailyWitnessedCandidateError):
        c1b.normalize_witnessed_entity_candidates(
            ctx, model, capture_sha256=saved.index_sha256,
        )


def test_candidate_response_bound_and_exact_ints(tmp_path, safe_layout):
    _, saved, _, ctx = _fixture(tmp_path)
    with pytest.raises(c1b.DailyWitnessedCandidateError):
        c1b.normalize_witnessed_entity_candidates(
            ctx, _reply([]) + b" " * c1b.MAX_MODEL_REPLY_BYTES,
            capture_sha256=saved.index_sha256,
        )
    with pytest.raises(c1b.DailyWitnessedCandidateError):
        c1b.normalize_witnessed_entity_candidates(
            ctx, None, capture_sha256=saved.index_sha256,
        )


def test_context_byte_budget_altered_at_model_boundary_rejected(
    tmp_path, safe_layout
):
    _, saved, _, ctx = _fixture(tmp_path)
    with pytest.raises(c1b.DailyWitnessedCandidateError):
        c1b.normalize_witnessed_entity_candidates(
            replace(ctx, max_context_bytes=2048),
            _reply([]), capture_sha256=saved.index_sha256,
        )


def test_read_only_model_normalization_uses_no_writer_or_network(
    tmp_path, safe_layout, monkeypatch
):
    _, saved, _, ctx = _fixture(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("C1b must not trigger effects")
    monkeypatch.setattr(
        "obsidian_automation.github_daily_source_route.collect_fresh_verified_routes",
        forbidden,
    )
    monkeypatch.setattr(
        "obsidian_automation.github_daily_activity.collect_daily_evidence",
        forbidden,
    )
    monkeypatch.setattr(
        "obsidian_automation.artifact_lifecycle._store_immutable",
        forbidden,
    )
    batch = c1b.normalize_witnessed_entity_candidates(
        ctx, _reply([]), capture_sha256=saved.index_sha256,
    )
    assert batch.report()["publication_authorized"] is False


def test_explicit_historical_capture_stays_historical_even_if_model_claims_merge(
    tmp_path, safe_layout
):
    _, saved, _, ctx = _fixture(tmp_path)
    assert ctx.collection_mode == "explicit_historical_reconfirmation"
    model = _reply([{"summary": "Merge was approved", "source_refs": [0]}])
    batch = c1b.normalize_witnessed_entity_candidates(
        ctx, model, capture_sha256=saved.index_sha256,
    )
    assert batch.report()["publication_authorized"] is False
    assert batch.report()["grounding_required"] is True
    # A syntactically valid model claim is advisory and cannot be interpreted
    # as a verified PR merge or a Daily Writer effect.
