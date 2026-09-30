from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _read_exact_file,
    _require_sha256,
    sha256_bytes,
)
from .knowledge_index import BM25_B, BM25_K1, tokenize
from .ollama_generator import (
    DEFAULT_TIMEOUT_SECONDS,
    OllamaProviderError,
    resolve_ollama_model,
)
from .production_io import ProductionIOError, mirror_read_lock
from .semantic_corpus import (
    SemanticChunk,
    SemanticCorpusError,
    SemanticCorpusManifest,
    SemanticSource,
    load_semantic_corpus_manifest,
    materialize_semantic_chunk_bytes,
    verify_semantic_corpus_current,
)
from .semantic_index import (
    ADAPTER_VERSION,
    MAX_VECTOR_DIMENSION,
    PLAN_DIR,
    PROVIDER_NAME,
    REQUEST_DIR,
    RESULT_DIR,
    RESULT_SET_DIR,
    VECTOR_ENCODING,
    SemanticIndexError,
    SemanticIndexManifest,
    SemanticVector,
    _artifact_directory,
    _embedding_request_json,
    _load_content_addressed,
    _require_identifier,
    _require_vector,
    _store_content_addressed,
    load_embedding_request,
    load_semantic_index_manifest,
)


QUERY_REQUEST_VERSION = 1
QUERY_RESULT_VERSION = 1
BENCHMARK_VERSION = 1
BENCHMARK_PLAN_VERSION = 1
BENCHMARK_RESULT_SET_VERSION = 1

QUERY_REQUEST_SUFFIX = "semantic-query-embedding-request"
QUERY_RESULT_SUFFIX = "semantic-query-embedding-result"
BENCHMARK_PLAN_SUFFIX = "semantic-retrieval-benchmark-plan"
BENCHMARK_RESULT_SET_SUFFIX = "semantic-retrieval-benchmark-result-set"

MAX_QUERY_BYTES = 16 * 1024
MAX_QUERY_CHARS = 8192
MAX_TOP_K = 64
MAX_BENCHMARK_CASES = 256
MAX_RELEVANT_PATHS = 32
DEFAULT_TOP_K = 8
DEFAULT_LEXICAL_WEIGHT = 0.6
DEFAULT_VECTOR_WEIGHT = 0.4
MAX_SOURCE_KIND_WEIGHT = 10.0

RETRIEVAL_PROFILES: dict[str, float] = {
    "semantic-retrieval-v0": 0.60,
    "semantic-retrieval-v1": 0.15,
}
DEFAULT_RETRIEVAL_PROFILE = "semantic-retrieval-v0"
DIAGNOSTIC_RETRIEVAL_PROFILE = "diagnostic-custom"

SOURCE_KINDS = ("daily", "idea", "project", "project-note", "knowledge")
MODES = ("bm25", "vector", "hybrid")
BENCHMARK_CATEGORIES = (
    "exact-technical",
    "semantic-paraphrase",
    "project-local",
    "daily-knowledge",
    "idea-project",
    "cross-domain-bridge",
)
SEMANTIC_BENCHMARK_CATEGORIES = tuple(
    item for item in BENCHMARK_CATEGORIES if item != "exact-technical"
)


class SemanticRetrievalError(ArtifactLifecycleError):
    """Raised when semantic retrieval cannot be reproduced safely."""


@dataclass(frozen=True)
class RetrievalFilter:
    source_kinds: tuple[str, ...] = ()
    workspaces: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    idea_statuses: tuple[str, ...] = ()
    project_statuses: tuple[str, ...] = ()
    created_from: str | None = None
    created_to: str | None = None
    knowledge_categories: tuple[str, ...] = ()
    knowledge_maturities: tuple[str, ...] = ()
    knowledge_source_types: tuple[str, ...] = ()

    def payload(self) -> dict[str, object]:
        return {
            "source_kinds": list(self.source_kinds),
            "workspaces": list(self.workspaces),
            "projects": list(self.projects),
            "idea_statuses": list(self.idea_statuses),
            "project_statuses": list(self.project_statuses),
            "created_from": self.created_from,
            "created_to": self.created_to,
            "knowledge_categories": list(self.knowledge_categories),
            "knowledge_maturities": list(self.knowledge_maturities),
            "knowledge_source_types": list(self.knowledge_source_types),
        }


@dataclass(frozen=True)
class QueryEmbeddingRequest:
    semantic_index_sha256: str
    provider: str
    adapter_version: str
    model_identifier: str
    model_revision: str
    vector_dimension: int
    vector_encoding: str
    query_sha256: str
    query: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": QUERY_REQUEST_VERSION,
                "semantic_index_sha256": self.semantic_index_sha256,
                "provider": self.provider,
                "adapter_version": self.adapter_version,
                "model_identifier": self.model_identifier,
                "model_revision": self.model_revision,
                "vector_dimension": self.vector_dimension,
                "vector_encoding": self.vector_encoding,
                "query_sha256": self.query_sha256,
                "query": self.query,
            }
        )


@dataclass(frozen=True)
class QueryEmbeddingResult:
    request_sha256: str
    provider: str
    adapter_version: str
    model_identifier: str
    model_revision: str
    vector_encoding: str
    vector: tuple[float, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": QUERY_RESULT_VERSION,
                "request_sha256": self.request_sha256,
                "provider": self.provider,
                "adapter_version": self.adapter_version,
                "model_identifier": self.model_identifier,
                "model_revision": self.model_revision,
                "vector_encoding": self.vector_encoding,
                "vector": list(self.vector),
            }
        )


@dataclass(frozen=True)
class RankedSemanticChunk:
    chunk_id: str
    source_path: str
    source_kind: str
    source_sha256: str
    content_sha256: str
    lexical_score: float
    lexical_normalized: float
    cosine_score: float
    semantic_normalized: float
    source_kind_weight: float
    score: float


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    category: str
    query: str
    relevant_paths: tuple[str, ...]
    filters: RetrievalFilter


@dataclass(frozen=True)
class BenchmarkSet:
    name: str
    cases: tuple[BenchmarkCase, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "benchmark_version": BENCHMARK_VERSION,
                "name": self.name,
                "cases": [
                    {
                        "id": case.case_id,
                        "category": case.category,
                        "query": case.query,
                        "relevant_paths": list(case.relevant_paths),
                        "filters": case.filters.payload(),
                    }
                    for case in self.cases
                ],
            }
        )


@dataclass(frozen=True)
class BenchmarkPlanEntry:
    case_id: str
    request_sha256: str

    def payload(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "request_sha256": self.request_sha256,
        }


@dataclass(frozen=True)
class BenchmarkPlan:
    semantic_index_sha256: str
    benchmark_sha256: str
    provider: str
    adapter_version: str
    model_identifier: str
    model_revision: str
    vector_dimension: int
    vector_encoding: str
    requests: tuple[BenchmarkPlanEntry, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": BENCHMARK_PLAN_VERSION,
                "semantic_index_sha256": self.semantic_index_sha256,
                "benchmark_sha256": self.benchmark_sha256,
                "provider": self.provider,
                "adapter_version": self.adapter_version,
                "model_identifier": self.model_identifier,
                "model_revision": self.model_revision,
                "vector_dimension": self.vector_dimension,
                "vector_encoding": self.vector_encoding,
                "requests": [entry.payload() for entry in self.requests],
            }
        )


@dataclass(frozen=True)
class BenchmarkResultEntry:
    case_id: str
    request_sha256: str
    result_sha256: str

    def payload(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "request_sha256": self.request_sha256,
            "result_sha256": self.result_sha256,
        }


@dataclass(frozen=True)
class BenchmarkResultSet:
    plan_sha256: str
    results: tuple[BenchmarkResultEntry, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": BENCHMARK_RESULT_SET_VERSION,
                "plan_sha256": self.plan_sha256,
                "results": [entry.payload() for entry in self.results],
            }
        )


@dataclass(frozen=True)
class SemanticCandidate:
    vector: SemanticVector
    source: SemanticSource
    chunk: SemanticChunk
    text: str
    term_freq: Mapping[str, int]
    token_count: int


def _round(value: float) -> float:
    return round(value, 8)


