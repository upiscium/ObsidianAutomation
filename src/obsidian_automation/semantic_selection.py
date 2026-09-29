from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .artifact_lifecycle import (
    ArtifactLifecycleError,
    _canonical_json_bytes,
    _decode_json_object,
    _read_exact_file,
    _require_safe_directory,
    _require_sha256,
    _store_immutable,
    _utc_now,
    sha256_bytes,
)
from .context_bundle import load_context_bundle
from .planner_cadence import record_novelty_skip
from .pre_review_job import PreReviewJobError, _connect_ro
from .semantic_corpus import SemanticCorpusManifest
from .semantic_index import SemanticIndexManifest
from .semantic_retrieval import (
    DEFAULT_LEXICAL_WEIGHT,
    MAX_TOP_K,
    RankedSemanticChunk,
    RetrievalFilter,
    SOURCE_KINDS,
    SemanticCandidate,
    SemanticRetrievalError,
    load_verified_semantic_candidates,
    parse_retrieval_filter,
    rank_semantic_chunks,
)


SELECTION_RECORD_VERSION = 1
SELECTION_DIR = "semantic-selections"
MAX_SELECTED_CHUNKS = 8
DEFAULT_SELECTED_CHUNKS = 6
DEFAULT_RECENT_CONTEXT_LIMIT = 8
MAX_RECENT_CONTEXT_LIMIT = 32
ANCHOR_QUERY_CHARS = 4096

RECENT_CONTEXT_SKIP_THRESHOLD = 0.94
FOCUS_KNOWLEDGE_SKIP_THRESHOLD = 0.96
PROJECT_KNOWLEDGE_SKIP_THRESHOLD = 0.98
TIMELINE_KNOWLEDGE_SKIP_THRESHOLD = 0.97
BRIDGE_KNOWLEDGE_SKIP_THRESHOLD = 0.98
GAP_MAX_KNOWLEDGE_SIMILARITY = 0.78

DEFAULT_CLUSTER_COHERENCE_MIN = 0.35
BRIDGE_CLUSTER_COHERENCE_MIN = 0.20
GAP_SUPPORT_MIN = 0.35
BRIDGE_PAIR_MIN = 0.30
BRIDGE_PAIR_MAX = 0.82

POLICIES = (
    "semantic-focus-v0",
    "semantic-project-distill-v0",
    "semantic-timeline-v0",
    "semantic-bridge-v0",
    "semantic-gap-v0",
    "semantic-idea-development-v0",
)


class SemanticSelectionError(ArtifactLifecycleError):
    """Raised when a Semantic Planner selection cannot be reproduced safely."""


@dataclass(frozen=True)
class AnchorBinding:
    role: str
    chunk_id: str
    source_path: str
    source_kind: str
    source_sha256: str
    content_sha256: str

    def payload(self) -> dict[str, str]:
        return {
            "role": self.role,
            "chunk_id": self.chunk_id,
            "source_path": self.source_path,
            "source_kind": self.source_kind,
            "source_sha256": self.source_sha256,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class SelectedChunk:
    rank: int
    role: str
    chunk_id: str
    source_path: str
    source_kind: str
    source_sha256: str
    content_sha256: str
    score: float
    lexical_score: float
    lexical_normalized: float
    cosine_score: float
    semantic_normalized: float
    source_kind_weight: float

    def payload(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "role": self.role,
            "chunk_id": self.chunk_id,
            "source_path": self.source_path,
            "source_kind": self.source_kind,
            "source_sha256": self.source_sha256,
            "content_sha256": self.content_sha256,
            "score": _round(self.score),
            "lexical_score": _round(self.lexical_score),
            "lexical_normalized": _round(self.lexical_normalized),
            "cosine_score": _round(self.cosine_score),
            "semantic_normalized": _round(self.semantic_normalized),
            "source_kind_weight": _round(self.source_kind_weight),
        }


@dataclass(frozen=True)
class RecentContextObservation:
    context_sha256: str
    similarity: float

    def payload(self) -> dict[str, object]:
        return {
            "context_sha256": self.context_sha256,
            "similarity": _round(self.similarity),
        }


@dataclass(frozen=True)
class NoveltyObservation:
    decision: str
    skip_reason: str | None
    cluster_coherence: float
    recent_context_max_similarity: float | None
    knowledge_max_similarity: float | None
    recent_contexts: tuple[RecentContextObservation, ...]
    recent_context_unmatched_count: int
    thresholds: Mapping[str, float | None]

    def payload(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "skip_reason": self.skip_reason,
            "cluster_coherence": _round(self.cluster_coherence),
            "recent_context_max_similarity": (
                None
                if self.recent_context_max_similarity is None
                else _round(self.recent_context_max_similarity)
            ),
            "knowledge_max_similarity": (
                None
                if self.knowledge_max_similarity is None
                else _round(self.knowledge_max_similarity)
            ),
            "recent_contexts": [item.payload() for item in self.recent_contexts],
            "recent_context_unmatched_count": self.recent_context_unmatched_count,
            "thresholds": {
                key: None if value is None else _round(value)
                for key, value in sorted(self.thresholds.items())
            },
        }


@dataclass(frozen=True)
class SemanticSelectionRecord:
    selection_policy: str
    semantic_index_sha256: str
    corpus_manifest_sha256: str
    metadata_filters: Mapping[str, object]
    retrieval_mode: str
    lexical_weight: float
    source_kind_weights: Mapping[str, float]
    anchors: tuple[AnchorBinding, ...]
    selected: tuple[SelectedChunk, ...]
    novelty: NoveltyObservation
    policy_observations: Mapping[str, object]

    def to_json_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "record_version": SELECTION_RECORD_VERSION,
                "selection_policy": self.selection_policy,
                "semantic_index_sha256": self.semantic_index_sha256,
                "corpus_manifest_sha256": self.corpus_manifest_sha256,
                "metadata_filters": dict(self.metadata_filters),
                "retrieval": {
                    "mode": self.retrieval_mode,
                    "lexical_weight": _round(self.lexical_weight),
                    "vector_weight": _round(1.0 - self.lexical_weight),
                    "source_kind_weights": dict(
                        sorted(self.source_kind_weights.items())
                    ),
                },
                "anchors": [item.payload() for item in self.anchors],
                "selected": [item.payload() for item in self.selected],
                "novelty": self.novelty.payload(),
                "policy_observations": dict(
                    sorted(self.policy_observations.items())
                ),
            }
        )


