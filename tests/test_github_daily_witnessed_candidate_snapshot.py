"""PR #351 regressions: consume only CAS-rebuilt owned source data."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from obsidian_automation import github_daily_witnessed_entity_context as c1a
from obsidian_automation import github_daily_witnessed_entity_candidate as c1b
from test_github_daily_route_cas import safe_layout
from test_github_daily_witnessed_entity_candidate import _fixture, _reply


def test_tuple_index_override_cannot_supply_non_cas_evidence(tmp_path, safe_layout):
    _, saved, _, ctx = _fixture(tmp_path)
    expected_ids = ctx.evidence_ids
    indexed = []

    class ReindexedEvents(tuple):
        def __getitem__(self, index):
            item = super().__getitem__(index)
            if type(index) is int:
                indexed.append(index)
                return replace(item, original=dict(item.original, evidence_id="f" * 64))
            return item

    supplied = replace(ctx, events=ReindexedEvents(ctx.events))
    # Iteration, equality, and canonical serialization are genuine. A verifier
    # returning a hash alone cannot make subsequent virtual indexing safe.
    assert c1a.verify_witnessed_entity_context(
        supplied, capture_sha256=saved.index_sha256,
    ) == ctx.sha256
    result = c1b.normalize_witnessed_entity_candidates(
        supplied, _reply([{"summary": "Source discussion", "source_refs": [0]}]),
        capture_sha256=saved.index_sha256,
    )
    assert not indexed, "candidate admission indexed caller-controlled events"
    assert result.candidates[0].evidence_ids == expected_ids[:1]
    assert result.uncited_evidence_ids == expected_ids[1:]


@pytest.mark.parametrize("mutation", ["event_id", "entity_key"])
def test_caller_mutation_during_reply_decode_cannot_change_result(
    tmp_path, safe_layout, monkeypatch, mutation,
):
    _, saved, _, ctx = _fixture(tmp_path)
    expected_ids = ctx.evidence_ids
    expected_key = (ctx.key.repository, ctx.key.entity_type, ctx.key.number)
    expected_sha = ctx.sha256
    decode = c1b._decode_json_object

    def decode_and_mutate(*args, **kwargs):
        reply = decode(*args, **kwargs)
        if mutation == "event_id":
            ctx.events[0].original["evidence_id"] = "f" * 64
        else:
            object.__setattr__(ctx.key, "number", 999999)
        return reply

    monkeypatch.setattr(c1b, "_decode_json_object", decode_and_mutate)
    result = c1b.normalize_witnessed_entity_candidates(
        ctx, _reply([{"summary": "Source discussion", "source_refs": [0]}]),
        capture_sha256=saved.index_sha256,
    )
    assert result.candidates[0].evidence_ids == expected_ids[:1]
    assert result.uncited_evidence_ids == expected_ids[1:]
    assert (result.repository, result.entity_type, result.number) == expected_key
    assert result.context_sha256 == expected_sha


def test_schema_count_uses_rebuilt_sequence_after_verification(
    tmp_path, safe_layout, monkeypatch,
):
    _, saved, _, ctx = _fixture(tmp_path)
    expected_count = len(ctx.events)
    state = {"changed": False}

    class ChangingLength(tuple):
        def __len__(self):
            return super().__len__() + (5 if state["changed"] else 0)

    supplied = replace(ctx, events=ChangingLength(ctx.events))
    verify = c1a.verify_witnessed_entity_context

    def verify_then_change(*args, **kwargs):
        result = verify(*args, **kwargs)
        state["changed"] = True
        return result

    monkeypatch.setattr(c1a, "verify_witnessed_entity_context", verify_then_change)
    schema = c1b.witnessed_model_candidate_schema(
        supplied, capture_sha256=saved.index_sha256,
    )
    refs = schema["properties"]["candidates"]["items"]["properties"]["source_refs"]
    assert refs["items"]["enum"] == list(range(expected_count))
    assert refs["maxItems"] == min(c1b.MAX_REFS_PER_CANDIDATE, expected_count)


@pytest.mark.parametrize("failure", ["missing", "changed", "count"])
def test_second_trusted_reconstruction_must_succeed_and_match(
    tmp_path, safe_layout, monkeypatch, failure,
):
    _, saved, _, ctx = _fixture(tmp_path)
    build = c1a.build_witnessed_entity_contexts
    calls = []

    def changing_source(*args, **kwargs):
        calls.append(1)
        result = build(*args, **kwargs)
        if len(calls) > 1:
            if failure == "missing":
                raise c1a.DailyWitnessedContextError("source unavailable")
            if failure == "count":
                return replace(result, contexts=())
            changed = replace(result.contexts[ctx.batch_index], evidence_sha256="d" * 64)
            items = list(result.contexts)
            items[ctx.batch_index] = changed
            return replace(result, contexts=tuple(items))
        return result

    monkeypatch.setattr(c1a, "build_witnessed_entity_contexts", changing_source)
    with pytest.raises(c1b.DailyWitnessedCandidateError):
        c1b.normalize_witnessed_entity_candidates(
            ctx, _reply([]), capture_sha256=saved.index_sha256,
        )
    assert len(calls) == 2


@pytest.mark.parametrize("field,value", [
    ("batch_index", None), ("batch_index", True), ("batch_index", -1),
    ("batch_count", "2"), ("batch_count", 0),
    ("max_context_bytes", None), ("max_context_bytes", True),
])
def test_bad_selection_fields_have_typed_errors(tmp_path, safe_layout, field, value):
    _, saved, _, ctx = _fixture(tmp_path)
    supplied = replace(ctx, **{field: value})
    with pytest.raises(c1b.DailyWitnessedCandidateError):
        c1b.witnessed_model_candidate_schema(supplied, capture_sha256=saved.index_sha256)
    with pytest.raises(c1b.DailyWitnessedCandidateError):
        c1b.normalize_witnessed_entity_candidates(
            supplied, _reply([]), capture_sha256=saved.index_sha256,
        )


def test_candidate_digest_matches_independently_encoded_complete_source_chain(
    tmp_path, safe_layout,
):
    _, saved, _, ctx = _fixture(tmp_path)
    summary = "Source discussion"
    result = c1b.normalize_witnessed_entity_candidates(
        ctx, _reply([{"summary": summary, "source_refs": [0]}]),
        capture_sha256=saved.index_sha256,
    )
    expected = {
        "record_version": 3,
        "capture_sha256": saved.index_sha256,
        "route_receipt_sha256": ctx.route_receipt_sha256,
        "evidence_sha256": ctx.evidence_sha256,
        "closure_sha256": ctx.closure_sha256,
        "context_sha256": ctx.sha256,
        "entity": {"repository": ctx.key.repository, "entity_type": ctx.key.entity_type,
                   "number": ctx.key.number},
        "summary": summary,
        "evidence_ids": [ctx.evidence_ids[0]],
    }
    raw = (json.dumps(expected, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")) + "\n").encode("utf-8")
    assert result.candidates[0].candidate_sha256 == hashlib.sha256(raw).hexdigest()
    report = json.loads(result.to_json_bytes())
    assert report["publication_authorized"] is False
    assert report["requires_human_quality_review"] is True
    assert report["grounding_required"] is True
