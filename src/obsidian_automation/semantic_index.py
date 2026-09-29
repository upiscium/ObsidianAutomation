from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _read_exact_file,
    _require_safe_directory,
    _require_sha256,
    _store_immutable,
    sha256_bytes,
)
from .ollama_generator import (
    DEFAULT_TIMEOUT_SECONDS,
    OllamaProviderError,
    _direct_opener,
    _validated_base_url,
    _validated_timeout,
    resolve_ollama_model,
)
from .production_io import ProductionIOError, mirror_read_lock
from .semantic_corpus import (
    CHUNK_POLICY_VERSION,
    MAX_CHUNKS,
    MAX_CHUNK_BYTES,
    SemanticChunk,
    SemanticCorpusError,
    SemanticCorpusManifest,
    SemanticSource,
    load_semantic_corpus_manifest,
    materialize_semantic_chunk_bytes,
    verify_semantic_corpus_current,
)


INDEX_STAGE = "04-Index"
REQUEST_DIR = "semantic-embedding-requests"
PLAN_DIR = "semantic-embedding-plans"
RESULT_DIR = "semantic-embedding-results"
RESULT_SET_DIR = "semantic-embedding-result-sets"
SEMANTIC_INDEX_DIR = "semantic-index"
REQUEST_VERSION = 1
PLAN_VERSION = 1
RESULT_VERSION = 1
LEGACY_RESULT_SET_VERSION = 1
RESULT_SET_VERSION = 2
INDEX_VERSION = 1
PROVIDER_NAME = "ollama"
ADAPTER_VERSION = "ollama-embed-v0"
VECTOR_ENCODING = "json-number-finite-v0"
MAX_MODEL_IDENTIFIER_CHARS = 512
MAX_VECTOR_DIMENSION = 8192
MAX_BATCH_SIZE = 16
DEFAULT_BATCH_SIZE = 8
MAX_RESULT_SET_RESULTS = MAX_CHUNKS
MAX_INDEX_BYTES = 512 * 1024 * 1024
MAX_EMBED_RESPONSE_BYTES = 16 * 1024 * 1024


class SemanticIndexError(ArtifactLifecycleError):
    """Raised when a semantic embedding artifact cannot be trusted safely."""


JSONTransport = Callable[..., dict[str, object]]


@dataclass(frozen=True)
class EmbeddingRequest:
    corpus_manifest_sha256: str
    chunk_policy: str
    provider: str
    adapter_version: str
    model_identifier: str
    model_revision: str
    chunk_id: str
    source_path: str
    source_kind: str
    source_sha256: str
    content_sha256: str
    byte_size: int
    input_text: str

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": REQUEST_VERSION,
                "corpus_manifest_sha256": self.corpus_manifest_sha256,
                "chunk_policy": self.chunk_policy,
                "provider": self.provider,
                "adapter_version": self.adapter_version,
                "model_identifier": self.model_identifier,
                "model_revision": self.model_revision,
                "chunk_id": self.chunk_id,
                "source_path": self.source_path,
                "source_kind": self.source_kind,
                "source_sha256": self.source_sha256,
                "content_sha256": self.content_sha256,
                "byte_size": self.byte_size,
                "input": self.input_text,
            }
        )


@dataclass(frozen=True)
class EmbeddingPlanEntry:
    chunk_id: str
    request_sha256: str

    def payload(self) -> dict[str, str]:
        return {
            "chunk_id": self.chunk_id,
            "request_sha256": self.request_sha256,
        }


@dataclass(frozen=True)
class EmbeddingPlan:
    corpus_manifest_sha256: str
    chunk_policy: str
    provider: str
    adapter_version: str
    model_identifier: str
    model_revision: str
    source_kind_counts: Mapping[str, int]
    requests: tuple[EmbeddingPlanEntry, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": PLAN_VERSION,
                "corpus_manifest_sha256": self.corpus_manifest_sha256,
                "chunk_policy": self.chunk_policy,
                "provider": self.provider,
                "adapter_version": self.adapter_version,
                "model_identifier": self.model_identifier,
                "model_revision": self.model_revision,
                "source_kind_counts": dict(sorted(self.source_kind_counts.items())),
                "requests": [entry.payload() for entry in self.requests],
            }
        )


@dataclass(frozen=True)
class EmbeddingResult:
    request_sha256: str
    provider: str
    adapter_version: str
    model_identifier: str
    model_revision: str
    vector: tuple[float, ...]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": RESULT_VERSION,
                "request_sha256": self.request_sha256,
                "provider": self.provider,
                "adapter_version": self.adapter_version,
                "model_identifier": self.model_identifier,
                "model_revision": self.model_revision,
                "vector": list(self.vector),
            }
        )


@dataclass(frozen=True)
class EmbeddingResultSetEntry:
    request_sha256: str
    result_sha256: str
    reused_from_result_sha256: str | None = None

    def payload(self, *, record_version: int) -> dict[str, object]:
        value: dict[str, object] = {
            "request_sha256": self.request_sha256,
            "result_sha256": self.result_sha256,
        }
        if record_version >= RESULT_SET_VERSION:
            value["reused_from_result_sha256"] = (
                self.reused_from_result_sha256
            )
        return value


@dataclass(frozen=True)
class EmbeddingResultSet:
    plan_sha256: str
    vector_dimension: int
    vector_encoding: str
    results: tuple[EmbeddingResultSetEntry, ...]
    record_version: int = RESULT_SET_VERSION

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": self.record_version,
                "plan_sha256": self.plan_sha256,
                "vector_dimension": self.vector_dimension,
                "vector_encoding": self.vector_encoding,
                "results": [
                    entry.payload(record_version=self.record_version)
                    for entry in self.results
                ],
            }
        )


@dataclass(frozen=True)
class IncrementalEmbeddingStats:
    reused_count: int
    embedded_count: int
    removed_count: int


@dataclass(frozen=True)
class SemanticVector:
    chunk_id: str
    source_path: str
    source_kind: str
    source_sha256: str
    content_sha256: str
    request_sha256: str
    result_sha256: str
    vector: tuple[float, ...]

    def payload(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "source_path": self.source_path,
            "source_kind": self.source_kind,
            "source_sha256": self.source_sha256,
            "content_sha256": self.content_sha256,
            "request_sha256": self.request_sha256,
            "result_sha256": self.result_sha256,
            "vector": list(self.vector),
        }