def _round(value: float) -> float:
    return round(float(value), 8)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise SemanticSelectionError("selection vector dimensions do not match")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    score = sum(a * b for a, b in zip(left, right, strict=True)) / (
        left_norm * right_norm
    )
    return max(-1.0, min(1.0, score))


def _centroid(vectors: Sequence[Sequence[float]]) -> tuple[float, ...]:
    if not vectors:
        raise SemanticSelectionError("cannot build centroid from no vectors")
    dimension = len(vectors[0])
    if dimension == 0 or any(len(item) != dimension for item in vectors):
        raise SemanticSelectionError("selection centroid dimensions do not match")
    return tuple(
        sum(item[index] for item in vectors) / len(vectors)
        for index in range(dimension)
    )


def _stable_candidate_key(candidate: SemanticCandidate) -> tuple[str, str, str]:
    return (
        candidate.source.path.casefold(),
        candidate.source.path,
        candidate.chunk.chunk_id,
    )


def _candidate_by_chunk(
    candidates: Sequence[SemanticCandidate],
) -> dict[str, SemanticCandidate]:
    result = {item.chunk.chunk_id: item for item in candidates}
    if len(result) != len(candidates):
        raise SemanticSelectionError("semantic candidates contain duplicate chunk ids")
    return result


def _anchor_binding(
    candidate: SemanticCandidate,
    *,
    role: str,
) -> AnchorBinding:
    return AnchorBinding(
        role=role,
        chunk_id=candidate.chunk.chunk_id,
        source_path=candidate.source.path,
        source_kind=candidate.source.source_kind,
        source_sha256=candidate.source.content_sha256,
        content_sha256=candidate.chunk.content_sha256,
    )


def _density(
    candidate: SemanticCandidate,
    candidates: Sequence[SemanticCandidate],
) -> float:
    similarities = sorted(
        (
            _cosine(candidate.vector.vector, other.vector.vector)
            for other in candidates
            if other.chunk.chunk_id != candidate.chunk.chunk_id
        ),
        reverse=True,
    )
    positive = [value for value in similarities[:3] if value > 0.0]
    return sum(positive) / len(positive) if positive else 0.0


def _choose_focus_anchor(
    candidates: Sequence[SemanticCandidate],
) -> SemanticCandidate | None:
    pool = [
        item
        for item in candidates
        if item.source.source_kind != "knowledge"
    ] or list(candidates)
    if not pool:
        return None
    return min(
        pool,
        key=lambda item: (
            -_density(item, candidates),
            *_stable_candidate_key(item),
        ),
    )


def _choose_project_anchor(
    candidates: Sequence[SemanticCandidate],
) -> SemanticCandidate | None:
    pool = [
        item
        for item in candidates
        if item.source.source_kind in {"project", "project-note"}
    ]
    if not pool:
        return None
    return min(
        pool,
        key=lambda item: (
            -_density(item, candidates),
            0 if item.source.source_kind == "project-note" else 1,
            *_stable_candidate_key(item),
        ),
    )


def _choose_timeline_anchor(
    candidates: Sequence[SemanticCandidate],
) -> SemanticCandidate | None:
    pool = [
        item for item in candidates if item.source.source_kind == "daily"
    ]
    if not pool:
        return None
    ordered = sorted(
        pool,
        key=lambda item: (
            str(item.source.metadata.get("date", "")),
            _density(item, candidates),
        ),
        reverse=True,
    )
    return ordered[0]


def _choose_idea_anchor(
    candidates: Sequence[SemanticCandidate],
) -> SemanticCandidate | None:
    pool = [
        item
        for item in candidates
        if item.source.source_kind == "idea"
        and item.source.metadata.get("status") == "active"
    ]
    if not pool:
        return None
    ordered = sorted(
        pool,
        key=lambda item: (
            str(item.source.metadata.get("created", "")),
            _density(item, candidates),
        ),
        reverse=True,
    )
    return ordered[0]


def _knowledge_similarity(
    anchor_vector: Sequence[float],
    candidates: Sequence[SemanticCandidate],
) -> float | None:
    knowledge = [
        _cosine(anchor_vector, item.vector.vector)
        for item in candidates
        if item.source.source_kind == "knowledge"
    ]
    return max(knowledge) if knowledge else None


