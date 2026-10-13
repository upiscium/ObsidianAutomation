"""C2a tests use synthetic GitHub inputs and real isolated POSIX-ACL CAS."""
from dataclasses import replace
import hashlib
import json

import pytest

from obsidian_automation import github_daily_witnessed_entity_grounding as c2
from obsidian_automation import github_daily_witnessed_entity_candidate as c1b
from obsidian_automation import github_daily_route_cas as cas
from test_github_daily_route_cas import safe_layout
from test_github_daily_witnessed_entity_candidate import _fixture, _reply


def encode(obj):
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode()


@pytest.fixture
def inputs(tmp_path, safe_layout):
    _, saved, _, ctx = _fixture(tmp_path)
    partial = _reply([{"summary": "The source discussion changed", "source_refs": [0]},
                      {"summary": "Another source observation", "source_refs": [0]}])
    grounds = c2.build_witnessed_grounding_contexts(
        ctx, partial, capture_sha256=saved.index_sha256,
    )
    return saved, ctx, partial, grounds, safe_layout[0]


def assess(inputs, ground=None, reply=None, partial=None):
    saved, ctx, original_reply, grounds, _ = inputs
    return c2.normalize_witnessed_grounding_verdict(
        grounds[0] if ground is None else ground,
        encode({"verdict": "supported", "reason": "Original discussion supports the claim"})
        if reply is None else reply,
        partial_context=ctx,
        partial_response_bytes=original_reply if partial is None else partial,
        capture_sha256=saved.index_sha256,
    )


def test_complete_cas_chain_and_one_candidate_per_grounding(inputs):
    saved, ctx, partial, grounds, _ = inputs
    assert len(grounds) == 2
    normalized = c1b.normalize_witnessed_entity_candidates(
        ctx, partial, capture_sha256=saved.index_sha256,
    )
    for item, candidate in zip(grounds, normalized.candidates):
        payload = json.loads(item.to_json_bytes())
        assert item.sha256 == hashlib.sha256(item.canonical_bytes).hexdigest()
        assert payload["record_version"] == 3
        assert payload["stage"] == c2.CONTEXT_STAGE
        assert payload["capture_sha256"] == saved.index_sha256
        assert payload["route_receipt_sha256"] == ctx.route_receipt_sha256
        assert payload["evidence_sha256"] == ctx.evidence_sha256
        assert payload["closure_sha256"] == ctx.closure_sha256
        assert payload["partial_context_sha256"] == ctx.sha256
        assert payload["partial_response_sha256"] == hashlib.sha256(partial).hexdigest()
        assert payload["candidate_sha256"] == candidate.candidate_sha256
        assert payload["evidence_ids"] == list(candidate.evidence_ids)
        assert payload["source_events"] == [ctx.events[0].to_json()]
        assert payload["publication_authorized"] is False
        assert payload["requires_human_quality_review"] is True
        assert payload["collection_mode"] == ctx.collection_mode
        assert payload["captured_at_utc"] == ctx.captured_at_utc
    assert grounds == c2.build_witnessed_grounding_contexts(
        ctx, partial, capture_sha256=saved.index_sha256,
    )


@pytest.mark.parametrize("verdict", ["supported", "unsupported"])
def test_verdict_advisory_reason_hash_only_no_false_publication(inputs, verdict):
    reason = "PRIVATE_REASON merged deployed https://example.invalid/forged"
    reply = encode({"verdict": verdict, "reason": reason})
    out = assess(inputs, reply=reply)
    report = out.report()
    assert report["verdict"] == verdict
    assert report["reason_sha256"] == hashlib.sha256(reason.encode()).hexdigest()
    assert report["raw_response_sha256"] == hashlib.sha256(reply).hexdigest()
    assert report["ground_context_sha256"] == inputs[3][0].sha256
    assert "reason" not in report and "summary" not in report and "source_events" not in report
    assert reason.encode() not in out.canonical_bytes
    assert report["publication_authorized"] is False
    assert report["model_verdict_is_advisory"] is True
    assert report["requires_human_quality_review"] is True
    assert out == assess(inputs, reply=reply)


