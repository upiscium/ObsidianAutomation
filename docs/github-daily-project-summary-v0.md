# GitHub Daily Project Summary Pipeline v0

Related Epic: #258
Depends on: #259
Implementation: #260

## Purpose

This pipeline converts one immutable GitHub Daily Event Evidence bundle into one
grounded structured Project-progress artifact. It does not render Markdown and
does not write the canonical Vault.

\`\`\`text
GitHub Daily Event Evidence
        |
        v
bounded raw-event contexts
        |
        v
Partial Summarizer
        |
        v
bounded claim contexts
        |
        v
Deterministic Reducer
        |
        v
bounded claim + cited-event contexts
        |
        v
Grounding Evaluator
        |
        v
Grounded Summary
\`\`\`

The pipeline is deliberately separate from Semantic Planner. It does not query a
Semantic Index, retrieve Knowledge, or perform Knowledge redundancy/consistency
evaluation.

## Lossless input partition

The collector in #259 does not impose a Daily-wide event limit. This pipeline
preserves that property.

Raw normalized events are grouped by repository in deterministic
first-appearance order, then greedily partitioned into content-addressed
partial contexts with a default maximum canonical context size of 64 KiB.
Every event appears exactly once. Its original deterministic evidence order
is preserved within its repository; the global cross-repository event order
is intentionally not the partial-context iteration order.

A single event that cannot fit the configured context bound fails closed. It is
never silently dropped.

## Model-facing bounded source references

The Partial model returns per-batch integer `source_refs`, rather than having
to copy 64-character SHA-256 evidence identifiers. Partial source references
select events in an exact repository-scoped context. Deterministic code resolves
them to original evidence IDs and derives the repository from the cited source.
Unknown, duplicate, boolean, or out-of-range source refs fail closed.

For Partial, the model-facing schema is derived for **each batch**: the
`source_refs.items.enum` contains exactly `0..N-1` for that batch's `N`
events (plus the integer min/max); `maxItems` is bounded by both eight and
the batch's available source count. The model-facing user message also
announces that range. The Partial prompt contract is version `v3`. The
original SHA-256 Evidence IDs, repository membership and immutable context
do not change. Neither the model nor the schema can legalize an invented
reference: the runtime resolver still rejects nonexistent, duplicate, boolean
and out-of-batch values without aliasing, clamping or discarding claims.

The enum constrains structured generation on supported providers but is not
a substitute for checking model output after inference. A provider that
ignores some JSON Schema constraints still fails closed at the deterministic
citation and Grounding boundaries.

The Grounding model receives one claim at a time and returns only the
`verdict` and `reason`; it does not produce a claim reference or claim ID.
Deterministic code binds that verdict to the sole immutable input claim's SHA.

### Repository-scoped contexts

Partial contexts are partitioned by repository before byte-bounded splitting.
All original evidence IDs appear exactly once, with order preserved within each
repository. Reducer contexts remain repository-scoped and include exact
source-output SHA bindings. The reducer no longer asks an LLM to select or
rewrite references; original normalized claims pass through deterministically.

## Structured partial claims

The model returns JSON only, citing source_refs. After deterministic resolution, a normalized stored claim has:

\`\`\`text
kind
repository
summary
evidence_ids[]
\`\`\`

Allowed kinds:

\`\`\`text
decision
implementation
bugfix
issue_pr_progress
\`\`\`

A normalized claim must retain 1..8 original evidence IDs. Runtime validation rejects:

- unknown evidence IDs;
- evidence outside the current partial context;
- mixed-repository citations;
- a repository that does not match cited events;
- duplicate evidence IDs;
- multiline or oversized summaries;
- unknown properties.

The model never returns Markdown and never chooses renderer structure.

## Deterministic reducer boundary

All normalized Partial claims pass through the reducer without an LLM call.
Its repository-specific contexts retain exact partial-output provenance,
bounded per-context bytes, up to 8 distinct original evidence IDs and up to
8 input claims. No source claim is dropped or silently rewritten.

For each source claim, the reducer recomputes its claim ID from the normalized
kind, repository, summary, and original evidence IDs, and checks the original
immutable GitHub events for repository/evidence closure. Any mismatch is fatal.
The corresponding reducer ClaimOutput carries the **same claim text, kind,
repository, claim ID, and evidence IDs** as its input. Identical claim IDs
are deduplicated exactly before grounding. Semantic near-duplicate merging
has been disabled pending a separately validated design.

Reducer contexts and ClaimOutputs remain SHA-256 content-addressed; they are
not falsely recorded as model inference. The inference provenance set
therefore contains only actual Partial and Ground model calls. Downstream
GroundedSummary and Daily projection/transport formats are unchanged.

## Grounding boundary

Each original reducer claim is evaluated in its own Grounding context, bounded
by 256 KiB and exactly one claim. The context retains its exact claim SHA,
the cited raw GitHub events, and exact content-addressed reducer-output SHA.
Before dispatch, code verifies that every cited event exists and belongs to
the declared repository.

The model-facing prompt contains only the claim's kind, repository, summary,
and the original cited event content. The model is not asked to repeat
claim_id, evidence_id or claim_ref identifiers. Its structured output is:

```json
{"verdict":"supported","reason":"Cited event directly supports the claim"}
```

The model may instead return `unsupported` with its reason. The normalized
GroundOutput retains an assessment for the **sole exact claim_id obtained from
the input context**, never a model-supplied identity. Unexpected model
properties, invalid verdicts, empty/multiline reasons, non-singleton contexts,
or absent/mismatched cited Evidence fail closed.

Unsupported claims are deterministically excluded from the GroundedSummary;
the model does not rewrite them. Exact Grounding coverage across all claims,
immutable output artifacts, prompt identity and inference provenance remain
required. The resulting Final Summary, Markdown projection and Nextcloud
CAS transport formats are unchanged.

This choice increases model invocations from one per multi-claim batch to one
per claim. It intentionally trades throughput for simpler identity binding
and fewer attribution failures. The semantic verdict remains model-based;
a structurally valid verdict is not a guarantee of perfect factual judgement.


## Evidence closure

Intermediate LLM text is never a provenance root.

\`\`\`text
GitHub Event
  -> evidence_id
  -> partial claim
  -> reducer claim
  -> final claim
\`\`\`

At each transition deterministic code verifies that output evidence IDs are a
subset of the exact previous-stage evidence closure.

Grounding then resolves those IDs back to the original raw GitHub events.

## Content-addressed artifacts

Under the supplied state root:

\`\`\`text
github-daily-summary/
  context/
  output/
  provenance/
  final/
\`\`\`

Contexts, normalized outputs, inference provenance, and the grounded summary are
stored immutably by SHA-256.

The grounded summary is the only artifact intended for the #261 deterministic
Markdown renderer.

## Inference provenance

Every model call stores a separate immutable inference record binding:

- stage: partial / ground (actual inference only);
- exact input Context SHA-256;
- exact normalized output SHA-256;
- implementation revision;
- prompt template version and SHA-256;
- provider;
- model identifier;
- model revision/binding;
- validated model configuration;
- generation timestamp.

The prompt digest covers the stage's fixed system prompt and the **exact
structured-output schema used for that invocation**. For Partial `v3`, its
allowed `source_ref` enum varies by the number of events in the batch, so
its prompt SHA varies deterministically with the batch's source count.
The variable original evidence user input remains bound by the Context SHA.
A stored inference record therefore binds both the actual constrained
schema and the immutable source context; the template version remains stable
across batches of the same implementation.

## Providers

The pipeline supports the same bounded provider families already used by the AI
system:

\`\`\`text
ollama
openai-compatible
\`\`\`

Ollama uses native \`/api/chat\` structured output with \`think=false\`.

OpenAI-compatible providers use strict JSON Schema response format.

Provider options keep the existing validation and provenance rules. Partial generation and Grounding use the configured model. The reducer is a
deterministic evidence-preserving transform, not a model call. A future
model-driven semantic compaction stage would require a separate validation
contract and explicit provenance.

## CLI

\`\`\`bash
obsidian-github-daily-summary-run \
  --provider ollama \
  --evidence /var/lib/obsidian-github-pipeline/daily-evidence/<sha>.github-daily-evidence.json \
  --state-root /var/lib/obsidian-github-pipeline \
  --base-url http://127.0.0.1:11434 \
  --model <model> \
  --implementation-revision <reviewed-commit-sha>
\`\`\`

The command prints the grounded-summary SHA plus partial/reduce/ground batch
counts and accepted/rejected claim counts.

## Empty days

A zero-event Evidence bundle produces a valid empty grounded-summary artifact
without invoking an LLM.

The renderer may therefore produce an empty Project Progress section
deterministically without manufacturing a "no progress" semantic claim.

## Authority

This pipeline may:

- read one exact immutable GitHub evidence artifact;
- call the configured LLM provider;
- write only its local content-addressed summary state.

It does not:

- read the canonical Vault;
- read the Semantic Index;
- write Daily Notes;
- hold a Nextcloud writer credential;
- create Human Review authority.

Issue #261 consumes only the final grounded summary and adds deterministic
Markdown rendering plus the narrowly-scoped Daily CAS writer.