def _choose_gap_anchor(
    candidates: Sequence[SemanticCandidate],
) -> tuple[SemanticCandidate | None, float | None, float]:
    nonknowledge = [
        item
        for item in candidates
        if item.source.source_kind != "knowledge"
    ]
    if not nonknowledge:
        return None, None, 0.0
    rows: list[tuple[float, float, SemanticCandidate]] = []
    for item in nonknowledge:
        knowledge_similarity = _knowledge_similarity(
            item.vector.vector,
            candidates,
        )
        support = max(
            (
                _cosine(item.vector.vector, other.vector.vector)
                for other in nonknowledge
                if other.source.path != item.source.path
            ),
            default=0.0,
        )
        gap_score = support - max(knowledge_similarity or 0.0, 0.0)
        rows.append((gap_score, support, item))
    rows.sort(
        key=lambda row: (
            -row[0],
            -row[1],
            *_stable_candidate_key(row[2]),
        )
    )
    _score, support, anchor = rows[0]
    return anchor, _knowledge_similarity(anchor.vector.vector, candidates), support


def _choose_bridge_anchors(
    candidates: Sequence[SemanticCandidate],
) -> tuple[SemanticCandidate, SemanticCandidate, float] | None:
    pool = [
        item
        for item in candidates
        if item.source.source_kind != "knowledge"
    ]
    best: tuple[float, SemanticCandidate, SemanticCandidate] | None = None
    for index, left in enumerate(pool):
        for right in pool[index + 1 :]:
            if (
                left.source.path == right.source.path
                or left.source.source_kind == right.source.source_kind
            ):
                continue
            similarity = _cosine(left.vector.vector, right.vector.vector)
            if not BRIDGE_PAIR_MIN <= similarity <= BRIDGE_PAIR_MAX:
                continue
            row = (similarity, left, right)
            if best is None:
                best = row
                continue
            if similarity > best[0]:
                best = row
            elif similarity == best[0]:
                current_key = (
                    *_stable_candidate_key(left),
                    *_stable_candidate_key(right),
                )
                best_key = (
                    *_stable_candidate_key(best[1]),
                    *_stable_candidate_key(best[2]),
                )
                if current_key < best_key:
                    best = row
    if best is None:
        return None
    return best[1], best[2], best[0]


def _anchor_query(anchors: Sequence[SemanticCandidate]) -> str:
    text = "\\n\\n".join(item.text.strip() for item in anchors if item.text.strip())
    text = text[:ANCHOR_QUERY_CHARS].strip()
    if not text:
        raise SemanticSelectionError("semantic anchor text is empty")
    return text


def _source_kind_weights(policy: str) -> dict[str, float]:
    weights = {
        "daily": 1.0,
        "idea": 1.0,
        "project": 1.0,
        "project-note": 1.0,
        "knowledge": 1.0,
    }
    if policy == "semantic-project-distill-v0":
        weights.update(
            {
                "daily": 1.05,
                "idea": 1.0,
                "project": 0.9,
                "project-note": 1.15,
                "knowledge": 1.05,
            }
        )
    elif policy == "semantic-timeline-v0":
        weights.update({"daily": 1.2, "knowledge": 1.05})
    elif policy == "semantic-gap-v0":
        weights.update(
            {
                "daily": 1.15,
                "idea": 1.15,
                "project": 1.0,
                "project-note": 1.05,
                "knowledge": 0.8,
            }
        )
    elif policy == "semantic-idea-development-v0":
        weights.update(
            {
                "daily": 0.8,
                "idea": 1.1,
                "project": 1.2,
                "project-note": 1.0,
                "knowledge": 1.1,
            }
        )
    return weights


def _rank_for_anchors(
    candidates: Sequence[SemanticCandidate],
    anchors: Sequence[SemanticCandidate],
    *,
    policy: str,
) -> tuple[RankedSemanticChunk, ...]:
    return rank_semantic_chunks(
        candidates,
        query=_anchor_query(anchors),
        query_vector=_centroid([item.vector.vector for item in anchors]),
        mode="hybrid",
        source_kind_weights=_source_kind_weights(policy),
        lexical_weight=DEFAULT_LEXICAL_WEIGHT,
        top_k=MAX_TOP_K,
    )


def _unique_source_rows(
    ranked: Sequence[RankedSemanticChunk],
    *,
    required_chunks: Sequence[str],
    max_selected: int,
    preferred_kinds: Sequence[str] = (),
) -> tuple[RankedSemanticChunk, ...]:
    by_chunk = {item.chunk_id: item for item in ranked}
    selected: list[RankedSemanticChunk] = []
    seen_paths: set[str] = set()

    def add(item: RankedSemanticChunk | None) -> None:
        if item is None or item.source_path in seen_paths:
            return
        if len(selected) >= max_selected:
            return
        selected.append(item)
        seen_paths.add(item.source_path)

    for chunk_id in required_chunks:
        add(by_chunk.get(chunk_id))

    for kind in preferred_kinds:
        add(next((item for item in ranked if item.source_kind == kind), None))

    for item in ranked:
        add(item)
        if len(selected) >= max_selected:
            break
    return tuple(selected)


def _cluster_coherence(
    selected: Sequence[RankedSemanticChunk],
    candidate_map: Mapping[str, SemanticCandidate],
) -> float:
    if len(selected) < 2:
        return 0.0
    values: list[float] = []
    for index, left in enumerate(selected):
        for right in selected[index + 1 :]:
            values.append(
                _cosine(
                    candidate_map[left.chunk_id].vector.vector,
                    candidate_map[right.chunk_id].vector.vector,
                )
            )
    return sum(values) / len(values) if values else 0.0


