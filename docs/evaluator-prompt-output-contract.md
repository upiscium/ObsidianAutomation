# Evaluator Prompt / Output Contract (output v3, prompt v4)

## Purpose

The Evaluator is an advisory semantic assessment stage between deterministic Validation and Human Review. Its current model-facing output contract is v3 and its current prompt/input template is v4.

Production testing showed two independent interference modes:

1. v1: groundedness evidence and duplicate candidates in one prompt caused cross-dimension task interference;
2. v2: even after dimension separation, multiple Evaluation Context candidates in one Redundancy/Consistency pass caused candidate interference. An unrelated MARL/LLM research note dominated the model output while a known Nextcloud near-duplicate was missed.

The current prompt keeps Groundedness isolated and evaluates every Evaluation Context candidate pairwise for Redundancy and Consistency. The pairwise isolation preserves the lesson from the historical v1/v2 failure corpus without giving the model workflow authority.

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

The current model-facing output contract is:

```json
{
  "assessment": "<dimension-specific enum>",
  "findings": [
    {"detail": "concise observation"}
  ]
}
```

For a Consistency pass, the model may also return structured conflict evidence. A concern must contain at least one conflict; a pass or unknown result must not contain conflicts:

```json
{
  "assessment": "concern",
  "findings": [
    {"detail": "concise observation"}
  ],
  "conflicts": [
    {
      "proposal_claim": "claim made by the proposal",
      "candidate_claim": "claim made by the candidate",
      "incompatibility": "why the claims cannot both apply"
    }
  ]
}
```

The current output contract version is:

```text
knowledge-note-evaluator-output-v3
```

The current prompt/input contract is:

```text
knowledge-note-evaluator-v4
```

The current prompt-template SHA-256 is:

```text
64be14bb5d17e351d9fb694dd17f9764a8a2e0daefa1f44946a5349ecec2aebd
```

The historical, readable prompt identity is an exact version/hash pair:

```text
knowledge-note-evaluator-v3
bf6265294a4b346f12d1951f594760c80221380ccee9993c6ab866b6b1eca937
```

Recipe parsing accepts that historical pair for readability and audit, but current runtime preflight requires the v4 pair and blocks a historical recipe before provider contact. Unknown prompt identities and cross-paired version/hash values are rejected. The dimension, candidate identity, and recommendation are fixed outside the model; the model cannot return `candidate_path` or `recommendation`.

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

Consistency asks only whether material factual or procedural claims explicitly conflict with that one candidate.

`concern` is reserved for an explicit material incompatibility that cannot both be true or followed in the same relevant context. Its structured evidence is bounded and has this model-facing shape:

```json
{
  "assessment": "concern",
  "findings": [],
  "conflicts": [
    {
      "proposal_claim": "...",
      "candidate_claim": "...",
      "incompatibility": "..."
    }
  ]
}
```

Each of `proposal_claim`, `candidate_claim`, and `incompatibility` is a non-empty, trimmed string of at most 1,000 characters. There may be at most four conflicts, with no duplicate evidence triples. `pass` and `unknown` have no conflicts: the model omits `conflicts` and the parser rejects conflicts on either assessment. `unknown` remains the result when the supplied pair is insufficient or ambiguous to judge.

The following are not conflicts by themselves: different topic or scope, a missing framework or detail, omissions, extra detail, formatting, and style. Those differences can coexist; a conflict requires the explicit material incompatibility above.

## Deterministic candidate binding

Candidate paths are not trusted to the model output. The model-facing conflict objects contain no path. After strict parsing, deterministic code binds the known candidate path from the prompt to each accepted pairwise finding and each accepted conflict:

```text
redundancy: [11-Knowledge/example.md] <detail>
consistency: [11-Knowledge/example.md] <detail>
conflict: [11-Knowledge/example.md]
```

The bound path must be the exact safe `11-Knowledge/...md` candidate supplied to that pass. Pairwise dimension/candidate order and the matching candidate sets for Redundancy and Consistency are checked outside the model. This preserves provenance and rejects unknown or cross-paired candidate identities even if the model omits or mistypes a path.

## Deterministic aggregation

Redundancy severity:

```text
none < possible < likely
```

Consistency severity:

```text
pass < unknown < concern
```

The strongest assessment across all candidates becomes the final dimension assessment. Findings are taken only from pairwise results at the winning severity, deduplicated, and bounded deterministically. Consistency conflicts are likewise taken only from the winning consistency severity, deduplicated by their three claim/evidence fields, and bounded to four.

If Evaluation Context contains zero candidates:

```text
redundancy = none
consistency = pass
```

Groundedness remains independent.

Model output is limited to 32 KiB; each dimension returns at most four findings and each finding detail is at most 1,000 characters. Normalized findings are at most 2,048 characters each, and the persisted assessment contains at most 16 findings. These bounds apply before durable Evaluation Record adoption.

## Fail-closed behavior

All provider calls use one resolved model identifier and digest.

No Evaluation Record is persisted until every required call has:

1. completed successfully;
2. returned the resolved model;
3. satisfied byte bounds;
4. passed strict deterministic parsing;
5. been bound to the expected candidate path and dimension.

Any provider/parser/binding failure aborts the whole Evaluation without writing a partial `15-Evaluation` artifact.

## Evaluation Record compatibility

Current evaluations persist Evaluation Record v2. Its structured conflict evidence is stored under `assessment.conflicts`, with the deterministic candidate path added to every item:

```json
{
  "record_version": 2,
  "assessment": {
    "groundedness": "pass",
    "redundancy": "none",
    "consistency": "concern",
    "recommendation": "do_not_proceed",
    "findings": ["consistency: [11-Knowledge/example.md] ..."],
    "conflicts": [
      {
        "candidate_path": "11-Knowledge/example.md",
        "proposal_claim": "...",
        "candidate_claim": "...",
        "incompatibility": "..."
      }
    ]
  }
}
```

For v2 `pass` or `unknown`, `assessment.conflicts` is the empty array. Historical Evaluation Record v1 artifacts remain readable as immutable evidence; their assessment shape has no `conflicts` member and they are not silently rewritten as current v2 records.

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

The current prompt-template SHA binds:

- `knowledge-note-evaluator-v4`;
- output contract `knowledge-note-evaluator-output-v3`;
- pairwise strategy identifier;
- all dimension-specific system prompts;
- all JSON Schemas;
- payload version 3;
- deterministic severity order;
- bounded finding and conflict aggregation policy;
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
