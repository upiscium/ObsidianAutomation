from __future__ import annotations

from collections.abc import Sequence

import pytest

from obsidian_automation.review_intake import (
    ReviewIntakeError,
    extract_review_decision,
)


CASE = "a" * 64
SOURCE = "b" * 64
PROPOSAL = "c" * 64
MUTATION = "d" * 64
EVALUATION = "e" * 64
TARGET = f"04-AI/50-Review/{CASE}.md"
BODY = "# Human Review\n\nThe candidate is ready for review."

PROTECTED_LINES = (
    "type: ai-pipeline-projection",
    f"ai_case_id: {CASE}",
    "ai_stage: review",
    "ai_status: awaiting_human_review",
    "source_kind: evaluation_record",
    f"source_sha256: {SOURCE}",
    "created_at: 2026-09-21T00:01:00Z",
    f"proposal_sha256: {PROPOSAL}",
    f"mutation_sha256: {MUTATION}",
    f"evaluation_sha256: {EVALUATION}",
    f"target_path: {TARGET}",
    "recommendation: proceed",
)


def _document(
    frontmatter_lines: Sequence[str],
    *,
    body: str = BODY,
) -> str:
    return "\n".join(("---", *frontmatter_lines, "---", "", body, ""))


def _review(
    value: str,
    *,
    protected_lines: Sequence[str] = PROTECTED_LINES,
    body: str = BODY,
) -> str:
    return _document((*protected_lines, f"review_request: {value}"), body=body)


def _replace_field(
    lines: Sequence[str],
    field: str,
    replacement: str,
) -> tuple[str, ...]:
    prefix = f"{field}:"
    matches = [line for line in lines if line.startswith(prefix)]
    assert len(matches) == 1
    return tuple(replacement if line.startswith(prefix) else line for line in lines)


def test_exact_blank_review_request_is_waiting() -> None:
    expected = _review("")

    assert extract_review_decision(expected, expected.encode("utf-8")) is None


@pytest.mark.parametrize("serialized", ["", '""', "''", "null", "Null", "NULL", "~"])
def test_expected_blank_and_null_equivalents_are_pending(serialized: str) -> None:
    expected = _review(serialized)
    remote = _review("reject")

    assert extract_review_decision(expected, remote.encode("utf-8")) == "reject"


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_raw_line_decisions_are_extracted(decision: str) -> None:
    expected = _review("")
    remote = _review(decision)

    assert extract_review_decision(expected, remote.encode("utf-8")) == decision


def test_meta_bind_reserialization_allows_reordered_equivalent_frontmatter() -> None:
    expected = _document(
        (
            *PROTECTED_LINES,
            "review_request: ",
        )
    )
    remote = _document(
        (
            f'target_path: "{TARGET}"',
            'recommendation: "proceed"',
            f'source_sha256: "{SOURCE}"',
            'created_at: "2026-09-21T00:01:00Z"',
            f'proposal_sha256: "{PROPOSAL}"',
            f'mutation_sha256: "{MUTATION}"',
            f'evaluation_sha256: "{EVALUATION}"',
            'source_kind: "evaluation_record"',
            'ai_status: "awaiting_human_review"',
            'ai_stage: "review"',
            f'ai_case_id: "{CASE}"',
            'type: "ai-pipeline-projection"',
            "review_request: 'approve'",
        )
    )

    assert extract_review_decision(expected, remote.encode("utf-8")) == "approve"


@pytest.mark.parametrize(
    ("serialized", "decision"),
    [
        ("", None),
        ("   ", None),
        ('""', None),
        ("''", None),
        ("null", None),
        ("Null", None),
        ("NULL", None),
        ("~", None),
        ("approve", "approve"),
        ('"approve"', "approve"),
        ("'approve'", "approve"),
        ("reject", "reject"),
        ('"reject"', "reject"),
        ("'reject'", "reject"),
    ],
)
def test_review_request_blank_null_and_decision_equivalents(
    serialized: str,
    decision: str | None,
) -> None:
    expected = _review("")
    remote = _review(serialized)

    assert extract_review_decision(expected, remote.encode("utf-8")) == decision


def test_body_change_is_rejected() -> None:
    expected = _review("")
    remote = _review("approve", body=BODY + "\n\nHuman edit.")

    with pytest.raises(ReviewIntakeError):
        extract_review_decision(expected, remote.encode("utf-8"))


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("type", "type: different-projection"),
        ("ai_stage", "ai_stage: evaluation"),
        ("source_sha256", f"source_sha256: {'c' * 64}"),
        ("target_path", "target_path: 04-AI/50-Review/other.md"),
        ("recommendation", "recommendation: reject"),
    ],
)
def test_protected_value_change_is_rejected(field: str, replacement: str) -> None:
    expected = _review("")
    changed = _replace_field(PROTECTED_LINES, field, replacement)
    remote = _review("approve", protected_lines=changed)

    with pytest.raises(ReviewIntakeError):
        extract_review_decision(expected, remote.encode("utf-8"))


def test_frontmatter_key_addition_is_rejected() -> None:
    expected = _review("")
    remote = _review(
        "approve",
        protected_lines=(*PROTECTED_LINES, "unexpected: value"),
    )

    with pytest.raises(ReviewIntakeError):
        extract_review_decision(expected, remote.encode("utf-8"))