@pytest.mark.parametrize("field,value", [
    ("record_version", 2), ("capture_sha256", "a"*64),
    ("route_receipt_sha256", "a"*64), ("evidence_sha256", "a"*64),
    ("closure_sha256", "a"*64), ("candidate_sha256", "a"*64),
    ("date", "2026-10-13"), ("publication_authorized", True),
    ("entity", {"repository": "upiscium/Test", "entity_type": "pull_request", "number": 999}),
    ("evidence_ids", ["f"*64]), ("source_events", []),
])
def test_rehashed_caller_grounding_changes_cannot_be_admitted(inputs, field, value):
    payload = json.loads(inputs[3][0].canonical_bytes)
    payload[field] = value
    fake = c2.WitnessedGroundContext(encode(payload))
    assert fake.sha256 != inputs[3][0].sha256
    with pytest.raises(c2.DailyWitnessedGroundingError, match="not bound"):
        assess(inputs, ground=fake)


def test_same_candidate_from_changed_response_envelope_is_rejected(inputs):
    with pytest.raises(c2.DailyWitnessedGroundingError, match="not bound"):
        assess(inputs, partial=inputs[2]+b" ")


@pytest.mark.parametrize("reply", [
    b'{"verdict":"supported","reason":"x","publication_authorized":true}',
    b'{"verdict":"supported","verdict":"unsupported","reason":"x"}',
    b'{"verdict":true,"reason":"x"}', b'{"verdict":"merged","reason":"x"}',
    b'{"verdict":"supported","reason":null}',
    b'{"verdict":"supported","reason":""}',
    b'{"verdict":"supported","reason":" x"}',
    b'{"verdict":"supported","reason":"x\\nchanged"}',
    b'{"verdict":"supported","reason":"x\\u2028changed"}',
    b'{"verdict":"supported","reason":"x\\u200bchanged"}',
    b'{"verdict":"supported","reason":"\\ud800"}',
    b'[]', b'{"verdict":', b'',
])
def test_untrusted_grounding_replies_have_typed_rejection(inputs, reply):
    with pytest.raises(c2.DailyWitnessedGroundingError):
        assess(inputs, reply=reply)


def test_reason_and_reply_exact_bounds(inputs):
    assess(inputs, reply=encode({"verdict":"supported", "reason":"x"*2048}))
    with pytest.raises(c2.DailyWitnessedGroundingError):
        assess(inputs, reply=encode({"verdict":"supported", "reason":"x"*2049}))
    with pytest.raises(c2.DailyWitnessedGroundingError):
        assess(inputs, reply=b" "*(c2.MAX_GROUND_REPLY_BYTES+1))


def test_empty_candidates_not_empty_day_and_still_recheck_missing_source(inputs):
    saved, ctx, _, _, root = inputs
    assert c2.build_witnessed_grounding_contexts(ctx, _reply([]), capture_sha256=saved.index_sha256) == ()
    with pytest.raises(c2.DailyWitnessedGroundingError, match="not bound"):
        assess(inputs, partial=_reply([]))
    (root/"receipt"/cas._filename("receipt", saved.receipt_sha256)).unlink()
    with pytest.raises(c2.DailyWitnessedGroundingError):
        c2.build_witnessed_grounding_contexts(ctx, _reply([]), capture_sha256=saved.index_sha256)


def test_context_index_override_never_supplies_grounding_originals(inputs):
    saved, ctx, partial, grounds, _ = inputs
    indexed = []
    class Reindexed(tuple):
        def __getitem__(self, index):
            indexed.append(index)
            return replace(super().__getitem__(index), original={"evidence_id":"f"*64})
    caller = replace(ctx, events=Reindexed(ctx.events))
    assert c2.build_witnessed_grounding_contexts(caller, partial, capture_sha256=saved.index_sha256) == grounds
    assert indexed == []


