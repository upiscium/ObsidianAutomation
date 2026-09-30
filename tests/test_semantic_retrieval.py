from __future__ import annotations

import json
from pathlib import Path

import pytest

from obsidian_automation.semantic_corpus import (
    build_semantic_corpus,
    store_semantic_corpus_manifest,
)
from obsidian_automation.semantic_index import (
    EmbeddingResult,
    EmbeddingResultSet,
    EmbeddingResultSetEntry,
    finalize_semantic_index,
    load_embedding_request,
    prepare_semantic_embedding_plan,
    store_embedding_result,
    store_embedding_result_set,
)
from obsidian_automation.semantic_retrieval import (
    BenchmarkResultEntry,
    BenchmarkResultSet,
    RetrievalFilter,
    SemanticRetrievalError,
    embed_benchmark_plan_with_ollama,
    evaluate_semantic_benchmark,
    load_benchmark_set,
    parse_benchmark_set,
    prepare_benchmark_plan,
    prepare_query_embedding,
    retrieve_semantic,
    store_benchmark_result_set,
    store_query_embedding_result,
    QueryEmbeddingResult,
    RETRIEVAL_PROFILES,
    retrieval_profile_lexical_weight,
)


MODEL = "fixture-embedding:latest"
MODEL_DIGEST = "a" * 64
DIMENSION = 6


def _note(frontmatter: str, body: str) -> str:
    return f"---\n{frontmatter}---\n{body}\n"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for root in ("00-DailyNote", "05-Idea", "10-Project", "11-Knowledge"):
        (vault / root).mkdir(parents=True)

    daily = vault / "00-DailyNote" / "2026" / "09"
    daily.mkdir(parents=True)
    (daily / "2026-09-29.md").write_text(
        _note(
            "type: daily-review\n",
            "# Note\nTrain cabin noise kept leaking through earbuds.\n"
            "# Tasks\n- [ ] unrelated task",
        ),
        encoding="utf-8",
    )

    (vault / "05-Idea" / "Worker.md").write_text(
        _note(
            "type: idea\n"
            "title: Semantic Worker Assignment\n"
            "created: 2026-09-29\n"
            "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "status: active\n",
            "# Worker Idea\n"
            "Use semantic affinity to assign LLM worker slots.",
        ),
        encoding="utf-8",
    )

    render = vault / "10-Project" / "Render"
    render.mkdir()
    (render / "Render.md").write_text(
        _note(
            "type: project\n"
            "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "status: running\n",
            "# Project Summary\n"
            "Frame graph and synchronization research.",
        ),
        encoding="utf-8",
    )
    (render / "Notes.md").write_text(
        _note(
            "type: project-note\n"
            "project: '[[10-Project/Render/Render|Render]]'\n"
            "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "category: design\n"
            "lifecycle: active\n",
            "# Queue Design\n"
            "Command queue ownership and barrier scheduling.",
        ),
        encoding="utf-8",
    )

    llm = vault / "10-Project" / "LLM"
    llm.mkdir()
    (llm / "LLM.md").write_text(
        _note(
            "type: project\n"
            "workspace: '[[03-Workspace/Lab/Lab|Lab]]'\n"
            "status: running\n",
            "# Project Summary\n"
            "GPU worker scheduler for a local inference pool.",
        ),
        encoding="utf-8",
    )

    knowledge = vault / "11-Knowledge"
    (knowledge / "Exact.md").write_text(
        _note(
            "type: knowledge-note\n"
            "status: active\n"
            "category: system\n"
            "maturity: stable\n"
            "source_type: self\n",
            "# CUDA Visibility\n"
            "CUDA_VISIBLE_DEVICES pins GPU visibility for a process.",
        ),
        encoding="utf-8",
    )
    (knowledge / "Spatial.md").write_text(
        _note(
            "type: knowledge-note\n"
            "status: active\n"
            "category: research\n"
            "maturity: draft\n"
            "source_type: self\n",
            "# Spatial Integration\n"
            "Private viewpoints are integrated into one latent scene representation.",
        ),
        encoding="utf-8",
    )
    (knowledge / "ANC.md").write_text(
        _note(
            "type: knowledge-note\n"
            "status: active\n"
            "category: audio\n"
            "maturity: stable\n"
            "source_type: self\n",
            "# Active Noise Cancellation\n"
            "Low-frequency inverse-phase control suppresses persistent ambient sound.",
        ),
        encoding="utf-8",
    )
    return vault


