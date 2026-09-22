# Evaluator Prompt / Output Contract (output v5, prompt v6)

## Purpose

The Evaluator is an advisory semantic assessment stage between deterministic Validation and Human Review. Its current model-facing output contract is v5 and its current prompt/input template is v6.

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
              └─ Consistency proposer
                   deterministic proposal/candidate excerpt tables
                   ↓ zero or more excerpt-ID pairs
                 deterministic excerpt binding
                   ↓ exact proposal quote + exact candidate quote
                 Consistency verifier (one call per bound pair)

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

For a Consistency pass, the model always returns a `conflicts` array. A concern must contain at least one conflict; a pass or unknown result must return an empty array:

```json
{
  "assessment": "concern",
  "findings": [
    {"detail": "concise observation"}
  ],
  "conflicts": [
    {
      "proposal_excerpt_id": "p0001",
      "candidate_excerpt_id": "c0001",
      "incompatibility": "why the claims cannot both apply"
    }
  ]
}
```

The current output contract version is:

```text
knowledge-note-evaluator-output-v5
```

The current prompt/input contract is:

```text
knowledge-note-evaluator-v6
```

The current prompt-template SHA-256 is:

```text
45439ec5f3ae0d9dd31fa5af37c45c572b3e520ac87548f0a739acf1ee5f9041
```

Historical readable prompt identities remain exact version/hash pairs:

```text
knowledge-note-evaluator-v5
ca9755c7b448be9bb2a42ab41ba182deb7b45785a4099d6ac85d854131a06291

knowledge-note-evaluator-v4
9411d74c10cd8c3450be6b79f12c644433862a4b292a26db7444d32606ddea3b

knowledge-note-evaluator-v3
bf6265294a4b346f12d1951f594760c80221380ccee9993c6ab866b6b1eca937
```

Recipe parsing accepts the historical v3, v4, and v5 pairs for readability and audit, but current runtime preflight requires the v6 pair and blocks historical recipes before provider contact. Unknown prompt identities and cross-paired version/hash values are rejected. The dimension, candidate identity, and recommendation are fixed outside the model; the model cannot return `candidate_path` or `recommendation`.

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

## Pairwise Consistency proposer and verifier

The first Consistency call is a proposer pass for each candidate. Deterministic
code first partitions the exact proposal and candidate bytes into bounded excerpt
tables with stable IDs such as `p0001` and `c0001`. The model may return zero
or more plausible conflicts by selecting one proposal excerpt ID and one candidate
excerpt ID plus an incompatibility description. It never reproduces quote text.

Deterministic code resolves the selected IDs back to the exact excerpt bytes.
Unknown or malformed IDs fail closed before any verifier call. Only these resolved
exact excerpts are passed to the verifier.

One verifier call is then made for each accepted proposal.

Assessment values:

```text
pass
unknown
concern
```

The proposer asks only whether material factual or procedural claims explicitly
conflict with that one candidate. The verifier receives the two exact quotes and
the proposed incompatibility, and returns only a verdict:

```json
{
  "verdict": "contradiction | compatible | unknown",
  "explanation": "bounded explanation"
}
```

`contradiction` means the anchored claims cannot both be true or followed in the
same relevant context. `compatible` includes different topics, scopes, papers,
frameworks, environments, or complementary details. `unknown` is used when the
anchored excerpts are insufficient or ambiguous.

Only a verifier `contradiction` becomes persisted conflict evidence.

`concern` is reserved for an explicit material incompatibility that cannot both be true or followed in the same relevant context. Its structured evidence is bounded and has this model-facing shape:

```json
{
  "assessment": "concern",
  "findings": [],
  "conflicts": [
    {
      "proposal_excerpt_id": "p0001",
      "candidate_excerpt_id": "c0001",
      "incompatibility": "..."
    }
  ]
}
```

`proposal_excerpt_id` and `candidate_excerpt_id` are fixed-shape identifiers selected from the deterministic tables supplied in that exact pass. `incompatibility` is a non-empty, trimmed, single-line string of at most 1,000 characters. Excerpt text itself is not model output. After deterministic ID resolution, exact excerpt bytes are carried into verification and persisted without model-side rewriting. There may be at most four proposals, with no duplicate evidence triples. `pass` and `unknown` have no proposals: the model returns `"conflicts": []`, while the parser rejects a non-empty array on either assessment. `concern` with an empty array is also rejected.

The following are not conflicts by themselves: different topic or scope, a missing framework or detail, omissions, extra detail, formatting, and style. Those differences can coexist; a conflict requires the explicit material incompatibility above.

## Deterministic candidate binding

Candidate paths are not trusted to the model output. The model-facing proposal
and verifier objects contain no path. After strict parsing, deterministic code
binds the known candidate path from the prompt to each accepted pairwise finding
and each verified conflict:

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

The strongest assessment across all candidates becomes the final dimension assessment. Findings are taken only from pairwise results at the winning severity, deduplicated, and bounded deterministically. Consistency conflicts are created only from verifier `contradiction` results, then taken only from the winning consistency severity, deduplicated by their three quote/evidence fields, and bounded to four. A verifier `compatible` removes the proposal; an `unknown` verifier produces `unknown` unless another proposal is contradictory.

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

For Consistency, every proposed pair must resolve to excerpt IDs present in the
exact deterministic tables for that pass, and every verifier response must pass
the strict verdict schema. A proposer concern
is never persisted directly.

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

For current v2 records, deterministically resolved and verifier-confirmed proposal
and candidate excerpts are persisted in the existing `proposal_claim` and
`candidate_claim` fields so historical readers remain compatible. Those persisted quote fields preserve
embedded LF line breaks exactly; `incompatibility` remains single-line.

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

- `knowledge-note-evaluator-v5`;
- output contract `knowledge-note-evaluator-output-v4`;
- pairwise strategy identifier;
- all dimension-specific system prompts;
- all JSON Schemas;
- payload version 6;
- the Consistency proposer/verifier pass order and verdict aggregation;
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
