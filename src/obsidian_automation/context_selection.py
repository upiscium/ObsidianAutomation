from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .artifact_lifecycle import ArtifactLifecycleError, _require_sha256
from .context_bundle import MAX_CONTEXT_BYTES, build_context_bundle, store_context_bundle
from .knowledge_index import (
    DEFAULT_TOP_K,
    MAX_TOP_K,
    KnowledgeIndex,
    RankedDocument,
    load_knowledge_index,
    rank_documents,
    verify_index_current,
)
from .retrieval_coverage import QueryCoverage, coverage_by_path


CONTEXT_SELECTION_POLICY_VERSION = "bm25-coverage-relative-v0"
MIN_QUERY_COVERAGE = 0.2
RELATIVE_SCORE_CUTOFF = 0.8


@dataclass(frozen=True)
class SelectionResult:
    selected: tuple[RankedDocument, ...]
    coverages: dict[str, QueryCoverage]
    eligible_count: int


def select_context_candidates(
    index: KnowledgeIndex,
    ranked: Sequence[RankedDocument],
    *,
    query: str,
    top_k: int,
) -> SelectionResult:
    if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
        raise ArtifactLifecycleError(
            f"top_k must be an integer in 1..{MAX_TOP_K}"
        )

    coverages = coverage_by_path(index, query)
    eligible = tuple(
        item
        for item in ranked
        if coverages[item.path].coverage >= MIN_QUERY_COVERAGE
    )
    if not eligible:
        return SelectionResult(
            selected=(),
            coverages=coverages,
            eligible_count=0,
        )

    score_threshold = eligible[0].score * RELATIVE_SCORE_CUTOFF
    byte_size_by_path = {doc.path: doc.byte_size for doc in index.documents}

    selected: list[RankedDocument] = []
    total_bytes = 0
    for item in eligible:
        if len(selected) >= top_k:
            break
        if item.score < score_threshold:
            break
        size = byte_size_by_path[item.path]
        if total_bytes + size > MAX_CONTEXT_BYTES:
            continue
        selected.append(item)
        total_bytes += size

    return SelectionResult(
        selected=tuple(selected),
        coverages=coverages,
        eligible_count=len(eligible),
    )


def retrieve_context(
    ai_root: Path,
    vault_root: Path,
    *,
    index_sha256: str,
    query: str,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, object]:
    if type(top_k) is not int or not 1 <= top_k <= MAX_TOP_K:
        raise ArtifactLifecycleError(
            f"top_k must be an integer in 1..{MAX_TOP_K}"
        )

    digest = _require_sha256(index_sha256, label="index_sha256")
    index = load_knowledge_index(ai_root, digest)
    verify_index_current(vault_root, index)

    ranked = rank_documents(index, query)
    selection = select_context_candidates(
        index,
        ranked,
        query=query,
        top_k=top_k,
    )

    bundle = build_context_bundle(
        vault_root,
        query=query,
        source_paths=[item.path for item in selection.selected],
    )

    indexed_by_path = {doc.path: doc for doc in index.documents}
    for source in bundle.sources:
        expected = indexed_by_path[source.path].content_sha256
        if source.content_sha256 != expected:
            raise ArtifactLifecycleError(
                "Knowledge source changed during context construction"
            )

    context_sha, context_path = store_context_bundle(ai_root, bundle)

    return {
        "index_sha256": digest,
        "context_sha256": context_sha,
        "context_path": str(context_path),
        "query": query,
        "matched_count": len(ranked),
        "eligible_count": selection.eligible_count,
        "selection_policy": {
            "version": CONTEXT_SELECTION_POLICY_VERSION,
            "min_query_coverage": MIN_QUERY_COVERAGE,
            "relative_score_cutoff": RELATIVE_SCORE_CUTOFF,
            "absolute_top1_score_gate": None,
        },
        "selected": [
            {
                "path": item.path,
                "content_sha256": item.content_sha256,
                "score": format(item.score, ".12g"),
                "query_coverage": round(
                    selection.coverages[item.path].coverage,
                    6,
                ),
            }
            for item in selection.selected
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obsidian-knowledge-retrieve")
    parser.add_argument("--ai-root", type=Path, required=True)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--index-sha256", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    args = parser.parse_args(argv)

    try:
        result = retrieve_context(
            args.ai_root,
            args.vault_root,
            index_sha256=args.index_sha256,
            query=args.query,
            top_k=args.top_k,
        )
    except (ArtifactLifecycleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
