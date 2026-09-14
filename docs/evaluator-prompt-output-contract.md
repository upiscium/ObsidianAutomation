# Evaluator Prompt / Output Contract v1

## Purpose

Evaluator v1 is an advisory semantic assessment stage between deterministic Validation and Human Review.

It evaluates an already-validated `create_note` candidate against two distinct evidence sets:

1. the original Reader-produced generation Context, for groundedness;
2. the Reader-produced Evaluation Context candidates, for redundancy and consistency.

The LLM does not own workflow authority.

```text
validated proposal
original 05-Context
14-Evaluation-Context
        ↓
Evaluator prompt
        ↓
LLM semantic assessment
        ↓ strict parser
Deterministic recommendation policy
        ↓
15-Evaluation
        ↓ advisory input
Human Review
```

## Model-owned output

The model returns exactly:

```json
{
  "groundedness": "pass | concern | unknown",
  "redundancy": "none | possible | likely",
  "consistency": "pass | concern | unknown",
  "findings": [
    {
      "dimension": "groundedness | redundancy | consistency",
      "detail": "concise observation"
    }
  ]
}
```

The model does **not** return `recommendation`.

Unknown or duplicate properties are rejected. Each finding is structurally scoped by a `dimension` enum instead of a regex-prefixed free-form string. `detail` contains only the observation. Deterministic code normalizes accepted findings to the existing Evaluation Record representation:

```text
groundedness: <detail>
redundancy: <detail>
consistency: <detail>
```

This keeps downstream Human Review compatibility while avoiding reliance on JSON Schema `pattern`, which is not accepted by some Ollama structured-output implementations.

## Why v1 changes the finding shape

Evaluator v0 required each model-produced finding string to start with one of:

```text
groundedness:
redundancy:
consistency:
```

The strict parser correctly enforced that rule, but the original provider schema could not express it. Adding JSON Schema `pattern` aligned the schema with the parser but caused production Ollama `/api/chat` requests to fail with HTTP 400 on an implementation that does not accept `pattern` in structured-output schemas.

v1 moves the scope marker into a normal enum field. This preserves fail-closed parsing without depending on regex schema support.

## Groundedness scope

Groundedness compares the proposal with the exact generation input: the original query plus the exact source bytes contained in the Reader-produced `05-Context` artifact.

```text
pass
  Material factual/procedural claims are supported by supplied generation input.

concern
  At least one material claim is unsupported by or materially conflicts with supplied generation input.

unknown
  Supplied generation input is insufficient for a defensible judgment.
```

`pass` is not an objective-truth guarantee. It means only that the proposal is adequately grounded in the evidence that was supplied to the Generator.

## Redundancy scope

Redundancy compares the proposal with the bounded recall-oriented candidates in `14-Evaluation-Context`.

```text
likely
  A candidate covers substantially the same core knowledge/procedure/conclusions
  and the proposal adds little meaningful unique information.

possible
  There is substantial overlap, but meaningful differentiation or additional
  information may remain.

none
  Supplied candidates are materially distinct or do not support a redundancy
  concern.
```

Filename punctuation, wording changes, reordered sections, or stylistic rewrites are explicitly not sufficient to make two notes semantically distinct.

The candidate set is not exhaustive, so `none` does not prove global non-duplication.

## Consistency scope

Consistency compares the proposal with the supplied Evaluation Context candidates for explicit material conflicts.

```text
concern
  A material factual or procedural claim is incompatible with a supplied candidate.

pass
  No material conflict is present among supplied candidates.

unknown
  Evidence is ambiguous or insufficient for a defensible judgment.
```

Different scope, omission, or extra detail alone does not constitute contradiction.

## Deterministic recommendation policy

Version:

```text
conservative-triad-v0
```

Recommendation is calculated by deterministic code after strict output parsing.

```text
proceed
  groundedness = pass
  redundancy   = none
  consistency  = pass


do_not_proceed
  groundedness = concern
  OR redundancy = likely
  OR consistency = concern

manual_review
  every other combination, including unknown and possible
```

The v1 output-shape change does not alter this recommendation policy.

This recommendation is still advisory. It is not Validation, Human approval, or execution authority.

## Prompt contract

Template version:

```text
knowledge-note-evaluator-v1
```

Output contract version:

```text
knowledge-note-evaluator-output-v1
```

The prompt-template SHA binds:

- prompt template version;
- evaluator output contract version;
- deterministic recommendation policy version;
- fixed system prompt;
- output JSON Schema;
- user payload format version.

The user payload separates:

```text
proposal

generation_input
  query
  exact generation source path/hash/content

evaluation_candidates
  exact candidate path/hash/content
```

BM25 scores are intentionally not sent to the LLM. Retrieval score is a candidate-selection mechanism, not semantic evidence and should not bias the model's final assessment.

## Structured-output compatibility boundary

The provider-facing schema intentionally uses basic object, array, enum, string, length, required, and `additionalProperties` constraints.

It intentionally does not depend on regex `pattern` for finding scope. The deterministic parser still revalidates every provider response and rejects malformed finding objects, invalid dimensions, untrimmed or oversized detail strings, duplicate findings, unknown properties, and model-controlled recommendations.

Provider structured-output enforcement is therefore defense in depth. It is not trusted as the sole parser or workflow authority.

## Prompt injection boundary

Proposal text, generation sources, and candidate Knowledge Note text are all treated as untrusted data.

The system prompt explicitly forbids following commands, role changes, policies, or output-format requests found in those fields.

This is defense in depth, not a security proof. Evaluator output remains advisory and passes through a strict deterministic parser before an Evaluation Record can be built.

## Authority

This contract does not change the authority topology introduced by Evaluator Architecture v0.

Evaluator remains unable to:

- read canonical Vault directly;
- read Reader-private `04-Index`;
- write `12-Evaluation-Request`;
- write `14-Evaluation-Context`;
- write Human Review, Execution, Transport, or Receipts;
- hold the Nextcloud writer credential.

## Production acceptance

The production near-duplicate case remains:

```text
existing:
11-Knowledge/Nextcloud+RemotelySaveでObsidianVaultを共有する方法.md

generated:
11-Knowledge/Nextcloud_RemotelySaveでObsidianVaultを共有する方法.md
```

Expected minimum result:

```text
redundancy = likely
recommendation = do_not_proceed
```

The exact deployed implementation revision, prompt template SHA, Ollama model identifier, and model digest must be bound into the persisted Evaluation Record.

## Out of scope

- automatic retries;
- cloud model providers;
- automatic rejection or approval based on recommendation;
- semantic/vector candidate retrieval;
- objective factual verification beyond supplied evidence.
