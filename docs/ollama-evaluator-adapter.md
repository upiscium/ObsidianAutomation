# Ollama Evaluator Adapter v0

## Purpose

`obsidian-knowledge-evaluate` connects the advisory Evaluator stage to an Ollama endpoint without changing the authority topology introduced by Evaluator Architecture v0.

```text
10-Validation accepted mutation
00-Untrusted Generation Record -> exact 05-Context
14-Evaluation-Context
        ↓ binding checks
Evaluator Prompt / Output Contract v0
        ↓
Ollama /api/chat structured output
        ↓ strict parser
conservative-triad-v0 recommendation
        ↓
15-Evaluation/<sha>.evaluation.json
```

The adapter never grants approval or execution authority. `15-Evaluation` remains an advisory machine assessment consumed by Human Review.

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

Production must bind `--implementation-revision` to the exact deployed merge commit, not a feature-branch head.

## Pre-inference binding checks

Before the provider is contacted, the adapter verifies:

1. the proposal has accepted Validation and loads the accepted mutation exact content;
2. the Generation Record is bound to the same proposal;
3. the original `05-Context` identified by the Generation Record exists and hash-validates;
4. the `14-Evaluation-Context` is bound to the same proposal and accepted mutation;
5. implementation revision, endpoint, timeout, and inference options satisfy their existing contracts.

The prompt therefore evaluates the accepted mutation content, not arbitrary unvalidated proposal bytes.

## Ollama request

The adapter resolves the installed model with `GET /api/tags` and binds the returned model digest into the Evaluation Record.

`POST /api/chat` uses:

```json
{
  "model": "<resolved canonical model>",
  "messages": [
    {"role": "system", "content": "<Evaluator Prompt Contract system>"},
    {"role": "user", "content": "<deterministic evaluator payload>"}
  ],
  "stream": false,
  "think": false,
  "format": "<Evaluator Output JSON Schema>",
  "options": {"temperature": 0}
}
```

The provider response must be complete, must identify the resolved model, must contain an assistant message, and must pass `parse_evaluator_output()` after UTF-8 and byte-size checks.

## Network boundary

The Evaluator reuses the Generator adapter's existing Ollama transport policy:

- remote endpoint requires HTTPS;
- HTTP is allowed only for loopback;
- embedded URL credentials are rejected;
- base URL path/query/fragment are rejected;
- environment proxies are not inherited;
- HTTP redirects are not followed;
- standard Python TLS certificate verification remains enabled;
- provider responses are size-bounded.

This is important because both original Generation Context and candidate Knowledge bytes are sent to the configured Ollama endpoint.

## Provenance

`15-Evaluation` binds:

- proposal SHA;
- accepted mutation SHA;
- Generation Record SHA;
- Evaluation Context SHA;
- evaluator implementation revision;
- evaluator prompt template version/SHA;
- provider `ollama`;
- resolved model identifier;
- installed model digest from `/api/tags`;
- adapter version `ollama-evaluator-chat-structured-v0`;
- `think=false`;
- exact inference options;
- semantic assessment;
- deterministic recommendation.

Raw prompts and raw provider responses are not persisted.

## Recommendation authority

The model cannot output `recommendation`.

`conservative-triad-v0` derives it deterministically after strict parsing:

```text
proceed
  groundedness=pass AND redundancy=none AND consistency=pass

do_not_proceed
  groundedness=concern OR redundancy=likely OR consistency=concern

manual_review
  otherwise
```

This recommendation remains advisory. It is not Validation, Human approval, or an Executor gate.

## Production acceptance case

The first production acceptance case is the observed near-duplicate:

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

The groundedness and consistency dimensions are inspected independently and are not hard-coded for this case.

## Out of scope

- automatic retry;
- cloud providers;
- provider authentication headers/API keys;
- semantic/vector candidate retrieval;
- automatic Human approval or rejection;
- Executor enforcement based on Evaluation;
- update/merge/delete/rename mutations.