@dataclass(frozen=True)
class SemanticIndexManifest:
    corpus_manifest_sha256: str
    embedding_plan_sha256: str
    embedding_result_set_sha256: str
    chunk_policy: str
    provider: str
    adapter_version: str
    model_identifier: str
    model_revision: str
    vector_dimension: int
    vector_encoding: str
    source_kind_counts: Mapping[str, int]
    vectors: tuple[SemanticVector, ...]

    def to_json_bytes(self) -> bytes:
        data = _canonical_json_bytes(
            {
                "record_version": INDEX_VERSION,
                "corpus_manifest_sha256": self.corpus_manifest_sha256,
                "embedding_plan_sha256": self.embedding_plan_sha256,
                "embedding_result_set_sha256": self.embedding_result_set_sha256,
                "chunk_policy": self.chunk_policy,
                "provider": self.provider,
                "adapter_version": self.adapter_version,
                "model_identifier": self.model_identifier,
                "model_revision": self.model_revision,
                "vector_dimension": self.vector_dimension,
                "vector_encoding": self.vector_encoding,
                "source_kind_counts": dict(sorted(self.source_kind_counts.items())),
                "vectors": [item.payload() for item in self.vectors],
            }
        )
        if len(data) > MAX_INDEX_BYTES:
            raise SemanticIndexError(
                f"semantic index exceeds {MAX_INDEX_BYTES} canonical bytes"
            )
        return data


def _require_identifier(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_MODEL_IDENTIFIER_CHARS
    ):
        raise SemanticIndexError(
            f"{label} must be a non-empty trimmed string up to "
            f"{MAX_MODEL_IDENTIFIER_CHARS} characters"
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise SemanticIndexError(f"{label} must not contain control characters")
    return value


def _require_source_kind(value: object) -> str:
    if value not in {"daily", "idea", "project", "project-note", "knowledge"}:
        raise SemanticIndexError("semantic embedding source kind is invalid")
    assert isinstance(value, str)
    return value


def _require_vector(raw: object) -> tuple[float, ...]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_VECTOR_DIMENSION:
        raise SemanticIndexError("embedding vector dimension is invalid")
    vector: list[float] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SemanticIndexError("embedding vector contains a non-numeric value")
        number = float(value)
        if not math.isfinite(number):
            raise SemanticIndexError("embedding vector contains a non-finite value")
        vector.append(number)
    return tuple(vector)


def _require_counts(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise SemanticIndexError("source_kind_counts must be an object")
    counts: dict[str, int] = {}
    for key, value in raw.items():
        kind = _require_source_kind(key)
        if type(value) is not int or value < 0:
            raise SemanticIndexError(
                "source_kind_counts values must be non-negative integers"
            )
        counts[kind] = value
    if list(raw) != sorted(raw):
        raise SemanticIndexError("source_kind_counts must use canonical key order")
    return counts


def _source_kind_counts(manifest: SemanticCorpusManifest) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source in manifest.sources:
        counts[source.source_kind] = counts.get(source.source_kind, 0) + 1
    return dict(sorted(counts.items()))


def _artifact_directory(ai_root: Path, name: str, *, create: bool = True) -> Path:
    root = ai_root.absolute()
    index_root = root / INDEX_STAGE
    _require_safe_directory(root, create=False)
    _require_safe_directory(index_root, create=False)
    directory = index_root / name
    _require_safe_directory(directory, create=create)
    return directory


def _store_content_addressed(
    directory: Path,
    suffix: str,
    data: bytes,
) -> tuple[str, Path]:
    digest = sha256_bytes(data)
    return digest, _store_immutable(directory / f"{digest}.{suffix}.json", data)


def _load_content_addressed(
    ai_root: Path,
    directory_name: str,
    suffix: str,
    digest: str,
    *,
    label: str,
) -> bytes:
    normalized = _require_sha256(digest, label=label)
    path = (
        _artifact_directory(ai_root, directory_name, create=False)
        / f"{normalized}.{suffix}.json"
    )
    data = _read_exact_file(path)
    if sha256_bytes(data) != normalized:
        raise SemanticIndexError(f"{label} hash mismatch")
    return data


def parse_embedding_request(data: bytes) -> EmbeddingRequest:
    value = _decode_json_object(data, label="semantic embedding request")
    expected = {
        "record_version",
        "corpus_manifest_sha256",
        "chunk_policy",
        "provider",
        "adapter_version",
        "model_identifier",
        "model_revision",
        "chunk_id",
        "source_path",
        "source_kind",
        "source_sha256",
        "content_sha256",
        "byte_size",
        "input",
    }
    if set(value) != expected:
        raise SemanticIndexError(
            "semantic embedding request properties do not match contract"
        )
    if value["record_version"] != REQUEST_VERSION:
        raise SemanticIndexError("unsupported semantic embedding request version")
    corpus_sha = _require_sha256(
        value["corpus_manifest_sha256"],
        label="corpus manifest SHA",
    )
    if value["chunk_policy"] != CHUNK_POLICY_VERSION:
        raise SemanticIndexError("semantic embedding request chunk policy mismatch")
    if (
        value["provider"] != PROVIDER_NAME
        or value["adapter_version"] != ADAPTER_VERSION
    ):
        raise SemanticIndexError("semantic embedding request adapter is unsupported")
    model_identifier = _require_identifier(
        value["model_identifier"],
        label="model identifier",
    )
    model_revision = _require_sha256(
        value["model_revision"],
        label="model revision",
    )
    chunk_id = _require_sha256(value["chunk_id"], label="chunk id")
    source_path = value["source_path"]
    if (
        not isinstance(source_path, str)
        or not source_path
        or source_path.startswith("/")
    ):
        raise SemanticIndexError("semantic embedding source path is invalid")
    source_kind = _require_source_kind(value["source_kind"])
    source_sha = _require_sha256(value["source_sha256"], label="source SHA")
    content_sha = _require_sha256(
        value["content_sha256"],
        label="chunk content SHA",
    )
    byte_size = value["byte_size"]
    input_text = value["input"]
    if type(byte_size) is not int or not 0 < byte_size <= MAX_CHUNK_BYTES:
        raise SemanticIndexError("semantic embedding request byte_size is invalid")
    if not isinstance(input_text, str):
        raise SemanticIndexError("semantic embedding input must be a string")
    try:
        input_bytes = input_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SemanticIndexError(
            "semantic embedding input is not UTF-8 encodable"
        ) from exc
    if (
        len(input_bytes) != byte_size
        or sha256_bytes(input_bytes) != content_sha
    ):
        raise SemanticIndexError("semantic embedding input binding mismatch")
    return EmbeddingRequest(
        corpus_manifest_sha256=corpus_sha,
        chunk_policy=CHUNK_POLICY_VERSION,
        provider=PROVIDER_NAME,
        adapter_version=ADAPTER_VERSION,
        model_identifier=model_identifier,
        model_revision=model_revision,
        chunk_id=chunk_id,
        source_path=source_path,
        source_kind=source_kind,
        source_sha256=source_sha,
        content_sha256=content_sha,
        byte_size=byte_size,
        input_text=input_text,
    )


def store_embedding_request(
    ai_root: Path,
    request: EmbeddingRequest,
) -> tuple[str, Path]:
    data = request.to_json_bytes()
    if parse_embedding_request(data) != request:
        raise SemanticIndexError(
            "semantic embedding request canonical round-trip mismatch"
        )
    return _store_content_addressed(
        _artifact_directory(ai_root, REQUEST_DIR),
        "semantic-embedding-request",
        data,
    )


def load_embedding_request(
    ai_root: Path,
    request_sha256: str,
) -> EmbeddingRequest:
    return parse_embedding_request(
        _load_content_addressed(
            ai_root,
            REQUEST_DIR,
            "semantic-embedding-request",
            request_sha256,
            label="semantic embedding request SHA",
        )
    )


def parse_embedding_plan(data: bytes) -> EmbeddingPlan:
    value = _decode_json_object(data, label="semantic embedding plan")
    if set(value) != {
        "record_version",
        "corpus_manifest_sha256",
        "chunk_policy",
        "provider",
        "adapter_version",
        "model_identifier",
        "model_revision",
        "source_kind_counts",
        "requests",
    }:
        raise SemanticIndexError(
            "semantic embedding plan properties do not match contract"
        )
    if value["record_version"] != PLAN_VERSION:
        raise SemanticIndexError("unsupported semantic embedding plan version")
    corpus_sha = _require_sha256(
        value["corpus_manifest_sha256"],
        label="corpus manifest SHA",
    )
    if value["chunk_policy"] != CHUNK_POLICY_VERSION:
        raise SemanticIndexError("semantic embedding plan chunk policy mismatch")
    if (
        value["provider"] != PROVIDER_NAME
        or value["adapter_version"] != ADAPTER_VERSION
    ):
        raise SemanticIndexError("semantic embedding plan adapter is unsupported")
    model_identifier = _require_identifier(
        value["model_identifier"],
        label="model identifier",
    )
    model_revision = _require_sha256(
        value["model_revision"],
        label="model revision",
    )
    counts = _require_counts(value["source_kind_counts"])
    raw_requests = value["requests"]
    if (
        not isinstance(raw_requests, list)
        or not 1 <= len(raw_requests) <= MAX_CHUNKS
    ):
        raise SemanticIndexError("semantic embedding plan requests are invalid")
    entries: list[EmbeddingPlanEntry] = []
    seen_chunks: set[str] = set()
    seen_requests: set[str] = set()
    for raw in raw_requests:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"chunk_id", "request_sha256"}
        ):
            raise SemanticIndexError("semantic embedding plan entry is invalid")
        chunk_id = _require_sha256(raw["chunk_id"], label="chunk id")
        request_sha = _require_sha256(
            raw["request_sha256"],
            label="embedding request SHA",
        )
        if chunk_id in seen_chunks or request_sha in seen_requests:
            raise SemanticIndexError(
                "semantic embedding plan contains duplicate entries"
            )
        seen_chunks.add(chunk_id)
        seen_requests.add(request_sha)
        entries.append(
            EmbeddingPlanEntry(
                chunk_id=chunk_id,
                request_sha256=request_sha,
            )
        )
    return EmbeddingPlan(
        corpus_manifest_sha256=corpus_sha,
        chunk_policy=CHUNK_POLICY_VERSION,
        provider=PROVIDER_NAME,
        adapter_version=ADAPTER_VERSION,
        model_identifier=model_identifier,
        model_revision=model_revision,
        source_kind_counts=counts,
        requests=tuple(entries),
    )


