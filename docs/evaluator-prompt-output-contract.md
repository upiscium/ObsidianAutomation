# Evaluator Prompt / Output Contract v0

## Purpose

Evaluator v0 is an advisory semantic assessment stage between deterministic Validation and Human Review.

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
  "findings": []
}
```

The model does **not** return `recommendation`.

Unknown or duplicate properties are rejected. Findings are bounded and must start with one of:

```text
groundedness:
redundancy:
consistency:
```

This keeps findings scoped to the three declared assessment dimensions rather than allowing free-form workflow instructions.

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

This recommendation is still advisory. It is not Validation, Human approval, or execution authority.

## Prompt contract

Template version:

```text
knowledge-note-evaluator-v0
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

## Out of scope

- Ollama/provider transport;
- model selection;
- automatic retries;
- automatic rejection or approval based on recommendation;
- semantic/vector candidate retrieval;
- objective factual verification beyond supplied evidence.

## Next step

Implement a thin Ollama Evaluator adapter that loads exact bound artifacts, renders this prompt, requests structured output, strictly parses it, applies `conservative-triad-v0`, and persists the resulting `15-Evaluation` record.
