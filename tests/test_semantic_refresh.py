from __future__ import annotations

from pathlib import Path

import pytest

from obsidian_automation.semantic_corpus import (
    build_semantic_corpus,
    store_semantic_corpus_manifest,
)
from obsidian_automation.semantic_index import (
    embed_semantic_plan_with_ollama,
    finalize_semantic_index,
    load_semantic_index_manifest,
    prepare_semantic_embedding_plan,
)
from obsidian_automation.semantic_refresh import (
    SemanticRefreshError,
    embed_refresh,
    finalize_refresh,
    load_active_semantic_index,
    prepare_refresh,
    resolve_active_semantic_index,
)


MODEL = "qwen3-embedding:0.6b"
MODEL_DIGEST = "a" * 64


def _note(frontmatter: str, body: str) -> str:
    return f"---\n{frontmatter}---\n{body}\n"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for root in (
        "00-DailyNote",
        "05-Idea",
        "10-Project",
        "11-Knowledge",
    ):
        (vault / root).mkdir(parents=True)

    (vault / "05-Idea" / "Idea.md").write_text(
        _note(
            "type: idea\n"
            "title: Semantic Idea\n"
            "created: 2026-10-04\n"
            "status: active\n",
            "# Semantic Idea\n"
            "Keep the semantic index bound to the current mirror.",
        ),
        encoding="utf-8",
    )

    project = vault / "10-Project" / "Planner"
    project.mkdir()
    (project / "Planner.md").write_text(
        _note(
            "type: project\n"
            "status: running\n",
            "# Project Summary\n"
            "Refresh semantic retrieval safely.",
        ),
        encoding="utf-8",
    )
    (project / "Design.md").write_text(
        _note(
            "type: project-note\n"
            "project: '[[10-Project/Planner/Planner|Planner]]'\n"
            "lifecycle: active\n",
            "# Design\n"
            "Use immutable vectors and a verified active binding.",
        ),
        encoding="utf-8",
    )

    (vault / "11-Knowledge" / "Knowledge.md").write_text(
        _note(
            "type: knowledge-note\n"
            "status: active\n"
            "category: system\n"
            "maturity: stable\n"
            "source_type: self\n",
            "# Knowledge\n"
            "Content-addressed indexes remain immutable.",
        ),
        encoding="utf-8",
    )
    return vault


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    index = state / "04-Index"
    for directory in (
        "semantic-corpus",
        "semantic-embedding-requests",
        "semantic-embedding-plans",
        "semantic-embedding-results",
        "semantic-embedding-result-sets",
        "semantic-index",
        "semantic-refresh",
        "semantic-refresh/reader",
        "semantic-refresh/embedder",
        "semantic-refresh/active",
    ):
        (index / directory).mkdir(parents=True, exist_ok=True)
    (state / "24-Locks" / "read-view").mkdir(parents=True)
    return state


def _transport(calls: list[tuple[str, object]]):
    def transport(
        base_url: str,
        *,
        method: str,
        path: str,
        payload,
        timeout: float,
    ):
        calls.append((path, payload))
        if path == "/api/tags":
            return {
                "models": [
                    {
                        "name": MODEL,
                        "model": MODEL,
                        "digest": MODEL_DIGEST,
                    }
                ]
            }
        assert path == "/api/embed"
        inputs = payload["input"]
        return {
            "model": MODEL,
            "embeddings": [
                [float(index + 1), float(len(text.encode("utf-8"))), 0.25]
                for index, text in enumerate(inputs)
            ],
        }

    return transport


def _initial_index(tmp_path: Path) -> tuple[Path, Path, str]:
    vault = _vault(tmp_path)
    state = _state(tmp_path)
    corpus = build_semantic_corpus(vault)
    corpus_sha, _ = store_semantic_corpus_manifest(state, corpus)
    plan_sha, _, _ = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=MODEL,
        model_revision=MODEL_DIGEST,
    )
    calls: list[tuple[str, object]] = []
    result_set_sha, _, _ = embed_semantic_plan_with_ollama(
        state,
        plan_sha256=plan_sha,
        base_url="http://127.0.0.1:11434",
        transport=_transport(calls),
    )
    index_sha, _, _ = finalize_semantic_index(
        state,
        vault,
        plan_sha256=plan_sha,
        result_set_sha256=result_set_sha,
    )
    return vault, state, index_sha


def test_noop_seed_activates_exact_current_index_without_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, index_sha = _initial_index(tmp_path)

    prepare_sha, prepared = prepare_refresh(
        state,
        vault,
        seed_index_sha256=index_sha,
        prepared_at="2026-10-04T00:00:00Z",
    )
    assert prepared.action == "noop"

    def forbidden(*args, **kwargs):
        raise AssertionError("noop refresh must not contact provider")

    monkeypatch.setattr(
        "obsidian_automation.semantic_refresh."
        "embed_semantic_plan_incremental_with_ollama",
        forbidden,
    )

    result_sha, embedded = embed_refresh(
        state,
        completed_at="2026-10-04T00:01:00Z",
    )
    assert embedded.action == "noop"
    assert embedded.prepare_sha256 == prepare_sha

    active, changed = finalize_refresh(
        state,
        vault,
        activated_at="2026-10-04T00:02:00Z",
    )
    assert changed is False
    assert active.semantic_index_sha256 == index_sha
    assert active.activation_source == "seed-current"
    assert active.embed_result_sha256 == result_sha
    assert resolve_active_semantic_index(state, vault) == index_sha


