"""Issue/PR entity-only Daily v2 preview contract (#324), no Vault effects."""
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import sha256_bytes
from obsidian_automation.github_daily_activity import (
    ProjectBinding, _bounded_text, _date_window, _make_event,
    make_daily_evidence_bundle,
)
from obsidian_automation.github_daily_issue_pr import (
    DailyEntityPreviewError,
    _read_artifact,
    build_daily_entity_preview,
    main,
    render_daily_entity_preview,
)
from obsidian_automation.github_daily_progress import (
    _claim_id, parse_grounded_summary,
)
from obsidian_automation.github_daily_summary import parse_evidence_bundle


TARGET = date(2026, 10, 12)
REPO = "upiscium/Test"
SECOND = "upiscium/Second"
PR_KINDS = {
    "pull_request_created", "pull_request_snapshot", "pull_request_merged",
    "pull_request_closed", "pull_request_reopened",
    "pull_request_ready_for_review", "pull_request_converted_to_draft",
    "pull_request_comment", "pull_request_review",
    "pull_request_review_comment", "pull_request_commit",
}


def event(
    kind: str, number: int | None = 12, *,
    repo: str = REPO, offset: int = 10, actor: str = "alice",
    title: str | None = None, body: str | None = None,
    state: str | None = None,
):
    start, _ = _date_window(TARGET)
    pr = kind in PR_KINDS
    if kind in {"pull_request_commit", "default_branch_commit"}:
        entity_type = "commit"
        url = f"https://github.com/{repo}/commit/" + "a" * 40
    elif pr:
        entity_type = (
            kind if kind in {
                "pull_request_comment", "pull_request_review",
                "pull_request_review_comment",
            } else "pull_request"
        )
        url = f"https://github.com/{repo}/pull/{number}"
    else:
        entity_type = "issue_comment" if kind == "issue_comment" else "issue"
        url = f"https://github.com/{repo}/issues/{number}"
    extras = {}
    if kind in {"pull_request_commit", "default_branch_commit"}:
        extras["sha"] = "a" * 40
    if title is not None:
        extras["title"] = _bounded_text(
            title, limit=2048, label="title",
        )
    if body is not None:
        extras["body"] = _bounded_text(
            body, limit=8192, label="body",
        )
    return _make_event(
        kind=kind, repository=repo,
        occurred_at=start + timedelta(minutes=offset),
        url=url, actor=actor, entity_type=entity_type, number=number,
        source_id=f"{kind}:{repo}:{number}:{offset}",
        state=state, **extras,
    )


def bundle(*events):
    repositories = sorted({e.repository for e in events} or {REPO}, key=str.casefold)
    collected = make_daily_evidence_bundle(
        target_date=TARGET,
        projects=[
            ProjectBinding(
                project_path=f"10-Project/{name.split('/')[1]}/Project.md",
                repository=name,
            ) for name in repositories
        ],
        events=list(events),
    )
    return parse_evidence_bundle(collected.canonical_bytes)


