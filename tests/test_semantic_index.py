from __future__ import annotations

import json
from pathlib import Path

import pytest

import obsidian_automation.semantic_index as semantic_index
from obsidian_automation.ollama_generator import OllamaProviderError
from obsidian_automation.semantic_corpus import (
    build_semantic_corpus,
    store_semantic_corpus_manifest,
)
from obsidian_automation.semantic_index import (
    ADAPTER_VERSION,
    PROVIDER_NAME,
    SemanticIndexError,
    embed_semantic_plan_incremental_with_ollama,
    embed_semantic_plan_with_ollama,
    finalize_semantic_index,
    load_embedding_request,
    load_semantic_index_manifest,
    parse_embedding_result,
    prepare_incremental_embedding_refresh_plan,
    prepare_semantic_embedding_plan,
)


MODEL = "qwen3-embedding:0.6b"
MODEL_DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


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

    daily = vault / "00-DailyNote" / "2026" / "09"
    daily.mkdir(parents=True)
    (daily / "2026-09-29.md").write_text(
        _note(
            "type: daily-review\n",
            "# Note\nDaily semantic signal.\n"
            "# Tasks\n- [ ] never embed this task",
        ),
        encoding="utf-8",
    )

    (vault / "05-Idea" / "Idea.md").write_text(
        _note(
            "type: idea\n"
            "title: Semantic Idea\n"
            "created: 2026-09-29\n"
            "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "status: active\n",
            "# Semantic Idea\n"
            "Connect observations to durable knowledge.",
        ),
        encoding="utf-8",
    )

    project = vault / "10-Project" / "Planner"
    project.mkdir()
    (project / "Planner.md").write_text(
        _note(
            "type: project\n"
            "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "status: running\n",
            "# Project Summary\nBuild semantic retrieval safely.",
        ),
        encoding="utf-8",
    )
    (project / "Design.md").write_text(
        _note(
            "type: project-note\n"
            "project: '[[10-Project/Planner/Planner|Planner]]'\n"
            "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "lifecycle: active\n",
            "# Design\nKeep provider credentials outside Reader.",
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
            "# Knowledge\nContent-addressed vectors are derived state.",
        ),
        encoding="utf-8",
    )
    return vault


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "04-Index").mkdir(parents=True)
    (state / "24-Locks" / "read-view").mkdir(parents=True)
    return state


def _corpus(state: Path, vault: Path) -> str:
    manifest = build_semantic_corpus(vault)
    digest, _ = store_semantic_corpus_manifest(state, manifest)
    return digest


def _successful_transport(
    calls: list[tuple[str, str, object]],
):
    def transport(
        base_url: str,
        *,
        method: str,
        path: str,
        payload,
        timeout: float,
    ):
        calls.append((method, path, payload))
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
        assert payload["model"] == MODEL
        assert payload["truncate"] is False
        inputs = payload["input"]
        return {
            "model": MODEL,
            "embeddings": [
                [
                    float(index + 1),
                    float(len(text.encode("utf-8"))),
                    0.5,
                ]
                for index, text in enumerate(inputs)
            ],
        }

    return transport


def _prepare_and_embed(tmp_path: Path):
    vault = _vault(tmp_path)
    state = _state(tmp_path)
    corpus_sha = _corpus(state, vault)
    plan_sha, _, plan = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=MODEL,
        model_revision=MODEL_DIGEST,
    )
    calls: list[tuple[str, str, object]] = []
    result_set_sha, _, result_set = (
        embed_semantic_plan_with_ollama(
            state,
            plan_sha256=plan_sha,
            base_url="http://127.0.0.1:11434",
            transport=_successful_transport(calls),
            batch_size=2,
        )
    )
    return (
        vault,
        state,
        corpus_sha,
        plan_sha,
        plan,
        result_set_sha,
        result_set,
        calls,
    )


