from __future__ import annotations

import pytest

from obsidian_automation.knowledge_note_layout import (
    KNOWLEDGE_METADATA_EMBED,
    NOTE_LAYOUT_VERSION,
    render_knowledge_body,
)


def test_layout_has_a_version_and_matches_core_embed() -> None:
    assert NOTE_LAYOUT_VERSION == "knowledge-note-layout-v1"
    assert KNOWLEDGE_METADATA_EMBED == "```meta-bind-embed\n[[knowledge-meta]]\n```\n"


@pytest.mark.parametrize("ending", ["", "\n", "\n\n\n"])
def test_adds_ui_without_relying_on_provider_and_normalizes_final_lf(ending: str) -> None:
    assert render_knowledge_body("# Body\n\nExact text." + ending) == (
        KNOWLEDGE_METADATA_EMBED + "\n# Body\n\nExact text.\n"
    )


@pytest.mark.parametrize("count", [1, 2, 3])
def test_exact_leading_scaffold_is_not_duplicated(count: int) -> None:
    body = "\n" + (KNOWLEDGE_METADATA_EMBED + "\n") * count + "# Body\n"
    rendered = render_knowledge_body(body)
    assert rendered == KNOWLEDGE_METADATA_EMBED + "\n# Body\n"
    assert render_knowledge_body(rendered) == rendered


@pytest.mark.parametrize("body", [
    "", "\n", "  ", KNOWLEDGE_METADATA_EMBED,
    KNOWLEDGE_METADATA_EMBED.rstrip("\n"),
    KNOWLEDGE_METADATA_EMBED + "\n" + KNOWLEDGE_METADATA_EMBED,
])
def test_ui_alone_is_not_semantic_note_content(body: str) -> None:
    with pytest.raises(ValueError):
        render_knowledge_body(body)


@pytest.mark.parametrize("body", [
    "\n\n# Body\n\nTrailing Markdown spaces.  \n",
    "````markdown\n" + KNOWLEDGE_METADATA_EMBED + "````\n",
    "> ```meta-bind-embed\n> [[knowledge-meta]]\n> ```\n",
    "```meta-bind-embed\n[[other-meta]]\n```\n# Body\n",
    "# Explanation\n\n" + KNOWLEDGE_METADATA_EMBED,
])
def test_does_not_rewrite_examples_other_embeds_or_body_bytes(body: str) -> None:
    assert render_knowledge_body(body) == KNOWLEDGE_METADATA_EMBED + "\n" + body
