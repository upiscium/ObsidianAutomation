# #411: Private evidence retrieval for External Source Human Review

Depends on Draft #428 (dedicated synthetic External Source Worker).
Addresses the missing Human Review evidence surface: the existing
`.external-pending.json` is a **metadata-only** integrity projection
and deliberately lacks the original text that a reviewer must inspect.

## Exact admission and presentation boundary

The new in-memory `read_private_review_evidence` API is synthetic-scope
only (`fake-e2e`). It requires an existing `awaiting_human_review`
External Job, protected Private Reader root, original Review Preview,
and exact Job ID. It holds the private Intake shared lock, then
independently revalidates the Source ZIP, Scope, HMAC identity, complete
Source revision, Journal, candidate quote anchors, and conservative
Evaluation/v3 Generator provenance before returning any private text.

For each pending candidate, it returns a bounded immutable
`PrivateReviewQuotation` with:

- Source message index and SHA, exact Unicode codepoint quote offsets,
  exact short quote and quote SHA-256;
- role and attribution (user report vs assistant claim),
  candidate kind, an untrusted proposed summary, and
  human-verification risk labels;
- `source_quote_verified=true` only for exact Source provenance;
- `semantic_truth_verified=false`, `user_confirmation_verified=false`,
  and `correction_chronology_verified=false` unconditionally.

The parent packet is explicitly `semantic_status=unknown` and
`review_state=awaiting_human_review`, with no approval, canonical
mutation or Job authorization. It never contains a raw original
conversation ID/title, full conversation, network credentials or
production Vault target.

Quotes can still contain deeply private personal information.
Therefore the packet is returned ONLY to an authorized in-process
Reader caller, is excluded from `repr`, is limited to 16 candidates
and 48 KiB per packet, and is NOT persisted, logged, exposed to public
Dashboard/UI or published to the existing Human Review projection.
Creating a production Reviewer-facing private channel with separate
UID/ACL is a later explicitly authorized requirement.

Even when a bounded Source page has full local coverage, a later
statement in another conversation may correct a decision or result.
The packet explicitly NEVER asserts that all corrections, Human
confirmations, implementation witnesses or chronological context
have been checked. Human/semantic reviewers must verify those.

## Test evidence and non-goals

Tests use synthetic Export ZIPs and a deterministic Fake Provider. They
cover exact Source quote equality, role/attribution, HMAC/Revision
binding, pending state and conservative Evaluation, no extra files
or Human decisions, stale Source rejection, forged quote or model
summary, wrong Job, invalid bound, repeated read identity and
synthetic Scope only.

This slice implements **neither** an automatic model-based semantic
truth assessor **nor** a Human approval action. The generic Knowledge
Review API intentionally rejects the synthetic External Fake
Evaluator; a future dedicated Human decision/Source correction
workflow is required. It also does not install an Importer, daemon,
WebUI or timer; no real ChatGPT Export text was read or sent to a model.

Do not Merge/deploy without explicit Human approval. #411 stays OPEN.
