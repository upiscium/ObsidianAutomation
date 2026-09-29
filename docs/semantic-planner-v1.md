# Semantic Planner v1

Related Epic: #198

## Purpose

Semantic Planner v1 replaces the current "fast, broad, concise" automatic input
strategy with a semantic planning layer that can choose *what kind of note should
be generated* as well as *which Vault sources should support it*.

The design must preserve the existing production authority model:

- Reader owns canonical Vault selection and derived retrieval state.
- Generator receives only exact Context Bundle bytes.
- Validator / Evaluator / Human Review remain separate authorities.
- Automation does not gain canonical `05-Idea` or Project mutation authority.
- Existing BM25 retrieval remains available as a deterministic baseline.

The first implementation wave is read-only: build and benchmark the semantic
corpus/index before changing automatic generation behavior.

## Source corpus

Semantic Planner may use the following canonical mirror roots:

```text
00-DailyNote/**
05-Idea/**
10-Project/**
11-Knowledge/**
```

Every source keeps a distinct `source_kind`. Different source kinds are never
treated as interchangeable merely because their embeddings are close.

### `daily`

Canonical shape:

```yaml
type: daily-review
```

Daily Notes are not embedded as one undifferentiated document by default.
Template-owned and operational sections can dominate a whole-note vector and
reduce retrieval quality.

The initial section policy is therefore explicit and versioned.

Default semantic content:

- free-form `# Note` content;
- later policy versions may add selected user-authored prose sections.

Default excluded content:

- task controls and `# Tasks`;
- finance/budget embeds;
- condition/mood/sleep/weather metadata;
- template boilerplate and Meta Bind controls.

A Daily chunk records its source date as metadata derived from the canonical
path/title, but date is not part of semantic similarity itself.

### `idea`

Canonical shape:

```yaml
type: idea
workspace: ...
project: ...
status: active | adopted | archived
created: ...
```

Default policy:

- `active`: eligible for idea-development and Project-fit selection;
- `adopted`: searchable as Project provenance/history;
- `archived`: indexed only when explicitly requested by a policy, excluded
  from default automatic generation.

Metadata retained outside the embedding:

- workspace;
- project;
- status;
- created;
- tags / aliases when available.

Semantic similarity never changes Idea status or relation.

### `project`

Project Entry:

```yaml
type: project
workspace: ...
status: ...
```

Project Entry is a short metadata-rich semantic anchor. Its summary text,
workspace, status and repository binding may be used to locate related material.

Cancelled/hidden terminal Projects are excluded from the default active planning
corpus unless a policy explicitly requests historical sources.

### `project-note`

Canonical shape:

```yaml
type: project-note
project: ...
workspace: ...
lifecycle: active
```

Only active Project Notes are eligible by default. The referenced Project and
Workspace are retained as metadata filters.

### `knowledge`

Canonical shape:

```yaml
type: knowledge-note
status: active
```

Active Knowledge remains:

- a generation source;
- the primary novelty/redundancy comparison corpus;
- the existing BM25 baseline corpus.

The current Evaluator Knowledge retrieval path is not replaced in the first
wave.

## Source and chunk identity

A semantic chunk is derived from exact canonical bytes. Its identity includes at
least:

```text
source path
source kind
source content SHA-256
chunk policy version
deterministic chunk ordinal/range
chunk content SHA-256
```

Chunking is deterministic for one exact source and policy version.

Heading/section boundaries are preferred over blind fixed-size splitting.
Oversized semantic sections may be subdivided deterministically with bounded
overlap, but arbitrary model-driven chunking is not authority.

The semantic index does not make copied source text canonical. Before selected
sources enter a Context Bundle, Reader re-reads the canonical pull-only mirror
and verifies exact source SHA binding.

## Embedding authority boundary

Reader must not gain provider credentials or general network inference authority
merely to add embeddings.

The preferred provider-backed architecture introduces a narrow Embedder role:

```text
Canonical Vault mirror
        |
        | Reader only
        v
Source Catalog / deterministic chunks
        |
        | bounded immutable embedding request
        v
obsidian-ai-embedder
        |
        | provider call
        v
bound vector result
        |
        | Reader validates
        v
04-Index semantic manifest / retrieval state
```

### Reader

May:

- read approved semantic corpus roots from the pull-only Vault mirror;
- construct chunk manifests;
- write bounded embedding requests;
- verify embedding results;
- write/finalize semantic index state;
- perform lexical/vector ranking;
- create exact Context Bundles.

Must not:

- hold Generator/Evaluator/provider secrets solely for embedding;
- write canonical Vault content.

### Embedder

May:

- read only bounded embedding requests;
- read embedding-provider configuration/credential when required;
- call the configured embedding provider;
- write vector results bound to exact request/chunk/model identity.

Must not:

- read the Vault mirror directly;
- read Human Review, canonical Knowledge transport credentials, or unrelated AI
  state;
- select sources or create Context Bundles.

### Generator

Remains unchanged:

- no Vault access;
- no semantic-index access;
- receives only Context Bundle bytes.

A future local in-process embedding backend may avoid a provider credential, but
it must preserve the same source/manifest/vector identity contract.

## Semantic index identity

A semantic index generation is bound to:

```text
corpus manifest SHA-256
chunk policy version
embedding adapter version
embedding model identifier
embedding model revision / immutable binding policy
vector dimension / encoding contract
```

Changing any of these creates a different index identity.

ANN/vector-database implementation details are caches, not authority. A mutable
backend may be rebuilt from verified derived artifacts. Retrieval records bind
to the semantic manifest/index identity, not to an opaque mutable "latest"
database state.

## Hybrid retrieval

Semantic Planner keeps two retrieval signals:

- lexical BM25;
- embedding similarity.

Hybrid retrieval can additionally apply metadata filters/weights:

- source kind;
- Workspace;
- Project;
- Idea status;
- Project status;
- Daily date range;
- Knowledge category/maturity/source type.

A selected result records enough metadata to reproduce and audit why it was
chosen, including component scores where applicable.

Before production default changes, BM25-only, vector-only and hybrid behavior
must be compared against the same retrieval benchmark corpus.

## Selection policy vs generation objective

Selection policy answers:

> Which source cluster should be considered?

Generation objective answers:

> What artifact/candidate should be produced from that cluster?

They are independent versioned contracts.

A Selection Record binds both its semantic-index identity and exact chosen
source/chunk identities. The Job/Generation provenance binds the selected
policy and objective.

### Initial selection policies

#### `semantic-focus-v0`

Choose one anchor and a dense neighborhood around it.

Use for narrow, deep Knowledge generation.

#### `semantic-project-distill-v0`

Anchor on one Project/Project Note and retrieve related:

- Project Notes;
- Daily observations;
- active/adopted Ideas;
- supporting Knowledge.

Use for durable lessons extracted from current work.

#### `semantic-timeline-v0`

Anchor on recent Daily semantic content and retrieve related historical sources.

Use for retrospectives and recurring-topic detection.

#### `semantic-bridge-v0`

Select coherent sources from distinct semantic clusters where a meaningful
bridge exists.

Use for cross-domain insights. Policy must avoid forcing unrelated clusters
together merely because a weak nearest-neighbor path exists.

#### `semantic-gap-v0`

Find a topic strongly represented in Daily/Project/Idea sources but weakly
covered by active Knowledge.

Use to discover candidate Knowledge gaps.

#### `semantic-idea-development-v0`

Anchor on one active Idea and retrieve:

- semantically supporting evidence;
- nearby Knowledge;
- contradictory/risk-bearing material where available;
- candidate active Projects.

Use for Idea refinement or Project adoption proposals.

## Novelty and repetition gate

Semantic similarity is advisory for planning, not canonical authority.

The Planner may skip automatic generation when:

- a candidate Context is too similar to recent generated Contexts;
- the topic is already strongly covered by active Knowledge;
- no sufficiently coherent semantic cluster exists.

Skip decisions are durable operational metadata and include the policy/version
and bounded similarity observations.

Similarity alone must never:

- delete or merge notes;
- archive an Idea;
- adopt an Idea into a Project;
- approve a Knowledge mutation.

## Generation objectives

### `deep-knowledge-v1`

Replaces the current "concise synthesis" bias for semantic generation.

Prefer one narrow topic explored in enough depth to be reusable without
reopening all source notes. When supported by sources, explain:

- central idea;
- mechanism / why it works;
- assumptions;
- constraints;
- trade-offs;
- concrete implications.

Do not pad unsupported detail.

### `idea-discovery-v0`

Generate a new Idea candidate from a semantic gap, bridge or recurring signal.

Automation emits a Human-facing candidate only. Canonical `05-Idea` creation
is performed by an ObsidianCore/Human action that chooses required Workspace and
optional Project.

This is distinct from the existing Human Review `keep_as_idea` disposition.
Both may reuse Core idempotency/provenance patterns without sharing semantics.

