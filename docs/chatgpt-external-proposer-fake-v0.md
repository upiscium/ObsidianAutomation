# #411: Explicit Fake-Provider ChatGPT evidence proposer

Depends on #414 (Private Reader), #416 (External Context contract),
and #419 (external context immutable storage). This is a source-only
candidate-proposing slice: it does NOT submit a pre-review Job.

## Actual new behavior

The module `chatgpt_external_proposer.py` provides:

- `render_external_proposer_prompt(admitted)` that renders one bounded,
  source-verified in-memory page into a versioned JSON evidence payload;
- `prompt_template_sha256()` bound to its static role/instruction policy
  and output schema, independent of source text or model identity;
- `run_explicit_external_proposer(private_root, ..., transport=...)`
  that requires an explicitly injected Callable transport with no default
  provider URL/network/credential. Tests inject a deterministic Fake
  Provider. Live transport is NOT authorized by this PR.

The prompt provides selected unit/slice integer indices, speaker role,
attribution and bounded exact text. It omits raw conversation IDs/titles,
protected HMACs, host paths, credentials and any Vault mutation targets.
It always marks Source text as untrusted DATA; instructions or policy
claims found inside the source cannot change the static system prompt.

The provider schema permits only strict exact-evidence proposals:
unit_index, slice_index, Unicode quote offsets, exact quote string,
unverified summary and one allowed candidate kind. Returned bytes
must pass #408's exact quote/role/content digest binder; malformed
outputs fail with a fixed error, with no echo of private text or
provider exceptions.

After transport returns, the private Reader reopens its committed
Journal/Source and checks the revision again. An updated or uncommitted
newer archive during the model call is rejected. This is not a
durable Source→Job transaction; later Job admission will need another
freshness/ownership gate.

## Never promote candidate assertions

All returned candidate classifications remain provisional, including
decisions, results and corrections. The result keeps
`job_submitted=false`, `generation_authorized=false`,
`review_preview.awaiting_human_review=false` and
`review_preview.semantic_evaluation_completed=false`. No Knowledge
Note/Validation/Evaluation Record, model calling service, cron, UI,
Vault write or production permission is created by this PR.

The transport callback is a deliberate privacy boundary: a malicious
caller could transmit the private evidence to an untrusted endpoint.
Do not attach a real provider without separate user authorization and
the dedicated Importer/Reader/worker UID separation.

## Tests and next steps

All tests use synthetic Export ZIPs and an injected Fake Provider.
Tests check exact input schema, prompt-template SHA identity,
untrusted prompt injection-as-data, user/assistant role preservation,
no-candidate, malformed/wrong quote, provider error sanitation,
missing transport rejection, fake provider update race, and
no Job/Vault side effects.

Next #411 milestones: a separate versioned External Job Recipe,
private Source admission and worker context ownership, exact
GenerationRecord provenance, independent semantic validation of user
confirmation and corrections, fresh Retrieval/Evaluation and the
real `awaiting_human_review` state transition on Fake Provider E2E.

Do not Merge or enable production without explicit user approval.
