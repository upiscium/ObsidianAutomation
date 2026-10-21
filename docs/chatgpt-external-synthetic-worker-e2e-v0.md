# #411 External Source dedicated synthetic Worker E2E

Following Drafts #414 → #416 → #419 → #421 → #423 → #424, this PR
adds a **distinct External Source Worker stage path** that reaches the
real `awaiting_human_review` Job state without test-only SQL updates.
All examples and tests are synthetic, not production Importer services.

## Why another stage path is necessary

Previously #424's feasibility test directly rewrote an isolated SQLite
generation from `external_admitted` to legacy `queued`. Although that
proved the old stage engine's artifact mechanics, it did **not** prove
a safe, deployable path. Legacy workers could claim the promoted row.

This change keeps the External Source pipeline in its own stage namespace
inside the existing pre-review SQLite DB, while preserving ordinary Job
IDs, attempts, stage_outputs, artifact hashes, and global Review backpressure.

External transitions:

1. `external_admitted` → `external_generating` → `external_validating`
2. `external_validating` → `external_building_evaluation_context`
3. `external_building_evaluation_context` → `external_evaluating`
4. `external_evaluating` → **`awaiting_human_review`**

Each stage writes an actual `attempts` row and selected immutable
`stage_outputs` into the *existing real pre-review Job DB*.
Every selected output is bound to the exact predecessor's SHA values.
Existing legacy Worker calls with `generation`, `validation`,
`evaluation_context`, `evaluation` never claim External states.
The generic claim/start APIs explicitly reject `external_*` stages.
Generic `regenerate_job` and `retry_generation` cannot requeue
an External Source Job as a legacy `queued` Generation.

## Immutable worker-mode Recipe gate

Old version-1 External Recipe continues to declare
`admission-only-no-worker` and is NOT runnable.

The new **version-2 synthetic-only Recipe** is pinned to
`admission_mode=synthetic-fake-worker-only`, the exact Candidate Proposer
hash, the distinct External Generator prompt version/SHA, and
`evaluation_policy=unknown-manual-review-fake-v0`. The Worker rejects
missing/stale/tampered versions, auto-proceed policy or a version-1
admission. No real provider identity or authorization can be expressed
in this Recipe.

## Source freshness and privacy

All stage activation and completion use the Reader's validated private
root and shared advisory lock, coordinated with the Intake writer's
exclusive lock. Before each stage is started/committed, the Worker
reopens the immutable External Context CAS and checks its original
Journal HMAC, scope, selected Source revision, ZIP digest, run and
evidence anchors. Writer-visible changes after the Fake Provider
begins but before any output selection cause a blocked stage.

The Worker accepts **only scope `fake-e2e` and a bounded byte string**
standing in for a Fake Provider's Knowledge Generator JSON output.
There is no HTTP code path, model key, provider, timer or background
import loop. The external source text is never sent to a model.

Dedicated Validation invokes the real deterministic Knowledge Note
Validator, which writes accepted untrusted mutation artifacts only
to isolated private state. Fresh Retrieval builds a real Knowledge
index over an isolated synthetic Vault and checks exact index/source
revisions through the existing retrieval contract. The Fake Evaluator
always records groundedness, knowledge_quality, epistemic_status and
consistency as `unknown`, with `recommendation=manual_review`. This
does **not** constitute independent semantic truth verification.

The current Review queue receives the exact DB state
`awaiting_human_review`. An additional private, bounded
`.external-pending.json` metadata projection carries HMAC Source
identity, Source revision, Evidence candidate anchors/role/risk flags,
generation and evaluation SHA hashes, and explicit
`human_approval_granted=false` / `canonical_mutation_authorized=false`.
No original raw conversation ID/title or full quote is exposed there.
The display/re-read function verifies the CURRENT protected private
Source and evaluation before returning review metadata. A newer
unprocessed or committed Export invalidates an old Review projection.

## Tests

- A complete synthetic ZIP → Private Intake/Journal → Reader → Fake
  candidate proposal → typed Context/CAS → external v2 Job admission →
  dedicated Worker claims **four real distinct stages** → real
  structural Validation → real fresh Knowledge retrieval → Fake
  Evaluation Record → actual `awaiting_human_review` DB state;
- Normal legacy Job states/claims remain isolated;
- Job stage records preserve exact output hashes and remain unique;
- Stale or changed Source before/during generation, during validation
  or after review prevents further use;
- Unsafe Recipe version, implicit v1 activation, malformed model output,
  oversized Fake wire, fake production scope, and fake proceed/auto
  approval are rejected;
- The shared Human Review backpressure is respected;
- No 20-Review approved decision/30-Receipts executable mutation or
  canonical Vault change occurs; no model/provider ever contacts network.

## Not production-ready

- This Worker is an explicitly synthetic-only function, not wired into
  systemd, GUI, production scheduler, credentials or a real model adapter.
- The v2 Recipe deliberately forbids real provider use. Real inference
  requires a new reviewed deployment policy and authority boundary.
- Reader and private Journal still run same-UID in synthetic tests.
  Separate nonroot Importer/Worker UID, cross-UID transport, ACL and
  guarded atomic Source→Worker ownership remain separate deliverables.
- There is no real external semantic confirmation/correction checker.
  Unknown/manual_review is **safer**, but does not prove correctness.
- The normal evaluator's ContextBundle-based grounding prompt is still
  incompatible with v3; the Fake Evaluator does not claim otherwise.
- No crash-restart auto-recovery for partially running External stages
  is enabled. A crash leaves work blocked/incomplete for explicit triage.
- Human Review projection is private metadata, not yet wired to the
  production management screen or canonical mutation review workflow.
- No uncontrolled initial ChatGPT Export backfill or real user ZIP LLM
  sampling has been executed. The original private Export remains
  untouched. No production LXC/Vault/systemd changes are authorized.

Do NOT close #411 or Merge/deploy without explicit user approval.

## 2026-10-21 review corrections: legacy write/Review boundaries

A separate read-only OpenCode review was attempted, but the review model
did not produce a completed verdict before timeout. The following defects
were instead identified by source inspection, reproduced with synthetic
red tests, and fixed with green regressions:

1. The generic pre-review `complete_attempt` previously accepted an
   `external_*` running Stage with arbitrary syntactically valid SHA
   outputs, **without invoking the dedicated private Source recheck**.
   It now rejects all External Stage outcomes. A distinct internal
   `_complete_source_bound_external_attempt` is called only from the
   dedicated Worker after validating the exact Source under the shared
   Intake reader lock. This is code-path isolation, NOT cryptographic
   proof against an adversarial process with the same UID.
2. The legacy `create_evaluation_bound_review` could accept an External
   **synthetic-fake-only** Evaluation Record and create an authoritative
   `approve` Review outside the unimplemented External Review UI.
   The generic Human Review API now refuses that Fake-provider identity.
   Future live External providers require a new explicit durable source
   provenance/policy boundary, not just another string allowlist.
3. The generic post-Review reconciler expected only legacy `evaluation`
   Stage Output. A valid External Job at `awaiting_human_review` caused
   the entire Reconcile to error. It now leaves External pending Jobs
   untouched for a future dedicated Human decision flow, and excludes
   them before applying its bounded `max_items` limit so legacy
   approved jobs cannot starve behind many External reviews.

Legacy Vault and Semantic Objective paths remain unchanged semantically.
Source-only review scope: no real ChatGPT user Export, production ACL or
runtime, real semantic trust evaluator, approved note, or remote model.
Independent model attestation remains **NOT OBTAINED**, and this PR
continues as Draft/UNMERGED pending explicit Human approval.
