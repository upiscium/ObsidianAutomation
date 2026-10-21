# #411 — Shared Human Review v3 for External Source (synthetic-only MVI)

**Parent Draft:** #435 (shared Validator, fresh Retrieval and Review backlog).
This change implements a Source-aware adapter for the ORIGINAL Human
Review artifact and Reconcile. It does not create a separate approval
database, approval UI, Vault executor or new canonical Knowledge format.

## What is actually reused

The existing `knowledge_review` v3 writer has one Source-neutral internal
`_store_validated_evaluation_disposition` helper which performs the original
accepted Validation → exact Mutation → immutable Evaluation-bound
`<mutation SHA>.approval.json` ReviewRecord v3 sequence, rereads it and
returns the existing `KnowledgeReviewResult`.

The original public `create_evaluation_bound_review` retains its existing
provider gate rejecting `synthetic-fake-only` External Evaluations.
No change to its ordinary Vault and Semantic Objective approval behavior.

`chatgpt_external_review_adapter.record_external_nonapproval_disposition`
wraps the common v3 writer with the following External-specific guards:

- Only explicitly synthetic `fake-e2e` Reader Scope is accepted;
- Input must refer to the exact current Source HMAC/Revision and
  48 KiB bounded private Review Evidence Packet SHA-256, with
  nonempty Quote/Role/Evaluation binding;
- `read_private_review_evidence` rereads the private ZIP/Journal, selected
  Source/Preview, current v3 GenerationRecord and unknown/manual Review
  Evaluation under the protected Importer shared lock;
- The Evaluation must still be conservative, `synthetic-fake-only`,
  `unknown` epistemic status, and `manual_review` recommendation;
- Only `reject` and `keep_as_idea` dispositions can be recorded.
  **`approve` is categorically refused**, irrespective of the purported
  user/model statements, current Revision or claimed role;
- New and crash-recovered records use the SAME Evaluation-bound
  ReviewRecord v3 store as normal pre-review, with the same accepted
  mutation and immutable SHA binding. A conflicting preexisting
  ReviewRecord is rejected.

After persisting a nonexecuting v3 record, the adapter updates only the
existing `generations` SQLite row, using transactional compare-and-set:

`awaiting_human_review → external_review_recorded`.

`external_review_recorded` is an intermediate **Job state**, not an
additional Human Review ledger. It prevents the original scheduler's
pending Review count from including a durable terminal decision that
still needs reconciliation. A crash after ReviewRecord publication but
before the Job update is recoverable by rerunning the same guarded
adapter; it detects the matching already stored v3 Review artifact
without generating a second timestamp or conflicting authorization.

## Existing Reconcile and executor reuse

`reconcile_post_review` now includes `external_review_recorded` in the
same bounded, transactional worker scan. It chooses the exact selected
`external_evaluation` output for that row and otherwise uses the
original v3 ReviewRecord verification and original nonexecution
transitions, without duplicating decision logic:

- `reject` → existing `human_rejected` terminal state;
- `keep_as_idea` → existing `human_kept_as_idea` terminal state;
- any `approve` or older/unbound review version → **fail closed**;
- missing ReviewRecord or execution Receipt → **fail closed**.

Undecided External `awaiting_human_review` jobs remain excluded from
the legacy bounded Reconcile scan, preserving mainline throughput.
The existing execution dispatcher observes `reject` / `keep_as_idea`
and does not invoke Vault publication. It receives NO new External
`approve` capability. Original `approve` for Vault-derived knowledge
continues unchanged.

## Synthetic test coverage

- Real synthetic Export → private Journal → Source Reader → Fake Provider
  → unverified Context and Generation v3 → real Job state machine →
  conservative Evaluation → protected Review Evidence → common
  ReviewRecord v3 → shared Reconcile → original terminal states;
- Neither decision generates a canonical Vault change/Receipt;
- `approve`, malformed decisions, stale Source, forged Evaluation, wrong
  Job, wrong Revision and packet SHA are rejected before Review write;
- A crash between ReviewRecord CAS and the Job state transition is
  idempotently recoverable, while mismatched existing records fail;
- Forged `approve` v3 Review for an External state cannot be reconciled;
- Ordinary legacy pre-review and post-Review behavior is regression tested.

## Limits — NOT a deployment/approval authorization

1. Real user ChatGPT Export Scope and real model/provider use are refused.
   Only synthetic Fake Provider evidence is accepted in this MVI test.
2. Approval remains unavailable until a real Source-aware semantic
   evaluator can adjudicate confirmations, corrections and implementation
   status using complete context, and the current protected Revision is
   rechecked at the actual Human decision and canonical execution gates.
3. Although the Source Reader lock excludes an authorized Intake writer,
   this same-UID test setup is NOT an adversarial filesystem/ACL boundary.
   Reviewer/Importer/Worker roles and a private UI transport need separate
   nonroot identities and permissions before production rollout.
4. The pending Review evidence packet remains in private memory, not a
   public Dashboard or remote Nextcloud document. This slice does not
   establish a safe Reviewer UID bridge, operational CLI or restart daemon.
5. Python-internal helper functions are shared code, not cryptographic
   capability tokens against same-UID adversarial imports. No production
   service identity, systemd, LXC, credential, Vault write, live ZIP sample
   or initial backfill was touched.

**MVI interpretation:** Existing Human Review and Vault executor should
be reused; only External Reader/Context/admission and Source-specific
revision/attribution/confirmation checks need to differ. This PR is
Draft and must NOT be merged or deployed without explicit user approval.
Keep #411 OPEN until real-source semantic/permission/GUI acceptance.

## Additional existing-status compatibility gate

The current legacy `pre_review_status` v2 `ALL_STATES` contract does NOT
yet include any `external_*` Job state, including the new transient
`external_review_recorded` state. Its status builder presently refuses
unrecognized Job states. This is a **known production integration blocker**
for the whole stacked #411 synthetic External pipeline, already applicable
to previous `external_admitted` and `external_generating` states.

Do not turn on External runtime in a shared production Job database until
the existing status/dashboard projection has a compatible typed-state
strategy and its parser/UI have passed explicit acceptance. This Source
adapter is only tested in a disposable synthetic database and does not
change public status version or mislabel External states as Vault jobs.
