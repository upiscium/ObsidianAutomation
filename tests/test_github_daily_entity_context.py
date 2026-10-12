"""Adversarial, entirely local Issue/PR-scoped Daily v2 candidate tests (#326)."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

from obsidian_automation.artifact_lifecycle import (
    ArtifactLifecycleError, _canonical_json_bytes, sha256_bytes,
)
from obsidian_automation.github_daily_activity import (
    ProjectBinding, _bounded_text, _date_window, _make_event,
    make_daily_evidence_bundle,
)
from obsidian_automation.github_daily_entity_context import (
    CONTEXT_STAGE, MAX_CONTEXT_BYTES, EntityPartialContext,
    DailyEntityContextError, build_entity_contexts,
    model_candidate_schema as _model_candidate_schema,
    normalize_entity_candidates as _normalize_entity_candidates,
)
from obsidian_automation.github_daily_issue_pr import EntityKey
from obsidian_automation.github_daily_summary import parse_evidence_bundle


DAY = date(2026, 10, 12)
REPO = "upiscium/Test"
OTHER = "upiscium/Other"
# Tests retain exact original Evidence bytes separately from each
# self-contained context. These bytes, not the embedded SHA, are the
# authoritative revalidation input at the model-candidate boundary.
_ORIGINAL_BUNDLES: dict[str, bytes] = {}


def normalize_entity_candidates(context, response):
    return _normalize_entity_candidates(
        context, response,
        original_evidence_bytes=_ORIGINAL_BUNDLES[context.evidence_bundle_sha256],
    )


def model_candidate_schema(context):
    return _model_candidate_schema(
        context,
        original_evidence_bytes=_ORIGINAL_BUNDLES[context.evidence_bundle_sha256],
    )


_PR_KINDS = {
    "pull_request_created", "pull_request_snapshot",
    "pull_request_comment", "pull_request_review",
    "pull_request_review_comment", "pull_request_merged",
    "pull_request_closed", "pull_request_ready_for_review",
    "pull_request_converted_to_draft", "pull_request_commit",
}


def event(
    kind: str, number: int | None = 7, *,
    repo: str = REPO, offset: int = 10, title: str | None = None,
    text: str | None = None, actor: str = "alice",
):
    start, _ = _date_window(DAY)
    pr = kind in _PR_KINDS
    if kind in {"pull_request_commit", "default_branch_commit"}:
        entity = "commit"
        url = f"https://github.com/{repo}/commit/" + "a" * 40
    elif pr:
        entity = (
            kind if kind in {
                "pull_request_comment", "pull_request_review",
                "pull_request_review_comment",
            } else "pull_request"
        )
        url = f"https://github.com/{repo}/pull/{number}"
    else:
        entity = "issue_comment" if kind == "issue_comment" else "issue"
        url = f"https://github.com/{repo}/issues/{number}"
    extra = {}
    if kind in {"default_branch_commit", "pull_request_commit"}:
        extra["sha"] = "a" * 40
        extra["message"] = _bounded_text(
            "feat: candidate", limit=4096, label="message",
        )
    if title is not None:
        extra["title"] = _bounded_text(title, limit=2048, label="title")
    if text is not None:
        extra["body"] = _bounded_text(text, limit=8192, label="body")
    return _make_event(
        kind=kind, repository=repo,
        occurred_at=start + timedelta(minutes=offset), url=url,
        actor=actor, entity_type=entity, number=number,
        source_id=f"{kind}:{repo}:{number}:{offset}", **extra,
    )


def evidence(*events) -> bytes:
    repos = sorted({item.repository for item in events} or {REPO}, key=str.casefold)
    raw = make_daily_evidence_bundle(
        target_date=DAY,
        projects=[
            ProjectBinding(
                project_path=f"10-Project/{repo.split('/')[1]}/Project.md",
                repository=repo,
            ) for repo in repos
        ],
        events=list(events),
    ).canonical_bytes
    _ORIGINAL_BUNDLES[sha256_bytes(raw)] = raw
    return raw


def model(*items) -> bytes:
    return (
        json.dumps(
            {"candidates": list(items)},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ) + "\n"
    ).encode("utf-8")


def test_issue_and_pr_same_number_are_distinct_and_unassigned_events_preserved():
    rows = (
        event("issue_created", 7, offset=2),
        event("pull_request_created", 7, offset=3),
        event("issue_comment", 7, offset=4, text="Approved new contract"),
        event("pull_request_review", 7, offset=5, text="Needs revision"),
        event("default_branch_commit", None, offset=6),
        event("pull_request_commit", 7, offset=7),
        event("issue_comment", 3, repo=OTHER, offset=8, text="New design"),
    )
    raw = evidence(*rows)
    first = build_entity_contexts(raw)
    second = build_entity_contexts(raw)
    assert first == second
    assert first.evidence_bundle_sha256 == parse_evidence_bundle(raw).sha256
    assert {c.key for c in first.contexts} == {
        EntityKey(REPO, "issue", 7),
        EntityKey(REPO, "pull_request", 7),
        EntityKey(OTHER, "issue", 3),
    }
    assert first.excluded_default_commits == 1
    assert first.unverified_pr_commits == 1
    assert first.grouped_event_count == len(rows) - 2
    assert first.report()["publication_authorized"] is False
    assert first.report()["requires_human_quality_review"] is True
    assert all(
        len({item["evidence_id"] for item in c.events}) == len(c.events)
        for c in first.contexts
    )
    assert all(
        len(c.to_json_bytes()) <= MAX_CONTEXT_BYTES
        and CONTEXT_STAGE.encode() in c.to_json_bytes()
        for c in first.contexts
    )


def test_multi_batches_one_entity_are_lossless_and_deterministic():
    entries = [
        event(
            "issue_comment", 17, offset=idx + 1,
            text="A" * 550 + f" meaningful progress step {idx}",
        ) for idx in range(9)
    ]
    raw = evidence(*entries)
    max_bytes = 1700
    data = build_entity_contexts(raw, max_bytes=max_bytes)
    assert len(data.contexts) > 1
    assert all(c.key == EntityKey(REPO, "issue", 17) for c in data.contexts)
    assert all(len(c.to_json_bytes()) <= max_bytes for c in data.contexts)
    assert tuple(
        item["evidence_id"] for c in data.contexts for item in c.events
    ) == tuple(e.evidence_id for e in entries)
    assert all(
        c.batch_count == len(data.contexts) and c.batch_index == i
        for i, c in enumerate(data.contexts)
    )
    assert [c.sha256 for c in data.contexts] == [
        c.sha256 for c in build_entity_contexts(
            raw, max_bytes=max_bytes,
        ).contexts
    ]


def test_single_event_exceeding_budget_fails_closed():
    raw = evidence(event("issue_comment", text="x" * 5000))
    with pytest.raises(DailyEntityContextError, match="single original Evidence"):
        build_entity_contexts(raw, max_bytes=1024)


def test_empty_evidence_yields_no_repository_output():
    result = build_entity_contexts(evidence())
    assert result.contexts == ()
    assert result.report()["grouped_event_count"] == 0
    assert result.report()["publication_authorized"] is False


@pytest.mark.parametrize("bad_budget", [True, 0, 1000, 65537, "24000", None])
def test_context_budget_requires_bounded_integer(bad_budget):
    with pytest.raises(DailyEntityContextError):
        build_entity_contexts(
            evidence(event("issue_created")), max_bytes=bad_budget,
        )


def test_event_wrong_github_entity_url_fails_closed():
    x = event("issue_comment", number=9)
    invalid = replace(x, url=f"https://github.com/{REPO}/issues/10")
    payload = invalid.to_json()
    payload.pop("evidence_id")
    invalid = replace(invalid, evidence_id=sha256_bytes(_canonical_json_bytes(payload)))
    with pytest.raises(DailyEntityContextError):
        build_entity_contexts(evidence(invalid))


def test_duplicate_raw_evidence_id_rejected():
    e = event("issue_comment", number=19)
    # The real collector suppresses duplicate events during production; forge
    # an otherwise well-formed input to exercise the parser's invariant.
    payload = json.loads(evidence(e))
    payload["events"].append(dict(payload["events"][0]))
    raw = _canonical_json_bytes(payload)
    with pytest.raises(ArtifactLifecycleError):
        build_entity_contexts(raw)


def test_noncanonical_date_window_rejected_even_with_parseable_evidence():
    raw = evidence(event("issue_created"))
    bundle = json.loads(raw)
    bundle["window_start"] = "2026-10-12T00:00:00Z"
    with pytest.raises(DailyEntityContextError, match="wrong JST window"):
        build_entity_contexts(
            (json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )


def test_model_schema_never_exposes_entity_identity_or_merge_state():
    c = build_entity_contexts(evidence(
        event("pull_request_created", offset=1),
        event("pull_request_comment", offset=2, text="Changed code"),
    )).contexts[0]
    schema = model_candidate_schema(c)
    assert set(schema["properties"]) == {"candidates"}
    item = schema["properties"]["candidates"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["properties"]) == {"summary", "source_refs"}
    assert item["properties"]["source_refs"]["items"]["enum"] == [0, 1]
    assert "merged" not in json.dumps(schema)
    assert "repository" not in json.dumps(schema)


def test_normalized_model_claims_bind_only_original_issue_event_ids():
    first = event("issue_comment", 19, offset=1, text="Decided on ADR")
    second = event("issue_comment", 19, offset=2, text="Wrote tests")
    c = build_entity_contexts(evidence(first, second)).contexts[0]
    payload = model({
        "summary": "設計が確定し、テストが追加されました。",
        "source_refs": [1, 0],
    })
    result = normalize_entity_candidates(c, payload)
    assert result.context_sha256 == c.sha256
    assert result.key == EntityKey(REPO, "issue", 19)
    assert result.response_sha256
    assert result.candidates[0].evidence_ids == (
        second.evidence_id, first.evidence_id,
    )
    assert result.uncited_evidence_ids == ()
    assert result.report()["grounding_required"] is True
    assert result.report()["semantic_coverage_review_required"] is True
    assert result.report()["publication_authorized"] is False
    assert all(
        "url" not in entry and "merged" not in entry
        for entry in result.report()["candidates"]
    )


def test_uncited_and_empty_model_result_cannot_certify_no_progress():
    a = event("issue_comment", 19, offset=1)
    b = event("issue_comment", 19, offset=2)
    c = build_entity_contexts(evidence(a, b)).contexts[0]
    empty = normalize_entity_candidates(c, model())
    assert empty.candidates == ()
    assert empty.uncited_evidence_ids == (a.evidence_id, b.evidence_id)
    assert empty.report()["semantic_coverage_review_required"] is True
    partial = normalize_entity_candidates(c, model({
        "summary": "1件の変更があります", "source_refs": [0],
    }))
    assert partial.uncited_evidence_ids == (b.evidence_id,)


@pytest.mark.parametrize("candidate", [
    {"summary": "Progress", "source_refs": [2]},
    {"summary": "Progress", "source_refs": [-1]},
    {"summary": "Progress", "source_refs": [True]},
    {"summary": "Progress", "source_refs": ["0"]},
    {"summary": "Progress", "source_refs": [0, 0]},
    {"summary": "Progress", "source_refs": []},
    {"summary": "Progress", "source_refs": [0], "url": "https://example.org"},
    {"summary": "Progress", "source_refs": [0], "number": 999},
    {"summary": " Progress ", "source_refs": [0]},
    {"summary": "Progress\nDeployed", "source_refs": [0]},
    {"summary": "", "source_refs": [0]},
    {"summary": "x" * 2100, "source_refs": [0]},
])
def test_invalid_model_refs_and_prose_are_rejected(candidate):
    c = build_entity_contexts(evidence(event("issue_comment"))).contexts[0]
    with pytest.raises(DailyEntityContextError):
        normalize_entity_candidates(c, model(candidate))


@pytest.mark.parametrize("response", [
    b'{"candidates":[],"merged":true}',
    b'{"candidates":[],"candidates":[]}',
    b'[]',
    b'{"candidates":"not-array"}',
    b'{"candidates":[]}\nnot json',
    b'not json',
    b"x" * (65536 + 1),
])
def test_invalid_top_level_model_output_is_rejected(response):
    c = build_entity_contexts(evidence(event("issue_created"))).contexts[0]
    with pytest.raises((DailyEntityContextError, ArtifactLifecycleError)):
        normalize_entity_candidates(c, response)


def test_wrong_entity_manual_context_cannot_normalize():
    original = event("issue_comment", 22)
    valid = build_entity_contexts(evidence(original)).contexts[0]
    forged = replace(
        valid, key=EntityKey(REPO, "pull_request", 22),
    )
    with pytest.raises(DailyEntityContextError, match="unbound entity"):
        normalize_entity_candidates(
            forged, model({"summary": "Updated", "source_refs": [0]}),
        )
    # Even the empty response must not provide a false "no progress" result
    # when the context itself points to the wrong Issue/PR identity.
    with pytest.raises(DailyEntityContextError, match="unbound entity"):
        normalize_entity_candidates(forged, model())
    with pytest.raises(DailyEntityContextError, match="unbound entity"):
        model_candidate_schema(forged)


def test_replaying_same_input_and_model_text_is_content_addressed():
    c = build_entity_contexts(evidence(event("issue_created"))).contexts[0]
    raw = model({"summary": "Issueの作業を開始", "source_refs": [0]})
    first = normalize_entity_candidates(c, raw)
    replay = normalize_entity_candidates(c, raw)
    assert first == replay
    assert first.candidates[0].candidate_sha256 == replay.candidates[0].candidate_sha256
    assert c.sha256 == c.sha256


def test_never_exposes_default_commit_as_repositorwide_summary():
    result = build_entity_contexts(evidence(
        event("default_branch_commit", None, offset=1),
    ))
    assert not result.contexts
    assert result.excluded_default_commits == 1
    assert result.report()["publication_authorized"] is False

def test_manual_context_with_mutated_original_event_digest_fails_closed():
    first = event("issue_comment", 10, offset=1, text="Actual change")
    c = build_entity_contexts(evidence(first)).contexts[0]
    wrong = dict(c.events[0])
    wrong["actor"] = "attacker"
    forged = replace(c, events=(wrong,))
    with pytest.raises(DailyEntityContextError, match="Evidence changed"):
        model_candidate_schema(forged)
    with pytest.raises(DailyEntityContextError, match="Evidence changed"):
        normalize_entity_candidates(forged, model())


def test_manual_context_with_out_of_window_event_fails_even_without_claims():
    first = event("issue_comment", 10, offset=1)
    c = build_entity_contexts(evidence(first)).contexts[0]
    wrong = dict(c.events[0])
    wrong["occurred_at"] = "2026-10-13T00:00:00Z"
    forged = replace(c, events=(wrong,))
    with pytest.raises(DailyEntityContextError):
        normalize_entity_candidates(forged, model())


def test_model_scope_excludes_external_context_ids_and_wrong_batch():
    rows = [
        event("issue_comment", 90, offset=i + 1, text="X" * 700)
        for i in range(5)
    ]
    contexts = build_entity_contexts(
        evidence(*rows), max_bytes=1800,
    ).contexts
    assert len(contexts) > 1
    first = contexts[0]
    ref_count = len(first.events)
    with pytest.raises(DailyEntityContextError, match="model source_refs"):
        normalize_entity_candidates(first, model({
            "summary": "Other context",
            "source_refs": [ref_count],
        }))


def test_self_consistent_forged_source_is_not_member_of_original_bundle():
    first = event("issue_comment", 12, offset=1, text="Correct approved action")
    raw = evidence(first)
    context = build_entity_contexts(raw).contexts[0]
    # An attacker changes the event AND recomputes the event identity,
    # while falsely retaining the original full-bundle digest.
    forged_row = dict(context.events[0])
    forged_row["body"] = _bounded_text(
        "Completely fabricated progress", limit=8192, label="body",
    ).to_json()
    identity = dict(forged_row)
    identity.pop("evidence_id")
    forged_row["evidence_id"] = sha256_bytes(_canonical_json_bytes(identity))
    forged = replace(context, events=(forged_row,))
    with pytest.raises(DailyEntityContextError, match="original Evidence partition"):
        normalize_entity_candidates(forged, model())
    with pytest.raises(DailyEntityContextError, match="original Evidence partition"):
        model_candidate_schema(forged)


def test_wrong_raw_bundle_and_tampered_batch_identity_are_not_accepted():
    a = event("issue_comment", 14, offset=1, text="Original")
    raw = evidence(a)
    context = build_entity_contexts(raw).contexts[0]
    different = evidence(event("issue_comment", 15, offset=1, text="Other"))
    with pytest.raises(DailyEntityContextError, match="original Evidence partition"):
        _normalize_entity_candidates(
            context, model(), original_evidence_bytes=different,
        )
    forged = replace(context, batch_count=2)
    with pytest.raises(DailyEntityContextError, match="original Evidence partition"):
        normalize_entity_candidates(forged, model())


def test_pr_conversation_comment_with_issue_url_is_unverified():
    original = event(
        "pull_request_comment", 32, offset=2,
        text="Important PR work in a shared GitHub issue conversation",
    )
    changed = replace(original, url=f"https://github.com/{REPO}/issues/32")
    payload = changed.to_json()
    payload.pop("evidence_id")
    changed = replace(
        changed, evidence_id=sha256_bytes(_canonical_json_bytes(payload)),
    )
    raw = evidence(changed)
    result = build_entity_contexts(raw)
    assert not result.contexts
    assert result.unverified_pr_comments == 1
    assert result.grouped_event_count == 0
    assert result.report()["publication_authorized"] is False
    assert result.report()["requires_human_quality_review"] is True


def test_duplicate_candidate_objects_are_rejected():
    c = build_entity_contexts(evidence(event("issue_comment", 8))).contexts[0]
    duplicate = {"summary": "進展がありました", "source_refs": [0]}
    with pytest.raises(DailyEntityContextError, match="duplicated entity candidate"):
        normalize_entity_candidates(c, model(duplicate, duplicate))


def test_nondefault_context_budget_still_binds_to_original_partition():
    a = event("issue_comment", 8, offset=1, text="A" * 350)
    b = event("issue_comment", 8, offset=2, text="B" * 350)
    raw = evidence(a, b)
    contexts = build_entity_contexts(raw, max_bytes=1300).contexts
    assert len(contexts) == 2
    for context in contexts:
        assert context.max_context_bytes == 1300
        response = normalize_entity_candidates(context, model({
            "summary": "根拠に対応した進捗候補",
            "source_refs": [0],
        }))
        assert response.candidates[0].evidence_ids == context.evidence_ids
