# #411 — Reuse native five-dimensional Evaluator with typed External Source evidence

Stacked Draft on #436 (shared Evaluation-bound Human Review adapter).
Only a synthetic input/rendering/record-provenance slice, NOT a live model
integration or production source analysis. Existing Vault and Semantic
Objective pipeline must retain its previous evaluation contract.

## Problem addressed

The existing `openai_evaluator` calls `generation_input_context`, which
accepts only a real Vault ContextBundle or Semantic Objective. A v3
ChatGPT Source Generation Record intentionally does not masquerade
as a Vault source. Passing its summary through a fake `query` or a
`11-Knowledge` source path would discard speaker, quote and revision
evidence, allowing a model's report to look like canonical Vault truth.

## Concrete shared implementation

`evaluator_contract.render_evaluator_prompts` now reuses one exact
prompt-schema/pairwise candidate rendering implementation for either:

1. Original `ContextBundle` inputs: identical v10 template identity,
   original query, original source paths and output schemas; or
2. New typed `ExternalEvaluationSource`: a distinct, version-hashed
   `chatgpt-external-evaluator-v0` template. It includes only the
   exact bounded quote, its Source message and anchor SHA hashes,
   speaker role/attribution, provisional summary/kind/risk flags,
   Source Revision/Run/Preview SHA and explicit `verified=false` markers.

The original Evaluation Candidate comparison data (fresh Vault index)
still comes from the original EvaluationContext; it is **not** the same
as private ChatGPT Source material. Redundancy, consistency, groundedness,
Knowledge quality and epistemic-status passes share the existing
`EvaluatorPrompt`, output schemas and deterministic aggregation.

The prompt's external system extension warns that the selected bounded
page is not a complete chronological history; exact Assistant reports
are not evidence of user confirmation or completed implementation.
No original conversation ID, title, HMAC or host path is sent in the
prompt payload. Exact quotes are private; they must remain inside the
authorized Reader process until a future independently approved
cross-UID model transport exists.

## Private Source admission

`read_external_evaluation_source` requires synthetic `fake-e2e` scope,
reopens the committed private Import Journal and exact source under
its SH lock, and rechecks bound candidate quote SHA/role/risk. It returns
bounded in-memory `repr=False` dataclasses: max 16 quotes, 4096 bytes
per quote/summary. It does **not** submit a Job or contact a provider.

## Evaluation result policy

`conservative_external_assessment` reuses the original evaluator
aggregation and preserves any observed material concern. It prevents
an all-PASS model verdict from elevating unverified Source evidence:

- groundedness PASS is downgraded to UNKNOWN;
- epistemic-status PASS is downgraded to UNKNOWN;
- a clearly detected concern remains `do_not_proceed`;
- otherwise recommendation is `manual_review` (never `proceed`);
- explicit absent verification of approval/results/chronology is logged
  in a fixed, non-private finding.

The synthetic External Worker now actually prepares the same
source-aware Evaluator prompts during its `external_evaluation` stage
and stores their distinct template version/SHA in the existing
EvaluationRecord v3, while retaining a deterministic Fake Provider.
No real OpenAI/Ollama call is made and no Source quote is persisted
in the shared Evaluation Record or public Review projection.

## Tests and retained boundaries

Synthetic Fake Provider E2E and existing Evaluator golden tests verify:

- old Vault prompt bytes/schema/hash and pass order remain compatible;
- External input carries exact quotes, speaker roles, Source revision
  and uncertainty, never forged `ContextBundle.sources`/Vault paths;
- original pairwise duplicate/consistency evaluation stays unchanged;
- stale Source is rejected before an Evaluator prompt is prepared;
- malformed SHA, forged quote, misleading role or unjustified
  `user_confirmation_verified=True` fails closed;
- an all-PASS Fake assessment cannot become automatically approvable;
- real EvaluationRecord captures the External prompt identity with
  UNKNOWN/manual_review and no Human approval or Vault effects.

This slice does **not** implement the real semantic correction detector,
chronological aggregation across conversations, real LLM transport,
Reviewer GUI, or positive approval policy. It deliberately keeps
the protected Source gate and the existing Review nonexecution
adapter enabled. Nonroot UID/ACL and v2 Status projection remain
separate production integration gates.

Do not Merge without explicit Human approval, do not close #411,
and do not use the real private ChatGPT Export in a model request.
