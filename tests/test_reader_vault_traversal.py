from __future__ import annotations

import os
from pathlib import Path

from obsidian_automation.context_bundle import build_context_bundle
from obsidian_automation.knowledge_index import build_knowledge_index


def _knowledge_note() -> str:
    return (
        "---\n"
        "type: knowledge-note\n"
        "status: active\n"
        "category: summary\n"
        "maturity: draft\n"
        "source_type: self\n"
        "---\n"
        "# Reader traversal regression\n\n"
        "The Vault root is intentionally execute-only.\n"
    )


def test_index_reads_knowledge_with_execute_only_vault_root(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    knowledge = vault / "11-Knowledge"
    knowledge.mkdir(parents=True)
    note = knowledge / "Traversal.md"
    note.write_text(_knowledge_note(), encoding="utf-8")

    original_mode = os.stat(vault).st_mode & 0o777
    os.chmod(vault, 0o111)
    try:
        index = build_knowledge_index(vault)
    finally:
        os.chmod(vault, original_mode)

    assert [doc.path for doc in index.documents] == ["11-Knowledge/Traversal.md"]


def test_context_reads_source_with_execute_only_vault_root(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    knowledge = vault / "11-Knowledge"
    knowledge.mkdir(parents=True)
    note = knowledge / "Traversal.md"
    content = _knowledge_note()
    note.write_text(content, encoding="utf-8")

    original_mode = os.stat(vault).st_mode & 0o777
    os.chmod(vault, 0o111)
    try:
        bundle = build_context_bundle(
            vault,
            query="reader traversal",
            source_paths=["11-Knowledge/Traversal.md"],
            created_at="2026-08-22T00:00:00Z",
        )
    finally:
        os.chmod(vault, original_mode)

    assert bundle.sources[0].path == "11-Knowledge/Traversal.md"
    assert bundle.sources[0].content == content
