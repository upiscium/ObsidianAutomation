from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from obsidian_automation.core_promotion_transport import (
    HTTPResponse,
    PromotionTransportNetworkError,
)
from obsidian_automation.github_daily_activity import (
    ProjectBinding,
    _bounded_text,
    _date_window,
    _make_event,
    make_daily_evidence_bundle,
)
from obsidian_automation.github_daily_progress import (
    DailyProgressConflict,
    DailyProgressError,
    DailyProgressTargetMissing,
    _claim_id,
    apply_projection,
    daily_target_path,
    make_projection,
    parse_grounded_summary,
    parse_projection,
    persist_transport_result,
    prepare_daily_update,
    render_section_body,
)
from obsidian_automation.github_daily_summary import SummaryClaim


def _bundle():
    start, _ = _date_window(date(2026, 10, 5))
    issue_body = _bounded_text(
        "fixed parser",
        limit=8192,
        label="body",
    )
    commit_message = _bounded_text(
        "fix: parser",
        limit=4096,
        label="message",
    )
    assert issue_body is not None
    assert commit_message is not None
    issue = _make_event(
        kind="issue_comment",
        repository="upiscium/Test",
        occurred_at=start,
        url=(
            "https://github.com/upiscium/Test/issues/7"
            "#issuecomment-100"
        ),
        actor="upiscium",
        entity_type="issue_comment",
        number=7,
        source_id="100",
        body=issue_body,
    )
    commit = _make_event(
        kind="default_branch_commit",
        repository="upiscium/Test",
        occurred_at=start,
        url="https://github.com/upiscium/Test/commit/" + "a" * 40,
        actor="upiscium",
        entity_type="commit",
        number=None,
        source_id="a" * 40,
        sha="a" * 40,
        message=commit_message,
    )
    bundle = make_daily_evidence_bundle(
        target_date=date(2026, 10, 5),
        projects=[
            ProjectBinding(
                project_path="10-Project/Test/Test.md",
                repository="upiscium/Test",
            )
        ],
        events=[issue, commit],
    )
    return bundle, issue, commit


