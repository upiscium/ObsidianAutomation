# Generator Artifact Contract v0

## Purpose

The Generator is an untrusted transformation stage between immutable Reader-produced Context Bundles and the existing untrusted canonical-mutation proposal stage.

```text
05-Context/<context_sha>.context.json
        ↓ read-only
Generator
        ├── 00-Untrusted/<proposal_sha>.proposal.json
        └── 00-Untrusted/<generation_sha>.generation.json
        ↓
Validator
```

This contract defines the provenance artifact written by the Generator before any LLM provider is integrated. It does not grant validation, approval, execution, transport, or canonical-write authority.

## Authority boundary

The Generator may:

- read immutable `05-Context/*.context.json` artifacts;
- create immutable `00-Untrusted/*.proposal.json` artifacts;
- create immutable `00-Untrusted/*.generation.json` artifacts.

The Generator must not:

- read the canonical Vault directly;
- read `04-Index`;
- write `05-Context`;
- write Validation, Review, Execution, Transport, or Receipt stages;
- hold the Nextcloud writer credential.

A Generation Record is written by the same untrusted identity that produced the proposal. Therefore it is audit provenance, not a trusted machine attestation. Validator and Human approval must not treat a Generation Record as proof that a proposal is correct, safe, or canonical-write eligible.

## Generation Record v0

Generation Records use canonical JSON and are content-addressed by the SHA-256 of the exact record bytes:

```text
00-Untrusted/<generation_sha256>.generation.json
```

Schema:

```json
{
  "record_version": 1,
  "context_sha256": "<64 lowercase hex>",
  "proposal_sha256": "<64 lowercase hex>",
  "generator": {
    "implementation_revision": "<generator implementation revision>",
    "prompt_template_version": "knowledge-note-generator-v0",
    "prompt_template_sha256": "<64 lowercase hex>"
  },
  "model": {
    "provider": "ollama",
    "identifier": "<model identifier>",
    "revision": "<model revision/digest>"
  },
  "model_config": {},
  "generated_at": "<UTC timestamp ending in Z>"
}
```

The minimum provenance chain is therefore:

```text
exact Context bytes
  └── context_sha256

versioned prompt template
  ├── prompt_template_version
  └── prompt_template_sha256

Generator implementation
  └── implementation_revision

model identity/configuration
  ├── provider
  ├── identifier
  ├── revision
  └── model_config

exact untrusted proposal bytes
  └── proposal_sha256
```

## Binding requirements

A Generation Record may be persisted only when:

1. `05-Context/<context_sha256>.context.json` exists and its bytes hash to `context_sha256`;
2. `00-Untrusted/<proposal_sha256>.proposal.json` exists and its bytes hash to `proposal_sha256`;
3. the Generation Record itself satisfies the v0 schema and bounds;
4. the canonical serialized Generation Record round-trips through the parser;
5. the final Generation Record is persisted immutably with `O_EXCL` semantics through the shared artifact storage primitive.

The record binds exact artifacts, not mutable filenames or "latest" pointers.

## Bounds

Generation Record v0 applies explicit bounds so an untrusted provider response cannot create unbounded lifecycle metadata:

- Generation Record: maximum 64 KiB canonical JSON;
- model configuration: maximum 16 KiB canonical JSON;
- model configuration nesting depth: maximum 4;
- list/object entries: maximum 128 per container;
- metadata strings: maximum 512 characters;
- model-configuration string values: maximum 4096 characters;
- non-finite JSON numbers are rejected.

Unknown or duplicate JSON properties are rejected.

## Proposal semantics

The Generation Record does not change the canonical mutation contract. The proposal remains the existing untrusted `create_note` proposal and must pass the existing deterministic Validator before it can produce a validated mutation.

When LLM inference is added, the model should not be given authority over invariant fields that deterministic code can construct safely. In particular, fixed Knowledge Note policy fields and mutation-envelope fields should be assembled by deterministic code where practical, while the Validator continues to re-check the complete resulting proposal.

## Reproducibility boundary

`implementation_revision`, prompt-template version/hash, Context SHA, model identity/revision/configuration, and proposal SHA provide an auditable generation envelope. They do not guarantee bit-for-bit reproducibility of nondeterministic model inference.

If future debugging requires binding the exact provider request or raw provider response, separate immutable request/response artifacts may be introduced in a later contract version rather than silently extending v0.

## Out of scope

- Ollama HTTP/API integration;
- prompt content and prompt rendering implementation;
- raw model request/response retention;
- Evaluator LLM;
- semantic retrieval or reranking;
- automatic Human approval;
- update/merge/delete/rename canonical mutations.
