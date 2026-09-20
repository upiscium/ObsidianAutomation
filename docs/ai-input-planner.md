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

`03-AI/**` is never an input source. Human-facing AI projections must not feed
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

The default cadence is four coverage selections followed by one random
selection, equivalent to an 80/20 split.

The seed is SHA-256-derived from the exact catalog digest plus epoch/cycle.
Selection is reproducible from durable metadata and does not depend on process
randomness.

## Queue policy

The planner creates at most one new job per invocation.

Defaults:

```text
target_inflight = 3
hard Human Review backpressure = 8
batch_size = 6
maximum batch_size = 8
```

`queued`, active processing states, and `awaiting_human_review` count toward
the target. New planning pauses when the target is already satisfied. It also
pauses on `blocked` or `retry_exhausted` generations instead of hiding an
operational problem by creating more work.

Post-review reconciliation is a later control-plane increment. Until that is
connected, three Human Review waits intentionally fill the queue and stop
automatic intake.

## Durable metadata

Mutable scheduler state:

```text
02-Orchestration/input-planner-state.json
02-Orchestration/input-planner-pending.json
```

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

The AI mirror must include both input roots. A reusable example is:

```text
examples/ai/vault-pull.filters
```

with:

```text
+ /11-Knowledge/**
+ /10-Project/**
- /**
```

The production file remains private deployment configuration at:

```text
/etc/obsidian-ai/vault-pull.filters
```

## Production service

`obsidian-ai-input-planner.service` runs before Generator. Generator only
`Wants=` the planner rather than `Requires=` it. Therefore a host without the
new model-selection file keeps the existing manual-submit pipeline working.

Automatic planning is enabled by creating:

```text
/etc/obsidian-ai/pre-review-input.env
```

with non-secret model identifiers:

```text
AI_INPUT_GENERATOR_MODEL=gemma4:12b
AI_INPUT_EVALUATOR_MODEL=gemma4:12b
```

The exact implementation revision is supplied separately by the updater-owned
`pre-review-revision.env`. The planner builds the immutable recipe with current
prompt hashes and the deployed revision every cycle. The pending-submission
journal also detects an unstarted queued job bound to an older recipe revision,
records that generation as `superseded`, and resubmits the same Context with
the current exact recipe. Started generations are never rewritten or silently
migrated.

Provider endpoints and credentials remain in the existing Generator/Evaluator
private environment files and are never readable by Reader.

## Future extensions

The source / selection / objective concepts are intentionally separate. Future
source adapters can add ChatGPT Export, Daily Notes, Papers, or other bounded
sources without changing Generator authority. Human-facing `03-AI` projection
and Approve -> Review -> Executor -> Transport wiring are separate increments.
