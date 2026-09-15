# AI artifact lifecycle v0

## Purpose

This document defines the filesystem contract for Phase 2 AI artifacts. It sits above the `create_note` canonical mutation contract and below any Generator/Evaluator LLM.

The lifecycle is append-only and content-addressed. An LLM may propose bytes, but no untrusted identifier is ever used as a filesystem pathname and no LLM receives canonical Vault write authority.

## Directory layout

The reusable lifecycle includes:

```text
20-AI/
├── 00-Untrusted/
├── 10-Validation/
├── 15-Evaluation/
├── 20-Review/
└── 30-Receipts/
```

The production deployment contains additional derived and transport stages. Production OS permissions remain the authority boundary; the Python library does not substitute for them.

Recommended authority for these stages:

```text
Generator / intake
  write: 00-Untrusted only

Deterministic Validator
  read:  00-Untrusted + canonical Vault
  write: 10-Validation only

Evaluator
  read: exact selected evidence + accepted Validation
  write: 15-Evaluation only

Human review tool
  read:  10-Validation + 15-Evaluation
  write: 20-Review only

Deterministic Executor
  read:  10-Validation + 20-Review
  write: approved execution/transport/receipt stages
```

The Snapshot LXC credential from Phase 1 remains read-only toward Nextcloud and must not silently become the canonical writer credential.

## Content addressing

`mutation_id` is untrusted opaque metadata. It may contain text that would be unsafe as a pathname and therefore is never used to name lifecycle files.

Relevant artifacts are named by SHA-256:

```text
00-Untrusted/<proposal_sha256>.proposal.json
10-Validation/<mutation_sha256>.mutation.json
10-Validation/<proposal_sha256>.validation.json
15-Evaluation/<evaluation_sha256>.evaluation.json
20-Review/<mutation_sha256>.approval.json
30-Receipts/<mutation_sha256>.receipt.json
```

All digest filenames use lowercase 64-character hexadecimal SHA-256. Lifecycle artifacts use create-only semantics. Repeating an operation with exactly the same bytes is idempotent; existing bytes are never overwritten.

## 00-Untrusted

The proposal is stored exactly as received:

```text
proposal_sha256 = SHA256(exact proposal bytes)
```

No trust is implied by storage in this directory. Invalid JSON and semantically invalid mutations may exist here.

## 10-Validation

An accepted proposal creates the exact deterministic validated mutation plus a validation record linking the proposal hash to the mutation hash.

Accepted record:

```json
{
  "record_version": 1,
  "proposal_sha256": "<64hex>",
  "result": "accepted",
  "validated_at": "2026-08-21T00:00:00Z",
  "mutation_sha256": "<64hex>",
  "reason": null
}
```

A rejected proposal has no canonical mutation artifact and records `mutation_sha256: null` plus a deterministic diagnostic reason. The reason grants no authority.

## 15-Evaluation

Evaluator writes an immutable advisory semantic assessment. Evaluation is machine output and is not approval authority.

An Evaluation Record binds, among other provenance, the exact proposal SHA-256 and accepted mutation SHA-256 that were evaluated. Its recommendation remains advisory:

```text
Evaluation != Validation
Evaluation != Human approval
Evaluation recommendation != execution authority
```

## 20-Review

### Review Record v2

New Evaluator-backed Human Review creates one immutable decision bound to both the exact validated mutation and the exact Evaluation artifact the Human reviewed:

```json
{
  "record_version": 2,
  "mutation_sha256": "<64hex>",
  "evaluation_sha256": "<64hex>",
  "decision": "approve",
  "decided_at": "2026-09-15T00:02:00Z",
  "approver": "human"
}
```

`obsidian-knowledge-review` verifies before persistence that:

1. `15-Evaluation/<evaluation_sha256>.evaluation.json` exists and its bytes hash to the supplied digest;
2. the Evaluation Record parses strictly;
3. the Evaluation's proposal has accepted Validation;
4. the accepted Validation mutation equals the Evaluation's `mutation_sha256`;
5. the exact validated mutation artifact exists and hashes to that mutation digest.

Only then is `20-Review/<mutation_sha256>.approval.json` created with `O_CREAT | O_EXCL` semantics.

Human authority is independent from the Evaluator recommendation. A Human may explicitly approve a `do_not_proceed` evaluation or reject a `proceed` evaluation. The Evaluator remains advisory.

`approver` is audit metadata, not cryptographic proof of human identity. Human authority comes from the production permission boundary around the review writer. Generator, Validator, Evaluator, and Executor processes must not be able to write `20-Review`.

### v1 compatibility

Legacy Review Record v1 remains parseable for existing artifacts:

```json
{
  "record_version": 1,
  "mutation_sha256": "<64hex>",
  "decision": "approve",
  "decided_at": "2026-08-21T00:01:00Z",
  "approver": "human"
}
```

New Evaluator-backed review creation uses v2. The legacy writer exists only for compatibility with earlier workflows and tests.

### Executor binding

The Executor does not receive read access to `15-Evaluation`. It parses the Human approval from `20-Review` and continues to bind the SHA-256 of the exact approval bytes into durable Execution Intent.

For v2 this gives a transitive audit chain:

```text
Execution Intent
  -> approval_sha256
     -> exact Review Record v2 bytes
        -> evaluation_sha256
           -> exact Evaluation Record bytes
```

Changing any approval bytes after intent preparation causes reconciliation to fail closed. The Human review file itself remains immutable under normal operation.

## 30-Receipts

A successful deterministic execution may persist the existing `ExecutionReceipt` bytes as:

```text
30-Receipts/<mutation_sha256>.receipt.json
```

Receipt persistence is create-only and requires the corresponding exact validated mutation artifact to exist.

Receipt persistence alone is not a cross-file transaction guarantee. A crash after canonical note creation but before receipt persistence can leave the canonical mutation applied without the final receipt. Durable execution intent and crash reconciliation handle this boundary explicitly.

## Symlinks and immutability

Lifecycle stage directories must be real directories, not symlinks. Artifact files use no-follow opens where `O_NOFOLLOW` is available and are created with `O_CREAT | O_EXCL`. Existing artifacts are accepted only when their bytes are exactly identical.

This contract assumes the lifecycle root itself is deployment-controlled. Production must not grant an LLM permission to replace stage directories or alter validated/evaluated/reviewed artifacts.

## Out of scope

- automatic Human approval;
- approval UI;
- cryptographic Human signatures;
- granting Human reviewer canonical Vault write authority;
- granting Executor read authority over Evaluation;
- update / merge / delete / rename canonical mutations.