def grounded(bundle, *claims):
    payload = []
    for text, kind, source_events in claims:
        ids = [source.evidence_id for source in source_events]
        repo = source_events[0].repository
        payload.append({
            "kind": kind, "summary": text, "repository": repo,
            "evidence_ids": ids, "claim_id": _claim_id(
                kind=kind, repository=repo, summary=text, evidence_ids=ids,
            ),
        })
    data = (json.dumps({
        "record_version": 1,
        "stage": "grounded_summary",
        "evidence_bundle_sha256": bundle.sha256,
        "claims": payload,
        "rejected_claims": [],
        "grounding_output_sha256s": ["a" * 64] if payload else [],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    return parse_grounded_summary(data, bundle=bundle)


def preview(*events, claims=()):
    b = bundle(*events)
    g = grounded(b, *claims)
    return build_daily_entity_preview(bundle=b, summary=g)


def test_human_issue_comment_progress_only_one_item_with_real_title() -> None:
    snapshot = event("issue_snapshot", title="Parser fixes", offset=9)
    first = event("issue_comment", body="implemented parser", offset=10)
    second = event("issue_comment", body="tests now pass", offset=11)
    p = preview(
        snapshot, first, second,
        claims=[
            ("パーサーを実装しました。", "implementation", [first]),
            ("テストが通過しました。", "bugfix", [second]),
        ],
    )
    assert len(p.items) == 1
    item = p.items[0]
    assert item.key.number == 12
    assert item.key.entity_type == "issue"
    assert item.title == "Parser fixes"
    assert item.summary.count("パーサーを実装しました。") == 1
    assert "テストが通過しました。" in item.summary
    rendered = render_daily_entity_preview(p)
    assert "## upiscium/Test" in rendered
    assert "[Issue #12 — Parser fixes](https://github.com/upiscium/Test/issues/12)" in rendered
    assert "Project:" not in rendered
    assert "- 実装:" not in rendered
    assert p.requires_review
    assert b'"publication_authorized":false' in p.to_json_bytes()


def test_bot_only_unchanged_issue_and_repo_are_absent() -> None:
    snapshot = event("issue_snapshot", offset=9, title="Old issue")
    notice = event("issue_comment", offset=10, actor="ci[bot]", body="routine")
    p = preview(
        snapshot, notice,
        claims=[("通知しました。", "issue_pr_progress", [notice])],
    )
    assert not p.items
    assert p.excluded_no_progress_count == 1
    assert render_daily_entity_preview(p) == "進展のあったIssue・PRはありません。\n"


def test_pr_merge_always_included_without_llm_claim_and_no_linked_issue() -> None:
    snapshot = event("pull_request_snapshot", 35, offset=3, title="Feature")
    merged = event("pull_request_merged", 35, offset=15)
    related = event("issue_snapshot", 12, offset=9, title="Unchanged issue")
    p = preview(snapshot, merged, related)
    assert len(p.items) == 1
    assert p.items[0].status == "merged"
    assert p.items[0].key.number == 35
    assert p.items[0].summary == "PRがマージされました。"
    rendered = render_daily_entity_preview(p)
    assert "[PR #35 — Feature](https://github.com/upiscium/Test/pull/35)" in rendered
    assert "Issue #12" not in rendered


def test_pr_commit_and_human_review_count_once_per_pr() -> None:
    snapshot = event("pull_request_snapshot", 20, offset=2, title="Implement X")
    commit = event("pull_request_commit", 20, offset=5)
    review = event("pull_request_review", 20, offset=9, body="Changes requested")
    p = preview(snapshot, commit, review, claims=[
        ("変更を実装。", "implementation", [commit]),
        ("レビュー指摘に対応中。", "issue_pr_progress", [review]),
    ])
    assert len(p.items) == 1
    assert p.items[0].summary.startswith("PRにコミットが追加されました。")
    assert "変更を実装。" in p.items[0].summary
    assert "レビュー指摘に対応中。" in p.items[0].summary
    assert "マージ" not in p.items[0].summary
    assert len(p.items[0].claim_ids) == 2


def test_same_number_issue_and_pr_are_not_mixed() -> None:
    issue = event("issue_closed", 7)
    pr = event("pull_request_merged", 7)
    p = preview(issue, pr)
    assert len(p.items) == 2
    text = render_daily_entity_preview(p)
    assert "issues/7" in text
    assert "pull/7" in text
    assert text.count("## upiscium/Test") == 1


def test_repeated_identical_claim_summaries_merge_without_duplicate_text() -> None:
    e1 = event("issue_comment", offset=10, body="fixed")
    e2 = event("issue_comment", offset=11, body="fixed again")
    p = preview(
        e1, e2, claims=[
            ("修正しました。", "bugfix", [e1]),
            ("修正しました。", "implementation", [e2]),
        ],
    )
    assert len(p.items) == 1
    assert p.items[0].summary == "修正しました。"
    assert len(p.items[0].claim_ids) == 2
    assert len(p.items[0].evidence_ids) == 2


def test_snapshot_and_unattributed_commit_do_not_generate_progress() -> None:
    snapshot = event("issue_snapshot", title="No progress")
    commit = event("default_branch_commit", number=None)
    p = preview(snapshot, commit, claims=[
        ("状態は変わりません。", "issue_pr_progress", [snapshot]),
    ])
    assert not p.items
    assert p.excluded_no_progress_count == 1
    assert p.unattributed_events_count == 1


def test_grounded_cross_entity_claim_never_guesses_target() -> None:
    issue = event("issue_comment", 7)
    other = event("issue_comment", 9)
    p = preview(issue, other, claims=[
        ("両方の作業が進みました。", "implementation", [issue, other]),
    ])
    assert p.items == ()
    assert len(p.ambiguous_claim_ids) == 1
    assert p.excluded_no_progress_count == 2
    assert not p.to_json_bytes().count(b'"publication_authorized":true')


def test_grounded_claim_with_default_branch_commit_is_ambiguous() -> None:
    issue = event("issue_comment", 7)
    commit = event("default_branch_commit", number=None)
    p = preview(issue, commit, claims=[
        ("Issueを修正しました。", "bugfix", [issue, commit]),
    ])
    assert p.items == ()
    assert len(p.ambiguous_claim_ids) == 1


def test_multi_repository_only_one_has_real_progress() -> None:
    issue = event("issue_comment", 3, repo=REPO, body="meaningful implementation")
    other = event("issue_snapshot", 7, repo=SECOND)
    p = preview(issue, other, claims=[
        ("進展しました。", "implementation", [issue]),
    ])
    assert len(p.items) == 1
    text = render_daily_entity_preview(p)
    assert "## upiscium/Test" in text
    assert "upiscium/Second" not in text


def test_merge_accept_and_green_comment_do_not_claim_merged() -> None:
    text = event("pull_request_comment", 44, body="Accept Merge, CI Green")
    snap = event("pull_request_snapshot", 44, title="Ready")
    p = preview(text, snap, claims=[
        ("マージ承認を受けました。", "issue_pr_progress", [text]),
    ])
    assert len(p.items) == 1
    assert p.items[0].status == "progress"
    assert not p.items[0].summary.startswith("PRがマージされました。")


def test_lifecycle_reopened_on_same_day_is_latest_verified_state() -> None:
    opened = event("issue_created", 8, offset=1)
    closed = event("issue_closed", 8, offset=4)
    reopened = event("issue_reopened", 8, offset=9)
    p = preview(opened, closed, reopened)
    assert len(p.items) == 1
    assert p.items[0].status == "issue_reopened"
    assert p.items[0].summary == "Issueが再オープンされました。"


def test_model_title_markdown_is_escaped_and_cannot_create_extra_section() -> None:
    issue = event(
        "issue_created", 2, title="**unsafe** [link](evil)",
    )
    p = preview(issue)
    text = render_daily_entity_preview(p)
    assert r"\*\*unsafe\*\*" in text
    assert r"\[link\]" in text
    assert text.count("## ") == 1


def test_forced_other_day_event_fails_closed() -> None:
    issue = event("issue_created", 2)
    b = bundle(issue)
    e = dict(b.events[0])
    e["occurred_at"] = "2026-10-13T03:00:00Z"
    tampered = replace(b, events=(e,))
    with pytest.raises(DailyEntityPreviewError, match="outside target"):
        build_daily_entity_preview(bundle=tampered, summary=grounded(b))


def test_wrong_url_number_and_wrong_kind_fail_closed() -> None:
    issue = event("issue_created", 2)
    b = bundle(issue)
    e = dict(b.events[0])
    e["url"] = "https://github.com/upiscium/Test/issues/999"
    with pytest.raises(DailyEntityPreviewError, match="target mismatch"):
        build_daily_entity_preview(bundle=replace(b, events=(e,)), summary=grounded(b))
    e = dict(b.events[0])
    e["entity_type"] = "pull_request"
    with pytest.raises(DailyEntityPreviewError, match="mismatch"):
        build_daily_entity_preview(bundle=replace(b, events=(e,)), summary=grounded(b))


def test_real_pr_merged_other_day_cannot_authorize_todays_merge() -> None:
    snapshot = event("pull_request_snapshot", 32, title="Open")
    p = preview(snapshot)
    assert p.items == ()
    future_merge = event("pull_request_merged", 32, offset=10)
    b = bundle(snapshot)
    later = dict(bundle(future_merge).events[0])
    later["occurred_at"] = "2026-10-13T04:00:00Z"
    with pytest.raises(DailyEntityPreviewError, match="outside target"):
        build_daily_entity_preview(
            bundle=replace(b, events=b.events + (later,)),
            summary=grounded(b),
        )


def test_renderer_does_not_drop_extra_progress_from_other_entities() -> None:
    entries = [event("issue_created", i, offset=i) for i in range(1, 12)]
    p = preview(*entries)
    assert len(p.items) == 11
    text = render_daily_entity_preview(p)
    assert text.count("- [Issue #") == 11
    assert text.count("## upiscium/Test") == 1


def test_verified_artifact_loader_rejects_fifo_symlink_or_wrong_digest(
    tmp_path: Path,
) -> None:
    source = tmp_path / ("a" * 64 + ".github-daily-evidence.json")
    source.write_bytes(b"fake")
    with pytest.raises(DailyEntityPreviewError, match="identity"):
        _read_artifact(
            source, max_bytes=1024, suffix=".github-daily-evidence.json",
        )
    source.unlink()
    if hasattr(os, "mkfifo"):
        os.mkfifo(source)
        with pytest.raises(DailyEntityPreviewError, match="inode"):
            _read_artifact(
                source, max_bytes=1024, suffix=".github-daily-evidence.json",
            )
        source.unlink()
    link_target = tmp_path / "other"
    link_target.write_bytes(b"fake")
    source.symlink_to(link_target)
    with pytest.raises(DailyEntityPreviewError, match="open"):
        _read_artifact(
            source, max_bytes=1024, suffix=".github-daily-evidence.json",
        )


def test_cli_preview_reads_sha_bound_sources_but_does_not_publish(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    issue = event("issue_created", 3, title="Plan")
    b = bundle(issue)
    ev = make_daily_evidence_bundle(
        target_date=TARGET,
        projects=[ProjectBinding(
            project_path="10-Project/Test/Project.md", repository=REPO,
        )],
        events=[issue],
    )
    assert b.sha256 == ev.sha256
    evpath = tmp_path / f"{ev.sha256}.github-daily-evidence.json"
    evpath.write_bytes(ev.canonical_bytes)
    summary = grounded(b)
    summary_blob = (json.dumps({
        "record_version": 1, "stage": "grounded_summary",
        "evidence_bundle_sha256": b.sha256,
        "claims": [], "rejected_claims": [], "grounding_output_sha256s": [],
    }, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert sha256_bytes(summary_blob) == summary.sha256
    sumpath = tmp_path / f"{summary.sha256}.github-daily-grounded-summary.json"
    sumpath.write_bytes(summary_blob)
    assert main(["--evidence", str(evpath), "--summary", str(sumpath)]) == 0
    out = capsys.readouterr()
    assert "Issue #3" in out.out
    assert "PREVIEW_ONLY" in out.err
    assert "publication_authorized=false" in out.err
    assert set(tmp_path.iterdir()) == {evpath, sumpath}

def test_routine_next_comment_cannot_be_promoted_by_model_claim() -> None:
    marker = event("issue_comment", 19, body="Next")
    p = preview(marker, claims=[
        ("実装が完了しました。", "implementation", [marker]),
    ])
    assert p.items == ()
    assert p.unresolved_candidate_keys == ()
    assert render_daily_entity_preview(p).strip() == "進展のあったIssue・PRはありません。"


def test_uncited_nonroutine_human_comment_is_unresolved_not_inactive() -> None:
    change = event("issue_comment", 19, body="We fixed the failing test")
    p = preview(change)
    assert p.items == ()
    assert len(p.unresolved_candidate_keys) == 1
    assert p.unresolved_candidate_keys[0].number == 19
    value = json.loads(p.to_json_bytes())
    assert value["unresolved_candidate_keys"] == [
        {"repository": REPO, "entity_type": "issue", "number": 19}
    ]
    assert value["publication_authorized"] is False


def test_pr_approved_review_is_progress_even_without_model_claim() -> None:
    review = event("pull_request_review", 27, state="APPROVED", offset=13)
    p = preview(review)
    assert len(p.items) == 1
    assert p.items[0].status == "pull_request_review"
    assert p.items[0].summary == "PRレビューで承認されました。"
    assert p.unresolved_candidate_keys == ()


def test_unknown_review_comment_without_claim_requires_review() -> None:
    review = event(
        "pull_request_review_comment", 27, body="Please fix the concurrency bug",
    )
    p = preview(review)
    assert p.items == ()
    assert len(p.unresolved_candidate_keys) == 1
    assert p.unresolved_candidate_keys[0].entity_type == "pull_request"


@pytest.mark.parametrize("false_summary", [
    "PRがマージされました。",
    "PRはマージ済みです。",
    "Merged into main",
    "The pull request was merged.",
])
def test_claim_cannot_promote_merge_accept_to_merged(false_summary: str) -> None:
    comment = event(
        "pull_request_comment", 22, body="Merge Accept. CI Green.",
    )
    b = bundle(comment)
    summary = grounded(b, (false_summary, "issue_pr_progress", [comment]))
    with pytest.raises(DailyEntityPreviewError, match="merge without"):
        build_daily_entity_preview(bundle=b, summary=summary)


@pytest.mark.parametrize("false_summary", [
    "本番反映完了しました。",
    "本番デプロイ済みです。",
    "Already deployed.",
])
def test_github_source_cannot_claim_live_deployment(false_summary: str) -> None:
    merged = event("pull_request_merged", 22)
    b = bundle(merged)
    summary = grounded(b, (false_summary, "implementation", [merged]))
    with pytest.raises(DailyEntityPreviewError, match="live deployment"):
        build_daily_entity_preview(bundle=b, summary=summary)


@pytest.mark.parametrize("bad_url,reason", [
    ("https://github.com/upiscium/Test/issues/2/fake", "path is not canonical"),
    ("https://github.com/upiscium/Test/issues/%32%2f", "path is not canonical"),
    ("https://github.com/upiscium/Test/issues/2?issue=3", "canonical GitHub"),
    ("https://github.com:bogus/upiscium/Test/issues/2", "port is malformed"),
])
def test_spoofed_issue_urls_fail_closed(bad_url: str, reason: str) -> None:
    issue = event("issue_created", 2)
    b = bundle(issue)
    e = dict(b.events[0])
    e["url"] = bad_url
    with pytest.raises(DailyEntityPreviewError, match=reason):
        build_daily_entity_preview(bundle=replace(b, events=(e,)), summary=grounded(b))


def test_a_verified_merge_event_allows_true_merged_summary() -> None:
    merged = event("pull_request_merged", 38)
    b = bundle(merged)
    summary = grounded(
        b, ("PRがマージされました。", "issue_pr_progress", [merged]),
    )
    p = build_daily_entity_preview(bundle=b, summary=summary)
    assert p.items[0].status == "merged"
    assert "マージされました。" in p.items[0].summary


def test_multiple_duplicate_event_ids_fail_closed_even_with_typed_bundle() -> None:
    issue = event("issue_created", 5)
    b = bundle(issue)
    with pytest.raises(DailyEntityPreviewError, match="duplicate evidence"):
        build_daily_entity_preview(
            bundle=replace(b, events=b.events + b.events),
            summary=grounded(b),
        )

@pytest.mark.parametrize("unbound_text,pattern", [
    ("Issue #99 の修正が完了しました。", "another unverified"),
    ("PR #42 was updated.", "another unverified"),
    ("See https://github.com/upiscium/Test/issues/99 for details", "links are not"),
])
def test_unbound_entity_references_in_claim_fail_closed(
    unbound_text: str, pattern: str,
) -> None:
    original = event("issue_comment", 12, body="Fixed a parser issue")
    b = bundle(original)
    summary = grounded(b, (unbound_text, "bugfix", [original]))
    with pytest.raises(DailyEntityPreviewError, match=pattern):
        build_daily_entity_preview(bundle=b, summary=summary)


def test_same_entity_number_in_grounded_claim_is_allowed() -> None:
    comment = event("issue_comment", 12, body="Issue #12 parser fix")
    p = preview(comment, claims=[
        ("Issue #12 の修正を確認しました。", "bugfix", [comment]),
    ])
    assert len(p.items) == 1
    assert p.items[0].key.number == 12


def test_bounded_json_fails_closed_when_preview_is_artificially_oversized() -> None:
    from obsidian_automation.github_daily_issue_pr import MAX_PREVIEW_JSON_BYTES

    p = preview(event("issue_created", 3, title="Task"))
    bloated = replace(
        p.items[0], summary="A" * (MAX_PREVIEW_JSON_BYTES + 1),
    )
    with pytest.raises(DailyEntityPreviewError, match="JSON exceeds"):
        replace(p, items=(bloated,)).to_json_bytes()
