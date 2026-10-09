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
is published with the existing atomic no-overwrite helper only **after**
both bound CAS artifacts are persisted and re-read/checked. If a process
crashes beforehand, the request safely becomes a miss, rather than adopting
an unindexed artifact or selecting an arbitrary similar output. A valid
empty Partial output is cacheable.

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

## Operational SSH/deployment transport

For future **separately approved** ObsidianAutomation deployments, use
Adam's direct RDC-launched terminal rather than relying on the tmux
SSH environment. The existing SSH agent lives at
`/run/user/1000/ssh-agent`. The verified route is:

```sh
SSH_AUTH_SOCK=/run/user/1000/ssh-agent \
  ssh -o BatchMode=yes -o StrictHostKeyChecking=yes \
  obsidian-automation 'hostname; id -un'
```

On Adam, the `obsidian-automation` alias resolves to `10.12.2.10`;
`obsidian-snapshot-taker` resolves to `10.12.2.11`. Recheck host-key
trust, service identity, current deployment HEAD, drift, test receipts
and exact SHA before using existing pinned/managed production updater
mechanisms. SSH transport permission is **not** publication approval.
Preserve original immutable Evidence and stage refs; do not directly
edit the live Vault, restart Writer, enable timers or bypass the
Nextcloud CAS/approval gates. Use read-only source inspection and
SHA-256-matched transfer for diagnostics.