def test_plan_is_deterministic_and_binds_exact_chunk_text_and_model(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    state = _state(tmp_path)
    corpus_sha = _corpus(state, vault)

    first_sha, _, first = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=MODEL,
        model_revision=MODEL_DIGEST,
    )
    second_sha, _, second = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=MODEL,
        model_revision=MODEL_DIGEST,
    )
    assert first_sha == second_sha
    assert first == second
    assert first.provider == PROVIDER_NAME
    assert first.adapter_version == ADAPTER_VERSION
    assert first.source_kind_counts == {
        "daily": 1,
        "idea": 1,
        "knowledge": 1,
        "project": 1,
        "project-note": 1,
    }

    requests = [
        load_embedding_request(state, entry.request_sha256)
        for entry in first.requests
    ]
    daily = next(
        item
        for item in requests
        if item.source_kind == "daily"
    )
    assert "Daily semantic signal." in daily.input_text
    assert "never embed this task" not in daily.input_text

    changed_sha, _, _ = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=MODEL,
        model_revision=OTHER_DIGEST,
    )
    assert changed_sha != first_sha


def test_ollama_result_set_and_index_bind_all_identities(
    tmp_path: Path,
) -> None:
    (
        vault,
        state,
        corpus_sha,
        plan_sha,
        plan,
        result_set_sha,
        result_set,
        calls,
    ) = _prepare_and_embed(tmp_path)

    assert calls[0][1] == "/api/tags"
    assert all(
        path == "/api/embed"
        for _method, path, _payload in calls[1:]
    )
    assert result_set.plan_sha256 == plan_sha
    assert result_set.vector_dimension == 3
    assert len(result_set.results) == len(plan.requests)

    index_sha, index_path, manifest = finalize_semantic_index(
        state,
        vault,
        plan_sha256=plan_sha,
        result_set_sha256=result_set_sha,
    )
    assert index_path.name == (
        f"{index_sha}.semantic-index.json"
    )
    assert load_semantic_index_manifest(
        state,
        index_sha,
    ) == manifest
    assert manifest.corpus_manifest_sha256 == corpus_sha
    assert manifest.embedding_plan_sha256 == plan_sha
    assert (
        manifest.embedding_result_set_sha256
        == result_set_sha
    )
    assert manifest.model_identifier == MODEL
    assert manifest.model_revision == MODEL_DIGEST
    assert manifest.vector_dimension == 3
    assert len(manifest.vectors) == len(plan.requests)
    assert all(
        len(item.vector) == 3
        for item in manifest.vectors
    )


def test_finalize_rejects_stale_corpus_and_corrupted_result(
    tmp_path: Path,
) -> None:
    (
        vault,
        state,
        _corpus_sha,
        plan_sha,
        _plan,
        result_set_sha,
        result_set,
        _calls,
    ) = _prepare_and_embed(tmp_path)

    first_result = result_set.results[0]
    result_path = (
        state
        / "04-Index"
        / "semantic-embedding-results"
        / (
            f"{first_result.result_sha256}."
            "semantic-embedding-result.json"
        )
    )
    original = result_path.read_bytes()
    value = json.loads(original)
    value["vector"][0] += 1.0
    result_path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(
        SemanticIndexError,
        match="hash mismatch",
    ):
        finalize_semantic_index(
            state,
            vault,
            plan_sha256=plan_sha,
            result_set_sha256=result_set_sha,
        )

    result_path.write_bytes(original)
    knowledge = vault / "11-Knowledge" / "Knowledge.md"
    knowledge.write_text(
        knowledge.read_text(encoding="utf-8")
        + "\nChanged after embedding.\n",
        encoding="utf-8",
    )
    with pytest.raises(
        SemanticIndexError,
        match="stale",
    ):
        finalize_semantic_index(
            state,
            vault,
            plan_sha256=plan_sha,
            result_set_sha256=result_set_sha,
        )


def test_model_digest_mismatch_fails_before_embedding(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    state = _state(tmp_path)
    corpus_sha = _corpus(state, vault)
    plan_sha, _, _ = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=MODEL,
        model_revision=MODEL_DIGEST,
    )
    calls: list[str] = []

    def transport(
        base_url: str,
        *,
        method: str,
        path: str,
        payload,
        timeout: float,
    ):
        calls.append(path)
        assert path == "/api/tags"
        return {
            "models": [
                {
                    "name": MODEL,
                    "model": MODEL,
                    "digest": OTHER_DIGEST,
                }
            ]
        }

    with pytest.raises(
        SemanticIndexError,
        match="digest does not match",
    ):
        embed_semantic_plan_with_ollama(
            state,
            plan_sha256=plan_sha,
            base_url="http://127.0.0.1:11434",
            transport=transport,
        )
    assert calls == ["/api/tags"]


