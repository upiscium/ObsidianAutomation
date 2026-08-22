# Context Selection Policy v0

## Purpose

`Knowledge Index v0` provides deterministic BM25 ranking. Context Selection Policy v0 defines which ranked Knowledge notes are actually allowed to cross the Reader -> Generator boundary as `05-Context` sources.

Ranking and selection remain separate concerns:

```text
Knowledge Index / BM25 ranking
        ↓
Context Selection Policy v0
        ↓
Context Bundle
        ↓
Generator
```

The policy does not add Validation, Human approval, Execution, Transport, or canonical write authority.

## Policy version

```text
bm25-coverage-relative-v0
```

Production values:

```text
minimum IDF-weighted query coverage = 0.2
relative BM25 score cutoff          = 0.8
absolute Top-1 score gate           = disabled
```

## Selection order

```text
BM25 positive-score ranking
  ↓
IDF-weighted query coverage >= 0.2
  ↓
eligible Top-1 becomes the score reference
  ↓
score >= eligible_top1_score * 0.8
  ↓
top-k limit
  ↓
aggregate Context source bytes <= 512 KiB
```

A raw BM25 Top-1 candidate that fails query coverage is removed before the relative score threshold is calculated. The next eligible candidate becomes the effective Top-1.

If no candidate survives coverage, Reader creates an empty Context Bundle. It must not invent or force a fallback source.

## Query coverage

Coverage uses the same deterministic tokenizer as Knowledge Index v0. Query token presence is weighted by BM25-style IDF:

```text
coverage(document, query)
  = sum(IDF(term) for matched unique query terms)
    ------------------------------------------------
    sum(IDF(term) for all unique query terms)
```

Terms that appear in zero indexed documents use `df = 0` and remain in the denominator. This is intentional: a document that matches only a generic prefix of a query should not receive high coverage when discriminative query terms are absent.

Field boosts affect BM25 ranking but do not multiply coverage. Coverage uses term presence only.

## Why 0.2 coverage and 0.8 relative score

The values were selected from a labelled local baseline with 21 positive and 5 negative cases. No private query text or Knowledge path is part of this repository.

The evaluated combination:

```text
minimum coverage = 0.2
relative cutoff  = 0.8
absolute gate    = disabled
```

produced:

```text
micro precision            0.807692
micro recall               0.954545
micro F1                   0.875
positive full-recall rate  0.952381
negative clean rate        1.0
average selected documents 1.0
```

A coverage threshold of 0.2 was the highest tested region that preserved the baseline recall while rejecting all labelled negative cases. Higher coverage thresholds reduced recall.

A relative cutoff of 0.8 removed lower-ranked context noise without reducing the measured recall relative to the coverage-only configuration.

Absolute Top-1 gates from 0 through 5 did not improve the chosen configuration, so v0 does not include one. Absolute BM25 scores are also more corpus-dependent than relative score and coverage.

## Known limitation

Selection policy cannot repair a ranking failure when an irrelevant document itself has high lexical score and sufficient query coverage.

The labelled baseline retained one positive-query failure after selection. That failure is deliberately not hidden by lowering relevance labels or adding a special-case rule. Ranking quality and query representation should be improved separately if this becomes operationally significant.

This is also why v0 does not claim semantic retrieval equivalence and does not introduce embeddings yet.

## CLI result

`obsidian-knowledge-retrieve` reports the active policy explicitly:

```json
{
  "selection_policy": {
    "version": "bm25-coverage-relative-v0",
    "min_query_coverage": 0.2,
    "relative_score_cutoff": 0.8,
    "absolute_top1_score_gate": null
  }
}
```

Each selected result also reports `query_coverage` in addition to the BM25 score.

## Authority and reproducibility

The policy operates only inside Reader authority:

```text
Reader
  read  11-Knowledge
  read  04-Index
  write 05-Context
```

Generator still cannot read `04-Index` or canonical `11-Knowledge` directly.

The selected source bytes are independently re-read from the canonical mirror and checked against the exact index content SHA before the Context Bundle is persisted. A stale index still fails closed before ranking or selection.

## Out of scope

- changing BM25 parameters;
- embeddings or vector databases;
- LLM reranking;
- query rewriting;
- automatic selection-policy tuning;
- using an absolute BM25 confidence threshold;
- fixing ranking errors by selection-specific exceptions.