def test_frontmatter_key_removal_is_rejected() -> None:
    expected = _review("")
    remaining = tuple(
        line for line in PROTECTED_LINES if not line.startswith("recommendation:")
    )
    remote = _review("approve", protected_lines=remaining)

    with pytest.raises(ReviewIntakeError):
        extract_review_decision(expected, remote.encode("utf-8"))


def test_duplicate_review_request_is_rejected() -> None:
    expected = _review("")
    remote = _document(
        (
            *PROTECTED_LINES,
            "review_request: ",
            "review_request: approve",
        )
    )

    with pytest.raises(ReviewIntakeError):
        extract_review_decision(expected, remote.encode("utf-8"))


def test_duplicate_protected_key_is_rejected() -> None:
    expected = _review("")
    remote = _document(
        (
            *PROTECTED_LINES,
            "type: duplicate",
            "review_request: approve",
        )
    )

    with pytest.raises(ReviewIntakeError):
        extract_review_decision(expected, remote.encode("utf-8"))


@pytest.mark.parametrize(
    ("shape", "replacement"),
    [
        ("nested", "target_path:\n  child: changed"),
        ("list", "target_path:\n  - changed"),
        ("map", "target_path: {child: changed}"),
        ("multiline", "target_path: |-\n  changed"),
        ("tag", "target_path: !!str changed"),
        ("hex-number", "target_path: 0x10"),
        ("octal-number", "target_path: 0123"),
        ("sexagesimal-number", "target_path: 1:2"),
    ],
)
def test_complex_protected_values_are_rejected(shape: str, replacement: str) -> None:
    expected = _review("")
    changed = _replace_field(PROTECTED_LINES, "target_path", replacement)
    remote = _review("approve", protected_lines=changed)

    with pytest.raises(ReviewIntakeError):
        extract_review_decision(expected, remote.encode("utf-8"))


def test_anchor_in_protected_frontmatter_is_rejected() -> None:
    expected = _review("")
    changed = _replace_field(
        PROTECTED_LINES,
        "target_path",
        "target_path: &changed changed",
    )
    remote = _review("approve", protected_lines=changed)

    with pytest.raises(ReviewIntakeError):
        extract_review_decision(expected, remote.encode("utf-8"))


def test_alias_in_protected_frontmatter_is_rejected() -> None:
    expected = _review("")
    changed = tuple(
        "type: &shared ai-pipeline-projection"
        if line == "type: ai-pipeline-projection"
        else "target_path: *shared"
        if line.startswith("target_path:")
        else line
        for line in PROTECTED_LINES
    )
    remote = _review("approve", protected_lines=changed)

    with pytest.raises(ReviewIntakeError):
        extract_review_decision(expected, remote.encode("utf-8"))


def test_malformed_utf8_is_rejected() -> None:
    expected = _review("")
    remote = _review("approve").encode("utf-8") + b"\xff"

    with pytest.raises(ReviewIntakeError):
        extract_review_decision(expected, remote)


def test_lone_cr_line_ending_is_rejected() -> None:
    expected = _review("")
    remote = _review("approve").replace("\n", "\r", 1).encode("utf-8")

    with pytest.raises(ReviewIntakeError):
        extract_review_decision(expected, remote)


OLD_146_BODY = (
    "# Human Review\n\n"
    f"Target: `{TARGET}`\n\n"
    "## Decision\n\n"
    "Review request is pending."
)
NEW_147_BODY = (
    "# Human Review\n\n"
    "## Candidate\n\n"
    "```markdown\n# Candidate\n```\n\n"
    "## Decision\n\n"
    "The Meta Bind control is pending."
)


@pytest.mark.parametrize(
    "body",
    [OLD_146_BODY, NEW_147_BODY],
    ids=["issue-146", "issue-147-plus"],
)
def test_old_and_new_review_bodies_remain_compatible(body: str) -> None:
    expected = _review("", body=body)
    remote = _review("reject", body=body)

    assert extract_review_decision(expected, remote.encode("utf-8")) == "reject"


PRODUCTION_DIAGNOSTIC_BODY = (
    "# Human Review\n\n"
    "## Decision\n\n"
    "The requested decision is recorded by the reviewer."
)


def test_production_diagnostic_fixture_returns_reject() -> None:
    # Production diagnostic: body_exact=True, key_set_exact=True,
    # semantic_changed_keys=review_request, protected_changed_count=0.
    expected = _document(
        (
            *PROTECTED_LINES,
            "review_request: ",
        ),
        body=PRODUCTION_DIAGNOSTIC_BODY,
    )
    remote = _document(
        (
            "review_request: reject",
            f'target_path: "{TARGET}"',
            'recommendation: "proceed"',
            f'source_sha256: "{SOURCE}"',
            'created_at: "2026-09-21T00:01:00Z"',
            f'proposal_sha256: "{PROPOSAL}"',
            f'mutation_sha256: "{MUTATION}"',
            f'evaluation_sha256: "{EVALUATION}"',
            'source_kind: "evaluation_record"',
            'ai_status: "awaiting_human_review"',
            'ai_stage: "review"',
            f'ai_case_id: "{CASE}"',
            'type: "ai-pipeline-projection"',
        ),
        body=PRODUCTION_DIAGNOSTIC_BODY,
    )

    assert extract_review_decision(expected, remote.encode("utf-8")) == "reject"
