# #411 — Typed External Source support in existing operational Status

Depends on #436 (shared Evaluation-bound Human Review v3). This is a
**source-only, unmerged status producer/consumer contract change**.
No new Dashboard, alternate status service, model or permission path.

## Compatibility design

The operational Status producer has two exact output contracts:

- **v2 is preserved** when all current Jobs belong to the existing
  Vault / Semantic Objective pipeline. The emitted JSON property sets,
  existing 15 state names and prior schema
  `schemas/pre-review-operational-status-v1.schema.json` remain unchanged.
- **v3 is emitted only when an External Source Job is present**, using
  `schemas/pre-review-operational-status-external-v3.schema.json`.
  The original legacy state names, non-authoritative status authority,
  review wait, backpressure and planner cadence fields stay unchanged.
  Additional explicit state counters cover:
  `external_admitted`, `external_generating`, `external_validating`,
  `external_building_evaluation_context`, `external_evaluating` and
  `external_review_recorded`.

Status v3 also publishes a bounded, aggregated `external_source` object:

- `jobs`: number of current latest-generation External Source Jobs;
- `review_waiting`: the subset at `awaiting_human_review`.

These are aggregate counts only. No original ChatGPT conversation title,
message, raw identifier, HMAC Source identifier, archive digest, paths,
evidence quote, model output, Job ID or private credential is emitted.

Both versions are parsed strictly. v1/v2 inputs retain their prior
property and state allowlists; an old v2 frame claiming External fields
is rejected. v3 requires its exact new properties, supported state keys,
nonnegative integer counts and consistency with `current_jobs` and
`awaiting_human_review`. A malformed/unknown state remains a hard failure.

## Identifying mixed and terminal External Jobs

The Job state database already shares terminal states like
`awaiting_human_review`, `blocked`, `human_rejected` and
`human_kept_as_idea`. These states alone do not reveal which typed
Context was used. The producer therefore determines a current Job's
External origin by an explicit `external_*` active state or by an
immutable `external_*` attempt/Stage Output recorded under its latest
generation. This also keeps blocked and terminal External Jobs visible
in v3 without introducing another Job database or approval ledger.

Review/backpressure still use the original shared SQL/global count.
v3 does not change the meanings of `awaiting_human_review`,
`blocked`, normal Knowledge Review or executor authorization.

## Tests and deployment boundary

All tests use synthetic Export and Fake Provider, with the existing
external Job, v3 Generation, conservative Evaluation, pending Review,
non-approval disposition and Reconcile from stacked #411 PRs.
They check v2 exact legacy output, v3 after admission, pending Review
and shared backpressure, external non-approval and terminal status,
blocked synthetic Worker, v3 privacy and schema validation, malformed
types/counts, and existing `admin_ai_status.load_ai_health` aggregate
read-model compatibility.

**Important separate rollout gate:** Other running clients, including
an already-deployed ObsidianCore instance, may only understand v2.
This PR proves compatibility of the **repository's current**
`admin_ai_status` parser, not every already-deployed Core binary or
plugin. Update/canary those consumers and review the new v3 schema
before any External Jobs are created in the production shared Job DB.
Do not silently downgrade v3 to v2 after External Jobs begin.

This PR does not add a real private Importer/Worker/Reviewer UID,
Cross-UID transport, model semantics, live Export sampling, systemd
timer, Production Cutover or Vault writes. Existing Human Review
rejection / keep-as-idea reuse remains synthetic-only. User approval
is required before any Merge or production deployment. Keep #411 OPEN.