### `project-adoption-proposal-v0`

Anchor on an existing active Idea and propose one or more semantically relevant
Projects.

The proposal contains, at minimum:

- target Project identity;
- fit rationale;
- supporting source evidence;
- risks/conflicts;
- missing information or uncertainty.

Automation does not mutate `Idea.project`, `Idea.workspace`, `Idea.status`
or Project content. A Human-triggered Core action performs any canonical adoption.

Future objectives may include retrospective, consolidation candidate,
procedure/specification and cross-domain insight.

## Generation cadence

Pipeline lifecycle polling remains short so Review Intake, post-review
reconciliation, projection cleanup and health/status stay responsive.

Only automatic *new generation submission* is rate-limited.

Initial policy:

```text
hard minimum interval        15 min
normal target interval       60 min
awaiting Human Review = 1    90 min
awaiting Human Review >= 2   180 min
initial target_inflight      2
```

Cooldown state is durable and survives service restart.

Once semantic selection is available, insufficient novelty/coherence may skip a
generation even after the time gate opens.

The Planner status should expose:

- last automatic submission time;
- next eligible time;
- backlog-derived interval;
- cooldown/skip reason;
- last selection/objective identity.

## Rollout sequence

### Phase A — corpus contract

Implement #199:

- mirror roots;
- ACLs;
- source parsing;
- section/chunk policy;
- exact identity tests.

No generation behavior change.

### Phase B — semantic index

Implement #200 via
[Semantic Embedding Index v1](semantic-embedding-index-v1.md).

Reader materializes exact bounded chunk requests, the dedicated
`obsidian-ai-embedder` performs pinned-model inference, and Reader validates the
bound results before publishing a content-addressed semantic index. Build vectors
and manifests in read-only/offline production mode. Do not connect to automatic
Planner selection.

### Phase C — benchmark/hybrid retrieval

Implement #201 via
[Semantic Hybrid Retrieval v1](semantic-hybrid-retrieval-v1.md).

Compute lexical BM25 and vector cosine over the same Semantic Corpus chunk
population, apply versioned metadata filters/source-kind weights, and compare
BM25-only / vector-only / hybrid against one fixed six-category benchmark.
Hybrid remains offline until the benchmark shows improved semantic recall
without regressing exact technical top-1 lookup.

### Phase D — cadence

#204 is implemented by
[Planner Generation Cadence v1](planner-generation-cadence-v1.md).

Only automatic new-generation submission is throttled. The lifecycle polling
timer remains short. Cooldown is durable across restart and uses 60 / 90 / 180
minute backlog-derived intervals with a 15 minute hard floor and
`target_inflight=2`.

The cadence state already reserves bounded novelty-skip observability; #202 owns
the actual semantic novelty decision.

### Phase E — semantic selection

Implement #202 via
[Semantic Selection and Novelty v1](semantic-selection-novelty-v1.md).

Six versioned policies produce content-addressed Selection Records from one
exact Semantic Index. Novelty observations cover recent generated Contexts,
active Knowledge coverage and selected-cluster coherence. Initial rollout is
observation-first: semantic selections are not yet fed into the legacy
`synthesize-v0` Generation Objective.

### Phase F — generation objectives

Implement #203 via
[Semantic Generation Objectives v1](semantic-generation-objectives-v1.md).

Selection and Objective remain separate versioned contracts. Reader materializes
an exact Objective Context from one accepted Semantic Selection, Generator sees
only that bounded Context, and objective-specific candidate/generation artifacts
bind the Selection and Semantic Index identities.

Initial objectives are `deep-knowledge-v1`, `idea-discovery-v0` and
`project-adoption-proposal-v0`. Phase F remains explicit/operator-driven and
does not route non-Knowledge candidates through the Knowledge mutation executor.

### Phase G — Core Human actions

ObsidianCore#198 owns canonical client-side Idea save/adoption actions.

## Non-goals

Semantic Planner v1 does not authorize:

- automatic Human approval;
- automatic Idea adoption;
- direct Automation writes to `05-Idea`;
- automatic Project relation/status mutation;
- Generator access to the Vault/vector index;
- similarity-driven deletion/merge;
- immediate retirement of existing BM25 evaluation paths.

## Tracking

- #198 Epic
- #199 source corpus/chunk contract
- #200 semantic embedding index
- #201 hybrid retrieval/benchmark
- #202 selection policies/novelty gate
- #203 generation objectives
- #204 Planner cadence
- upiscium/ObsidianCore#198 Human-side Idea/Project actions
