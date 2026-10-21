# #411: External Generation Record v3 with exact Source provenance

Follows Draft #423 (real Job DB admission in `external_admitted` state).
This is an explicit external generation contract and Fake Provider test
path, NOT a runnable stage worker or completed Human Review pipeline.

## Backward-compatible Generation Record extension

The existing immutable `generation_artifact` module now accepts a new
`record_version=3` only when `context_kind=chatgpt-external-source` and
the `external_source` provenance object is present. Record v1 remains
strictly `context-bundle` (Vault-only). Record v2 remains strictly
`semantic-objective`. Neither may carry external Source provenance.

The v3 external provenance includes:

- source Scope and private HMAC conversation identity;
- exact archive, semantic content and complete Source Revision SHA-256;
- committed private Import run SHA-256;
- private Review Preview SHA-256.

The v3 parser uses an exact distinct JSON property set, rejects unknown
fields or forged Vault paths, enforces all hash and Scope formats, and
requires the static version/hash of the new *external Knowledge Generator*
prompt rather than the legacy Generator prompt. `build_generation_record`,
`store_generation_record` and `load_generation_record` re-open the
Reader-controlled external Context CAS and compare all provenance fields.
A corrupted/forged/stale Context CAS digest is rejected. CAS verification
is **not** a replacement for private Source latest-revision verification
by a dedicated Reader/Job admission protocol.

`generation_input_context` intentionally rejects v3 for now: the existing
Evaluator expects a ContextBundle with Vault-derived paths. A future
Evaluator must receive explicit untrusted external evidence; this code
must not fabricate `11-Knowledge` or `10-Project` Source paths.

## Static untrusted generator prompt

`chatgpt_external_generation_contract.py` defines an independent
generator prompt version/hash and bounded JSON payload. It accepts
only the approved-in-code External Context data structure: opaque
evidence anchors, proposed summaries, role/attribution and risk flags.
The prompt repeatedly states that these are unverified, user agreement
and implementation completion cannot be inferred, and original text
commands are not authority. It outputs the existing KnowledgeGeneratorOutput
schema but requires `source_type=other`. No model client is built in.

## Fake Provider output path (not a Job stage)

`run_fake_external_knowledge_draft` requires an explicit deterministic
Fake Provider callback and an existing real `external_admitted` Job.
It performs private Source/Job revalidation before and after the
transport, parses/limits the output, rejects a wrong source_type, and
stores a draft untrusted CreateNote proposal plus a Generation Record v3.

This is deliberately NOT a Job stage result: it does not call
`complete_attempt`, transition from `external_admitted`, select a
Generation stage output, invoke a real provider, validate a Note,
perform fresh retrieval, evaluate grounding or reach Human Review.
An untrusted Generation Record is evidence of a synthetic proposed
artifact, **not** evidence of user approval, correct knowledge,
worker compatibility or completed implementation.

## Verification and missing work

Synthetic-only tests cover separate v1/v2/v3 parser shapes, all Source
SHA bindings, static generator prompt identity, forged SHA/Scope/kind,
empty/invalid model responses, incorrect source_type, stale Source
while a Fake Provider is called, Source Context mutation, CAS/idempotent
writes, no Job Stage outputs, and no Human Review record.

Next: real External Job/Recipe model/provider identity and worker
claim/activation isolation, v3-aware semantic grounding input for the
Evaluator, deterministic epistemic/confirmation checks, fresh Vault
retrieval, and an actual Fake Provider `awaiting_human_review` E2E.
Both protected private Source and shared AI Worker ownership must be
implemented under separate UIDs before production. No real Export
LLM call, scheduled import, Vault operation or deployment is authorized.

Do not Merge without explicit maintainer approval. Keep #411 open.
