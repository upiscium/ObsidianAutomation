# #303 V1/B0 — integrated synthetic-only Nextcloud DAV handoff

Status: **source-only synthetic prototype** originally reviewed at
`main@12c949ddd217`, then requalified against `main@527bfd7f58b8`.
Owner-authorized scope now includes reviewed Commit/Push/Draft PR publication.
No Ready/Merge/Deploy, production credentials, provider I/O, Vault access,
new Dashboard import/route, activation, Run Once, or Job changes.
Keep #303/#301/#304 and parent #298 OPEN.

## What exists in this worktree

- `vault_root_preview.py`: merged V1/A fail-closed deterministic validator, plus
  source-only hardening for exact input types and immediate-child names.
- `vault_root_dav_fixture.py`: **fabricated** Depth:1 DAV Multi-Status XML,
  source context and independent alias classifiers, parsed offline with strict
  href/status/type/size checks and delegated to V1/A. There is no transport.
- `vault_root_dav_adapter.py`: independent **fabricated internal verified-page**
  assembler with bounded cursor/byte/row limits, explicit identity/snapshot
  claims, and final delegation to V1/A. There is no authenticated verifier.
- `test_303_v1b_dav_fixture.py` and `test_303_v1b_dav_evidence_adapter.py`:
  isolated adversarial synthetic tests; no production account or real DAV URL.
- The two V1/B modules are **parallel test contracts**, not a connected,
  trusted production pipeline. Importing either into a route would cross an
  unapproved authority boundary. Dataclass fields such as `verified=True`,
  `origin=synthetic_fixture`, IDs, tokens and revisions can be fabricated.

Acceptance observed locally on 2026-11-08 JST (pinned combined worktree):
- First three #303 synthetic suites: **215 passed**.
- B0 synthetic fixture boundary hardening: reject malformed str subclasses
  before scoped comparisons in the source/captures/selection/classifier.
  Added 12 explicit hostile-comparison regressions; no production changes.
- V1/A provenance regression found in local read-only adversarial proof:
  a `str` subclass overriding comparisons in `DirectoryEvidence.namespace`
  could make a mismatched namespace pass as `candidate_valid`. Selected
  `Resource.kind` subclass could also spoof a directory and a malformed
  `RootSelection.requested` could raise during `!=`. The V1/B producers
  already checked their own inputs, but V1/A's internal guard was incomplete.
- Added 19 V1/A regression cases for scope/parent/revision exact types, kind
  enum and path comparisons; **18 RED / 1 PASS before correction**.
  Local V1/A correction checks exact types BEFORE comparing these fields and
  bounds the evidence path; no activation authority added.
- First independent GPT-6 Astra offline source review on the pre-correction
  immutable prompt (`v1b-b0-independent-review-20261108.txt`) found P2
  malformed/nested `DAV:collection` acceptance and the already reproduced
  V1/A dataclass type-spoofing gap, plus P3 UTF-8 declaration prefix bypass.
  All were locally corrected. XML regression proof: **14 RED / 1 PASS**
  before changes; strict marker structure and full bounded-declaration
  matching now reject those malformed envelopes.
- A second, focused GPT-6 Astra-fast review of the V1/A comparison hardening
  accepted that correction and identified P3 acceptance of malformed
  **unselected immediate-child** names (slash, dot, dot-dot, NUL). V1/A now
  rejects these without banning legitimate Unicode names, including via
  the V1/B verified-page path. The ten regression cases were **10 RED**
  before the small correction.
- Final six-suite local command, using `nix develop --offline --command env
  PYTHONPATH=src python -m pytest -q` against #303 V1/A, both #303 V1/B
  suites, Nextcloud-only setup, Snapshot setup and #301 Gitea TOCTOU:
  **292 passed**, and again **292 passed** after the final test-only
  correction to target the actual selected-child XML marker
  (`v1b-b0-exact-final-tests-20261108.log`).
- Final independent GPT-6 Astra-fast **source-only** static re-review of
  the post-correction preview and synthetic XML parser
  (`v1b-b0-final-independent-re-review-20261108.txt`) found all four
  reported issues resolved and **ZERO NEW ACTIONABLE P0–P3 FINDINGS**.
  The reviewer did not execute tests, perform provider checks, inspect CI,
  or grant GitHub approval; the separate evidence assembler was included
  in the earlier broad review, but not re-pasted in the final narrow review.
- Final review noted that a selected-child XML test had originally mutated
  the parent SELF marker instead. The test now explicitly mutates the
  selected child and was rerun with the full six-suite selection: 292 PASS.
This is synthetic source correctness evidence only, not a provider
compatibility statement, repository-wide CI or a production approval.
- Latest-main SHA-pinned integration on `main@527bfd7f58b8`: the 9 candidate
  files are byte-identical to reviewed source and latest-main drift adds
  only 4 unrelated Project Watch / Phase Schedule source/test files. Eight
  selected regression suites (including both new feature tests) returned
  **396 passed / 0 failed** on the disposable main-pinned worktree
  (`v1b-main527bfd-related-tests-20261108.log`). Full CI still requires
  independent published PR HEAD qualification.
Human source-publication authorization was granted on 2026-11-08, limited
to Commit/Push/Draft PR; independent Ready/Merge/Deploy/activation gates remain.

## Mandatory provenance chain for a future real V1/B provider verifier

