from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Mapping

from .knowledge_index import KnowledgeIndex, IndexedDocument, tokenize


COVERAGE_THRESHOLDS = (0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)


@dataclass(frozen=True)
class QueryCoverage:
    coverage: float
    matched_weight: float
    total_weight: float
    matched_terms: tuple[str, ...]
    missing_terms: tuple[str, ...]


def query_idf_weights(index: KnowledgeIndex, query: str) -> dict[str, float]:
    terms = sorted(set(tokenize(query)))
    if not terms:
        return {}

    n_docs = len(index.documents)
    if n_docs == 0:
        return {term: 1.0 for term in terms}

    wanted = set(terms)
    doc_freq: Counter[str] = Counter()
    for doc in index.documents:
        for term in doc.term_freq.keys() & wanted:
            doc_freq[term] += 1

    return {
        term: math.log(
            1.0 + (n_docs - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5)
        )
        for term in terms
    }


def _coverage_from_weights(
    document: IndexedDocument,
    weights: Mapping[str, float],
) -> QueryCoverage:
    if not weights:
        return QueryCoverage(
            coverage=0.0,
            matched_weight=0.0,
            total_weight=0.0,
            matched_terms=(),
            missing_terms=(),
        )

    matched_terms = tuple(
        term for term in weights if document.term_freq.get(term, 0) > 0
    )
    missing_terms = tuple(
        term for term in weights if document.term_freq.get(term, 0) <= 0
    )
    matched_weight = sum(weights[term] for term in matched_terms)
    total_weight = sum(weights.values())
    coverage = matched_weight / total_weight if total_weight > 0.0 else 0.0

    return QueryCoverage(
        coverage=coverage,
        matched_weight=matched_weight,
        total_weight=total_weight,
        matched_terms=matched_terms,
        missing_terms=missing_terms,
    )


def weighted_query_coverage(
    index: KnowledgeIndex,
    query: str,
    document: IndexedDocument,
) -> QueryCoverage:
    return _coverage_from_weights(document, query_idf_weights(index, query))


def coverage_by_path(index: KnowledgeIndex, query: str) -> dict[str, QueryCoverage]:
    weights = query_idf_weights(index, query)
    return {
        doc.path: _coverage_from_weights(doc, weights)
        for doc in index.documents
    }
