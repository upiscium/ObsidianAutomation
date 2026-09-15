# Evaluator Prompt / Output Contract v3

## Purpose

Evaluator v3 is an advisory semantic assessment stage between deterministic Validation and Human Review.

Production testing showed two independent interference modes:

1. v1: groundedness evidence and duplicate candidates in one prompt caused cross-dimension task interference;
2. v2: even after dimension separation, multiple Evaluation Context candidates in one Redundancy/Consistency pass caused candidate interference. An unrelated MARL/LLM research note dominated the model output while a known Nextcloud near-duplicate was missed.

v3 keeps Groundedness isolated and evaluates every Evaluation Context candidate pairwise for Redundancy and Consistency.

```text
validated proposal
        │
        ├─ Groundedness
        │    proposal + original 05-Context
        │
        └─ for each Evaluation Context candidate
             ├─ Redundancy
             │    proposal + exactly one candidate
             └─ Consistency
                  proposal + exactly one candidate

all provider calls strict-parse successfully
        ↓
deterministic per-dimension severity aggregation
        ↓
conservative-triad-v0
        ↓
15-Evaluation
        ↓ advisory input
Human Review
```

The LLM does not own workflow authority.

## Model-facing output

The model-facing output shape is unchanged from v2:

```json
{
  "assessment": "<dimension-specific enum>",
  "findings": [
    {"detail": "concise observation"}
  ]
}
```

Therefore the output contract version remains:

```text
knowledge-note-evaluator-output-v2
```

The prompt/input contract changes to:

```text
knowledge-note-evaluator-v3
```

The dimension and candidate identity are fixed outside the model. The model cannot return `recommendation`.

## Groundedness pass

Input:

```text
proposal
generation_input
  query
  exact source path/hash/content
```

Evaluation candidates are absent.

Assessment values:

```text
pass
concern
unknown
```

Groundedness asks only whether material proposal claims are supported by the exact evidence supplied to the Generator.

## Pairwise Redundancy

One provider call is made for each candidate.

Input:

```text
proposal
evaluation_candidate
  exact candidate path/hash/content
```

Only one candidate is present. Generation input, retrieval scores, and all other candidates are absent.

Assessment values:

```text
none
possible
likely
```

Filename punctuation, wording, section order, formatting, readability improvements, and stylistic rewrites do not make two notes semantically distinct.

## Pairwise Consistency

One provider call is made for each candidate using the same pairwise evidence shape.

Assessment values:

```text
pass
unknown
concern
```

Consistency asks only whether material factual or procedural claims explicitly conflict with that one candidate. Different scope, omission, formatting, or extra detail alone is not a contradiction.

## Deterministic candidate binding

Candidate paths are not trusted to the model output. After strict parsing, deterministic code binds the known candidate path to each accepted pairwise finding:

```text
redundancy: [11-Knowledge/example.md] <detail>
consistency: [11-Knowledge/example.md] <detail>
```

This preserves provenance even if the model omits or mistypes a path.

## Deterministic aggregation

Redundancy severity:

```text
none < possible < likely
```

Consistency severity:

```text
pass < unknown < concern
```

The strongest assessment across all candidates becomes the final dimension assessment. Findings are taken only from pairwise results at the winning severity and are bounded deterministically.

If Evaluation Context contains zero candidates:

```text
redundancy = none
consistency = pass
```

Groundedness remains independent.

## Fail-closed behavior

All provider calls use one resolved model identifier and digest.

No Evaluation Record is persisted until every required call has:

1. completed successfully;
2. returned the resolved model;
3. satisfied byte bounds;
4. passed strict deterministic parsing;
5. been bound to the expected candidate path and dimension.

Any provider/parser/binding failure aborts the whole Evaluation without writing a partial `15-Evaluation` artifact.

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

Recommendation remains advisory and is not Human approval or execution authority.

## Prompt-template provenance

The prompt-template SHA binds:

- `knowledge-note-evaluator-v3`;
- output contract `knowledge-note-evaluator-output-v2`;
- pairwise strategy identifier;
- all dimension-specific system prompts;
- all JSON Schemas;
- payload version 3;
- deterministic severity order;
- finding aggregation policy;
- `conservative-triad-v0`.

## Structured-output compatibility

Schemas use basic object, array, enum, string, length, required, and `additionalProperties` constraints. JSON Schema `pattern` is intentionally not used because the production Ollama implementation rejected it.

## Prompt injection boundary

Proposal text, generation sources, and candidate Knowledge Note text are untrusted data. System prompts explicitly forbid following commands, role changes, policies, or output-format requests found inside them.

Pairwise isolation also reduces the blast radius of a malicious or instruction-like candidate: it cannot share one model context with other candidates.

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

Existing:

```text
11-Knowledge/Nextcloud+RemotelySaveでObsidianVaultを共有する方法.md
```

Generated proposal:

```text
11-Knowledge/Nextcloud_RemotelySaveでObsidianVaultを共有する方法.md
```

Expected minimum result:

```text
redundancy = likely
recommendation = do_not_proceed
```

All earlier v1/v2 Evaluation artifacts remain immutable failure-corpus records.