def _require_retrieval_vector(raw: object) -> tuple[float, ...]:
    try:
        return _require_vector(raw)
    except SemanticIndexError as exc:
        raise SemanticRetrievalError(str(exc)) from exc


def _require_query(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_QUERY_CHARS
    ):
        raise SemanticRetrievalError(
            f"query must be a non-empty string up to {MAX_QUERY_CHARS} characters"
        )
    if any(ord(ch) == 0 for ch in value):
        raise SemanticRetrievalError("query must not contain NUL")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SemanticRetrievalError("query is not UTF-8 encodable") from exc
    if len(raw) > MAX_QUERY_BYTES:
        raise SemanticRetrievalError(
            f"query exceeds {MAX_QUERY_BYTES} UTF-8 bytes"
        )
    return value


def _require_string_list(
    value: object,
    *,
    label: str,
    allowed: set[str] | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SemanticRetrievalError(f"{label} must be an array")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if (
            not isinstance(raw, str)
            or not raw
            or raw != raw.strip()
            or len(raw) > 1024
        ):
            raise SemanticRetrievalError(f"{label} contains an invalid value")
        if allowed is not None and raw not in allowed:
            raise SemanticRetrievalError(f"{label} contains unsupported value: {raw}")
        if raw in seen:
            raise SemanticRetrievalError(f"{label} contains duplicate value: {raw}")
        seen.add(raw)
        result.append(raw)
    return tuple(sorted(result, key=lambda item: (item.casefold(), item)))


def _require_optional_date(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SemanticRetrievalError(f"{label} must be YYYY-MM-DD or null")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise SemanticRetrievalError(f"{label} must be YYYY-MM-DD") from exc
    return parsed.isoformat()


def parse_retrieval_filter(value: object) -> RetrievalFilter:
    if value is None:
        return RetrievalFilter()
    if not isinstance(value, dict):
        raise SemanticRetrievalError("retrieval filters must be an object")
    expected = {
        "source_kinds",
        "workspaces",
        "projects",
        "idea_statuses",
        "project_statuses",
        "created_from",
        "created_to",
        "knowledge_categories",
        "knowledge_maturities",
        "knowledge_source_types",
    }
    unknown = set(value) - expected
    if unknown:
        raise SemanticRetrievalError(
            f"retrieval filter contains unsupported properties: {sorted(unknown)}"
        )
    created_from = _require_optional_date(
        value.get("created_from"),
        label="created_from",
    )
    created_to = _require_optional_date(
        value.get("created_to"),
        label="created_to",
    )
    if (
        created_from is not None
        and created_to is not None
        and created_from > created_to
    ):
        raise SemanticRetrievalError("created_from must not be after created_to")
    return RetrievalFilter(
        source_kinds=_require_string_list(
            value.get("source_kinds", []),
            label="source_kinds",
            allowed=set(SOURCE_KINDS),
        ),
        workspaces=_require_string_list(
            value.get("workspaces", []),
            label="workspaces",
        ),
        projects=_require_string_list(
            value.get("projects", []),
            label="projects",
        ),
        idea_statuses=_require_string_list(
            value.get("idea_statuses", []),
            label="idea_statuses",
            allowed={"active", "adopted"},
        ),
        project_statuses=_require_string_list(
            value.get("project_statuses", []),
            label="project_statuses",
        ),
        created_from=created_from,
        created_to=created_to,
        knowledge_categories=_require_string_list(
            value.get("knowledge_categories", []),
            label="knowledge_categories",
        ),
        knowledge_maturities=_require_string_list(
            value.get("knowledge_maturities", []),
            label="knowledge_maturities",
        ),
        knowledge_source_types=_require_string_list(
            value.get("knowledge_source_types", []),
            label="knowledge_source_types",
        ),
    )


def _default_filter_payload() -> dict[str, object]:
    return RetrievalFilter().payload()


def retrieval_profile_lexical_weight(profile: str) -> float:
    try:
        return RETRIEVAL_PROFILES[profile]
    except KeyError as exc:
        raise SemanticRetrievalError(
            f"unsupported semantic retrieval profile: {profile}"
        ) from exc


def _resolve_benchmark_retrieval(
    *,
    retrieval_profile: str,
    lexical_weight: float | None,
) -> tuple[str, float]:
    profile_weight = retrieval_profile_lexical_weight(retrieval_profile)
    if lexical_weight is None:
        return retrieval_profile, profile_weight
    if (
        isinstance(lexical_weight, bool)
        or not isinstance(lexical_weight, (int, float))
        or not math.isfinite(float(lexical_weight))
        or not 0.0 <= float(lexical_weight) <= 1.0
    ):
        raise SemanticRetrievalError(
            "lexical_weight must be finite in 0..1"
        )
    return DIAGNOSTIC_RETRIEVAL_PROFILE, float(lexical_weight)


def parse_source_kind_weights(
    value: Mapping[str, object] | None,
) -> dict[str, float]:
    result = {kind: 1.0 for kind in SOURCE_KINDS}
    if value is None:
        return result
    for key, raw in value.items():
        if key not in result:
            raise SemanticRetrievalError(
                f"unsupported source-kind weight: {key}"
            )
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise SemanticRetrievalError(
                f"source-kind weight for {key} must be numeric"
            )
        number = float(raw)
        if (
            not math.isfinite(number)
            or number < 0.0
            or number > MAX_SOURCE_KIND_WEIGHT
        ):
            raise SemanticRetrievalError(
                f"source-kind weight for {key} must be finite in 0.."
                f"{MAX_SOURCE_KIND_WEIGHT}"
            )
        result[key] = number
    return result


def _safe_relevant_path(value: object) -> str:
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise SemanticRetrievalError("benchmark relevant path is invalid")
    if "\\" in value or "\x00" in value:
        raise SemanticRetrievalError("benchmark relevant path is invalid")
    parts = PurePosixPath(value).parts
    if (
        not parts
        or parts[0] not in {"00-DailyNote", "05-Idea", "10-Project", "11-Knowledge"}
        or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
        or not value.endswith(".md")
    ):
        raise SemanticRetrievalError("benchmark relevant path is invalid")
    return value


def parse_benchmark_set(data: bytes) -> BenchmarkSet:
    value = _decode_json_object(data, label="semantic retrieval benchmark")
    if set(value) != {"benchmark_version", "name", "cases"}:
        raise SemanticRetrievalError(
            "benchmark properties do not match contract"
        )
    if value["benchmark_version"] != BENCHMARK_VERSION:
        raise SemanticRetrievalError("unsupported benchmark version")
    name = value["name"]
    raw_cases = value["cases"]
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > 256
    ):
        raise SemanticRetrievalError("benchmark name is invalid")
    if (
        not isinstance(raw_cases, list)
        or not 1 <= len(raw_cases) <= MAX_BENCHMARK_CASES
    ):
        raise SemanticRetrievalError("benchmark cases are invalid")

    cases: list[BenchmarkCase] = []
    seen_ids: set[str] = set()
    seen_categories: set[str] = set()
    for raw in raw_cases:
        if not isinstance(raw, dict) or not {
            "id",
            "category",
            "query",
            "relevant_paths",
        }.issubset(raw) or set(raw) - {
            "id",
            "category",
            "query",
            "relevant_paths",
            "filters",
        }:
            raise SemanticRetrievalError(
                "benchmark case properties do not match contract"
            )
        case_id = raw["id"]
        category = raw["category"]
        if (
            not isinstance(case_id, str)
            or not case_id.strip()
            or len(case_id) > 128
        ):
            raise SemanticRetrievalError("benchmark case id is invalid")
        if case_id in seen_ids:
            raise SemanticRetrievalError(
                f"duplicate benchmark case id: {case_id}"
            )
        if category not in BENCHMARK_CATEGORIES:
            raise SemanticRetrievalError(
                f"unsupported benchmark category: {category}"
            )
        seen_ids.add(case_id)
        seen_categories.add(category)
        query = _require_query(raw["query"])
        raw_paths = raw["relevant_paths"]
        if (
            not isinstance(raw_paths, list)
            or not 1 <= len(raw_paths) <= MAX_RELEVANT_PATHS
        ):
            raise SemanticRetrievalError(
                f"benchmark relevant_paths are invalid: {case_id}"
            )
        paths: list[str] = []
        seen_paths: set[str] = set()
        for item in raw_paths:
            path = _safe_relevant_path(item)
            folded = path.casefold()
            if folded in seen_paths:
                raise SemanticRetrievalError(
                    f"duplicate relevant path in benchmark case: {case_id}"
                )
            seen_paths.add(folded)
            paths.append(path)
        cases.append(
            BenchmarkCase(
                case_id=case_id,
                category=category,
                query=query,
                relevant_paths=tuple(
                sorted(paths, key=lambda item: (item.casefold(), item))
            ),
                filters=parse_retrieval_filter(raw.get("filters")),
            )
        )

    missing = [
        category
        for category in BENCHMARK_CATEGORIES
        if category not in seen_categories
    ]
    if missing:
        raise SemanticRetrievalError(
            f"benchmark is missing required categories: {missing}"
        )
    return BenchmarkSet(name=name, cases=tuple(cases))


def load_benchmark_set(path: Path) -> BenchmarkSet:
    parsed = parse_benchmark_set(_read_exact_file(path))
    if parse_benchmark_set(parsed.to_json_bytes()) != parsed:
        raise SemanticRetrievalError(
            "benchmark canonical round-trip mismatch"
        )
    return parsed


def parse_query_embedding_request(data: bytes) -> QueryEmbeddingRequest:
    value = _decode_json_object(data, label="semantic query embedding request")
    if set(value) != {
        "record_version",
        "semantic_index_sha256",
        "provider",
        "adapter_version",
        "model_identifier",
        "model_revision",
        "vector_dimension",
        "vector_encoding",
        "query_sha256",
        "query",
    }:
        raise SemanticRetrievalError(
            "query embedding request properties do not match contract"
        )
    if value["record_version"] != QUERY_REQUEST_VERSION:
        raise SemanticRetrievalError(
            "unsupported query embedding request version"
        )
    index_sha = _require_sha256(
        value["semantic_index_sha256"],
        label="semantic index SHA",
    )
    if (
        value["provider"] != PROVIDER_NAME
        or value["adapter_version"] != ADAPTER_VERSION
    ):
        raise SemanticRetrievalError(
            "query embedding request adapter is unsupported"
        )
    model_identifier = _require_identifier(
        value["model_identifier"],
        label="model identifier",
    )
    model_revision = _require_sha256(
        value["model_revision"],
        label="model revision",
    )
    dimension = value["vector_dimension"]
    if (
        type(dimension) is not int
        or not 1 <= dimension <= MAX_VECTOR_DIMENSION
    ):
        raise SemanticRetrievalError(
            "query embedding request vector dimension is invalid"
        )
    if value["vector_encoding"] != VECTOR_ENCODING:
        raise SemanticRetrievalError(
            "query embedding request vector encoding is unsupported"
        )
    query = _require_query(value["query"])
    query_sha = _require_sha256(
        value["query_sha256"],
        label="query SHA",
    )
    if sha256_bytes(query.encode("utf-8")) != query_sha:
        raise SemanticRetrievalError("query embedding request text binding mismatch")
    return QueryEmbeddingRequest(
        semantic_index_sha256=index_sha,
        provider=PROVIDER_NAME,
        adapter_version=ADAPTER_VERSION,
        model_identifier=model_identifier,
        model_revision=model_revision,
        vector_dimension=dimension,
        vector_encoding=VECTOR_ENCODING,
        query_sha256=query_sha,
        query=query,
    )


def store_query_embedding_request(
    ai_root: Path,
    request: QueryEmbeddingRequest,
) -> tuple[str, Path]:
    data = request.to_json_bytes()
    if parse_query_embedding_request(data) != request:
        raise SemanticRetrievalError(
            "query embedding request canonical round-trip mismatch"
        )
    return _store_content_addressed(
        _artifact_directory(ai_root, REQUEST_DIR),
        QUERY_REQUEST_SUFFIX,
        data,
    )


def load_query_embedding_request(
    ai_root: Path,
    request_sha256: str,
) -> QueryEmbeddingRequest:
    return parse_query_embedding_request(
        _load_content_addressed(
            ai_root,
            REQUEST_DIR,
            QUERY_REQUEST_SUFFIX,
            request_sha256,
            label="query embedding request SHA",
        )
    )


def parse_query_embedding_result(data: bytes) -> QueryEmbeddingResult:
    value = _decode_json_object(data, label="semantic query embedding result")
    if set(value) != {
        "record_version",
        "request_sha256",
        "provider",
        "adapter_version",
        "model_identifier",
        "model_revision",
        "vector_encoding",
        "vector",
    }:
        raise SemanticRetrievalError(
            "query embedding result properties do not match contract"
        )
    if value["record_version"] != QUERY_RESULT_VERSION:
        raise SemanticRetrievalError(
            "unsupported query embedding result version"
        )
    request_sha = _require_sha256(
        value["request_sha256"],
        label="query embedding request SHA",
    )
    if (
        value["provider"] != PROVIDER_NAME
        or value["adapter_version"] != ADAPTER_VERSION
    ):
        raise SemanticRetrievalError(
            "query embedding result adapter is unsupported"
        )
    model_identifier = _require_identifier(
        value["model_identifier"],
        label="model identifier",
    )
    model_revision = _require_sha256(
        value["model_revision"],
        label="model revision",
    )
    if value["vector_encoding"] != VECTOR_ENCODING:
        raise SemanticRetrievalError(
            "query embedding result vector encoding is unsupported"
        )
    vector = _require_retrieval_vector(value["vector"])
    return QueryEmbeddingResult(
        request_sha256=request_sha,
        provider=PROVIDER_NAME,
        adapter_version=ADAPTER_VERSION,
        model_identifier=model_identifier,
        model_revision=model_revision,
        vector_encoding=VECTOR_ENCODING,
        vector=vector,
    )


def store_query_embedding_result(
    ai_root: Path,
    result: QueryEmbeddingResult,
) -> tuple[str, Path]:
    data = result.to_json_bytes()
    if parse_query_embedding_result(data) != result:
        raise SemanticRetrievalError(
            "query embedding result canonical round-trip mismatch"
        )
    return _store_content_addressed(
        _artifact_directory(ai_root, RESULT_DIR),
        QUERY_RESULT_SUFFIX,
        data,
    )


def load_query_embedding_result(
    ai_root: Path,
    result_sha256: str,
) -> QueryEmbeddingResult:
    return parse_query_embedding_result(
        _load_content_addressed(
            ai_root,
            RESULT_DIR,
            QUERY_RESULT_SUFFIX,
            result_sha256,
            label="query embedding result SHA",
        )
    )


def prepare_query_embedding(
    ai_root: Path,
    *,
    semantic_index_sha256: str,
    query: str,
) -> tuple[str, Path, QueryEmbeddingRequest]:
    index_sha = _require_sha256(
        semantic_index_sha256,
        label="semantic index SHA",
    )
    text = _require_query(query)
    index = load_semantic_index_manifest(ai_root, index_sha)
    request = QueryEmbeddingRequest(
        semantic_index_sha256=index_sha,
        provider=index.provider,
        adapter_version=index.adapter_version,
        model_identifier=index.model_identifier,
        model_revision=index.model_revision,
        vector_dimension=index.vector_dimension,
        vector_encoding=index.vector_encoding,
        query_sha256=sha256_bytes(text.encode("utf-8")),
        query=text,
    )
    request_sha, path = store_query_embedding_request(ai_root, request)
    return request_sha, path, request


def _embed_queries(
    *,
    base_url: str,
    model_identifier: str,
    inputs: Sequence[str],
    timeout: float,
    transport,
) -> tuple[tuple[float, ...], ...]:
    payload = {
        "model": model_identifier,
        "input": list(inputs),
        "truncate": False,
    }
    if transport is None:
        response = _embedding_request_json(
            base_url,
            payload=payload,
            timeout=timeout,
        )
    else:
        response = transport(
            base_url,
            method="POST",
            path="/api/embed",
            payload=payload,
            timeout=timeout,
        )
    if response.get("model") != model_identifier:
        raise SemanticRetrievalError(
            "Ollama query embedding response model does not match request"
        )
    raw_vectors = response.get("embeddings")
    if (
        not isinstance(raw_vectors, list)
        or len(raw_vectors) != len(inputs)
    ):
        raise SemanticRetrievalError(
            "Ollama query embedding vector count mismatch"
        )
    return tuple(_require_retrieval_vector(item) for item in raw_vectors)


def embed_query_with_ollama(
    ai_root: Path,
    *,
    request_sha256: str,
    base_url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport=None,
) -> tuple[str, Path, QueryEmbeddingResult]:
    request_sha = _require_sha256(
        request_sha256,
        label="query embedding request SHA",
    )
    request = load_query_embedding_request(ai_root, request_sha)
    identity = resolve_ollama_model(
        base_url,
        request.model_identifier,
        timeout=timeout,
        transport=transport,
    )
    if (
        identity.identifier != request.model_identifier
        or identity.digest != request.model_revision
    ):
        raise SemanticRetrievalError(
            "resolved Ollama model identity does not match query request"
        )
    vectors = _embed_queries(
        base_url=base_url,
        model_identifier=request.model_identifier,
        inputs=(request.query,),
        timeout=timeout,
        transport=transport,
    )
    vector = vectors[0]
    if len(vector) != request.vector_dimension:
        raise SemanticRetrievalError(
            "query embedding vector dimension does not match semantic index"
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
    result_sha, path = store_query_embedding_result(ai_root, result)
    return result_sha, path, result


def _validate_query_binding(
    index_sha: str,
    index: SemanticIndexManifest,
    request_sha: str,
    request: QueryEmbeddingRequest,
    result: QueryEmbeddingResult,
) -> None:
    if request.semantic_index_sha256 != index_sha:
        raise SemanticRetrievalError(
            "query request does not target selected semantic index"
        )
    if result.request_sha256 != request_sha:
        raise SemanticRetrievalError(
            "query result does not bind selected query request"
        )
    if (
        request.provider != index.provider
        or request.adapter_version != index.adapter_version
        or request.model_identifier != index.model_identifier
        or request.model_revision != index.model_revision
        or request.vector_dimension != index.vector_dimension
        or request.vector_encoding != index.vector_encoding
        or result.provider != index.provider
        or result.adapter_version != index.adapter_version
        or result.model_identifier != index.model_identifier
        or result.model_revision != index.model_revision
        or result.vector_encoding != index.vector_encoding
        or len(result.vector) != index.vector_dimension
    ):
        raise SemanticRetrievalError(
            "query embedding identity does not match semantic index"
        )


def _metadata_matches(
    source: SemanticSource,
    filters: RetrievalFilter,
) -> bool:
    metadata = source.metadata
    if filters.source_kinds and source.source_kind not in filters.source_kinds:
        return False
    if filters.workspaces and metadata.get("workspace") not in filters.workspaces:
        return False
    if filters.projects:
        def normalize_project(value: object) -> str | None:
            if not isinstance(value, str) or not value:
                return None
            raw = value.strip()
            if raw.startswith("[[") and raw.endswith("]]"):
                raw = raw[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
            if raw.endswith(".md"):
                raw = raw[:-3]
            return raw or None

        project_value = (
            source.path[:-3]
            if source.source_kind == "project"
            else normalize_project(metadata.get("project"))
        )
        requested = {
            normalized
            for item in filters.projects
            if (normalized := normalize_project(item)) is not None
        }
        if project_value not in requested:
            return False
    if source.source_kind == "idea" and filters.idea_statuses:
        if metadata.get("status") not in filters.idea_statuses:
            return False
    if filters.project_statuses:
        status = (
            metadata.get("project_status")
            if source.source_kind == "project-note"
            else metadata.get("status")
            if source.source_kind == "project"
            else None
        )
        if status not in filters.project_statuses:
            return False
    if source.source_kind == "knowledge":
        if (
            filters.knowledge_categories
            and metadata.get("category") not in filters.knowledge_categories
        ):
            return False
        if (
            filters.knowledge_maturities
            and metadata.get("maturity") not in filters.knowledge_maturities
        ):
            return False
        if (
            filters.knowledge_source_types
            and metadata.get("source_type") not in filters.knowledge_source_types
        ):
            return False
    if filters.created_from is not None or filters.created_to is not None:
        raw_date = metadata.get("date") or metadata.get("created")
        if not isinstance(raw_date, str):
            return False
        try:
            normalized = date.fromisoformat(raw_date).isoformat()
        except ValueError as exc:
            raise SemanticRetrievalError(
                f"source has invalid date metadata: {source.path}"
            ) from exc
        if filters.created_from is not None and normalized < filters.created_from:
            return False
        if filters.created_to is not None and normalized > filters.created_to:
            return False
    return True


def _build_candidates(
    ai_root: Path,
    index: SemanticIndexManifest,
    corpus: SemanticCorpusManifest,
    filters: RetrievalFilter,
) -> tuple[SemanticCandidate, ...]:
    chunk_by_id: dict[str, tuple[SemanticSource, SemanticChunk]] = {}
    for source in corpus.sources:
        for chunk in source.chunks:
            chunk_by_id[chunk.chunk_id] = (source, chunk)

    candidates: list[SemanticCandidate] = []
    for item in index.vectors:
        pair = chunk_by_id.get(item.chunk_id)
        if pair is None:
            raise SemanticRetrievalError(
                "semantic index references chunk absent from corpus"
            )
        source, chunk = pair
        if (
            source.path != item.source_path
            or source.source_kind != item.source_kind
            or source.content_sha256 != item.source_sha256
            or chunk.content_sha256 != item.content_sha256
        ):
            raise SemanticRetrievalError(
                "semantic index source/chunk binding mismatch"
            )
        if not _metadata_matches(source, filters):
            continue
        request = load_embedding_request(ai_root, item.request_sha256)
        if (
            request.chunk_id != item.chunk_id
            or request.source_path != item.source_path
            or request.source_kind != item.source_kind
            or request.source_sha256 != item.source_sha256
            or request.content_sha256 != item.content_sha256
            or request.corpus_manifest_sha256 != index.corpus_manifest_sha256
            or request.provider != index.provider
            or request.adapter_version != index.adapter_version
            or request.model_identifier != index.model_identifier
            or request.model_revision != index.model_revision
        ):
            raise SemanticRetrievalError(
                "semantic index request binding mismatch"
            )
        tokens = tokenize(request.input_text)
        candidates.append(
            SemanticCandidate(
                vector=item,
                source=source,
                chunk=chunk,
                text=request.input_text,
                term_freq=Counter(tokens),
                token_count=len(tokens),
            )
        )
    return tuple(candidates)


def load_verified_semantic_candidates(
    ai_root: Path,
    vault_root: Path,
    *,
    semantic_index_sha256: str,
    filters: RetrievalFilter | None = None,
) -> tuple[SemanticIndexManifest, SemanticCorpusManifest, tuple[SemanticCandidate, ...]]:
    index_sha = _require_sha256(
        semantic_index_sha256,
        label="semantic index SHA",
    )
    index = load_semantic_index_manifest(ai_root, index_sha)
    try:
        with mirror_read_lock(ai_root):
            corpus = load_semantic_corpus_manifest(
                ai_root,
                index.corpus_manifest_sha256,
            )
            verify_semantic_corpus_current(vault_root, corpus)
            candidates = _build_candidates(
                ai_root,
                index,
                corpus,
                filters or RetrievalFilter(),
            )
    except (ProductionIOError, SemanticCorpusError) as exc:
        raise SemanticRetrievalError(str(exc)) from exc
    return index, corpus, candidates


def _bm25_scores(
    candidates: Sequence[SemanticCandidate],
    query: str,
) -> dict[str, float]:
    query_tokens = tuple(dict.fromkeys(tokenize(query)))
    if not candidates or not query_tokens:
        return {item.vector.chunk_id: 0.0 for item in candidates}
    document_count = len(candidates)
    avg_len = sum(item.token_count for item in candidates) / document_count
    df: Counter[str] = Counter()
    for item in candidates:
        for token in query_tokens:
            if item.term_freq.get(token, 0):
                df[token] += 1

    scores: dict[str, float] = {}
    for item in candidates:
        score = 0.0
        for token in query_tokens:
            tf = item.term_freq.get(token, 0)
            if tf <= 0:
                continue
            idf = math.log(
                1.0
                + (
                    document_count
                    - df[token]
                    + 0.5
                )
                / (df[token] + 0.5)
            )
            denominator = tf + BM25_K1 * (
                1.0
                - BM25_B
                + BM25_B
                * (
                    item.token_count / avg_len
                    if avg_len > 0
                    else 0.0
                )
            )
            score += idf * (
                tf * (BM25_K1 + 1.0) / denominator
            )
        scores[item.vector.chunk_id] = score
    return scores


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise SemanticRetrievalError("cosine vector dimensions do not match")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0:
        raise SemanticRetrievalError("query embedding vector has zero norm")
    if right_norm == 0.0:
        return 0.0
    score = sum(a * b for a, b in zip(left, right, strict=True)) / (
        left_norm * right_norm
    )
    return max(-1.0, min(1.0, score))


def rank_semantic_chunks(
    candidates: Sequence[SemanticCandidate],
    *,
    query: str,
    query_vector: Sequence[float],
    mode: str,
    source_kind_weights: Mapping[str, object] | None = None,
    lexical_weight: float = DEFAULT_LEXICAL_WEIGHT,
    top_k: int = DEFAULT_TOP_K,
) -> tuple[RankedSemanticChunk, ...]:
    text = _require_query(query)
    if mode not in MODES:
        raise SemanticRetrievalError(f"unsupported retrieval mode: {mode}")
    if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
        raise SemanticRetrievalError(
            f"top_k must be an integer in 1..{MAX_TOP_K}"
        )
    if (
        isinstance(lexical_weight, bool)
        or not isinstance(lexical_weight, (int, float))
        or not math.isfinite(float(lexical_weight))
        or not 0.0 <= float(lexical_weight) <= 1.0
    ):
        raise SemanticRetrievalError("lexical_weight must be finite in 0..1")
    lexical_weight_value = float(lexical_weight)
    vector_weight = 1.0 - lexical_weight_value
    weights = parse_source_kind_weights(source_kind_weights)
    query_vec = _require_retrieval_vector(list(query_vector))

    lexical = _bm25_scores(candidates, text)
    top_lexical = max(lexical.values(), default=0.0)
    rows: list[RankedSemanticChunk] = []
    for candidate in candidates:
        raw_lexical = lexical[candidate.vector.chunk_id]
        normalized_lexical = (
            raw_lexical / top_lexical
            if top_lexical > 0.0
            else 0.0
        )
        cosine = _cosine(query_vec, candidate.vector.vector)
        semantic_normalized = (cosine + 1.0) / 2.0
        kind_weight = weights[candidate.source.source_kind]
        if mode == "bm25":
            score = raw_lexical * kind_weight
        elif mode == "vector":
            score = cosine * kind_weight
        else:
            score = (
                lexical_weight_value * normalized_lexical
                + vector_weight * semantic_normalized
            ) * kind_weight
        rows.append(
            RankedSemanticChunk(
                chunk_id=candidate.vector.chunk_id,
                source_path=candidate.source.path,
                source_kind=candidate.source.source_kind,
                source_sha256=candidate.source.content_sha256,
                content_sha256=candidate.chunk.content_sha256,
                lexical_score=raw_lexical,
                lexical_normalized=normalized_lexical,
                cosine_score=cosine,
                semantic_normalized=semantic_normalized,
                source_kind_weight=kind_weight,
                score=score,
            )
        )
    rows.sort(
        key=lambda item: (
            -item.score,
            -item.lexical_score,
            -item.cosine_score,
            item.source_path.casefold(),
            item.source_path,
            item.chunk_id,
        )
    )
    if mode == "bm25":
        rows = [item for item in rows if item.lexical_score > 0.0]
    return tuple(rows[:top_k])


def retrieve_semantic(
    ai_root: Path,
    vault_root: Path,
    *,
    semantic_index_sha256: str,
    query_request_sha256: str,
    query_result_sha256: str,
    mode: str = "hybrid",
    filters: RetrievalFilter | None = None,
    source_kind_weights: Mapping[str, object] | None = None,
    lexical_weight: float = DEFAULT_LEXICAL_WEIGHT,
    top_k: int = DEFAULT_TOP_K,
) -> tuple[RankedSemanticChunk, ...]:
    index_sha = _require_sha256(
        semantic_index_sha256,
        label="semantic index SHA",
    )
    request_sha = _require_sha256(
        query_request_sha256,
        label="query embedding request SHA",
    )
    result_sha = _require_sha256(
        query_result_sha256,
        label="query embedding result SHA",
    )
    index = load_semantic_index_manifest(ai_root, index_sha)
    request = load_query_embedding_request(ai_root, request_sha)
    result = load_query_embedding_result(ai_root, result_sha)
    _validate_query_binding(
        index_sha,
        index,
        request_sha,
        request,
        result,
    )
    try:
        with mirror_read_lock(ai_root):
            corpus = load_semantic_corpus_manifest(
                ai_root,
                index.corpus_manifest_sha256,
            )
            verify_semantic_corpus_current(vault_root, corpus)
            filter_value = filters or RetrievalFilter()
            candidates = _build_candidates(
                ai_root,
                index,
                corpus,
                filter_value,
            )
            ranked = rank_semantic_chunks(
                candidates,
                query=request.query,
                query_vector=result.vector,
                mode=mode,
                source_kind_weights=source_kind_weights,
                lexical_weight=lexical_weight,
                top_k=top_k,
            )

            by_path = {source.path: source for source in corpus.sources}
            chunks = {
                chunk.chunk_id: chunk
                for source in corpus.sources
                for chunk in source.chunks
            }
            for item in ranked:
                source = by_path[item.source_path]
                chunk = chunks[item.chunk_id]
                materialize_semantic_chunk_bytes(
                    vault_root,
                    source,
                    chunk,
                )
    except (ProductionIOError, SemanticCorpusError) as exc:
        raise SemanticRetrievalError(str(exc)) from exc
    return ranked


def parse_benchmark_plan(data: bytes) -> BenchmarkPlan:
    value = _decode_json_object(data, label="semantic retrieval benchmark plan")
    if set(value) != {
        "record_version",
        "semantic_index_sha256",
        "benchmark_sha256",
        "provider",
        "adapter_version",
        "model_identifier",
        "model_revision",
        "vector_dimension",
        "vector_encoding",
        "requests",
    }:
        raise SemanticRetrievalError(
            "benchmark plan properties do not match contract"
        )
    if value["record_version"] != BENCHMARK_PLAN_VERSION:
        raise SemanticRetrievalError("unsupported benchmark plan version")
    index_sha = _require_sha256(
        value["semantic_index_sha256"],
        label="semantic index SHA",
    )
    benchmark_sha = _require_sha256(
        value["benchmark_sha256"],
        label="benchmark SHA",
    )
    if (
        value["provider"] != PROVIDER_NAME
        or value["adapter_version"] != ADAPTER_VERSION
        or value["vector_encoding"] != VECTOR_ENCODING
    ):
        raise SemanticRetrievalError(
            "benchmark plan embedding identity is unsupported"
        )
    model_identifier = _require_identifier(
        value["model_identifier"],
        label="model identifier",
    )
    model_revision = _require_sha256(
        value["model_revision"],
        label="model revision",
    )
    dimension = value["vector_dimension"]
    if (
        type(dimension) is not int
        or not 1 <= dimension <= MAX_VECTOR_DIMENSION
    ):
        raise SemanticRetrievalError("benchmark vector dimension is invalid")
    raw_requests = value["requests"]
    if (
        not isinstance(raw_requests, list)
        or not 1 <= len(raw_requests) <= MAX_BENCHMARK_CASES
    ):
        raise SemanticRetrievalError("benchmark plan requests are invalid")
    entries: list[BenchmarkPlanEntry] = []
    seen_cases: set[str] = set()
    for raw in raw_requests:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"case_id", "request_sha256"}
        ):
            raise SemanticRetrievalError("benchmark plan entry is invalid")
        case_id = raw["case_id"]
        if not isinstance(case_id, str) or not case_id:
            raise SemanticRetrievalError("benchmark plan case id is invalid")
        request_sha = _require_sha256(
            raw["request_sha256"],
            label="query embedding request SHA",
        )
        if case_id in seen_cases:
            raise SemanticRetrievalError(
                "benchmark plan contains duplicate case ids"
            )
        seen_cases.add(case_id)
        entries.append(
            BenchmarkPlanEntry(
                case_id=case_id,
                request_sha256=request_sha,
            )
        )
    return BenchmarkPlan(
        semantic_index_sha256=index_sha,
        benchmark_sha256=benchmark_sha,
        provider=PROVIDER_NAME,
        adapter_version=ADAPTER_VERSION,
        model_identifier=model_identifier,
        model_revision=model_revision,
        vector_dimension=dimension,
        vector_encoding=VECTOR_ENCODING,
        requests=tuple(entries),
    )


def store_benchmark_plan(
    ai_root: Path,
    plan: BenchmarkPlan,
) -> tuple[str, Path]:
    data = plan.to_json_bytes()
    if parse_benchmark_plan(data) != plan:
        raise SemanticRetrievalError(
            "benchmark plan canonical round-trip mismatch"
        )
    return _store_content_addressed(
        _artifact_directory(ai_root, PLAN_DIR),
        BENCHMARK_PLAN_SUFFIX,
        data,
    )


def load_benchmark_plan(
    ai_root: Path,
    plan_sha256: str,
) -> BenchmarkPlan:
    return parse_benchmark_plan(
        _load_content_addressed(
            ai_root,
            PLAN_DIR,
            BENCHMARK_PLAN_SUFFIX,
            plan_sha256,
            label="benchmark plan SHA",
        )
    )


def prepare_benchmark_plan(
    ai_root: Path,
    *,
    semantic_index_sha256: str,
    benchmark: BenchmarkSet,
) -> tuple[str, Path, BenchmarkPlan]:
    index_sha = _require_sha256(
        semantic_index_sha256,
        label="semantic index SHA",
    )
    index = load_semantic_index_manifest(ai_root, index_sha)
    benchmark_bytes = benchmark.to_json_bytes()
    benchmark_sha = sha256_bytes(benchmark_bytes)
    entries: list[BenchmarkPlanEntry] = []
    for case in benchmark.cases:
        request_sha, _, _ = prepare_query_embedding(
            ai_root,
            semantic_index_sha256=index_sha,
            query=case.query,
        )
        entries.append(
            BenchmarkPlanEntry(
                case_id=case.case_id,
                request_sha256=request_sha,
            )
        )
    plan = BenchmarkPlan(
        semantic_index_sha256=index_sha,
        benchmark_sha256=benchmark_sha,
        provider=index.provider,
        adapter_version=index.adapter_version,
        model_identifier=index.model_identifier,
        model_revision=index.model_revision,
        vector_dimension=index.vector_dimension,
        vector_encoding=index.vector_encoding,
        requests=tuple(entries),
    )
    plan_sha, path = store_benchmark_plan(ai_root, plan)
    return plan_sha, path, plan


def parse_benchmark_result_set(data: bytes) -> BenchmarkResultSet:
    value = _decode_json_object(
        data,
        label="semantic retrieval benchmark result set",
    )
    if set(value) != {"record_version", "plan_sha256", "results"}:
        raise SemanticRetrievalError(
            "benchmark result-set properties do not match contract"
        )
    if value["record_version"] != BENCHMARK_RESULT_SET_VERSION:
        raise SemanticRetrievalError(
            "unsupported benchmark result-set version"
        )
    plan_sha = _require_sha256(
        value["plan_sha256"],
        label="benchmark plan SHA",
    )
    raw_results = value["results"]
    if (
        not isinstance(raw_results, list)
        or not 1 <= len(raw_results) <= MAX_BENCHMARK_CASES
    ):
        raise SemanticRetrievalError(
            "benchmark result-set entries are invalid"
        )
    entries: list[BenchmarkResultEntry] = []
    seen_cases: set[str] = set()
    for raw in raw_results:
        if (
            not isinstance(raw, dict)
            or set(raw) != {
                "case_id",
                "request_sha256",
                "result_sha256",
            }
        ):
            raise SemanticRetrievalError(
                "benchmark result-set entry is invalid"
            )
        case_id = raw["case_id"]
        if not isinstance(case_id, str) or not case_id:
            raise SemanticRetrievalError(
                "benchmark result-set case id is invalid"
            )
        request_sha = _require_sha256(
            raw["request_sha256"],
            label="query embedding request SHA",
        )
        result_sha = _require_sha256(
            raw["result_sha256"],
            label="query embedding result SHA",
        )
        if case_id in seen_cases:
            raise SemanticRetrievalError(
                "benchmark result-set contains duplicate case ids"
            )
        seen_cases.add(case_id)
        entries.append(
            BenchmarkResultEntry(
                case_id=case_id,
                request_sha256=request_sha,
                result_sha256=result_sha,
            )
        )
    return BenchmarkResultSet(
        plan_sha256=plan_sha,
        results=tuple(entries),
    )


def store_benchmark_result_set(
    ai_root: Path,
    result_set: BenchmarkResultSet,
) -> tuple[str, Path]:
    data = result_set.to_json_bytes()
    if parse_benchmark_result_set(data) != result_set:
        raise SemanticRetrievalError(
            "benchmark result-set canonical round-trip mismatch"
        )
    return _store_content_addressed(
        _artifact_directory(ai_root, RESULT_SET_DIR),
        BENCHMARK_RESULT_SET_SUFFIX,
        data,
    )


def load_benchmark_result_set(
    ai_root: Path,
    result_set_sha256: str,
) -> BenchmarkResultSet:
    return parse_benchmark_result_set(
        _load_content_addressed(
            ai_root,
            RESULT_SET_DIR,
            BENCHMARK_RESULT_SET_SUFFIX,
            result_set_sha256,
            label="benchmark result-set SHA",
        )
    )


def embed_benchmark_plan_with_ollama(
    ai_root: Path,
    *,
    plan_sha256: str,
    base_url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    batch_size: int = 8,
    transport=None,
) -> tuple[str, Path, BenchmarkResultSet]:
    plan_sha = _require_sha256(
        plan_sha256,
        label="benchmark plan SHA",
    )
    if type(batch_size) is not int or not 1 <= batch_size <= 32:
        raise SemanticRetrievalError("batch_size must be an integer in 1..32")
    plan = load_benchmark_plan(ai_root, plan_sha)
    identity = resolve_ollama_model(
        base_url,
        plan.model_identifier,
        timeout=timeout,
        transport=transport,
    )
    if (
        identity.identifier != plan.model_identifier
        or identity.digest != plan.model_revision
    ):
        raise SemanticRetrievalError(
            "resolved Ollama model identity does not match benchmark plan"
        )

    loaded: list[tuple[BenchmarkPlanEntry, QueryEmbeddingRequest]] = []
    for entry in plan.requests:
        request = load_query_embedding_request(
            ai_root,
            entry.request_sha256,
        )
        if (
            request.semantic_index_sha256 != plan.semantic_index_sha256
            or request.provider != plan.provider
            or request.adapter_version != plan.adapter_version
            or request.model_identifier != plan.model_identifier
            or request.model_revision != plan.model_revision
            or request.vector_dimension != plan.vector_dimension
            or request.vector_encoding != plan.vector_encoding
        ):
            raise SemanticRetrievalError(
                "benchmark query request does not match benchmark plan"
            )
        loaded.append((entry, request))

    results: list[BenchmarkResultEntry] = []
    for start in range(0, len(loaded), batch_size):
        batch = loaded[start : start + batch_size]
        vectors = _embed_queries(
            base_url=base_url,
            model_identifier=plan.model_identifier,
            inputs=tuple(request.query for _, request in batch),
            timeout=timeout,
            transport=transport,
        )
        for (entry, request), vector in zip(batch, vectors, strict=True):
            if len(vector) != plan.vector_dimension:
                raise SemanticRetrievalError(
                    "benchmark query vector dimension mismatch"
                )
            result = QueryEmbeddingResult(
                request_sha256=entry.request_sha256,
                provider=request.provider,
                adapter_version=request.adapter_version,
                model_identifier=request.model_identifier,
                model_revision=request.model_revision,
                vector_encoding=request.vector_encoding,
                vector=vector,
            )
            result_sha, _ = store_query_embedding_result(
                ai_root,
                result,
            )
            results.append(
                BenchmarkResultEntry(
                    case_id=entry.case_id,
                    request_sha256=entry.request_sha256,
                    result_sha256=result_sha,
                )
            )

    result_set = BenchmarkResultSet(
        plan_sha256=plan_sha,
        results=tuple(results),
    )
    result_set_sha, path = store_benchmark_result_set(
        ai_root,
        result_set,
    )
    return result_set_sha, path, result_set


def _unique_path_ranking(
    ranked: Sequence[RankedSemanticChunk],
) -> tuple[RankedSemanticChunk, ...]:
    seen: set[str] = set()
    result: list[RankedSemanticChunk] = []
    for item in ranked:
        if item.source_path in seen:
            continue
        seen.add(item.source_path)
        result.append(item)
    return tuple(result)


def _metrics_for_cases(
    cases: Sequence[BenchmarkCase],
    rankings: Mapping[str, tuple[RankedSemanticChunk, ...]],
    *,
    top_k: int,
) -> dict[str, object]:
    top1 = 0
    recall_sum = 0.0
    reciprocal_sum = 0.0
    for case in cases:
        ranked = _unique_path_ranking(rankings[case.case_id])
        relevant = set(case.relevant_paths)
        if ranked and ranked[0].source_path in relevant:
            top1 += 1
        top = ranked[:top_k]
        hits = len(relevant.intersection(item.source_path for item in top))
        recall_sum += hits / len(relevant)
        first = next(
            (
                position
                for position, item in enumerate(ranked, 1)
                if item.source_path in relevant
            ),
            None,
        )
        if first is not None:
            reciprocal_sum += 1.0 / first
    count = len(cases)
    return {
        "case_count": count,
        "top1_accuracy": _round(top1 / count) if count else None,
        "recall_at_k_macro": _round(recall_sum / count) if count else None,
        "mrr": _round(reciprocal_sum / count) if count else None,
        "k": top_k,
    }


def evaluate_semantic_benchmark(
    ai_root: Path,
    vault_root: Path,
    *,
    semantic_index_sha256: str,
    benchmark: BenchmarkSet,
    plan_sha256: str,
    result_set_sha256: str,
    top_k: int = 3,
    retrieval_profile: str = DEFAULT_RETRIEVAL_PROFILE,
    lexical_weight: float | None = None,
    source_kind_weights: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
        raise SemanticRetrievalError("benchmark top_k is invalid")
    resolved_profile, resolved_lexical_weight = (
        _resolve_benchmark_retrieval(
            retrieval_profile=retrieval_profile,
            lexical_weight=lexical_weight,
        )
    )
    index_sha = _require_sha256(
        semantic_index_sha256,
        label="semantic index SHA",
    )
    plan_sha = _require_sha256(
        plan_sha256,
        label="benchmark plan SHA",
    )
    result_set_sha = _require_sha256(
        result_set_sha256,
        label="benchmark result-set SHA",
    )
    plan = load_benchmark_plan(ai_root, plan_sha)
    result_set = load_benchmark_result_set(
        ai_root,
        result_set_sha,
    )
    if (
        plan.semantic_index_sha256 != index_sha
        or plan.benchmark_sha256 != sha256_bytes(benchmark.to_json_bytes())
        or result_set.plan_sha256 != plan_sha
    ):
        raise SemanticRetrievalError(
            "benchmark artifacts do not match selected index/benchmark"
        )
    if len(plan.requests) != len(benchmark.cases):
        raise SemanticRetrievalError(
            "benchmark plan request count mismatch"
        )
    if [
        entry.case_id for entry in plan.requests
    ] != [
        case.case_id for case in benchmark.cases
    ]:
        raise SemanticRetrievalError(
            "benchmark plan case order mismatch"
        )
    if [
        (entry.case_id, entry.request_sha256)
        for entry in result_set.results
    ] != [
        (entry.case_id, entry.request_sha256)
        for entry in plan.requests
    ]:
        raise SemanticRetrievalError(
            "benchmark result-set order/binding mismatch"
        )

    index = load_semantic_index_manifest(ai_root, index_sha)
    try:
        with mirror_read_lock(ai_root):
            corpus = load_semantic_corpus_manifest(
                ai_root,
                index.corpus_manifest_sha256,
            )
            verify_semantic_corpus_current(vault_root, corpus)
    except (ProductionIOError, SemanticCorpusError) as exc:
        raise SemanticRetrievalError(str(exc)) from exc

    result_by_case = {
        entry.case_id: entry
        for entry in result_set.results
    }
    rankings_by_mode: dict[
        str,
        dict[str, tuple[RankedSemanticChunk, ...]],
    ] = {mode: {} for mode in MODES}
    reports: list[dict[str, object]] = []

    corpus_paths = {source.path for source in corpus.sources}
    for case, plan_entry in zip(
        benchmark.cases,
        plan.requests,
        strict=True,
    ):
        missing = [
            path
            for path in case.relevant_paths
            if path not in corpus_paths
        ]
        if missing:
            raise SemanticRetrievalError(
                f"benchmark case {case.case_id} references sources "
                f"absent from semantic corpus: {missing}"
            )
        result_entry = result_by_case[case.case_id]
        request = load_query_embedding_request(
            ai_root,
            plan_entry.request_sha256,
        )
        result = load_query_embedding_result(
            ai_root,
            result_entry.result_sha256,
        )
        _validate_query_binding(
            index_sha,
            index,
            plan_entry.request_sha256,
            request,
            result,
        )
        if request.query != case.query:
            raise SemanticRetrievalError(
                "benchmark query request text does not match benchmark case"
            )
        candidates = _build_candidates(
            ai_root,
            index,
            corpus,
            case.filters,
        )
        case_modes: dict[str, list[dict[str, object]]] = {}
        for mode in MODES:
            ranked = rank_semantic_chunks(
                candidates,
                query=case.query,
                query_vector=result.vector,
                mode=mode,
                source_kind_weights=source_kind_weights,
                lexical_weight=resolved_lexical_weight,
                top_k=MAX_TOP_K,
            )
            rankings_by_mode[mode][case.case_id] = ranked
            case_modes[mode] = [
                {
                    "chunk_id": item.chunk_id,
                    "source_path": item.source_path,
                    "source_kind": item.source_kind,
                    "score": _round(item.score),
                    "lexical_score": _round(item.lexical_score),
                    "cosine_score": _round(item.cosine_score),
                    "relevant": item.source_path in set(case.relevant_paths),
                }
                for item in ranked[:top_k]
            ]
        reports.append(
            {
                "id": case.case_id,
                "category": case.category,
                "query": case.query,
                "relevant_paths": list(case.relevant_paths),
                "filters": case.filters.payload(),
                "rankings": case_modes,
            }
        )

    all_metrics: dict[str, object] = {}
    exact_cases = [
        case
        for case in benchmark.cases
        if case.category == "exact-technical"
    ]
    semantic_cases = [
        case
        for case in benchmark.cases
        if case.category in SEMANTIC_BENCHMARK_CATEGORIES
    ]
    for mode in MODES:
        all_metrics[mode] = {
            "overall": _metrics_for_cases(
                benchmark.cases,
                rankings_by_mode[mode],
                top_k=top_k,
            ),
            "exact_technical": _metrics_for_cases(
                exact_cases,
                rankings_by_mode[mode],
                top_k=top_k,
            ),
            "semantic": _metrics_for_cases(
                semantic_cases,
                rankings_by_mode[mode],
                top_k=top_k,
            ),
        }

    bm25_semantic = all_metrics["bm25"]["semantic"]["recall_at_k_macro"]
    hybrid_semantic = all_metrics["hybrid"]["semantic"]["recall_at_k_macro"]
    bm25_exact = all_metrics["bm25"]["exact_technical"]["top1_accuracy"]
    hybrid_exact = all_metrics["hybrid"]["exact_technical"]["top1_accuracy"]
    assert isinstance(bm25_semantic, float)
    assert isinstance(hybrid_semantic, float)
    assert isinstance(bm25_exact, float)
    assert isinstance(hybrid_exact, float)
    improved = hybrid_semantic > bm25_semantic
    exact_baseline_present = bm25_exact > 0.0
    exact_not_regressed = hybrid_exact >= bm25_exact

    return {
        "benchmark_version": BENCHMARK_VERSION,
        "name": benchmark.name,
        "benchmark_sha256": plan.benchmark_sha256,
        "semantic_index_sha256": index_sha,
        "benchmark_plan_sha256": plan_sha,
        "benchmark_result_set_sha256": result_set_sha,
        "top_k": top_k,
        "retrieval_profile": resolved_profile,
        "lexical_weight": resolved_lexical_weight,
        "vector_weight": 1.0 - resolved_lexical_weight,
        "source_kind_weights": parse_source_kind_weights(
            source_kind_weights
        ),
        "metrics": all_metrics,
        "acceptance": {
            "hybrid_semantic_recall_improved_over_bm25": improved,
            "bm25_exact_technical_top1_present": exact_baseline_present,
            "hybrid_exact_technical_top1_not_regressed": exact_not_regressed,
            "passed": (
                improved
                and exact_baseline_present
                and exact_not_regressed
            ),
        },
        "cases": reports,
    }


def _load_json_object(path: Path, *, label: str) -> dict[str, object]:
    return _decode_json_object(_read_exact_file(path), label=label)


def _rank_payload(item: RankedSemanticChunk) -> dict[str, object]:
    return {
        "chunk_id": item.chunk_id,
        "source_path": item.source_path,
        "source_kind": item.source_kind,
        "source_sha256": item.source_sha256,
        "content_sha256": item.content_sha256,
        "score": _round(item.score),
        "lexical_score": _round(item.lexical_score),
        "lexical_normalized": _round(item.lexical_normalized),
        "cosine_score": _round(item.cosine_score),
        "semantic_normalized": _round(item.semantic_normalized),
        "source_kind_weight": _round(item.source_kind_weight),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-semantic-retrieval"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    query_prepare = subparsers.add_parser("query-prepare")
    query_prepare.add_argument("--ai-root", type=Path, required=True)
    query_prepare.add_argument("--semantic-index-sha", required=True)
    query_prepare.add_argument("--query", required=True)

    query_embed = subparsers.add_parser("query-embed")
    query_embed.add_argument("--ai-root", type=Path, required=True)
    query_embed.add_argument("--request-sha", required=True)
    query_embed.add_argument("--base-url", required=True)
    query_embed.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )

    rank = subparsers.add_parser("rank")
    rank.add_argument("--ai-root", type=Path, required=True)
    rank.add_argument("--vault-root", type=Path, required=True)
    rank.add_argument("--semantic-index-sha", required=True)
    rank.add_argument("--request-sha", required=True)
    rank.add_argument("--result-sha", required=True)
    rank.add_argument("--mode", choices=MODES, default="hybrid")
    rank.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    rank.add_argument(
        "--lexical-weight",
        type=float,
        default=DEFAULT_LEXICAL_WEIGHT,
    )
    rank.add_argument("--filters-json", type=Path)
    rank.add_argument("--source-kind-weights-json", type=Path)

    benchmark_prepare = subparsers.add_parser("benchmark-prepare")
    benchmark_prepare.add_argument("--ai-root", type=Path, required=True)
    benchmark_prepare.add_argument("--semantic-index-sha", required=True)
    benchmark_prepare.add_argument("--benchmark", type=Path, required=True)

    benchmark_embed = subparsers.add_parser("benchmark-embed")
    benchmark_embed.add_argument("--ai-root", type=Path, required=True)
    benchmark_embed.add_argument("--plan-sha", required=True)
    benchmark_embed.add_argument("--base-url", required=True)
    benchmark_embed.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    benchmark_embed.add_argument("--batch-size", type=int, default=8)

    benchmark_eval = subparsers.add_parser("benchmark-evaluate")
    benchmark_eval.add_argument("--ai-root", type=Path, required=True)
    benchmark_eval.add_argument("--vault-root", type=Path, required=True)
    benchmark_eval.add_argument("--semantic-index-sha", required=True)
    benchmark_eval.add_argument("--benchmark", type=Path, required=True)
    benchmark_eval.add_argument("--plan-sha", required=True)
    benchmark_eval.add_argument("--result-set-sha", required=True)
    benchmark_eval.add_argument("--top-k", type=int, default=3)
    benchmark_eval.add_argument(
        "--retrieval-profile",
        choices=tuple(RETRIEVAL_PROFILES),
        default=DEFAULT_RETRIEVAL_PROFILE,
    )
    benchmark_eval.add_argument(
        "--lexical-weight",
        type=float,
        default=None,
        help="diagnostic override; production acceptance uses a versioned retrieval profile",
    )
    benchmark_eval.add_argument("--source-kind-weights-json", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "query-prepare":
            request_sha, path, request = prepare_query_embedding(
                args.ai_root,
                semantic_index_sha256=args.semantic_index_sha,
                query=args.query,
            )
            output = {
                "request_sha256": request_sha,
                "path": str(path),
                "semantic_index_sha256": request.semantic_index_sha256,
                "query_sha256": request.query_sha256,
                "model_identifier": request.model_identifier,
                "model_revision": request.model_revision,
            }
        elif args.command == "query-embed":
            result_sha, path, result = embed_query_with_ollama(
                args.ai_root,
                request_sha256=args.request_sha,
                base_url=args.base_url,
                timeout=args.timeout,
            )
            output = {
                "result_sha256": result_sha,
                "path": str(path),
                "request_sha256": result.request_sha256,
                "vector_dimension": len(result.vector),
            }
        elif args.command == "rank":
            filters = (
                parse_retrieval_filter(
                    _load_json_object(
                        args.filters_json,
                        label="retrieval filters",
                    )
                )
                if args.filters_json is not None
                else RetrievalFilter()
            )
            weights = (
                _load_json_object(
                    args.source_kind_weights_json,
                    label="source-kind weights",
                )
                if args.source_kind_weights_json is not None
                else None
            )
            ranked = retrieve_semantic(
                args.ai_root,
                args.vault_root,
                semantic_index_sha256=args.semantic_index_sha,
                query_request_sha256=args.request_sha,
                query_result_sha256=args.result_sha,
                mode=args.mode,
                filters=filters,
                source_kind_weights=weights,
                lexical_weight=args.lexical_weight,
                top_k=args.top_k,
            )
            output = {
                "semantic_index_sha256": args.semantic_index_sha,
                "mode": args.mode,
                "top_k": args.top_k,
                "results": [_rank_payload(item) for item in ranked],
            }
        elif args.command == "benchmark-prepare":
            benchmark = load_benchmark_set(args.benchmark)
            plan_sha, path, plan = prepare_benchmark_plan(
                args.ai_root,
                semantic_index_sha256=args.semantic_index_sha,
                benchmark=benchmark,
            )
            output = {
                "plan_sha256": plan_sha,
                "path": str(path),
                "semantic_index_sha256": plan.semantic_index_sha256,
                "benchmark_sha256": plan.benchmark_sha256,
                "case_count": len(plan.requests),
            }
        elif args.command == "benchmark-embed":
            result_set_sha, path, result_set = (
                embed_benchmark_plan_with_ollama(
                    args.ai_root,
                    plan_sha256=args.plan_sha,
                    base_url=args.base_url,
                    timeout=args.timeout,
                    batch_size=args.batch_size,
                )
            )
            output = {
                "result_set_sha256": result_set_sha,
                "path": str(path),
                "plan_sha256": result_set.plan_sha256,
                "case_count": len(result_set.results),
            }
        else:
            benchmark = load_benchmark_set(args.benchmark)
            weights = (
                _load_json_object(
                    args.source_kind_weights_json,
                    label="source-kind weights",
                )
                if args.source_kind_weights_json is not None
                else None
            )
            output = evaluate_semantic_benchmark(
                args.ai_root,
                args.vault_root,
                semantic_index_sha256=args.semantic_index_sha,
                benchmark=benchmark,
                plan_sha256=args.plan_sha,
                result_set_sha256=args.result_set_sha,
                top_k=args.top_k,
                retrieval_profile=args.retrieval_profile,
                lexical_weight=args.lexical_weight,
                source_kind_weights=weights,
            )
    except (
        ArtifactLifecycleError,
        OllamaProviderError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
