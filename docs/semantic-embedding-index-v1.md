# Semantic Embedding Index v1

## Purpose

Semantic Embedding Index v1 attaches pinned semantic vectors to the exact chunks
defined by Semantic Corpus v1 without changing automatic Planner selection.

The index is Reader-owned derived state. It is not canonical Vault authority and
it does not replace the existing BM25 Knowledge index.

The initial provider adapter is Ollama:

```text
provider         = ollama
adapter          = ollama-embed-v0
vector encoding  = json-number-finite-v0
```

The model identifier and exact installed Ollama digest are pinned before any
request is accepted into an index generation.

## Authority flow

Provider access is deliberately separated from Vault access.

```text
pull-only Vault mirror
        |
        | obsidian-ai-reader
        v
04-Index/semantic-corpus/<corpus-sha>.semantic-corpus.json
        |
        | exact source + chunk re-read
        v
04-Index/semantic-embedding-requests/<sha>.semantic-embedding-request.json
04-Index/semantic-embedding-plans/<sha>.semantic-embedding-plan.json
        |
        | read-only
        v
obsidian-ai-embedder
        |
        | GET /api/tags
        | POST /api/embed
        v
04-Index/semantic-embedding-results/<sha>.semantic-embedding-result.json
04-Index/semantic-embedding-result-sets/<sha>.semantic-embedding-result-set.json
        |
        | read-only
        v
obsidian-ai-reader
        |
        | revalidate corpus + every binding
        v
04-Index/semantic-index/<sha>.semantic-index.json
```

### Reader

Reader may:

- read the approved Vault mirror roots;
- read Semantic Corpus manifests;
- reconstruct exact bounded chunk bytes;
- write embedding requests and plans;
- read Embedder results;
- finalize the semantic index.

Reader does not need provider credentials or provider network authority for this
flow.

### Embedder

`obsidian-ai-embedder` may:

- traverse `04-Index` without listing the generic Index root;
- read `semantic-embedding-requests`;
- read `semantic-embedding-plans`;
- write `semantic-embedding-results`;
- write `semantic-embedding-result-sets`;
- call the configured embedding endpoint.

Embedder cannot read:

- the Vault mirror;
- Semantic Corpus manifests;
- the finalized semantic index;
- Generator Context;
- Validation, Evaluation, Review, Execution, Transport, or Receipt state.

Embedder cannot rewrite requests or plans and cannot publish the final semantic
index.

Generator remains unable to read `04-Index` or the Vault directly.

## Embedding request contract

One immutable request corresponds to one Semantic Corpus chunk.

A request binds:

```text
corpus manifest SHA-256
chunk policy version
provider
adapter version
model identifier
model revision
chunk identity
source path
source kind
source SHA-256
chunk content SHA-256
chunk byte size
exact bounded chunk UTF-8 input
```

The request contains copied chunk bytes only as a bounded provider input. Those
bytes are derived state and never become canonical source authority.

Reader creates requests while holding the mirror read-view lock. It first
rebuilds the current corpus identity and then re-reads each exact source. Source
SHA, line range, chunk content SHA, and chunk identity are rechecked before the
request is stored.

## Embedding plan contract

The content-addressed plan binds the complete ordered request set to:

- exact Semantic Corpus manifest SHA;
- chunk policy version;
- provider and adapter version;
- exact model identifier;
- exact model revision;
- source-kind counts.

Changing the source bytes, corpus identity, chunk policy, adapter, model name, or
model digest creates a different plan identity.

## Ollama model identity

Before vector inference, Embedder resolves the requested model using
`GET /api/tags`.

The resolved identifier and digest must exactly match the values pinned in the
embedding plan. A digest mismatch fails before `POST /api/embed`.

Embedding calls use the native Ollama `/api/embed` endpoint with
`truncate=false`. Chunk overflow therefore fails instead of silently changing
the exact input.

Requests may be batched, but each returned vector is persisted as an immutable
result bound to one exact request SHA.

## Result and result-set contract

An embedding result contains:

- exact request SHA;
- provider;
- adapter version;
- exact model identifier;
- exact model revision;
- one finite numeric vector.

NaN and infinity are rejected. Vector dimension is bounded.

A result set is published only after every planned request has produced one
valid result. It binds the exact ordered request/result pairs plus:

```text
vector dimension
vector encoding = json-number-finite-v0
```

If the provider fails after some individual results have been written, those
immutable partial artifacts may remain as rebuildable cache/audit material, but
no result set is published and Reader cannot finalize a new index from them.

## Final semantic index identity

Reader finalization reloads the plan and result set while holding the mirror
read-view lock and verifies that the Semantic Corpus is still current.

For every ordered chunk it verifies:

```text
corpus chunk
  == plan chunk
  == request chunk/source/content binding

request SHA
  == result request binding

plan provider/adapter/model
  == result provider/adapter/model

result dimension
  == result-set dimension
```

The final content-addressed semantic index is bound to:

- Semantic Corpus manifest SHA;
- embedding plan SHA;
- embedding result-set SHA;
- chunk policy version;
- provider;
- adapter version;
- model identifier;
- model revision;
- vector dimension;
- vector encoding;
- source-kind counts;
- ordered exact chunk/source/request/result/vector bindings.

