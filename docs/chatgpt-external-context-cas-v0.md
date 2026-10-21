# #411: External Source Context CAS and legacy-Job denial

Depends on Draft #414 (private Reader) and Draft #416 (typed External Context).
This is an isolated source-only storage/admission step, not the completed
Generator → Validator → Retrieval → Evaluator → Human Review pipeline.

## What this slice actually does

- `store_external_context_for_review(ai_root, private_root, preview, context)`
  rechecks the complete bounded private Source and HMAC/Journal revision
  through #414 and #416, then publishes exact canonical Context bytes
  atomically to the preexisting Reader-owned `05-Context` stage.
- Context is stored as `<SHA256>.chatgpt-external-context.json`, never as
  the old `<SHA256>.context.json` used by Vault `ContextBundle v1`.
- Existing atomic CAS no-replace publication and revalidation on collision
  are reused. No new Root path, credentials, permissions, job database
  or systemd unit is created by the private Reader.
- `load_external_context_for_review` requires a valid canonical versioned
  External Context file, exact digest and regular non-hard-linked inode.
  It does not claim that the underlying private Source is still current.
- `revalidate_saved_external_context` checks the saved CAS **and** the
  current private Source Journal, selected source and candidate evidence.
  Stale, absent, newly uncommitted, tampered or wrong-source material fails.
- Empty-candidate Source pages are valid preview outcomes, but cannot
  become a persisted potential-generation Context.

The separate context kind is `chatgpt-external-source`. It retains
only private unverified proposed summaries, risk flags, hashes/cursors
and scope/HMAC, with no raw conversation title/ID, full quote, Vault
target path or Human approval. It may still contain private personal
information in proposed summaries: `05-Context` must remain
Reader-controlled, explicitly restricted to authorized AI workers.
Never publish the Context bytes to public logs/GitHub.

## Critical boundaries

Existing `load_context_bundle`, legacy `render_generator_prompt` and
`pre_review_job.submit_job` **reject** these newly stored Context digests
because they use the old filename/schema and never treat External Source
as a Vault note. The legacy Job database is not created by a rejected
attempt.

This CAS does **not** grant Generation/Job Submission or Human Review.
All three External Context authority flags remain false. The private
Source may change after CAS publication; future durable Job admission
must revalidate the Source and enforce a cross-UID authority/freshness
protocol. It must not merely trust content hashes or re-label the
Context as a legacy Vault ContextBundle.

## Verification

All fixtures are synthetic, including fake candidate responses. Tests
cover idempotent CAS storage and load, Journal unchanged after read,
immutable digest and symlink/hardlink rejection, superseding Export
invalidation, disallowed empty context, missing private/shared roots,
forged kind/revision, legacy prompt/job refusal without creating a Job.

No actual ChatGPT Export was read/sent, no real provider/model invoked,
no LXC/service/Vault deployment performed, and no Human Review state
was entered.

## Next required steps in #411

1. A *new and versioned* `ExternalPreReviewRecipe` plus prompt hash and
   Source Context kind, with explicit Worker admission/ownership.
2. A Generator/GenerationRecord route that consumes only the validated
   external CAS, carries exact HMAC/Archive/Revision provenance, and
   cannot be confused with either original ContextBundle or semantic objective.
3. Deterministic quote and semantic corrections/confirmation checks;
   real Validation, fresh Retrieval and Evaluation, then real Job state
   `awaiting_human_review` under a Fake Provider E2E.
4. Distinct service UID/ACL and production-specific rollout gates.

Draft only. Do not merge without explicit User approval; do not close #411.