def test_changed_vault_incrementally_refreshes_and_advances_active_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, index_sha = _initial_index(tmp_path)

    prepare_refresh(
        state,
        vault,
        seed_index_sha256=index_sha,
        prepared_at="2026-10-04T00:00:00Z",
    )
    embed_refresh(state, completed_at="2026-10-04T00:01:00Z")
    finalize_refresh(state, vault, activated_at="2026-10-04T00:02:00Z")

    design = vault / "10-Project" / "Planner" / "Design.md"
    design.write_text(
        design.read_text(encoding="utf-8")
        + "\nNew semantic refresh evidence.\n",
        encoding="utf-8",
    )

    prepare_sha, prepared = prepare_refresh(
        state,
        vault,
        seed_index_sha256=None,
        prepared_at="2026-10-04T00:03:00Z",
    )
    assert prepared.action == "refresh"
    assert prepared.previous_index_sha256 == index_sha
    assert prepared.plan_sha256 is not None
    assert prepared.refresh_plan_sha256 is not None

    calls: list[tuple[str, object]] = []

    def incremental(*args, **kwargs):
        from obsidian_automation.semantic_index import (
            embed_semantic_plan_incremental_with_ollama,
        )

        return embed_semantic_plan_incremental_with_ollama(
            *args,
            **{
                **kwargs,
                "transport": _transport(calls),
            },
        )

    monkeypatch.setattr(
        "obsidian_automation.semantic_refresh."
        "embed_semantic_plan_incremental_with_ollama",
        incremental,
    )

    _result_sha, embedded = embed_refresh(
        state,
        completed_at="2026-10-04T00:04:00Z",
    )
    assert embedded.action == "refresh"
    assert embedded.prepare_sha256 == prepare_sha
    assert embedded.reused_count > 0
    assert embedded.embedded_count > 0
    assert any(path == "/api/embed" for path, _ in calls)

    active, changed = finalize_refresh(
        state,
        vault,
        activated_at="2026-10-04T00:05:00Z",
    )
    assert changed is True
    assert active.semantic_index_sha256 != index_sha
    assert active.previous_index_sha256 == index_sha
    assert resolve_active_semantic_index(state, vault) == active.semantic_index_sha256

    refreshed = load_semantic_index_manifest(
        state,
        active.semantic_index_sha256,
    )
    assert refreshed.corpus_manifest_sha256 == active.corpus_manifest_sha256


def test_stale_embed_handoff_cannot_advance_active_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, index_sha = _initial_index(tmp_path)

    prepare_refresh(
        state,
        vault,
        seed_index_sha256=index_sha,
        prepared_at="2026-10-04T00:00:00Z",
    )
    embed_refresh(state, completed_at="2026-10-04T00:01:00Z")
    finalize_refresh(state, vault, activated_at="2026-10-04T00:02:00Z")

    design = vault / "10-Project" / "Planner" / "Design.md"
    design.write_text(
        design.read_text(encoding="utf-8") + "\nFirst change.\n",
        encoding="utf-8",
    )
    prepare_refresh(
        state,
        vault,
        seed_index_sha256=None,
        prepared_at="2026-10-04T00:03:00Z",
    )

    calls: list[tuple[str, object]] = []

    def incremental(*args, **kwargs):
        from obsidian_automation.semantic_index import (
            embed_semantic_plan_incremental_with_ollama,
        )

        return embed_semantic_plan_incremental_with_ollama(
            *args,
            **{
                **kwargs,
                "transport": _transport(calls),
            },
        )

    monkeypatch.setattr(
        "obsidian_automation.semantic_refresh."
        "embed_semantic_plan_incremental_with_ollama",
        incremental,
    )
    embed_refresh(state, completed_at="2026-10-04T00:04:00Z")

    design.write_text(
        design.read_text(encoding="utf-8") + "\nSecond change.\n",
        encoding="utf-8",
    )
    prepare_refresh(
        state,
        vault,
        seed_index_sha256=None,
        prepared_at="2026-10-04T00:05:00Z",
    )

    with pytest.raises(SemanticRefreshError, match="handoff mismatch"):
        finalize_refresh(state, vault)

    active = load_active_semantic_index(state)
    assert active is not None
    assert active.semantic_index_sha256 == index_sha


def test_active_resolution_fails_closed_after_unrefreshed_vault_change(
    tmp_path: Path,
) -> None:
    vault, state, index_sha = _initial_index(tmp_path)

    prepare_refresh(
        state,
        vault,
        seed_index_sha256=index_sha,
        prepared_at="2026-10-04T00:00:00Z",
    )
    embed_refresh(state, completed_at="2026-10-04T00:01:00Z")
    finalize_refresh(state, vault, activated_at="2026-10-04T00:02:00Z")

    design = vault / "10-Project" / "Planner" / "Design.md"
    design.write_text(
        design.read_text(encoding="utf-8") + "\nUnrefreshed change.\n",
        encoding="utf-8",
    )

    with pytest.raises(SemanticRefreshError, match="stale"):
        resolve_active_semantic_index(state, vault)
