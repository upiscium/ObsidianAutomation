# #411: Reuse existing pre-review rather than a second pipeline

## Decision

Following maintainer direction, **the long-term design should reuse the
existing Job/Validation/Reader/Evaluator/Human Review infrastructure**.
External ChatGPT Export remains a low-trust input adapter, not a separate
Knowledge approval or Vault publication authority.

Current Draft lineage: #414 (Reader) → #416 (Source Context) → #419
(immutable CAS) → #421 (Fake candidate proposer) → #423 (inert
admission) → #424 (Generation v3) → #428 (synthetic dedicated Worker)
→ #431 (private evidence view) → this new incremental shared-step PR.
Every Draft remains unmerged and no current production Importer/Worker
or real user ZIP model call is authorized.

## Exact reused vs. Source-specific boundaries

| Stage | Keep/reuse | New Source-specific rule |
|---|---|---|
| Intake/selection | Existing private immutable CAS approach | ZIP/Journal/HMAC/revision, selected branch, omitted coverage |
| Job lifecycle | Existing pre-review jobs/generations/attempts/output SHA and shared backlog | Explicit typed external Context/Recipe and freshness before stage selection |
| Generator | Existing Knowledge Note output schema and v3 Generation Record | Bound source-role/quote/prompt and candidate disclaimers, no fake Vault path |
| Validator | Existing `knowledge_validator.validate_proposal` | Quote/role/revision/source checks **before** shared validation; no semantic certainty from syntactic validation |
| Evaluation Request | **Shared `create_bound_evaluation_request`** | Source gate remains caller's responsibility |
| Retrieval | **Shared `build_bound_fresh_retrieval`** | External origin must not be represented as existing Vault note; retrieval alone cannot confirm user intent |
| Evaluation | Existing Evaluation Record and eventual model adapter | Conservative unknown/manual_review until corrections/user confirmation are independently evidenced |
| Human Review | Existing Review record, approval/reject UX and state machinery as design target | Bind source identity/revision at decision time and restrict approve until verified; never duplicate approval mechanism |
| Canonical Vault | Existing post-Human approval execution only | No automatic import/backfill, no auto-approve |

## What this PR concretely changes

Adds `pre_review_shared_artifacts.py` with two Source-neutral reusable
operations extracted from the currently functioning legacy Reader/Validator:

1. `create_bound_evaluation_request`: reuses the same deterministic accepted
   Validation → Evaluation Request CAS and exact mutation/proposal binding.
2. `build_bound_fresh_retrieval`: one locked Knowledge Index build and
   EvaluationContext CAS, checked against exact proposal, mutation **and**
   request SHA values, with the existing recipe's `top_k`.
3. `pre_review_job._awaiting_review_count_conn`: one authoritative
   latest-Generation-only SQLite count for **both** legacy Generator queue
   admission and External Source synthetic stage admission. This keeps
   backpressure semantics identical rather than maintaining two SQL queries.

`pre_review_worker.py` and the current synthetic External Worker both use
the shared operations. All preexisting exposed helper imports used by
synthetic legacy tests remain available as compatibility aliases.

No ContextBundle validation, existing Recipe format, stage state, Review
button, approval writer, `reconcile_post_review`, database schema or
production service configuration is broadened by this PR. This change
does not make `fake-e2e`/synthetic stages deployable.

## Why not simply queue External Source as legacy `context-bundle`?

The existing Job requires a Vault ContextBundle or semantic objective,
and its evaluator uses that contract as trusted provenance. Relabeling
a ChatGPT Export source as `11-Knowledge`, `10-Project`, or a generic
query would strip role/quote/original revision constraints and allow
model speculation to masquerade as verified project state.

The minimum acceptable adapter is therefore a **typed External Source
Context + immediate Source freshness binding**, not an additional
approval pipeline. The temporary external_* synthetic Stage names
in #428 are an isolation gate only while proof against legacy Worker
misclassification and real external evaluator input is incomplete.
Future work should collapse the separate dispatcher into common Job
execution once typed context and freshness guards are integrated;
do not prematurely delete these guards merely to reduce code.

## Human Review integration decision

The existing generic `create_evaluation_bound_review` intentionally
rejects the synthetic External Fake Evaluator. #428 review proved
the danger: without an external Source gate, that generic path could
create an authoritative `approve` record, potentially forwarded to
canonical execution even though original Source meaning is unknown.

A future typed adapter should **reuse** the existing Evaluation→
Validation→ReviewRecord CAS, Core Review controls, Reconcile and
Vault executor, adding an exact protected Source/revision check at
the real Human decision boundary. This is preferable to an unrelated
second approval UI or writer. The cross-UID Reviewer access contract
and safe user-confirmation/correction policy must be fixed before
enabling this path. For now no External `approve` route is added.

## Evidence and non-goals

Tests verify existing native Worker and synthetic External E2E behavior
after extracting the same bound Validation Request and fresh Retrieval
functions, negative mismatched mutation/proposal hashes, no canonical
Vault mutation and no implied Source or Human authority.

Remaining #411: true source-aware evaluator/semantic verification,
typed Human decision adapter and Reviewer UID/ACL, crash recovery,
controlled real ZIP pilot, WebUI, plus independent review. No merges
or production rollout without explicit Human approval.
