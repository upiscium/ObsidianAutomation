from __future__ import annotations

from pathlib import Path

import pytest

import obsidian_automation.semantic_refresh as semantic_refresh
from obsidian_automation.semantic_corpus import (
    build_semantic_corpus,
    store_semantic_corpus_manifest,
)
from obsidian_automation.semantic_index import (
    embed_semantic_plan_incremental_with_ollama,
    embed_semantic_plan_with_ollama,
    finalize_semantic_index,
    prepare_semantic_embedding_plan,
)
from obsidian_automation.semantic_refresh import (
    SemanticRefreshError,
    activate_semantic_index,
    embed_refresh,
    finalize_refresh,
    load_active_semantic_index_binding,
    prepare_refresh,
    resolve_active_semantic_index_sha,
)


MODEL = "qwen3-embedding:4b"
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
            "type: idea\nstatus: active\n",
            "# Idea\nReusable unchanged evidence.",
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
            "# Knowledge\nInitial semantic content.",
        ),
        encoding="utf-8",
    )
    return vault


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "04-Index").mkdir(parents=True)
    (state / "24-Locks" / "read-view").mkdir(parents=True)
    return state


def _transport(calls: list[str]):
    def transport(
        base_url: str,
        *,
        method: str,
        path: str,
        payload,
        timeout: float,
    ):
        calls.append(path)
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
        return {
            "model": MODEL,
            "embeddings": [
                [
                    float(index + 1),
                    float(len(text.encode("utf-8"))),
                    0.5,
                ]
                for index, text in enumerate(payload["input"])
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
    result_sha, _, _ = embed_semantic_plan_with_ollama(
        state,
        plan_sha256=plan_sha,
        base_url="http://127.0.0.1:11434",
        transport=_transport([]),
    )
    index_sha, _, _ = finalize_semantic_index(
        state,
        vault,
        plan_sha256=plan_sha,
        result_set_sha256=result_sha,
    )
    return vault, state, index_sha


def _patch_incremental_provider(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[str],
) -> None:
    original = embed_semantic_plan_incremental_with_ollama

    def wrapped(
        ai_root: Path,
        *,
        refresh_plan_sha256: str,
        base_url: str,
    ):
        return original(
            ai_root,
            refresh_plan_sha256=refresh_plan_sha256,
            base_url=base_url,
            transport=_transport(calls),
        )

    monkeypatch.setattr(
        semantic_refresh,
        "embed_semantic_plan_incremental_with_ollama",
        wrapped,
    )


def test_activate_and_unchanged_refresh_keep_exact_index(
    tmp_path: Path,
) -> None:
    vault, state, index_sha = _initial_index(tmp_path)

    binding = activate_semantic_index(
        state,
        vault,
        semantic_index_sha256=index_sha,
    )
    assert binding["semantic_index_sha256"] == index_sha
    assert resolve_active_semantic_index_sha(state) == index_sha

    prepared = prepare_refresh(state, vault)
    assert prepared["phase"] == "unchanged"

    embedded = embed_refresh(
        state,
        base_url="http://127.0.0.1:11434",
    )
    assert embedded["phase"] == "unchanged"

    finalized = finalize_refresh(state, vault)
    assert finalized["status"] == "unchanged"
    assert finalized["semantic_index_sha256"] == index_sha
    assert resolve_active_semantic_index_sha(state) == index_sha


def test_incremental_refresh_activates_only_finalized_current_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, index_sha = _initial_index(tmp_path)
    activate_semantic_index(
        state,
        vault,
        semantic_index_sha256=index_sha,
    )

    knowledge = vault / "11-Knowledge" / "Knowledge.md"
    knowledge.write_text(
        _note(
            "type: knowledge-note\n"
            "status: active\n"
            "category: system\n"
            "maturity: stable\n"
            "source_type: self\n",
            "# Knowledge\nChanged semantic content.",
        ),
        encoding="utf-8",
    )

    prepared = prepare_refresh(state, vault)
    assert prepared["phase"] == "prepared"
    assert prepared["previous_index_sha256"] == index_sha

    calls: list[str] = []
    _patch_incremental_provider(monkeypatch, calls)
    embedded = embed_refresh(
        state,
        base_url="http://127.0.0.1:11434",
    )
    assert embedded["reused_count"] >= 1
    assert embedded["embedded_count"] >= 1
    assert "/api/embed" in calls

    finalized = finalize_refresh(state, vault)
    assert finalized["status"] == "activated"
    assert finalized["previous_index_sha256"] == index_sha
    assert finalized["semantic_index_sha256"] != index_sha

    active = load_active_semantic_index_binding(state)
    assert (
        active["semantic_index_sha256"]
        == finalized["semantic_index_sha256"]
    )


def test_mirror_change_after_embedding_leaves_previous_binding_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    vault, state, index_sha = _initial_index(tmp_path)
    activate_semantic_index(
        state,
        vault,
        semantic_index_sha256=index_sha,
    )

    knowledge = vault / "11-Knowledge" / "Knowledge.md"
    knowledge.write_text(
        _note(
            "type: knowledge-note\n"
            "status: active\n"
            "category: system\n"
            "maturity: stable\n"
            "source_type: self\n",
            "# Knowledge\nFirst changed content.",
        ),
        encoding="utf-8",
    )
    assert prepare_refresh(state, vault)["phase"] == "prepared"

    _patch_incremental_provider(monkeypatch, [])
    embed_refresh(
        state,
        base_url="http://127.0.0.1:11434",
    )

    knowledge.write_text(
        _note(
            "type: knowledge-note\n"
            "status: active\n"
            "category: system\n"
            "maturity: stable\n"
            "source_type: self\n",
            "# Knowledge\nSecond changed content races finalization.",
        ),
        encoding="utf-8",
    )

    with pytest.raises(Exception, match="stale"):
        finalize_refresh(state, vault)

    assert resolve_active_semantic_index_sha(state) == index_sha


def test_missing_active_binding_fails_closed(tmp_path: Path) -> None:
    state = _state(tmp_path)
    with pytest.raises(
        SemanticRefreshError,
        match="binding is missing",
    ):
        resolve_active_semantic_index_sha(state)
