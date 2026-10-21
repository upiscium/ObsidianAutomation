# #411 External Source Job admission (inert state, MVI prerequisite)

Depends on Draft PR #421 (Fake Provider to immutable external Source CAS),
which depends on Draft PRs #419, #416 and #414. This slice introduces a
**new external-only Recipe and an inert Job row in the real pre-review
orchestration database**, not a runnable Generator/Validator job.

## Contract and supported behavior

The separate module `chatgpt_external_job.py` defines:

- `ExternalPreReviewRecipe`, pipeline `chatgpt-external-pre-review-v0`,
  prompt hash/version tied to the #421 *candidate proposer*,
  context kind `chatgpt-external-source`, current-revision input policy,
  and explicit `admission-only-no-worker` mode.
- `parse_external_admission_recipe` with an exact allowlist, strict
  version/pipeline/prompt hash/authority-policy compatibility,
  4 KiB byte limit and no arbitrary provider/shell/endpoint properties.
- A separate immutable `<SHA256>.external-pre-review-recipe.json`
  artifact under the existing `02-Orchestration/recipes`. It cannot
  load as a legacy `<SHA256>.recipe.json`.
- `stage_external_admission(ai_root,private_root,preview,external_context_sha)`
  which reopens the protected Reader/Journal and CAS, validates Source
  HMAC/content/revision/run/preview and evidence freshness, and then
  inserts a real `jobs` and `generations` record transactionally.
- `inspect_external_admission` which verifies the actual Job row,
  immutable recipe hash, and **current** private Source again.

The newly created generation state is `external_admitted`, NOT `queued`.
All existing legacy, validation, retrieval and evaluator worker
claim functions only accept their original active stage states. They
must never claim an external admission by accident. There is no code
in this slice that transitions an external Job to `queued`.

The Job ID is namespaced by `chatgpt-external-pre-review-v0` and the
exact Context SHA and Recipe SHA, to prevent collision with legacy
Job IDs even if the same source hashes are encountered.

Admission is idempotent and bounded (default 128 outstanding admission
records, explicitly configurable downwards); a repeated identical
admission does not allocate a new Job, Generation or attempt.
No generation or Human Review artifact is created, and existing
`awaiting_human_review` queue counts remain unchanged.

## Test evidence and immutable gates

Synthetic ZIP → private Intake/Journal → Reader → fake candidate
→ strict External Context → CAS → real Job DB `external_admitted`.
Tests verify idempotence, cap/backpressure, same-source/revision binding,
stale Source rejection, forged/unknown Recipe policy rejection,
hardlink/symlink/FIFO-like behavior for recipe files, no generation
stages claimed, no canonical Review/Validation/Receipt artifacts, and
correct coexistence with a legitimate legacy queued Job.

## Explicit limitations and next gate

- This IS NOT the complete #411 pipeline. No External Generator or
  GenerationRecord is produced; no knowledge draft, semantic judgment,
  fresh retrieval, Evaluator or `awaiting_human_review` transition.
- The durable admission transaction is in the shared Job SQLite DB
  but is NOT atomic with private Journal revisions. That is acceptable
  only because the admission is **not runnable**. A future activation
  must recheck Source under an established dedicated UID/ownership
  and transaction protocol before dispatch.
- The implementation uses repository-private SQLite/Job helpers,
  subject to independent review and maintenance coordination.
- Do NOT call `regenerate_job` to activate the external admission;
  it correctly rejects state `external_admitted`.
- Existing producers and Workers remain unchanged. Do not loosen
  `ContextBundle` or semantic-objective schema checks.
- No real user Export was sent to any model; all tests use synthetic
  ZIPs and deterministic Fake Provider. No production LXC, systemd,
  user permission, credential or Vault mutation was made.

Next: a separately versioned External Generator recipe + source-bound
Generation Record and a dedicated stage claim/activation path that
cannot be handled by an old Worker, then strict Validator → fresh
Retrieval → Evaluator → actual `awaiting_human_review` with synthetic
Fake Provider E2E and queue backpressure. Human deployment approval
is a separate gate. Keep #411 OPEN.

Do not Merge without explicit human authorization.