def test_caller_mutation_during_grounding_reply_decode_cannot_change_result(inputs, monkeypatch):
    _, ctx, _, grounds, _ = inputs
    expected = assess(inputs)
    real_decode = c2._decode_json_object
    def mutate_after_admission(raw, *, label):
        result = real_decode(raw, label=label)
        if label == "Grounding reply":
            ctx.events[0].original["evidence_id"] = "f"*64
            object.__setattr__(ctx.key, "number", 999)
            object.__setattr__(grounds[0], "canonical_bytes", b"forged")
        return result
    monkeypatch.setattr(c2, "_decode_json_object", mutate_after_admission)
    assert assess(inputs) == expected


def test_changed_cas_partition_after_candidate_admission_fails_closed(inputs, monkeypatch):
    saved, ctx, partial, _, _ = inputs
    real_normalize = c2.c1b.normalize_witnessed_entity_candidates
    real_build = c2.c1a.build_witnessed_entity_contexts
    def normalize_then_break(*args, **kwargs):
        batch = real_normalize(*args, **kwargs)
        def empty_rebuild(*a, **kw):
            return replace(real_build(*a, **kw), contexts=())
        monkeypatch.setattr(c2.c1a, "build_witnessed_entity_contexts", empty_rebuild)
        return batch
    monkeypatch.setattr(c2.c1b, "normalize_witnessed_entity_candidates", normalize_then_break)
    with pytest.raises(c2.DailyWitnessedGroundingError):
        c2.build_witnessed_grounding_contexts(ctx, partial, capture_sha256=saved.index_sha256)


def test_no_effect_calls_and_fetched_vs_linked_proofs_preserved(inputs, monkeypatch):
    saved, ctx, partial, _, _ = inputs
    def forbidden(*a, **kw):
        raise AssertionError("Grounding must remain an offline read-only normalizer")
    monkeypatch.setattr("obsidian_automation.artifact_lifecycle._store_immutable", forbidden)
    monkeypatch.setattr("obsidian_automation.github_daily_source_route.collect_fresh_verified_routes", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    out = c2.build_witnessed_grounding_contexts(ctx, partial, capture_sha256=saved.index_sha256)
    assert out and assess(inputs).report()["publication_authorized"] is False
    schema = c2.witnessed_grounding_output_schema()
    assert set(schema["properties"]) == {"verdict", "reason"}
    assert schema["additionalProperties"] is False


def test_all_four_route_proofs_remain_exact_and_original_only_events_stay_null(tmp_path, safe_layout):
    _, saved, contexts, _ = _fixture(tmp_path)
    witnessed = set()
    original_only = 0
    for ctx in contexts.contexts:
        for index, part in enumerate(ctx.events):
            raw = _reply([{"summary":"One source observation", "source_refs":[index]}])
            item, = c2.build_witnessed_grounding_contexts(ctx, raw, capture_sha256=saved.index_sha256)
            payload = json.loads(item.canonical_bytes)
            assert payload["source_events"] == [part.to_json()]
            if part.route_proof is None:
                original_only += 1
                assert payload["source_events"][0]["verified_route"] is None
            else:
                witnessed.add(part.original["kind"])
                proof = payload["source_events"][0]["verified_route"]
                assert proof == part.route_proof.to_json()
                assert "linked_entity_api_path" in proof and "entity_api_path" not in proof
    assert witnessed == {"issue_comment", "pull_request_comment", "pull_request_review_comment", "pull_request_commit"}
    assert original_only > 0


@pytest.mark.parametrize("raw", [None, b"", bytearray(b"{}"), "{}"])
def test_noncanonical_input_envelope_rejected_before_source_read(inputs, raw):
    with pytest.raises(c2.DailyWitnessedGroundingError):
        assess(inputs, ground=c2.WitnessedGroundContext(raw))


def test_returned_report_does_not_share_mutable_output_state(inputs):
    assessment = assess(inputs)
    original = assessment.canonical_bytes
    changed = assessment.report()
    changed["entity"]["number"] = 999
    changed["evidence_ids"].append("f"*64)
    changed["publication_authorized"] = True
    assert assessment.canonical_bytes == original
    assert assessment.report()["publication_authorized"] is False
