# Semantic Selection and Novelty v1

## Purpose

Semantic Selection and Novelty v1 implements Semantic Planner Phase E / issue
#202.

It turns one exact Semantic Embedding Index into a reproducible Selection Record
without changing Generation Objective or canonical Vault state.

The first rollout is observation-first. Selection output can be inspected and
benchmarked before #203 connects semantic selections to new generation
objectives.

## Authority boundary

Selection runs as Reader.

Reader may:

- read one exact Semantic Embedding Index;
- read the bound Semantic Corpus manifest;
- verify the current pull-only Vault mirror;
- read immutable embedding request text for lexical scoring;
- inspect bounded recent Context identities from orchestration metadata;
- read those immutable Context Bundles;
- write content-addressed Semantic Selection Records;
- optionally record a bounded novelty-skip reason in Planner cadence metadata.

Selection does not require Embedder or Generator credentials and performs no
provider call.

Generator remains unable to read the Vault, semantic index, or Selection Record
store directly.

## Selection Record

Stored at:

```text
02-Orchestration/semantic-selections/<sha>.semantic-selection.json
```

Schema:

```text
schemas/semantic-selection-v1.schema.json
```

A record binds:

- selection policy version;
- exact Semantic Index SHA-256;
- exact Semantic Corpus manifest SHA-256;
- metadata filters;
- hybrid retrieval weights;
- one or two exact anchor chunks;
- selected chunk/source identities;
- lexical and vector component scores;
- source-kind weighting;
- novelty observations;
- policy-specific bounded observations.

Generation Objective is intentionally absent. Objective execution is defined by
[Semantic Generation Objectives v1](semantic-generation-objectives-v1.md).

## Initial policies

### `semantic-focus-v0`

Chooses a dense non-Knowledge anchor when available and ranks one coherent
neighborhood around it.

The deterministic anchor objective is local semantic density, with path/chunk
identity tie-breaking.

### `semantic-project-distill-v0`

Requires a Project or Project Note anchor and favors related:

- Project Notes;
- Daily observations;
- Ideas;
- Knowledge.

This identifies material suitable for extracting durable lessons from active
work. It does not mutate Project state.

### `semantic-timeline-v0`

Chooses the lexically latest eligible Daily source as primary anchor, then
retrieves related Daily/history/Knowledge material.

The Daily date is corpus metadata; it is not mixed into embedding similarity.

### `semantic-bridge-v0`

Chooses two non-Knowledge anchors from different source kinds whose cosine
similarity lies within the versioned bridge window:

```text
0.30 <= pair cosine <= 0.82
```

The lower bound avoids forcing unrelated clusters together. The upper bound
avoids calling near-duplicate sources a cross-domain bridge.

The pair centroid and combined bounded anchor text drive hybrid ranking.

### `semantic-gap-v0`

Chooses a non-Knowledge anchor with:

- strong support from another non-Knowledge source;
- comparatively weak coverage from active Knowledge.

Initial thresholds:

```text
minimum non-Knowledge support cosine = 0.35
maximum Knowledge coverage cosine    = 0.78
```

The policy does not claim that a topic is objectively absent from Knowledge; it
records the bounded similarity observation used by this policy version.

### `semantic-idea-development-v0`

Requires an active Idea anchor, preferring the latest `created` metadata and
then local semantic density.

The selected set preferentially includes:

- nearby Knowledge;
- candidate Project Entries;
- Project Notes.

Similarity does not adopt the Idea, change `Idea.project`, or mutate Project
content.

## Ranking contract

Anchor-based policies reuse Phase C hybrid scoring.

```text
lexical_normalized = BM25 / max_BM25
semantic_normalized = (cosine + 1) / 2

hybrid =
  source_kind_weight
  * (
      0.60 * lexical_normalized
      + 0.40 * semantic_normalized
    )
```

Anchor chunk text is the exact bounded text already bound into the semantic
embedding request. No model generates a search query.

One selected chunk per source path is retained in v1 so a single long note
cannot consume the entire selection budget.

## Novelty observations

Novelty is advisory planning metadata.

For every candidate selection Reader records:

- mean pairwise selected-cluster cosine;
- maximum similarity to comparable recent generated Contexts;
- number of recent Contexts that could not be compared to the current index;
- maximum anchor similarity to active Knowledge;
- versioned policy thresholds.

Recent Context comparison uses exact source path + source SHA binding. A source
changed since the old Context was generated does not get silently compared
against a new vector.

Default recent Context history:

```text
8 comparable Contexts
maximum configured bound: 32
```

## Skip rules

Common initial rules:

```text
recent generated Context max similarity >= 0.94 -> skip
cluster coherence below policy threshold        -> skip
fewer than two distinct selected sources        -> skip
```

Knowledge coverage thresholds:

```text
semantic-focus-v0           0.96
semantic-project-distill-v0 0.98
semantic-timeline-v0        0.97
semantic-bridge-v0          0.98
semantic-gap-v0             0.78 (gap must remain below this)
semantic-idea-development-v0 observational only
```

A skip creates a Selection Record with:

```text
novelty.decision = skipped
novelty.skip_reason = <versioned reason>
```

Similarity never deletes, merges, archives, adopts, approves, or otherwise
mutates canonical state.

## Durable novelty skip metadata

Observation is non-mutating by default.

The CLI flag:

```text
--record-skip
```

may additionally write a bounded reason into
`02-Orchestration/input-planner-cadence.json` when the Selection Record is
skipped.

The durable reason is namespaced by policy, for example:

```text
semantic-gap-v0:recent_context_too_similar
```

Recording a novelty skip does not rewrite `last_submission_at` and therefore
does not impersonate a generation submission.

## CLI

Observe one policy:

```bash
sudo -u obsidian-ai-reader \
  obsidian-semantic-selection \
  --ai-root /var/lib/obsidian-ai/state \
  --vault-root /var/lib/obsidian-ai/vault \
  --semantic-index-sha <semantic-index-sha256> \
  --policy semantic-gap-v0
```

Optionally change the bounded output/history sizes:

```bash
  --max-selected 6 \
  --recent-context-limit 8
```

To persist a skip reason into Planner cadence metadata:

```bash
  --record-skip
```

## Rollout boundary

Phase E intentionally does not yet replace the existing automatic
`coverage-shuffle-v0` / `random-set-v0` generation source selection.

The safe rollout is:

1. create/verify Semantic Corpus and Index;
2. run Phase C benchmark;
3. observe Phase E Selection Records across real Vault state;
4. inspect skip frequency and selected source quality;
5. create and inspect Phase F Objective Contexts/candidates;
6. connect approved semantic selection + objective pairs to durable production jobs.

This avoids coupling a new source-selection policy to the legacy
`synthesize-v0` objective before the objective contract is versioned.

## Non-goals

This stage does not:

- create a Generation Context from Daily/Idea/Project sources;
- change Context Bundle source-root authority;
- replace Evaluator Knowledge BM25;
- choose a Generation Objective;
- create canonical Ideas;
- adopt Ideas into Projects;
- write canonical Knowledge;
- automatically enable semantic selection in production generation.