def test_provider_failure_never_publishes_result_set_or_index(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    state = _state(tmp_path)
    corpus_sha = _corpus(state, vault)
    plan_sha, _, _ = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=MODEL,
        model_revision=MODEL_DIGEST,
    )

    def transport(
        base_url: str,
        *,
        method: str,
        path: str,
        payload,
        timeout: float,
    ):
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
        raise OllamaProviderError("fixture provider failure")

    with pytest.raises(
        OllamaProviderError,
        match="fixture provider failure",
    ):
        embed_semantic_plan_with_ollama(
            state,
            plan_sha256=plan_sha,
            base_url="http://127.0.0.1:11434",
            transport=transport,
        )

    result_sets = (
        state
        / "04-Index"
        / "semantic-embedding-result-sets"
    )
    semantic_indexes = (
        state
        / "04-Index"
        / "semantic-index"
    )
    assert (
        not result_sets.exists()
        or not any(result_sets.iterdir())
    )
    assert (
        not semantic_indexes.exists()
        or not any(semantic_indexes.iterdir())
    )


def test_failed_rebuild_preserves_existing_usable_index(
    tmp_path: Path,
) -> None:
    (
        vault,
        state,
        corpus_sha,
        old_plan_sha,
        _old_plan,
        old_result_set_sha,
        _old_result_set,
        _calls,
    ) = _prepare_and_embed(tmp_path)
    old_index_sha, _, old_index = finalize_semantic_index(
        state,
        vault,
        plan_sha256=old_plan_sha,
        result_set_sha256=old_result_set_sha,
    )

    new_plan_sha, _, _ = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=MODEL,
        model_revision=OTHER_DIGEST,
    )
    embed_calls = 0

    def transport(
        base_url: str,
        *,
        method: str,
        path: str,
        payload,
        timeout: float,
    ):
        nonlocal embed_calls
        if path == "/api/tags":
            return {
                "models": [
                    {
                        "name": MODEL,
                        "model": MODEL,
                        "digest": OTHER_DIGEST,
                    }
                ]
            }
        assert path == "/api/embed"
        embed_calls += 1
        if embed_calls == 1:
            return {
                "model": MODEL,
                "embeddings": [
                    [1.0, float(index + 1), 0.25]
                    for index, _ in enumerate(payload["input"])
                ],
            }
        raise OllamaProviderError("fixture partial rebuild failure")

    with pytest.raises(
        OllamaProviderError,
        match="partial rebuild failure",
    ):
        embed_semantic_plan_with_ollama(
            state,
            plan_sha256=new_plan_sha,
            base_url="http://127.0.0.1:11434",
            transport=transport,
            batch_size=2,
        )
    assert embed_calls >= 2
    assert load_semantic_index_manifest(
        state,
        old_index_sha,
    ) == old_index
    index_files = list(
        (state / "04-Index" / "semantic-index").glob(
            "*.semantic-index.json"
        )
    )
    assert [path.name for path in index_files] == [
        f"{old_index_sha}.semantic-index.json"
    ]


def test_result_parser_rejects_non_finite_vector() -> None:
    payload = {
        "record_version": 1,
        "request_sha256": "c" * 64,
        "provider": PROVIDER_NAME,
        "adapter_version": ADAPTER_VERSION,
        "model_identifier": MODEL,
        "model_revision": MODEL_DIGEST,
        "vector": [1.0, float("nan")],
    }
    data = json.dumps(
        payload,
        allow_nan=True,
    ).encode("utf-8")
    with pytest.raises(
        SemanticIndexError,
        match="non-finite",
    ):
        parse_embedding_result(data)

def _finalized_index(tmp_path: Path):
    (
        vault,
        state,
        corpus_sha,
        plan_sha,
        plan,
        result_set_sha,
        result_set,
        calls,
    ) = _prepare_and_embed(tmp_path)
    index_sha, _, index = finalize_semantic_index(
        state,
        vault,
        plan_sha256=plan_sha,
        result_set_sha256=result_set_sha,
    )
    return (
        vault,
        state,
        corpus_sha,
        plan_sha,
        plan,
        result_set_sha,
        result_set,
        index_sha,
        index,
        calls,
    )


def _refresh_plan(
    state: Path,
    *,
    plan_sha: str,
    previous_index_sha: str,
):
    return prepare_incremental_embedding_refresh_plan(
        state,
        plan_sha256=plan_sha,
        previous_index_sha256=previous_index_sha,
    )