def _summary_bytes(bundle, issue, commit, *, summary_text="Fixed parser"):
    evidence_ids = [issue.evidence_id, commit.evidence_id]
    claim_id = _claim_id(
        kind="bugfix",
        repository="upiscium/Test",
        summary=summary_text,
        evidence_ids=evidence_ids,
    )
    return (
        json.dumps(
            {
                "record_version": 1,
                "stage": "grounded_summary",
                "evidence_bundle_sha256": bundle.sha256,
                "claims": [
                    {
                        "claim_id": claim_id,
                        "kind": "bugfix",
                        "repository": "upiscium/Test",
                        "summary": summary_text,
                        "evidence_ids": evidence_ids,
                    }
                ],
                "rejected_claims": [],
                "grounding_output_sha256s": ["b" * 64],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _projection(*, summary_text="Fixed parser"):
    bundle, issue, commit = _bundle()
    summary = parse_grounded_summary(
        _summary_bytes(
            bundle,
            issue,
            commit,
            summary_text=summary_text,
        ),
        bundle=bundle,
    )
    return bundle, make_projection(bundle=bundle, summary=summary)


def _daily(body: str, *, eol: str = "\n", daily_type: str = "daily-review"):
    text = (
        "---\n"
        f"type: {daily_type}\n"
        "---\n"
        "# Note\n"
        "Human note.\n"
        + body
        + "# Tasks\n"
        "- [ ] Human task\n"
    )
    if eol == "\r\n":
        text = text.replace("\n", "\r\n")
    return text.encode("utf-8")


def test_renderer_is_deterministic_and_escapes_model_markdown() -> None:
    bundle, issue, commit = _bundle()
    text = "Fixed *parser* and [[unsafe-link]]"
    summary = parse_grounded_summary(
        _summary_bytes(
            bundle,
            issue,
            commit,
            summary_text=text,
        ),
        bundle=bundle,
    )

    rendered = render_section_body(bundle, summary)

    assert rendered.startswith(
        "## upiscium/Test\nProject: Test\n\n"
    )
    assert (
        r"- バグ修正: Fixed \*parser\* and "
        r"\[\[unsafe-link\]\]"
    ) in rendered
    assert (
        "[#7](https://github.com/upiscium/Test/issues/7"
        "#issuecomment-100)"
    ) in rendered
    assert (
        "[commit aaaaaaa](https://github.com/upiscium/Test/commit/"
        + "a" * 40
        + ")"
    ) in rendered
    assert "# Project Progress" not in rendered


def test_projection_binds_date_path_and_exact_section() -> None:
    bundle, projection = _projection()

    assert projection.date == "2026-10-05"
    assert projection.target_path == (
        "00-DailyNote/2026/10/2026-10-05.md"
    )
    assert daily_target_path(projection.date) == projection.target_path
    assert parse_projection(projection.canonical_bytes) == projection
    assert projection.section_body_sha256 == (
        __import__("hashlib").sha256(
            projection.section_body.encode("utf-8")
        ).hexdigest()
    )


def test_grounded_summary_requires_grounding_identity_for_claims() -> None:
    bundle, issue, commit = _bundle()
    value = json.loads(_summary_bytes(bundle, issue, commit))
    value["grounding_output_sha256s"] = []

    with pytest.raises(
        DailyProgressError,
        match="requires grounding output",
    ):
        parse_grounded_summary(
            (
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode(),
            bundle=bundle,
        )


def test_grounded_summary_rejects_wrong_evidence_binding() -> None:
    bundle, issue, commit = _bundle()
    value = json.loads(_summary_bytes(bundle, issue, commit))
    value["evidence_bundle_sha256"] = "f" * 64

    with pytest.raises(
        DailyProgressError,
        match="does not bind",
    ):
        parse_grounded_summary(
            (
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode(),
            bundle=bundle,
        )


def test_prepare_replaces_only_owned_h1_subtree_and_ignores_fenced_heading():
    _, projection = _projection()
    before = _daily(
        "~~~text\n"
        "# Project Progress\n"
        "fake heading\n"
        "~~~\n"
        "# Project Progress\n"
        "Old generated body.\n"
        "## Nested\n"
        "Old nested body.\n"
    )

    disposition, desired = prepare_daily_update(
        projection,
        before,
    )

    assert disposition == "apply"
    text = desired.decode()
    assert "Human note." in text
    assert "fake heading" in text
    assert "Old generated body." not in text
    assert "Old nested body." not in text
    assert projection.section_body in text
    assert "- [ ] Human task" in text


def test_prepare_preserves_crlf_and_unrelated_bytes() -> None:
    _, projection = _projection()
    before = _daily(
        "# Project Progress\nOld.\n",
        eol="\r\n",
    )

    disposition, desired = prepare_daily_update(
        projection,
        before,
    )

    assert disposition == "apply"
    assert b"\r\n" in desired
    assert b"\n" not in desired.replace(b"\r\n", b"")
    assert b"Human note.\r\n" in desired
    assert b"- [ ] Human task\r\n" in desired


def test_prepare_requires_exactly_one_visible_progress_heading() -> None:
    _, projection = _projection()
    duplicate = _daily(
        "# Project Progress\nOne.\n"
        "# Project Progress\nTwo.\n"
    )

    with pytest.raises(
        DailyProgressConflict,
        match="exactly one",
    ):
        prepare_daily_update(projection, duplicate)

    missing = _daily("# Other\nNothing.\n")
    with pytest.raises(
        DailyProgressConflict,
        match="exactly one",
    ):
        prepare_daily_update(projection, missing)


def test_prepare_requires_daily_review_type() -> None:
    _, projection = _projection()
    before = _daily(
        "# Project Progress\nOld.\n",
        daily_type="note",
    )

    with pytest.raises(
        DailyProgressConflict,
        match="daily-review",
    ):
        prepare_daily_update(projection, before)


class _WebDAV:
    def __init__(
        self,
        content: bytes | None,
        *,
        put_status: int = 204,
        apply_on_put: bool = True,
        network_on_put: bool = False,
    ) -> None:
        self.content = content
        self.etag = '"d1"'
        self.put_status = put_status
        self.apply_on_put = apply_on_put
        self.network_on_put = network_on_put
        self.calls = []
        self.puts = 0

    def __call__(
        self,
        *,
        method,
        target_url,
        username,
        password,
        headers=None,
        body=None,
        timeout,
        response_limit,
    ):
        self.calls.append((method, target_url, headers, body))
        if method == "GET":
            if self.content is None:
                return HTTPResponse(
                    status=404,
                    body=b"",
                    etag=None,
                )
            return HTTPResponse(
                status=200,
                body=self.content,
                etag=self.etag,
            )
        if method == "PUT":
            self.puts += 1
            assert headers["If-Match"] == self.etag
            if self.apply_on_put and body is not None:
                self.content = body
                self.etag = '"d2"'
            if self.network_on_put:
                raise PromotionTransportNetworkError(
                    "lost response"
                )
            return HTTPResponse(
                status=self.put_status,
                body=b"",
                etag=self.etag,
            )
        raise AssertionError(method)


def test_apply_uses_etag_cas_and_exact_post_get() -> None:
    _, projection = _projection()
    remote = _WebDAV(
        _daily("# Project Progress\nOld.\n")
    )

    result = apply_projection(
        projection,
        base_url="https://nextcloud.example/remote.php/dav/files/writer",
        username="writer",
        password="secret",
        transport=remote,
    )

    assert result.outcome == "applied"
    assert remote.puts == 1
    assert remote.content is not None
    assert projection.section_body.encode() in remote.content

    again = apply_projection(
        projection,
        base_url="https://nextcloud.example/remote.php/dav/files/writer",
        username="writer",
        password="secret",
        transport=remote,
    )
    assert again.outcome == "already_desired"
    assert remote.puts == 1


def test_missing_daily_is_retryable_not_created() -> None:
    _, projection = _projection()
    remote = _WebDAV(None)

    with pytest.raises(
        DailyProgressTargetMissing,
    ) as exc:
        apply_projection(
            projection,
            base_url="https://nextcloud.example/remote.php/dav/files/writer",
            username="writer",
            password="secret",
            transport=remote,
        )

    assert exc.value.reason_code == "target_missing"
    assert remote.puts == 0


def test_etag_412_is_conflict() -> None:
    _, projection = _projection()
    remote = _WebDAV(
        _daily("# Project Progress\nOld.\n"),
        put_status=412,
        apply_on_put=False,
    )

    with pytest.raises(
        DailyProgressConflict,
        match="precondition",
    ) as exc:
        apply_projection(
            projection,
            base_url="https://nextcloud.example/remote.php/dav/files/writer",
            username="writer",
            password="secret",
            transport=remote,
        )

    assert exc.value.reason_code == "etag_cas_conflict"


@pytest.mark.parametrize("network", [False, True])
def test_ambiguous_put_recovers_only_from_exact_desired_bytes(
    network: bool,
) -> None:
    _, projection = _projection()
    remote = _WebDAV(
        _daily("# Project Progress\nOld.\n"),
        put_status=503,
        apply_on_put=True,
        network_on_put=network,
    )

    result = apply_projection(
        projection,
        base_url="https://nextcloud.example/remote.php/dav/files/writer",
        username="writer",
        password="secret",
        transport=remote,
    )

    assert result.outcome == "recovered"


def test_ambiguous_unchanged_remote_remains_retryable_error() -> None:
    _, projection = _projection()
    remote = _WebDAV(
        _daily("# Project Progress\nOld.\n"),
        put_status=503,
        apply_on_put=False,
    )

    with pytest.raises(
        DailyProgressError,
        match="ambiguous",
    ) as exc:
        apply_projection(
            projection,
            base_url="https://nextcloud.example/remote.php/dav/files/writer",
            username="writer",
            password="secret",
            transport=remote,
        )

    assert exc.value.reason_code == "ambiguous_transport"


def test_transport_result_is_idempotent_for_same_projection(
    tmp_path: Path,
) -> None:
    _, projection = _projection()
    remote = _WebDAV(
        _daily("# Project Progress\nOld.\n")
    )
    result = apply_projection(
        projection,
        base_url="https://nextcloud.example/remote.php/dav/files/writer",
        username="writer",
        password="secret",
        transport=remote,
    )
    path = tmp_path / "result.json"

    first = persist_transport_result(path, result)
    second = persist_transport_result(path, result)

    assert first == second
    value = json.loads(first)
    assert value["projection_sha256"] == projection.sha256