def store_embedding_plan(
    ai_root: Path,
    plan: EmbeddingPlan,
) -> tuple[str, Path]:
    data = plan.to_json_bytes()
    if parse_embedding_plan(data) != plan:
        raise SemanticIndexError(
            "semantic embedding plan canonical round-trip mismatch"
        )
    return _store_content_addressed(
        _artifact_directory(ai_root, PLAN_DIR),
        "semantic-embedding-plan",
        data,
    )


def load_embedding_plan(
    ai_root: Path,
    plan_sha256: str,
) -> EmbeddingPlan:
    return parse_embedding_plan(
        _load_content_addressed(
            ai_root,
            PLAN_DIR,
            "semantic-embedding-plan",
            plan_sha256,
            label="semantic embedding plan SHA",
        )
    )


def parse_embedding_result(data: bytes) -> EmbeddingResult:
    value = _decode_json_object(data, label="semantic embedding result")
    if set(value) != {
        "record_version",
        "request_sha256",
        "provider",
        "adapter_version",
        "model_identifier",
        "model_revision",
        "vector",
    }:
        raise SemanticIndexError(
            "semantic embedding result properties do not match contract"
        )
    if value["record_version"] != RESULT_VERSION:
        raise SemanticIndexError("unsupported semantic embedding result version")
    request_sha = _require_sha256(
        value["request_sha256"],
        label="embedding request SHA",
    )
    if (
        value["provider"] != PROVIDER_NAME
        or value["adapter_version"] != ADAPTER_VERSION
    ):
        raise SemanticIndexError("semantic embedding result adapter is unsupported")
    model_identifier = _require_identifier(
        value["model_identifier"],
        label="model identifier",
    )
    model_revision = _require_sha256(
        value["model_revision"],
        label="model revision",
    )
    vector = _require_vector(value["vector"])
    return EmbeddingResult(
        request_sha256=request_sha,
        provider=PROVIDER_NAME,
        adapter_version=ADAPTER_VERSION,
        model_identifier=model_identifier,
        model_revision=model_revision,
        vector=vector,
    )


def store_embedding_result(
    ai_root: Path,
    result: EmbeddingResult,
) -> tuple[str, Path]:
    data = result.to_json_bytes()
    if parse_embedding_result(data) != result:
        raise SemanticIndexError(
            "semantic embedding result canonical round-trip mismatch"
        )
    return _store_content_addressed(
        _artifact_directory(ai_root, RESULT_DIR),
        "semantic-embedding-result",
        data,
    )


def load_embedding_result(
    ai_root: Path,
    result_sha256: str,
) -> EmbeddingResult:
    return parse_embedding_result(
        _load_content_addressed(
            ai_root,
            RESULT_DIR,
            "semantic-embedding-result",
            result_sha256,
            label="semantic embedding result SHA",
        )
    )


