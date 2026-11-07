# Rejected Validation Cleanup — bounded preflight (Issue #309)

**State:** isolated Phase A; **no automatic deletion is implemented or enabled**.
Parent Dashboard integration branch: `feature/298-management-dashboard`.
The user's accepted product contract is **Review-only Obsidian UI**, with
operational incidents in the Automation management Dashboard.

## Why this is a separate authority

The existing `human_projection_cleanup.py` is a six-stage process bound to a
**Human Review decision** (reject/keep as idea) or an Executor terminal Receipt.
A deterministic Validator rejection has **neither** of those authorities.
Simply widening its allowlist or using the former Acknowledge path would let
one authorization bypass a different gate. #287 (Human Acknowledge) is closed.

The original Validator worker first emits a *private* Validation projection
request and only then commits `deterministic_reject` to the scheduler DB.
The Sync publisher writes human-facing documents and immutable publication
results independently and potentially later. A scheduler status or a
`04-AI/30-Validation` filename alone is **not** permission to delete.

## Phase A implementation

`src/obsidian_automation/validation_rejection_cleanup.py` provides:

- `prepare_rejection_cleanup(ai_root, case_id)`: read-only, fixed-case
  eligibility check; **no** WebDAV, producer invocation, Sync trigger or DELETE.
- It requires a **current** job generation whose DB state is
  `deterministic_reject`, with an authoritative completed Validation attempt
  `deterministic_reject / validation_rejected`, and a selected Generation
  stage output with the exact proposal SHA.
- It independently loads the immutable original `10-Validation` record,
  requires `result=rejected` and no accepted mutation, and reconstructs the
  original Validation Markdown from that record and the bound proposal. The
  original private Validator projection request must match **exactly**.
- Searches only fixed `16-Human-Projection/<role>` request directories, with
  a hard `4096` entry bound (configurable at the call site, max 16384).
  There must be exactly one original request for each of the four stages
  Input/Context/Generation/Validation, in the correct producer roles, with a
  successful matching immutable publication result (`created` or
  `already_matching`). Missing/unpublished/over-bound targets are **Pending**;
  duplicates, mismatched digests, unexpected later stages, mutated source,
  invalid role or state are **Blocked**.
- The resulting versioned, canonically serialized, SHA-bound attestation has
  exactly four fixed `04-AI` paths, the case/generation and job, original
  request and publication result hashes, and original content hashes.
  **No caller-supplied remote path or network URL** is accepted.
- `verify_rejection_cleanup` rebuilds the attestation from current
  authoritative local state. The scheduler is also rechecked after the
  projection scan to catch an in-progress regeneration during preflight.
- `store_rejection_cleanup_attestation` is **opt-in**, guarded by the existing
  canonical I/O lock, writes a private immutable case-named document in the
  Validator request area and is idempotent for unchanged evidence.
  `load_rejection_cleanup_attestation` only reads a stored proof.
  **A stored proof is not a remote deletion request.**
- No source projection, original publication evidence, immutable validation,
  attempt history, canonical Knowledge file or user-authored Review is removed.

This strict first slice intentionally requires **all four** original successful
publication results. Legacy cases that lack a stage, have a pending publication,
have multiple ambiguous originals, or exceed the bounded scan fail closed. This
is not a guarantee that all historical rejects are already eligible.

## Phase B: prohibited from activating until independently accepted

Before an actual Sync-only deletion can be connected, implement and test
**all** of the following separately:

1. Separate versioned *server-originated* immutable cleanup intent/result, with
   configured per-role identities/ACL, no GUI/Review delete capability. Build
   from Phase A evidence; do not trust the document alone.
2. Prove there is **no earlier queued/in-flight publisher or Sync replay** able
   to re-create any target after delete. Use a durable non-resurrection guard
   inspected by the publication path, not simply an in-process lock.
3. Under the same canonical-I/O authority boundary, recheck the scheduler,
   attestation, original immutable source publication results and all four
   targets. For every remotely present file, require exact original
   **byte-for-byte content** plus verified strong ETag; otherwise fail closed.
   Stage ordering should preserve Validation until earlier stages are settled.
4. Execute deletion *only* through Sync's narrowly scoped read+conditional
   delete transport using `If-Match: <strong-etag>`; require ETag-safe WebDAV
   behavior, post-delete verification, explicit HTTP 404/409/412/5xx handling,
   and no unauthorized fallbacks or generic rclone upload/delete semantics.
5. Persist idempotent per-target receipts, handle partial crash and lost
   DELETE responses, safe replay and remote resurrection. If the next read
   differs from expected bytes, **do not delete** it. Unknown/changed status
   becomes an actionable operations Dashboard incident, not silent cleanup.
