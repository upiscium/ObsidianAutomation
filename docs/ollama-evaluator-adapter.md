# Ollama Evaluator Adapter v1

## Purpose

`obsidian-knowledge-evaluate` connects the advisory Evaluator stage to Ollama while preserving the existing authority topology.

Evaluator v1 executes the v2 prompt contract as three isolated `/api/chat` calls:

```text
accepted mutation
Generation Record -> exact 05-Context
14-Evaluation-Context
        ↓ binding checks
GET /api/tags -> resolve exact model identifier/digest
        ↓
Groundedness /api/chat
        ↓ strict parse
Redundancy /api/chat
        ↓ strict parse
Consistency /api/chat
        ↓ strict parse
all three succeed
        ↓ deterministic aggregation
conservative-triad-v0
        ↓
15-Evaluation/<sha>.evaluation.json
```

No partial Evaluation Record is written if any pass fails.

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

Before provider inference, the adapter verifies that:

1. Validation accepted the proposal and exact mutation content is available;
2. the Generation Record is bound to the same proposal;
3. the exact `05-Context` bound by the Generation Record hash-validates;
4. `14-Evaluation-Context` is bound to the same proposal and accepted mutation;
5. endpoint, timeout, model options, and implementation revision satisfy existing contracts.

## Model identity

The adapter resolves the requested model once with `GET /api/tags`.

All three semantic passes use the same resolved model identifier and digest. The final Evaluation Record binds that identifier and digest once.

## Three provider calls

Each `/api/chat` request uses:

```json
{
  "model": "<resolved model>",
  "messages": [
    {"role": "system", "content": "<dimension-specific system prompt>"},
    {"role": "user", "content": "<dimension-specific deterministic payload>"}
  ],
  "stream": false,
  "think": false,
  "format": "<dimension-specific JSON Schema>",
  "options": {"temperature": 0}
}
```

The model-facing result is:

```json
{
  "assessment": "<dimension-specific enum>",
  "findings": [
    {"detail": "concise observation"}
  ]
}
```

The dimension is fixed by the pass and cannot be selected by the model.

Provider responses must be complete, identify the resolved model, contain a valid assistant message, satisfy output byte bounds, and pass the deterministic dimension parser.

## Evidence isolation

Groundedness receives:

```text
proposal + original generation input
```

It does not receive Evaluation Context candidates.

Redundancy and consistency receive:

```text
proposal + Evaluation Context candidates
```

They do not receive generation input.

This isolation prevents a model from treating generation-quality or rewrite-quality as evidence against Knowledge redundancy.

## Persistence boundary

Inference results remain in memory until all three passes succeed.

If pass 1, 2, or 3 fails because of transport, response shape, model identity, UTF-8, byte bounds, or strict parser validation, `15-Evaluation` is not written.

After all passes succeed, findings are normalized to:

```text
<dimension>: <detail>
```

and the three assessments are aggregated into the existing Evaluation Record shape.

## Network boundary

The Evaluator reuses the Generator adapter transport policy:

- remote endpoints require HTTPS;
- HTTP is allowed only for loopback;
- URL credentials are rejected;
- base URL path/query/fragment are rejected;
- environment proxies are not inherited;
- HTTP redirects are not followed;
- normal TLS certificate validation remains enabled;
- provider responses are bounded.

## Provenance

`15-Evaluation` binds:

- proposal SHA;
- accepted mutation SHA;
- Generation Record SHA;
- Evaluation Context SHA;
- evaluator implementation revision;
- prompt template version/SHA covering all three passes;
- provider `ollama`;
- resolved model identifier and model digest;
- adapter version `ollama-evaluator-chat-structured-v1`;
- pass order `groundedness`, `redundancy`, `consistency`;
- `think=false`;
- exact inference options;
- aggregated semantic assessment;
- deterministic recommendation.

Raw prompts and raw provider responses are not persisted.

## Recommendation authority

The model cannot output `recommendation`.

`conservative-triad-v0` remains unchanged:

```text
proceed
  groundedness=pass AND redundancy=none AND consistency=pass

do_not_proceed
  groundedness=concern OR redundancy=likely OR consistency=concern

manual_review
  otherwise
```

The recommendation remains advisory. Human Review remains authority.

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

The existing v1 Evaluation artifacts from gemma4:12b, gemma4:26b, and qwen3.6:27b remain immutable failure-corpus evidence.
