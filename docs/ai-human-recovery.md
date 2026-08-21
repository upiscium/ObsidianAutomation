# Human recovery contract v0

## Purpose

Durable execution intent prevents blind retry after a crash, but it intentionally leaves two ambiguous states unresolved:

- `effect_observed_without_receipt`: the canonical target matches the intended bytes, but no durable receipt proves which actor created it;
- `conflict`: the canonical target or durable artifacts no longer match the approved execution intent.

Human recovery does not convert either state into a fabricated executor success. It records a separate Human decision bound to the exact durable execution intent.

## Recovery artifact

A recovery decision is stored in the Human-owned review stage:

```text
20-AI/20-Review/<mutation_sha256>.recovery.json
```

The filename is content-addressed by the already validated mutation SHA, never by untrusted `mutation_id`.

The record contains:

- `mutation_sha256`: exact validated mutation artifact;
- `intent_sha256`: SHA-256 of the exact durable execution intent bytes;
- `decision`;
- the reconciliation state observed when the decision was created;
- target path and expected content SHA-256 from the intent;
- UTC decision timestamp;
- resolver metadata and an audit reason.

The reusable schema is `schemas/human-recovery-v0.schema.json`.

`resolver` is audit metadata, not proof of Human identity. Human Authority is established by the production permission boundary that grants the Human recovery writer access to `20-Review` while Generator, Validator, Evaluator, and Executor accounts do not receive that write authority.

## Decisions

### `adopt_observed_effect`

Allowed only when reconciliation is exactly `effect_observed_without_receipt`.

Meaning:

> A Human inspected the ambiguous state and accepts the currently observed canonical effect as the desired Vault state.

This decision does **not** state that the deterministic Executor created the note. Therefore:

- no execution receipt is synthesized;
- no executor timestamp is invented;
- no automatic retry occurs;
- recovery-aware reconciliation reports `resolved_effect_adopted` while the target continues to match the intent.

If the target later diverges, the state becomes `conflict`; the Human recovery record does not mask later canonical drift.

### `abandon`

Allowed when reconciliation is `effect_observed_without_receipt` or `conflict`.

Meaning:

> This approved mutation is retired and must never be automatically executed again.

Recovery-aware reconciliation reports `resolved_abandoned`. Even if the conflicting target is later removed and the underlying raw execution state would otherwise become `pending_retry`, the recovery-aware runner remains blocked. A future desired write requires a new validated mutation and new Human approval.

## Immutability

There is one recovery decision per mutation SHA. The record is append-only / immutable:

- an identical semantic retry returns the existing record;
- a conflicting second decision is rejected;
- changing from `adopt_observed_effect` to `abandon`, or vice versa, is not supported in v0.

This prevents recovery history from becoming a mutable control flag.

## Recovery-aware production entrypoint

Once this contract is adopted, production must use:

```text
run_recovery_aware_create_note
```

rather than the lower-level `run_approved_create_note` primitive.

The recovery-aware runner holds the same per-mutation host-local lock used by execution intent orchestration and checks the Human recovery record before any retryable effect.

Terminal states include:

```text
completed
resolved_effect_adopted
resolved_abandoned
effect_observed_without_receipt
conflict
```

Only `not_started` and `pending_retry` may proceed toward canonical write, and only when no Human recovery decision blocks the mutation.

## Concurrency and Authority boundary

The existing `flock` remains host-local. Production v0 therefore requires one canonical Executor / writer host.

Recommended write authorities:

```text
Generator
  write: 20-AI/00-Untrusted

Validator
  write: 20-AI/10-Validation

Human review / recovery tool
  write: 20-AI/20-Review

Executor
  write: 20-AI/25-Execution
  write: 20-AI/30-Receipts
  write: approved canonical roots
```

The Executor reads Human recovery records but cannot create or replace them in the production permission model.

## Failure semantics

- recovery without durable intent -> reject;
- recovery for `not_started`, `pending_retry`, or `completed` -> reject;
- `adopt_observed_effect` for conflicting content -> reject;
- recovery record / intent hash mismatch -> `conflict`;
- adopted target later diverges -> `conflict`;
- success receipt appearing after a Human recovery decision -> `conflict`;
- abandoned mutation -> never auto-retry, even if the target later disappears.

## Out of scope

- proving Human identity cryptographically;
- undoing or deleting an observed canonical effect;
- changing a recovery decision after it is recorded;
- automatically repairing conflicts;
- distributed multi-host execution locks;
- update / merge / delete / rename mutation types.

After this Gate, the remaining prerequisite before a production AI write path is the concrete canonical-writer topology and OS permission/credential boundary. Generator and Evaluator LLM integration should still wait until that boundary is exercised on fixtures and a disposable Vault.
