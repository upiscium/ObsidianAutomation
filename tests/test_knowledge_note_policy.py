from __future__ import annotations

import pytest

from obsidian_automation.canonical_mutation import CreateNoteMutation, MutationValidationError
from obsidian_automation.knowledge_note_policy import validate_knowledge_note_v0


def _mutation(*, target: str = "11-Knowledge/example.md", content: str | None = None):
    if content is None:
        content = (
            "---\n"
            "type: knowledge-note\n"
            "status: active\n"
            "category: explanation\n"
            "maturity: draft\n"
            "source_type: official\n"
            "---\n"
            "# About\n\n"
            "Validated body.\n"
        )
    return CreateNoteMutation(
        contract_version=1,
        operation="create_note",
        mutation_id="knowledge-policy-test",
        target_path=target,
        content=content,
    )


def _replace(content: str, old: str, new: str) -> str:
    assert old in content
    return content.replace(old, new, 1)


def test_accepts_current_obsidian_core_creation_shape() -> None:
    validate_knowledge_note_v0(_mutation())


def test_allows_blank_category_but_not_literal_none() -> None:
    valid = _mutation().content.replace("category: explanation", "category:")
    validate_knowledge_note_v0(_mutation(content=valid))

    invalid = valid.replace("category:", "category: none")
    with pytest.raises(MutationValidationError, match="category"):
        validate_knowledge_note_v0(_mutation(content=invalid))


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("status: active", "status: outdated", "status must be active"),
        ("maturity: draft", "maturity: stable", "maturity must be draft"),
        ("source_type: official", "source_type:", "source_type is not allowed"),
        ("category: explanation", "category: invented", "category is not allowed"),
    ],
)
def test_rejects_creation_states_outside_v0(old: str, new: str, message: str) -> None:
    content = _replace(_mutation().content, old, new)
    with pytest.raises(MutationValidationError, match=message):
        validate_knowledge_note_v0(_mutation(content=content))


def test_rejects_unknown_or_complex_frontmatter() -> None:
    unknown = _mutation().content.replace("source_type: official\n", "source_type: official\nuid: x\n")
    with pytest.raises(MutationValidationError, match="keys do not match policy"):
        validate_knowledge_note_v0(_mutation(content=unknown))

    quoted = _mutation().content.replace("category: explanation", 'category: "explanation"')
    with pytest.raises(MutationValidationError, match="plain scalar"):
        validate_knowledge_note_v0(_mutation(content=quoted))


def test_rejects_hidden_or_cross_platform_unsafe_target() -> None:
    with pytest.raises(MutationValidationError, match="hidden path"):
        validate_knowledge_note_v0(_mutation(target="11-Knowledge/.hidden/example.md"))

    with pytest.raises(MutationValidationError, match="unsafe character"):
        validate_knowledge_note_v0(_mutation(target="11-Knowledge/bad?.md"))


def test_rejects_empty_body_and_crlf() -> None:
    empty = (
        "---\n"
        "type: knowledge-note\n"
        "status: active\n"
        "category:\n"
        "maturity: draft\n"
        "source_type: self\n"
        "---\n"
    )
    with pytest.raises(MutationValidationError, match="body must not be empty"):
        validate_knowledge_note_v0(_mutation(content=empty))

    with pytest.raises(MutationValidationError, match="LF line endings"):
        validate_knowledge_note_v0(_mutation(content=_mutation().content.replace("\n", "\r\n")))
