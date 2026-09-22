# Ollama Evaluator Adapter v3

## Purpose

`obsidian-knowledge-evaluate` connects the advisory Evaluator stage to Ollama while preserving the existing authority topology.

Adapter v3 executes the current v5 prompt contract and v4 output contract as one Groundedness call plus Redundancy and Consistency proposer calls for every Evaluation Context candidate. Each bounded Consistency proposal then receives one verifier call:

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
  Consistency proposer /api/chat
    ↓ one call per exact quote pair
  Consistency verifier /api/chat
        ↓
all calls strict-parse and bind successfully
        ↓ deterministic severity aggregation
conservative-triad-v0
        ↓
15-Evaluation/<sha>.evaluation.json
```

No partial Evaluation Record is written if any provider call or parser/binding step fails.

The current prompt identity is:

```text
knowledge-note-evaluator-v5
ca9755c7b448be9bb2a42ab41ba182deb7b45785a4099d6ac85d854131a06291
```

The historical identities `knowledge-note-evaluator-v3` /
`bf6265294a4b346f12d1951f594760c80221380ccee9993c6ab866b6b1eca937` and
`knowledge-note-evaluator-v4` /
`9411d74c10cd8c3450be6b79f12c644433862a4b292a26db7444d32606ddea3b` remain
readable in recipes for audit. Current runtime preflight blocks historical
recipes before provider contact; unknown and cross-paired prompt version/hash
identities are rejected.

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
6. generated provider-call order exactly matches Groundedness followed by `(Redundancy, Consistency proposer, Consistency verifier*)` for every candidate in Evaluation Context order.

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

The Consistency proposer response additionally uses the v4 structured-conflict shape when
the assessment is `concern`:

```json
{
  "assessment": "concern",
  "findings": [{"detail": "concise observation"}],
  "conflicts": [
    {
      "proposal_quote": "...",
      "candidate_quote": "...",
      "incompatibility": "..."
    }
  ]
}
```

Each quote/evidence field is bounded to 1,000 characters and at most four
proposals are accepted. Each proposal is exact-quote checked before its verifier
call. A verifier returns `contradiction`, `compatible`, or `unknown`; only
`contradiction` becomes persisted conflict evidence. Different topic or scope,
missing framework/details, omissions, extra detail, formatting, and style are
not conflicts.

The verifier receives only the two anchored quotes and the proposed
incompatibility; it does not receive or control candidate identity or path. The
model never controls dimension, candidate identity, candidate path,
recommendation, model identity, or aggregation policy. The current output
 contract is `knowledge-note-evaluator-output-v4`.

## Evidence isolation

Groundedness receives:

```text
proposal + original generation input
```

Each Redundancy or Consistency proposer call receives:

```text
proposal + exactly one evaluation_candidate
```

Generation input, retrieval scores, and every other candidate are absent from that pairwise call.

This prevents an unrelated candidate from dominating the semantic comparison for a known near-duplicate.

## Candidate provenance

Pairwise model findings contain only a detail string, and model proposals and
verifier outputs do not contain a candidate path. After strict parsing,
deterministic code binds the expected candidate path to every finding and
verified conflict:

```text
redundancy: [11-Knowledge/example.md] <detail>
consistency: [11-Knowledge/example.md] <detail>
conflict: [11-Knowledge/example.md]
```

The candidate path therefore does not depend on the model reproducing it
correctly. The adapter rejects a dimension/candidate mismatch and the two
pairwise dimensions must cover the same candidate paths in Evaluation Context
order.

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

Winning-severity aggregation is deterministic:

```text
redundancy:  none < possible < likely
consistency: pass < unknown < concern
```

Only findings and Consistency conflicts from the winning severity are retained.
Compatible proposals are removed, unknown proposals produce `unknown` unless a
contradiction exists, and contradiction dominates.
Duplicate findings and duplicate conflict evidence are removed; findings are
bounded to four per dimension (16 in the persisted assessment), and conflicts
to four. Zero candidates produce `redundancy=none` and `consistency=pass`.

If Evaluation Context has zero candidates, aggregation yields:

```text
redundancy=none
consistency=pass
```

## Persistence boundary

All inference results remain in memory until all required calls succeed.

Any transport, response-shape, model-identity, UTF-8, byte-bound, parser, candidate-binding, or aggregation failure prevents `15-Evaluation` persistence.

Only after successful deterministic aggregation is one Evaluation Record built and stored.

Current persistence writes Evaluation Record v2. Its `assessment.conflicts`
array includes the deterministically bound `candidate_path` alongside
`proposal_claim`, `candidate_claim`, and `incompatibility`. Historical
Evaluation Record v1 artifacts remain readable; their assessment has no
`conflicts` member and they remain legacy evidence rather than being silently
treated as current v2 output.

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
- current output contract `knowledge-note-evaluator-output-v4`;
- provider `ollama`;
- resolved model identifier and digest;
- adapter version `ollama-evaluator-chat-structured-v3`;
- strategy `groundedness-plus-pairwise-candidates-with-verifier-v1`;
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