6. Test fake and disposable authenticated WebDAV servers, adversarial remote
   edits, CAS conflict, symlink/path traversal, role privilege, interrupted
   publication, historical case collisions, malformed receipts and real
   end-to-end Vault projection behavior. Final Security/Correctness
   qualification and explicit human deployment approval are separate.

Never run this Phase A proof to infer that production credentials or live Vault
files may be deleted. The single-LXC ADR remains Proposed; final merge to
`main` is separately controlled by integration Draft PR #311. Likewise
ObsidianCore Review-only PR #221 must not be deployed before the management
Dashboard truly surfaces provider, cleanup, and pipeline incidents.

## Independent review hardening (Phase A revision)

Initial Correctness and Security reviews of commit `2b946a0` returned
**BLOCKED**, identifying an insufficient trust binding for the first three
projection stages and a bounded-scan/unsafe-file-read risk. The amended proof
therefore verifies:

- Input against the **immutable Selection** record, Context source bindings,
  selected policy and the original full producer Markdown. Legacy selections
  are checked against exact Context paths, content SHA and byte size.
- Context against the job's exact Context SHA, original canonical Context
  record (legacy or semantic-objective), its producer format and original
  Markdown. Semantic selection/chunk bindings are also compared to Context.
- Generation against the **selected Generation SHA in SQLite**, the
  corresponding hash-verified Generation record, the selected job Context,
  proposal SHA, semantic objective metadata where applicable, and original
  producer Markdown.
- Validation against its immutable rejected Validation record and proposal as
  before. A request that is correctly signed, published and case-labeled but
  points to another source is **not** an eligible original.
- A complete bounded scan of **all** directory entries (including junk names)
  and all four roles before raising Pending. A missing publication result never
  masks a later conflicting Review projection or duplicate stage.
- A nonblocking, `O_NOFOLLOW` reader using `fstat` on the **opened
  descriptor**, regular-file, one-hardlink and per-artifact byte limits,
  including request and published-result files. A path swapped to a FIFO,
  symlink or oversized file must not stall the canonical I/O lock.

These are local tests and source contracts, not proof of production Linux ACLs.
The existing production roles do not grant one role all Reader/Generator/
Validator/Sync and orchestration-DB read access. An **independently reviewed,
least-privilege evidence observer/producer identity and storage path** are
required before deploying the optional private proof-storing helper. Do not
grant Validator arbitrary cross-role reads or give Sync unbounded scheduler
database rights as a shortcut. A local Semantic Deep Planner → Generator → deterministic Validation rejection fixture now exercises the real source-producing chain with a simulated model transport and synthetic publication receipts. Real Nextcloud publication, mixed-role UID/ACL security, and crash/restart under concurrent writers remain required acceptance tests.

Re-validating a proof at a later effect boundary remains mandatory. It
does not by itself stop another publisher from re-creating a deleted note.
**No remote deletion is implemented by Phase A.**


## Job-bound Selection proof for new submissions

Independent Correctness review of the preceding amendment identified a remaining
Legacy identity ambiguity: two immutable Selection records can contain the same
Context sources but different policies, epochs or cycles. Merely verifying
source-byte equality does not establish which Selection was actually submitted
with the job.

The Reader Planner now records a small canonical, **private immutable**
`<generation>.submitted-selection-binding.json` in its existing
`16-Human-Projection/reader` request directory when projection generation is
enabled. The record binds exact job ID, generation ID, Context SHA,
Selection SHA, and recipe SHA. It is emitted **before** the Input request on
normal Legacy and Semantic Deep submissions, and on recovery of pending
submissions. It is *not* a Review note, a Sync publication request, a remote
delete instruction or a new credential.

Phase A re-reads this binding using the same bounded nofollow reader and
compares every identity with the latest-generation scheduler state and
original Input request. Tests verify that a second valid Selection with
identical Context sources but a different policy/cycle cannot replace the
**submitted** Input. An existing binding cannot be overwritten with a new
value via the immutable-store contract.

**Backward compatibility is intentionally fail-closed.** Historic Legacy
generations without a securely recorded job-to-Selection binding are **Pending**,
not automatically eligible for cleanup. No heuristic reconstruction, overwrite,
or silent backfill is performed. Future historical recovery would need
independent, exact original provenance evidence and a separate accepted
procedure.

The immutable Planner binding is an **application-level** evidence record,
not a cryptographic signature or server-enforced immutability guarantee against
a compromised Reader UID. The new least-privilege evidence observer and
per-role ACL design is tracked in #321; producer/consumer separation and
live security qualification remain necessary before Phase B.
