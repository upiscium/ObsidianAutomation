from __future__ import annotations

import json

from obsidian_automation.github_project_overview import (
    BoundIssueRef,
    OverviewItem,
    make_overview_proposal,
    parse_overview_proposal,
    render_status_note,
)
from obsidian_automation.github_project_overview_queue import bound_issues_from_body


def test_closing_references_extract_bound_issues_without_plain_mentions() -> None:
    refs = bound_issues_from_body(
        "upiscium/Test",
        """
Closes #12
Fixes upiscium/Other#3
Resolves https://github.com/upiscium/Test/issues/99
Mention only: #777
Closes #12
""",
    )

    assert refs == (
        BoundIssueRef(repository="upiscium/Other", number=3),
        BoundIssueRef(repository="upiscium/Test", number=12),
        BoundIssueRef(repository="upiscium/Test", number=99),
    )


def test_pull_request_metadata_is_rendered_for_dataview() -> None:
    proposal = make_overview_proposal(
        project_path="10-Project/Test/Test.md",
        repository="upiscium/Test",
        issues=[],
        pull_requests=[
            OverviewItem(
                number=42,
                title='Quoted "title"',
                draft=True,
                bound_issues=(
                    BoundIssueRef(repository="upiscium/Test", number=12),
                    BoundIssueRef(repository="upiscium/Other", number=3),
                ),
            )
        ],
    )

    note = render_status_note(
        proposal,
        None,
        workspace="[[03-Workspace/Lab/Lab|Lab]]",
    ).decode("utf-8")

    assert "github_pull_requests:" in note
    assert "  - number: 42" in note
    assert '    title: "Quoted \\"title\\""' in note
    assert "    status: draft" in note
    assert '    url: "https://github.com/upiscium/Test/pull/42"' in note
    assert '      - repository: "upiscium/Test"' in note
    assert '        url: "https://github.com/upiscium/Test/issues/12"' in note
    assert '      - repository: "upiscium/Other"' in note
    assert '        url: "https://github.com/upiscium/Other/issues/3"' in note


def test_old_proposal_without_bound_issues_remains_parseable() -> None:
    raw = {
        "event": "project-overview-desired",
        "project": "10-Project/Test/Test.md",
        "repository": "upiscium/Test",
        "issues": [],
        "pull_requests": [
            {
                "number": 42,
                "title": "Legacy queued PR",
                "draft": False,
            }
        ],
    }

    parsed = parse_overview_proposal(
        (json.dumps(raw, separators=(",", ":")) + "\n").encode("utf-8")
    )

    assert parsed.pull_requests == (
        OverviewItem(number=42, title="Legacy queued PR", draft=False),
    )
