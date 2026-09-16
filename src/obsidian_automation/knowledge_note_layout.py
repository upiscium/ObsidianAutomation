"""Deterministic presentation scaffold for newly generated Knowledge Notes.

This module assembles proposal content, never edits existing notes or artifacts.
"""
from __future__ import annotations


NOTE_LAYOUT_VERSION = "knowledge-note-layout-v1"
KNOWLEDGE_METADATA_EMBED = "```meta-bind-embed\n[[knowledge-meta]]\n```\n"


def render_knowledge_body(body: str) -> str:
    """Prepend the Core metadata UI while preserving semantic body content.

    Only exact copies of our scaffold at the start of the body are folded into
    the owned scaffold. Quoted examples, other embeds, and later occurrences
    are deliberately not rewritten. This is not a Markdown sanitizer.
    """
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Knowledge body must not be empty")
    # Keep the existing final-newline contract. Do not strip meaningful spaces
    # or leading blank lines from ordinary Markdown bodies.
    content = body.rstrip("\n") + "\n"
    while content.lstrip("\n").startswith(KNOWLEDGE_METADATA_EMBED):
        content = content.lstrip("\n")[len(KNOWLEDGE_METADATA_EMBED) :].lstrip("\n")
    if not content.strip():
        raise ValueError("Knowledge body must contain content beyond metadata UI")
    return KNOWLEDGE_METADATA_EMBED + "\n" + content
