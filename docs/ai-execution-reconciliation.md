# Durable execution intent and crash reconciliation

## Purpose

`create_note` changes canonical Vault state and then persists an execution receipt. Those
two filesystem effects cannot be made one cross-directory atomic transaction.

Without an intermediate durable record, a process crash after note creation but before
receipt persistence leaves an ambiguous state:

```text
canonical note exists
receipt missing
```

This module closes the retry-safety gap with a durable pre-effect execution intent and
deterministic reconciliation. It does not claim provenance that cannot be proven.

## Layout extension

Phase 2 adds one execution-journal directory:

```text
20-AI/
├── 00-Untrusted/
├── 10-Validation/
├── 20-Review/
├── 25-Execution/
│   ├── <mutation_sha256>.intent.json
│   └── <mutation_sha256>.lock
└── 30-Receipts/
```

The intent is an immutable artifact. The lock file is operational state used only for
host-local mutual exclusion and is not an approval or audit artifact.

## Durable intent

Before canonical mutation, the executor persists and `fsync`s an intent containing:

- exact validated `mutation_sha256`;
- SHA-256 of the exact immutable Human approval record bytes;
- target path;
- intended content SHA-256;
- preparation timestamp.

Only after the intent file and its directory entry are durable may the canonical effect
be attempted.

The intent does not itself grant authority. The executor still requires the immutable
affirmative Human review record and rechecks current deployment policy.

## Execution order

The v0 orchestration is:

```text
per-mutation host lock
  ↓
reconcile existing state
  ↓
load exact validated mutation
  ↓
load exact affirmative Human approval
  ↓
revalidate current allowed root + target absence
  ↓
persist + fsync immutable execution intent
  ↓
effect-boundary create_note validation
  ↓
exclusive canonical create
  ↓
persist + fsync immutable success receipt
  ↓
final reconciliation
```

A retry never converts an existing target into overwrite or merge behavior.

## Reconciliation states

`reconcile_execution` returns one of four relevant states plus `not_started`:

### `not_started`

No durable intent exists. No canonical effect may be inferred.

### `pending_retry`

A durable intent exists, no success receipt exists, and the canonical target is absent.
It is safe to retry the original approved `create_note`, subject to effect-boundary
revalidation.

### `completed`

The success receipt exists and is bound to the same mutation, target, and content hash,
and the current canonical target still matches that content.

### `effect_observed_without_receipt`

The durable intent exists, the success receipt is absent, and the canonical target
contains exactly the intended bytes.

The system deliberately does **not** synthesize a success receipt in this state. An
identical file could have been created manually or by another authority after intent
creation. Matching state proves that the intended effect is present; it does not prove
which actor produced it.

Automatic retry is therefore suppressed to avoid overwrite and duplicate-authority
claims. A later recovery workflow may let a Human resolve this state explicitly.

### `conflict`

The journal and canonical state cannot be reconciled safely. Examples include:

- target content differs from the intent;
- a success receipt exists but the target is missing or changed;
- target or parent path is replaced by a symlink;
- case-fold collision appears;
- the current deployment policy no longer authorizes the target.

No canonical write occurs in this state.

## Approval binding

The intent binds the SHA-256 of the exact `20-Review/<mutation>.approval.json` bytes.
If that artifact is externally changed after intent preparation, reconciliation fails
instead of accepting the new bytes under the same filename.

`approver` remains metadata. Human Authority is provided by the production permission
boundary that controls who may write `20-Review`.

## Concurrency

v0 uses a per-mutation `flock` in `25-Execution` to serialize executor operations on a
single Linux host.

This is **not** a distributed lock. Production v0 therefore requires one canonical
executor host / writer authority. Multi-host execution requires a separate distributed
coordination design.

## Durability assumptions

Intent and receipt persistence use file `fsync` plus parent-directory `fsync`.
Correctness assumes the underlying local filesystem honors normal POSIX durability
semantics.

This does not make Nextcloud, multiple filesystems, or remote synchronization one atomic
transaction.

## Out of scope

- automatic resolution of `effect_observed_without_receipt`;
- distributed multi-host locking;
- update / merge / delete / rename;
- automatic Human approval;
- production Nextcloud writer credentials;
- Generator or Evaluator LLM integration.
