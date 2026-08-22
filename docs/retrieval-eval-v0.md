# Retrieval Evaluation v0

## Purpose

Before changing BM25 ranking or adding embeddings, retrieval quality is measured against an explicit labelled query set.

The evaluation is deterministic and read-only. It does not create Context, proposals, validation records, approvals, execution intents, transport results, or canonical Vault writes.

## Evaluation set

Schema: `schemas/retrieval-eval-v0.schema.json`

```json
{
  "eval_version": 1,
  "name": "knowledge-retrieval-baseline",
  "cases": [
    {
      "id": "example-positive",
      "query": "example query",
      "relevant_paths": ["11-Knowledge/Example.md"]
    },
    {
      "id": "example-negative",
      "query": "no matching knowledge",
      "relevant_paths": []
    }
  ]
}
```

An empty `relevant_paths` marks a negative/no-match case. Positive relevant paths must exist in the exact selected active Knowledge Index. Missing or inactive paths fail evaluation instead of being silently ignored.

## CLI

```bash
obsidian-knowledge-retrieval-eval \
  --ai-root /var/lib/obsidian-ai/state \
  --vault-root /var/lib/obsidian-ai/vault \
  --index-sha256 <sha256> \
  --eval-set ./retrieval-eval.json \
  --top-k 8
```

The selected index is hash-verified and checked against the current active Vault corpus before evaluation. A stale index fails closed.

## Baseline metrics

Positive cases report:

- `top1_accuracy`: top-ranked document is relevant;
- `hit_at_3`: at least one relevant document appears in the first three;
- `recall_at_3_macro`: per-case relevant recall within the first three, then macro averaged;
- `precision_at_3_macro`: standard fixed-denominator Precision@3, then macro averaged;
- `mrr`: mean reciprocal rank of the first relevant document.

Negative cases report:

- `negative_accuracy`: fraction of negative queries for which BM25 returns zero positive-score matches.

`Precision@3` intentionally uses denominator 3 even when a case has only one relevant document. It measures ranking noise, not only whether the expected note was found.

## IDF-weighted query coverage

BM25 can assign a positive score when only a small subset of query terms matches. That is useful for ranking but can be unsafe for Context selection, especially for hard negatives such as:

```text
LLM agent sandbox security permission isolation
```

A document that matches only `LLM` and `agent` should not look equivalent to one that also covers the security-specific terms.

Retrieval Evaluation v0 therefore computes an IDF-weighted query coverage for each document.

For the unique tokenizer terms in query `q`, let `idf(t)` use the same BM25-style IDF form as the ranker. Then:

```text
coverage(d, q)
  = sum(idf(t) for t in q if t occurs in d)
    ------------------------------------------------
    sum(idf(t) for t in q)
```

Terms absent from the entire corpus are deliberately retained in the denominator with `df = 0`. This means an unrepresented high-information query term lowers coverage instead of disappearing from the confidence calculation.

Coverage uses token presence only. BM25 field boosts affect ranking term frequency but do not multiply coverage weight.

The tokenizer remains `nfkc-ascii-cjk-bigram-v0`, so Japanese coverage includes the same CJK unigram/bigram representation used by the lexical baseline.

Each reported top result includes:

- `query_coverage`;
- `matched_query_terms`;
- `query_terms`.

## Query coverage sweep

The report simulates minimum coverage thresholds:

```text
0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8
```

A document is eligible only when:

```text
query_coverage >= minimum_query_coverage
```

The coverage-only sweep uses no relative cutoff and no absolute Top-1 score gate.

## Relative-score cutoff sweep

The report simulates Context selection at ratios:

```text
0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0
```

For the highest-scoring eligible candidate with score `S`, a candidate survives ratio `r` only when:

```text
candidate_score >= S * r
```

The simulation also applies the existing `top_k` and aggregate Context byte limit.

A relative cutoff always retains the eligible Top-1 when any positive-score match exists. Therefore it can reduce lower-ranked context noise, but cannot by itself reject a hard negative that has a spurious Top-1 match.

## Absolute Top-1 score diagnostic sweep

The report also simulates minimum eligible Top-1 BM25 score thresholds:

```text
0, 1, 2, 3, 5, 8, 12
```

A query produces no selected Context when:

```text
top1_score < minimum_top1_score
```

Absolute BM25 scores depend on corpus and query composition, so these thresholds are diagnostic only and must not be transferred blindly between corpora.

## Combined sweep

The evaluation also enumerates the full Cartesian product:

```text
minimum query coverage
    × relative score ratio
    × minimum eligible Top-1 score
```

Selection order is intentional:

```text
BM25 ranking
    ↓
IDF-weighted query coverage eligibility
    ↓
eligible Top-1 absolute score gate
    ↓
relative score cutoff against eligible Top-1
    ↓
top-k / aggregate Context byte limit
```

If the raw BM25 Top-1 fails coverage, it is removed before the absolute and relative gates are evaluated. The next eligible ranked document becomes the effective Top-1. This allows evaluation of cases where a broad lexical match outranks a more complete semantic/lexical match.

Every sweep row reports:

- micro precision;
- micro recall;
- micro F1;
- positive full-recall rate;
- negative clean rate;
- average number of selected documents;
- total selected documents.

The combined sweep is intentionally diagnostic and does not automatically choose a production policy.

## Evaluation-set guidance

A useful set should include multiple query classes:

- exact title/keyword queries;
- Japanese paraphrases;
- English/Japanese mixed queries;
- broad research-topic queries;
- near-neighbour discrimination cases;
- multi-relevant-document cases;
- clean negative queries with no lexical overlap;
- hard negative queries that deliberately share vocabulary with unrelated Knowledge notes.

Do not label relevance from the current ranking itself. Labels should express which canonical Knowledge notes are actually useful for answering the query.

## Out of scope

- changing BM25 parameters;
- applying a production query-coverage threshold;
- applying a production score cutoff;
- embeddings or vector databases;
- LLM-based relevance judging;
- automatic generation of evaluation labels.
