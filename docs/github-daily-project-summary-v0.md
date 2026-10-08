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

The Grounding model returns integer `claim_ref` assessments. Normalization
binds these to the exact claim IDs, rejecting missing or duplicate assessments.

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

Reducer claims are grouped into grounding contexts. Each grounding context
contains:

- the exact structured claims;
- only the raw GitHub events cited by those claims;
- exact reducer-output SHA identities.

Grounding contexts are bounded by both 256 KiB and eight input claims.
Each model call receives an exact JSON Schema enum of the valid per-batch
integer `claim_ref` values (0 through batch size minus one), with exactly
that many assessments required. This reduces copy/range errors but never
weakens the original exact-ID and per-claim support validation.

The model returns one assessment per integer claim_ref; normalized GroundOutput must cover every exact claim_id once:

\`\`\`text
supported
unsupported
\`\`\`

Missing assessments, duplicate assessments, or unknown claim IDs fail closed.

Unsupported claims are deterministically removed from the final grounded summary.
The evaluator does not rewrite them.

Therefore the final artifact contains only claims that survived both provenance
closure and raw-evidence grounding.

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

The prompt digest covers the stage's fixed system prompt and structured-output
schema. The variable user input is bound by the Context SHA.

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
