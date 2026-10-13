# GitHub Daily per-context verified resume v1 (#288)

This contract adds crash-safe reuse of previously validated **Partial** and
**Grounding** model outputs. The deterministic reducer still recomputes
its outputs and never asks the LLM to select evidence identities.

## Trust boundary

The Ollama adapter resolves its exact model identifier and 64-character digest
via /api/tags before the first inference. It fixes the provider, validated
model configuration/options, adapter version and the model digest in a typed
pre-inference `PreboundInferenceIdentity`. If an inference response's metadata
does not match the prebound identity, it is rejected before publishing resume
state. The key is never taken from provider-generated content.

The current Ollama /api/chat response reports the requested model name,
but does not attest the loaded model-manifest digest for that request.
Pre-resolving an alias and checking the echoed response name alone cannot
prove that a mutable model tag was not retargeted mid-run. The revised
ollama-chat-structured-v1-tag-guard adapter therefore re-resolves the
exact alias/digest immediately before and after each model inference, with
bounded /api/tags timeout, and fails closed before provenance or resume-pointer
publication if it observes a change. The adapter version and explicit
model_tag_trust_boundary enter the prebound cache identity so earlier outputs
cannot be silently reused under this stronger policy.

Residual assumption: this double-check detects persistent retargeting,
but it cannot detect an adversarial D1 -> D2 -> D1 substitution entirely
between checks. Model tags must therefore remain operationally immutable
for the full run under a trusted Ollama server. The prebound digest is an
operational trust claim, not cryptographic per-response model attestation.
If the server owner cannot enforce this assumption, production resume using
this adapter is not qualified. The client does not invent unverified digest
syntax for the /api/chat model argument.

Unbound Python Infer callbacks and the current OpenAI-compatible adapter
**do not use the resume cache**. The latter resolves its identity during the
response rather than before inference; adding a trusted prebinding would
require a separate contract. Generic and fixture callbacks remain compatible
with the original API.

## Immutable resume pointer

Each key covers all the following properties:

- Exact stage (`partial` or `ground`).
- Canonical input SummaryContext SHA-256, incorporating immutable Evidence
  binding, batch index/count, original event identities and citations.
- Stage/source-count-specific prompt template SHA-256.
- Exact implementation revision.
- Prebound model provider, identifier, revision/digest and validated model
  configuration/options, including adapter version.

The SHA-256 of a canonical JSON key locates a **single immutable pointer**
under `github-daily-summary/resume/`. The pointer binds the normalized
Output SHA-256 and the corresponding Inference Provenance SHA-256. A pointer
is published through the atomic no-overwrite CAS helper from merged #293
**only after** both bound CAS artifacts are persisted and re-read/checked.
A process interrupted before pointer publication leaves no partial *final*
pointer; uncommitted temporary files are not automatically adopted or
deleted. A valid empty Partial output is cacheable.

The public `run_pipeline` entrypoint takes a **nonblocking advisory exclusive
lock on the existing, local `state_root` directory inode** before reading
Evidence, calling a model or creating any Summary CAS entries. Concurrent
pipelines using the same state directory fail before inference. The kernel
releases this lock on process exit or crash, so a later invocation can verify
and reuse the first pipeline's completed pointers. This avoids competing
nondeterministic provenance/pointer writes. Low-level internal stage helpers
do not independently provide the cross-process admission gate; callers must
enter through `run_pipeline`. Trusted local-filesystem inode stability is
required, and old runtimes not participating in this lock must be quiesced
before an upgrade. No lock file is written into the strict CAS namespace.

On a cache hit, all pointer and bound CAS files are read with bounded
no-follow regular-file access, checked against exact expected hashes, and
validated for exact schema and canonical serialization. Provenance must
match stage, Context SHA, Output SHA, prompt SHA/version, implementation
revision and the prebound provider/model/digest/options. For Partial
outputs, claim identities and source evidence are independently regenerated
from immutable originals **within this exact context**. For Grounding,
the record must carry precisely the current singleton claim and supported
or unsupported verdict, with original cited event binding. The producer's
integrity gate remains authoritative; cache entries cannot supply new
evidence/citation authority.

**Missing pointer:** normal cache miss and fresh inference.

**Existing but invalid/corrupt/ambiguous pointer or bound artifact:** fail
closed; never fall back to another cache entry or silent inference.

Changed Evidence, partitioning, model revision, prompt/schema, options or
implementation revision necessarily produces a different key and misses.
Already-persisted historical Partial records lack verified pointers and
therefore are *not* automatically adopted.

## Failure behavior and remaining work

Per-context resume solves repeated computation after a later timeout; it
does **not** make a heavy prompt run faster or prove meaningful coverage.
Issue #290 evaluates batch-dependent omissions and Draft PR #289
experiments with a smaller 24-KiB raw-byte model-facing partition.
Neither is implicitly included in this change. Do not restore recurring
Daily operation or write a date-scoped Daily Note based only on cache tests.

The initial candidate leaves transport retries unchanged: provider timeouts
remain fail closed, and the next invocation recovers only prior verified
contexts. Model error logging must never include source text, prompts,
response bodies, model summaries, tokens/credentials or bearer headers.

## Operational deployment boundary

Private hostnames, network endpoints, SSH-agent locations and deployment
identities belong in separately access-controlled operator runbooks, **not**
in this public repository. Operators must independently verify host-key trust,
the installed checkout revision and drift, exact reviewed target SHA, release
receipts and authorized service identity before using the managed, staged
deployment path.

SSH transport permission is **not** Stage, Activate, Writer or publication
approval. Preserve all previously accepted immutable Evidence and stage refs.
Do not directly edit the live Vault, restart Writer, enable timers, or bypass
Nextcloud CAS/approval gates. Only separately approved, exact-SHA operations
may be performed on the production environment.
