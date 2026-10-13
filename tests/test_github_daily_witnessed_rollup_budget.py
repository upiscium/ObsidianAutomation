"""C2b review regressions: bounded replay work and exact reply identities."""
from dataclasses import replace
import hashlib

import pytest

from obsidian_automation import github_daily_witnessed_entity_rollup as roll
from obsidian_automation import github_daily_witnessed_entity_context as c1a
from obsidian_automation import github_daily_witnessed_entity_candidate as c1b
from obsidian_automation import github_daily_witnessed_entity_grounding as c2a
from obsidian_automation import github_daily_route_cas as cas
from test_github_daily_route_cas import safe_layout
from test_github_daily_witnessed_entity_rollup import capture, replies, build, encode


@pytest.mark.parametrize("kind,count", [("partial", 103), ("ground", 171)])
def test_amplified_reply_count_refused_before_any_source_read(monkeypatch, kind, count):
    def forbidden(*args, **kwargs):
        raise AssertionError("count budget must precede CAS reconstruction")
    monkeypatch.setattr(c1a, "build_witnessed_entity_contexts", forbidden)
    partials = tuple(roll.PartialReply(f"{i:064x}", b'{"candidates":[]}')
                     for i in range(count)) if kind == "partial" else ()
    grounds = tuple(roll.GroundReply(f"{i:064x}", encode({"verdict":"supported","reason":"x"}))
                    for i in range(count)) if kind == "ground" else ()
    with pytest.raises(roll.DailyWitnessedRollupError, match="reconstruction-count"):
        roll.build_witnessed_entity_rollup("a"*64, partials, grounds)


def test_source_size_work_refused_before_per_reply_normalization(capture, monkeypatch):
    partials, grounds = replies(capture)
    stored = cas.read_verified_capture(capture[1].index_sha256)
    size = len(stored.evidence_bytes) + len(stored.receipt_bytes) + len(stored.capture_index.canonical_bytes)
    planned = 1 + 5*len(partials) + 3*len(grounds)
    monkeypatch.setattr(roll, "MAX_SOURCE_WORK_BYTES", size*planned - 1)
    rebuild = c1a.build_witnessed_entity_contexts
    calls = []
    def count_build(*args, **kwargs):
        calls.append(1)
        return rebuild(*args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("work quota must precede partial/Grounding normalization")
    monkeypatch.setattr(c1a, "build_witnessed_entity_contexts", count_build)
    monkeypatch.setattr(c1b, "normalize_witnessed_entity_candidates", forbidden)
    monkeypatch.setattr(c2a, "build_witnessed_grounding_contexts", forbidden)
    with pytest.raises(roll.DailyWitnessedRollupError, match="source-work"):
        build(capture, partials, grounds)
    assert len(calls) == 1


def test_exact_work_boundary_and_dependency_multiplier(capture, monkeypatch):
    partials, grounds = replies(capture)
    stored = cas.read_verified_capture(capture[1].index_sha256)
    size = len(stored.evidence_bytes) + len(stored.receipt_bytes) + len(stored.capture_index.canonical_bytes)
    planned = 1 + 5*len(partials) + 3*len(grounds)
    expected = build(capture, partials, grounds)
    monkeypatch.setattr(roll, "MAX_SOURCE_WORK_BYTES", size*planned)
    monkeypatch.setattr(roll, "MAX_SOURCE_RECONSTRUCTIONS", planned)
    rebuild = c1a.build_witnessed_entity_contexts
    calls = []
    def count_build(*args, **kwargs):
        calls.append(1)
        return rebuild(*args, **kwargs)
    monkeypatch.setattr(c1a, "build_witnessed_entity_contexts", count_build)
    assert build(capture, partials, grounds) == expected
    assert len(calls) == planned, "update replay budget if dependency call graph changes"


def test_valid_changed_ground_reply_binds_exact_raw_and_assessment_hash(capture):
    partials, grounds = replies(capture)
    before = build(capture, partials, grounds).report()["entities"][0]["candidates"][0]
    new_raw = encode({"verdict":"supported", "reason":"A different private reason"})
    changed = (replace(grounds[0], response_bytes=new_raw),) + grounds[1:]
    after = build(capture, partials, changed).report()["entities"][0]["candidates"][0]
    assert after["ground_context_sha256"] == before["ground_context_sha256"]
    assert after["candidate_sha256"] == before["candidate_sha256"]
    assert after["ground_response_sha256"] == hashlib.sha256(new_raw).hexdigest()
    assert after["ground_response_sha256"] != before["ground_response_sha256"]
    assert after["ground_assessment_sha256"] != before["ground_assessment_sha256"]
    assert after["reason_sha256"] == hashlib.sha256(b"A different private reason").hexdigest()


def test_reissued_partial_and_new_groundings_bind_both_raw_reply_hashes(capture):
    partials, grounds = replies(capture)
    before = build(capture, partials, grounds).report()["entities"][0]["candidates"][0]
    changed = (replace(partials[0], response_bytes=partials[0].response_bytes+b" \n"),) + partials[1:]
    reissued = []
    at = 0
    for ctx, partial in zip(capture[2].contexts, changed):
        for singleton in c2a.build_witnessed_grounding_contexts(
            ctx, partial.response_bytes, capture_sha256=capture[1].index_sha256,
        ):
            reissued.append(roll.GroundReply(singleton.sha256, grounds[at].response_bytes))
            at += 1
    data = build(capture, changed, tuple(reissued)).report()
    row = data["entities"][0]["candidates"][0]
    assert row["candidate_sha256"] == before["candidate_sha256"]
    assert row["partial_response_sha256"] == hashlib.sha256(changed[0].response_bytes).hexdigest()
    assert row["ground_response_sha256"] == hashlib.sha256(reissued[0].response_bytes).hexdigest()
    assert row["partial_response_sha256"] != before["partial_response_sha256"]
    assert row["ground_context_sha256"] != before["ground_context_sha256"]
    assert row["ground_assessment_sha256"] != before["ground_assessment_sha256"]
    assert data["response_set_complete"] is True
    assert data["publication_authorized"] is False