1. **Account authority** — Read exactly the configured shared Nextcloud account
   from the already trusted, service-private `SetupStore` record; derive a
   short-lived, opaque Nextcloud-only source revision without exposing hashes
   of secrets or reusing Gitea-dependent `SetupStore.generation()`. Bind the
   verified remote instance, authenticated DAV account base, effective scope,
   and server-side candidate selection together. No browser-supplied evidence.
2. **Transport authority** — Explicit read-only `PROPFIND Depth:1` requests
   only, no redirect following or credential forwarding; require same origin,
   HTTPS, per-call deadline and a cumulative time budget, capped raw bytes
   **during stream read**, XML allocation cap, HTTP 207 and individually
   successful property statuses. Reject 401/403/404/409/429/5xx, timeouts,
   3xx, partial 207, malformed/ambiguous percent escaping, absolute external
   hrefs, duplicate/self-missing rows, and out-of-root descendants. A response
   header reporting a small size is not proof of bounded body consumption.
3. **Resource identity** — Nextcloud documents `oc:fileid` as unique within
   its instance and `oc:id` as file ID namespaced by instance ID (globally
   unique). Compare both on a disposable provider where available; establish
   actual scope, stability, changes under rename/move/re-share and external
   mounts. Neither documented name alone proves alias equivalence or safety.
   Obtain a trusted server-side classification of directory/file/symlink,
   aliases/shares/external mounts for **every** observed child (selected and
   unselected). Evaluate Nextcloud's documented `nc:mount-type` and
   `nc:is-mount-root` as possible inputs to a trusted classifier, not as
   stand-alone proof of absence of aliasing. `DAV:resourcetype`, href, ETag,
   path hashes and missing mount flags cannot be silently promoted into
   that classification.
4. **Complete inventory** — Verify an exhaustive listing of every immediate
   sibling at each selected level, including the parent self record, with
   strict row/byte/level caps. If the provider cannot prove completeness
   within the cap, return a redacted denial rather than silently truncate.
   Cursor pagination is only admissible with documented authoritative
   cursor/coherence semantics; synthetic tokens do not create such semantics.
5. **Coherent directory snapshot** — Distinguish connection revision from
   remote directory mutation epoch. Verify actual source and directory
   consistency across **all** levels and pages; exercise concurrent create,
   delete, rename, share/mount and source rotation. Matching ETag strings or
   client-computed revision labels are not proof of an atomic snapshot.
   If a supported provider epoch or a sufficient robust revalidation
   contract cannot be demonstrated, keep `snapshot_unverified`/NYI.
6. **Pure decision boundary** — Only a trusted verifier may build
   `DirectoryEvidence`; then V1/A enforces exact parent/namespace/revision,
   global ID uniqueness, alias/symlink rejection, casefold/Unicode collisions,
   active Job fence and legacy active-root handling. Every result remains
   `writes_vault=false`, `activation_allowed=false`, with fixed redacted
   reason fields. A `candidate_valid` is an **advisory** preview, never an
   invocation of the existing effectful Nextcloud POST.

## Disposable feasibility proof matrix (separate future approval required)

- Record exact Nextcloud version, DAV property support, HTTP/proxy behavior,
  scopes and authenticated account base for a dedicated **disposable** fixture.
  Do not use production account credentials or real Vault contents.
- Repeat directory enumeration across restarts; rename/move within and across
  parents; same-instance shares, mount/unmount, re-share, external storage,
  aliases and ambiguous identity types. Compare `oc:id`/`oc:fileid`,
  `DAV:resourcetype`, `oc:permissions`, ETags and actual observed identity.
- Inject 207 partial propstats (404/403 for required properties), HTTP
  401/403/404/409/429/5xx, redirects, timeouts, slow/oversized responses,
  encoded traversal, duplicate href/ID, casefold collisions, truncated and
  reordered listings, concurrent rename/delete and credential rotation.
- For each scenario, prove exact denied/admitted scope, no secret/path/ID
  reflection to clients, zero remote mutation, predictable bounded resource
  usage, and absence of cross-account or cross-mount identity confusion.
- If a provider configuration lacks reliable canonical identity, complete
  enumeration, alias classification or coherent revalidation: **fail closed,
  mark V1/B provider integration BLOCKED**, and do not add a compatibility
  fallback that invents identities or weakens V1/A invariants.

Nextcloud public references (for design only; no provider calls):
- https://docs.nextcloud.com/server/stable/developer_manual/client_apis/WebDAV/basic.html
- https://docs.nextcloud.com/server/stable/user_manual/en/files/access_webdav.html

## Separate authorization gates

- **V1/B0 now:** offline synthetic-only two-module source; isolated tests
  and scoped independent reviews. Reviewed Commit/Push/Draft PR approved;
  no Ready/Merge/Deploy or provider I/O.
- **V1/B1 later:** explicit owner approval for disposable instance READ-only
  feasibility, actual authenticated identity/alias/coherence proof.
- **V1/B2 later:** source implementation of a real bounded service-side
  verifier, under distinct security review and human publication gate.
- **V1/C later:** redacted, private candidate-picker UI and read-only API;
  only opaque server-side handles; never reuse credential-saving POST.
- **V1/D later:** source+directory CAS, all-affected Job quiescence, rollback,
  owner-confirmed activation and separate Deploy/activation authority.
- **Phase 2 deferred:** versioned logical role-to-folder mappings, immutable
  receipt/review-path compatibility and no silent canonical writes.