def parse_embedding_result_set(data: bytes) -> EmbeddingResultSet:
    value = _decode_json_object(data, label="semantic embedding result set")
    if set(value) != {
        "record_version",
        "plan_sha256",
        "vector_dimension",
        "vector_encoding",
        "results",
    }:
        raise SemanticIndexError(
            "semantic embedding result set properties do not match contract"
        )
    record_version = value["record_version"]
    if record_version not in {
        LEGACY_RESULT_SET_VERSION,
        RESULT_SET_VERSION,
    }:
        raise SemanticIndexError(
            "unsupported semantic embedding result set version"
        )
    plan_sha = _require_sha256(
        value["plan_sha256"],
        label="embedding plan SHA",
    )
    dimension = value["vector_dimension"]
    if (
        type(dimension) is not int
        or not 1 <= dimension <= MAX_VECTOR_DIMENSION
    ):
        raise SemanticIndexError(
            "semantic embedding result set dimension is invalid"
        )
    if value["vector_encoding"] != VECTOR_ENCODING:
        raise SemanticIndexError(
            "semantic embedding result set vector encoding is unsupported"
        )
    raw_results = value["results"]
    if (
        not isinstance(raw_results, list)
        or not 1 <= len(raw_results) <= MAX_RESULT_SET_RESULTS
    ):
        raise SemanticIndexError(
            "semantic embedding result set entries are invalid"
        )
    entries: list[EmbeddingResultSetEntry] = []
    seen_requests: set[str] = set()
    seen_results: set[str] = set()
    for raw in raw_results:
        expected = (
            {"request_sha256", "result_sha256"}
            if record_version == LEGACY_RESULT_SET_VERSION
            else {
                "request_sha256",
                "result_sha256",
                "reused_from_result_sha256",
            }
        )
        if not isinstance(raw, dict) or set(raw) != expected:
            raise SemanticIndexError(
                "semantic embedding result set entry is invalid"
            )
        request_sha = _require_sha256(
            raw["request_sha256"],
            label="embedding request SHA",
        )
        result_sha = _require_sha256(
            raw["result_sha256"],
            label="embedding result SHA",
        )
        reused_from: str | None = None
        if record_version == RESULT_SET_VERSION:
            raw_reused = raw["reused_from_result_sha256"]
            if raw_reused is not None:
                reused_from = _require_sha256(
                    raw_reused,
                    label="reused embedding result SHA",
                )
        if request_sha in seen_requests or result_sha in seen_results:
            raise SemanticIndexError(
                "semantic embedding result set contains duplicate entries"
            )
        seen_requests.add(request_sha)
        seen_results.add(result_sha)
        entries.append(
            EmbeddingResultSetEntry(
                request_sha256=request_sha,
                result_sha256=result_sha,
                reused_from_result_sha256=reused_from,
            )
        )
    return EmbeddingResultSet(
        plan_sha256=plan_sha,
        vector_dimension=dimension,
        vector_encoding=VECTOR_ENCODING,
        results=tuple(entries),
        record_version=record_version,
    )


def store_embedding_result_set(
    ai_root: Path,
    result_set: EmbeddingResultSet,
) -> tuple[str, Path]:
    data = result_set.to_json_bytes()
    if parse_embedding_result_set(data) != result_set:
        raise SemanticIndexError(
            "semantic embedding result set canonical round-trip mismatch"
        )
    return _store_content_addressed(
        _artifact_directory(ai_root, RESULT_SET_DIR),
        "semantic-embedding-result-set",
        data,
    )


def load_embedding_result_set(
    ai_root: Path,
    result_set_sha256: str,
) -> EmbeddingResultSet:
    return parse_embedding_result_set(
        _load_content_addressed(
            ai_root,
            RESULT_SET_DIR,
            "semantic-embedding-result-set",
            result_set_sha256,
            label="semantic embedding result set SHA",
        )
    )


def parse_semantic_index_manifest(data: bytes) -> SemanticIndexManifest:
    if len(data) > MAX_INDEX_BYTES:
        raise SemanticIndexError("semantic index exceeds canonical byte limit")
    value = _decode_json_object(data, label="semantic index manifest")
    if set(value) != {
        "record_version",
        "corpus_manifest_sha256",
        "embedding_plan_sha256",
        "embedding_result_set_sha256",
        "chunk_policy",
        "provider",
        "adapter_version",
        "model_identifier",
        "model_revision",
        "vector_dimension",
        "vector_encoding",
        "source_kind_counts",
        "vectors",
    }:
        raise SemanticIndexError(
            "semantic index properties do not match contract"
        )
    if value["record_version"] != INDEX_VERSION:
        raise SemanticIndexError("unsupported semantic index version")
    corpus_sha = _require_sha256(
        value["corpus_manifest_sha256"],
        label="corpus manifest SHA",
    )
    plan_sha = _require_sha256(
        value["embedding_plan_sha256"],
        label="embedding plan SHA",
    )
    result_set_sha = _require_sha256(
        value["embedding_result_set_sha256"],
        label="embedding result set SHA",
    )
    if value["chunk_policy"] != CHUNK_POLICY_VERSION:
        raise SemanticIndexError("semantic index chunk policy mismatch")
    if (
        value["provider"] != PROVIDER_NAME
        or value["adapter_version"] != ADAPTER_VERSION
    ):
        raise SemanticIndexError("semantic index adapter is unsupported")
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
        raise SemanticIndexError("semantic index vector dimension is invalid")
    if value["vector_encoding"] != VECTOR_ENCODING:
        raise SemanticIndexError(
            "semantic index vector encoding is unsupported"
        )
    counts = _require_counts(value["source_kind_counts"])
    raw_vectors = value["vectors"]
    if (
        not isinstance(raw_vectors, list)
        or not 1 <= len(raw_vectors) <= MAX_CHUNKS
    ):
        raise SemanticIndexError("semantic index vectors are invalid")
    vectors: list[SemanticVector] = []
    seen_chunks: set[str] = set()
    seen_requests: set[str] = set()
    seen_results: set[str] = set()
    for raw in raw_vectors:
        if not isinstance(raw, dict) or set(raw) != {
            "chunk_id",
            "source_path",
            "source_kind",
            "source_sha256",
            "content_sha256",
            "request_sha256",
            "result_sha256",
            "vector",
        }:
            raise SemanticIndexError("semantic index vector entry is invalid")
        chunk_id = _require_sha256(raw["chunk_id"], label="chunk id")
        source_path = raw["source_path"]
        if (
            not isinstance(source_path, str)
            or not source_path
            or source_path.startswith("/")
        ):
            raise SemanticIndexError("semantic index source path is invalid")
        source_kind = _require_source_kind(raw["source_kind"])
        source_sha = _require_sha256(raw["source_sha256"], label="source SHA")
        content_sha = _require_sha256(
            raw["content_sha256"],
            label="chunk content SHA",
        )
        request_sha = _require_sha256(
            raw["request_sha256"],
            label="embedding request SHA",
        )
        result_sha = _require_sha256(
            raw["result_sha256"],
            label="embedding result SHA",
        )
        vector = _require_vector(raw["vector"])
        if len(vector) != dimension:
            raise SemanticIndexError(
                "semantic index contains mixed vector dimensions"
            )
        if (
            chunk_id in seen_chunks
            or request_sha in seen_requests
            or result_sha in seen_results
        ):
            raise SemanticIndexError(
                "semantic index contains duplicate vector bindings"
            )
        seen_chunks.add(chunk_id)
        seen_requests.add(request_sha)
        seen_results.add(result_sha)
        vectors.append(
            SemanticVector(
                chunk_id=chunk_id,
                source_path=source_path,
                source_kind=source_kind,
                source_sha256=source_sha,
                content_sha256=content_sha,
                request_sha256=request_sha,
                result_sha256=result_sha,
                vector=vector,
            )
        )
    return SemanticIndexManifest(
        corpus_manifest_sha256=corpus_sha,
        embedding_plan_sha256=plan_sha,
        embedding_result_set_sha256=result_set_sha,
        chunk_policy=CHUNK_POLICY_VERSION,
        provider=PROVIDER_NAME,
        adapter_version=ADAPTER_VERSION,
        model_identifier=model_identifier,
        model_revision=model_revision,
        vector_dimension=dimension,
        vector_encoding=VECTOR_ENCODING,
        source_kind_counts=counts,
        vectors=tuple(vectors),
    )


