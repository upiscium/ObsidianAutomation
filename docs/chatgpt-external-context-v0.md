# #411 External Source Context v0 (second Draft slice)

This change follows Draft PR #414 (private Source Reader and Review Preview).
The External Context is an explicitly distinct contract with
`context_kind="chatgpt-external-source"` and `record_version=1`.
It is NOT a Vault ContextBundle, semantic objective, generation record or
existing pre-review Job. No existing Context/Recipe/Worker checks are changed.

## Contract

`chatgpt_external_context.py` provides:

- `build_external_context(preview)` from a syntactically validated, private,
  non-authoritative Review Preview;
- `parse_external_context(data)` strict bounded JSON with exact field sets,
  duplicate-key rejection, SHA-256, typed cursor and byte limits;
- `revalidate_external_context(root, preview, context)` to re-open private
  Reader/Journal and verify the Source revision, HMAC, candidate anchors,
  exact evidence binding, risk classification, and context payload.

The contract includes opaque scope/source HMAC, archive/content/revision/run
digests, preview digest, bounded source cursors and evidence descriptors
(candidate ID, anchor hash, source-message hash, quote hash, source role,
attribution, provisional kind/summary and explicit risk flags).

It intentionally contains no original conversation title, raw source ID,
full excerpt, Vault path, proposed mutation, Human approval or model
credentials. The proposed summary is still untrusted and may be false;
a quote hash proves provenance, not semantic truth.

All three authority fields are hardcoded false and strictly parsed:
`generation_authorized`, `human_review_approved` and
`vault_mutation_authorized`. A future Job-admission stage must use its own
independent recipe, private-reader ownership and atomic revision check.
This Context is not automatically transportable or safe for shared logging.

## Test evidence

Synthetic only; no real Export or provider/model calls. Tests include
roundtrip, quote/summary binding, source digest tampering, wrong Context
kind, forged authority flags, duplicate keys/candidates, unsupported
extra fields including forged Vault target, Unicode byte bounds,
cursor bounds, and empty candidate sets.

## Remaining #411 work

1. Private Reader-owned Source artifact and explicit worker-safe bounded
   context delivery under dedicated UID/ACL, with atomic revision check.
2. New versioned pre-review Job Recipe, generation prompt hash and
   GenerationRecord Context kind without relaxing legacy or semantic paths.
3. Model proposer and semantic Validator with chronological correction
   and confirmation checks; fresh Retrieval/Evaluator and actual
   `awaiting_human_review` transition using a Fake Provider E2E.
4. Independent review, real-data controlled acceptance, production
   deployment and WebUI as separate gates.

Do not Merge without Human approval. Do not claim #411 complete.
