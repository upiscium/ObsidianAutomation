# Planner Generation Cadence v1

## Purpose

Planner Generation Cadence v1 rate-limits only automatic **new generation
submission**. It does not slow the pre-review lifecycle timer, Human Review
intake, post-review reconciliation, projection cleanup, status projection, or
worker progress.

This implements Semantic Planner issue #204 without enabling semantic selection
or novelty gating yet.

## Initial policy

The fixed initial policy is:

```text
hard minimum interval        15 min
normal target interval       60 min
awaiting Human Review = 1    90 min
awaiting Human Review >= 2   180 min
target_inflight default      2
hard Review backpressure     8
```

The hard minimum is a floor for all configured cadence intervals. The current
three backlog-derived intervals are all above that floor.

The interval is measured from the most recent durable automatic submission
anchor. The current Human Review backlog selects the applicable interval on each
Planner invocation.

Examples:

```text
last submission 10:00, backlog 0 -> next eligible 11:00
last submission 10:00, backlog 1 -> next eligible 11:30
last submission 10:00, backlog 2 -> next eligible 13:00
```

If the backlog changes, next eligibility is recomputed from the same last
submission timestamp. The backlog transition itself does not reset the clock.

## Lifecycle polling remains short

The production timer remains:

```ini
OnActiveSec=5min
OnUnitInactiveSec=2min
```

The timer still targets the aggregate pre-review status service and therefore
continues to drive the complete dependency graph.

The Input Planner is invoked on those cycles, but it returns
`paused_cooldown` without constructing a catalog, Context, selection artifact,
or new durable job until the generation gate opens.

This separation is intentional:

```text
short lifecycle polling
  !=
new generation submission cadence
```

Do not increase the systemd timer interval to implement generation throttling.

## Gate order

Automatic planning evaluates gates in this order:

1. reconcile an existing pending submission transaction;
2. pause on `blocked` or `retry_exhausted`;
3. enforce hard Human Review backpressure;
4. stop when `target_inflight` is already satisfied;
5. enforce Planner cooldown;
6. build catalog / selection / Context and submit at most one new job.

Operational/safety gates therefore take priority over cooldown reporting.

A pending submission recovery bypasses the normal cooldown check because it is
finishing one already-started durable submission transaction, not authorizing a
second generation.

## Durable state

Cadence state is separate from the existing selection cursor:

```text
02-Orchestration/input-planner-state.json
02-Orchestration/input-planner-cadence.json
02-Orchestration/input-planner-pending.json
```

`input-planner-state.json` continues to own coverage/random selection progress.

`input-planner-cadence.json` records only bounded orchestration metadata:

- last automatic submission timestamp;
- last selection policy identity;
- last objective policy identity;
- last semantic novelty skip timestamp, when present;
- last semantic novelty skip reason, when present.

It does not contain source text, Context bytes, proposal content, model output,
job IDs, or canonical mutation authority.

The cadence file is atomically replaced and fsynced. Restarting the service does
not reset cooldown.

## Pending transaction recovery

The pending submission journal stores a `cadence_anchor_at` timestamp before
job submission.

Once `submit_job` has durably succeeded, Planner records that anchor in the
cadence state before Human projection emission.

This ordering covers the relevant crash windows:

```text
prepared pending
  -> durable job
  -> durable cadence
  -> projections
  -> selection cursor
  -> clear pending
```

If the process crashes after durable job creation but before cadence persistence,
pending recovery reuses/resubmits the exact idempotent job and records the same
cadence anchor.

If it crashes after cadence persistence but before projections/cursor advance,
pending recovery finishes the existing transaction even though the ordinary
cooldown gate is closed.

Older pending records without `cadence_anchor_at` remain readable; their exact
Context creation timestamp becomes the recovery anchor.

## Observability

Pre-review operational status v1 / record version 2 adds
`planner_cadence`:

```text
eligible
interval_seconds
reason
awaiting_human_review
last_submission_at
next_eligible_at
last_selection_policy
last_objective_policy
last_novelty_skip_at
last_novelty_skip_reason
```

Status intentionally exposes policy identities rather than selection artifact
SHA values. The aggregate Status identity still does not receive semantic
artifact authority.

Historical operational status v0 / record version 1 remains parseable. New
status projections use:

```text
schemas/pre-review-operational-status-v1.schema.json
```

## Novelty extension

The durable cadence contract already has bounded novelty skip observability.

Issue #202 may record:

```text
last_novelty_skip_at
last_novelty_skip_reason
```

when semantic selection determines that no candidate is novel/coherent enough.

A novelty skip does not impersonate a submission and therefore does not rewrite
`last_submission_at`.

The exact novelty policy and skip decision remain out of scope for #204.

## Non-goals

This stage does not:

- change Semantic Corpus eligibility;
- choose BM25/vector/hybrid retrieval policy;
- enable Semantic Planner source selection;
- define semantic novelty thresholds;
- change Generator objectives/prompts;
- slow Human Review or cleanup polling;
- alter canonical Vault mutation authority.
