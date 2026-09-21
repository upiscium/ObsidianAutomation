# Ollama Evaluator Adapter v2

## Purpose

`obsidian-knowledge-evaluate` connects the advisory Evaluator stage to Ollama while preserving the existing authority topology.

Adapter v2 executes the v3 prompt contract as one Groundedness call plus two pairwise calls for every Evaluation Context candidate:

```text
accepted mutation
Generation Record -> exact 05-Context
14-Evaluation-Context
        ↓ binding checks
GET /api/tags -> resolve exact model identifier/digest
        ↓
Groundedness /api/chat
        ↓
for each candidate, in Evaluation Context order:
  Redundancy /api/chat
  Consistency /api/chat
        ↓
all calls strict-parse and bind successfully
        ↓ deterministic severity aggregation
conservative-triad-v0
        ↓
15-Evaluation/<sha>.evaluation.json
```

No partial Evaluation Record is written if any provider call or parser/binding step fails.

## CLI

```text
obsidian-knowledge-evaluate \
  --ai-root <state-root> \
  --proposal-sha256 <proposal-sha> \
  --generation-sha256 <generation-sha> \
  --evaluation-context-sha256 <evaluation-context-sha> \
  --ollama-base-url <https-url> \
  --model <installed-model> \
  --implementation-revision <deployed-commit-sha> \
  [--options-file <json>] \
  [--timeout <seconds>]
```

Production binds `--implementation-revision` to the exact deployed merge commit.

## Binding checks

Before inference, the adapter verifies:

1. Validation accepted the proposal and exact mutation content is available;
2. the Generation Record is bound to the same proposal;
3. the exact `05-Context` bound by the Generation Record hash-validates;
4. `14-Evaluation-Context` is bound to the same proposal and accepted mutation;
5. endpoint, timeout, model options, and implementation revision satisfy existing contracts;
6. generated provider-call order exactly matches Groundedness followed by `(Redundancy, Consistency)` for every candidate in Evaluation Context order.

## Model identity

The adapter resolves the requested model once with `GET /api/tags`.

Every semantic call uses the same resolved identifier and digest. The final Evaluation Record binds that model identity once.

## Provider calls

Each `/api/chat` request uses:

```json
{
  "model": "<resolved model>",
  "messages": [
    {"role": "system", "content": "<dimension-specific system prompt>"},
    {"role": "user", "content": "<deterministic payload>"}
  ],
  "stream": false,
  "think": false,
  "format": "<dimension-specific JSON Schema>",
  "options": {"temperature": 0}
}
```

The model returns:

```json
{
  "assessment": "<dimension-specific enum>",
  "findings": [
    {"detail": "concise observation"}
  ]
}
```

The model never controls dimension, candidate identity, recommendation, model identity, or aggregation policy.

## Evidence isolation

Groundedness receives:

```text
proposal + original generation input
```

Each Redundancy or Consistency call receives:

```text
proposal + exactly one evaluation_candidate
```

Generation input, retrieval scores, and every other candidate are absent from that pairwise call.

This prevents an unrelated candidate from dominating the semantic comparison for a known near-duplicate.

## Candidate provenance

Pairwise model findings contain only a detail string. After strict parsing, deterministic code binds the expected candidate path:

```text
redundancy: [11-Knowledge/example.md] <detail>
consistency: [11-Knowledge/example.md] <detail>
```

The candidate path therefore does not depend on the model reproducing it correctly.

## Deterministic aggregation

Redundancy chooses the strongest assessment:

```text
none < possible < likely
```

Consistency chooses:

```text
pass < unknown < concern
```

Findings are retained only from pairwise results at the winning severity, subject to existing bounds.

If Evaluation Context has zero candidates, aggregation yields:

```text
redundancy=none
consistency=pass
```

## Persistence boundary

All inference results remain in memory until all required calls succeed.

Any transport, response-shape, model-identity, UTF-8, byte-bound, parser, candidate-binding, or aggregation failure prevents `15-Evaluation` persistence.

Only after successful deterministic aggregation is one Evaluation Record built and stored.

## Network boundary

The Evaluator reuses the Generator transport policy:

- remote endpoints require HTTPS;
- HTTP is allowed only for loopback;
- URL credentials are rejected;
- base URL path/query/fragment are rejected;
- environment proxies are not inherited;
- redirects are not followed;
- standard TLS validation remains enabled;
- provider responses are bounded.

## Provenance

`15-Evaluation` binds:

- proposal SHA;
- accepted mutation SHA;
- Generation Record SHA;
- Evaluation Context SHA;
- evaluator implementation revision;
- prompt template version/SHA;
- provider `ollama`;
- resolved model identifier and digest;
- adapter version `ollama-evaluator-chat-structured-v2`;
- strategy `groundedness-plus-pairwise-candidates-v0`;
- the exact immutable recipe `think` value (`false` for new recipes; historical
  `low` remains supported);
- exact inference options;
- aggregated semantic assessment;
- deterministic recommendation.

Raw prompts, raw model responses, and partial pairwise outputs are not persisted.

## Recommendation authority

`conservative-triad-v0` remains unchanged:

```text
proceed
  groundedness=pass AND redundancy=none AND consistency=pass

do_not_proceed
  groundedness=concern OR redundancy=likely OR consistency=concern

manual_review
  otherwise
```

Recommendation remains advisory. Human Review remains authority.

## Production acceptance

Existing:

```text
11-Knowledge/Nextcloud+RemotelySaveでObsidianVaultを共有する方法.md
```

Generated:

```text
11-Knowledge/Nextcloud_RemotelySaveでObsidianVaultを共有する方法.md
```

Expected minimum result:

```text
redundancy = likely
recommendation = do_not_proceed
```

Earlier v1/v2 Evaluation artifacts remain immutable failure-corpus evidence.
