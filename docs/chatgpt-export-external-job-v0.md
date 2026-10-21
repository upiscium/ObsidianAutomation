# ChatGPT Export External Source → pre-review Job v0

Parent Issue #411. Stacks on Draft PR #414. This is a **synthetic E2E /
disabled-by-default** implementation, not production acceptance or approval
for real Export ingestion.

## New opt-in external context path

The existing Vault ContextBundle v1 and Semantic Objective v2 contracts
remain unchanged, and their path validators are never broadened to treat
ChatGPT Export history as Knowledge or Project Markdown notes.

1. A Reader-owned ExternalSourceContext v1 selects exactly ONE previously
   reviewed, bounded and exact-quote-linked unverified candidate. The
   immutable 05-Context artifact includes protected HMAC source identity,
   archive/run, original content and full revision digests, candidate ID,
   anchor/quote hash and offsets, quoted excerpt, unverified summary and
   explicit uncertainty/risk labels. It contains NO full Export ZIP and
   NO plaintext conversation title or original user identity.
2. store_external_context first revalidates the latest private Journal and
   Source page and requires an exact ReviewPreview candidate to exist.
   This is intentionally a cross-UID permission boundary for later work.
3. Dedicated generator prompt chatgpt-external-knowledge-note-v0 is versioned
   and SHA-bound. Only source role, bounded excerpt, uncertainty flags,
   proposed interpretation, and evidence anchor/quote hashes are included
   in the external model payload. Conversation HMAC, Source revision and
   candidate ID remain in local Generation provenance, not disclosed to
   the model. The JSON model response must echo the actual evidence
   anchor and exact quote hash, preserve the original literal excerpt in
   its Markdown body, begin with the warning heading 未検証, and set
   source_type=other.
   Unknown output fields, changes to hashes, missing quote and invented
   authority fields fail closed.
4. Generation Record v3 binds the actual Context SHA and source HMAC, archive
   SHA, run, Source Revision, candidate, anchor and preview digest. It
   cross-checks those against the immutable external Context during
   creation, storage and load. Legacy Generation Record v1/v2 remain intact.
5. The existing PreReview Recipe gets a separate prompt identity and explicit
   context_kind for the external path. The first version supports only
   OpenAI-compatible API provider configuration, NOT Ollama as a raw provider.
6. Both Job submission AND Generator Worker invocation independently require
   explicit external_source_opt_in=True. Existing production worker CLIs
   never pass that option, so new external Job creation/model dispatch is
   **disabled by default** even if this code is merged.
7. Until chronological semantic adjudication is implemented, only
   unclassified, question_candidate and hypothesis_candidate with complete
   admitted Source and risk flags may enter pre-review. Decision, result,
   correction and feature-change candidates remain blocked.

## Synthetic end-to-end acceptance

A deterministic synthetic ZIP is published privately, committed to the
Journal, read via the private Source Reader, transformed into strict JSON
Evidence Proposals, and verified as an untrusted Preview. Only then is
a single external Context stored and a dedicated Job submitted with
explicit test-only opt-in.

The test uses Fake Provider output but exercises the REAL pre-review worker
state machine:
- Generator with exact external prompt and Generation Record v3;
- Validator creating accepted private mutation artifacts;
- Reader building a fresh BM25 candidate index and Evaluation Context;
- Evaluator with Fake Provider yielding manual_review;
- Job database transitioning to **awaiting_human_review**.

The test asserts the canonical Vault is unchanged and no actual Human
Review approval record exists. It also proves that a changed or forged
Generation Record, invalid model output or missing opt-in cannot
silently advance.

These tests prove orchestrator compatibility, NOT independent correctness
of extracted statements, user approval or implementation completion.

## Non-negotiable remaining restrictions

A Generated Knowledge Note is only a proposal until human decision,
regardless of a pre-review recommendation. Exact quotations and hashes
prove byte provenance, not semantic entailment. There is no finished
chronological adjudicator for later user corrections or retractions,
observed experiment results, feature status, contradictory evidence,
unverified assistant visibility/finality or unsupported multimodal parts.

Production is still disabled. The new Context artifact exposes a bounded
private excerpt in Reader/Generator-visible 05-Context. A separate
Importer/Reader service UID, scoped consent to the model recipient,
cross-UID escrow, privacy audit and source revocation check are required
before any real user Export content is copied there.

The private Journal is checked when materializing the Context, but the
worker cannot currently re-query that private Journal when claiming or
restarting the Job; therefore a queued Context might become stale after
a newer Export. This is a **production blocker**, not a test success.

No systemd, cron, model endpoints, credential grants, Vault writes or
automatic backfill were changed or enabled. No real user's Export bytes
are used in tests or sent to models.

## Remaining #411 work

Source-specific semantic evaluator and risk reasoning; trusted revocable
Reader → Generator admission at every Job claim and pre-dispatch;
Human Review projection with Source provenance; queue/backpressure tests;
real service isolation and independent review. After these gates,
request explicit authorization for a tiny real-Export sample and
controlled production rollout. Never close #411 based solely on fake E2E.
