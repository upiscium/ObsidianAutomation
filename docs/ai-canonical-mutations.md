# AI canonical mutation contracts

## Purpose

Phase 2 introduces AI-assisted Vault maintenance without granting an LLM direct write authority over canonical Vault content.

The first canonical mutation is `create_note`. It is deliberately narrow: create exactly one new Markdown file at an approved relative path, with exact UTF-8 content, and never overwrite an existing note.

This contract is not an LLM response format. Reasoning, confidence, summaries, source selection, and evaluator commentary remain outside the canonical mutation. Only the deterministic operation that may eventually reach the Vault belongs here.

## Authority model

```text
Reader / Indexer
  -> read-only Vault access

Generator LLM
  -> may write proposals only to 20-AI/00-Untrusted

Deterministic Validator
  -> reads untrusted proposals
  -> emits validated canonical mutation artifacts to 20-AI/10-Validation
  -> has no canonical Vault write authority

Evaluator LLM
  -> may evaluate validated proposals
  -> has no canonical Vault write authority

Human
  -> approves or rejects an exact validated mutation artifact

Deterministic Executor
  -> executes only an exact human-approved canonical mutation
  -> may write only to deployment-policy-approved canonical roots
  -> emits execution receipts to 20-AI/30-Receipts
```

No LLM is permitted to write directly to `11-Knowledge` or another canonical Vault root.

## `create_note` v0

The reusable syntactic contract is defined in `schemas/create-note-v0.schema.json`.

Minimal shape:

```json
{
  "contract_version": 1,
  "operation": "create_note",
  "mutation_id": "018f6c3e-7c8f-7b52-a7a2-8be3518e7182",
  "target": {
    "path": "11-Knowledge/example.md"
  },
  "content": "# Example\n"
}
```

`mutation_id` is an opaque stable identifier used to correlate validation, review, execution, and receipts. It does not grant authority and must not be used as a substitute for content binding.

`target.path` is always a Vault-relative POSIX path. The JSON Schema provides only basic syntactic constraints; the deterministic validator and executor must enforce the semantic path rules below.

## Semantic validation requirements

A canonical `create_note` mutation is admissible only when all of the following hold:

- `contract_version == 1` and `operation == "create_note"`.
- The mutation matches the JSON Schema with no unknown properties.
- `target.path` is a relative POSIX path and ends in `.md`.
- The path contains no empty component, `.` component, `..` component, backslash, NUL, or absolute-path prefix.
- The normalized target is inside a deployment-policy-approved canonical root. The reusable public contract does not hard-code `11-Knowledge`; production policy does.
- No component of the destination path resolves through a symlink.
- The target does not already exist as any filesystem object.
- A case-fold-equivalent sibling collision is rejected so that sync to case-insensitive clients cannot silently alias two notes.
- `content` is non-empty Unicode text and is encoded as UTF-8 by the executor.
- Any Vault-specific note policy, such as required frontmatter for `11-Knowledge`, passes deterministic validation before approval.

`create_note` never overwrites, merges, renames, patches, or deletes existing canonical content. Those are separate future mutation types and must not be smuggled through this operation.

## Approval binding

Human approval must bind to the exact bytes of the validated canonical mutation artifact, not merely to `mutation_id`, filename, title, or a natural-language summary.

The validator therefore emits a validated mutation file and records:

```text
mutation_sha256 = SHA256(exact validated mutation file bytes)
```

The approval record references that SHA-256. Immediately before execution, the executor recalculates the hash of the validated artifact and requires an exact match.

This prevents a proposal from being changed after review while retaining the same `mutation_id` or visible summary.

The hash binds the validated artifact as stored; v0 does not require independent components to reproduce a cross-language JSON canonicalization algorithm.

## Execution semantics

Immediately before mutating the Vault, the deterministic executor revalidates:

1. approval exists and is affirmative;
2. approval references the exact validated mutation SHA-256;
3. schema and semantic path policy still pass;
4. the target is still absent;
5. no symlink or path-boundary condition has changed;
6. deployment policy still authorizes the target root.

Only then may it create the note.

Creation should use a temporary file in the destination directory followed by an atomic rename that fails rather than replacing an existing target. A successful execution writes a receipt containing at least the mutation ID, validated mutation SHA-256, target path, resulting content SHA-256, execution timestamp, and result.

## Failure semantics

Any failed precondition means no canonical mutation occurs.

In particular:

- invalid schema -> reject;
- unsafe or unauthorized path -> reject;
- missing approval -> reject;
- approval hash mismatch -> reject;
- target already exists -> reject;
- symlink/path race detected -> reject;
- deterministic note-policy validation failure -> reject;
- filesystem creation failure -> report failure and do not claim success.

An executor failure must never be converted into an overwrite or merge fallback.

## Out of scope for v0

- update or merge of an existing note;
- delete or rename;
- attachment creation;
- arbitrary filesystem operations;
- LLM-controlled destination roots;
- LLM-controlled approval;
- automatic approval based on evaluator score;
- direct Generator/Evaluator writes to canonical Vault content.