There is no mutable `latest` pointer in v1. A failed rebuild therefore cannot
replace a previously usable content-addressed index.

ANN/vector database state is explicitly outside the authority contract. A future
ANN backend is a rebuildable cache derived from a verified semantic index.

## CLI

Prepare the Reader-owned request set:

```bash
sudo -u obsidian-ai-reader \
  obsidian-semantic-index prepare \
  --ai-root /var/lib/obsidian-ai/state \
  --vault-root /var/lib/obsidian-ai/vault \
  --corpus-sha <semantic-corpus-sha256> \
  --model qwen3-embedding:0.6b \
  --model-revision <exact-ollama-model-digest>
```

Run embedding as the dedicated Embedder identity:

```bash
sudo -u obsidian-ai-embedder \
  obsidian-semantic-index embed \
  --ai-root /var/lib/obsidian-ai/state \
  --plan-sha <embedding-plan-sha256> \
  --base-url http://127.0.0.1:11434
```

Finalize as Reader:

```bash
sudo -u obsidian-ai-reader \
  obsidian-semantic-index finalize \
  --ai-root /var/lib/obsidian-ai/state \
  --vault-root /var/lib/obsidian-ai/vault \
  --plan-sha <embedding-plan-sha256> \
  --result-set-sha <embedding-result-set-sha256>
```

## Incremental refresh

A later mirror snapshot may produce a different Semantic Corpus while retaining
most exact chunks. Re-embedding every unchanged chunk is unnecessary.

Prepare the new Corpus and embedding plan normally with the same pinned
embedding model/revision. Reader then builds a bounded refresh plan from the
previous finalized index:

```bash
sudo -u obsidian-ai-reader \
  obsidian-semantic-index prepare-refresh \
  --ai-root /var/lib/obsidian-ai/state \
  --plan-sha <new-embedding-plan-sha256> \
  --previous-index-sha <previous-semantic-index-sha256>
```

Only that content-addressed refresh plan crosses the Reader -> Embedder
boundary:

```bash
sudo -u obsidian-ai-embedder \
  obsidian-semantic-index embed-refresh \
  --ai-root /var/lib/obsidian-ai/state \
  --refresh-plan-sha <embedding-refresh-plan-sha256> \
  --base-url http://127.0.0.1:11434
```

Embedder never reads `semantic-index/`. Reader compares the previous finalized
index with the new plan and records only bounded reuse decisions in the refresh
plan. A vector is reusable when the pinned embedding contract and exact UTF-8
input bytes match. Source path/SHA/chunk identity may change without forcing a
new provider call when the actual embedding input is unchanged. The Corpus
manifest SHA itself may differ.

Reused vectors are not silently presented as fresh provider inference.
Incremental result-set v2 binds the exact refresh-plan SHA and records
`reused_from_result_sha256` for every reused entry. Finalization reloads the
refresh plan, previous index, prior result and request, then independently
verifies the full reuse binding before accepting the new index.

Historical result-set v1 artifacts remain readable. Ordinary full `embed`
continues to emit v1; only `embed-refresh` emits v2 provenance.

The refresh output reports:

- `reused_count`;
- `embedded_count`;
- `removed_count`.

If every current chunk is reusable, Embedder performs no Ollama request at all.
New or changed chunks alone are sent to the provider. Removed chunks simply do
not appear in the new plan/index.

A failed incremental embedding may leave immutable partial result artifacts, but
it cannot replace or invalidate the previous content-addressed index. The new
index is published only after Reader finalization succeeds against the current
mirror snapshot.

The Phase B primitives remain independently operator-runnable. Production
automation is layered on top by
[Automatic Semantic Index refresh v1](semantic-index-auto-refresh-v1.md):
a successful Vault pull can trigger Reader prepare -> Embedder incremental
embedding -> Reader finalize/activate. The finalized Semantic Index artifacts
remain unchanged and content addressed; only the separate Reader-owned active
binding is mutable control state.

## Observability

The CLI reports:

- corpus / plan / result-set / final index identities;
- request/result/vector counts;
- vector dimension and encoding;
- model identifier and exact model revision;
- source-kind counts.

Source-kind counts are document counts from the exact Semantic Corpus manifest,
not vector counts.

## Failure semantics

The flow fails closed when:

- the corpus is stale;
- source bytes no longer match their manifest SHA;
- reconstructed chunk bytes do not match the chunk SHA;
- the model identifier or digest differs from the plan;
- a result is bound to another request;
- vector dimensions are inconsistent;
- a vector contains a non-finite value;
- a result set is incomplete or reordered;
- any content-addressed artifact bytes do not match their filename SHA.

Provider failure cannot mutate canonical Vault state and cannot publish a final
semantic index. Incremental refresh additionally preserves the previous usable
index because it never mutates an existing content-addressed manifest.

## Rollout boundary

This implements Semantic Planner Phase B / issue #200 only.

It does not:

- change automatic Planner source selection;
- perform vector or hybrid ranking;
- add novelty gating;
- change generation cadence;
- change Generator prompts/objectives;
- grant Automation canonical Idea or Project mutation authority.

The next stage is
[Semantic Hybrid Retrieval v1](semantic-hybrid-retrieval-v1.md), which implements
issue #201 by comparing BM25-only, vector-only, and hybrid ranking over this exact
index without changing automatic Planner behavior.
