# Durable pre-review job orchestration v0

## Scope

This is Wave A of #64. It defines durable orchestration metadata for moving an
explicitly submitted immutable Context toward Human Review. It does **not** yet
start Generator, Validator, Reader, or Evaluator workers.

It also does not approve, reject, execute, transport, or write canonical Vault
content.

## Authority boundary

The job database is scheduling/progress metadata only.

```text
05-Context/<sha>.context.json
        |
        | explicit submit
        v
02-Jobs/
  recipes/<sha>.recipe.json
  pre-review-jobs.sqlite3
```

A job state is never evidence that:

- Validation accepted a proposal;
- Evaluation produced a particular recommendation;
- a Human approved or rejected anything;
- a canonical mutation occurred;
- a Receipt exists.

Those statements remain authoritative only in their existing immutable lifecycle
artifacts.

## Identity model

The logical job identity is deterministic:

```text
job_id = SHA256(
  record_version
  + pipeline
  + exact Context SHA
  + exact Recipe SHA
)
```

Submitting the same Context + Recipe again resolves to the existing job and
does not create another generation.

A Human/operator may explicitly request regeneration. Regeneration creates a new
`generation_id` under the same logical job. Automatic retries do not silently
become new generations.

Within one generation, each worker invocation has a separate deterministic
`attempt_id` based on generation, stage, and attempt ordinal. LLM inference is
therefore not described as exactly-once.

## Recipe

The immutable recipe pins only bounded processing identity:

- Generator implementation revision;
- Generator prompt version/hash;
- Generator provider/model identity and model configuration;
- deterministic Validator policy name;
- Evaluation Context selection policy and `top_k`;
- Evaluator implementation revision;
- Evaluator prompt version/hash;
- Evaluator provider/model identity and model configuration.

Recipe v0 supports only the existing `ollama` adapters.

The recipe deliberately has no field for:

- shell commands;
- executable paths;
- arbitrary environment variables;
- credentials;
- Nextcloud/GitHub tokens;
- provider endpoint URLs;
- arbitrary filesystem paths.

Endpoints and credentials remain deployment configuration owned by the relevant
worker identity. A future worker must verify that its actual implementation,
prompt, policy, resolved model revision and model configuration match the pinned
recipe before accepting durable output.

## State machine

One generation follows this pre-review state machine:

```text
queued
  -> generating
       -> validating
            -> building_evaluation_context
                 -> evaluating
                      -> awaiting_human_review
```

Any active processing state may enter `retryable_failure` or
`deterministic_reject`.

`retryable_failure` requires an explicit retry transition back to `queued`.
`deterministic_reject` and `awaiting_human_review` are terminal for that
generation. A new model generation after deterministic rejection requires the
explicit `regenerate` operation.

This metadata never writes a Human Review artifact. Both `proceed` and
`do_not_proceed` Evaluation outcomes will eventually terminate orchestration at
`awaiting_human_review`; recommendation interpretation remains outside this
Wave A persistence layer.

## CLI

Submit an exact Context and recipe:

```bash
obsidian-pre-review-job submit \
  --ai-root /var/lib/obsidian-ai/state \
  --context-sha256 <exact-context-sha> \
  --recipe-file /path/to/reviewed-recipe.json
```

Submitting the same pair again reports `created=false`.

Explicit regeneration:

```bash
obsidian-pre-review-job regenerate \
  --ai-root /var/lib/obsidian-ai/state \
  --job-id <job-id>
```

Read orchestration status:

```bash
obsidian-pre-review-job status \
  --ai-root /var/lib/obsidian-ai/state \
  --job-id <job-id>
```

Status output contains only hashes, timestamps, generation count/state and the
literal authority marker `orchestration_metadata_only`. It does not include
Context text, proposal text, credentials, provider endpoints, or Review content.

## Persistence

`pre-review-jobs.sqlite3` uses SQLite transactions, foreign keys and
`synchronous=FULL`. The database file is mode 0600.

Recipes remain immutable content-addressed JSON files so a job can always prove
which fixed recipe it references even if future deployment defaults change.

## Deferred to later #64 slices

This PR intentionally does not add:

- systemd worker units or new production identities;
- automatic Generator -> Validator -> Reader -> Evaluator execution;
- retry timers/backoff;
- durable mirror-success projection or lock-wait metrics;
- Human Review queue limits/backpressure;
- immutable read-view coordination;
- notification delivery;
- production deployment or acceptance.

Those are subsequent Wave A/B/C/D slices built on this contract.