def _context_centroid(
    ai_root: Path,
    context_sha256: str,
    candidates: Sequence[SemanticCandidate],
) -> tuple[float, ...] | None:
    context = load_context_bundle(ai_root, context_sha256)
    vectors: list[Sequence[float]] = []
    for source in context.sources:
        for candidate in candidates:
            if (
                candidate.source.path == source.path
                and candidate.source.content_sha256 == source.content_sha256
            ):
                vectors.append(candidate.vector.vector)
    return _centroid(vectors) if vectors else None


def _recent_context_observations(
    ai_root: Path,
    *,
    selection_vector: Sequence[float],
    candidates: Sequence[SemanticCandidate],
    limit: int,
) -> tuple[tuple[RecentContextObservation, ...], int]:
    if type(limit) is not int or not 0 <= limit <= MAX_RECENT_CONTEXT_LIMIT:
        raise SemanticSelectionError(
            f"recent_context_limit must be 0..{MAX_RECENT_CONTEXT_LIMIT}"
        )
    if limit == 0:
        return (), 0
    try:
        conn = _connect_ro(ai_root)
    except PreReviewJobError as exc:
        if "database does not exist" in str(exc):
            return (), 0
        raise SemanticSelectionError(str(exc)) from exc
    try:
        rows = conn.execute(
            """
            SELECT context_sha256, created_at
            FROM jobs
            ORDER BY created_at DESC, job_id DESC
            LIMIT ?
            """,
            (limit * 2,),
        ).fetchall()
    finally:
        conn.close()

    observations: list[RecentContextObservation] = []
    unmatched = 0
    seen: set[str] = set()
    for row in rows:
        context_sha = str(row["context_sha256"])
        if context_sha in seen:
            continue
        seen.add(context_sha)
        centroid = _context_centroid(ai_root, context_sha, candidates)
        if centroid is None:
            unmatched += 1
            continue
        observations.append(
            RecentContextObservation(
                context_sha256=context_sha,
                similarity=_round(_cosine(selection_vector, centroid)),
            )
        )
        if len(observations) >= limit:
            break
    observations.sort(
        key=lambda item: (-item.similarity, item.context_sha256)
    )
    return tuple(observations), unmatched


def _selected_chunks(
    ranked: Sequence[RankedSemanticChunk],
    *,
    anchor_ids: set[str],
) -> tuple[SelectedChunk, ...]:
    return tuple(
        SelectedChunk(
            rank=index,
            role="anchor" if item.chunk_id in anchor_ids else "support",
            chunk_id=item.chunk_id,
            source_path=item.source_path,
            source_kind=item.source_kind,
            source_sha256=item.source_sha256,
            content_sha256=item.content_sha256,
            score=_round(item.score),
            lexical_score=_round(item.lexical_score),
            lexical_normalized=_round(item.lexical_normalized),
            cosine_score=_round(item.cosine_score),
            semantic_normalized=_round(item.semantic_normalized),
            source_kind_weight=_round(item.source_kind_weight),
        )
        for index, item in enumerate(ranked, 1)
    )


def _policy_thresholds(policy: str) -> dict[str, float | None]:
    knowledge_limit: float | None
    coherence = DEFAULT_CLUSTER_COHERENCE_MIN
    if policy == "semantic-focus-v0":
        knowledge_limit = FOCUS_KNOWLEDGE_SKIP_THRESHOLD
    elif policy == "semantic-project-distill-v0":
        knowledge_limit = PROJECT_KNOWLEDGE_SKIP_THRESHOLD
    elif policy == "semantic-timeline-v0":
        knowledge_limit = TIMELINE_KNOWLEDGE_SKIP_THRESHOLD
    elif policy == "semantic-bridge-v0":
        knowledge_limit = BRIDGE_KNOWLEDGE_SKIP_THRESHOLD
        coherence = BRIDGE_CLUSTER_COHERENCE_MIN
    elif policy == "semantic-gap-v0":
        knowledge_limit = GAP_MAX_KNOWLEDGE_SIMILARITY
    elif policy == "semantic-idea-development-v0":
        knowledge_limit = None
    else:
        raise SemanticSelectionError(f"unsupported semantic selection policy: {policy}")
    return {
        "recent_context_skip": RECENT_CONTEXT_SKIP_THRESHOLD,
        "knowledge_coverage_skip": knowledge_limit,
        "cluster_coherence_min": coherence,
        "gap_support_min": GAP_SUPPORT_MIN if policy == "semantic-gap-v0" else None,
        "bridge_pair_min": BRIDGE_PAIR_MIN if policy == "semantic-bridge-v0" else None,
        "bridge_pair_max": BRIDGE_PAIR_MAX if policy == "semantic-bridge-v0" else None,
    }


