# Durable pre-review job orchestration v0

## Scope

This contract is the durable pre-review control plane for #64. It accepts one
explicit immutable Context and coordinates identity-scoped Generator, Validator,
Reader, and Evaluator workers until the selected generation reaches Human Review.

It never approves, rejects on behalf of a Human, executes, transports, or writes
canonical Vault content.

## Authority boundary

The job database is scheduling/progress metadata only.

```text
05-Context/<sha>.context.json
        |
        | explicit submit
        v
02-Orchestration/
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

Recipe v0 supports only the currently deployed pipeline contracts:

- Generator prompt `knowledge-note-generator-v0`;
- Generator adapter `ollama-chat-structured-v0`;
- Validator policy `knowledge-note-v0`;
- Evaluation Context policy `bm25-topk-recall-v0` with `top_k=5`;
- Evaluator prompt `knowledge-note-evaluator-v3`;
- Evaluator adapter `ollama-evaluator-chat-structured-v2`;
- Evaluator strategy `groundedness-plus-pairwise-candidates-v0`.

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

Any active processing state may enter `retryable_failure`, `blocked`, or
`deterministic_reject`.

Transient failures are retried at the **same failed stage**, never by silently
restarting Generator. The automatic worker path allows at most three attempts
per stage. A fourth claim converts the generation to `retry_exhausted`.

`blocked` represents recipe/runtime/binding drift that requires operator
attention. `deterministic_reject` is reserved for deterministic Validator
rejection. `deterministic_reject`, `retry_exhausted`, `blocked`, and
`awaiting_human_review` stop automatic progress.

Explicit `retry` restores the failed stage. Explicit `regenerate` creates a
new generation under the same job.

Each successful stage stores only a selected bounded SHA binding in the
`stage_outputs` table. Downstream workers consume that selected output and
re-validate the immutable artifact itself; they never scan artifact directories
and guess which output to adopt.

This metadata never writes a Human Review artifact. Both `proceed` and
`do_not_proceed` Evaluation outcomes terminate orchestration at
`awaiting_human_review`. Recommendation interpretation remains Human-owned.

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
`synchronous=FULL`. The database file is mode 0660 so the narrow
Generator/Validator/Reader/Evaluator operational ACL can share metadata without
sharing semantic artifact authority.

Recipes remain immutable content-addressed JSON files so a job can always prove
which fixed recipe it references even if future deployment defaults change.

## Identity worker chain

The reusable worker entrypoints are:

```text
obsidian-pre-review-generator-worker
obsidian-pre-review-validator-worker
obsidian-pre-review-reader-worker
obsidian-pre-review-evaluator-worker
```

The example systemd chain starts the Evaluator service and pulls dependencies in
this order:

```text
Generator -> Validator -> Reader -> Evaluator
```

Every service runs as its matching existing Linux identity. There is no root
or all-artifact orchestration runner.

A worker invocation processes at most one generation/stage. A service restart
may find a prior `running` attempt left by a crash; it marks that attempt
`interrupted` and starts a new attempt without adopting unselected artifacts.

Generator backpressure stops new generation claims when eight current
generations are already `awaiting_human_review`.

## Deferred to Wave D

- production ACL rollout and exact-revision package installation;
- enabling the example timer;
- production crash/resume/provider/backpressure acceptance;
- role-limited operational status projection and notifications;
- any post-Human-review automatic execution or transport.

The example timer is intentionally not enabled by repository code.
