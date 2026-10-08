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
Reducer
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

Raw normalized events are greedily partitioned into content-addressed partial
contexts with a default maximum canonical context size of 64 KiB. Every event
appears in exactly one partial context, in original deterministic evidence order.

A single event that cannot fit the configured context bound fails closed. It is
never silently dropped.

## Model-facing bounded source references (v1)

The model-facing protocol uses per-batch integer `source_refs` instead of
requiring the model to copy 64-character SHA-256 evidence identifiers. Partial
source references select events in the exact partial batch. Reducer source
references select input claims in the exact reducer batch and deterministically
inherit their original evidence IDs. Grounding uses a per-batch integer
`claim_ref`. Unknown, duplicate, boolean or out-of-range references fail
closed; no guessing or fuzzy identity repair is performed. All normalized
intermediate and final artifacts still bind the original exact evidence IDs
and claim IDs.

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

## Reducer boundary

All partial claims enter the reducer. Reducer input is itself partitioned into
bounded 64 KiB contexts, so a high-activity day does not move the overflow
problem from raw events into the reduction step.

The reducer may merge duplicate or overlapping claims, but it may cite only
original evidence IDs already present in its input claims. It cannot introduce a
new evidence root.

Each reducer context binds the exact content-addressed partial output artifacts
that supplied its claims.

Reducer outputs remain structured claims and retain original GitHub evidence IDs.

## Grounding boundary

Reducer claims are grouped into grounding contexts. Each grounding context
contains:

- the exact structured claims;
- only the raw GitHub events cited by those claims;
- exact reducer-output SHA identities.

Default grounding context bound is 256 KiB.

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

- stage: partial / reduce / ground;
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

Provider options keep the existing validation and provenance rules. Generator,
Reducer, and Grounding use the same configured model in v0; separate per-role
models can be introduced later without changing artifact semantics.

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
