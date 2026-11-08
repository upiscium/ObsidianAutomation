# #303 V1/B — Nextcloud Vault-root read-only DAV adapter feasibility

Status: **source-only, synthetic-only prototype (Draft PR approved)**. This is
not an authenticated Nextcloud adapter, provider proof, new HTTP route, new credential
store, or permission to change the selected Vault. #303/#301/#304/#298 stay
open. V1/A is source-merged in PR #557; it remains an unwired preview contract.
The newer #298 UX-first one-LXC/one-shared-Nextcloud-account direction governs
this slice, not the older October mega-branch policy.

## Implementation boundary in the current slice

- `vault_root_dav_fixture.py` accepts **already captured, fabricated**
  Depth:1 DAV 207 Multi-Status XML plus explicitly fabricated source/session
  and classifier objects. It never invokes an HTTP client or opens any file,
  reads any secret, mutates any persisted connection/Job, or calls a writer.
- Only `origin="synthetic_fixture"` is admitted. Source and capture dataclasses
  are **claims supplied by the fixture**, not trusted/attested objects. They
  must **never be created from HTTP/browser-supplied JSON**. There is no real
  transport implementation or Dashboard import to mistakenly treat them as
  authenticated. A later production adapter requires separate source approval
  to accept verified real transport receipts.
- Synthetic source context binds account-local DAV prefix, namespace, canonical
  account-root resource ID, source-revision fence and a separately named
  listing revision. All frame claims must match the same session and exact
  expected parent chain. Equal revision strings do **not** establish a
  coherent provider snapshot; actual producer requirements are below.
- The pure XML parser requires canonical **path-only** hrefs under the exact
  account prefix, exactly one parent self response and complete direct
  children. It rejects external/absolute authority URLs, redirects, encoded
  slash/backslash, dot segments, nested/out-of-scope descendants, noncanonical
  percent encodings, insecure XML constructs, invalid property statuses,
  unsupported/missing IDs, duplicate hrefs, oversized/truncated/partial
  responses and unverified symlink/alias classifications. All failures are
  fixed redacted `RootPreview` codes.
- `oc:fileid` is a *candidate input identifier*, not authenticated evidence.
  A synthetic classification must match each child href **and** its file ID,
  and provide definite type and alias information. No ETag/path/hash/filename
  is synthesized into an ID. Unknown or missing classifications fail closed.
  V1/A then checks global reused identities and Unicode/casefold collisions
  across all sibling evidence before any `candidate_valid` response.
- The parser does not claim the content is safe to activate. V1/A always
  returns `writes_vault=False`, `activation_allowed=False`,
  `requires_explicit_activation=True`. The output must never trigger
  existing `POST /api/v1/nextcloud/connections` or a canonical Vault writer.

## Actual provider feasibility questions — still OPEN

Before writing a live adapter or claiming this parser is Nextcloud-compatible:

1. **Canonical account scope:** Find the service's *one* authenticated
   Nextcloud-only configuration without rendering or logging credentials.
   Canonicalize the actual DAV account base (including possible subdirectory
   deployments) once, disallow cross-origin redirects or re-authentication
   to a different host, and enforce Depth:1 and strict time/body budgets.
   Test 401/403/404/409/429/5xx and transport timeout/failure as redacted
   fail-closed states. The URL whitelist in this **synthetic fixture** is not
   a real deployment-base resolver.
2. **Identity semantics:** Inspect the provider's DAV `oc:fileid`, `oc:id`
   or supported equivalent from actual authenticated PROPFIND responses,
   including shares, external storage, mounts, renames and version changes.
   Determine whether the chosen canonical ID is **globally unique within the
   entire observed account root and stable for the preview-to-save window**.
   Separate on-server ID reuse from stable namespace identity. Do not infer
   this from `getetag`, href, a path hash, a generated UUID, a filename or
   mere string equality across two fake frames.
