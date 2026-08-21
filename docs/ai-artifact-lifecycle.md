# AI artifact lifecycle v0

## Purpose

This document defines the filesystem contract for Phase 2 AI artifacts. It sits above the `create_note` canonical mutation contract and below any Generator/Evaluator LLM.

The lifecycle is append-only and content-addressed. An LLM may propose bytes, but no untrusted identifier is ever used as a filesystem pathname and no LLM receives canonical Vault write authority.

## Directory layout

The reusable layout is rooted at `20-AI/`:

```text
20-AI/
├── 00-Untrusted/
├── 10-Validation/
├── 20-Review/
└── 30-Receipts/
```

The directory names are fixed by this contract. Production deployment is expected to enforce narrower OS or service-account permissions for each role. The Python library does not substitute for that authority boundary.

Recommended authority:

```text
Generator / intake
  write: 00-Untrusted only

Deterministic Validator
  read:  00-Untrusted + canonical Vault
  write: 10-Validation only

Human review tool
  read:  10-Validation
  write: 20-Review only

Deterministic Executor
  read:  10-Validation + 20-Review
  write: approved canonical roots + 30-Receipts only
```

The Snapshot LXC credential from Phase 1 remains read-only toward Nextcloud and must not silently become the canonical writer credential.

## Content addressing

`mutation_id` is untrusted opaque metadata. It may contain text that would be unsafe as a pathname and therefore is never used to name lifecycle files.

Artifacts are named by SHA-256:

```text
00-Untrusted/<proposal_sha256>.proposal.json
10-Validation/<mutation_sha256>.mutation.json
10-Validation/<proposal_sha256>.validation.json
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

## 20-Review

Human review creates one immutable decision for an exact validated mutation SHA-256:

```json
{
  "record_version": 1,
  "mutation_sha256": "<64hex>",
  "decision": "approve",
  "decided_at": "2026-08-21T00:01:00Z",
  "approver": "human"
}
```

`decision` is `approve` or `reject`. `approver` is audit metadata, not cryptographic proof of human identity; human authority comes from the production permission boundary around the review writer. Generator and Evaluator processes must not be able to write this directory.

The review file is immutable. For v0, rejection is terminal for that exact validated mutation. A revised proposal must yield a new validated artifact and mutation SHA-256.

The Executor converts an affirmative review into the existing `ApprovalRecord` and still recalculates the validated artifact hash immediately before effect.

## 30-Receipts

A successful deterministic execution may persist the existing `ExecutionReceipt` bytes as:

```text
30-Receipts/<mutation_sha256>.receipt.json
```

Receipt persistence is create-only and requires the corresponding exact validated mutation artifact to exist.

Receipt persistence alone is not a cross-file transaction guarantee. A crash after canonical note creation but before receipt persistence can leave the canonical mutation applied without the final receipt. Durable execution intent and crash reconciliation are therefore the next executor-orchestration Gate rather than being hidden by this v0 contract.

## Symlinks and immutability

Lifecycle stage directories must be real directories, not symlinks. Artifact files use no-follow opens where `O_NOFOLLOW` is available and are created with `O_CREAT | O_EXCL`. Existing artifacts are accepted only when their bytes are exactly identical.

This contract assumes the lifecycle root itself is deployment-controlled. Production must not grant an LLM permission to replace stage directories or alter validated/reviewed artifacts.

## Out of scope

- Generator or Evaluator LLM implementation
- automatic approval
- approval UI
- cryptographic human signatures
- production Nextcloud write credentials
- update / merge / delete / rename canonical mutations
- durable pre-effect execution intent and crash reconciliation
