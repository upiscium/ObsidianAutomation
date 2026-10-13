"""C2b: real local CAS + synthetic GitHub, no inference or production effects."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import hashlib
import json

import pytest

from obsidian_automation import github_daily_witnessed_entity_rollup as roll
from obsidian_automation import github_daily_witnessed_entity_context as c1a
from obsidian_automation import github_daily_witnessed_entity_grounding as c2a
from obsidian_automation import github_daily_route_cas as cas
from obsidian_automation import github_daily_source_route as source
from test_github_daily_route_cas import safe_layout
from test_github_daily_witnessed_entity_candidate import _fixture, _reply
from test_github_daily_source_route import _FakeClient, _config, DATE, PR_COMMIT


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode()


@pytest.fixture
def capture(tmp_path, safe_layout):
    original, saved, contexts, _ = _fixture(tmp_path)
    return original, saved, contexts, safe_layout


def replies(capture, budget=c1a.MAX_CONTEXT_BYTES):
    _, saved, contexts, _ = capture
    if budget != c1a.MAX_CONTEXT_BYTES:
        contexts = c1a.build_witnessed_entity_contexts(saved.index_sha256, max_bytes=budget)
    partials, grounds = [], []
    for ctx in contexts.contexts:
        raw = _reply([{"summary": "Supported observation", "source_refs": [0]},
                      {"summary": "Unsupported observation", "source_refs": [0]},
                      {"summary": "Another observation", "source_refs": [0]}])
        partials.append(roll.PartialReply(ctx.sha256, raw))
        singletons = c2a.build_witnessed_grounding_contexts(
            ctx, raw, capture_sha256=saved.index_sha256,
        )
        for i, item in enumerate(singletons):
            grounds.append(roll.GroundReply(item.sha256, encode({
                "verdict": "unsupported" if i == 1 else "supported",
                "reason": "PRIVATE_REASON not for rollup text",
            })))
    return tuple(partials), tuple(grounds)


def build(capture, partials=(), grounds=(), **kwargs):
    return roll.build_witnessed_entity_rollup(
        capture[1].index_sha256, partials, grounds, **kwargs,
    )


def assert_lossless(capture, report):
    original, _, contexts, _ = capture
    covered = set(report["unattributed_default_branch_commit_ids"])
    for entity in report["entities"]:
        ids = set(entity["source_evidence_ids"])
        assert not covered & ids
        covered |= ids
        categories = [set(entity[key]) for key in (
            "supported_evidence_ids", "unsupported_evidence_ids",
            "unresolved_evidence_ids", "uncited_evidence_ids", "unprocessed_evidence_ids",
        )]
        assert set.union(*categories) == ids
        assert not categories[3] & categories[4]
        assert not set.union(*categories[:3]) & (categories[3] | categories[4])
    assert covered == {event.evidence_id for event in original.evidence.events}
    assert report["original_event_count"] == len(covered)
    assert report["grouped_event_count"] == contexts.grouped_event_count
    assert report["publication_authorized"] is False
    assert report["requires_human_quality_review"] is True
    assert report["semantic_coverage_review_required"] is True
    assert report["model_verdict_is_advisory"] is True


def test_one_row_per_actual_issue_pr_and_exact_source_chain(capture):
    partials, grounds = replies(capture)
    out = build(capture, partials, grounds)
    data = out.report()
    assert_lossless(capture, data)
    assert data["response_set_complete"] is True
    assert data["entity_count"] == 2
    assert [row["entity"] for row in data["entities"]] == [
        {"repository": "upiscium/Test", "entity_type": "issue", "number": 1},
        {"repository": "upiscium/Test", "entity_type": "pull_request", "number": 2},
    ]
    for name in ("capture_sha256", "route_receipt_sha256", "evidence_sha256", "closure_sha256"):
        assert data[name] == getattr(capture[2], name)
    assert data["date"] == "2026-10-05"
    assert data["collection_mode"] == "explicit_historical_reconfirmation"
    assert data["candidate_counts"] == {"supported": 4, "unsupported": 2, "unresolved": 0}
    assert out.sha256 == hashlib.sha256(out.canonical_bytes).hexdigest()
    assert out.to_json_bytes().endswith(b"\n")
    assert b"PRIVATE_REASON" not in out.canonical_bytes
    assert b'"original_event":' not in out.canonical_bytes
    assert b'"actor"' not in out.canonical_bytes
    for row in data["entities"]:
        for candidate in row["candidates"]:
            assert candidate["ground_assessment_sha256"]
            assert candidate["reason_sha256"]
            assert candidate["partial_context_sha256"] in {p.context_sha256 for p in partials}


def test_input_order_independent_and_result_report_copies(capture):
    partials, grounds = replies(capture)
    expected = build(capture, partials, grounds)
    assert expected == build(capture, tuple(reversed(partials)), tuple(reversed(grounds)))
    changed = expected.report()
    changed["publication_authorized"] = True
    changed["entities"][0]["candidates"].clear()
    assert expected.report()["publication_authorized"] is False
    assert expected.report()["entities"][0]["candidates"]


def test_missing_partials_are_unprocessed_not_an_empty_day(capture):
    data = build(capture).report()
    assert_lossless(capture, data)
    assert data["response_set_complete"] is False
    assert data["received_partial_count"] == 0
    assert data["expected_context_count"] > 0
    for row in data["entities"]:
        assert not row["candidates"]
        assert row["unprocessed_evidence_ids"] == row["source_evidence_ids"]
        assert row["missing_partial_context_sha256s"]
        assert not row["response_set_complete"]


def test_empty_partials_keep_all_uncited_evidence(capture):
    partials = tuple(roll.PartialReply(ctx.sha256, _reply([])) for ctx in capture[2].contexts)
    data = build(capture, partials).report()
    assert_lossless(capture, data)
    assert data["response_set_complete"] is True  # Transport census ONLY.
    assert data["candidate_counts"] == dict(supported=0, unsupported=0, unresolved=0)
    assert all(row["uncited_evidence_ids"] == row["source_evidence_ids"] for row in data["entities"])
    assert "no_progress" not in data


def test_missing_grounding_and_unsupported_evidence_remain_visible(capture):
    partials, grounds = replies(capture)
    data = build(capture, partials, grounds[:-1]).report()
    assert_lossless(capture, data)
    assert data["response_set_complete"] is False
    assert data["candidate_counts"] == dict(supported=3, unsupported=2, unresolved=1)
    pr = data["entities"][1]
    assert pr["supported_evidence_ids"]
    assert pr["unsupported_evidence_ids"]
    assert pr["unresolved_evidence_ids"]
    # Verdict-specific coverage may overlap; unresolved candidates are not hidden
    # merely because some other candidate citing that same event was supported.
    assert set(pr["supported_evidence_ids"]) & set(pr["unresolved_evidence_ids"])
    unresolved = next(c for c in pr["candidates"] if c["verdict"] == "unresolved")
    assert unresolved["ground_response_sha256"] is None
    assert unresolved["ground_assessment_sha256"] is None


def test_one_missing_partial_retains_whole_entity(capture):
    partials, _ = replies(capture)
    data = build(capture, partials[:1]).report()
    assert_lossless(capture, data)
    assert data["entity_count"] == 2
    assert data["entities"][1]["unprocessed_evidence_ids"]


@pytest.mark.parametrize("kind", ["partial", "ground"])
def test_duplicate_reply_identity_is_rejected_even_when_bytes_equal(capture, kind):
    partials, grounds = replies(capture)
    if kind == "partial": partials += partials[:1]
    else: grounds += grounds[:1]
    with pytest.raises(roll.DailyWitnessedRollupError, match="duplicate"):
        build(capture, partials, grounds)


@pytest.mark.parametrize("kind", ["partial", "ground"])
def test_foreign_reply_context_cannot_select_another_entity(capture, kind):
    partials, grounds = replies(capture)
    if kind == "partial": partials = (replace(partials[0], context_sha256="f"*64),) + partials[1:]
    else: grounds = (replace(grounds[0], ground_context_sha256="f"*64),) + grounds[1:]
    with pytest.raises(roll.DailyWitnessedRollupError, match="foreign"):
        build(capture, partials, grounds)


def test_raw_partial_change_invalidates_old_grounding_even_with_same_candidate(capture):
    partials, grounds = replies(capture)
    altered = (replace(partials[0], response_bytes=partials[0].response_bytes + b" "),) + partials[1:]
    with pytest.raises(roll.DailyWitnessedRollupError, match="foreign or stale"):
        build(capture, altered, grounds)


@pytest.mark.parametrize("raw", [b'{', b'{"candidates":{},"publication_authorized":true}',
    b'{"candidates":[{"summary":"ok","source_refs":[true]}]}'])
def test_bad_partial_is_error_not_successful_empty(capture, raw):
    ctx = capture[2].contexts[0]
    with pytest.raises(roll.DailyWitnessedRollupError):
        build(capture, (roll.PartialReply(ctx.sha256, raw),))


@pytest.mark.parametrize("raw", [b'{', b'{"verdict":"supported","reason":""}',
    b'{"verdict":"supported","reason":"x","publication_authorized":true}',
    b'{"verdict":"supported","verdict":"unsupported","reason":"x"}'])
def test_bad_grounding_cannot_be_admitted_as_supported(capture, raw):
    partials, grounds = replies(capture)
    altered = (replace(grounds[0], response_bytes=raw),) + grounds[1:]
    with pytest.raises(roll.DailyWitnessedRollupError): build(capture, partials, altered)


@pytest.mark.parametrize("invalid", [None, True, "a"*63, "A"*64, "../"+"a"*64])
def test_bad_capture_identity_is_typed(capture, invalid):
    with pytest.raises(roll.DailyWitnessedRollupError):
        roll.build_witnessed_entity_rollup(invalid, (), ())


@pytest.mark.parametrize("kind", ["tuple_subclass", "record_subclass", "bytes_subclass", "assessment"])
def test_in_memory_objects_cannot_replace_raw_reply_contract(capture, kind):
    ctx = capture[2].contexts[0]
    partial = roll.PartialReply(ctx.sha256, _reply([]))
    if kind == "tuple_subclass":
        class X(tuple): pass
        records = X((partial,))
    elif kind == "record_subclass":
        class X(roll.PartialReply): pass
        records = (X(ctx.sha256, _reply([])),)
    elif kind == "bytes_subclass":
        class X(bytes): pass
        records = (replace(partial, response_bytes=X(_reply([]))),)
    else:
        records = (c2a.WitnessedGroundAssessment(b'{"verdict":"supported"}'),)
    with pytest.raises(roll.DailyWitnessedRollupError): build(capture, records)


def test_missing_cas_and_wrong_role_never_become_empty(capture, monkeypatch):
    _, saved, _, (root, roles) = capture
    monkeypatch.setattr(cas, "_roles", lambda: replace(roles, reader_uid=roles.reader_uid+10))
    with pytest.raises(roll.DailyWitnessedRollupError): build(capture)
    monkeypatch.setattr(cas, "_roles", lambda: roles)
    (root/'receipt'/cas._filename('receipt', saved.receipt_sha256)).unlink()
    with pytest.raises(roll.DailyWitnessedRollupError): build(capture)


def test_reply_records_are_snapshotted_before_cas_access(capture, monkeypatch):
    partials, grounds = replies(capture)
    expected = build(capture, partials, grounds)
    original = c1a.build_witnessed_entity_contexts
    def mutate_caller(*args, **kwargs):
        object.__setattr__(partials[0], "response_bytes", b'{}')
        object.__setattr__(grounds[0], "ground_context_sha256", "f"*64)
        return original(*args, **kwargs)
    monkeypatch.setattr(c1a, "build_witnessed_entity_contexts", mutate_caller)
    assert build(capture, partials, grounds) == expected


def test_byte_and_count_budgets_reject_without_truncation(capture, monkeypatch):
    partials, grounds = replies(capture)
    monkeypatch.setattr(roll, "MAX_INPUT_BYTES", 2)
    with pytest.raises(roll.DailyWitnessedRollupError, match="byte budget"):
        build(capture, partials, grounds)
    monkeypatch.setattr(roll, "MAX_INPUT_BYTES", 32*1024*1024)
    monkeypatch.setattr(roll, "MAX_PARTIAL_REPLIES", 0)
    with pytest.raises(roll.DailyWitnessedRollupError, match="bounded"):
        build(capture, partials, grounds)
    monkeypatch.setattr(roll, "MAX_PARTIAL_REPLIES", 4096)
    monkeypatch.setattr(roll, "MAX_ROLLUP_BYTES", 128)
    with pytest.raises(roll.DailyWitnessedRollupError): build(capture, partials, grounds)


def test_archived_read_keeps_original_day_and_never_calls_writer_or_network(capture, monkeypatch):
    partials, grounds = replies(capture)
    def forbidden(*args, **kwargs): raise AssertionError("unexpected effect")
    monkeypatch.setattr(source, "collect_fresh_verified_routes", forbidden)
    monkeypatch.setattr("obsidian_automation.artifact_lifecycle._store_immutable", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr(cas, "_utc_now", forbidden)
    data = build(capture, partials, grounds).report()
    assert data["date"] == "2026-10-05"
    assert data["publication_authorized"] is False


@pytest.mark.parametrize("with_commit", [False, True])
def test_zero_entity_capture_does_not_erase_default_commits_or_authorize_inactivity(
    tmp_path, safe_layout, with_commit,
):
    class DefaultOnly(_FakeClient):
        def _paged(self, path):
            if "/commits?" in path and with_commit:
                row = deepcopy(PR_COMMIT)
                row["sha"] = "b"*40
                row["html_url"] = "https://github.com/upiscium/Test/commit/"+"b"*40
                return [row]
            return []
    collected = source.collect_fresh_verified_routes(
        _config(tmp_path), target_date=DATE, client=DefaultOnly(),
        explicit_historical_reconfirmation=True,
    )
    saved = cas.publish_fresh_capture(collected)
    data = roll.build_witnessed_entity_rollup(saved.index_sha256, (), ()).report()
    assert data["entities"] == []
    assert data["original_event_count"] == (1 if with_commit else 0)
    assert data["unattributed_default_branch_commit_ids"] == [e.evidence_id for e in collected.evidence.events]
    assert data["response_set_complete"] is True
    assert data["semantic_coverage_review_required"] is True
    assert data["publication_authorized"] is False


@pytest.mark.parametrize("budget", [None, True, 1, 1023, 65537, "4096"])
def test_context_budget_requires_exact_bounded_integer(capture, budget):
    with pytest.raises(roll.DailyWitnessedRollupError):
        build(capture, max_context_bytes=budget)


def test_foreign_capture_same_original_evidence_cannot_reuse_partial_hashes(
    capture, tmp_path, monkeypatch,
):
    partials, grounds = replies(capture)
    old = capture[0]
    captured = datetime.fromisoformat(json.loads(old.receipt_bytes)["captured_at_utc"].replace("Z", "+00:00"))
    monkeypatch.setattr(source, "_utc_now", lambda: captured + timedelta(seconds=2))
    newer = source.collect_fresh_verified_routes(
        _config(tmp_path), target_date=DATE,
        client=_FakeClient(visible_pr=True, include_commit=True, include_review_comment=True),
        explicit_historical_reconfirmation=True,
    )
    saved = cas.publish_fresh_capture(newer)
    assert old.evidence.sha256 == newer.evidence.sha256
    assert saved.index_sha256 != capture[1].index_sha256
    with pytest.raises(roll.DailyWitnessedRollupError, match="foreign"):
        roll.build_witnessed_entity_rollup(saved.index_sha256, partials, grounds)


def test_multiple_batches_still_one_entity_and_lossless(capture):
    partials, grounds = replies(capture, budget=4000)
    data = build(capture, partials, grounds, max_context_bytes=4000).report()
    assert_lossless(capture, data)
    assert data["entity_count"] == 2
    assert data["expected_context_count"] > 2
    assert all(row["response_set_complete"] for row in data["entities"])


def test_normalized_assessment_object_is_not_accepted_as_raw_grounding(capture):
    partials, grounds = replies(capture)
    forged = c2a.WitnessedGroundAssessment(encode({"verdict":"supported", "publication_authorized":True}))
    with pytest.raises(roll.DailyWitnessedRollupError, match="record type"):
        build(capture, partials, (forged,))
