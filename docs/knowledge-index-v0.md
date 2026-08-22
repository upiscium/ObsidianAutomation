# Deterministic Knowledge Index v0

## Purpose

Reader/Indexer needs a reproducible retrieval baseline before any embedding model or LLM-based ranking is introduced.

v0 deliberately uses only deterministic local processing:

```text
11-Knowledge
    ↓ Reader only
04-Index/<sha>.index.json
    ↓ explicit index SHA
BM25 lexical ranking
    ↓ top-k
05-Context/<sha>.context.json
    ↓ read-only
Generator
```

The index and ranking carry no Validation, Human approval, Execution, Transport, or Receipt authority.

## Authority

`04-Index` is Reader-private derived state.

| Resource | Reader | Generator | Sync | Validator | Reviewer | Executor |
| --- | --- | --- | --- | --- | --- | --- |
| `11-Knowledge` | read | - | rw | read | - | read |
| `04-Index` | rw | - | - | - | - | - |
| `05-Context` | rw | read | - | - | - | - |
| `00-Untrusted` | - | rw | - | read | - | - |

Generator never receives index access. It receives only exact Context Bundle bytes selected by Reader.

## Corpus policy

v0 scans only Markdown files below `11-Knowledge`.

A file is eligible only when its top-level frontmatter contains:

```yaml
type: knowledge-note
status: active
```

`outdated`, `archived`, and `deleted` notes are not indexed in v0. Non-Knowledge Markdown files are ignored.

Hidden path components are ignored. Symlinks anywhere in the visible retrieval corpus are rejected rather than followed. Case-fold collisions are rejected.

Limits:

- maximum visible Markdown files scanned: 4096;
- maximum source size: 256 KiB;
- maximum serialized index size: 64 MiB.

## Tokenizer

Tokenizer version:

```text
nfkc-ascii-cjk-bigram-v0
```

Steps:

1. Unicode NFKC normalization;
2. case folding;
3. ASCII-style alphanumeric word extraction;
4. CJK character unigram extraction;
5. adjacent CJK bigram extraction.

This is intentionally dependency-free. It provides a deterministic Japanese/English baseline without requiring MeCab, Sudachi, an embedding model, or an external service.

## Field weighting

The document term-frequency vector is constructed from:

- full Markdown content: 1x;
- filename stem: +3x;
- Markdown headings: +2x;
- `category`, `maturity`, `source_type`: +1x.

The weighting is encoded into term frequencies before ranking.

## Ranker

Ranker version:

```text
bm25-fieldboost-v0
```

Parameters:

```text
k1 = 1.2
b  = 0.75
```

Ranking is deterministic for one exact index artifact and one exact query. Score ties are resolved by case-folded path and then exact path.

Only documents with a positive lexical score are selected. A query with zero matches produces an empty Context Bundle rather than inventing context.

## Content-addressed index

The index artifact contains no build timestamp. Therefore an unchanged active Knowledge corpus produces the same canonical JSON bytes and the same SHA-256.

The artifact path is:

```text
04-Index/<index_sha256>.index.json
```

There is intentionally no mutable `current` pointer in v0. Callers must explicitly select an index SHA.

## Staleness rule

Retrieval never silently uses a stale index.

Before ranking, Reader rebuilds the current active corpus inventory and compares ordered `(path, content_sha256)` pairs against the selected index artifact.

Any of these conditions cause fail-closed `Knowledge index is stale`:

- active note changed;
- active note added;
- active note removed;
- status changed into or out of `active`;
- path changed.

The caller must build a new index and retry.

This verification is intentionally conservative. v0 optimizes for auditability and correctness rather than indexing throughput.

## Retrieval to Context

CLI:

```text
obsidian-knowledge-index
obsidian-knowledge-retrieve
```

Example:

```bash
obsidian-knowledge-index \
  --ai-root /var/lib/obsidian-ai/state \
  --vault-root /var/lib/obsidian-ai/vault
```

The returned `index_sha256` is then supplied explicitly:

```bash
obsidian-knowledge-retrieve \
  --ai-root /var/lib/obsidian-ai/state \
  --vault-root /var/lib/obsidian-ai/vault \
  --index-sha256 <sha256> \
  --query 'NixOS RA 受信' \
  --top-k 8
```

Retrieval output reports:

- exact index SHA;
- exact Context Bundle SHA;
- selected paths;
- each selected source SHA;
- deterministic BM25 score string.

The Context Bundle then independently contains the exact selected Markdown bytes and source SHA values.

## Why lexical first

This baseline establishes measurable retrieval behavior before semantic retrieval is introduced.

A later hybrid retriever can be evaluated against the same corpus and queries:

```text
BM25 baseline
    vs
BM25 + embedding vector ranking
```

Embedding retrieval must not replace the Reader/Generator authority boundary. A future embedding database remains Reader-owned derived state; Generator still receives only Context Bundles.

## Out of scope

- embeddings;
- vector database;
- reranker LLM;
- query generation LLM;
- automatic index scheduling;
- mutable latest-index pointer;
- automatic evaluation of retrieval relevance.
