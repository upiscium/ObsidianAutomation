"""Adversarial zero-effects Issue/PR-level v2 grounded preview tests (#332)."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from typing import Callable

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
    build_entity_grounding_contexts,
)
from obsidian_automation.github_daily_entity_rollup import (
    BoundModelResponse, PARTIAL_REPLY_STAGE, GROUND_REPLY_STAGE,
    DailyEntityRollupError, build_entity_rollup_preview,
    render_entity_rollup_preview,
)


DAY = date(2026, 10, 12)
REPO = "upiscium/Test"
OTHER = "upiscium/Other"
_PR_KIND = {
    "pull_request_created", "pull_request_closed", "pull_request_merged",
    "pull_request_reopened", "pull_request_ready_for_review",
    "pull_request_review_comment", "pull_request_review",
    "pull_request_comment", "pull_request_snapshot",
}


def _event(
    kind: str, number: int, *, repo: str = REPO, minute: int = 1,
    body: str = "Implemented a meaningful change",
    actor: str = "human", state: str | None = None,
):
    start, _ = _date_window(DAY)
    is_pr = kind in _PR_KIND
    if kind in {"pull_request_review_comment", "pull_request_review", "pull_request_comment"}:
        entity = kind
    else:
        entity = "pull_request" if is_pr else (
            "issue_comment" if kind == "issue_comment" else "issue"
        )
    path = "pull" if is_pr else "issues"
    return _make_event(
        kind=kind, repository=repo,
        occurred_at=start + timedelta(minutes=minute),
        url=f"https://github.com/{repo}/{path}/{number}",
        actor=actor, entity_type=entity, number=number,
        source_id=f"{repo}:{kind}:{number}:{minute}", state=state,
        title=_bounded_text("Change title", limit=2048, label="title"),
        body=_bounded_text(body, limit=8192, label="body"),
    )


def _original(*rows) -> bytes:
    repos = sorted({x.repository for x in rows} or {REPO})
    return make_daily_evidence_bundle(
        target_date=DAY,
        projects=[
            ProjectBinding(f"10-Project/{repo.split('/')[1]}/Project.md", repo)
            for repo in repos
        ],
        events=list(rows),
    ).canonical_bytes


def _json(value: object) -> bytes:
    return _canonical_json_bytes(value)


def _bound(stage: str, context_sha: str, response: bytes) -> BoundModelResponse:
    from hashlib import sha256
    return BoundModelResponse(
        stage, context_sha, sha256(response).hexdigest(), response,
    )


def _responses(
    evidence: bytes,
    *,
    partial: Callable = lambda ctx: [{"summary":"実装方針が決まりました。", "source_refs":[0]}],
    ground: Callable = lambda context: "supported",
):
    grouped = build_entity_contexts(evidence)
    partials = {}
    grounds = {}
    for context in grouped.contexts:
        model_partial = _json({"candidates":partial(context)})
        partials[context.sha256] = _bound(
            PARTIAL_REPLY_STAGE, context.sha256, model_partial,
        )
        for single in build_entity_grounding_contexts(
            context, model_partial, original_evidence_bytes=evidence,
        ):
            grounds[single.sha256] = _bound(
                GROUND_REPLY_STAGE, single.sha256, _json({
                    "verdict":ground(single), "reason":"元Evidenceが支持している候補です。"
                }),
            )
    return partials, grounds


def _preview(evidence, *, partial=None, ground=None):
    opts = {}
    if partial is not None:
        opts["partial"] = partial
    if ground is not None:
        opts["ground"] = ground
    partials, grounds = _responses(evidence, **opts)
    return build_entity_rollup_preview(
        evidence,
        partial_responses_by_context_sha256=partials,
        grounding_responses_by_context_sha256=grounds,
    )


def test_issue_pr_same_number_and_multi_repo_one_item_each():
    original = _original(
        _event("issue_created", 7, minute=1),
        _event("pull_request_created", 7, minute=2),
        _event("pull_request_merged", 7, minute=3),
        _event("pull_request_review_comment", 12, repo=OTHER, minute=4),
    )
    outcome = _preview(original)
    assert len(outcome.items) == 3
    assert {(x.key.repository,x.key.entity_type,x.key.number) for x in outcome.items} == {
        (REPO, "issue", 7), (REPO, "pull_request", 7),
        (OTHER, "pull_request", 12),
    }
    merged = next(x for x in outcome.items if x.key.entity_type == "pull_request" and x.key.number == 7)
    assert merged.verified_merge_event
    assert merged.status_event == "pull_request_merged"
    assert not merged.lifecycle_conflict
    body = render_entity_rollup_preview(outcome)
    assert "## upiscium/Test" in body
    assert "## upiscium/Other" in body
    assert f"[PR #7](https://github.com/{REPO}/pull/7)" in body
    assert f"[Issue #7](https://github.com/{REPO}/issues/7)" in body
    assert "PRがマージされました。" in body
    assert "未承認" in body
    assert outcome.payload()["publication_authorized"] is False
    assert outcome.payload()["requires_human_quality_review"] is True


def test_many_events_crossing_entity_context_boundary_one_item_without_source_loss():
    # 67 events exceed the 64-original-events per-entity source cap.
    rows = [_event("pull_request_review_comment", 28, minute=i + 1, body=f"Change #{i}") for i in range(67)]
    original = _original(*rows)
    outcome = _preview(original, partial=lambda ctx:[
        {"summary":f"実装変更バッチ{ctx.batch_index}を実施。", "source_refs":list(range(min(8,len(ctx.events))))}
    ])
    assert len(outcome.items) == 1
    assert outcome.processed_context_count == 2
    assert outcome.processed_ground_count == 2
    assert len(outcome.items[0].supported_candidate_sha256s) == 2
    assert outcome.items[0].key.number == 28
    assert outcome.incomplete
    assert len(outcome.items[0].unresolved_source_ids) > 0
    assert outcome.payload()["publication_authorized"] is False


@pytest.mark.parametrize("which", ["partial_missing", "partial_extra", "ground_missing", "ground_extra"])
def test_exact_input_map_completeness_fails_closed(which):
    original = _original(_event("issue_created", 10))
    partials, grounds = _responses(original)
    if which == "partial_missing":
        partials.clear()
    if which == "partial_extra":
        partials["a" * 64] = _bound(
            PARTIAL_REPLY_STAGE, "a" * 64, _json({"candidates":[]}),
        )
    if which == "ground_missing":
        grounds.clear()
    if which == "ground_extra":
        grounds["a" * 64] = _bound(
            GROUND_REPLY_STAGE, "a" * 64,
            _json({"verdict":"supported","reason":"Valid"}),
        )
    with pytest.raises(DailyEntityRollupError, match="missing, extra or swapped"):
        build_entity_rollup_preview(
            original, partial_responses_by_context_sha256=partials,
            grounding_responses_by_context_sha256=grounds,
        )


def test_source_swapping_of_original_evidence_fails_closed():
    original = _original(_event("issue_created", 11))
    partials, grounds = _responses(original)
    other = _original(_event("issue_created", 12))
    with pytest.raises(DailyEntityRollupError):
        build_entity_rollup_preview(
            other, partial_responses_by_context_sha256=partials,
            grounding_responses_by_context_sha256=grounds,
        )


def test_invalid_model_response_or_wrong_ground_context_fails_closed():
    original = _original(_event("issue_created", 13))
    partials, grounds = _responses(original)
    target = next(iter(grounds))
    grounds[target] = _bound(
        GROUND_REPLY_STAGE, target,
        b'{"verdict":"supported","reason":"ok","merged":true}',
    )
    with pytest.raises(DailyEntityContextError):
        build_entity_rollup_preview(
            original, partial_responses_by_context_sha256=partials,
            grounding_responses_by_context_sha256=grounds,
        )


@pytest.mark.parametrize("summary", [
    "PRがマージされました。", "PR #31 をマージしました。",
    "PR #31 has been merged.", "Merge completed for PR 31."
])
def test_unverified_model_merge_wording_is_never_displayed(summary):
    original = _original(_event("pull_request_created", 31))
    report = _preview(original, partial=lambda ctx:[
        {"summary":summary,"source_refs":[0]}
    ])
    assert not report.items[0].verified_merge_event
    assert report.items[0].status_event == "pull_request_created"
    assert report.items[0].supported_candidate_sha256s
    assert summary not in render_entity_rollup_preview(report)
    assert summary not in report.to_json_bytes().decode("utf-8")
    assert "PRが作成されました" in render_entity_rollup_preview(report)


@pytest.mark.parametrize("summary", [
    "Production deployed.", "See https://evil.invalid/pr/5",
    "PR #999 implementation was accepted."
])
def test_fabricated_model_deployment_link_and_other_issue_are_not_exposed(summary):
    original = _original(_event("pull_request_created", 5))
    report = _preview(original,partial=lambda ctx:[
        {"summary":summary,"source_refs":[0]}
    ])
    assert summary not in report.to_json_bytes().decode("utf-8")
    assert summary not in render_entity_rollup_preview(report)
    assert report.payload()["publication_authorized"] is False


def test_grounded_unsupported_without_structural_change_is_unresolved_not_no_progress():
    original = _original(_event("pull_request_review_comment", 44, body="Security review found a flaw"))
    outcome = _preview(original, ground=lambda ctx:"unsupported")
    assert outcome.items == ()
    assert outcome.incomplete
    assert outcome.unresolved_entity_keys[0].number == 44
    assert "未完了" in render_entity_rollup_preview(outcome)
    assert outcome.payload()["publication_authorized"] is False


def test_empty_model_candidates_do_not_hide_possible_progress():
    original = _original(_event("pull_request_review_comment", 43))
    outcome = _preview(original, partial=lambda ctx:[])
    assert outcome.items == ()
    assert outcome.incomplete
    assert outcome.unresolved_entity_keys[0].number == 43
    assert outcome.processed_ground_count == 0


def test_ambiguous_issue_comment_remains_unresolved_and_unassigned():
    original = _original(_event("issue_comment", 16, body="Design was finalized"))
    outcome = _preview(original)
    assert outcome.items == ()
    assert outcome.incomplete
    assert outcome.unverified_issue_comments == 1
    assert "未完了" in render_entity_rollup_preview(outcome)


def test_empty_day_and_no_invented_repository_prose():
    original = _original()
    report = _preview(original)
    assert not report.items
    assert not report.incomplete
    assert "## " not in render_entity_rollup_preview(report)
    assert report.payload()["publication_authorized"] is False


def test_ready_and_merge_accept_without_actual_merge_is_not_merged():
    original = _original(_event("pull_request_ready_for_review", 17))
    outcome = _preview(original,partial=lambda ctx:[])
    assert len(outcome.items)==1
    assert outcome.items[0].verified_merge_event is False
    assert outcome.items[0].status_event == "pull_request_ready_for_review"
    assert "マージされました" not in render_entity_rollup_preview(outcome)


def test_explicit_merge_followed_by_contradicting_reopen_is_flagged():
    original = _original(
        _event("pull_request_merged", 31, minute=1),
        _event("pull_request_reopened", 31, minute=2),
    )
    outcome = _preview(original, partial=lambda ctx:[])
    assert outcome.items[0].verified_merge_event
    assert outcome.items[0].lifecycle_conflict
    assert outcome.incomplete
    assert "状態要確認" in render_entity_rollup_preview(outcome)


def test_exact_same_input_replay_is_byte_identical_and_no_write_authority():
    original = _original(
        _event("issue_created", 9),
        _event("pull_request_created", 10, minute=2),
    )
    partials, grounds = _responses(original)
    kw = {
        "partial_responses_by_context_sha256":partials,
        "grounding_responses_by_context_sha256":grounds,
    }
    first=build_entity_rollup_preview(original,**kw)
    second=build_entity_rollup_preview(original,**kw)
    assert first == second
    assert first.sha256 == second.sha256
    assert first.to_json_bytes() == second.to_json_bytes()
    assert "publication_authorized" in first.payload()
    assert first.payload()["publication_authorized"] is False


@pytest.mark.parametrize("summary", [
    "実装を更新。\u2028次のIssueまでマージ済み",
    "見えない\u200bリンク表示",
    "予期しない\u0085改行",
])
def test_unprintable_model_candidate_never_reaches_markdown(summary):
    original = _original(_event("pull_request_created", 52))
    with pytest.raises(DailyEntityRollupError, match="nonprintable"):
        _preview(original, partial=lambda ctx: [
            {"summary": summary, "source_refs": [0]}
        ])


def test_grounded_supported_with_no_lifecycle_remains_advisory():
    original = _original(_event("pull_request_review_comment", 51))
    report = _preview(original)
    assert len(report.items) == 1
    item = report.items[0]
    assert item.status_event is None
    assert not item.verified_merge_event
    assert item.supported_candidate_sha256s
    assert report.payload()["requires_human_quality_review"] is True
    text = render_entity_rollup_preview(report)
    assert "根拠付き候補1件" in text
    assert "マージされました" not in text


def test_empty_candidate_for_structural_event_retains_lifecycle_fact_only():
    original = _original(_event("issue_closed", 33))
    report = _preview(original, partial=lambda ctx: [])
    assert len(report.items) == 1
    assert report.items[0].status_event == "issue_closed"
    assert report.items[0].supported_candidate_sha256s == ()
    assert "Issueがクローズされました。" in render_entity_rollup_preview(report)


def test_conflicting_lifecycle_does_not_publish_model_current_merge_claim():
    original = _original(
        _event("pull_request_merged", 34, minute=1),
        _event("pull_request_reopened", 34, minute=2),
    )
    summary = "PRはマージ済みで現在もClosedです。"
    report = _preview(original, partial=lambda ctx:[
        {"summary":summary,"source_refs":[0]}
    ])
    assert report.items[0].verified_merge_event
    assert report.items[0].lifecycle_conflict
    assert report.incomplete
    assert summary not in render_entity_rollup_preview(report)
    assert summary not in report.to_json_bytes().decode("utf-8")


@pytest.mark.parametrize("review_state,expected", [
    ("approved", "PRレビューで承認されました。"),
    ("changes_requested", "PRレビューで変更要求を受けました。"),
])
def test_explicit_review_decision_is_verified_progress_without_model_candidate(
    review_state, expected,
):
    original = _original(
        _event("pull_request_review", 71, state=review_state, body=""),
    )
    outcome = _preview(original, partial=lambda ctx:[])
    assert len(outcome.items) == 1
    assert outcome.items[0].status_event == "pull_request_review:" + review_state
    assert expected in render_entity_rollup_preview(outcome)
    assert not outcome.items[0].verified_merge_event


def test_review_without_recognized_decision_or_candidate_is_not_misreported_as_inactive():
    original = _original(
        _event("pull_request_review", 72, state="commented", body=""),
    )
    outcome = _preview(original, partial=lambda ctx:[])
    assert outcome.items == ()
    assert outcome.incomplete
    assert outcome.unresolved_entity_keys[0].number == 72


def test_bounded_evidence_reparse_budget_fails_closed_before_candidate_reads(monkeypatch):
    import obsidian_automation.github_daily_entity_rollup as rollup
    original = _original(_event("issue_created", 91))
    partials, grounds = _responses(original)
    monkeypatch.setattr(rollup, "MAX_EVIDENCE_REPLAY_BYTES", len(original) - 1)
    with pytest.raises(DailyEntityRollupError, match="replay budget"):
        build_entity_rollup_preview(
            original,
            partial_responses_by_context_sha256=partials,
            grounding_responses_by_context_sha256=grounds,
        )


def test_swapped_partial_bound_replies_are_rejected_even_when_both_are_valid_empty():
    original = _original(
        _event("issue_created", 201, minute=1),
        _event("pull_request_created", 202, minute=2),
    )
    partials, grounds = _responses(original, partial=lambda ctx:[])
    assert grounds == {}
    keys = list(partials)
    assert len(keys) == 2
    partials[keys[0]], partials[keys[1]] = partials[keys[1]], partials[keys[0]]
    with pytest.raises(DailyEntityRollupError, match="context binding"):
        build_entity_rollup_preview(
            original, partial_responses_by_context_sha256=partials,
            grounding_responses_by_context_sha256=grounds,
        )


def test_swapped_grounding_envelopes_and_replaced_bytes_are_rejected():
    original = _original(
        _event("issue_created", 203, minute=1),
        _event("pull_request_created", 204, minute=2),
    )
    partials, grounds = _responses(original)
    keys = list(grounds)
    assert len(keys) == 2
    grounds[keys[0]], grounds[keys[1]] = grounds[keys[1]], grounds[keys[0]]
    with pytest.raises(DailyEntityRollupError, match="context binding"):
        build_entity_rollup_preview(
            original, partial_responses_by_context_sha256=partials,
            grounding_responses_by_context_sha256=grounds,
        )
    partials, grounds = _responses(original)
    key = next(iter(grounds))
    grounds[key] = replace(grounds[key], response_bytes=b'{"verdict":"supported"}')
    with pytest.raises(DailyEntityRollupError, match="content digest"):
        build_entity_rollup_preview(
            original, partial_responses_by_context_sha256=partials,
            grounding_responses_by_context_sha256=grounds,
        )


def test_bare_bytes_or_wrong_stage_never_qualify_as_bound_model_reply():
    original = _original(_event("issue_created", 205))
    partials, grounds = _responses(original)
    key = next(iter(partials))
    partials[key] = partials[key].response_bytes
    with pytest.raises(DailyEntityRollupError, match="context binding"):
        build_entity_rollup_preview(
            original, partial_responses_by_context_sha256=partials,
            grounding_responses_by_context_sha256=grounds,
        )
    partials, grounds = _responses(original)
    key = next(iter(partials))
    partials[key] = replace(partials[key], stage=GROUND_REPLY_STAGE)
    with pytest.raises(DailyEntityRollupError, match="context binding"):
        build_entity_rollup_preview(
            original, partial_responses_by_context_sha256=partials,
            grounding_responses_by_context_sha256=grounds,
        )


def test_locally_constructed_bound_reply_is_not_model_or_producer_attestation():
    original = _original(_event("issue_created", 206))
    partials, grounds = _responses(original, partial=lambda ctx:[])
    report = build_entity_rollup_preview(
        original, partial_responses_by_context_sha256=partials,
        grounding_responses_by_context_sha256=grounds,
    )
    assert report.payload()["producer_attestation_verified"] is False
    assert report.payload()["requires_human_quality_review"] is True
    assert report.payload()["publication_authorized"] is False
