# Automatic AI Input Planner v0

## Purpose

The Input Planner keeps the pre-review queue supplied without letting Generator
scan the Vault directly. Reader remains the only actor that chooses canonical
source bytes and produces immutable Generation Context.

The v0 source pool is deliberately limited to:

```text
11-Knowledge/**
  type: knowledge-note
  status: active

10-Project/**
  type: project-note
  lifecycle: active
  parent Project status != cancelled
```

`04-AI/**` is never an input source. Human-facing AI projections must not feed
back into generation.

The existing Evaluation corpus is unchanged: BM25 Index and Evaluation Context
continue to use active `11-Knowledge/**` only. Project Notes are Generation
material, not canonical Knowledge for redundancy/consistency evaluation.

## Pipeline position

```text
pull-only Vault mirror
        |
        v
AI Input Planner (Reader)
  catalog Knowledge + Project Notes
  choose bounded source set
  store immutable selection manifest
  create immutable 05-Context
  submit durable pre-review job
        |
        v
Generator -> Validator -> Reader -> Evaluator -> awaiting_human_review
```

The planner runs as `obsidian-ai-reader`. It has no provider credential,
Nextcloud writer credential, Review authority, Executor authority, or Transport
authority.

## Selection policies

v0 alternates deterministically between:

- `coverage-shuffle-v0`: deterministic shuffled epochs cover every eligible
  source before advancing to a new epoch;
- `random-set-v0`: deterministic pseudo-random bounded sets for cross-source
  discovery. When both source kinds exist and the batch permits it, at least one
  Knowledge Note and one Project Note are selected.

The default selection-policy mix is four coverage selections followed by one
random selection, equivalent to an 80/20 split. This is independent from the
generation submission cadence described below.

The seed is SHA-256-derived from the exact catalog digest plus epoch/cycle.
Selection is reproducible from durable metadata and does not depend on process
randomness.

## Queue policy

The planner creates at most one new job per invocation.

Defaults:

```text
target_inflight = 2
hard Human Review backpressure = 8
batch_size = 6
maximum batch_size = 8
```

Automatic generation submission is additionally gated by
[Planner Generation Cadence v1](planner-generation-cadence-v1.md):

```text
hard minimum interval        15 min
normal target interval       60 min
awaiting Human Review = 1    90 min
awaiting Human Review >= 2   180 min
```

Only new Planner submission is rate-limited. The short pre-review lifecycle
timer remains unchanged so Review Intake, reconciliation, cleanup and status
projection stay responsive.

`queued`, active pre-review processing states,
`awaiting_human_review`, and `approved_pending_execution` count toward the
target. New planning pauses when the target is already satisfied. It also pauses
on `blocked` or `retry_exhausted` generations instead of hiding an
operational problem by creating more work.

Human-facing post-review projection follows the same lifecycle but is not
scheduler authority. Review Intake persists the exact case/evaluation/mutation
mapping as a reviewer-owned immutable binding in `20-Review`; post-review
services consume that binding rather than querying scheduler internals.
`60-Execution`, `70-Transport`, and `80-Completed` are derived from
durable Execution, Transport, and Receipt artifacts respectively;
`80-Completed` remains after terminal projection cleanup.

Post-review reconciliation observes authoritative `20-Review` and
`30-Receipts` artifacts and projects their outcome back into scheduler
metadata:

```text
awaiting_human_review
  -> approved_pending_execution
  -> completed

awaiting_human_review
  -> human_rejected

awaiting_human_review
  -> human_kept_as_idea
```

The database does not grant approval or completion authority. Review and Receipt
artifacts remain authoritative; reconciliation only prevents completed/rejected
work from permanently occupying the planner target queue.

## Durable metadata

Mutable scheduler state:

```text
02-Orchestration/input-planner-state.json
02-Orchestration/input-planner-cadence.json
02-Orchestration/input-planner-pending.json
```

The cadence file is independently durable and records the last automatic
submission timestamp plus bounded selection/objective policy and future novelty
skip observability. Service restart does not reset cooldown.

`input-planner-pending.json` is a fsynced transaction journal spanning Context
creation, durable job submission, Human projection emission and scheduler-state
advance. It is written before submission and cleared only after projections and
the next scheduler cursor are durable.

On retry, the planner reconciles the pending record before queue/backpressure
decisions. Same-revision retries reuse the exact existing job. If the software
revision changed after a submitted job was durably queued but before any attempt
started, the old generation is retained as audited `superseded` state and the
same immutable Context is submitted with the new exact recipe revision. A
generation with any attempt/output evidence is never superseded by this path.

Immutable selection records:

```text
02-Orchestration/input-selections/<sha>.selection.json
```

A selection records:

- catalog SHA-256;
- coverage epoch and scheduler cycle;
- selection and objective policy versions;
- deterministic seed;
- selected path;
- source kind;
- exact content SHA-256;
- byte size;
- parent Project status for Project Notes.

It does not contain source body text. Exact selected Markdown bytes live only in
the content-addressed `05-Context` artifact already consumed by Generator.

## Context boundary

Context Bundle v0 accepts source paths below:

```text
11-Knowledge/**
10-Project/**
```

A `10-Project/**` Context source must itself contain:

```yaml
type: project-note
lifecycle: active
```

This prevents manual Context construction from using Project Entry files as
Generation material. The planner additionally resolves the Project reference and
excludes notes whose parent Project is cancelled.

## Mirror read view

Catalog construction, selection, and exact source re-read occur while holding
the existing host-local `mirror_read_lock`. The pull-only mirror therefore
cannot change between catalog hashing and Context construction on this host.

The lock does not claim that the local mirror is current with Nextcloud.

## Production mirror filter