def test_incremental_noop_reuses_all_vectors_without_provider_call(
    tmp_path: Path,
) -> None:
    (
        vault,
        state,
        _corpus_sha,
        _plan_sha,
        old_plan,
        _result_set_sha,
        _result_set,
        old_index_sha,
        _old_index,
        _calls,
    ) = _finalized_index(tmp_path)

    corpus_sha = _corpus(state, vault)
    plan_sha, _, plan = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=MODEL,
        model_revision=MODEL_DIGEST,
    )
    provider_calls: list[str] = []

    def no_provider(*args, **kwargs):
        provider_calls.append("called")
        raise AssertionError("provider must not be called for no-op refresh")

    refresh_sha, _, refresh_plan = _refresh_plan(
        state,
        plan_sha=plan_sha,
        previous_index_sha=old_index_sha,
    )
    result_set_sha, _, result_set, stats = (
        embed_semantic_plan_incremental_with_ollama(
            state,
            refresh_plan_sha256=refresh_sha,
            base_url="http://127.0.0.1:11434",
            transport=no_provider,
        )
    )

    assert provider_calls == []
    assert refresh_plan.previous_index_sha256 == old_index_sha
    assert result_set.refresh_plan_sha256 == refresh_sha
    assert stats.reused_count == len(plan.requests)
    assert stats.embedded_count == 0
    assert stats.removed_count == 0
    assert result_set.record_version == 2
    assert all(
        entry.reused_from_result_sha256 is not None
        for entry in result_set.results
    )

    new_index_sha, _, new_index = finalize_semantic_index(
        state,
        vault,
        plan_sha256=plan_sha,
        result_set_sha256=result_set_sha,
    )
    assert new_index_sha != old_index_sha
    assert [item.chunk_id for item in new_index.vectors] == [
        item.chunk_id for item in _old_index.vectors
    ]
    assert len(new_index.vectors) == len(old_plan.requests)


def test_incremental_refresh_embeds_only_changed_chunk(
    tmp_path: Path,
) -> None:
    (
        vault,
        state,
        _corpus_sha,
        _plan_sha,
        old_plan,
        _result_set_sha,
        _result_set,
        old_index_sha,
        _old_index,
        _calls,
    ) = _finalized_index(tmp_path)

    daily = vault / "00-DailyNote" / "2026" / "09" / "2026-09-29.md"
    daily.write_text(
        daily.read_text(encoding="utf-8").replace(
            "Daily semantic signal.",
            "Daily semantic signal changed once.",
        ),
        encoding="utf-8",
    )
    corpus_sha = _corpus(state, vault)
    plan_sha, _, plan = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=MODEL,
        model_revision=MODEL_DIGEST,
    )

    calls: list[tuple[str, str, object]] = []
    refresh_sha, _, refresh_plan = _refresh_plan(
        state,
        plan_sha=plan_sha,
        previous_index_sha=old_index_sha,
    )
    result_set_sha, _, result_set, stats = (
        embed_semantic_plan_incremental_with_ollama(
            state,
            refresh_plan_sha256=refresh_sha,
            base_url="http://127.0.0.1:11434",
            transport=_successful_transport(calls),
            batch_size=8,
        )
    )

    assert stats.reused_count == len(old_plan.requests) - 1
    assert stats.embedded_count == 1
    assert stats.removed_count == 1
    assert [path for _method, path, _payload in calls] == [
        "/api/tags",
        "/api/embed",
    ]
    embed_payload = calls[-1][2]
    assert isinstance(embed_payload, dict)
    assert len(embed_payload["input"]) == 1
    assert sum(
        entry.reused_from_result_sha256 is None
        for entry in result_set.results
    ) == 1

    _, _, manifest = finalize_semantic_index(
        state,
        vault,
        plan_sha256=plan_sha,
        result_set_sha256=result_set_sha,
    )
    assert len(manifest.vectors) == len(plan.requests)


