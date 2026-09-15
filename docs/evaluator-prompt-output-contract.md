# Evaluator Prompt / Output Contract v2

## Purpose

Evaluator v2 is an advisory semantic assessment stage between deterministic Validation and Human Review.

Production acceptance with `gemma4:12b`, `gemma4:26b`, and `qwen3.6:27b` showed the same failure mode: when groundedness evidence and duplicate candidates were presented in one prompt, all three models classified a known near-duplicate as `redundancy=none`. The models repeatedly interpreted formatting/readability improvements as evidence against redundancy.

v2 removes that task interference by evaluating each semantic dimension in a separate provider call.

```text
validated proposal
        │
        ├─ Groundedness pass
        │    proposal + original 05-Context only
        │
        ├─ Redundancy pass
        │    proposal + 14-Evaluation-Context candidates only
        │
        └─ Consistency pass
             proposal + 14-Evaluation-Context candidates only

all three strict parses succeed
        ↓
deterministic aggregation
        ↓
conservative-triad-v0
        ↓
15-Evaluation
        ↓ advisory input
Human Review
```

The LLM does not own workflow authority.

## Model-facing output

Each pass returns only one assessment and findings for that pass:

```json
{
  "assessment": "<dimension-specific enum>",
  "findings": [
    {"detail": "concise observation"}
  ]
}
```

The dimension is fixed by the pass and is not model-controlled.

The model cannot return `recommendation`.

Accepted findings are normalized deterministically into the existing Evaluation Record representation:

```text
groundedness: <detail>
redundancy: <detail>
consistency: <detail>
```

The persisted `15-Evaluation` record shape therefore remains unchanged.

## Pass 1: Groundedness

Input:

```text
proposal
generation_input
  query
  exact generation source path/hash/content
```

`evaluation_candidates` are intentionally absent.

Assessment values:

```text
pass
concern
unknown
```

Groundedness asks only whether material factual/procedural claims are supported by the exact evidence supplied to the Generator. It is not objective-truth verification.

## Pass 2: Redundancy

Input:

```text
proposal
evaluation_candidates
  exact candidate path/hash/content
```

`generation_input` is intentionally absent.

Assessment values:

```text
none
possible
likely
```

`likely` means that one or more candidates cover substantially the same core knowledge, procedure, or conclusions and the proposal adds little meaningful unique information.

Filename punctuation, wording, section order, formatting, readability improvements, and stylistic rewrites do not make two notes semantically distinct.

BM25 scores are not sent to the model.

## Pass 3: Consistency

Input:

```text
proposal
evaluation_candidates
  exact candidate path/hash/content
```

`generation_input` is intentionally absent.

Assessment values:

```text
pass
concern
unknown
```

Consistency asks only whether material factual or procedural claims explicitly conflict with supplied candidates. Different scope, omission, formatting, or extra detail alone is not a contradiction.

## Fail-closed aggregation

The three provider calls use the same resolved model identifier and model digest.

No Evaluation Record is persisted until all three passes have:

1. completed successfully;
2. returned the resolved model;
3. passed byte bounds;
4. passed strict deterministic parsing.

If any pass fails, no partial `15-Evaluation` artifact is written.

Only after all three passes succeed are their assessments aggregated into one internal Evaluator output.

## Deterministic recommendation policy

Version remains:

```text
conservative-triad-v0
```

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
  every other combination
```

Recommendation remains advisory. It is not Validation, Human approval, or execution authority.

## Contract versions

```text
prompt template:
knowledge-note-evaluator-v2

model output contract:
knowledge-note-evaluator-output-v2
```

The prompt-template SHA binds all three fixed system prompts, each dimension-specific JSON Schema, pass order, payload format version, output contract version, and recommendation policy version.

## Structured-output compatibility

Each pass schema uses only basic object, array, enum, string, length, required, and `additionalProperties` constraints. It does not use JSON Schema `pattern`.

Finding scope is deterministic because each provider call is already bound to exactly one dimension.

## Prompt injection boundary

Proposal text, generation sources, and candidate Knowledge Note text are untrusted data. Each system prompt forbids following commands, role changes, policies, or output-format requests found inside those fields.

This is defense in depth, not a security proof.

## Authority

Evaluator remains unable to:

- read canonical Vault directly;
- read Reader-private `04-Index`;
- write `12-Evaluation-Request`;
- write `14-Evaluation-Context`;
- write Human Review, Execution, Transport, or Receipts;
- hold the Nextcloud writer credential.

Human Review remains the authority after Evaluation.

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

The existing v1 failure artifacts are retained as immutable failure-corpus records.
