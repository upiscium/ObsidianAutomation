"""No-effects regression suite for singleton entity Grounding (#330)."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

from obsidian_automation.artifact_lifecycle import _canonical_json_bytes
from obsidian_automation.github_daily_activity import (
    ProjectBinding, _bounded_text, _date_window, _make_event,
    make_daily_evidence_bundle,
)
from obsidian_automation.github_daily_entity_context import (
    DailyEntityContextError, build_entity_contexts,
)
from obsidian_automation.github_daily_entity_grounding import (
    GROUND_CONTEXT_STAGE, GROUND_RESULT_STAGE, MAX_GROUND_OUTPUT_BYTES,
    DailyEntityGroundingError,
    build_entity_grounding_contexts, entity_grounding_output_schema,
    normalize_entity_grounding_verdict,
)


DAY = date(2026, 10, 12)
REPO = "upiscium/Test"


def _event(
    kind: str, number: int, *, minutes: int = 5,
    summary: str = "meaningful code or design change",
):
    start, _ = _date_window(DAY)
    pr = kind.startswith("pull_request")
    entity = "pull_request_review_comment" if kind == "pull_request_review_comment" else (
        "pull_request" if pr else "issue"
    )
    url = f"https://github.com/{REPO}/{'pull' if pr else 'issues'}/{number}"
    text = _bounded_text(summary, limit=8192, label="body")
    return _make_event(
        kind=kind, repository=REPO,
        occurred_at=start + timedelta(minutes=minutes),
        url=url, actor="human", entity_type=entity, number=number,
        source_id=f"{kind}/{number}/{minutes}",
        body=text, title=_bounded_text(f"Test {number}", limit=2048, label="title"),
    )


def _original(*events) -> bytes:
    return make_daily_evidence_bundle(
        target_date=DAY,
        projects=[ProjectBinding("10-Project/Test/Project.md", REPO)],
        events=list(events),
    ).canonical_bytes


def _candidate(*records) -> bytes:
    return _canonical_json_bytes({"candidates": list(records)})


def _model(verdict="supported", reason="元のGitHub証拠が直接支持しています。", **extra):
    return _canonical_json_bytes({"verdict": verdict, "reason": reason, **extra})


def _case(*events, records=None):
    raw = _original(*events)
    context = build_entity_contexts(raw).contexts[0]
    response = _candidate(*(
        records if records is not None
        else [{"summary": "その日の変更を実装しました。", "source_refs": [0]}]
    ))
    singleton = build_entity_grounding_contexts(
        context, response, original_evidence_bytes=raw,
    )
    return raw, context, response, singleton


def _normalize(ground, model_bytes, original, partial, partial_bytes):
    return normalize_entity_grounding_verdict(
        ground, model_bytes,
        partial_context=partial, partial_model_bytes=partial_bytes,
        original_evidence_bytes=original,
    )


def test_issue_and_pr_same_number_are_different_grounding_entities():
    raw = _original(
        _event("issue_created", 7, minutes=1),
        _event("pull_request_created", 7, minutes=2),
    )
    parts = build_entity_contexts(raw).contexts
    assert len(parts) == 2
    rows = []
    for part in parts:
        response = _candidate({
            "summary": "当日に進展がありました。", "source_refs": [0],
        })
        contexts = build_entity_grounding_contexts(
            part, response, original_evidence_bytes=raw,
        )
        assert len(contexts) == 1
        rows.append(_normalize(contexts[0], _model(), raw, part, response))
    assert {row.key.entity_type for row in rows} == {"issue", "pull_request"}
    assert len({row.candidate_sha256 for row in rows}) == 2
    assert len({row.ground_context_sha256 for row in rows}) == 2
    assert all(row.report()["publication_authorized"] is False for row in rows)


def test_two_candidate_claims_become_two_singleton_contexts_with_exact_source_refs():
    first = _event("pull_request_review_comment", 13, minutes=1)
    second = _event("pull_request_review_comment", 13, minutes=2)
    raw, part, reply, contexts = _case(first, second, records=[
        {"summary": "設計が決まりました。", "source_refs": [1, 0]},
        {"summary": "実装済みの試験を修正しました。", "source_refs": [0]},
    ])
    assert len(contexts) == 2
    assert contexts[0].evidence_ids == (second.evidence_id, first.evidence_id)
    assert contexts[1].evidence_ids == (first.evidence_id,)
    assert tuple(row["evidence_id"] for row in contexts[0].original_events) == (
        second.evidence_id, first.evidence_id,
    )
    assert GROUND_CONTEXT_STAGE.encode() in contexts[0].to_json_bytes()
    results = [
        _normalize(x, _model(), raw, part, reply) for x in contexts
    ]
    assert all(x.verdict == "supported" for x in results)
    assert len({x.candidate_sha256 for x in results}) == 2
    assert all(x.report()["requires_human_quality_review"] is True for x in results)
    assert all(x.report()["model_verdict_is_advisory"] is True for x in results)
    assert all(x.report()["stage"] == GROUND_RESULT_STAGE for x in results)


def test_unsupported_is_distinct_but_never_publishing_authority():
    raw, part, candidate, contexts = _case(_event("issue_created", 22))
    result = _normalize(contexts[0], _model(
        verdict="unsupported", reason="根拠が不足しています。"
    ), raw, part, candidate)
    assert result.verdict == "unsupported"
    assert result.report()["publication_authorized"] is False
    assert result.report()["requires_human_quality_review"] is True
    assert result.evidence_ids == contexts[0].evidence_ids


def test_replay_is_content_addressed_and_does_not_accept_model_identity():
    raw, part, candidate, contexts = _case(_event("issue_created", 22))
    reply = _model()
    first = _normalize(contexts[0], reply, raw, part, candidate)
    second = _normalize(contexts[0], reply, raw, part, candidate)
    assert first == second
    assert first.sha256 == second.sha256
    schema = entity_grounding_output_schema()
    assert set(schema["properties"]) == {"reason", "verdict"}
    assert schema["additionalProperties"] is False
    assert "entity" not in json.dumps(schema)
    assert "merged" not in json.dumps(schema)
    assert "url" not in first.report()


def test_empty_candidate_reply_does_not_certify_zero_progress():
    original = _original(_event("pull_request_created", 9))
    part = build_entity_contexts(original).contexts[0]
    contexts = build_entity_grounding_contexts(
        part, _candidate(), original_evidence_bytes=original,
    )
    assert contexts == ()
    with pytest.raises(DailyEntityGroundingError, match="original source"):
        normalize_entity_grounding_verdict(
            # A fake context from another run is not the sole candidate here.
            _case(_event("issue_created", 99))[3][0],
            _model(),
            partial_context=part,
            partial_model_bytes=_candidate(),
            original_evidence_bytes=original,
        )


@pytest.mark.parametrize("invalid", [
    b"[]", b"not-json", b'{"verdict":"supported","verdict":"unsupported","reason":"x"}',
    b'{"verdict":"supported"}',
    b'{"verdict":"supported","reason":"x","number":20}',
    b'{"verdict":"supported","reason":"x","merged":true}',
    b'{"verdict":"supported","reason":"x","publication_authorized":true}',
    b'{"verdict":"supported","reason":"x","candidate_sha256":"0"}',
    b'{"verdict":"supported","reason":"x","url":"https://github.com/x/y/pull/9"}',
    b'{"verdict":true,"reason":"x"}',
    b'{"verdict":["supported"],"reason":"x"}',
    b'{"verdict":"unknown","reason":"x"}',
    b'{"verdict":"supported","reason":null}',
    b'{"verdict":"supported","reason":" x "}',
    b'{"verdict":"supported","reason":"\n"}',
    b'{"verdict":"supported","reason":"x\ny"}',
    b'{"verdict":"supported","reason":""}',
    rb'{"verdict":"supported","reason":"\u0000"}',
    b'{"verdict":"supported","reason":"bad",}',
    b"\xff",
    b"x" * (MAX_GROUND_OUTPUT_BYTES + 1),
])
def test_invalid_model_outputs_fail_closed(invalid):
    raw, part, candidate, contexts = _case(_event("issue_created", 10))
    with pytest.raises(DailyEntityContextError):
        _normalize(contexts[0], invalid, raw, part, candidate)


def test_forged_context_or_candidate_reply_fails():
    raw, part, candidate, contexts = _case(_event("issue_created", 13))
    ground = contexts[0]
    with pytest.raises(DailyEntityGroundingError):
        _normalize(
            replace(ground, candidate_sha256="a" * 64),
            _model(), raw, part, candidate,
        )
    with pytest.raises(DailyEntityGroundingError):
        _normalize(
            replace(ground, summary="違う事実"),
            _model(), raw, part, candidate,
        )
    with pytest.raises(DailyEntityGroundingError):
        _normalize(
            ground, _model(), raw, part,
            _candidate({"summary":"別の候補","source_refs":[0]}),
        )


def test_wrong_raw_evidence_and_forged_batch_fail_closed():
    raw, part, candidate, contexts = _case(_event("pull_request_created", 14))
    changed = _original(_event("pull_request_created", 15))
    with pytest.raises(DailyEntityContextError):
        _normalize(contexts[0], _model(), changed, part, candidate)
    with pytest.raises(DailyEntityContextError):
        _normalize(contexts[0], _model(), raw, replace(part, batch_count=2), candidate)


def test_noncanonical_later_day_or_foreign_entity_not_accepted():
    raw, part, candidate, contexts = _case(_event("issue_created", 10))
    with pytest.raises(DailyEntityContextError):
        _normalize(
            contexts[0], _model(), raw,
            replace(part, date="2026-10-13"), candidate,
        )
    with pytest.raises(DailyEntityGroundingError):
        _normalize(
            replace(contexts[0], key=replace(contexts[0].key, number=11)),
            _model(), raw, part, candidate,
        )


def test_original_event_bytes_are_bound_no_user_url_or_merge_status_can_be_selected():
    raw, part, candidate, contexts = _case(_event("pull_request_created", 88))
    data = contexts[0].to_json_bytes()
    assert b'"candidate_sha256"' in data
    assert b'"evidence_bundle_sha256"' in data
    assert b'"original_events"' in data
    for injected in (
        {"verdict":"supported", "reason":"OK", "entity_type":"issue"},
        {"verdict":"supported", "reason":"OK", "state":"merged"},
    ):
        with pytest.raises(DailyEntityGroundingError):
            _normalize(contexts[0], _canonical_json_bytes(injected), raw, part, candidate)


def test_multiline_long_reason_and_model_metadata_rejected():
    raw, part, candidate, contexts = _case(_event("issue_created", 27))
    with pytest.raises(DailyEntityGroundingError):
        _normalize(contexts[0], _model(reason="x" * 2049), raw, part, candidate)
    with pytest.raises(DailyEntityGroundingError):
        _normalize(contexts[0], _model(reason="x\ty"), raw, part, candidate)