def test_incremental_refresh_removes_chunk_without_provider_call(
    tmp_path: Path,
) -> None:
    (
        vault,
        state,
        _corpus_sha,
        _plan_sha,
        old_plan,
        _result_set_sha,
        _result_set,
        old_index_sha,
        _old_index,
        _calls,
    ) = _finalized_index(tmp_path)

    (vault / "05-Idea" / "Idea.md").unlink()
    corpus_sha = _corpus(state, vault)
    plan_sha, _, plan = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=MODEL,
        model_revision=MODEL_DIGEST,
    )
    provider_calls: list[str] = []

    def no_provider(*args, **kwargs):
        provider_calls.append("called")
        raise AssertionError("provider must not be called for removal-only refresh")

    refresh_sha, _, _refresh_plan_value = _refresh_plan(
        state,
        plan_sha=plan_sha,
        previous_index_sha=old_index_sha,
    )
    result_set_sha, _, _result_set, stats = (
        embed_semantic_plan_incremental_with_ollama(
            state,
            refresh_plan_sha256=refresh_sha,
            base_url="http://127.0.0.1:11434",
            transport=no_provider,
        )
    )

    assert provider_calls == []
    assert stats.reused_count == len(plan.requests)
    assert stats.embedded_count == 0
    assert stats.removed_count == len(old_plan.requests) - len(plan.requests)

    _, _, manifest = finalize_semantic_index(
        state,
        vault,
        plan_sha256=plan_sha,
        result_set_sha256=result_set_sha,
    )
    assert all(item.source_kind != "idea" for item in manifest.vectors)


def test_incremental_provider_failure_preserves_previous_index(
    tmp_path: Path,
) -> None:
    (
        vault,
        state,
        _corpus_sha,
        _plan_sha,
        _old_plan,
        _result_set_sha,
        _result_set,
        old_index_sha,
        old_index,
        _calls,
    ) = _finalized_index(tmp_path)

    knowledge = vault / "11-Knowledge" / "Knowledge.md"
    knowledge.write_text(
        knowledge.read_text(encoding="utf-8")
        + "\nChanged for incremental failure.\n",
        encoding="utf-8",
    )
    corpus_sha = _corpus(state, vault)
    plan_sha, _, _ = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=MODEL,
        model_revision=MODEL_DIGEST,
    )

    refresh_sha, _, _ = _refresh_plan(
        state,
        plan_sha=plan_sha,
        previous_index_sha=old_index_sha,
    )

    def failing_transport(
        base_url: str,
        *,
        method: str,
        path: str,
        payload,
        timeout: float,
    ):
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
        raise OllamaProviderError("incremental provider failure")

    with pytest.raises(
        OllamaProviderError,
        match="incremental provider failure",
    ):
        embed_semantic_plan_incremental_with_ollama(
            state,
            refresh_plan_sha256=refresh_sha,
            base_url="http://127.0.0.1:11434",
            transport=failing_transport,
        )

    assert load_semantic_index_manifest(
        state,
        old_index_sha,
    ) == old_index
    index_files = list(
        (state / "04-Index" / "semantic-index").glob(
            "*.semantic-index.json"
        )
    )
    assert old_index_sha in {
        path.name.split(".", 1)[0]
        for path in index_files
    }

def test_embed_refresh_does_not_read_finalized_semantic_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        vault,
        state,
        _corpus_sha,
        _plan_sha,
        _old_plan,
        _result_set_sha,
        _result_set,
        old_index_sha,
        _old_index,
        _calls,
    ) = _finalized_index(tmp_path)

    corpus_sha = _corpus(state, vault)
    plan_sha, _, _ = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=MODEL,
        model_revision=MODEL_DIGEST,
    )
    refresh_sha, _, _ = _refresh_plan(
        state,
        plan_sha=plan_sha,
        previous_index_sha=old_index_sha,
    )

    def forbidden_index_read(*args, **kwargs):
        raise AssertionError(
            "Embedder refresh path must not read finalized Semantic Index"
        )

    monkeypatch.setattr(
        semantic_index,
        "load_semantic_index_manifest",
        forbidden_index_read,
    )

    provider_calls: list[str] = []

    def no_provider(*args, **kwargs):
        provider_calls.append("called")
        raise AssertionError("provider must not be called for no-op refresh")

    result_set_sha, _, result_set, stats = (
        embed_semantic_plan_incremental_with_ollama(
            state,
            refresh_plan_sha256=refresh_sha,
            base_url="http://127.0.0.1:11434",
            transport=no_provider,
        )
    )

    assert provider_calls == []
    assert result_set.refresh_plan_sha256 == refresh_sha
    assert stats.embedded_count == 0

    # Reader finalize is expected to read the previous index again, so restore
    # the loader before finalization.
    monkeypatch.undo()
    finalize_semantic_index(
        state,
        vault,
        plan_sha256=plan_sha,
        result_set_sha256=result_set_sha,
    )