def store_semantic_index_manifest(
    ai_root: Path,
    manifest: SemanticIndexManifest,
) -> tuple[str, Path]:
    data = manifest.to_json_bytes()
    if parse_semantic_index_manifest(data) != manifest:
        raise SemanticIndexError(
            "semantic index canonical round-trip mismatch"
        )
    return _store_content_addressed(
        _artifact_directory(ai_root, SEMANTIC_INDEX_DIR),
        "semantic-index",
        data,
    )


def load_semantic_index_manifest(
    ai_root: Path,
    index_sha256: str,
) -> SemanticIndexManifest:
    return parse_semantic_index_manifest(
        _load_content_addressed(
            ai_root,
            SEMANTIC_INDEX_DIR,
            "semantic-index",
            index_sha256,
            label="semantic index SHA",
        )
    )


def _request_for_chunk(
    *,
    corpus_sha256: str,
    source: SemanticSource,
    chunk: SemanticChunk,
    chunk_bytes: bytes,
    model_identifier: str,
    model_revision: str,
) -> EmbeddingRequest:
    try:
        input_text = chunk_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SemanticIndexError("semantic chunk is not UTF-8") from exc
    return EmbeddingRequest(
        corpus_manifest_sha256=corpus_sha256,
        chunk_policy=CHUNK_POLICY_VERSION,
        provider=PROVIDER_NAME,
        adapter_version=ADAPTER_VERSION,
        model_identifier=model_identifier,
        model_revision=model_revision,
        chunk_id=chunk.chunk_id,
        source_path=source.path,
        source_kind=source.source_kind,
        source_sha256=source.content_sha256,
        content_sha256=chunk.content_sha256,
        byte_size=chunk.byte_size,
        input_text=input_text,
    )


def prepare_semantic_embedding_plan(
    ai_root: Path,
    vault_root: Path,
    *,
    corpus_manifest_sha256: str,
    model_identifier: str,
    model_revision: str,
) -> tuple[str, Path, EmbeddingPlan]:
    corpus_sha = _require_sha256(
        corpus_manifest_sha256,
        label="corpus manifest SHA",
    )
    identifier = _require_identifier(
        model_identifier,
        label="model identifier",
    )
    revision = _require_sha256(
        model_revision,
        label="model revision",
    )
    try:
        with mirror_read_lock(ai_root):
            manifest = load_semantic_corpus_manifest(ai_root, corpus_sha)
            verify_semantic_corpus_current(vault_root, manifest)
            entries: list[EmbeddingPlanEntry] = []
            for source in manifest.sources:
                for chunk in source.chunks:
                    chunk_bytes = materialize_semantic_chunk_bytes(
                        vault_root,
                        source,
                        chunk,
                    )
                    request = _request_for_chunk(
                        corpus_sha256=corpus_sha,
                        source=source,
                        chunk=chunk,
                        chunk_bytes=chunk_bytes,
                        model_identifier=identifier,
                        model_revision=revision,
                    )
                    request_sha, _ = store_embedding_request(
                        ai_root,
                        request,
                    )
                    entries.append(
                        EmbeddingPlanEntry(
                            chunk_id=chunk.chunk_id,
                            request_sha256=request_sha,
                        )
                    )
            if not entries:
                raise SemanticIndexError(
                    "semantic corpus contains no chunks to embed"
                )
            plan = EmbeddingPlan(
                corpus_manifest_sha256=corpus_sha,
                chunk_policy=CHUNK_POLICY_VERSION,
                provider=PROVIDER_NAME,
                adapter_version=ADAPTER_VERSION,
                model_identifier=identifier,
                model_revision=revision,
                source_kind_counts=_source_kind_counts(manifest),
                requests=tuple(entries),
            )
            plan_sha, plan_path = store_embedding_plan(ai_root, plan)
    except (ProductionIOError, SemanticCorpusError) as exc:
        raise SemanticIndexError(str(exc)) from exc
    return plan_sha, plan_path, plan