3. **Alias/symlink/mount classification:** Determine whether Nextcloud exposes
   authoritative resource-kind and alias status that detect shared folders,
   symlink-like collections, external mounts, hardlinks and redirect-like
   aliases. `DAV:resourcetype` proving `collection` is **not** proof that
   a folder is not an alias. Require a trusted server-side classifier or
   reject the candidate if classification remains unknown. Source V1/A's
   `is_alias=False` must never be guessed from absent XML.
4. **Complete and coherent enumeration:** Prove that each Depth:1 listing is
   complete (not truncated, partial or paginated); establish deterministic
   sibling count and bound bytes, time and rows. Directory ETag is not a
   guaranteed atomic tree generation. Confirm a provider capability, change
   token, or validated before/after re-listing strategy that detects
   concurrent rename/add/delete across **all** selected ancestor directories.
   If no coherent bounded read is possible, fail closed instead of producing
   apparently authoritative `DirectoryEvidence`.
5. **Setup and Job-state fences:** Derive an opaque source revision from the
   verified canonical Nextcloud configuration alone (not the existing
   `SetupStore.generation()` that expects full Gitea setup). Capture the
   applicable Snapshot, Daily, Formatter, B3/AI/Writer Jobs under the
   existing lock order for a future activation gate. A read-only preview
   cannot quiesce jobs, persist source bindings, or authorize writes.
6. **Serialization and API separation:** A future UI receives only fixed
   statuses and short-lived opaque server-bound candidate handles. Raw
   `DirectoryEvidence`, hrefs, source IDs, account names, private paths,
   config snapshots or credentials must never be returned to the browser.
   No credential-bearing request is accepted as a preview. Maintain
   Host/Origin/CSRF rules and bounded pagination if a read-only route is
   separately approved.
7. **Disposable proof before production:** Use an isolated, synthetic or
   disposable Nextcloud instance with explicit permission for actual
   read-only DAV I/O. Record version, supported properties, scope, alias and
   coherence behavior, stable identities and failures without recording
   credentials or sensitive paths. If any required guarantee is unavailable,
   mark V1/B provider integration **NYI/BLOCKED**, not `candidate_valid`.
   No production credentials may be used without separate authorization.

## Deliberately limited synthetic acceptances

Synthetic XML can verify parser logic and adapter-to-V1/A composition, but it
cannot verify HTTP authentication, real `oc:fileid` stability, account ACLs,
multi-request snapshot coherence, actual Nextcloud redirects, or provider
alias semantics. All source/classification/revision fields are fabricated
test claims. The fake provider URL prefix is not an official compatibility
promise for every Nextcloud deployment.

The current offline prototype accepts deliberately conservative canonical
absolute-path href spellings and a single synthetic root prefix. A real
Nextcloud provider's href escaping and prefix variants **must be separately
observed**; don't silently weaken path guards or decode twice to make it work.

## Next bounded stages and gates

- **V1/B0 (this slice):** Source-only no-I/O synthetic response parser and
  deterministic adapter-to-`preview_root` negative tests. Independent static
  review. Draft PR publication was separately authorized on 2026-11-08;
  no Ready/Merge, provider I/O, or activation follows from that authorization.
- **V1/B1:** Design, disposable-producer feasibility tests and authoritative
  provenance classifier on an explicitly approved *disposable* DAV fixture.
  No access to a live user vault. Separate owner approval for provider I/O.
- **V1/B2:** Harden a real read-only, authenticated, bounded service-side
  adapter against V1/B1 evidence. Exact source+inventory revision and
  generation revalidation. Additional owner approval for publication.
- **V1/C:** New read-only GUI candidate listing and preview endpoint, opaque
  server-owned IDs and redacted results; no existing credential-saving POST
  reuse. Separate code/PR/Ready/Merge and no activation.
- **V1/D:** Human-confirmed root update, CAS + all-writer quiescence +
  rollback. **Separate explicit activation gate** after complete E2E proof.
- **Phase 2:** Free logical-role-to-folder mapping deferred; do not rewrite
  immutable outstanding paths, receipts or human-authored notes.

No new LXC, no account proliferation, no live provider I/O, no Deploy, no
Root selection activation, no Run Once and no Job changes in V1/B0.
