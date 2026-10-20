# ChatGPT Export: private Reader and review-preview admission v0

Parent: #411, following merged #393, #396, #398 and #408. This is the
**first read-only Source admission slice**. The target remains a real
Source → Generator → Validation → fresh Retrieval → Evaluation →
awaiting_human_review pipeline; this PR **does not implement that Job**.

## Actual functionality in this slice

Two new modules (pure input metadata / no side-effectful shared AI actions):

1. chatgpt_export_reader.read_admitted_source_page
2. chatgpt_export_review_preview.prepare_private_review_preview,
   build_private_review_preview and revalidate_private_review_preview

The private Reader requires an existing nonroot-owned private Intake root
with 0700 directories, 0600 archive/SQLite/salt/lock and committed Journal
inventory. It does NOT create missing directories or an Import Journal.
A reader operates under a shared advisory flock compatible with existing
Intake's exclusive writer lock. The SQLite database is opened with
mode=ro and query_only; no updates, shadow journals or HMAC regeneration.

The private selector must explicitly provide:

- exact scope (matching committed Journal selection policy);
- HMAC conversation identity from the private Journal;
- expected semantic content SHA-256;
- expected complete Source revision SHA-256;
- optional explicit JSON-member selection;
- bounded page SourceCursor and extraction limits.

The reader verifies:

1. Journal schema/version, quick integrity, permissions and lock owner;
2. protected 32-byte HMAC salt bound to committed Journal salt_sha256;
3. latest source identity's content+revision+run_id and revision record;
4. run_items and run state (inventory_only or inventory_review_required);
5. exact parser+selector policy, global latest committed scope run, and
   absence of newer ready archives not yet committed or rejected;
6. archive readiness, owner/mode, byte length, full archive SHA-256;
7. a fresh strict parse of the current selected conversations, exact
   run digest tied to the committed selector, and HMAC conversation match;
8. exact semantic content revision and complete Source revision match;
9. bounded SourceContextPage and its independent span/unit/hash revalidation.

The result (PrivateAdmittedPage) includes selected private text and node/
message IDs **in process memory only**. It is not a Vault or shared Context
artifact and sets generation_authorized=False and
approved_for_human_review=False. These flags are never permission grants.

A committed conversation that is absent in a newer Export is NOT
silently resurrected as current evidence; it still exists in the Journal
only for no-delete history. If a newer ready Export has not been evaluated,
reading the old Source is blocked pending inventory adjudication. A
metadata/title-only Source revision is bound independently even when the
semantic content digest has not changed.

## Strict review-preview handoff, still no Job

The review-preview module takes only:

- the admitted private Source page (or the complete Reader parameters);
- an explicit bounded JSON response from an untrusted proposed-candidate
  generator. The code does **not** call any model or transport.

It reuses the previously merged strict candidate decoder and exact
Source quote/position/hash binder. The result contains private
Evidence Candidates, deterministic preview digest, and review risk
signals. All candidate kinds remain provisional, including
decision_candidate, result_candidate and correction_candidate.

The deterministic risk signals do not attempt to *prove* meaning.
They only identify:
- user report vs. assistant claim;
- need for user confirmation on suggested decisions;
- need for independent result/status verification;
- need for chronological context around possible corrections;
- incomplete Source coverage and partial-page context.

A model's exact quotation is **not** proof that its interpretation
is true, its suggested project state is correct, or the user agreed.
We deliberately avoid heuristic promotion to an adopted decision or a
verified implementation result.

No production Review status is asserted:

- status is only review_required or no_candidate;
- job_status is always not_submitted;
- semantic_evaluation_completed is always false;
- awaiting_human_review, generation_authorized and
  canonical_mutation_authorized are always false.

revalidate_private_review_preview reopens the current Source from the
private Journal, rechecks source/run/selection and every candidate
binding, then compares the expected risk set, bounded cursor and
preview digest. Thus a stale preview cannot be reused as a fresh
artifact just because it once passed Source validation. This check is
not atomic with a future production commit and must be incorporated
into the eventual guarded job-admission transaction.

## Synthetic test coverage

Strictly synthetic and Fake-Provider-only: no real ChatGPT ZIP is read,
persisted, submitted or sent to the network by these tests.

- a complete synthetic ZIP → immutable-ready Intake → committed Journal
  → private Reader → strict Fake-Provider JSON → validated preview;
- Unicode pagination across page boundaries without text loss;
- missing/corrupt Journal, missing/changed salt, invalid permissions,
  unsafe symlink, tampered ready ZIP, unsupported scope/member selector;
- stale source revisions, newer uncommitted/rejected Export, no-delete
  behavior when a conversation is absent in a later Export;
- a forged Journal run_item or revision record rejected before reading;
- strict evidence quote, role attribution and stable anchor identity;
- decision/result/correction/hypothesis feature risk labels remain
  unverified even for syntactically correct, semantically false text;
- rejected tampering of preview flags, status, digest, candidate batch,
  quote and risks; repeat freshness check rejects updated source;
- no model provider invocation, Job scheduling, Vault access or writes.

## Security assumptions and deployment limitations

This is a local, same-owned-root data prototype. It is explicitly NOT
a production Importer service:

- No Importer/Reader separate UID has been provisioned.
- Same-UID adversarial concurrent file replacement is out of scope;
  full path FD-relative anti-symlink hardening and host ACL deployment
  remain separate requirements.
- It does not issue a capability token for shared AI workers or cross
  UID communication. Never hand a PrivateAdmittedPage or
  ExternalReviewPreview directly to a public logger/UI.
- Re-parsing a large ZIP for each admitted page is bounded but not yet
  optimal for high-volume paging. A trusted per-archive snapshot cache
  with revision binding may be considered after profiling.
- The existing production pre_review_job.submit_job accepts only
  Vault ContextBundle or semantic-objective contexts. **Do not forge**
  11-Knowledge/10-Project paths, weaken those validators, or inject raw
  Export excerpts into their query field.
- The system does not perform semantic verification, fresh retrieval,
  real Generation/Validation/Evaluator model calls or Review Job
  projection. Its return status is NOT awaiting_human_review.
- No automatic backfill, initial import of all conversations, cron,
  systemd, credential broadening, WebDAV PUT/DELETE or Human Review
  auto-approval is authorized.
- The real ChatGPT Export still has unresolved assistant final/visibility
  evidence, omitted multimodal and empty-slot coverage, and unverified
  reciprocal child edges.

## Required next slices for #411

1. A dedicated, versioned external Source artifact/ownership split
   (Reader-owned private source and scoped worker-readable Context)
   with source and review snapshot admission under a separate UID.
2. New exact Context kind/Recipe identity in the existing Job pipeline
   without altering the original Vault and semantic objective validators.
3. A low-trust Candidate proposer/model adapter, semantic evidence
   checker (especially corrections/user confirmation), and bounded
   Generation path. Reject/unknown remains a safe non-promotion.
4. Existing Validation, fresh Retrieval, Evaluator and
   awaiting_human_review Job state with visible source provenance,
   rate limits and shared Review queue backpressure.
5. A fully synthetic Fake-Provider integration test exercising the
   **real production Job state machine**, plus independent code review.
   Only then request separate permission for real private Export
   sampling and controlled production rollout.

This PR may be technically accepted as an isolated no-effect Source
admission foundation. It MUST NOT close #411 or claim complete Import.