def _embedding_request_json(
    base_url: str,
    *,
    payload: Mapping[str, object],
    timeout: float,
) -> dict[str, object]:
    root = _validated_base_url(base_url)
    timeout_value = _validated_timeout(timeout)
    try:
        data = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise OllamaProviderError(
            "Ollama embedding request payload is not strict JSON"
        ) from exc

    request = Request(
        root + "/api/embed",
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    deadline = time.monotonic() + timeout_value
    try:
        response = _direct_opener().open(
            request,
            timeout=timeout_value,
        )
    except HTTPError as exc:
        raise OllamaProviderError(
            f"Ollama embedding request failed with status {exc.code}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise OllamaProviderError(
            "Ollama embedding request failed"
        ) from exc

    chunks: list[bytes] = []
    total = 0
    with response:
        while total <= MAX_EMBED_RESPONSE_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OllamaProviderError(
                    "Ollama embedding response exceeded request timeout"
                )
            file_object = getattr(response, "fp", None)
            raw_socket = getattr(
                getattr(file_object, "raw", None),
                "_sock",
                None,
            )
            if raw_socket is not None and hasattr(
                raw_socket,
                "settimeout",
            ):
                raw_socket.settimeout(remaining)
            try:
                chunk = response.read(
                    min(
                        65536,
                        MAX_EMBED_RESPONSE_BYTES + 1 - total,
                    )
                )
            except (TimeoutError, OSError) as exc:
                raise OllamaProviderError(
                    "Ollama embedding response read failed"
                ) from exc
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise OllamaProviderError(
                    "Ollama embedding response body is not bytes"
                )
            chunks.append(chunk)
            total += len(chunk)

    raw = b"".join(chunks)
    if len(raw) > MAX_EMBED_RESPONSE_BYTES:
        raise OllamaProviderError(
            f"Ollama embedding response exceeds "
            f"{MAX_EMBED_RESPONSE_BYTES} bytes"
        )
    try:
        return _decode_json_object(
            raw,
            label="Ollama embedding response",
        )
    except ArtifactLifecycleError as exc:
        raise OllamaProviderError(str(exc)) from exc


def embed_semantic_plan_with_ollama(
    ai_root: Path,
    *,
    plan_sha256: str,
    base_url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    transport: JSONTransport | None = None,
) -> tuple[str, Path, EmbeddingResultSet]:
    plan_sha = _require_sha256(
        plan_sha256,
        label="embedding plan SHA",
    )
    if (
        type(batch_size) is not int
        or not 1 <= batch_size <= MAX_BATCH_SIZE
    ):
        raise SemanticIndexError(
            f"batch_size must be between 1 and {MAX_BATCH_SIZE}"
        )
    plan = load_embedding_plan(ai_root, plan_sha)

    identity = resolve_ollama_model(
        base_url,
        plan.model_identifier,
        timeout=timeout,
        transport=transport,
    )
    if identity.identifier != plan.model_identifier:
        raise SemanticIndexError(
            "resolved Ollama model identifier does not match "
            "pinned embedding plan"
        )
    if identity.digest != plan.model_revision:
        raise SemanticIndexError(
            "resolved Ollama model digest does not match "
            "pinned embedding plan"
        )

    loaded: list[tuple[str, EmbeddingRequest]] = []
    for entry in plan.requests:
        request = load_embedding_request(
            ai_root,
            entry.request_sha256,
        )
        if request.chunk_id != entry.chunk_id:
            raise SemanticIndexError(
                "embedding plan chunk/request binding mismatch"
            )
        if (
            request.corpus_manifest_sha256
            != plan.corpus_manifest_sha256
            or request.chunk_policy != plan.chunk_policy
            or request.provider != plan.provider
            or request.adapter_version != plan.adapter_version
            or request.model_identifier != plan.model_identifier
            or request.model_revision != plan.model_revision
        ):
            raise SemanticIndexError(
                "embedding request does not match embedding plan"
            )
        loaded.append((entry.request_sha256, request))

    result_entries: list[EmbeddingResultSetEntry] = []
    vector_dimension: int | None = None
    for start in range(0, len(loaded), batch_size):
        batch = loaded[start : start + batch_size]
        payload = {
            "model": plan.model_identifier,
            "input": [
                request.input_text
                for _, request in batch
            ],
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

        if response.get("model") != plan.model_identifier:
            raise SemanticIndexError(
                "Ollama embedding response model does not match plan"
            )
        raw_vectors = response.get("embeddings")
        if (
            not isinstance(raw_vectors, list)
            or len(raw_vectors) != len(batch)
        ):
            raise SemanticIndexError(
                "Ollama embedding response vector count mismatch"
            )

        for (request_sha, _request), raw_vector in zip(
            batch,
            raw_vectors,
            strict=True,
        ):
            vector = _require_vector(raw_vector)
            if vector_dimension is None:
                vector_dimension = len(vector)
            elif len(vector) != vector_dimension:
                raise SemanticIndexError(
                    "Ollama embedding response has mixed dimensions"
                )
            result = EmbeddingResult(
                request_sha256=request_sha,
                provider=plan.provider,
                adapter_version=plan.adapter_version,
                model_identifier=plan.model_identifier,
                model_revision=plan.model_revision,
                vector=vector,
            )
            result_sha, _ = store_embedding_result(
                ai_root,
                result,
            )
            result_entries.append(
                EmbeddingResultSetEntry(
                    request_sha256=request_sha,
                    result_sha256=result_sha,
                )
            )

    if vector_dimension is None:
        raise SemanticIndexError(
            "embedding plan produced no vectors"
        )
    result_set = EmbeddingResultSet(
        plan_sha256=plan_sha,
        vector_dimension=vector_dimension,
        vector_encoding=VECTOR_ENCODING,
        results=tuple(result_entries),
        record_version=LEGACY_RESULT_SET_VERSION,
    )
    result_set_sha, result_set_path = (
        store_embedding_result_set(
            ai_root,
            result_set,
        )
    )
    return result_set_sha, result_set_path, result_set


def _requests_match_for_vector_reuse(
    previous: EmbeddingRequest,
    current: EmbeddingRequest,
) -> bool:
    return (
        previous.chunk_policy == current.chunk_policy
        and previous.provider == current.provider
        and previous.adapter_version == current.adapter_version
        and previous.model_identifier == current.model_identifier
        and previous.model_revision == current.model_revision
        and previous.chunk_id == current.chunk_id
        and previous.source_path == current.source_path
        and previous.source_kind == current.source_kind
        and previous.source_sha256 == current.source_sha256
        and previous.content_sha256 == current.content_sha256
        and previous.byte_size == current.byte_size
        and previous.input_text == current.input_text
    )


def _load_reusable_vector(
    ai_root: Path,
    *,
    current_request: EmbeddingRequest,
    reused_from_result_sha256: str,
    expected_vector: tuple[float, ...] | None = None,
) -> tuple[float, ...]:
    previous_result = load_embedding_result(
        ai_root,
        reused_from_result_sha256,
    )
    previous_request = load_embedding_request(
        ai_root,
        previous_result.request_sha256,
    )
    if not _requests_match_for_vector_reuse(
        previous_request,
        current_request,
    ):
        raise SemanticIndexError(
            "reused embedding result request does not match current chunk"
        )
    if (
        previous_result.provider != current_request.provider
        or previous_result.adapter_version
        != current_request.adapter_version
        or previous_result.model_identifier
        != current_request.model_identifier
        or previous_result.model_revision
        != current_request.model_revision
    ):
        raise SemanticIndexError(
            "reused embedding result model binding mismatch"
        )
    if (
        expected_vector is not None
        and previous_result.vector != expected_vector
    ):
        raise SemanticIndexError(
            "reused embedding result does not match previous index vector"
        )
    return previous_result.vector


def embed_semantic_plan_incremental_with_ollama(
    ai_root: Path,
    *,
    plan_sha256: str,
    previous_index_sha256: str,
    base_url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    transport: JSONTransport | None = None,
) -> tuple[
    str,
    Path,
    EmbeddingResultSet,
    IncrementalEmbeddingStats,
]:
    plan_sha = _require_sha256(
        plan_sha256,
        label="embedding plan SHA",
    )
    previous_index_sha = _require_sha256(
        previous_index_sha256,
        label="previous semantic index SHA",
    )
    if (
        type(batch_size) is not int
        or not 1 <= batch_size <= MAX_BATCH_SIZE
    ):
        raise SemanticIndexError(
            f"batch_size must be between 1 and {MAX_BATCH_SIZE}"
        )

    plan = load_embedding_plan(ai_root, plan_sha)
    previous_index = load_semantic_index_manifest(
        ai_root,
        previous_index_sha,
    )
    if (
        previous_index.provider != plan.provider
        or previous_index.adapter_version != plan.adapter_version
        or previous_index.model_identifier != plan.model_identifier
        or previous_index.model_revision != plan.model_revision
        or previous_index.vector_encoding != VECTOR_ENCODING
    ):
        raise SemanticIndexError(
            "previous semantic index model identity does not match new plan"
        )

    loaded: list[
        tuple[EmbeddingPlanEntry, EmbeddingRequest]
    ] = []
    for entry in plan.requests:
        request = load_embedding_request(
            ai_root,
            entry.request_sha256,
        )
        if request.chunk_id != entry.chunk_id:
            raise SemanticIndexError(
                "embedding plan chunk/request binding mismatch"
            )
        if (
            request.corpus_manifest_sha256
            != plan.corpus_manifest_sha256
            or request.chunk_policy != plan.chunk_policy
            or request.provider != plan.provider
            or request.adapter_version != plan.adapter_version
            or request.model_identifier != plan.model_identifier
            or request.model_revision != plan.model_revision
        ):
            raise SemanticIndexError(
                "embedding request does not match embedding plan"
            )
        loaded.append((entry, request))

    previous_by_chunk = {
        item.chunk_id: item
        for item in previous_index.vectors
    }
    current_chunk_ids = {
        entry.chunk_id for entry, _ in loaded
    }
    removed_count = len(
        set(previous_by_chunk) - current_chunk_ids
    )

    result_by_request: dict[
        str,
        EmbeddingResultSetEntry,
    ] = {}
    pending: list[
        tuple[EmbeddingPlanEntry, EmbeddingRequest]
    ] = []
    reused_count = 0
    embedded_count = 0
    vector_dimension = previous_index.vector_dimension

    for entry, request in loaded:
        previous_vector = previous_by_chunk.get(entry.chunk_id)
        if previous_vector is None:
            pending.append((entry, request))
            continue
        vector = _load_reusable_vector(
            ai_root,
            current_request=request,
            reused_from_result_sha256=(
                previous_vector.result_sha256
            ),
            expected_vector=previous_vector.vector,
        )
        if len(vector) != vector_dimension:
            raise SemanticIndexError(
                "reused embedding vector dimension mismatch"
            )
        result = EmbeddingResult(
            request_sha256=entry.request_sha256,
            provider=plan.provider,
            adapter_version=plan.adapter_version,
            model_identifier=plan.model_identifier,
            model_revision=plan.model_revision,
            vector=vector,
        )
        result_sha, _ = store_embedding_result(
            ai_root,
            result,
        )
        result_by_request[entry.request_sha256] = (
            EmbeddingResultSetEntry(
                request_sha256=entry.request_sha256,
                result_sha256=result_sha,
                reused_from_result_sha256=(
                    previous_vector.result_sha256
                ),
            )
        )
        reused_count += 1

    if pending:
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
            raise SemanticIndexError(
                "resolved Ollama model identity does not match "
                "pinned embedding plan"
            )

        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            payload = {
                "model": plan.model_identifier,
                "input": [
                    request.input_text
                    for _, request in batch
                ],
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
            if response.get("model") != plan.model_identifier:
                raise SemanticIndexError(
                    "Ollama embedding response model does not match plan"
                )
            raw_vectors = response.get("embeddings")
            if (
                not isinstance(raw_vectors, list)
                or len(raw_vectors) != len(batch)
            ):
                raise SemanticIndexError(
                    "Ollama embedding response vector count mismatch"
                )

            for (entry, _request), raw_vector in zip(
                batch,
                raw_vectors,
                strict=True,
            ):
                vector = _require_vector(raw_vector)
                if len(vector) != vector_dimension:
                    raise SemanticIndexError(
                        "incremental embedding vector dimension mismatch"
                    )
                result = EmbeddingResult(
                    request_sha256=entry.request_sha256,
                    provider=plan.provider,
                    adapter_version=plan.adapter_version,
                    model_identifier=plan.model_identifier,
                    model_revision=plan.model_revision,
                    vector=vector,
                )
                result_sha, _ = store_embedding_result(
                    ai_root,
                    result,
                )
                result_by_request[entry.request_sha256] = (
                    EmbeddingResultSetEntry(
                        request_sha256=entry.request_sha256,
                        result_sha256=result_sha,
                        reused_from_result_sha256=None,
                    )
                )
                embedded_count += 1

    ordered_results = tuple(
        result_by_request[entry.request_sha256]
        for entry in plan.requests
    )
    if len(ordered_results) != len(plan.requests):
        raise SemanticIndexError(
            "incremental embedding result set is incomplete"
        )

    result_set = EmbeddingResultSet(
        plan_sha256=plan_sha,
        vector_dimension=vector_dimension,
        vector_encoding=VECTOR_ENCODING,
        results=ordered_results,
        record_version=RESULT_SET_VERSION,
    )
    result_set_sha, result_set_path = (
        store_embedding_result_set(
            ai_root,
            result_set,
        )
    )
    stats = IncrementalEmbeddingStats(
        reused_count=reused_count,
        embedded_count=embedded_count,
        removed_count=removed_count,
    )
    return result_set_sha, result_set_path, result_set, stats


def _corpus_chunk_bindings(
    manifest: SemanticCorpusManifest,
) -> list[tuple[SemanticSource, SemanticChunk]]:
    return [
        (source, chunk)
        for source in manifest.sources
        for chunk in source.chunks
    ]


def finalize_semantic_index(
    ai_root: Path,
    vault_root: Path,
    *,
    plan_sha256: str,
    result_set_sha256: str,
) -> tuple[str, Path, SemanticIndexManifest]:
    plan_sha = _require_sha256(
        plan_sha256,
        label="embedding plan SHA",
    )
    result_set_sha = _require_sha256(
        result_set_sha256,
        label="embedding result set SHA",
    )
    plan = load_embedding_plan(ai_root, plan_sha)
    result_set = load_embedding_result_set(
        ai_root,
        result_set_sha,
    )
    if result_set.plan_sha256 != plan_sha:
        raise SemanticIndexError(
            "embedding result set does not match embedding plan"
        )
    if len(result_set.results) != len(plan.requests):
        raise SemanticIndexError(
            "embedding result set is incomplete"
        )
    if [
        entry.request_sha256
        for entry in result_set.results
    ] != [
        entry.request_sha256
        for entry in plan.requests
    ]:
        raise SemanticIndexError(
            "embedding result set order does not match embedding plan"
        )

    try:
        with mirror_read_lock(ai_root):
            corpus = load_semantic_corpus_manifest(
                ai_root,
                plan.corpus_manifest_sha256,
            )
            verify_semantic_corpus_current(vault_root, corpus)
            bindings = _corpus_chunk_bindings(corpus)
            if len(bindings) != len(plan.requests):
                raise SemanticIndexError(
                    "embedding plan chunk count does not match corpus"
                )
            if _source_kind_counts(corpus) != dict(
                plan.source_kind_counts
            ):
                raise SemanticIndexError(
                    "embedding plan source-kind counts do not match corpus"
                )

            vectors: list[SemanticVector] = []
            for (
                (source, chunk),
                plan_entry,
                result_entry,
            ) in zip(
                bindings,
                plan.requests,
                result_set.results,
                strict=True,
            ):
                if chunk.chunk_id != plan_entry.chunk_id:
                    raise SemanticIndexError(
                        "embedding plan chunk order does not match corpus"
                    )
                request = load_embedding_request(
                    ai_root,
                    plan_entry.request_sha256,
                )
                if (
                    request.chunk_id != chunk.chunk_id
                    or request.source_path != source.path
                    or request.source_kind != source.source_kind
                    or request.source_sha256
                    != source.content_sha256
                    or request.content_sha256
                    != chunk.content_sha256
                    or request.corpus_manifest_sha256
                    != plan.corpus_manifest_sha256
                ):
                    raise SemanticIndexError(
                        "embedding request does not match corpus chunk"
                    )
                result = load_embedding_result(
                    ai_root,
                    result_entry.result_sha256,
                )
                if (
                    result.request_sha256
                    != plan_entry.request_sha256
                ):
                    raise SemanticIndexError(
                        "embedding result request binding mismatch"
                    )
                if (
                    result.provider != plan.provider
                    or result.adapter_version
                    != plan.adapter_version
                    or result.model_identifier
                    != plan.model_identifier
                    or result.model_revision
                    != plan.model_revision
                ):
                    raise SemanticIndexError(
                        "embedding result model binding mismatch"
                    )
                if (
                    len(result.vector)
                    != result_set.vector_dimension
                ):
                    raise SemanticIndexError(
                        "embedding result dimension mismatch"
                    )
                if (
                    result_entry.reused_from_result_sha256
                    is not None
                ):
                    reused_vector = _load_reusable_vector(
                        ai_root,
                        current_request=request,
                        reused_from_result_sha256=(
                            result_entry.reused_from_result_sha256
                        ),
                    )
                    if reused_vector != result.vector:
                        raise SemanticIndexError(
                            "reused embedding provenance vector mismatch"
                        )
                vectors.append(
                    SemanticVector(
                        chunk_id=chunk.chunk_id,
                        source_path=source.path,
                        source_kind=source.source_kind,
                        source_sha256=source.content_sha256,
                        content_sha256=chunk.content_sha256,
                        request_sha256=plan_entry.request_sha256,
                        result_sha256=result_entry.result_sha256,
                        vector=result.vector,
                    )
                )

            manifest = SemanticIndexManifest(
                corpus_manifest_sha256=(
                    plan.corpus_manifest_sha256
                ),
                embedding_plan_sha256=plan_sha,
                embedding_result_set_sha256=result_set_sha,
                chunk_policy=plan.chunk_policy,
                provider=plan.provider,
                adapter_version=plan.adapter_version,
                model_identifier=plan.model_identifier,
                model_revision=plan.model_revision,
                vector_dimension=result_set.vector_dimension,
                vector_encoding=result_set.vector_encoding,
                source_kind_counts=dict(
                    plan.source_kind_counts
                ),
                vectors=tuple(vectors),
            )
            index_sha, index_path = (
                store_semantic_index_manifest(
                    ai_root,
                    manifest,
                )
            )
    except (ProductionIOError, SemanticCorpusError) as exc:
        raise SemanticIndexError(str(exc)) from exc

    return index_sha, index_path, manifest


def _print_json(payload: Mapping[str, object]) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsidian-semantic-index"
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument(
        "--ai-root",
        type=Path,
        required=True,
    )
    prepare.add_argument(
        "--vault-root",
        type=Path,
        required=True,
    )
    prepare.add_argument("--corpus-sha", required=True)
    prepare.add_argument("--model", required=True)
    prepare.add_argument(
        "--model-revision",
        required=True,
    )

    embed = subparsers.add_parser("embed")
    embed.add_argument(
        "--ai-root",
        type=Path,
        required=True,
    )
    embed.add_argument("--plan-sha", required=True)
    embed.add_argument("--base-url", required=True)
    embed.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    embed.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
    )

    embed_refresh = subparsers.add_parser("embed-refresh")
    embed_refresh.add_argument(
        "--ai-root",
        type=Path,
        required=True,
    )
    embed_refresh.add_argument("--plan-sha", required=True)
    embed_refresh.add_argument(
        "--previous-index-sha",
        required=True,
    )
    embed_refresh.add_argument("--base-url", required=True)
    embed_refresh.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    embed_refresh.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
    )

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument(
        "--ai-root",
        type=Path,
        required=True,
    )
    finalize.add_argument(
        "--vault-root",
        type=Path,
        required=True,
    )
    finalize.add_argument("--plan-sha", required=True)
    finalize.add_argument(
        "--result-set-sha",
        required=True,
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            plan_sha, path, plan = (
                prepare_semantic_embedding_plan(
                    args.ai_root,
                    args.vault_root,
                    corpus_manifest_sha256=args.corpus_sha,
                    model_identifier=args.model,
                    model_revision=args.model_revision,
                )
            )
            _print_json(
                {
                    "plan_sha256": plan_sha,
                    "path": str(path),
                    "corpus_manifest_sha256": (
                        plan.corpus_manifest_sha256
                    ),
                    "model_identifier": (
                        plan.model_identifier
                    ),
                    "model_revision": plan.model_revision,
                    "request_count": len(plan.requests),
                    "source_kinds": dict(
                        plan.source_kind_counts
                    ),
                }
            )
        elif args.command == "embed":
            result_set_sha, path, result_set = (
                embed_semantic_plan_with_ollama(
                    args.ai_root,
                    plan_sha256=args.plan_sha,
                    base_url=args.base_url,
                    timeout=args.timeout,
                    batch_size=args.batch_size,
                )
            )
            _print_json(
                {
                    "result_set_sha256": (
                        result_set_sha
                    ),
                    "path": str(path),
                    "plan_sha256": result_set.plan_sha256,
                    "result_count": len(
                        result_set.results
                    ),
                    "vector_dimension": (
                        result_set.vector_dimension
                    ),
                    "vector_encoding": (
                        result_set.vector_encoding
                    ),
                }
            )
        elif args.command == "embed-refresh":
            (
                result_set_sha,
                path,
                result_set,
                stats,
            ) = embed_semantic_plan_incremental_with_ollama(
                args.ai_root,
                plan_sha256=args.plan_sha,
                previous_index_sha256=args.previous_index_sha,
                base_url=args.base_url,
                timeout=args.timeout,
                batch_size=args.batch_size,
            )
            _print_json(
                {
                    "result_set_sha256": result_set_sha,
                    "path": str(path),
                    "plan_sha256": result_set.plan_sha256,
                    "result_count": len(result_set.results),
                    "vector_dimension": result_set.vector_dimension,
                    "vector_encoding": result_set.vector_encoding,
                    "reused_count": stats.reused_count,
                    "embedded_count": stats.embedded_count,
                    "removed_count": stats.removed_count,
                }
            )
        else:
            index_sha, path, manifest = (
                finalize_semantic_index(
                    args.ai_root,
                    args.vault_root,
                    plan_sha256=args.plan_sha,
                    result_set_sha256=(
                        args.result_set_sha
                    ),
                )
            )
            _print_json(
                {
                    "index_sha256": index_sha,
                    "path": str(path),
                    "corpus_manifest_sha256": (
                        manifest.corpus_manifest_sha256
                    ),
                    "embedding_plan_sha256": (
                        manifest.embedding_plan_sha256
                    ),
                    "embedding_result_set_sha256": (
                        manifest.embedding_result_set_sha256
                    ),
                    "model_identifier": (
                        manifest.model_identifier
                    ),
                    "model_revision": (
                        manifest.model_revision
                    ),
                    "vector_dimension": (
                        manifest.vector_dimension
                    ),
                    "vector_encoding": (
                        manifest.vector_encoding
                    ),
                    "vector_count": len(
                        manifest.vectors
                    ),
                    "source_kinds": dict(
                        manifest.source_kind_counts
                    ),
                }
            )
    except (
        ArtifactLifecycleError,
        OllamaProviderError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
