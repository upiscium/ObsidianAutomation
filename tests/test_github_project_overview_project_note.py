from __future__ import annotations

from obsidian_automation.core_promotion_transport import HTTPResponse
from obsidian_automation.github_project_overview import (
    MANAGED_END,
    MANAGED_START,
    OverviewItem,
    ProjectOverviewConflict,
    apply_project_overview,
    make_overview_proposal,
    render_status_note,
)


def _proposal():
    return make_overview_proposal(
        project_path="10-Project/Terreate/Terreate.md",
        repository="upiscium/Terreate",
        issues=[OverviewItem(203, "Issue")],
        pull_requests=[],
    )


def test_new_status_note_uses_obsidian_core_project_note_schema() -> None:
    rendered = render_status_note(
        _proposal(),
        None,
        workspace="[[03-Workspace/Research/Research|Research]]",
    ).decode()

    assert rendered.startswith(
        "---\n"
        "type: project-note\n"
        'project: "[[10-Project/Terreate/Terreate|Terreate]]"\n'
        'workspace: "[[03-Workspace/Research/Research|Research]]"\n'
        "category: list\n"
        "lifecycle: active\n"
        "aliases: []\n"
        "tags: []\n"
        "github_repo: upiscium/Terreate\n"
        "github_status_managed: true\n"
        "---\n"
    )
    assert MANAGED_START in rendered
    assert MANAGED_END in rendered


def test_legacy_github_status_is_migrated_without_losing_checkbox_or_notes() -> None:
    legacy = (
        "---\n"
        "type: github-status\n"
        'project: "[[Terreate]]"\n'
        "github_repo: upiscium/Terreate\n"
        "---\n\n"
        "# GitHub Status\n\n"
        f"{MANAGED_START}\n"
        "## Issues\n"
        "- [x] [#203 Old title](https://github.com/upiscium/Terreate/issues/203) <!-- github:issue:203 -->\n\n"
        "## Pull Requests\n"
        "- _No open pull requests._\n"
        f"{MANAGED_END}\n\n"
        "## Notes\n\n"
        "keep this human note\n"
    ).encode()

    rendered = render_status_note(
        _proposal(),
        legacy,
        workspace="[[03-Workspace/Research/Research|Research]]",
    ).decode()

    assert "type: project-note" in rendered
    assert 'project: "[[10-Project/Terreate/Terreate|Terreate]]"' in rendered
    assert 'workspace: "[[03-Workspace/Research/Research|Research]]"' in rendered
    assert "category: list" in rendered
    assert "lifecycle: active" in rendered
    assert "- [x] [#203 Issue]" in rendered
    assert "keep this human note" in rendered
    assert "type: github-status" not in rendered


def test_unowned_project_note_status_still_fails_closed() -> None:
    existing = (
        "---\n"
        "type: project-note\n"
        'project: "[[10-Project/Terreate/Terreate|Terreate]]"\n'
        "lifecycle: active\n"
        "github_repo: upiscium/Terreate\n"
        "---\n\n"
        f"{MANAGED_START}\n{MANAGED_END}\n"
    ).encode()

    try:
        render_status_note(_proposal(), existing)
    except ProjectOverviewConflict as exc:
        assert "not automation-managed" in str(exc)
    else:
        raise AssertionError("unowned project-note Status.md must fail closed")


class _WebDAV:
    def __init__(self) -> None:
        self.project = (
            "---\n"
            "type: project\n"
            "status: running\n"
            'workspace: "[[03-Workspace/Research/Research|Research]]"\n'
            "github_repo: upiscium/Terreate\n"
            "github_watch: true\n"
            "---\n"
        ).encode()
        self.status: bytes | None = None

    def __call__(
        self,
        *,
        method: str,
        target_url: str,
        username: str,
        password: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float,
        response_limit: int,
    ) -> HTTPResponse:
        if method == "GET" and target_url.endswith("/Terreate/Terreate.md"):
            return HTTPResponse(200, self.project, '"project"')
        if method == "GET" and target_url.endswith("/Terreate/Status.md"):
            if self.status is None:
                return HTTPResponse(404, b"", None)
            return HTTPResponse(200, self.status, '"status"')
        if method == "PUT" and target_url.endswith("/Terreate/Status.md"):
            assert headers is not None
            assert headers.get("If-None-Match") == "*"
            assert body is not None
            self.status = body
            return HTTPResponse(204, b"", '"status"')
        raise AssertionError((method, target_url))


def test_apply_inherits_workspace_from_canonical_project() -> None:
    remote = _WebDAV()

    result = apply_project_overview(
        _proposal(),
        base_url="https://nextcloud.example/remote.php/dav/files/writer",
        username="writer",
        password="secret",
        transport=remote,
    )

    assert result.outcome == "created"
    assert remote.status is not None
    rendered = remote.status.decode()
    assert 'workspace: "[[03-Workspace/Research/Research|Research]]"' in rendered
    assert 'project: "[[10-Project/Terreate/Terreate|Terreate]]"' in rendered
