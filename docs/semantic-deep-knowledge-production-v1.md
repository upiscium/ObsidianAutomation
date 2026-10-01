# Semantic Deep Knowledge Production v1

## Purpose

This runbook is the controlled production integration for Semantic Planner v1.
It enables only `deep-knowledge-v1` automatic jobs while preserving the existing
Knowledge Validation, Evaluation, Human Review, Executor, Transport and Receipt
authority chain.

Idea discovery and Project adoption remain operator-driven Human-facing
candidates until the corresponding ObsidianCore Human actions are available.

## Default and opt-in modes

The systemd unit defaults to:

```text
AI_INPUT_MODE=legacy
AI_INPUT_SEMANTIC_INDEX_SHA=disabled
AI_INPUT_SEMANTIC_SELECTION_POLICY=semantic-project-distill-v0
```

Therefore deploying this code does not switch production behavior.

To opt in:

```text
AI_INPUT_MODE=semantic-deep-knowledge
AI_INPUT_SEMANTIC_INDEX_SHA=<exact-reviewed-semantic-index-sha256>
AI_INPUT_SEMANTIC_SELECTION_POLICY=semantic-project-distill-v2
```

The Semantic Index SHA is mandatory and exact. There is no mutable `latest`
pointer and this stage does not automatically rebuild the index.

A missing, stale, corrupted or mismatched index fails before durable job
submission.

## Planner sequence

Existing gates still execute first:

```text
pending recovery
  -> blocked/retry-exhausted gate
  -> Human Review hard backpressure
  -> target_inflight
  -> durable cadence
```

When the gate opens in semantic mode:

1. Reader loads the exact pinned Semantic Index.
2. Reader revalidates the bound Semantic Corpus against the current pull-only mirror.
3. Reader executes the configured versioned Semantic Selection policy.
4. Reader stores the content-addressed Selection Record.
5. If novelty says `skipped`, Reader records the bounded skip reason and creates no job.
6. If selected, Reader materializes the bounded `deep-knowledge-v1` Objective Context in memory and applies the deterministic evidence-sufficiency gate. Structural-only evidence, fewer than two substantive sources, or fewer than 160 substantive bytes causes `skipped_evidence` with no durable job/provider call.
7. If evidence is sufficient, Reader stores the Objective Context and journals mode/index/selection/objective/context in the existing durable pending record.
8. Reader submits the exact Objective Context + deep recipe into the existing pre-review orchestration DB.
9. Only successful durable submission advances `last_submission_at`.

A novelty skip therefore does not consume the generation cadence.

## Pending recovery

The pending journal binds:

- input mode;
- exact Semantic Index SHA;
- Selection SHA and policy;
- `deep-knowledge-v1`;
- exact Objective Context SHA;
- exact selected source/chunk bindings;
- recipe SHA;
- cadence anchor;
- scheduler state before/after submission.

Recovery requires the current production mode, index SHA and selection policy to
match the pending transaction. Configuration drift fails closed instead of
silently resubmitting against another index.

The same Objective Context + recipe produces the same job identity, so restart
recovery is idempotent.

## Generation stage

The existing `knowledge-pre-review-v0` orchestration state machine is retained.

A deep recipe is identified by exact prompt identity:

```text
deep-knowledge-generator-v2
```

Generation performs. `deep-knowledge-generator-v2` may also return the structured `no_candidate / insufficient_evidence` form; that outcome is persisted for provenance and terminates as `deterministic_reject` before proposal materialization or Human Review.


```text
Objective Context
  -> objective-specific structured output
  -> Objective Candidate
  -> Objective Generation provenance
  -> deterministic Knowledge create_note proposal
  -> Generation Record v2
```

The stage output remains the existing:

```text
proposal_sha256
generation_sha256
```

so Validator and later orchestration states do not require a parallel state
machine.

Generation Record v2 binds the exact Objective Context, Objective Generation,
Objective Candidate, Selection, Semantic Index, prompt identity, model identity
and model configuration.

Historical Generation Record v1 remains readable and executable through the
legacy path.

## Evaluation grounding

For Generation Record v1, Evaluator groundedness reads the legacy Context Bundle.

For Generation Record v2, Evaluator groundedness reads an in-memory Context view
derived from the exact Objective Context selected chunks. This preserves Daily,
Idea, Project, Project Note and Knowledge evidence selected by Reader without
granting Evaluator direct Vault or Semantic Index access.

Redundancy and consistency retrieval continue to use the existing canonical
Knowledge BM25 Evaluation Context.

## Authority

`02-Orchestration/semantic-selections` is explicitly Reader-only.

Generator, Validator and Evaluator still share the orchestration DB needed for
stage transitions, but cannot read or write Semantic Selection Records.

Generator receives only `05-Context/<sha>.objective-context.json`, and keeps no
Vault or Index authority.

Validator, Evaluator, Reviewer and Executor permissions are unchanged.

## Production acceptance gate

Before enabling semantic mode, run the machine-checkable acceptance flow in
[Semantic Planner production acceptance v1](semantic-production-acceptance-v1.md).

It proves the exact deployed revision, legacy default, Reader-only Selection
Store, exact current Semantic Index, Phase C benchmark result and one Selection
observation before emitting an inert canary environment plan. The benchmark and
Selection receipts must bind the same retrieval profile; the initial accepted
rollout uses `semantic-retrieval-v1` through
`semantic-project-distill-v2`. v2 preserves v1 ranking semantics while
deterministically advancing past Project clusters rejected by novelty or other
selection gates.

## Rollback

Set:

```text
AI_INPUT_MODE=legacy
AI_INPUT_SEMANTIC_INDEX_SHA=disabled
```

and restart/reload the normal service configuration. Historical semantic jobs
already started are not rewritten; the change affects only later Planner cycles.

## Out of scope

- automatic Semantic Index rebuild or latest-pointer management;
- automatic Idea-discovery durable jobs;
- automatic Project-adoption durable jobs;
- Human-side Core save/adoption implementation;
- replacing Evaluator Knowledge BM25 with hybrid retrieval;
- automatic Human approval.