def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "04-Index").mkdir(parents=True)
    (state / "24-Locks" / "read-view").mkdir(parents=True)
    return state


SOURCE_VECTORS = {
    "00-DailyNote/2026/09/2026-09-29.md": (0.0, 0.0, 0.0, 0.0, 1.0, 0.0),
    "05-Idea/Worker.md": (0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
    "10-Project/LLM/LLM.md": (0.0, 0.0, 0.0, 0.0, 0.1, 0.99),
    "10-Project/Render/Notes.md": (0.0, 0.0, 0.0, 1.0, 0.0, 0.0),
    "10-Project/Render/Render.md": (0.0, 0.0, 0.1, 0.8, 0.0, 0.0),
    "11-Knowledge/ANC.md": (0.0, 0.0, 0.0, 0.0, 0.99, 0.0),
    "11-Knowledge/Exact.md": (1.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    "11-Knowledge/Spatial.md": (0.0, 0.0, 1.0, 0.0, 0.0, 0.0),
}


def _semantic_index(tmp_path: Path):
    vault = _vault(tmp_path)
    state = _state(tmp_path)
    corpus = build_semantic_corpus(vault)
    corpus_sha, _ = store_semantic_corpus_manifest(state, corpus)
    plan_sha, _, plan = prepare_semantic_embedding_plan(
        state,
        vault,
        corpus_manifest_sha256=corpus_sha,
        model_identifier=MODEL,
        model_revision=MODEL_DIGEST,
    )

    result_entries: list[EmbeddingResultSetEntry] = []
    for entry in plan.requests:
        request = load_embedding_request(state, entry.request_sha256)
        result = EmbeddingResult(
            request_sha256=entry.request_sha256,
            provider=plan.provider,
            adapter_version=plan.adapter_version,
            model_identifier=plan.model_identifier,
            model_revision=plan.model_revision,
            vector=SOURCE_VECTORS[request.source_path],
        )
        result_sha, _ = store_embedding_result(state, result)
        result_entries.append(
            EmbeddingResultSetEntry(
                request_sha256=entry.request_sha256,
                result_sha256=result_sha,
            )
        )

    result_set = EmbeddingResultSet(
        plan_sha256=plan_sha,
        vector_dimension=DIMENSION,
        vector_encoding="json-number-finite-v0",
        results=tuple(result_entries),
    )
    result_set_sha, _ = store_embedding_result_set(state, result_set)
    index_sha, _, index = finalize_semantic_index(
        state,
        vault,
        plan_sha256=plan_sha,
        result_set_sha256=result_set_sha,
    )
    return vault, state, index_sha, index


def _query_result(
    state: Path,
    index_sha: str,
    query: str,
    vector: tuple[float, ...],
):
    request_sha, _, request = prepare_query_embedding(
        state,
        semantic_index_sha256=index_sha,
        query=query,
    )
    result = QueryEmbeddingResult(
        request_sha256=request_sha,
        provider=request.provider,
        adapter_version=request.adapter_version,
        model_identifier=request.model_identifier,
        model_revision=request.model_revision,
        vector_encoding=request.vector_encoding,
        vector=vector,
    )
    result_sha, _ = store_query_embedding_result(state, result)
    return request_sha, result_sha


def _benchmark_bytes() -> bytes:
    return json.dumps(
        {
            "benchmark_version": 1,
            "name": "semantic-hybrid-fixture",
            "cases": [
                {
                    "id": "exact",
                    "category": "exact-technical",
                    "query": "CUDA_VISIBLE_DEVICES",
                    "relevant_paths": ["11-Knowledge/Exact.md"],
                },
                {
                    "id": "semantic-ja",
                    "category": "semantic-paraphrase",
                    "query": "異なる視点の情報を一つの空間理解にまとめる",
                    "relevant_paths": ["11-Knowledge/Spatial.md"],
                },
                {
                    "id": "project-local",
                    "category": "project-local",
                    "query": "描画処理の実行順序と同期を管理する設計",
                    "relevant_paths": ["10-Project/Render/Notes.md"],
                    "filters": {
                        "source_kinds": ["project-note"],
                        "project_statuses": ["running"],
                    },
                },
                {
                    "id": "daily-knowledge",
                    "category": "daily-knowledge",
                    "query": "train cabin noise",
                    "relevant_paths": [
                        "00-DailyNote/2026/09/2026-09-29.md",
                        "11-Knowledge/ANC.md",
                    ],
                },
                {
                    "id": "idea-project",
                    "category": "idea-project",
                    "query": "semantic affinity worker slots",
                    "relevant_paths": [
                        "05-Idea/Worker.md",
                        "10-Project/LLM/LLM.md",
                    ],
                },
                {
                    "id": "bridge",
                    "category": "cross-domain-bridge",
                    "query": "device isolation meets queue ownership",
                    "relevant_paths": [
                        "11-Knowledge/Exact.md",
                        "10-Project/Render/Notes.md",
                    ],
                },
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")


QUERY_VECTORS = {
    "CUDA_VISIBLE_DEVICES": (0.0, 0.0, 1.0, 0.0, 0.0, 0.0),
    "異なる視点の情報を一つの空間理解にまとめる": (0.0, 0.0, 1.0, 0.0, 0.0, 0.0),
    "描画処理の実行順序と同期を管理する設計": (0.0, 0.0, 0.0, 1.0, 0.0, 0.0),
    "train cabin noise": (0.0, 0.0, 0.0, 0.0, 1.0, 0.0),
    "semantic affinity worker slots": (0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
    "device isolation meets queue ownership": (
        0.70710678,
        0.0,
        0.0,
        0.70710678,
        0.0,
        0.0,
    ),
}


def _benchmark_transport(calls: list[str]):
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
                list(QUERY_VECTORS[text])
                for text in payload["input"]
            ],
        }

    return transport


def test_exact_lookup_hybrid_preserves_lexical_signal(tmp_path: Path) -> None:
    vault, state, index_sha, _ = _semantic_index(tmp_path)
    request_sha, result_sha = _query_result(
        state,
        index_sha,
        "CUDA_VISIBLE_DEVICES",
        QUERY_VECTORS["CUDA_VISIBLE_DEVICES"],
    )

    bm25 = retrieve_semantic(
        state,
        vault,
        semantic_index_sha256=index_sha,
        query_request_sha256=request_sha,
        query_result_sha256=result_sha,
        mode="bm25",
        top_k=3,
    )
    vector = retrieve_semantic(
        state,
        vault,
        semantic_index_sha256=index_sha,
        query_request_sha256=request_sha,
        query_result_sha256=result_sha,
        mode="vector",
        top_k=3,
    )
    hybrid = retrieve_semantic(
        state,
        vault,
        semantic_index_sha256=index_sha,
        query_request_sha256=request_sha,
        query_result_sha256=result_sha,
        mode="hybrid",
        top_k=3,
    )

    assert bm25[0].source_path == "11-Knowledge/Exact.md"
    assert vector[0].source_path == "11-Knowledge/Spatial.md"
    assert hybrid[0].source_path == "11-Knowledge/Exact.md"
    assert hybrid[0].lexical_score > 0
    assert hybrid[0].cosine_score == pytest.approx(0.0)


def test_metadata_filters_and_source_kind_weights(tmp_path: Path) -> None:
    vault, state, index_sha, _ = _semantic_index(tmp_path)
    request_sha, result_sha = _query_result(
        state,
        index_sha,
        "semantic worker allocation",
        QUERY_VECTORS["semantic affinity worker slots"],
    )

    idea_only = retrieve_semantic(
        state,
        vault,
        semantic_index_sha256=index_sha,
        query_request_sha256=request_sha,
        query_result_sha256=result_sha,
        mode="vector",
        filters=RetrievalFilter(
            source_kinds=("idea",),
            idea_statuses=("active",),
        ),
        top_k=8,
    )
    assert [item.source_path for item in idea_only] == [
        "05-Idea/Worker.md"
    ]

    weighted = retrieve_semantic(
        state,
        vault,
        semantic_index_sha256=index_sha,
        query_request_sha256=request_sha,
        query_result_sha256=result_sha,
        mode="vector",
        source_kind_weights={
            "idea": 0.1,
            "project": 2.0,
        },
        top_k=3,
    )
    assert weighted[0].source_path == "10-Project/LLM/LLM.md"

    daily_request, daily_result = _query_result(
        state,
        index_sha,
        "commute sound",
        QUERY_VECTORS["train cabin noise"],
    )
    recent_daily = retrieve_semantic(
        state,
        vault,
        semantic_index_sha256=index_sha,
        query_request_sha256=daily_request,
        query_result_sha256=daily_result,
        mode="vector",
        filters=RetrievalFilter(
            source_kinds=("daily",),
            created_from="2026-09-29",
            created_to="2026-09-29",
        ),
        top_k=3,
    )
    assert [item.source_kind for item in recent_daily] == ["daily"]


def test_retrieval_rechecks_canonical_source_before_return(tmp_path: Path) -> None:
    vault, state, index_sha, _ = _semantic_index(tmp_path)
    request_sha, result_sha = _query_result(
        state,
        index_sha,
        "CUDA_VISIBLE_DEVICES",
        QUERY_VECTORS["CUDA_VISIBLE_DEVICES"],
    )
    path = vault / "11-Knowledge" / "Exact.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "\nChanged after indexing.\n",
        encoding="utf-8",
    )

    with pytest.raises(SemanticRetrievalError, match="stale"):
        retrieve_semantic(
            state,
            vault,
            semantic_index_sha256=index_sha,
            query_request_sha256=request_sha,
            query_result_sha256=result_sha,
            mode="hybrid",
        )


def test_repository_benchmark_example_matches_parser_contract() -> None:
    path = Path("examples/ai/semantic-retrieval-benchmark.example.json")
    benchmark = parse_benchmark_set(path.read_bytes())
    assert {case.category for case in benchmark.cases} == {
        "exact-technical",
        "semantic-paraphrase",
        "project-local",
        "daily-knowledge",
        "idea-project",
        "cross-domain-bridge",
    }


def test_versioned_retrieval_profiles_are_immutable_contract_values() -> None:
    assert RETRIEVAL_PROFILES == {
        "semantic-retrieval-v0": 0.60,
        "semantic-retrieval-v1": 0.15,
    }
    assert retrieval_profile_lexical_weight(
        "semantic-retrieval-v0"
    ) == pytest.approx(0.60)
    assert retrieval_profile_lexical_weight(
        "semantic-retrieval-v1"
    ) == pytest.approx(0.15)
    with pytest.raises(
        SemanticRetrievalError,
        match="unsupported semantic retrieval profile",
    ):
        retrieval_profile_lexical_weight("semantic-retrieval-v2")


def test_benchmark_requires_all_six_categories() -> None:
    value = json.loads(_benchmark_bytes())
    value["cases"] = value["cases"][:-1]
    with pytest.raises(
        SemanticRetrievalError,
        match="missing required categories",
    ):
        parse_benchmark_set(
            json.dumps(value, ensure_ascii=False).encode("utf-8")
        )


def test_benchmark_compares_same_queries_and_meets_acceptance(tmp_path: Path) -> None:
    vault, state, index_sha, _ = _semantic_index(tmp_path)
    benchmark_path = tmp_path / "benchmark.json"
    benchmark_path.write_bytes(_benchmark_bytes())
    benchmark = load_benchmark_set(benchmark_path)

    plan_sha, _, plan = prepare_benchmark_plan(
        state,
        semantic_index_sha256=index_sha,
        benchmark=benchmark,
    )
    calls: list[str] = []
    result_set_sha, _, result_set = embed_benchmark_plan_with_ollama(
        state,
        plan_sha256=plan_sha,
        base_url="http://127.0.0.1:11434",
        transport=_benchmark_transport(calls),
        batch_size=3,
    )
    assert calls == ["/api/tags", "/api/embed", "/api/embed"]
    assert len(plan.requests) == len(benchmark.cases) == 6
    assert len(result_set.results) == 6

    report = evaluate_semantic_benchmark(
        state,
        vault,
        semantic_index_sha256=index_sha,
        benchmark=benchmark,
        plan_sha256=plan_sha,
        result_set_sha256=result_set_sha,
        top_k=3,
    )
    assert report["acceptance"]["passed"] is True
    assert report["retrieval_profile"] == "semantic-retrieval-v0"
    assert report["lexical_weight"] == pytest.approx(0.60)
    assert (
        report["metrics"]["hybrid"]["semantic"]["recall_at_k_macro"]
        > report["metrics"]["bm25"]["semantic"]["recall_at_k_macro"]
    )
    assert (
        report["metrics"]["hybrid"]["exact_technical"]["top1_accuracy"]
        >= report["metrics"]["bm25"]["exact_technical"]["top1_accuracy"]
    )

    by_id = {case["id"]: case for case in report["cases"]}
    assert by_id["exact"]["rankings"]["hybrid"][0]["source_path"] == (
        "11-Knowledge/Exact.md"
    )
    assert by_id["semantic-ja"]["rankings"]["hybrid"][0]["source_path"] == (
        "11-Knowledge/Spatial.md"
    )


def test_benchmark_result_binding_is_fail_closed(tmp_path: Path) -> None:
    vault, state, index_sha, _ = _semantic_index(tmp_path)
    benchmark = parse_benchmark_set(_benchmark_bytes())
    plan_sha, _, plan = prepare_benchmark_plan(
        state,
        semantic_index_sha256=index_sha,
        benchmark=benchmark,
    )

    results: list[BenchmarkResultEntry] = []
    for case, entry in zip(benchmark.cases, plan.requests, strict=True):
        request = prepare_query_embedding(
            state,
            semantic_index_sha256=index_sha,
            query=case.query,
        )[2]
        result = QueryEmbeddingResult(
            request_sha256=entry.request_sha256,
            provider=request.provider,
            adapter_version=request.adapter_version,
            model_identifier=request.model_identifier,
            model_revision=request.model_revision,
            vector_encoding=request.vector_encoding,
            vector=QUERY_VECTORS[case.query],
        )
        result_sha, _ = store_query_embedding_result(state, result)
        results.append(
            BenchmarkResultEntry(
                case_id=case.case_id,
                request_sha256=entry.request_sha256,
                result_sha256=result_sha,
            )
        )

    swapped = list(results)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    result_set = BenchmarkResultSet(
        plan_sha256=plan_sha,
        results=tuple(swapped),
    )
    result_set_sha, _ = store_benchmark_result_set(state, result_set)

    with pytest.raises(
        SemanticRetrievalError,
        match="order/binding mismatch",
    ):
        evaluate_semantic_benchmark(
            state,
            vault,
            semantic_index_sha256=index_sha,
            benchmark=benchmark,
            plan_sha256=plan_sha,
            result_set_sha256=result_set_sha,
        )

def test_project_filter_matches_project_entry_identity(tmp_path: Path) -> None:
    vault, state, index_sha, _ = _semantic_index(tmp_path)
    request_sha, result_sha = _query_result(
        state,
        index_sha,
        "local inference scheduler",
        QUERY_VECTORS["semantic affinity worker slots"],
    )
    ranked = retrieve_semantic(
        state,
        vault,
        semantic_index_sha256=index_sha,
        query_request_sha256=request_sha,
        query_result_sha256=result_sha,
        mode="vector",
        filters=RetrievalFilter(
            source_kinds=("project",),
            projects=("10-Project/LLM/LLM",),
        ),
        top_k=8,
    )
    assert [item.source_path for item in ranked] == [
        "10-Project/LLM/LLM.md"
    ]


def test_benchmark_reuses_content_addressed_request_for_duplicate_query(
    tmp_path: Path,
) -> None:
    _vault_root, state, index_sha, _ = _semantic_index(tmp_path)
    value = json.loads(_benchmark_bytes())
    value["cases"][1]["query"] = value["cases"][0]["query"]
    benchmark = parse_benchmark_set(
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    )
    _plan_sha, _, plan = prepare_benchmark_plan(
        state,
        semantic_index_sha256=index_sha,
        benchmark=benchmark,
    )
    assert plan.requests[0].request_sha256 == plan.requests[1].request_sha256


def test_query_result_rejects_non_finite_vector(tmp_path: Path) -> None:
    _vault_root, state, index_sha, _ = _semantic_index(tmp_path)
    request_sha, _, request = prepare_query_embedding(
        state,
        semantic_index_sha256=index_sha,
        query="finite query",
    )
    with pytest.raises(
        SemanticRetrievalError,
        match="non-finite",
    ):
        store_query_embedding_result(
            state,
            QueryEmbeddingResult(
                request_sha256=request_sha,
                provider=request.provider,
                adapter_version=request.adapter_version,
                model_identifier=request.model_identifier,
                model_revision=request.model_revision,
                vector_encoding=request.vector_encoding,
                vector=(float("nan"),) + (0.0,) * (DIMENSION - 1),
            ),
        )