def _decision(
    *,
    policy: str,
    selected: Sequence[RankedSemanticChunk],
    cluster_coherence: float,
    recent_max: float | None,
    knowledge_max: float | None,
    policy_observations: Mapping[str, object],
) -> tuple[str, str | None]:
    thresholds = _policy_thresholds(policy)
    if len(selected) < 2:
        return "skipped", "insufficient_cluster_size"
    if cluster_coherence < float(thresholds["cluster_coherence_min"] or 0.0):
        return "skipped", "insufficient_cluster_coherence"
    if (
        recent_max is not None
        and recent_max >= float(thresholds["recent_context_skip"] or 1.0)
    ):
        return "skipped", "recent_context_too_similar"

    knowledge_limit = thresholds["knowledge_coverage_skip"]
    if policy == "semantic-gap-v0":
        support = policy_observations.get("nonknowledge_support_similarity")
        if not isinstance(support, (int, float)) or support < GAP_SUPPORT_MIN:
            return "skipped", "insufficient_gap_support"
        if knowledge_max is not None and knowledge_max >= GAP_MAX_KNOWLEDGE_SIMILARITY:
            return "skipped", "knowledge_gap_too_weak"
    elif (
        knowledge_limit is not None
        and knowledge_max is not None
        and knowledge_max >= knowledge_limit
    ):
        return "skipped", "existing_knowledge_coverage"

    return "selected", None


def _selection_dir(ai_root: Path) -> Path:
    root = ai_root.absolute()
    _require_safe_directory(root, create=False)
    orchestration = root / "02-Orchestration"
    _require_safe_directory(orchestration, create=True)
    directory = orchestration / SELECTION_DIR
    _require_safe_directory(directory, create=True)
    return directory


def store_semantic_selection(
    ai_root: Path,
    record: SemanticSelectionRecord,
) -> tuple[str, Path]:
    data = record.to_json_bytes()
    parsed = parse_semantic_selection(data)
    if parsed != record:
        raise SemanticSelectionError("semantic selection canonical round-trip mismatch")
    digest = sha256_bytes(data)
    path = _selection_dir(ai_root) / f"{digest}.semantic-selection.json"
    return digest, _store_immutable(path, data)


