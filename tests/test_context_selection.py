from __future__ import annotations

from pathlib import Path

import pytest

from obsidian_automation.artifact_lifecycle import ArtifactLifecycleError
from obsidian_automation.context_bundle import load_context_bundle
from obsidian_automation.context_selection import (
    CONTEXT_SELECTION_POLICY_VERSION,
    MIN_QUERY_COVERAGE,
    RELATIVE_SCORE_CUTOFF,
    retrieve_context,
    select_context_candidates,
)
from obsidian_automation.knowledge_index import (
    RankedDocument,
    build_knowledge_index,
    rank_documents,
    store_knowledge_index,
)


def _note(body: str) -> str:
    return (
        "---\n"
        "type: knowledge-note\n"
        "status: active\n"
        "category: summary\n"
        "maturity: verified\n"
        "source_type: self\n"
        "---\n"
        f"{body}\n"
    )


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    (vault / "11-Knowledge").mkdir(parents=True)
    state = tmp_path / "state"
    (state / "04-Index").mkdir(parents=True)
    (state / "05-Context").mkdir()
    return vault, state


def test_policy_constants_are_fixed() -> None:
    assert CONTEXT_SELECTION_POLICY_VERSION == "bm25-coverage-relative-v0"
    assert MIN_QUERY_COVERAGE == 0.2
    assert RELATIVE_SCORE_CUTOFF == 0.8


def test_query_coverage_rejects_partial_hard_negative(tmp_path: Path) -> None:
    vault, _ = _roots(tmp_path)
    (vault / "11-Knowledge" / "Agents.md").write_text(
        _note("# Agents\nLLM agent coordination"),
        encoding="utf-8",
    )
    (vault / "11-Knowledge" / "Other.md").write_text(
        _note("# Other\nunrelated material"),
        encoding="utf-8",
    )

    index = build_knowledge_index(vault)
    query = "LLM agent sandbox security permission isolation"
    ranked = rank_documents(index, query)
    assert ranked

    selection = select_context_candidates(index, ranked, query=query, top_k=5)

    assert selection.eligible_count == 0
    assert selection.selected == ()
    assert selection.coverages[ranked[0].path].coverage < MIN_QUERY_COVERAGE


def test_relative_cutoff_uses_eligible_top1_score(tmp_path: Path) -> None:
    vault, _ = _roots(tmp_path)
    for name in ("A.md", "B.md", "C.md"):
        (vault / "11-Knowledge" / name).write_text(
            _note(f"# {name}\nalpha beta gamma"),
            encoding="utf-8",
        )

    index = build_knowledge_index(vault)
    docs = {doc.path: doc for doc in index.documents}
    ranked = (
        RankedDocument(
            path="11-Knowledge/A.md",
            content_sha256=docs["11-Knowledge/A.md"].content_sha256,
            score=10.0,
        ),
        RankedDocument(
            path="11-Knowledge/B.md",
            content_sha256=docs["11-Knowledge/B.md"].content_sha256,
            score=7.99,
        ),
        RankedDocument(
            path="11-Knowledge/C.md",
            content_sha256=docs["11-Knowledge/C.md"].content_sha256,
            score=7.0,
        ),
    )

    selection = select_context_candidates(
        index,
        ranked,
        query="alpha beta gamma",
        top_k=5,
    )

    assert [item.path for item in selection.selected] == ["11-Knowledge/A.md"]


def test_retrieval_emits_policy_metadata_and_exact_context(tmp_path: Path) -> None:
    vault, state = _roots(tmp_path)
    content = _note("# NixOS clangd\nflake Neovim clangd LSP troubleshooting")
    (vault / "11-Knowledge" / "clangd.md").write_text(content, encoding="utf-8")

    index_sha, _ = store_knowledge_index(state, build_knowledge_index(vault))
    result = retrieve_context(
        state,
        vault,
        index_sha256=index_sha,
        query="flake Neovim clangd LSP",
        top_k=5,
    )

    assert result["selection_policy"] == {
        "version": CONTEXT_SELECTION_POLICY_VERSION,
        "min_query_coverage": 0.2,
        "relative_score_cutoff": 0.8,
        "absolute_top1_score_gate": None,
    }
    assert result["matched_count"] == 1
    assert result["eligible_count"] == 1
    selected = result["selected"]
    assert isinstance(selected, list)
    assert selected[0]["path"] == "11-Knowledge/clangd.md"
    assert selected[0]["query_coverage"] >= MIN_QUERY_COVERAGE

    context = load_context_bundle(state, str(result["context_sha256"]))
    assert len(context.sources) == 1
    assert context.sources[0].content == content


def test_hard_negative_produces_empty_context_bundle(tmp_path: Path) -> None:
    vault, state = _roots(tmp_path)
    (vault / "11-Knowledge" / "Agents.md").write_text(
        _note("# Agents\nLLM agent coordination"),
        encoding="utf-8",
    )
    (vault / "11-Knowledge" / "Other.md").write_text(
        _note("# Other\nunrelated material"),
        encoding="utf-8",
    )

    index_sha, _ = store_knowledge_index(state, build_knowledge_index(vault))
    result = retrieve_context(
        state,
        vault,
        index_sha256=index_sha,
        query="LLM agent sandbox security permission isolation",
        top_k=5,
    )

    assert result["matched_count"] >= 1
    assert result["eligible_count"] == 0
    assert result["selected"] == []
    context = load_context_bundle(state, str(result["context_sha256"]))
    assert context.sources == ()


def test_policy_rejects_invalid_top_k(tmp_path: Path) -> None:
    vault, _ = _roots(tmp_path)
    index = build_knowledge_index(vault)

    with pytest.raises(ArtifactLifecycleError, match="top_k"):
        select_context_candidates(index, (), query="anything", top_k=0)
