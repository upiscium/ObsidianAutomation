"""#444 grouped Daily presentation: typed state and source title, not fake prose.

Offline fixture only; no GitHub, Nextcloud, Writer or real state access.
"""
from __future__ import annotations

import hashlib

from obsidian_automation import github_daily_deterministic_digest as daily
from test_github_daily_deterministic_digest import bundle, event


def test_four_groups_per_repo_sorted_without_duplicate_merge_or_close() -> None:
    original = bundle(
        event("issue_comment", 9, body="Tests completed and results recorded.",
              minute=5),
        event("issue_closed", 2, minute=8, title="Fix & ship [preview](x)"),
        event("issue_reopened", 3, minute=7, title="Reopened issue"),
        event("issue_closed", 3, minute=6, title="Previously closed"),
        event("pull_request_commit", 5, minute=6,
              message="fix: implement missing update gate"),
        event("pull_request_created", 7, minute=1, title="Implement API"),
        event("pull_request_commit", 7, minute=2, message="feat: implement API"),
        event("pull_request_merged", 7, minute=3),
        event("pull_request_review", 7, minute=4, state="APPROVED",
              body="Approved after confirming tests."),
        event("issue_created", 12, minute=1, repo="Example/Beta",
              title="Beta feature"),
        event("pull_request_created", 11, minute=2, repo="Example/Beta",
              title="Beta PR description"),
    )
    digest = daily.build_digest_from_bundle(original)
    rendered = digest.render()
    assert rendered == (
        "## Example/Alpha\n"
        "### Closed Issues\n"
        "- [Issue #2](https://github.com/Example/Alpha/issues/2): "
        r"Fix &amp; ship \[preview\]\(x\)" + "\n"
        "### Updated Issues\n"
        "- [Issue #3](https://github.com/Example/Alpha/issues/3): Reopened issue\n"
        "- [Issue #9](https://github.com/Example/Alpha/issues/9): タイトル未取得\n"
        "### Merged PRs\n"
        "- [PR #7](https://github.com/Example/Alpha/pull/7): Implement API\n"
        "### Updated PRs\n"
        "- [PR #5](https://github.com/Example/Alpha/pull/5): タイトル未取得\n"
        "\n## Example/Beta\n"
        "### Updated Issues\n"
        "- [Issue #12](https://github.com/Example/Beta/issues/12): Beta feature\n"
        "### Updated PRs\n"
        "- [PR #11](https://github.com/Example/Beta/pull/11): Beta PR description\n"
    )
    assert rendered.count("[PR #7]") == 1
    assert len(digest.items) == 7
    assert [i for i in digest.items if i["entity"]["number"] == 7
            and i["entity"]["repository"] == "Example/Alpha"][0]["status"] == "merged"
    assert "当日にPRがマージされました。" not in rendered
    assert "実装コミットが記録されました。" not in rendered
    assert "進捗コメントが記録されました。" not in rendered


def test_source_titles_are_not_commit_messages_or_untrusted_comments() -> None:
    digest = daily.build_digest_from_bundle(bundle(
        event("pull_request_commit", 21, minute=1,
              message="feat: implement backend (not actual PR title)",
              title="WRONG: title in commit-only event"),
        event("issue_comment", 22, minute=2,
              body="The user reported a planned GPU PASS next week."),
        event("issue_snapshot", 22, minute=3,
              title="Today's observable issue label"),
    ))
    assert len(digest.items) == 2
    assert "backend (not actual PR title)" not in digest.render()
    assert "WRONG: title in commit-only event" not in digest.render()
    assert "[PR #21](https://github.com/Example/Alpha/pull/21): タイトル未取得" in digest.render()
    assert "[Issue #22](https://github.com/Example/Alpha/issues/22): Today&#x27;s observable issue label" in digest.render()
    assert "GPU" not in digest.render()
    # Typed, conservative fallback diagnostics remain in the private receipt.
    by_number = {item["entity"]["number"]: item for item in digest.items}
    assert by_number[21]["sentences"] == ["実装コミットが記録されました。"]
    assert by_number[22]["sentences"] == ["進捗コメントが記録されました。"]
    assert by_number[22]["evidence_ids"]


def test_source_title_injection_newlines_and_html_are_rendered_as_literal_text() -> None:
    title = "# Fake Heading\n<script>alert(1)</script> [link](https://evil.invalid)"
    digest = daily.build_digest_from_bundle(bundle(
        event("issue_closed", 4, title=title),
    ))
    markdown = digest.render()
    assert markdown.count("### ") == 1
    assert "### Closed Issues" in markdown
    assert "\n<script>" not in markdown
    assert "<script>" not in markdown
    assert "&lt;script&gt;" in markdown
    assert r"\[link\]\(https://evil.invalid\)" in markdown
    assert "https://github.com/Example/Alpha/issues/4" in markdown
    assert markdown.count("[Issue #4]") == 1
    assert len(markdown.encode("utf-8")) < 2 * 1024 * 1024


def test_reopen_after_close_not_false_closed_and_commits_not_false_merges() -> None:
    result = daily.build_digest_from_bundle(bundle(
        event("issue_closed", 5, minute=1, title="Reopened"),
        event("issue_reopened", 5, minute=4, title="Reopened"),
        event("pull_request_closed", 6, minute=3, title="Closed unmerged"),
        event("pull_request_commit", 7, minute=1,
              message="test: add acceptance fixtures"),
    ))
    data = result.render()
    assert "### Closed Issues" not in data
    assert "### Merged PRs" not in data
    assert "### Updated Issues" in data
    assert "### Updated PRs" in data
    assert "Closed unmerged" in data
    assert data.count("[PR #7]") == 1
    assert result.report()["model_used"] is False
    assert result.report()["publication_authorized"] is False


def test_digest_receipt_v3_exposes_replaceable_title_slot_and_source_identity() -> None:
    original = bundle(
        event("pull_request_created", 7, title="Complete SSO", minute=1),
        event("pull_request_merged", 7, minute=2),
        event("issue_closed", 3, minute=4, title="Fix check"),
    )
    first = daily.build_digest_from_bundle(original)
    second = daily.build_digest_from_bundle(original)
    assert first.render() == second.render()
    assert first.sha256 == second.sha256
    report = first.report()
    assert report["record_version"] == "github-daily-deterministic-digest-v3"
    assert report["source_evidence_sha256"] == original.sha256
    assert report["rendered_markdown_sha256"] == hashlib.sha256(first.render().encode()).hexdigest()
    assert report["items"][0]["title"] == "Fix check"
    assert report["items"][1]["title"] == "Complete SSO"
    assert "当日にPRがマージされました。" in report["items"][1]["sentences"]
    assert first.render().count("[PR #7]") == 1
    assert first.capture_sha256 is None  # Test-only: no production authority.
    assert report["verified_cas_source"] is False
    assert report["needs_user_quality_approval"] is False


def test_zero_event_has_no_empty_repository_or_status_headings() -> None:
    result = daily.build_digest_from_bundle(bundle())
    assert result.render() == "進展のあったIssue・PRはありません。\n"
    assert result.report()["items"] == []