The AI mirror includes the current Generation roots plus Reader-owned Semantic Corpus roots. A reusable example is:

```text
examples/ai/vault-pull.filters
```

with:

```text
+ /00-DailyNote/**
+ /05-Idea/**
+ /11-Knowledge/**
+ /10-Project/**
- /**
```

Input Planner v0 continues to select only active Knowledge and active Project Notes. Daily and Idea are mirrored for Semantic Corpus v1 and do not enter Generation Context until a later versioned selection policy enables them.

The production file is a repository-reviewed managed policy staged from the
exact deployed revision:

```text
examples/ai/vault-pull.filters
  -> /etc/obsidian-ai/vault-pull.filters
```

The contract-4 exact-SHA updater installs it atomically as
`root:obsidian-ai-sync 0640`. It is not part of private-config transfer and
must not drift independently from the deployed revision.

## Production service

`obsidian-ai-input-planner.service` runs before Generator. Generator only
`Wants=` the planner rather than `Requires=` it. Therefore a host without the
new model-selection file keeps the existing manual-submit pipeline working.

Automatic planning is enabled by creating:

```text
/etc/obsidian-ai/pre-review-input.env
```

The service itself defaults to `AI_INPUT_MODE=legacy`. Deploying a release does
not enable semantic production automatically.

For controlled semantic deep-Knowledge canaries, an exact reviewed index SHA
may still be supplied. After the reviewed index is explicitly activated, normal
production can bind through Reader-owned control state:

```text
AI_INPUT_MODE=semantic-deep-knowledge
AI_INPUT_SEMANTIC_INDEX_SHA=active
AI_INPUT_SEMANTIC_SELECTION_POLICY=semantic-project-distill-v3
```

The token `active` is resolved once per Planner invocation to one exact
finalized Semantic Index SHA. Pending state, Selection, Context and jobs store
that exact SHA rather than the token.

The controlled rollout uses `semantic-project-distill-v3`, which retains v2's deterministic novelty-aware Project-anchor exploration and `semantic-retrieval-v1` (lexical 0.15 / vector 0.85). Support rows are admitted only when anchor cosine is at least 0.70, and after the first support, a candidate with cosine at least 0.88 to an already accepted support is rejected as redundant. The policy does not pad the Context to six sources when no additional support meets those gates. Historical `semantic-project-distill-v0`, v1 and v2 remain available with their original semantics.

The Semantic Index manifest identity remains exact and immutable for the cycle.
There is no mutable `latest` manifest. The separate active binding can advance
only after the Reader -> Embedder -> Reader refresh chain finalizes a new
content-addressed index against the current mirror. A missing or stale binding
fails before job submission.

Set `AI_INPUT_MODE=legacy` (and leave the index value disabled) to retain or
restore the original catalog/coverage Planner.

Provider/model bindings remain non-secret. For Ollama production:

```text
AI_INPUT_GENERATOR_PROVIDER=ollama
AI_INPUT_GENERATOR_MODEL=gemma4:12b
AI_INPUT_GENERATOR_MODEL_REVISION=<64-hex-model-digest>
AI_INPUT_EVALUATOR_PROVIDER=ollama
AI_INPUT_EVALUATOR_MODEL=gemma4:12b
AI_INPUT_EVALUATOR_MODEL_REVISION=<64-hex-model-digest>
```

The Planner remains `PrivateNetwork=true`. It does not query Ollama to discover
model identity. Ollama's exact model digest is supplied as non-secret deployment
configuration and becomes part of the immutable recipe/job identity. For generic
OpenAI-compatible providers, provider defaults to `openai-compatible` and model
revision may remain `auto`, which resolves to identifier-only binding.

The exact implementation revision is supplied separately by the updater-owned
`pre-review-revision.env`. The planner builds the immutable recipe with current
prompt hashes and the deployed revision every cycle. The pending-submission
journal also detects an unstarted queued job bound to an older recipe revision,
records that generation as `superseded`, and resubmits the same Context with
the current exact recipe. Started generations are never rewritten or silently
migrated.

Provider endpoints and credentials remain in the existing Generator/Evaluator
private environment files and are never readable by Reader.

For Ollama production, automatic pre-review uses the native `/api/chat`
Structured Output path with role-specific thinking policy:

```text
Generator: temperature=0, think=false
Evaluator: temperature=0, think=false
```

Generator is intentionally non-thinking because its job is bounded synthesis from
explicit sources. Production diagnostics of the exact Gemma 4 groundedness prompt
measured `think=low` timing out after 300 seconds without a response, while
`think=false` returned valid structured output in about 6 seconds. New Ollama
Evaluator recipes therefore use `think=false`; immutable historical recipes with
`think=low` remain parseable and execute with their stored value. Provider, adapter,
model digest, thinking policy, and options are all part of the immutable recipe/job
identity. The generic OpenAI-compatible adapter remains available for non-Ollama
providers.

## Semantic production integration

The controlled `deep-knowledge-v1` integration is documented in
[Semantic Deep Knowledge Production v1](semantic-deep-knowledge-production-v1.md).

Semantic mode selects across Daily, Idea, Project, Project Note and Knowledge,
then Reader materializes only the exact selected chunk bytes into an Objective
Context. Generator still has no Vault/Index/Selection-store access.

Idea-discovery and Project-adoption objectives remain outside automatic durable
jobs until the Human/Core canonical actions are ready.

## Future extensions

Selection policy and objective remain independently versioned. Automatic
Semantic Index rebuild, non-Knowledge durable jobs and other objectives require
separate reviewed rollout contracts.

Human-facing `04-AI` remains excluded from the input corpus even though its
Review controls feed separate fail-closed Human actions.