def _require_score(value: object, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SemanticSelectionError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise SemanticSelectionError(f"{label} must be finite")
    return number


def parse_semantic_selection(data: bytes) -> SemanticSelectionRecord:
    value = _decode_json_object(data, label="semantic selection record")
    if set(value) != {
        "record_version",
        "selection_policy",
        "semantic_index_sha256",
        "corpus_manifest_sha256",
        "metadata_filters",
        "retrieval",
        "anchors",
        "selected",
        "novelty",
        "policy_observations",
    }:
        raise SemanticSelectionError(
            "semantic selection properties do not match contract"
        )
    if value["record_version"] != SELECTION_RECORD_VERSION:
        raise SemanticSelectionError("unsupported semantic selection version")
    policy = value["selection_policy"]
    if policy not in POLICIES:
        raise SemanticSelectionError("semantic selection policy is unsupported")
    index_sha = _require_sha256(value["semantic_index_sha256"], label="semantic index SHA")
    corpus_sha = _require_sha256(value["corpus_manifest_sha256"], label="corpus manifest SHA")
    filters = value["metadata_filters"]
    if not isinstance(filters, dict):
        raise SemanticSelectionError("semantic selection metadata filters are invalid")
    try:
        normalized_filters = parse_retrieval_filter(filters).payload()
    except SemanticRetrievalError as exc:
        raise SemanticSelectionError(str(exc)) from exc

    retrieval = value["retrieval"]
    if not isinstance(retrieval, dict) or set(retrieval) != {
        "mode",
        "lexical_weight",
        "vector_weight",
        "source_kind_weights",
    }:
        raise SemanticSelectionError("semantic selection retrieval contract is invalid")
    if retrieval["mode"] != "hybrid":
        raise SemanticSelectionError("semantic selection retrieval mode must be hybrid")
    lexical_weight = _require_score(retrieval["lexical_weight"], label="lexical_weight")
    vector_weight = _require_score(retrieval["vector_weight"], label="vector_weight")
    if not 0.0 <= lexical_weight <= 1.0 or abs((lexical_weight + vector_weight) - 1.0) > 1e-8:
        raise SemanticSelectionError("semantic selection retrieval weights are invalid")
    raw_weights = retrieval["source_kind_weights"]
    if not isinstance(raw_weights, dict) or set(raw_weights) != {
        "daily", "idea", "project", "project-note", "knowledge"
    }:
        raise SemanticSelectionError("semantic selection source-kind weights are invalid")
    weights = {
        key: _require_score(raw, label=f"source_kind_weights.{key}")
        for key, raw in raw_weights.items()
    }
    if any(value < 0.0 or value > 10.0 for value in weights.values()):
        raise SemanticSelectionError(
            "semantic selection source-kind weights must be in 0..10"
        )

    anchors: list[AnchorBinding] = []
    for raw in value["anchors"]:
        if not isinstance(raw, dict) or set(raw) != {
            "role", "chunk_id", "source_path", "source_kind", "source_sha256", "content_sha256"
        }:
            raise SemanticSelectionError("semantic selection anchor is invalid")
        role = raw["role"]
        source_path = raw["source_path"]
        source_kind = raw["source_kind"]
        if role not in {"primary", "bridge-secondary"}:
            raise SemanticSelectionError("semantic selection anchor role is invalid")
        if not isinstance(source_path, str) or not source_path:
            raise SemanticSelectionError("semantic selection anchor source path is invalid")
        if source_kind not in SOURCE_KINDS:
            raise SemanticSelectionError("semantic selection anchor source kind is invalid")
        anchors.append(
            AnchorBinding(
                role=role,
                chunk_id=_require_sha256(raw["chunk_id"], label="anchor chunk id"),
                source_path=source_path,
                source_kind=source_kind,
                source_sha256=_require_sha256(raw["source_sha256"], label="anchor source SHA"),
                content_sha256=_require_sha256(raw["content_sha256"], label="anchor content SHA"),
            )
        )
    if not 1 <= len(anchors) <= 2:
        raise SemanticSelectionError("semantic selection must contain one or two anchors")

    selected: list[SelectedChunk] = []
    raw_selected = value["selected"]
    if not isinstance(raw_selected, list) or not 1 <= len(raw_selected) <= MAX_SELECTED_CHUNKS:
        raise SemanticSelectionError("semantic selection selected chunks are invalid")
    seen_chunks: set[str] = set()
    seen_paths: set[str] = set()
    for expected_rank, raw in enumerate(raw_selected, 1):
        if not isinstance(raw, dict) or set(raw) != {
            "rank", "role", "chunk_id", "source_path", "source_kind",
            "source_sha256", "content_sha256", "score", "lexical_score",
            "lexical_normalized", "cosine_score", "semantic_normalized",
            "source_kind_weight",
        }:
            raise SemanticSelectionError("semantic selection selected chunk is invalid")
        if raw["rank"] != expected_rank:
            raise SemanticSelectionError("semantic selection ranks are not contiguous")
        chunk_id = _require_sha256(raw["chunk_id"], label="selected chunk id")
        source_path = raw["source_path"]
        if not isinstance(source_path, str) or not source_path:
            raise SemanticSelectionError("semantic selection source path is invalid")
        if chunk_id in seen_chunks or source_path.casefold() in seen_paths:
            raise SemanticSelectionError("semantic selection contains duplicate source/chunk")
        seen_chunks.add(chunk_id)
        seen_paths.add(source_path.casefold())
        role = raw["role"]
        source_kind = raw["source_kind"]
        if role not in {"anchor", "support"}:
            raise SemanticSelectionError("semantic selection selected role is invalid")
        if source_kind not in SOURCE_KINDS:
            raise SemanticSelectionError("semantic selection selected source kind is invalid")
        selected.append(
            SelectedChunk(
                rank=expected_rank,
                role=role,
                chunk_id=chunk_id,
                source_path=source_path,
                source_kind=source_kind,
                source_sha256=_require_sha256(raw["source_sha256"], label="selected source SHA"),
                content_sha256=_require_sha256(raw["content_sha256"], label="selected content SHA"),
                score=_require_score(raw["score"], label="selected score"),
                lexical_score=_require_score(raw["lexical_score"], label="selected lexical score"),
                lexical_normalized=_require_score(raw["lexical_normalized"], label="selected lexical normalized"),
                cosine_score=_require_score(raw["cosine_score"], label="selected cosine score"),
                semantic_normalized=_require_score(raw["semantic_normalized"], label="selected semantic normalized"),
                source_kind_weight=_require_score(raw["source_kind_weight"], label="selected source-kind weight"),
            )
        )

    raw_novelty = value["novelty"]
    if not isinstance(raw_novelty, dict) or set(raw_novelty) != {
        "decision", "skip_reason", "cluster_coherence",
        "recent_context_max_similarity", "knowledge_max_similarity",
        "recent_contexts", "recent_context_unmatched_count", "thresholds",
    }:
        raise SemanticSelectionError("semantic selection novelty contract is invalid")
    decision = raw_novelty["decision"]
    skip_reason = raw_novelty["skip_reason"]
    if decision not in {"selected", "skipped"}:
        raise SemanticSelectionError("semantic selection novelty decision is invalid")
    if (decision == "selected" and skip_reason is not None) or (
        decision == "skipped" and (not isinstance(skip_reason, str) or not skip_reason)
    ):
        raise SemanticSelectionError("semantic selection skip reason is inconsistent")
    recent_contexts: list[RecentContextObservation] = []
    for raw in raw_novelty["recent_contexts"]:
        if not isinstance(raw, dict) or set(raw) != {"context_sha256", "similarity"}:
            raise SemanticSelectionError("recent Context observation is invalid")
        recent_contexts.append(
            RecentContextObservation(
                context_sha256=_require_sha256(raw["context_sha256"], label="recent Context SHA"),
                similarity=_require_score(raw["similarity"], label="recent Context similarity"),
            )
        )
    unmatched = raw_novelty["recent_context_unmatched_count"]
    if type(unmatched) is not int or unmatched < 0:
        raise SemanticSelectionError("recent Context unmatched count is invalid")
    thresholds = raw_novelty["thresholds"]
    if not isinstance(thresholds, dict):
        raise SemanticSelectionError("semantic selection thresholds are invalid")
    normalized_thresholds: dict[str, float | None] = {}
    for key, raw in thresholds.items():
        normalized_thresholds[str(key)] = (
            None if raw is None else _require_score(raw, label=f"thresholds.{key}")
        )
    novelty = NoveltyObservation(
        decision=decision,
        skip_reason=skip_reason,
        cluster_coherence=_require_score(raw_novelty["cluster_coherence"], label="cluster coherence"),
        recent_context_max_similarity=(
            None
            if raw_novelty["recent_context_max_similarity"] is None
            else _require_score(raw_novelty["recent_context_max_similarity"], label="recent Context max similarity")
        ),
        knowledge_max_similarity=(
            None
            if raw_novelty["knowledge_max_similarity"] is None
            else _require_score(raw_novelty["knowledge_max_similarity"], label="Knowledge max similarity")
        ),
        recent_contexts=tuple(recent_contexts),
        recent_context_unmatched_count=unmatched,
        thresholds=normalized_thresholds,
    )
    observations = value["policy_observations"]
    if not isinstance(observations, dict):
        raise SemanticSelectionError("semantic selection policy observations are invalid")
    return SemanticSelectionRecord(
        selection_policy=policy,
        semantic_index_sha256=index_sha,
        corpus_manifest_sha256=corpus_sha,
        metadata_filters=normalized_filters,
        retrieval_mode="hybrid",
        lexical_weight=lexical_weight,
        source_kind_weights=weights,
        anchors=tuple(anchors),
        selected=tuple(selected),
        novelty=novelty,
        policy_observations=observations,
    )


def load_semantic_selection(
    ai_root: Path,
    selection_sha256: str,
) -> SemanticSelectionRecord:
    digest = _require_sha256(selection_sha256, label="semantic selection SHA")
    path = _selection_dir(ai_root) / f"{digest}.semantic-selection.json"
    data = _read_exact_file(path)
    if sha256_bytes(data) != digest:
        raise SemanticSelectionError("semantic selection artifact hash mismatch")
    return parse_semantic_selection(data)


def _make_record(
    ai_root: Path,
    *,
    semantic_index_sha256: str,
    index: SemanticIndexManifest,
    corpus: SemanticCorpusManifest,
    candidates: Sequence[SemanticCandidate],
    policy: str,
    filters: RetrievalFilter,
    anchors: Sequence[SemanticCandidate],
    ranked: Sequence[RankedSemanticChunk],
    policy_observations: Mapping[str, object],
    recent_context_limit: int,
) -> SemanticSelectionRecord:
    candidate_map = _candidate_by_chunk(candidates)
    selected_vector = _centroid(
        [candidate_map[item.chunk_id].vector.vector for item in ranked]
    )
    anchor_vector = _centroid([item.vector.vector for item in anchors])
    coherence = _cluster_coherence(ranked, candidate_map)
    recent, unmatched = _recent_context_observations(
        ai_root,
        selection_vector=selected_vector,
        candidates=candidates,
        limit=recent_context_limit,
    )
    recent_max = recent[0].similarity if recent else None
    knowledge_raw = _knowledge_similarity(anchor_vector, candidates)
    knowledge_max = None if knowledge_raw is None else _round(knowledge_raw)
    coherence = _round(coherence)
    decision, skip_reason = _decision(
        policy=policy,
        selected=ranked,
        cluster_coherence=coherence,
        recent_max=recent_max,
        knowledge_max=knowledge_max,
        policy_observations=policy_observations,
    )
    anchor_ids = {item.chunk.chunk_id for item in anchors}
    return SemanticSelectionRecord(
        selection_policy=policy,
        semantic_index_sha256=semantic_index_sha256,
        corpus_manifest_sha256=index.corpus_manifest_sha256,
        metadata_filters=filters.payload(),
        retrieval_mode="hybrid",
        lexical_weight=DEFAULT_LEXICAL_WEIGHT,
        source_kind_weights=_source_kind_weights(policy),
        anchors=tuple(
            _anchor_binding(
                item,
                role="primary" if position == 0 else "bridge-secondary",
            )
            for position, item in enumerate(anchors)
        ),
        selected=_selected_chunks(ranked, anchor_ids=anchor_ids),
        novelty=NoveltyObservation(
            decision=decision,
            skip_reason=skip_reason,
            cluster_coherence=coherence,
            recent_context_max_similarity=recent_max,
            knowledge_max_similarity=knowledge_max,
            recent_contexts=recent,
            recent_context_unmatched_count=unmatched,
            thresholds=_policy_thresholds(policy),
        ),
        policy_observations=dict(policy_observations),
    )


def build_semantic_selection(
    ai_root: Path,
    vault_root: Path,
    *,
    semantic_index_sha256: str,
    policy: str,
    max_selected: int = DEFAULT_SELECTED_CHUNKS,
    recent_context_limit: int = DEFAULT_RECENT_CONTEXT_LIMIT,
) -> SemanticSelectionRecord:
    if policy not in POLICIES:
        raise SemanticSelectionError(f"unsupported semantic selection policy: {policy}")
    if type(max_selected) is not int or not 2 <= max_selected <= MAX_SELECTED_CHUNKS:
        raise SemanticSelectionError(
            f"max_selected must be 2..{MAX_SELECTED_CHUNKS}"
        )
    index_sha = _require_sha256(
        semantic_index_sha256,
        label="semantic index SHA",
    )
    filters = RetrievalFilter()
    try:
        index, corpus, candidates = load_verified_semantic_candidates(
            ai_root,
            vault_root,
            semantic_index_sha256=index_sha,
            filters=filters,
        )
    except SemanticRetrievalError as exc:
        raise SemanticSelectionError(str(exc)) from exc
    if not candidates:
        raise SemanticSelectionError("semantic index contains no eligible candidates")

    policy_observations: dict[str, object] = {}
    anchors: tuple[SemanticCandidate, ...]
    preferred: tuple[str, ...] = ()

    if policy == "semantic-focus-v0":
        anchor = _choose_focus_anchor(candidates)
        if anchor is None:
            raise SemanticSelectionError("semantic-focus-v0 has no eligible anchor")
        anchors = (anchor,)
    elif policy == "semantic-project-distill-v0":
        anchor = _choose_project_anchor(candidates)
        if anchor is None:
            raise SemanticSelectionError(
                "semantic-project-distill-v0 has no Project anchor"
            )
        anchors = (anchor,)
        preferred = ("project-note", "daily", "idea", "knowledge")
    elif policy == "semantic-timeline-v0":
        anchor = _choose_timeline_anchor(candidates)
        if anchor is None:
            raise SemanticSelectionError("semantic-timeline-v0 has no Daily anchor")
        anchors = (anchor,)
        policy_observations["anchor_date"] = anchor.source.metadata.get("date")
        preferred = ("daily", "knowledge")
    elif policy == "semantic-bridge-v0":
        pair = _choose_bridge_anchors(candidates)
        if pair is None:
            raise SemanticSelectionError(
                "semantic-bridge-v0 has no meaningful cross-kind anchor pair"
            )
        left, right, similarity = pair
        anchors = (left, right)
        policy_observations["anchor_pair_similarity"] = _round(similarity)
    elif policy == "semantic-gap-v0":
        anchor, knowledge_similarity, support = _choose_gap_anchor(candidates)
        if anchor is None:
            raise SemanticSelectionError("semantic-gap-v0 has no non-Knowledge anchor")
        anchors = (anchor,)
        policy_observations["anchor_knowledge_similarity"] = (
            None if knowledge_similarity is None else _round(knowledge_similarity)
        )
        policy_observations["nonknowledge_support_similarity"] = _round(support)
        preferred = ("daily", "idea", "project-note", "knowledge")
    else:
        anchor = _choose_idea_anchor(candidates)
        if anchor is None:
            raise SemanticSelectionError(
                "semantic-idea-development-v0 has no active Idea anchor"
            )
        anchors = (anchor,)
        policy_observations["idea_status"] = anchor.source.metadata.get("status")
        policy_observations["idea_created"] = anchor.source.metadata.get("created")
        preferred = ("knowledge", "project", "project-note")

    ranked_all = _rank_for_anchors(candidates, anchors, policy=policy)
    ranked = _unique_source_rows(
        ranked_all,
        required_chunks=[item.chunk.chunk_id for item in anchors],
        max_selected=max_selected,
        preferred_kinds=preferred,
    )
    if not ranked:
        raise SemanticSelectionError("semantic selection produced no ranked sources")
    return _make_record(
        ai_root,
        semantic_index_sha256=index_sha,
        index=index,
        corpus=corpus,
        candidates=candidates,
        policy=policy,
        filters=filters,
        anchors=anchors,
        ranked=ranked,
        policy_observations=policy_observations,
        recent_context_limit=recent_context_limit,
    )


def observe_semantic_selection(
    ai_root: Path,
    vault_root: Path,
    *,
    semantic_index_sha256: str,
    policy: str,
    max_selected: int = DEFAULT_SELECTED_CHUNKS,
    recent_context_limit: int = DEFAULT_RECENT_CONTEXT_LIMIT,
    record_skip: bool = False,
) -> tuple[str, Path, SemanticSelectionRecord]:
    record = build_semantic_selection(
        ai_root,
        vault_root,
        semantic_index_sha256=semantic_index_sha256,
        policy=policy,
        max_selected=max_selected,
        recent_context_limit=recent_context_limit,
    )
    digest, path = store_semantic_selection(ai_root, record)
    if record_skip and record.novelty.decision == "skipped":
        assert record.novelty.skip_reason is not None
        record_novelty_skip(
            ai_root,
            skipped_at=_utc_now(),
            reason=f"{policy}:{record.novelty.skip_reason}",
        )
    return digest, path, record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-semantic-selection")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--semantic-index-sha", required=True)
    parser.add_argument("--policy", choices=POLICIES, required=True)
    parser.add_argument(
        "--max-selected",
        type=int,
        default=DEFAULT_SELECTED_CHUNKS,
    )
    parser.add_argument(
        "--recent-context-limit",
        type=int,
        default=DEFAULT_RECENT_CONTEXT_LIMIT,
    )
    parser.add_argument("--record-skip", action="store_true")
    args = parser.parse_args(argv)

    try:
        selection_sha, path, record = observe_semantic_selection(
            args.ai_root,
            args.vault_root,
            semantic_index_sha256=args.semantic_index_sha,
            policy=args.policy,
            max_selected=args.max_selected,
            recent_context_limit=args.recent_context_limit,
            record_skip=args.record_skip,
        )
    except (
        ArtifactLifecycleError,
        PreReviewJobError,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "event": "semantic-selection-observation",
                "selection_sha256": selection_sha,
                "path": str(path),
                "selection_policy": record.selection_policy,
                "semantic_index_sha256": record.semantic_index_sha256,
                "decision": record.novelty.decision,
                "skip_reason": record.novelty.skip_reason,
                "anchor_paths": [item.source_path for item in record.anchors],
                "selected_count": len(record.selected),
                "selected_paths": [item.source_path for item in record.selected],
                "cluster_coherence": _round(record.novelty.cluster_coherence),
                "recent_context_max_similarity": record.novelty.recent_context_max_similarity,
                "knowledge_max_similarity": record.novelty.knowledge_max_similarity,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
